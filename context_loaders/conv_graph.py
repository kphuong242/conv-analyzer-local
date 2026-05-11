"""Load the call's conversational graph + transcript + pre-computed analytics.

Maps directly onto prompt tags that logs alone can't decide:
  - graph_translation_issue       (need node text)
  - same_questions_several_times  (need node reach counts in analytics)
  - agent_not_ending_the_conversation (need to know which nodes are END)
  - action_node_error             (need node type)
  - conversation_not_starting     (need start node identity)

Source schema:
  - calls.conv_graph_id          → conversationalgraphs.id
  - calls.conversation_transcript (List[dict])
  - calls.conv_graph_analytics    (dict — populated by SmartCaller routine)
  - conversationalgraphs.graph    (V2Graph JSONB) — see datamodel/.../conversational_graphs.py

The graph is projected to a slim view (no coordinates, no node_settings,
no history) so the user-message footprint stays bounded. Each node's
`content` is truncated to NODE_CONTENT_TRUNC characters.
"""

from __future__ import annotations

from typing import Any

from context_loaders._db import coerce_json, escape_sql_literal, parse_rows
from mcp_clients import McpHub

NODE_CONTENT_TRUNC = 300


async def load(hub: McpHub, call_id: str, *, context: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    call_info = ((context or {}).get("call") or {}).get("call") or {}
    conv_graph_id = call_info.get("conv_graph_id")
    if not conv_graph_id:
        return {
            "graph_id": None,
            "graph": None,
            "transcript": None,
            "analytics": None,
            "analytics_status": None,
            "reason": "call has no conv_graph_id (likely a non-graph call path)",
        }

    sql = (
        "SELECT g.id AS graph_id, g.assistant_id AS graph_assistant_id, g.graph AS graph_json, "
        "c.conversation_transcript AS transcript, "
        "c.conv_graph_analytics AS analytics, "
        "c.conv_graph_analytics_status AS analytics_status "
        "FROM conversationalgraphs g "
        "JOIN calls c ON c.conv_graph_id = g.id "
        f"WHERE c.id = '{escape_sql_literal(call_id)}' LIMIT 1"
    )
    raw = await hub.call_tool("prod_execute_sql", {"sql": sql})
    rows = parse_rows(raw)
    if not rows:
        return {
            "graph_id": conv_graph_id,
            "graph": None,
            "transcript": None,
            "analytics": None,
            "analytics_status": None,
            "reason": f"graph {conv_graph_id} not found in DB join",
        }

    row = rows[0]
    graph_json = coerce_json(row.get("graph_json"))
    transcript = coerce_json(row.get("transcript"))
    analytics = coerce_json(row.get("analytics"))

    return {
        "graph_id": row.get("graph_id") or conv_graph_id,
        "graph_assistant_id": row.get("graph_assistant_id"),
        "graph": _project_graph(graph_json) if isinstance(graph_json, dict) else None,
        "transcript": transcript,
        "analytics": analytics,
        "analytics_status": row.get("analytics_status"),
    }


def _project_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Strip everything the analyzer doesn't need (coordinates, history,
    node_settings, conv_paths_ids) and keep just structure + labels."""
    nodes_dict = graph.get("nodes") or {}
    nodes: list[dict[str, Any]] = []
    for node_id, node in nodes_dict.items():
        if not isinstance(node, dict):
            continue
        content = node.get("content") or ""
        if isinstance(content, str) and len(content) > NODE_CONTENT_TRUNC:
            content = content[:NODE_CONTENT_TRUNC] + "…"
        nodes.append({
            "id": node_id,
            "type": node.get("type"),
            "content": content,
            "is_hidden": node.get("is_hidden"),
        })

    edges: list[dict[str, Any]] = []
    for edge in (graph.get("unassigned_edges") or []):
        if not isinstance(edge, dict):
            continue
        edges.append({
            "id": edge.get("id"),
            "source": edge.get("source_node_id"),
            "target": edge.get("target_node_id"),
            "kind": "unassigned",
            "user_prompt_count": len(edge.get("user_prompt_ids") or []),
        })
    for edge_id, edge in (graph.get("conditional_edges") or {}).items():
        if not isinstance(edge, dict):
            continue
        edges.append({
            "id": edge_id,
            "source": edge.get("source_node_id"),
            "target": edge.get("target_node_id"),
            "kind": "conditional",
        })

    return {
        "version": graph.get("version"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
        "pre_call_action_node_ids": graph.get("pre_call_action_node_ids") or [],
        "global_action_node_ids": graph.get("global_action_node_ids") or [],
        "edge_names": graph.get("edge_names") or {},
    }
