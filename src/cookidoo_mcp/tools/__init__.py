"""Tool registration entrypoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import auth, calendar, collections, discovery, recipes, shopping

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Register all tool modules onto the given FastMCP instance."""
    for module in (auth, recipes, collections, shopping, calendar, discovery):
        module.register(mcp)


__all__ = ["register_all"]
