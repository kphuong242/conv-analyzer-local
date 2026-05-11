#!/usr/bin/env python3
"""Clone or pull the code repos the conversation analyzer is allowed to read.

Reads `code-repos.yaml` at the project root; for each repo either clones
it into `code-repos/<name>/` (shallow by default) or, if the directory
already exists, fetches and hard-resets to the configured branch.

This is the source-of-truth checkout for the analyzer's code-reading
tools — pin it explicitly here rather than reaching into your wider
workspace where each clone might be on an unrelated feature branch.
Clones live inside the harness project and never overwrite your
workspace checkouts.

Usage:
    python scripts/sync-code-repos.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
DEST = ROOT / "code-repos"
CONFIG = ROOT / "code-repos.yaml"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    if not CONFIG.exists():
        _log(f"[sync] config not found: {CONFIG.relative_to(ROOT)}")
        return 2

    config = yaml.safe_load(CONFIG.read_text()) or {}
    repos = config.get("repos") or []
    if not repos:
        _log("[sync] no repos configured in code-repos.yaml")
        return 0

    DEST.mkdir(exist_ok=True)
    errors = 0

    for repo in repos:
        name = repo.get("name")
        url = repo.get("url")
        branch = repo.get("branch", "main")
        depth = int(repo.get("depth", 1))
        if not name or not url:
            _log(f"[sync] skipping malformed entry (missing name/url): {repo!r}")
            errors += 1
            continue

        target = DEST / name

        if target.exists() and not (target / ".git").is_dir():
            _log(f"[sync] {name}: {target.relative_to(ROOT)} exists but isn't a git repo — skipping")
            errors += 1
            continue

        try:
            if (target / ".git").is_dir():
                _log(f"[sync] {name}: pulling {branch} (depth={depth})…")
                _run(["git", "-C", str(target), "fetch", "--quiet", "--depth", str(depth), "origin", branch])
                _run(["git", "-C", str(target), "reset", "--hard", "--quiet", f"origin/{branch}"])
            else:
                _log(f"[sync] {name}: cloning {url} @ {branch} (depth={depth})…")
                _run([
                    "git", "clone", "--quiet",
                    "--depth", str(depth),
                    "--branch", branch,
                    url, str(target),
                ])
        except subprocess.CalledProcessError as e:
            _log(f"[sync] {name}: git command failed ({e})")
            errors += 1
            continue

    _log(f"[sync] done — {len(repos) - errors}/{len(repos)} repo(s) synced into {DEST.relative_to(ROOT)}/")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
