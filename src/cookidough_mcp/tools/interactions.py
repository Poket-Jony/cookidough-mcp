"""Recipe interaction tools: rating, bookmark, personal note, cooked-history.

These wrap Cookidoo endpoints that are not part of ``cookidoo-api``; the
session layer talks to them directly over the authenticated HTTP channel.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..errors import NotFoundError, UpstreamApiError
from ..models import CookedRecipe, RecipeInteractionResult, RecipeSearchResult
from ..session import CookidoughSessionProtocol

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from mcp.server.mcpserver import MCPServer


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def set_recipe_interactions(
        ctx: ToolContext,
        recipe_id: str,
        rating: int | None = None,
        bookmarked: bool | None = None,
        note: str | None = None,
        mark_cooked: bool = False,
        is_custom_recipe: bool = False,
        cooked_at: datetime | None = None,
    ) -> RecipeInteractionResult:
        """Set the user's interactions with a recipe in one call.

        Provide at least one action: ``rating`` (1-5 stars), ``bookmarked``
        (true saves, false removes the bookmark), ``note`` (personal note
        text; an empty string deletes the note), ``mark_cooked`` (true logs
        the recipe in the cooking history). Set ``is_custom_recipe=true``
        when logging one of your own recipes as cooked; rating, bookmark
        and note apply to catalogue recipes only. ``cooked_at`` (ISO-8601
        with a timezone, not in the future) backdates the cooked entry;
        without it Cookidoo stamps the current time. The history keeps one
        entry per recipe and the newest timestamp wins, so an older date
        never replaces a newer one.

        Actions run independently — the result reports ``"ok"`` or
        ``"failed: …"`` per action instead of failing the whole call.
        Read everything back via ``get_recipe_details`` with
        ``include_interactions=true``.
        """
        if cooked_at is not None:
            _validate_cooked_at(cooked_at, mark_cooked=mark_cooked)
        if rating is None and bookmarked is None and note is None and not mark_cooked:
            raise ValueError(
                "Provide at least one action: rating, bookmarked, note or mark_cooked."
            )
        session = get_context(ctx).session
        actions = _build_actions(
            session,
            recipe_id,
            rating=rating,
            bookmarked=bookmarked,
            note=note,
            mark_cooked=mark_cooked,
            is_custom_recipe=is_custom_recipe,
            cooked_at=cooked_at,
        )
        outcomes = dict(
            await asyncio.gather(*(_run_action(field, coro) for field, coro in actions))
        )
        return RecipeInteractionResult(recipe_id=recipe_id, **outcomes)

    @mcp.tool()
    async def list_bookmarked_recipes(ctx: ToolContext) -> list[RecipeSearchResult]:
        """List the recipes the user has bookmarked ("My recipes")."""
        return await get_context(ctx).session.list_bookmarked_recipes()

    @mcp.tool()
    async def get_cooking_history(ctx: ToolContext, limit: int = 20) -> list[CookedRecipe]:
        """List the recipes the user has logged as cooked, newest first."""
        return await get_context(ctx).session.get_cooking_history(limit)


def _build_actions(
    session: CookidoughSessionProtocol,
    recipe_id: str,
    *,
    rating: int | None,
    bookmarked: bool | None,
    note: str | None,
    mark_cooked: bool,
    is_custom_recipe: bool,
    cooked_at: datetime | None,
) -> list[tuple[str, Coroutine[Any, Any, None]]]:
    actions: list[tuple[str, Coroutine[Any, Any, None]]] = []
    if rating is not None:
        actions.append(("rating", session.rate_recipe(recipe_id, rating)))
    if bookmarked is not None:
        actions.append(("bookmark", session.set_recipe_bookmark(recipe_id, bookmarked)))
    if note is not None:
        actions.append(("note", session.set_recipe_note(recipe_id, note)))
    if mark_cooked:
        cooked = session.mark_recipe_cooked(recipe_id, is_custom_recipe, cooked_at)
        actions.append(("cooked", cooked))
    return actions


def _validate_cooked_at(cooked_at: datetime, *, mark_cooked: bool) -> None:
    if not mark_cooked:
        raise ValueError("cooked_at requires mark_cooked=true.")
    if cooked_at.tzinfo is None:
        raise ValueError("cooked_at needs a timezone, e.g. 2026-09-21T13:00:00-03:00.")
    # Entries can be neither edited nor deleted, so a future date would stay on top for good.
    if cooked_at > datetime.now(UTC):
        raise ValueError("cooked_at must not lie in the future.")


async def _run_action(field: str, coro: Coroutine[Any, Any, None]) -> tuple[str, str]:
    # AuthenticationError propagates — if the session is broken, every
    # action fails the same way and a per-field report would only obscure it.
    try:
        await coro
    except (NotFoundError, UpstreamApiError) as e:
        return field, f"failed: {e}"
    return field, "ok"
