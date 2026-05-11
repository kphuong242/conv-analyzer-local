"""Load ActionItems (the platform's per-call ticket records) for the target call.

Eval-safe projection. Fields that would let the model copy the answer
instead of deriving one are not loaded:

  - comments              → contain human-written conclusions
  - responsible_team      → maps 1:1 to the analyzer's `team_recommendation`
  - tags                  → maps 1:1 to the analyzer's `tags`
  - status, dt_resolved,
    resolver_id           → leak resolution state ("someone already fixed it")
  - sub_tasks             → leak investigation decomposition

What's kept is intake-time information about the symptom (title,
description, priority, job, assignment, assignee, dt_created/eta) — i.e.
what was known when the ticket was opened, not what was concluded.
"""

from __future__ import annotations

from typing import Any

from context_loaders._db import coerce_json, escape_sql_literal, parse_rows
from mcp_clients import McpHub

TICKET_FIELDS = [
    "id",
    "title",
    "description",
    "priority",
    "job",
    "assignment",
    "assignee_id",
    "reviewer_id",
    "dt_reviewed",
    "dt_created",
    "dt_updated",
    "dt_eta",
    "flag_type",
    "feedback_card_ids",
    "meta_data",
]


async def load(hub: McpHub, call_id: str, **_: Any) -> dict[str, Any]:
    sql = (
        f"SELECT {', '.join(TICKET_FIELDS)} FROM actionitems "
        f"WHERE call_or_conversation_id = '{escape_sql_literal(call_id)}' "
        f"ORDER BY dt_created DESC"
    )
    raw = await hub.call_tool("prod_execute_sql", {"sql": sql})
    rows = parse_rows(raw)

    tickets: list[dict[str, Any]] = []
    for row in rows:
        tickets.append({
            "id": row.get("id"),
            "title": row.get("title"),
            "description": row.get("description"),
            "priority": row.get("priority"),
            "job": row.get("job"),
            "assignment": row.get("assignment"),
            "assignee_id": row.get("assignee_id"),
            "reviewer_id": row.get("reviewer_id"),
            "dt_reviewed": row.get("dt_reviewed"),
            "dt_created": row.get("dt_created"),
            "dt_updated": row.get("dt_updated"),
            "dt_eta": row.get("dt_eta"),
            "flag_type": row.get("flag_type"),
            "feedback_card_ids": coerce_json(row.get("feedback_card_ids")) or [],
            "meta_data": row.get("meta_data"),
        })

    return {
        "ticket_count": len(tickets),
        "tickets": tickets,
    }
