"""Discovery tools: full-text recipe search and ingredient-based suggestions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..models import RecipeSearchResult, RecipeSuggestion

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def search_recipes(
        ctx: ToolContext, query: str, limit: int = 10
    ) -> list[RecipeSearchResult]:
        """Search the Cookidoo recipe library by keyword.

        Returns up to ``limit`` (default 10, max 50) matching recipes sorted
        by Cookidoo's own relevance ranking. The query is matched against the
        configured locale (`COOKIDOO_COUNTRY` / `COOKIDOO_LANGUAGE`).
        """
        return await get_context(ctx).session.search_recipes(query, limit)

    @mcp.tool()
    async def suggest_recipes_from_ingredients(
        ctx: ToolContext,
        available_ingredients: list[str],
        collection_ids: list[str] | None = None,
        max_results: int = 10,
    ) -> list[RecipeSuggestion]:
        """Suggest recipes from the user's collections by ingredient match.

        Walks the recipes inside the user's managed + custom collections (or
        only the specified ``collection_ids``) and ranks them by how many of
        ``available_ingredients`` they require. Each result carries the
        match score (0.0-1.0), the matching and missing ingredient names,
        and the full ``RecipeDetails`` payload.

        Tip: keep ``available_ingredients`` short and use head nouns
        (``"chicken"``, ``"rice"``) — substring matching means "rice"
        matches "basmati rice", "wild rice", etc.
        """
        return await get_context(ctx).session.suggest_recipes_from_ingredients(
            available_ingredients=available_ingredients,
            collection_ids=collection_ids,
            max_results=max_results,
        )
