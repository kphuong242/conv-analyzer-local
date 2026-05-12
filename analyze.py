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
    runs/<call_id>__<prompt_name>__<ctx>__<ts>.json — full input, prompt
    commit sha (gitops main), tool-call trace, final structured JSON,
    model + reasoning_effort, duration, and a free-form `note` field for
    human grading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openai
import yaml
from dotenv import load_dotenv

import code_tools
from context_loaders import call_db, conv_graph, ticket
from mcp_clients import open_hub

ROOT = Path(__file__).parent
PROMPTS_DIR = ROOT / "prompts"
GITOPS_PROMPTS_DIR = PROMPTS_DIR / "gitops"
DEFAULT_PROMPT = GITOPS_PROMPTS_DIR / "conversation-analyzer.yaml"
SYNC_SCRIPT = ROOT / "scripts" / "sync-prompt.sh"
SYNC_CODE_REPOS_SCRIPT = ROOT / "scripts" / "sync-code-repos.py"
RUNS_DIR = ROOT / "runs"

GRAFANA_TOOL_ALLOWLIST = {"query_loki_logs", "list_loki_label_names"}
DB_TOOL_ALLOWLIST = {"prod_execute_sql"}

# Run timestamps (started_at, trace entries, filename) are recorded in Paris
# local time. Loki query times in context_loaders/call_db.py stay UTC because
# Grafana / Loki interpret RFC3339 by offset — the harness's own logs being
# local just makes them easier to skim.
LOCAL_TZ = ZoneInfo("Europe/Paris")

# Insertion order matters — loaders run in this order, and later loaders may
# read earlier loaders' output via the shared `context` kwarg.
LOADER_REGISTRY = {
    "call": call_db.load,
    "conv_graph": conv_graph.load,
    "ticket": ticket.load,
}
MANDATORY_LOADERS = {"call"}

# Optional features that aren't data loaders — they toggle extra tools
# exposed to the model. Selected via the same --context flag for uniformity.
OPTIONAL_TOOL_FEATURES = {"code"}


@dataclass
class TraceEntry:
    kind: str
    payload: Any
    dt: str = field(default_factory=lambda: datetime.now(LOCAL_TZ).isoformat())


