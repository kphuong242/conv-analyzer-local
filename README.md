# conversation-analyzer-eval

Local harness for iterating on the **conversation-analyzer** kagent prompt
(`gitops/deployment/getvocal/kagent/agents/conversation-analyzer.yaml`)
without going through a kagent deploy cycle.

## What it does

Given a `call_id`, the script:

1. Looks up the call row in the **production Postgres DB** via the
   `db-toolbox-prod` MCP server (`prod_execute_sql`) to get `call_sid`,
   `dt_started`, `dt_ended`, `assistant_id`, `status`, etc.
2. Derives a Loki time window (call duration ± `LOOKUP_BUFFER_MINUTES`).
3. Loads a system prompt from `prompts/<name>.md`.
4. Runs an OpenAI agent loop against that prompt — using the same model and
   reasoning effort the deployed kagent runs with (`gpt-5.4`,
   `reasoning_effort=high`) — exposing only the same Loki tools the kagent
   uses (`query_loki_logs`, `list_loki_label_names`) from the
   **grafana-prod-vpn** MCP server.
5. Saves the full run — input context, prompt sha256, every tool call and
   response, the final JSON, model, duration, and a free-form `note` — to
   `runs/<timestamp>__<call_id>__<prompt_name>.json`.

## Requirements

- VPN access to `internal.getvocal.ai` (both MCP servers are internal).
- Python 3.11+.
- `OPENAI_API_KEY`.

## Provider parity with the deployed kagent

The harness replicates the kagent's model settings 1:1 with what's in
`gitops/deployment/argocd/applications/infra/prod-kagent.yaml`:

| kagent helm value                       | harness env / arg              | default     |
|-----------------------------------------|--------------------------------|-------------|
| `providers.openAI.provider`             | (hardcoded)                    | `OpenAI`    |
| `providers.openAI.model`                | `OPENAI_MODEL` / `--model`     | `gpt-5.4`   |
| `providers.openAI.config.reasoningEffort` | `OPENAI_REASONING_EFFORT` / `--reasoning-effort` | `high` |
| `spec.declarative.stream`               | (hardcoded)                    | `false`     |
| `spec.declarative.tools[].toolNames`    | `GRAFANA_TOOL_ALLOWLIST` const | `query_loki_logs`, `list_loki_label_names` |

`temperature` and `response_format` are intentionally not set — kagent
doesn't set them either.

The harness uses the OpenAI **Responses API** (`/v1/responses`), not Chat
Completions. gpt-5.x rejects function tools combined with `reasoning_effort`
on `/v1/chat/completions`, so the Responses endpoint is mandatory for parity.
Tool-call state is chained between turns via `previous_response_id`.

## Setup

```bash
uv venv
uv sync
cp .env.example .env  # fill in OPENAI_API_KEY
```

## Run

```bash
# Broad investigation (no symptom — agent uses DEFAULT scope):
python analyze.py --call-id 0193abcd-... --note "baseline against known-bad call"

# Symptom-targeted run (exercises Step 1 service scoping):
python analyze.py --call-id 0193abcd-... \
    --question "TTS cut off mid-sentence around the greeting" \
    --note "checking tts-only scope behaviour"

# Pull in the conversational graph + transcript + pre-computed analytics:
python analyze.py --call-id 0193abcd-... --context conv_graph \
    --question "agent kept asking the same question" \
    --note "graph context A/B"

# Iterate on a new prompt:
cp prompts/baseline.md prompts/2026-05-07-tighter-search.md
$EDITOR prompts/2026-05-07-tighter-search.md
python analyze.py \
    --call-id 0193abcd-... \
    --prompt prompts/2026-05-07-tighter-search.md \
    --note "trying tighter SEARCH STRATEGY ordering"

# Sanity-check what the agent will see without burning OpenAI tokens:
python analyze.py --call-id 0193abcd-... --dry-run
```

## Layout

```
conversation-analyzer-eval/
├── analyze.py              # entrypoint: arg parsing, agent loop, run saving
├── mcp_clients.py          # MCP client wiring for both servers
├── context_loaders/
│   ├── __init__.py
│   └── call_db.py          # call lookup via db-toolbox-prod
├── prompts/
│   └── baseline.md         # verbatim copy of the deployed systemMessage
└── runs/                   # per-run JSON outputs (gitignored)
```

## Prompt versioning

Prompts are plain markdown files. `baseline.md` is the verbatim copy of the
deployed kagent's `systemMessage`. To iterate, copy it to a new file with a
descriptive name (date + intent), edit, and pass `--prompt path/to/file.md`.

The output filename — `runs/<utc-ts>__<call_id>__<prompt-stem>[__<optional-loaders>].json` —
plus `prompt_sha256` and `context_loaders` inside the run JSON make every
result traceable to a specific prompt revision and context shape:

- No optional context: `20260507T130612Z__abc__baseline.json`
- With graph context: `20260507T130612Z__abc__baseline__conv_graph.json`
- With multiple loaders: `20260507T130612Z__abc__baseline__conv_graph+ticket.json`

The mandatory `call` loader is implicit and elided from the filename. If
you tweak a prompt without renaming the file, the `prompt_sha256` field in
`runs/` will diverge and let you tell runs apart.

## Selectable context loaders

The user message is assembled from a registry of context loaders. The `call`
loader is **mandatory** (the agent can't query Loki without its time range);
all other loaders are **opt-in** via `--context <names>`:

```bash
# default: only call_db
python analyze.py --call-id 0193abcd-...

# add the conv-graph + transcript + analytics bundle
python analyze.py --call-id 0193abcd-... --context conv_graph

# multiple loaders (when more land):
python analyze.py --call-id 0193abcd-... --context conv_graph,ticket
```

### Available loaders

- **`call`** (mandatory) — id, call_sid, conv_graph_id, dt_started/ended, telecom_provider, status, language. Derives the Loki time window.
- **`conv_graph`** (optional) — given the call's `conv_graph_id`, fetches the slim graph (nodes, edges, action-node IDs), the call's `conversation_transcript`, and the pre-computed `conv_graph_analytics` blob if it's been treated. This is the loader that lets the analyzer support tags it can't decide from logs alone (`graph_translation_issue`, `same_questions_several_times`, `agent_not_ending_the_conversation`, `action_node_error`, `conversation_not_starting`).

### Adding a new loader

1. Drop a module in `context_loaders/` with `async def load(hub, call_id, *, context=None, **_): ...`.
2. Register it in `LOADER_REGISTRY` in `analyze.py` (insertion order = run order).
3. Mark it mandatory by adding to `MANDATORY_LOADERS` if needed.

Loaders may read earlier loaders' output via the `context` kwarg — see how
`conv_graph` reads `context["call"]["call"]["conv_graph_id"]` to avoid a
redundant call lookup.

## Notes

- Only `prod_execute_sql` is exposed to the DB MCP, and only inside the
  loader (the model itself never sees DB tools — it only gets the Loki
  tools the kagent has). Loaders interpolate values into SQL after a
  defensive escape; if you add loaders that take user-supplied input,
  parameterize properly.
- Defaults match the deployed kagent: `gpt-5.4` with `reasoning_effort=high`.
  Override via env vars or CLI flags if you want to A/B against a different
  model — the chosen model + effort are saved into every run JSON.
- `--dry-run` prints the assembled user message and the tool list without
  calling OpenAI — useful for verifying DB lookup before spending tokens.
