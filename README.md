# conversation-analyzer-eval

Local harness for iterating on the **conversation-analyzer** kagent prompt
(`gitops/deployment/getvocal/kagent/agents/conversation-analyzer.yaml`)
without going through a kagent deploy cycle.

## What it does

Given a `call_id`, the script:

1. Auto-syncs the latest `conversation-analyzer.yaml` from
   `getvocal/gitops@main` (skip with `--no-sync`; see "Sync from gitops"
   below). The local copy lives at `prompts/gitops/`.
2. Looks up the call row in the **production Postgres DB** via the
   `db-toolbox-prod` MCP server (`prod_execute_sql`) to get `call_sid`,
   `dt_started`, `dt_ended`, `assistant_id`, `status`, etc.
3. Derives a Loki time window (call duration ± `LOOKUP_BUFFER_MINUTES`).
4. Loads the system prompt. `.yaml` files have `spec.declarative.systemMessage`
   extracted; `.md` files are read raw.
5. Runs an OpenAI agent loop against that prompt — using the same model and
   reasoning effort the deployed kagent runs with (`gpt-5.4`,
   `reasoning_effort=high`) — exposing only the same Loki tools the kagent
   uses (`query_loki_logs`, `list_loki_label_names`) from the
   **grafana-prod-vpn** MCP server.
6. Saves the full run — input context, gitops commit sha, every tool call
   and response, the final JSON, model, duration, and a free-form `note` —
   to `runs/<call_id>__<prompt_name>__<ctx>__<ts>.json`.

## Requirements

- VPN access to `internal.getvocal.ai` (both MCP servers are internal).
- Python 3.11+.
- `OPENAI_API_KEY`.
- `gh` CLI authenticated against the `getvocal` org (for the auto-sync of
  the gitops prompt). Skip with `--no-sync` if you don't have it.

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

# Pull in any existing ActionItems (tickets) attached to this call:
python analyze.py --call-id 0193abcd-... --context ticket

# Enable code-reading tools (auto-syncs code-repos/ first; skip with --no-code-sync):
python analyze.py --call-id 0193abcd-... --context code

# Use already-synced code-repos as-is (no fresh `git fetch` for each repo):
python analyze.py --call-id 0193abcd-... --context code --no-code-sync

