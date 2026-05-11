"""Shared helpers for context loaders that hit the db-toolbox-prod MCP."""

from __future__ import annotations

import json
from typing import Any


def escape_sql_literal(value: str) -> str:
    if "'" in value or ";" in value or "--" in value:
        raise ValueError(f"refusing to interpolate suspicious value: {value!r}")
    return value


def parse_rows(raw: str) -> list[dict[str, Any]]:
    """The prod_execute_sql tool returns a JSON-encoded result. Tolerate
    several wrapper shapes across MCP server versions; fall back to a
    pipe-delimited text table parser for older formats."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _parse_text_table(raw)

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("rows", "result", "data", "records"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        return [parsed]
    return []


def _parse_text_table(raw: str) -> list[dict[str, Any]]:
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = [c.strip() for c in lines[0].split("|")]
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def coerce_json(value: Any) -> Any:
    """Some MCP responses encode JSONB columns as strings; decode if so."""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value
