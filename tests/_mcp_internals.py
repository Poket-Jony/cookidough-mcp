"""Single chokepoint for the FastMCP private API used by tool tests.

`FastMCP.call_tool` requires a fully bootstrapped MCP request context, but our
tool tests inject a custom `AppContext` via `fake_mcp_context` and call the
underlying function directly. The only way to retrieve that function today is
through FastMCP's internal tool manager. Isolating the access here means a
single line breaks if FastMCP rearranges its internals, instead of a dozen
test call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def get_tool_fn(mcp: FastMCP, name: str) -> Any:
    """Return the raw async tool function registered under `name`."""
    tool = mcp._tool_manager.get_tool(name)
    if tool is None:
        raise KeyError(f"Tool {name!r} is not registered.")
    return tool.fn
