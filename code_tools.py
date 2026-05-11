"""Read-only code-reading tools for the conversation analyzer.

These tools let the model cross-reference log analysis with actual source
code. They operate strictly on the repos under `code-repos/` (populated
by `scripts/sync-code-repos.py`) — never the wider workspace.

Safety guarantees (defense in depth):

  1. API surface: only read operations are exposed. No write/exec/git-mutate
     tool exists, so the model can't request one.
  2. Repo allowlist: the set of valid repo names is computed from the
     subdirectories of `code-repos/` that contain a `.git` directory. The
     filesystem IS the allowlist — there's no separate config to drift.
  3. Path-traversal protection: every `path` argument is resolved against
     the repo root and rejected if it escapes via `..` or is absolute.
  4. Output bounds: files >MAX_FILE_BYTES and non-UTF-8 files are rejected;
     search results are capped at max_results.

The shallow clones (depth=1) mean `git log` would only see one commit, so
no git tool is exposed. Raise the per-repo `depth` in `code-repos.yaml`
and add a git_log tool here if needed later.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent
CODE_REPOS_DIR = ROOT / "code-repos"

MAX_FILE_BYTES = 500_000
MAX_SEARCH_RESULTS_CAP = 100
RIPGREP_TIMEOUT_S = 15

_SKIP_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", ".next", ".turbo",
}


# ---------------------------------------------------------------- helpers


def _available_repos() -> list[str]:
    if not CODE_REPOS_DIR.is_dir():
        return []
    return sorted(p.name for p in CODE_REPOS_DIR.iterdir() if (p / ".git").is_dir())


def _resolve_repo(repo: str) -> Path:
    if not CODE_REPOS_DIR.is_dir():
        raise ValueError("code-repos/ does not exist — run `python scripts/sync-code-repos.py` first")
    target = CODE_REPOS_DIR / repo
    if not (target / ".git").is_dir():
        available = _available_repos()
        raise ValueError(f"repo not synced: {repo!r}. Available: {available}")
    return target


def _safe_resolve(repo_root: Path, user_path: str) -> Path:
    if user_path is None:
        return repo_root
    user_path = user_path.strip()
    if user_path.startswith("/"):
        raise ValueError(f"absolute paths are not allowed: {user_path!r}")
    if not user_path or user_path == ".":
        return repo_root
    resolved = (repo_root / user_path).resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"path escapes {repo_root.name}: {user_path!r}")
    return resolved


# ----------------------------------------------------------------- tools


async def list_code_repos() -> str:
    repos = []
    if CODE_REPOS_DIR.is_dir():
        for entry in sorted(CODE_REPOS_DIR.iterdir()):
            if (entry / ".git").is_dir():
                repos.append({
                    "name": entry.name,
                    "path": str(entry.relative_to(ROOT)),
                })
    return json.dumps({"repos": repos}, indent=2)


async def list_directory(repo: str, path: str = "") -> str:
    repo_root = _resolve_repo(repo)
    target = _safe_resolve(repo_root, path)
    if not target.exists():
        raise ValueError(f"path not found: {repo}/{path}")
    if not target.is_dir():
        raise ValueError(f"not a directory: {repo}/{path}")

    entries: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if entry.name in _SKIP_NAMES:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if entry.is_dir():
            entries.append({"name": entry.name + "/", "kind": "dir", "size": None})
        else:
            entries.append({"name": entry.name, "kind": "file", "size": stat.st_size})

    return json.dumps({"repo": repo, "path": path or "", "entries": entries}, indent=2)


async def read_file(
    repo: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    repo_root = _resolve_repo(repo)
    target = _safe_resolve(repo_root, path)
    if not target.exists():
        raise ValueError(f"file not found: {repo}/{path}")
    if not target.is_file():
        raise ValueError(f"not a file: {repo}/{path}")

    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"file too large ({size} bytes; max {MAX_FILE_BYTES}). "
            f"Use start_line/end_line to read a range."
        )
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"file is not UTF-8 text (binary?): {repo}/{path}")

    lines = content.splitlines()
    total = len(lines)
    s = max(1, start_line or 1)
    e = min(total, end_line or total)

    if s > total:
        return json.dumps({
            "repo": repo,
            "path": path,
            "total_lines": total,
            "lines_returned": [s, s - 1],
            "content": "",
        }, indent=2)

    selected = lines[s - 1:e]
    numbered = "\n".join(f"{s + i:6d}  {line}" for i, line in enumerate(selected))
    return json.dumps({
        "repo": repo,
        "path": path,
        "total_lines": total,
        "lines_returned": [s, e],
        "content": numbered,
    }, indent=2)


async def search_code(
    repo: str,
    query: str,
    max_results: int = 20,
    file_glob: str | None = None,
) -> str:
    repo_root = _resolve_repo(repo)
    if not query:
        raise ValueError("query is required")
    max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS_CAP))

    cmd = [
        "rg", "--vimgrep", "-n",
        "--max-count", str(max_results),
        "--no-heading",
        "--smart-case",
    ]
    if file_glob:
        cmd += ["-g", file_glob]
    cmd += ["--", query, str(repo_root)]

    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=RIPGREP_TIMEOUT_S,
        )
    except FileNotFoundError:
        # ripgrep absent — fall back to a slow Python walk.
        return await _python_grep(repo_root, repo, query, max_results, file_glob)
    except subprocess.TimeoutExpired:
        raise ValueError(f"search timed out after {RIPGREP_TIMEOUT_S}s — narrow your query or scope with file_glob")

    matches: list[dict[str, Any]] = []
    repo_resolved = repo_root.resolve()
    for line in proc.stdout.splitlines():
        # vimgrep format: path:line:col:text
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        try:
            ln = int(parts[1])
        except ValueError:
            continue
        try:
            rel = Path(parts[0]).resolve().relative_to(repo_resolved)
        except ValueError:
            continue
        matches.append({"path": str(rel), "line": ln, "match": parts[3]})
        if len(matches) >= max_results:
            break

    return json.dumps({
        "repo": repo,
        "query": query,
        "match_count": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }, indent=2)


async def _python_grep(
    repo_root: Path, repo: str, query: str, max_results: int, file_glob: str | None,
) -> str:
    """Slow fallback for hosts without ripgrep. Substring-only, no regex."""
    matches: list[dict[str, Any]] = []
    glob = file_glob or "**/*"
    for path in repo_root.glob(glob):
        if not path.is_file() or any(part in _SKIP_NAMES for part in path.parts):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for ln, line in enumerate(f, start=1):
                    if query in line:
                        try:
                            rel = path.relative_to(repo_root)
                        except ValueError:
                            continue
                        matches.append({"path": str(rel), "line": ln, "match": line.rstrip()})
                        if len(matches) >= max_results:
                            break
        except OSError:
            continue
        if len(matches) >= max_results:
            break

    return json.dumps({
        "repo": repo,
        "query": query,
        "match_count": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
        "note": "ripgrep not installed — used slower Python fallback (substring-only, no regex)",
    }, indent=2)


# ------------------------------------------------ OpenAI tool schemas


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_code_repos",
        "description": (
            "Return the list of code repositories the analyzer can read. "
            "Use this first to see what's available — repo names from here are "
            "what other code tools expect as the `repo` argument."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": (
            "List entries in a directory inside a repo. Example: "
            "`repo=\"SmartCaller\", path=\"vocal/common\"`. Pass empty path for "
            "the repo root. Common noise directories (.git, __pycache__, "
            "node_modules, .venv, ...) are excluded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo name as returned by list_code_repos."},
                "path": {
                    "type": "string",
                    "description": "Relative subpath inside the repo. Empty or '.' for repo root.",
                },
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file from a repo, optionally restricted to a "
            "line range. Files larger than ~500 kB are rejected — use "
            "start_line/end_line for big files. Returns content with 1-indexed "
            "line numbers prefixed for easy referencing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "Relative file path within the repo."},
                "start_line": {
                    "type": "integer", "minimum": 1,
                    "description": "1-indexed, inclusive. Defaults to file start.",
                },
                "end_line": {
                    "type": "integer", "minimum": 1,
                    "description": "1-indexed, inclusive. Defaults to file end.",
                },
            },
            "required": ["repo", "path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_code",
        "description": (
            "Search a repo using ripgrep. Returns up to max_results matches "
            "with file:line:text. Uses smart-case (insensitive unless the query "
            "contains uppercase). Supports regex via ripgrep syntax. Scope with "
            "file_glob (e.g. '*.py', 'vocal/**/*.py') for faster, narrower "
            "results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string", "description": "Substring or ripgrep regex."},
                "max_results": {
                    "type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS_CAP,
                    "default": 20,
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional ripgrep glob like '*.py' or '!*test*' to scope the search.",
                },
            },
            "required": ["repo", "query"],
            "additionalProperties": False,
        },
    },
]


DISPATCH: dict[str, Callable[..., Any]] = {
    "list_code_repos": list_code_repos,
    "list_directory": list_directory,
    "read_file": read_file,
    "search_code": search_code,
}


def available() -> bool:
    """True if there's at least one synced repo under code-repos/."""
    return bool(_available_repos())
