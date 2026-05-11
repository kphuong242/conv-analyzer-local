"""MCP client wiring for db-toolbox-prod and grafana-prod-vpn.

Both servers are reachable only on the GetVocal internal network — the script
must be run from a machine with VPN access (same as Claude Code's MCP setup).

The two servers expose different transports:
  - db-toolbox-prod:  Streamable HTTP  (POST /mcp)
  - grafana-prod-vpn: SSE              (GET /sse)

`McpHub` keeps both sessions open for the lifetime of one analysis run and
exposes a single `call_tool` entry point that dispatches by tool name.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.session import ClientSession as _ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str

    def to_openai(self) -> dict[str, Any]:
        """Tool schema for the OpenAI Responses API (`/v1/responses`).

        Note this is flatter than the Chat Completions shape — no nested
        `function: {...}` wrapper. Reasoning models like gpt-5.4 only support
        function tools through the Responses endpoint.
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


@dataclass
class McpHub:
    db_session: _ClientSession
    grafana_session: _ClientSession
    tools: list[ToolSpec] = field(default_factory=list)
    _by_name: dict[str, ToolSpec] = field(default_factory=dict)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._by_name.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name}")
        session = self.db_session if spec.server == "db-toolbox-prod" else self.grafana_session
        result = await session.call_tool(name, arguments)
        return _stringify_tool_result(result)


def _stringify_tool_result(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(repr(block))
    if getattr(result, "isError", False):
        return "[tool error]\n" + "\n".join(parts)
    return "\n".join(parts) if parts else ""


@contextlib.asynccontextmanager
async def open_hub(
    db_url: str,
    grafana_url: str,
    grafana_tool_allowlist: set[str],
    db_tool_allowlist: set[str] | None = None,
) -> AsyncIterator[McpHub]:
    """Open both MCP sessions and return a McpHub.

    `grafana_tool_allowlist` mirrors the kagent's `toolNames` list — only those
    Grafana tools are exposed to the model. Pass `db_tool_allowlist=None` to
    expose every tool the db server reports (the default; context loaders use
    them directly, the model never sees them).
    """
    async with streamablehttp_client(db_url) as (db_read, db_write, _):
        async with ClientSession(db_read, db_write) as db_session:
            await db_session.initialize()
            async with sse_client(grafana_url) as (g_read, g_write):
                async with ClientSession(g_read, g_write) as grafana_session:
                    await grafana_session.initialize()

                    hub = McpHub(db_session=db_session, grafana_session=grafana_session)
                    db_tools = await db_session.list_tools()
                    for tool in db_tools.tools:
                        if db_tool_allowlist is not None and tool.name not in db_tool_allowlist:
                            continue
                        spec = ToolSpec(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                            server="db-toolbox-prod",
                        )
                        hub.tools.append(spec)
                        hub._by_name[spec.name] = spec

                    grafana_tools = await grafana_session.list_tools()
                    for tool in grafana_tools.tools:
                        if tool.name not in grafana_tool_allowlist:
                            continue
                        spec = ToolSpec(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                            server="grafana-prod-vpn",
                        )
                        hub.tools.append(spec)
                        hub._by_name[spec.name] = spec

                    yield hub