@dataclass
class RunResult:
    call_id: str
    prompt_path: str
    prompt_commit_sha: str | None
    model: str
    reasoning_effort: str
    context_loaders: list[str]
    code_tools_enabled: bool
    tool_usage: dict[str, int]
    started_at: str
    duration_seconds: float
    context: dict[str, Any]
    user_message: str
    trace: list[dict[str, Any]]
    final_json: dict[str, Any] | None
    final_text: str
    derived_route_trace: str | None
    derived_route_trace_steps: list[dict[str, Any]] | None
    status: str | None
    iterations: int
    note: str
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the conversation-analyzer prompt against a real call.")
    p.add_argument("--call-id", required=True, help="UUID of the call to analyze.")
    p.add_argument(
        "--prompt",
        default=str(DEFAULT_PROMPT),
        help=(
            "Path to the system prompt file. Supports .yaml (extracts "
            "spec.declarative.systemMessage) and .md (raw). "
            f"Default: {DEFAULT_PROMPT.relative_to(ROOT)} (auto-synced from gitops)."
        ),
    )
    p.add_argument(
        "--no-sync",
        action="store_true",
        help=(
            "Skip the auto-sync from gitops before the run. "
            "Only relevant when --prompt is inside prompts/gitops/."
        ),
    )
    p.add_argument(
        "--no-code-sync",
        action="store_true",
        help=(
            "Skip the auto-sync of code-repos/ before the run. "
            "Only relevant when --context code is enabled."
        ),
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
    optional = sorted((set(LOADER_REGISTRY) - MANDATORY_LOADERS) | OPTIONAL_TOOL_FEATURES)
    p.add_argument(
        "--context",
        default="",
        help=(
            f"Comma-separated optional features to enable for this run. "
            f"Available: {', '.join(optional) or '(none)'}. "
            f"Loaders inject data into the user message; tool features "
            f"(e.g. 'code') expose extra tools to the model. "
            f"Mandatory loaders ({', '.join(sorted(MANDATORY_LOADERS))}) always run. "
            f"Example: --context conv_graph,code"
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


def _tool_tag(name: str) -> str:
    """Short marker indicating which subsystem a tool belongs to."""
    if name in code_tools.DISPATCH:
        return "code"
    if name in GRAFANA_TOOL_ALLOWLIST:
        return "loki"
    return "mcp"


def _format_usage(usage: dict[str, int]) -> str:
    """Group tool usage by tag for a readable summary."""
    by_tag: dict[str, dict[str, int]] = {}
    for name, count in sorted(usage.items()):
        tag = _tool_tag(name)
        by_tag.setdefault(tag, {})[name] = count
    parts: list[str] = []
    for tag in ("loki", "code", "mcp"):
        if tag in by_tag:
            inner = ", ".join(f"{n}={c}" for n, c in by_tag[tag].items())
            total = sum(by_tag[tag].values())
            parts.append(f"{tag}={total} ({inner})")
    return "; ".join(parts) if parts else "(no tool calls)"


def _sync_code_repos() -> None:
    """Run scripts/sync-code-repos.py inline.

    Warns (not fails) on errors. The `code_tools.available()` check that
    follows is what hard-stops the run if code-repos/ ends up empty.
    """
    if not SYNC_CODE_REPOS_SCRIPT.exists():
        _log(f"[sync] {SYNC_CODE_REPOS_SCRIPT.relative_to(ROOT)} not found — skipping code sync")
        return
    try:
        result = subprocess.run(
            [sys.executable, str(SYNC_CODE_REPOS_SCRIPT)],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _log("[sync] code sync timed out after 120s — proceeding with existing code-repos/")
        return
    if result.returncode != 0:
        _log(f"[sync] code sync exited {result.returncode} — proceeding with existing code-repos/")


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
    max_iterations = int(os.getenv("MAX_AGENT_ITERATIONS", "10"))

    missing = [n for n, v in (
        ("OPENAI_API_KEY", api_key),
        ("DB_TOOLBOX_MCP_URL", db_url),
        ("GRAFANA_MCP_URL", grafana_url),
    ) if not v]
    if missing and not args.dry_run:
        sys.stderr.write(f"missing env vars: {', '.join(missing)}\n")
        return 2

    _log(
        f"[init] call_id={args.call_id} model={model} "
        f"reasoning={reasoning_effort} dry_run={args.dry_run}"
    )

    prompt_path = Path(args.prompt).resolve()
    if _is_gitops_prompt(prompt_path):
        if args.no_sync:
            _log("[sync] skipped (--no-sync)")
        else:
            _log("[sync] checking gitops for updates…")
            _run_sync_prompt()
    else:
        rel_gitops = GITOPS_PROMPTS_DIR.relative_to(ROOT)
        _log(f"[sync] skipped (prompt is outside {rel_gitops}/)")
    if not prompt_path.exists():
        sys.stderr.write(f"prompt file not found: {prompt_path}\n")
        return 2
    system_prompt, prompt_commit_sha = load_prompt(prompt_path)
    try:
        prompt_rel = prompt_path.relative_to(ROOT)
    except ValueError:
        prompt_rel = prompt_path
    _log(
        f"[init] prompt: {prompt_rel} "
        f"(commit={(prompt_commit_sha or 'none')[:8]}, "
        f"chars={len(system_prompt)})"
    )

    started_wall = time.monotonic()
    # Drop microseconds — filename uses the same instant and stays second-resolution.
    started_iso = datetime.now(LOCAL_TZ).replace(microsecond=0).isoformat()
    _log(f"[init] started_at={started_iso}")

    _log("[init] connecting to MCP servers (db-toolbox-prod, grafana-prod-vpn)…")
    async with open_hub(
        db_url=db_url or "",
        grafana_url=grafana_url or "",
        grafana_tool_allowlist=GRAFANA_TOOL_ALLOWLIST,
        db_tool_allowlist=DB_TOOL_ALLOWLIST,
    ) as hub:
        _log(f"[init] connected — {len(hub.tools)} MCP tool(s) available")

        extra = {x.strip() for x in args.context.split(",") if x.strip()}
        unknown = extra - LOADER_REGISTRY.keys() - OPTIONAL_TOOL_FEATURES
        if unknown:
            available = sorted(LOADER_REGISTRY.keys() | OPTIONAL_TOOL_FEATURES)
            sys.stderr.write(f"unknown --context names: {sorted(unknown)} (available: {available})\n")
            return 2

        loader_extra = extra & LOADER_REGISTRY.keys()
        to_run = MANDATORY_LOADERS | loader_extra
        selected_loaders = [k for k in LOADER_REGISTRY if k in to_run]
        _log(f"[init] context loaders: {selected_loaders}")

        code_tools_enabled = "code" in extra
        if code_tools_enabled:
            if args.no_code_sync:
                _log("[sync] code-repos sync skipped (--no-code-sync)")
            else:
                _sync_code_repos()
        if code_tools_enabled and not code_tools.available():
            sys.stderr.write(
                "[error] --context code requested but no repos under code-repos/. "
                "Run: python scripts/sync-code-repos.py\n"
            )
            return 2

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

        if code_tools_enabled:
            _log(f"[init] code-reading tools enabled — repos: {code_tools._available_repos()}")

        if args.dry_run:
            print(user_message)
            print("\n--- tools exposed to the model ---")
            for t in hub.tools:
                if t.server == "grafana-prod-vpn":
                    print(f"- {t.name} (grafana-prod-vpn)")
            if code_tools_enabled:
                for t in code_tools.TOOLS:
                    print(f"- {t['name']} (local)")
            return 0

        client = openai.OpenAI(api_key=api_key)
        tools_payload = [t.to_openai() for t in hub.tools if t.server == "grafana-prod-vpn"]
        if code_tools_enabled:
            tools_payload.extend(code_tools.TOOLS)
        common_kwargs = {
            "model": model,
            "tools": tools_payload,
            "reasoning": {"effort": reasoning_effort},
            "parallel_tool_calls": True,
            "max_output_tokens": args.max_output_tokens,
        }

        trace: list[TraceEntry] = []
        trace.append(TraceEntry(
            kind="system",
            payload={
                "prompt_commit_sha": prompt_commit_sha,
                "length": len(system_prompt),
            },
        ))
        trace.append(TraceEntry(kind="user", payload=user_message))

        status: str | None = None
        final_text = ""
        iterations = 0
        error: str | None = None
        tool_usage: dict[str, int] = {}

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
                    tag = _tool_tag(fc.name)
                    tool_usage[fc.name] = tool_usage.get(fc.name, 0) + 1
                    _log(f"[turn {iterations}]   → [{tag}] {fc.name}({_summarise_args(tool_args)})")
                    t_tool = time.monotonic()
                    try:
                        if fc.name in code_tools.DISPATCH:
                            kwargs = tool_args if isinstance(tool_args, dict) else {}
                            out = await code_tools.DISPATCH[fc.name](**kwargs)
                        else:
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
                            f"[turn {iterations}]   ← [{tag}] {fc.name}: {lines_str}, "
                            f"{_human_bytes(out_bytes)} in {tool_dt:.1f}s"
                        )
                    else:
                        _log(
                            f"[turn {iterations}]   ← [{tag}] {fc.name}: "
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
        derived_route_trace_steps = derive_route_trace_steps(context)
        derived_route_trace = format_route_trace_steps(derived_route_trace_steps)
        if final_json is not None and derived_route_trace_steps:
            if not isinstance(final_json.get("route_trace"), list):
                final_json["route_trace"] = derived_route_trace_steps

        result = RunResult(
            call_id=args.call_id,
            prompt_path=str(prompt_path.relative_to(ROOT) if prompt_path.is_relative_to(ROOT) else prompt_path),
            prompt_commit_sha=prompt_commit_sha,
            model=model,
            reasoning_effort=reasoning_effort,
            context_loaders=selected_loaders,
            code_tools_enabled=code_tools_enabled,
            tool_usage=tool_usage,
            started_at=started_iso,
            duration_seconds=round(time.monotonic() - started_wall, 3),
            context=context,
            user_message=user_message,
            trace=[asdict(e) for e in trace],
            final_json=final_json,
            final_text=final_text,
            derived_route_trace=derived_route_trace,
            derived_route_trace_steps=derived_route_trace_steps,
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
        _log(f"[done] tool usage: {_format_usage(tool_usage)}")
        print(f"\nrun saved to: {out_path}")
        if final_json is not None:
            print(json.dumps(final_json, indent=2, default=str))
        else:
            print(final_text or "(no final text)")
        if derived_route_trace:
            print("\nderived_route_trace:")
            print(derived_route_trace)
        if derived_route_trace_steps:
            print("\nderived_route_trace_steps:")
            print(json.dumps(derived_route_trace_steps, indent=2, ensure_ascii=False))
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
        else "Question / symptom: (none)"
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
    parts.append("Follow the system prompt's task and output format exactly.")
    return "\n".join(parts)


def derive_route_trace(context: dict[str, Any]) -> str | None:
    """Best-effort one-line graph route from optional conv_graph context."""
    return format_route_trace_steps(derive_route_trace_steps(context))


def format_route_trace_steps(steps: list[dict[str, Any]] | None) -> str | None:
    if steps is None:
        return None
    if not steps:
        return "not_available"
    return " -> ".join(_format_route_step(step) for step in steps)


def derive_route_trace_steps(context: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Best-effort structured graph route from optional conv_graph context.

    This is intentionally conservative: use direct graph node IDs from transcript
    matching metadata first, then fall back to matching assistant text to node
    content. Repeated action nodes are preserved when user turns occur between
    them so self-loops remain visible.
    """
    conv_graph = context.get("conv_graph") or {}
    graph = conv_graph.get("graph") or {}
    transcript = conv_graph.get("transcript") or []
    nodes = graph.get("nodes") or []
    if not isinstance(graph, dict) or not isinstance(transcript, list) or not isinstance(nodes, list):
        return None

    node_by_id = {
        node.get("id"): node
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    }
    if not node_by_id:
        return []

    start_node_id = _find_start_node_id(nodes)
    segments: list[dict[str, Any]] = []
    if start_node_id:
        segments.append({
            "type": "node",
            "node_id": start_node_id,
            "node_label": _route_label(_node_text(node_by_id[start_node_id])),
            "node_text": _node_text(node_by_id[start_node_id]),
            "assistant_text": None,
            "is_end": False,
        })

    last_node_id: str | None = start_node_id
    last_segment_kind = "node" if start_node_id else ""
    saw_node_after_start = False

    for entry in transcript:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").upper()
        text = _clean_inline(str(entry.get("text") or ""))
        if not text:
            continue

        if role == "USER":
            if text.startswith("Init@????"):
                continue
            segments.append({
                "type": "user",
                "user_text": _trunc(text, 140),
            })
            last_segment_kind = "user"
            continue

        if role != "ASSISTANT":
            continue

        node_id = _resolve_transcript_node_id(entry, node_by_id)
        if not node_id:
            segments.append({
                "type": "assistant",
                "node_id": None,
                "assistant_text": _trunc(text, 180),
                "mapped": False,
            })
            last_segment_kind = "assistant"
            continue

        # Avoid duplicate adjacent assistant fragments only when the transcript
        # repeats the node's static text. Preserve distinct utterances from the
        # same action node because they explain the actual route behavior.
        node = node_by_id[node_id]
        node_text = _node_text(node)
        if (
            node_id == last_node_id
            and last_segment_kind == "node"
            and _norm_for_match(text) == _norm_for_match(node_text)
        ):
            continue

        segments.append({
            "type": "node",
            "node_id": node_id,
            "node_label": _route_label(node_text or text),
            "node_text": node_text or _trunc(text, 120),
            "assistant_text": _trunc(text, 180),
            "is_end": False,
        })
        last_node_id = node_id
        last_segment_kind = "node"
        if node_id != start_node_id:
            saw_node_after_start = True

    if not saw_node_after_start:
        return []

    for segment in reversed(segments):
        if segment["type"] == "node":
            segment["is_end"] = True
            break

    return [
        {"index": idx, **segment}
        for idx, segment in enumerate(segments)
    ]


def _resolve_transcript_node_id(entry: dict[str, Any], node_by_id: dict[str, dict[str, Any]]) -> str | None:
    matching = entry.get("matching") or {}
    if isinstance(matching, dict):
        for key in ("id", "primary_assistant_question_id"):
            value = matching.get(key)
            if value in node_by_id:
                return value

    text = _clean_inline(str(entry.get("text") or ""))
    if not text:
        return None

    best_id: str | None = None
    best_score = 0.0
    for node_id, node in node_by_id.items():
        node_text = _node_text(node)
        if not node_text or node_text == "START":
            continue
        score = SequenceMatcher(None, _norm_for_match(text), _norm_for_match(node_text)).ratio()
        if score > best_score:
            best_id = node_id
            best_score = score
    return best_id if best_score >= 0.72 else None


def _find_start_node_id(nodes: list[dict[str, Any]]) -> str | None:
    for node in nodes:
        if _node_text(node).upper() == "START":
            return node.get("id")
    return None


def _node_text(node: dict[str, Any]) -> str:
    return _clean_inline(str(node.get("content") or ""))


def _format_route_step(step: dict[str, Any]) -> str:
    step_type = step.get("type")
    if step_type == "user":
        return f'User:"{step.get("user_text", "")}"'
    if step_type == "assistant":
        return f'Assistant[id=unknown]:"{step.get("assistant_text", "")}"'

    text = _trunc(str(step.get("node_text") or "node"), 120)
    label = str(step.get("node_label") or _route_label(text))
    suffix = ":End" if step.get("is_end") else ""
    utterance = step.get("assistant_text")
    if isinstance(utterance, str) and utterance and _norm_for_match(utterance) != _norm_for_match(text):
        return f'{label}[id={step.get("node_id")}] ({text}; Assistant:"{utterance}"){suffix}'
    return f"{label}[id={step.get('node_id')}] ({text}){suffix}"


def _route_label(text: str) -> str:
    label = "".join(ch if ch.isalnum() else "_" for ch in text.strip())
    label = "_".join(part for part in label.split("_") if part)
    return _trunc(label or "Node", 32)


def _clean_inline(text: str) -> str:
    return " ".join(text.split())


def _trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _norm_for_match(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


def save_run(result: RunResult, prompt_path: Path) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    prompt_name = prompt_path.stem
    safe_call_id = result.call_id.replace("/", "_")

    # Encode the optional features into the filename so two runs of the
    # same prompt against the same call but with different context are easy to
    # tell apart at a glance. Mandatory loaders are implicit and elided.
    features = [k for k in result.context_loaders if k not in MANDATORY_LOADERS]
    if result.code_tools_enabled:
        features.append("code")
    ctx_suffix = f"__{'+'.join(features)}" if features else ""

    # Filename order: call_id → prompt → optional loaders → timestamp.
    # Timestamp is at second resolution and derived from `started_at` so the
    # filename matches the run's recorded start time exactly.
    ts = datetime.fromisoformat(result.started_at).strftime("%Y%m%d-%H%M%S")

    out_path = RUNS_DIR / f"{safe_call_id}__{prompt_name}{ctx_suffix}__{ts}.json"
    out_path.write_text(json.dumps(asdict(result), indent=2, default=str))
    return out_path


def load_prompt(prompt_path: Path) -> tuple[str, str | None]:
    """Return (system_prompt_text, source_sha).

    - .yaml/.yml: parses spec.declarative.systemMessage from the kagent Agent yaml.
    - any other extension: read as raw text.
    - source_sha comes from a sibling .sha file (e.g. prompts/gitops/.sha), or None.
    """
    if prompt_path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(prompt_path.read_text())
        try:
            text = data["spec"]["declarative"]["systemMessage"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                f"could not find spec.declarative.systemMessage in {prompt_path}: {e}"
            ) from e
        if not isinstance(text, str):
            raise RuntimeError(
                f"spec.declarative.systemMessage in {prompt_path} is not a string"
            )
    else:
        text = prompt_path.read_text()

    sha_path = prompt_path.parent / ".sha"
    source_sha: str | None = None
    if sha_path.is_file():
        content = sha_path.read_text().strip()
        source_sha = content or None
    return text, source_sha


def _is_gitops_prompt(prompt_path: Path) -> bool:
    return prompt_path.is_relative_to(GITOPS_PROMPTS_DIR.resolve())


def _run_sync_prompt() -> None:
    if not SYNC_SCRIPT.exists():
        _log(f"[sync] script not found at {SYNC_SCRIPT}; skipping")
        return
    try:
        subprocess.run([str(SYNC_SCRIPT)], check=True)
    except subprocess.CalledProcessError as e:
        _log(f"[sync] failed (exit {e.returncode}); continuing with local copy")
    except FileNotFoundError:
        _log("[sync] bash not found; continuing with local copy")


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
