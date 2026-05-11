"""Load ActionItems (the platform's per-call ticket records) for the target call.

Maps onto Q&A about the call:
  - Has someone already raised this issue?   → look at title / description
  - What was the operator's read on it?      → comments
  - Was it triaged or resolved?              → status, resolver_id, dt_resolved
  - Which team owned it?                     → responsible_team, tags

Two filters are applied to comments before they reach the model:

1. `type != "comment"` is dropped. ActionItemCommentBlock has a `type`
   field ("comment" | "log"); every audit-trail entry written by nexus
   ("<user> changed status from X to Y", sub-task edits, agent-owner
   reassignments, etc.) is `type="log"`. Those entries are workflow
   metadata, not analyst input, so they're stripped. The count surfaces
   as `dropped_log_comments`.
2. Comments whose `content` contains a UUID that isn't the target
   call_id are dropped (cross-references to sibling calls). The count
   surfaces as `dropped_cross_reference_comments`.

Limitation on filter (2): only UUID-shaped IDs are detected. Twilio
call_sids (CA-prefixed hex) and other non-UUID references slip through.
"""

from __future__ import annotations

import re
from typing import Any

from context_loaders._db import coerce_json, escape_sql_literal, parse_rows
from mcp_clients import McpHub

UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

TICKET_FIELDS = [
    "id",
    "title",
    "description",
    "status",
    "priority",
    "tags",
    "job",
    "assignment",
    "assignee_id",
    "responsible_team",
    "reviewer_id",
    "dt_reviewed",
    "resolver_id",
    "dt_resolved",
    "dt_created",
    "dt_updated",
    "dt_eta",
    "flag_type",
    "comments",
    "sub_tasks",
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
    total_dropped_logs = 0
    total_dropped_xref = 0

    for row in rows:
        raw_comments = coerce_json(row.get("comments")) or []
        filtered, dropped_logs, dropped_xref = _filter_comments(raw_comments, call_id)
        total_dropped_logs += dropped_logs
        total_dropped_xref += dropped_xref

        tickets.append({
            "id": row.get("id"),
            "title": row.get("title"),
            "description": row.get("description"),
            "status": row.get("status"),
            "priority": row.get("priority"),
            "job": row.get("job"),
            "assignment": row.get("assignment"),
            "tags": coerce_json(row.get("tags")) or [],
            "assignee_id": row.get("assignee_id"),
            "responsible_team": row.get("responsible_team"),
            "reviewer_id": row.get("reviewer_id"),
            "dt_reviewed": row.get("dt_reviewed"),
            "resolver_id": row.get("resolver_id"),
            "dt_resolved": row.get("dt_resolved"),
            "dt_created": row.get("dt_created"),
            "dt_updated": row.get("dt_updated"),
            "dt_eta": row.get("dt_eta"),
            "flag_type": row.get("flag_type"),
            "feedback_card_ids": coerce_json(row.get("feedback_card_ids")) or [],
            "meta_data": row.get("meta_data"),
            "sub_tasks": coerce_json(row.get("sub_tasks")) or [],
            "comments": filtered,
            "dropped_log_comments": dropped_logs,
            "dropped_cross_reference_comments": dropped_xref,
        })

    return {
        "ticket_count": len(tickets),
        "total_dropped_log_comments": total_dropped_logs,
        "total_dropped_cross_reference_comments": total_dropped_xref,
        "tickets": tickets,
    }


def _filter_comments(comments: Any, call_id: str) -> tuple[list[dict[str, Any]], int, int]:
    """Returns (kept, dropped_log_count, dropped_xref_count)."""
    if not isinstance(comments, list):
        return [], 0, 0
    target = call_id.lower()
    kept: list[dict[str, Any]] = []
    dropped_logs = 0
    dropped_xref = 0
    for c in comments:
        if not isinstance(c, dict):
            continue
        if c.get("type") != "comment":
            dropped_logs += 1
            continue
        text = c.get("content") or ""
        if _mentions_other_call(text, target):
            dropped_xref += 1
            continue
        kept.append({
            "content": text,
            "dt_created": c.get("dt_created"),
            "assets": c.get("assets"),
        })
    return kept, dropped_logs, dropped_xref


def _mentions_other_call(text: str, target_call_id: str) -> bool:
    matches = UUID_RE.findall(text)
    return any(m.lower() != target_call_id for m in matches)
