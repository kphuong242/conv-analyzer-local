"""Context loaders fetch supplementary information about a call before the agent runs.

Each loader is an async callable
    `async def load(hub, call_id, *, context=None, **kwargs) -> dict`
whose return value is merged into the context block prepended to the user message.

The registry lives in `analyze.py` (`LOADER_REGISTRY`). The `call` loader is
mandatory and runs first; everything else is opt-in via `--context <name>`.
Loaders may read earlier loaders' output via the `context` kwarg — e.g.
`conv_graph` reads `context["call"]["call"]["conv_graph_id"]` to know which
graph to fetch, avoiding a redundant DB round-trip.
"""
