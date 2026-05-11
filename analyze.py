"""Local harness for iterating on the conversation-analyzer kagent prompt.

Mirrors the deployed kagent settings (gitops):
  - provider:        OpenAI
  - model:           gpt-5.4              (deployment/argocd/applications/infra/prod-kagent.yaml)
  - reasoningEffort: high
  - stream:          false                (agents/conversation-analyzer.yaml)
  - tools exposed:   query_loki_logs, list_loki_label_names

Uses the OpenAI Responses API (`/v1/responses`) — Chat Completions does not
support function tools combined with `reasoning_effort` on gpt-5.x.

Usage:
    python analyze.py --call-id <uuid> [--prompt prompts/baseline.md] [--question "..."] [--note "..."]

Outputs:
    runs/<timestamp>__<call_id>__<prompt_name>.json — full input, prompt sha,
    tool-call trace, final structured JSON, model + reasoning_effort, duration,
    and a free-form `note` field for human grading.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv

from context_loaders import call_db, conv_graph
from mcp_clients import open_hub

ROOT = Path(__file__).parent
PROMPTS_DIR = ROOT / "prompts"
RUNS_DIR = ROOT / "runs"

GRAFANA_TOOL_ALLOWLIST = {"query_loki_logs", "list_loki_label_names"}
DB_TOOL_ALLOWLIST = {"prod_execute_sql"}

# Insertion order matters — loaders run in this order, and later loaders may
# read earlier loaders' output via the shared `context` kwarg.
LOADER_REGISTRY = {
    "call": call_db.load,
    "conv_graph": conv_graph.load,
}
MANDATORY_LOADERS = {"call"}


@dataclass
class TraceEntry:
    kind: str
    payload: Any
    dt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RunResult:
    call_id: str
    prompt_path: str
    prompt_sha256: str
    model: str
    reasoning_effort: str
    context_loaders: list[str]
    started_at: str
    duration_seconds: float
    context: dict[str, Any]
    user_message: str
    trace: list[dict[str, Any]]
    final_json: dict[str, Any] | None
    final_text: str
    status: str | None
    iterations: int
    note: str
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the conversation-analyzer prompt against a real call.")
    p.add_argument("--call-id", required=True, help="UUID of the call to analyze.")
    p.add_argument(
        "--prompt",
        default=str(PROMPTS_DIR / "baseline.md"),
        help="Path to the system prompt file (default: prompts/baseline.md).",
    )
    p.add_argument("--note", default="", help="Free-form note saved alongside the run output.")
    p.add_argument(
        "--question",
        default="",
        help=(
            "Optional user-facing question/symptom (e.g. 'TTS cut off mid-sentence'). "
            "The prompt routes service scope from this — leave blank for the DEFAULT scope."
        ),
    )
    p.add_argument("--model", default=None, help="Override OPENAI_MODEL (default: gpt-5.4 to match kagent).")
    p.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high"],
        default=None,
        help="Override OPENAI_REASONING_EFFORT (default: high to match kagent).",
    )
    p.add_argument("--max-output-tokens", type=int, default=16384)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Load context only; print the user message and tool list, then exit without invoking OpenAI.",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress progress lines on stderr.")
    optional_loaders = sorted(set(LOADER_REGISTRY) - MANDATORY_LOADERS)
    p.add_argument(
        "--context",
        default="",
        help=(
            f"Comma-separated optional context loaders to include. "
            f"Available: {', '.join(optional_loaders) or '(none)'}. "
            f"Mandatory loaders ({', '.join(sorted(MANDATORY_LOADERS))}) always run. "
            f"Example: --context conv_graph"
        ),
    )
    return p.parse_args()


_QUIET = False


def _log(msg: str) -> None:
    if not _QUIET:
        print(msg, file=sys.stderr, flush=True)


def _summarise_args(args_dict: dict[str, Any], width: int = 80) -> str:
    s = json.dumps(args_dict, default=str, separators=(",", ":"))
    return s if len(s) <= width else s[: width - 1] + "…"


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n / (1024 * 1024):.2f} MB"


def _count_loki_lines(out: str) -> int | None:
    """Best-effort line count from a query_loki_logs MCP result.

    The grafana MCP server's exact output shape isn't pinned, so we try
    several candidates: a flat JSON list, a Loki HTTP-API-style dict
    (`{"data": {"result": [{"values": [[ts, line], ...]}, ...]}}` or
    `{"streams": [...]}`), then a newline-counting fallback.
    Returns None when nothing parses cleanly.
    """
    if not out:
        return 0
    s = out.strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        non_empty = sum(1 for ln in s.splitlines() if ln.strip())
        return non_empty or None

    if isinstance(parsed, list):
        return len(parsed)

    if isinstance(parsed, dict):
        for path in (("data", "result"), ("result",), ("streams",)):
            cur: Any = parsed
            ok = True
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    ok = False
                    break
                cur = cur[k]
            if ok and isinstance(cur, list):
                if cur and isinstance(cur[0], dict) and "values" in cur[0]:
                    return sum(len(stream.get("values", []) or []) for stream in cur)
                return len(cur)
    return None


async def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env")

    global _QUIET
    _QUIET = args.quiet

    api_key = os.getenv("OPENAI_API_KEY")
    model = args.model or os.getenv("OPENAI_MODEL", "gpt-5.4")
    reasoning_effort = args.reasoning_effort or os.getenv("OPENAI_REASONING_EFFORT", "high")
    db_url = os.getenv("DB_TOOLBOX_MCP_URL")
    grafana_url = os.getenv("GRAFANA_MCP_URL")
    buffer_minutes = int(os.getenv("LOOKUP_BUFFER_MINUTES", "5"))
    max_iterations = int(os.getenv("MAX_AGENT_ITERATIONS", "25"))

    missing = [n for n, v in (
        ("OPENAI_API_KEY", api_key),
        ("DB_TOOLBOX_MCP_URL", db_url),
        ("GRAFANA_MCP_URL", grafana_url),
    ) if not v]
    if missing and not args.dry_run:
        sys.stderr.write(f"missing env vars: {', '.join(missing)}\n")
        return 2

    prompt_path = Path(args.prompt).resolve()
    if not prompt_path.exists():
        sys.stderr.write(f"prompt file not found: {prompt_path}\n")
        return 2
    system_prompt = prompt_path.read_text()
    prompt_sha = hashlib.sha256(system_prompt.encode()).hexdigest()

    started_wall = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    _log("[init] connecting to MCP servers (db-toolbox-prod, grafana-prod-vpn)…")
    async with open_hub(
        db_url=db_url or "",
        grafana_url=grafana_url or "",
        grafana_tool_allowlist=GRAFANA_TOOL_ALLOWLIST,
        db_tool_allowlist=DB_TOOL_ALLOWLIST,
    ) as hub:
        _log(f"[init] connected — {len(hub.tools)} MCP tool(s) available")

        extra = {x.strip() for x in args.context.split(",") if x.strip()}
        unknown = extra - LOADER_REGISTRY.keys()
        if unknown:
            sys.stderr.write(
                f"unknown context loaders: {sorted(unknown)} "
                f"(available: {sorted(LOADER_REGISTRY)})\n"
            )
            return 2
        to_run = MANDATORY_LOADERS | extra
        selected_loaders = [k for k in LOADER_REGISTRY if k in to_run]
        _log(f"[init] context loaders: {selected_loaders}")

        context: dict[str, Any] = {}
        for key in selected_loaders:
            _log(f"[init] running context loader: {key}")
            t_load = time.monotonic()
            kwargs: dict[str, Any] = {"context": context}
            if key == "call":
                kwargs["buffer_minutes"] = buffer_minutes
            context[key] = await LOADER_REGISTRY[key](hub, args.call_id, **kwargs)
            _log(f"[init] loader {key} done in {time.monotonic() - t_load:.1f}s")

        call_info = (context.get("call", {}) or {}).get("call", {}) or {}
        time_range = (context.get("call", {}) or {}).get("loki_time_range", {}) or {}
        _log(
            f"[init] call: status={call_info.get('status')!r} "
            f"assistant={call_info.get('assistant_id')!r} "
            f"telecom={call_info.get('telecom_provider')!r}"
        )
        _log(f"[init] loki window: {time_range.get('start')} → {time_range.get('end')}")

        user_message = build_user_message(args.call_id, context, args.question)

        if args.dry_run:
            print(user_message)
            print("\n--- tools exposed to the model ---")
            for t in hub.tools:
                if t.server == "grafana-prod-vpn":
                    print(f"- {t.name}")
            return 0

        client = openai.OpenAI(api_key=api_key)
        tools_payload = [t.to_openai() for t in hub.tools if t.server == "grafana-prod-vpn"]
        common_kwargs = {
            "model": model,
            "tools": tools_payload,
            "reasoning": {"effort": reasoning_effort},
            "parallel_tool_calls": True,
            "max_output_tokens": args.max_output_tokens,
        }

        trace: list[TraceEntry] = []
        trace.append(TraceEntry(kind="system", payload={"prompt_sha256": prompt_sha, "length": len(system_prompt)}))
        trace.append(TraceEntry(kind="user", payload=user_message))

        status: str | None = None
        final_text = ""
        iterations = 0
        error: str | None = None

        try:
            iterations += 1
            _log(f"[turn {iterations}] requesting {model} (reasoning={reasoning_effort})…")
            t_turn = time.monotonic()
            response = client.responses.create(
                instructions=system_prompt,
                input=[{"role": "user", "content": user_message}],
                **common_kwargs,
            )
            while True:
                status = response.status
                usage = _usage_to_dict(getattr(response, "usage", None))
                function_calls = [
                    item for item in (response.output or [])
                    if getattr(item, "type", None) == "function_call"
                ]
                text_parts: list[str] = []
                for item in (response.output or []):
                    if getattr(item, "type", None) == "message":
                        for block in (getattr(item, "content", []) or []):
                            if getattr(block, "type", None) == "output_text":
                                text_parts.append(getattr(block, "text", ""))

                _log(
                    f"[turn {iterations}] response in {time.monotonic() - t_turn:.1f}s, "
                    f"status={status}, {len(function_calls)} tool call(s), "
                    f"usage in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
                    f"reasoning={usage.get('reasoning_tokens')}"
                )

                trace.append(TraceEntry(
                    kind="assistant",
                    payload={
                        "response_id": response.id,
                        "status": status,
                        "incomplete_details": _maybe_dump(getattr(response, "incomplete_details", None)),
                        "output": [_maybe_dump(item) for item in (response.output or [])],
                        "usage": usage,
                    },
                ))

                if not function_calls:
                    final_text = "".join(text_parts)
                    break

                next_input: list[dict[str, Any]] = []
                for fc in function_calls:
                    raw_args = getattr(fc, "arguments", "") or "{}"
                    try:
                        tool_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        tool_args = {}
                    requested_limit = tool_args.get("limit") if isinstance(tool_args, dict) else None
                    _log(f"[turn {iterations}]   → {fc.name}({_summarise_args(tool_args)})")
                    t_tool = time.monotonic()
                    try:
                        out = await hub.call_tool(fc.name, tool_args)
                    except Exception as e:  # noqa: BLE001
                        out = f"[tool dispatch error] {type(e).__name__}: {e}"
                    tool_dt = time.monotonic() - t_tool
                    out_bytes = len(out.encode("utf-8")) if isinstance(out, str) else 0

                    if fc.name == "query_loki_logs":
                        line_count = _count_loki_lines(out) if isinstance(out, str) else None
                        if line_count is None:
                            lines_str = "? lines"
                        elif isinstance(requested_limit, int):
                            saturated = " SATURATED" if line_count >= requested_limit else ""
                            lines_str = f"{line_count}/{requested_limit} lines{saturated}"
                        else:
                            lines_str = f"{line_count} lines"
                        _log(
                            f"[turn {iterations}]   ← {fc.name}: {lines_str}, "
                            f"{_human_bytes(out_bytes)} in {tool_dt:.1f}s"
                        )
                    else:
                        _log(
                            f"[turn {iterations}]   ← {fc.name}: "
                            f"{_human_bytes(out_bytes)} in {tool_dt:.1f}s"
                        )

                    trace.append(TraceEntry(
                        kind="tool_result",
                        payload={
                            "tool": fc.name,
                            "call_id": fc.call_id,
                            "input": tool_args,
                            "output": out,
                            "duration_seconds": round(tool_dt, 3),
                            "output_bytes": out_bytes,
                        },
                    ))
                    next_input.append({
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": out,
                    })

                if iterations >= max_iterations:
                    error = f"hit max_iterations={max_iterations} without final answer"
                    break
                iterations += 1
                _log(f"[turn {iterations}] requesting {model} (reasoning={reasoning_effort})…")
                t_turn = time.monotonic()
                response = client.responses.create(
                    previous_response_id=response.id,
                    input=next_input,
                    **common_kwargs,
                )
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"

        final_json = _try_parse_json(final_text)

        result = RunResult(
            call_id=args.call_id,
            prompt_path=str(prompt_path.relative_to(ROOT) if prompt_path.is_relative_to(ROOT) else prompt_path),
            prompt_sha256=prompt_sha,
            model=model,
            reasoning_effort=reasoning_effort,
            context_loaders=selected_loaders,
            started_at=started_iso,
            duration_seconds=round(time.monotonic() - started_wall, 3),
            context=context,
            user_message=user_message,
            trace=[asdict(e) for e in trace],
            final_json=final_json,
            final_text=final_text,
            status=status,
            iterations=iterations,
            note=args.note,
            error=error,
        )

        out_path = save_run(result, prompt_path)
        rel = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
        _log(
            f"[done] {iterations} turn(s), {result.duration_seconds:.1f}s total, "
            f"status={status} → {rel}"
        )
        print(f"\nrun saved to: {out_path}")
        if final_json is not None:
            print(json.dumps(final_json, indent=2, default=str))
        else:
            print(final_text or "(no final text)")
        if error:
            print(f"\n[error] {error}", file=sys.stderr)
            return 1
        return 0


def build_user_message(call_id: str, context: dict[str, Any], question: str) -> str:
    call = context.get("call") or {}
    call_row = call.get("call") or {}
    time_range = call.get("loki_time_range") or {}

    question_block = (
        f"Question / symptom: {question.strip()}"
        if question.strip()
        else "Question / symptom: (none — broad investigation)"
    )

    parts: list[str] = [
        f"Analyze call_id={call_id}.",
        "",
        question_block,
        "",
        "Loki time range to use:",
        f"  start: {time_range.get('start')}",
        f"  end:   {time_range.get('end')}",
        "",
        "Call row from prod DB:",
        json.dumps(call_row, indent=2, default=str),
    ]

    for key, payload in context.items():
        if key == "call":
            continue
        parts.append("")
        parts.append(f"--- context: {key} ---")
        parts.append(json.dumps(payload, indent=2, default=str))

    parts.append("")
    parts.append("Investigate per the system prompt and return only the JSON object.")
    return "\n".join(parts)


def save_run(result: RunResult, prompt_path: Path) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompt_name = prompt_path.stem
    safe_call_id = result.call_id.replace("/", "_")

    # Encode the optional context loaders into the filename so two runs of the
    # same prompt against the same call but with different context are easy to
    # tell apart at a glance. Mandatory loaders are implicit and elided.
    optional = [k for k in result.context_loaders if k not in MANDATORY_LOADERS]
    ctx_suffix = f"__{'+'.join(optional)}" if optional else ""

    out_path = RUNS_DIR / f"{ts}__{safe_call_id}__{prompt_name}{ctx_suffix}.json"
    out_path.write_text(json.dumps(asdict(result), indent=2, default=str))
    return out_path


def _maybe_dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:  # noqa: BLE001
            pass
    return repr(obj)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    out: dict[str, Any] = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    details = getattr(usage, "output_tokens_details", None)
    if details is not None:
        out["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)
    return out


def _try_parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
