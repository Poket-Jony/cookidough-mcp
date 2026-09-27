"""Shopping list tools."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..constants import CALENDAR_SHOPPING_MAX_RANGE_DAYS
from ..context import ToolContext, get_context
from ..models import (
    AdditionalItemRename,
    ShoppingItemOwnershipUpdate,
    ShoppingList,
    ShoppingListItem,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def get_shopping_list(ctx: ToolContext) -> ShoppingList:
        """Return all items on the user's shopping list, grouped by source.

        Also lists the ``recipes`` whose ingredients are currently on the
        list (with their IDs, usable for ``remove_recipes_from_shopping_list``).
        """
        return await get_context(ctx).session.get_shopping_list()

    @mcp.tool()
    async def add_recipes_to_shopping_list(
        ctx: ToolContext,
        recipe_ids: list[str] | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> str:
        """Add recipe ingredients to the shopping list (two modes).

        Pass EITHER ``recipe_ids`` (add the ingredients of those catalogue
        recipes) OR ``from_date`` + ``to_date`` (add every recipe planned in
        the calendar within that inclusive range — max 4 weeks — including
        custom recipes, deduplicated across days).
        """
        session = get_context(ctx).session
        range_given = from_date is not None or to_date is not None
        if (recipe_ids is not None) == range_given:
            raise ValueError(
                "Pass either recipe_ids or a from_date/to_date range — not both, not neither."
            )
        if recipe_ids is not None:
            added = await session.add_recipes_to_shopping_list(recipe_ids)
            return (
                f"Added ingredients of {len(recipe_ids)} recipe(s); "
                f"{added} new item(s) appended to the list."
            )
        if from_date is None or to_date is None:
            raise ValueError("Calendar mode needs both from_date and to_date.")
        if to_date < from_date:
            raise ValueError("to_date must not be before from_date.")
        if (to_date - from_date).days > CALENDAR_SHOPPING_MAX_RANGE_DAYS:
            raise ValueError(
                f"Date range exceeds {CALENDAR_SHOPPING_MAX_RANGE_DAYS} days; "
                f"split it into smaller chunks."
            )
        summary = await session.add_calendar_range_to_shopping_list(from_date, to_date)
        return (
            f"Added ingredients of {len(summary.recipe_ids)} recipe(s) and "
            f"{len(summary.custom_recipe_ids)} custom recipe(s) planned between "
            f"{from_date.isoformat()} and {to_date.isoformat()}; "
            f"{summary.item_count} new item(s) appended to the list."
        )

    @mcp.tool()
    async def remove_recipes_from_shopping_list(ctx: ToolContext, recipe_ids: list[str]) -> str:
        """Remove the ingredients of the given recipes from the shopping list."""
        await get_context(ctx).session.remove_recipes_from_shopping_list(recipe_ids)
        return f"Removed ingredients of {len(recipe_ids)} recipe(s)."

    @mcp.tool()
    async def add_additional_items(ctx: ToolContext, names: list[str]) -> list[ShoppingListItem]:
        """Append free-text items (not tied to a recipe) to the shopping list."""
        return await get_context(ctx).session.add_additional_items(names)

    @mcp.tool()
    async def remove_additional_items(ctx: ToolContext, item_ids: list[str]) -> str:
        """Remove the given free-text shopping list items by their IDs."""
        await get_context(ctx).session.remove_additional_items(item_ids)
        return f"Removed {len(item_ids)} additional item(s)."

    @mcp.tool()
    async def clear_shopping_list(ctx: ToolContext) -> str:
        """Remove every item from the shopping list."""
        await get_context(ctx).session.clear_shopping_list()
        return "Shopping list cleared."

    @mcp.tool()
    async def add_custom_recipes_to_shopping_list(ctx: ToolContext, recipe_ids: list[str]) -> str:
        """Add all ingredients of one or more **custom** recipes to the shopping list."""
        added = await get_context(ctx).session.add_custom_recipes_to_shopping_list(recipe_ids)
        return (
            f"Added ingredients of {len(recipe_ids)} custom recipe(s); "
            f"{added} new item(s) appended to the list."
        )

    @mcp.tool()
    async def remove_custom_recipes_from_shopping_list(
        ctx: ToolContext, recipe_ids: list[str]
    ) -> str:
        """Remove the ingredients of the given **custom** recipes from the shopping list."""
        await get_context(ctx).session.remove_custom_recipes_from_shopping_list(recipe_ids)
        return f"Removed ingredients of {len(recipe_ids)} custom recipe(s)."

    @mcp.tool()
    async def set_ingredient_items_ownership(
        ctx: ToolContext, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        """Check or uncheck ingredient items by ID.

        Pass one ``{"id": "...", "is_owned": true|false}`` entry per item to
        tick (already-bought) or untick it on the shopping list. Item IDs
        come from ``get_shopping_list`` (``ingredient_items[*].id``).
        """
        return await get_context(ctx).session.set_ingredient_items_ownership(updates)

    @mcp.tool()
    async def set_additional_items_ownership(
        ctx: ToolContext, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        """Check or uncheck free-text shopping list items by ID."""
        return await get_context(ctx).session.set_additional_items_ownership(updates)

    @mcp.tool()
    async def rename_additional_items(
        ctx: ToolContext, updates: list[AdditionalItemRename]
    ) -> list[ShoppingListItem]:
        """Rename free-text shopping list items in place by ID."""
        return await get_context(ctx).session.rename_additional_items(updates)
