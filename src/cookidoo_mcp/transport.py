"""Transport strategies for running the MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .config import Settings, TransportMode

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


class Transport(Protocol):
    """Pluggable strategy that runs a FastMCP instance."""

    def run(self, mcp: FastMCP) -> None: ...


class StdioTransport:
    """Subprocess-style stdio transport used by Claude Desktop."""

    def run(self, mcp: FastMCP) -> None:
        mcp.run(transport="stdio")


class HttpTransport:
    """Streamable HTTP transport for remote MCP clients."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def run(self, mcp: FastMCP) -> None:
        mcp.settings.host = self.host
        mcp.settings.port = self.port
        mcp.run(transport="streamable-http")


def transport_from_settings(settings: Settings) -> Transport:
    """Pick the configured transport implementation."""
    match settings.mcp_mode:
        case TransportMode.STDIO:
            return StdioTransport()
        case TransportMode.HTTP:
            return HttpTransport(settings.mcp_host, settings.mcp_port)
