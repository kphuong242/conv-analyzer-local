#!/usr/bin/env bash
# Sync the conversation-analyzer kagent yaml from getvocal/gitops main.
#
# Drops:
#   prompts/gitops/conversation-analyzer.yaml   ← full yaml as in gitops
#   prompts/gitops/.sha                         ← gitops main commit at fetch time
#   prompts/baseline.md                         ← extracted systemMessage (human-readable mirror)
#
# Requires: gh CLI authenticated against getvocal org, plus `uv` for the
# yaml → markdown extraction (uses the project's PyYAML dep).
# analyze.py invokes this automatically when --prompt points inside
# prompts/gitops/; you can also run it standalone to refresh on demand.
set -euo pipefail

REPO="getvocal/gitops"
YAML_PATH="deployment/getvocal/kagent/agents/conversation-analyzer.yaml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$PROJECT_ROOT/prompts/gitops"

mkdir -p "$OUT_DIR"

prev_sha=""
[[ -f "$OUT_DIR/.sha" ]] && prev_sha="$(cat "$OUT_DIR/.sha")"

tmp_yaml="$(mktemp)"
trap 'rm -f "$tmp_yaml"' EXIT

gh api "repos/$REPO/contents/$YAML_PATH?ref=main" \
    -H "Accept: application/vnd.github.raw" \
    > "$tmp_yaml"

new_sha="$(gh api "repos/$REPO/commits/main" --jq .sha)"

mv "$tmp_yaml" "$OUT_DIR/conversation-analyzer.yaml"
trap - EXIT
printf '%s' "$new_sha" > "$OUT_DIR/.sha"

# Extract spec.declarative.systemMessage to prompts/baseline.md so humans
# (and experiments that copy this file) get the prompt in readable markdown
# form, refreshed each sync. baseline.md stays byte-identical to the string
# the LLM sees from the yaml.
uv run --project "$PROJECT_ROOT" --quiet python - \
    "$OUT_DIR/conversation-analyzer.yaml" \
    "$PROJECT_ROOT/prompts/baseline.md" <<'PY'
import sys, yaml
src, dst = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(src))
text = data["spec"]["declarative"]["systemMessage"]
open(dst, "w").write(text)
PY

if [[ -z "$prev_sha" ]]; then
    echo "[sync] initial sync from gitops@${new_sha:0:7}" >&2
elif [[ "$prev_sha" != "$new_sha" ]]; then
    echo "[sync] prompt updated ${prev_sha:0:7} -> ${new_sha:0:7}" >&2
else
    echo "[sync] gitops@${new_sha:0:7} (no change)" >&2
fi
