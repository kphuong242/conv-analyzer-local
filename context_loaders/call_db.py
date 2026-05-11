"""Look up a call row from the production Postgres DB via the db-toolbox-prod MCP.

Returns the fields the conversation-analyzer prompt needs to scope its Loki
queries: the primary id, telecom call_sid, assistant_id, status, conv_graph_id,
and the dt_started / dt_ended window used to bound the log search.

This loader is mandatory — the agent can't query Loki without its time range.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from context_loaders._db import escape_sql_literal, parse_rows
from mcp_clients import McpHub

CALL_FIELDS = [
    "id",
    "call_sid",
    "assistant_id",
    "company_id",
    "conv_graph_id",
    "status",
    "telecom_provider",
    "source",
    "environment",
    "language",
    "dt_created",
    "dt_started",
    "dt_ended",
]


async def load(hub: McpHub, call_id: str, *, buffer_minutes: int = 5, **_: Any) -> dict[str, Any]:
    sql = (
        f"SELECT {', '.join(CALL_FIELDS)} FROM calls "
        f"WHERE id = '{escape_sql_literal(call_id)}' LIMIT 1"
    )
    raw = await hub.call_tool("prod_execute_sql", {"sql": sql})
    rows = parse_rows(raw)
    if not rows:
        raise LookupError(f"no call found with id={call_id}")
    call = rows[0]

    started = _parse_dt(call.get("dt_started")) or _parse_dt(call.get("dt_created"))
    ended = _parse_dt(call.get("dt_ended"))
    if started is None:
        raise LookupError(f"call {call_id} has no dt_started/dt_created — cannot derive Loki time range")
    if ended is None:
        ended = started + timedelta(minutes=15)

    loki_start = (started - timedelta(minutes=buffer_minutes)).isoformat()
    loki_end = (ended + timedelta(minutes=buffer_minutes)).isoformat()

    return {
        "call": call,
        "loki_time_range": {"start": loki_start, "end": loki_end},
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None
