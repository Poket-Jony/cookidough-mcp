"""Lifespan-scoped application context shared with every tool invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context

from .config import Settings
from .quality import QualityScorer
from .session import CookidooSessionProtocol
from .web_import import WebRecipeImporter


@dataclass(frozen=True)
class AppContext:
    """Dependencies injected into every tool call via FastMCP's lifespan."""

    settings: Settings
    session: CookidooSessionProtocol
    scorer: QualityScorer
    importer: WebRecipeImporter


ToolContext = Context[Any, AppContext, Any]


def get_context(ctx: ToolContext) -> AppContext:
    """Retrieve the injected `AppContext` from a tool's `Context`."""
    return ctx.request_context.lifespan_context
