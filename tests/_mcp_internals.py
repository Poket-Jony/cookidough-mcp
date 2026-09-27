"""Single chokepoint for the MCPServer private API used by tool tests.

`MCPServer.call_tool` requires a fully bootstrapped MCP request context, but our
tool tests inject a custom `AppContext` via `fake_mcp_context` and call the
underlying function directly. The only way to retrieve that function today is
through MCPServer's internal tool manager. Isolating the access here means a
single line breaks if MCPServer rearranges its internals, instead of a dozen
test call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def get_tool_fn(mcp: MCPServer, name: str) -> Any:
    """Return the raw async tool function registered under `name`."""
    tool = mcp._tool_manager.get_tool(name)
    if tool is None:
        raise KeyError(f"Tool {name!r} is not registered.")
    return tool.fn


def get_tool_parameters(mcp: MCPServer, name: str) -> dict[str, Any]:
    """Return the JSON schema of the arguments the tool announces to clients."""
    tool = mcp._tool_manager.get_tool(name)
    if tool is None:
        raise KeyError(f"Tool {name!r} is not registered.")
    return tool.parameters


def get_resource_fn(mcp: MCPServer, uri: str) -> Any:
    """Return the raw function backing the static resource at `uri`."""
    for resource in mcp._resource_manager.list_resources():
        if str(resource.uri) == uri:
            return resource.fn  # type: ignore[attr-defined]
    raise KeyError(f"Resource {uri!r} is not registered.")


def get_prompt_fn(mcp: MCPServer, name: str) -> Any:
    """Return the raw function backing the prompt registered under `name`."""
    for prompt in mcp._prompt_manager.list_prompts():
        if prompt.name == name:
            return prompt.fn
    raise KeyError(f"Prompt {name!r} is not registered.")


def get_lifespan(mcp: MCPServer) -> Any:
    """Return the registered lifespan context manager factory."""
    return mcp.settings.lifespan
