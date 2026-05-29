"""FastMCP server assembly: lifespan, dependency injection and tool wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .context import AppContext
from .quality import QualityScorer
from .session import CookidooSession
from .tools import register_all
from .web_import import WebRecipeImporter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def build_server(settings: Settings | None = None) -> FastMCP:
    """Construct the FastMCP server with all tools registered."""
    resolved = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(_mcp: FastMCP) -> AsyncIterator[AppContext]:
        session = CookidooSession(resolved)
        try:
            yield AppContext(
                settings=resolved,
                session=session,
                scorer=QualityScorer(threshold=resolved.quality_bar),
                importer=WebRecipeImporter(),
            )
        finally:
            await session.aclose()

    mcp = FastMCP(name="cookidoo", lifespan=lifespan)
    register_all(mcp)
    return mcp