# Stack multiple contexts:
python analyze.py --call-id 0193abcd-... --context conv_graph,ticket,code

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
├── mcp_clients.py          # MCP client wiring for both remote servers
├── code_tools.py           # in-process read-only code tools (--context code)
├── code-repos.yaml         # repos the analyzer is allowed to read
├── context_loaders/
│   ├── __init__.py
│   ├── call_db.py          # call lookup via db-toolbox-prod
│   ├── conv_graph.py       # conv graph + transcript + analytics loader
│   └── ticket.py           # ActionItems / tickets loader (eval-safe projection)
├── prompts/
│   ├── baseline.md         # extracted systemMessage, refreshed each sync (readable mirror)
│   └── gitops/             # auto-synced from gitops main (default --prompt target)
│       ├── conversation-analyzer.yaml
│       └── .sha            # gitops commit at last sync
├── scripts/
│   ├── sync-prompt.sh      # gh-based fetch of conversation-analyzer.yaml + sha
│   └── sync-code-repos.py  # clone/pull the repos listed in code-repos.yaml
├── code-repos/             # shallow clones populated by sync-code-repos.py (gitignored)
└── runs/                   # per-run JSON outputs (gitignored)
```

## Sync from gitops

The default `--prompt` is `prompts/gitops/conversation-analyzer.yaml`, the
auto-synced copy of the deployed kagent Agent yaml in
`getvocal/gitops/deployment/getvocal/kagent/agents/`. Every time you run
`analyze.py` with a prompt under `prompts/gitops/`, it invokes
`scripts/sync-prompt.sh` before loading the prompt:

- On first run, `[sync] initial sync from gitops@<sha>` is printed.
- When gitops `main` has new commits since your last sync,
  `[sync] prompt updated <old-sha> -> <new-sha>` is printed and the local
  files are overwritten. The run proceeds with the **new** prompt and the
  new sha is recorded as `prompt_commit_sha` in `runs/*.json`.
- When upstream hasn't moved, `[sync] gitops@<sha> (no change)` is printed.

Each sync writes three files:
- `prompts/gitops/conversation-analyzer.yaml` — full kagent Agent yaml.
- `prompts/gitops/.sha` — gitops commit SHA at fetch time.
- `prompts/baseline.md` — `spec.declarative.systemMessage` extracted from
  the yaml, byte-identical to what the LLM sees. Read it to skim the
  current deployed prompt without parsing yaml, or copy it as the starting
  point for an experiment (`cp prompts/baseline.md prompts/my-test.md`).

Manual sync (no run): `./scripts/sync-prompt.sh`.

Skip auto-sync: pass `--no-sync` (useful when you're editing
`prompts/gitops/conversation-analyzer.yaml` locally to test a prompt change
without it being overwritten on the next run).

If the sync fails (no network, `gh` unauth'd, etc.), the harness prints a
warning to stderr and falls back to whatever local copy exists. It only
hard-fails if the prompt file is missing entirely.

To commit a synced prompt into this repo's history (e.g. you've reviewed
runs against the new version and want it pinned):

```bash
git diff prompts/gitops/      # review what changed
git add prompts/gitops/
git commit -m "chore: sync prompt to gitops <short-sha>"
```

## Prompt versioning

Every run records `prompt_commit_sha` — the gitops `main` commit the prompt
was synced from (read from the sibling `.sha` file). This is the same 40-char
SHA you see on GitHub for that commit, so you can paste it into
`github.com/getvocal/gitops/commit/<sha>` to view the exact prompt that
produced a run.

`prompt_commit_sha` is `null` for prompts not under `prompts/gitops/` (e.g.
`baseline.md`, ad-hoc experiments). For those, the prompt filename + the
full `system_prompt` text saved inside the trace are the identifier.

To iterate on a new prompt without touching the gitops snapshot, copy
`baseline.md` (or the synced yaml) to a new file and edit it. Anything
under `prompts/` that is **not** inside `prompts/gitops/` is exempt from
auto-sync, so your experimental file won't be overwritten:

```bash
cp prompts/baseline.md prompts/2026-05-12-tighter-search.md
$EDITOR prompts/2026-05-12-tighter-search.md
python analyze.py --call-id ... --prompt prompts/2026-05-12-tighter-search.md
```

The output filename is
`runs/<call_id>__<prompt-stem>[__<optional-loaders>]__<YYYYMMDD-HHMMSS>.json`
— call_id first for grepping by call, timestamp last at second resolution.
Combined with `prompt_commit_sha` and `context_loaders` inside the run JSON,
every result is traceable to a specific prompt revision and context shape:

- No optional context: `abc__baseline__20260511-143012.json`
- With graph context: `abc__baseline__conv_graph__20260511-143012.json`
- With code tools: `abc__baseline__code__20260511-143012.json`
- With multiple features: `abc__baseline__conv_graph+ticket+code__20260511-143012.json`

The timestamp matches the `started_at` field in the run JSON exactly (same
Paris-local instant, same second-level precision). The mandatory `call`
loader is implicit and elided from the filename. If you re-sync mid-day
and gitops has moved, the `prompt_commit_sha` field in `runs/*.json` will
diverge and let you tell runs apart even when the filename and call_id
match.

## Selectable features (`--context`)

A single `--context <names>` flag controls what's enabled for each run.
Two kinds of feature share the flag:

- **Data loaders** inject information into the user message before the agent
  loop starts (e.g. the call row, conv graph, tickets).
- **Tool features** expose extra tools to the model during the agent loop
  (currently just `code`).

The `call` loader is **mandatory** (the agent can't query Loki without its
time range); everything else is opt-in:

```bash
# default: only call_db
python analyze.py --call-id 0193abcd-...

# add the conv-graph + transcript + analytics bundle
python analyze.py --call-id 0193abcd-... --context conv_graph

# expose code-reading tools (auto-syncs code-repos/ first)
python analyze.py --call-id 0193abcd-... --context code

# same, but reuse whatever's already synced (no network fetch this run)
python analyze.py --call-id 0193abcd-... --context code --no-code-sync

# stack anything
python analyze.py --call-id 0193abcd-... --context conv_graph,ticket,code
```

Unknown names error out with the available list. `--context code` triggers
the auto-sync of `code-repos/` before the agent loop; if the sync fails
*and* `code-repos/` is empty, the run hard-stops before calling OpenAI.

### Available loaders (data → user message)

- **`call`** (mandatory) — id, call_sid, conv_graph_id, dt_started/ended, telecom_provider, status, language. Derives the Loki time window.
- **`conv_graph`** (optional) — given the call's `conv_graph_id`, fetches the slim graph (nodes, edges, action-node IDs), the call's `conversation_transcript`, and the pre-computed `conv_graph_analytics` blob if it's been treated. This is the loader that lets the analyzer support tags it can't decide from logs alone (`graph_translation_issue`, `same_questions_several_times`, `agent_not_ending_the_conversation`, `action_node_error`, `conversation_not_starting`).
- **`ticket`** (optional) — pulls every `ActionItems` row whose `call_or_conversation_id` matches the target call (the platform's per-call ticket records), ordered newest first. **Eval-safe projection**: returns only intake-time fields (`title`, `description`, `priority`, `job`, `assignment`, `dt_created`, `dt_eta`, `assignee_id`, etc.). Fields that would let the model copy the answer instead of deriving one are deliberately not loaded: `comments` (contain the human conclusion), `responsible_team` and `tags` (map 1:1 to the analyzer's output fields), `status`/`dt_resolved`/`resolver_id` (leak resolution state), and `sub_tasks` (leak investigation decomposition).

### Available tool features (capability → model)

- **`code`** (optional) — exposes four read-only code-reading tools to the model: `list_code_repos`, `list_directory`, `read_file`, `search_code` (ripgrep-backed). Operates strictly on the repos cloned into `code-repos/` (see "Code-reading tools" below). Enabling this feature **auto-syncs** the repos before the run (skip with `--no-code-sync`). Disabled by default — opt in per run; if not opted in, the model never sees the tools.

### Adding a new loader

1. Drop a module in `context_loaders/` with `async def load(hub, call_id, *, context=None, **_): ...`.
2. Register it in `LOADER_REGISTRY` in `analyze.py` (insertion order = run order).
3. Mark it mandatory by adding to `MANDATORY_LOADERS` if needed.

Loaders may read earlier loaders' output via the `context` kwarg — see how
`conv_graph` reads `context["call"]["call"]["conv_graph_id"]` to avoid a
redundant call lookup.

### Adding a new tool feature

Add a module that defines `TOOLS` (OpenAI tool schemas) and `DISPATCH` (name
→ async callable), the way `code_tools.py` does. Then add its feature name to
`OPTIONAL_TOOL_FEATURES` in `analyze.py` and wire the enable/expose logic
following the `code` example.

## Code-reading tools

When `--context code` is on, the model can read source code from the repos
listed in `code-repos.yaml`. The repos are shallow-cloned into `code-repos/`
by a separate script, kept fully isolated from your wider workspace
checkouts.

### Sync the repos

Auto-syncs whenever you pass `--context code`. Each such run:

1. Invokes `scripts/sync-code-repos.py` before opening the agent loop.
2. Clones any missing repos from `code-repos.yaml`; on existing ones does
   `git fetch --depth=1 origin <branch>` + `git reset --hard origin/<branch>`.
3. Continues even if the sync errors — `[sync] code sync exited N — proceeding with existing code-repos/` is printed and the run uses whatever was on disk before. If `code-repos/` is empty after that, the run hard-stops with the same `[error] --context code requested but no repos under code-repos/` as before.

Manual sync (any time):
```bash
python scripts/sync-code-repos.py
```

Skip the auto-sync for a given run:
```bash
python analyze.py --call-id ... --context code --no-code-sync
```

Use `--no-code-sync` when you want to investigate against the code as it
was last synced (e.g. debugging a call from a week ago against code from
that period) — otherwise every `--context code` run grabs fresh `main`.

Default depth is 1 (shallow); raise per-repo in `code-repos.yaml` if you
want git-log/blame in scope. Your workspace clones
(`~/.../SmartCaller-main/`, etc.) are never touched.

### Tools exposed

| Tool | What it does |
|---|---|
| `list_code_repos` | Returns the list of synced repos. |
| `list_directory` | Lists a directory inside a repo; skips `.git`, `__pycache__`, `node_modules`, `.venv`, ... |
| `read_file` | Reads a UTF-8 text file (or a line range). Files >500 kB rejected; non-UTF-8 rejected. Output has 1-indexed line numbers prefixed. |
| `search_code` | ripgrep wrapper. Smart-case, regex supported, scoped by `file_glob`. 15s timeout. Falls back to a slow Python substring grep if `rg` is absent. |

### Safety guarantees

1. **API surface** — only read operations are exposed; no write/exec/git-mutate tool exists.
2. **Repo allowlist** — derived from `code-repos/<name>/.git` directory presence. The filesystem IS the allowlist.
3. **Path traversal protection** — every `path` argument is resolved with `Path.resolve()` and rejected if it escapes the repo root via `..` or absolute paths.
4. **Output bounds** — file size and search result caps prevent context-window blowup.

The tools are *only* added to the model's tool list when `--context code` is
passed. Without it, they don't appear, the model can't see them, and runs
go through the same Loki-only path as before.

## Notes

- Only `prod_execute_sql` is exposed to the DB MCP, and only inside the
  loader (the model itself never sees DB tools — it only gets the Loki
  tools the kagent has). Loaders interpolate values into SQL after a
  defensive escape; if you add loaders that take user-supplied input,
  parameterize properly.
- Defaults match the deployed kagent: `gpt-5.4` with `reasoning_effort=high`.
  Override via env vars or CLI flags if you want to A/B against a different
  model — the chosen model + effort are saved into every run JSON.
- `--dry-run` prints the assembled user message and the tool list (Loki
  MCP tools plus `code` tools if enabled) without calling OpenAI — useful
  for verifying DB lookup and which tools are exposed before spending tokens.
