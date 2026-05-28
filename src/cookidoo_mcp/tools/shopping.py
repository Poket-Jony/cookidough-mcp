"""Shopping list tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..models import (
    AdditionalItemRename,
    ShoppingItemOwnershipUpdate,
    ShoppingList,
    ShoppingListItem,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_shopping_list(ctx: ToolContext) -> ShoppingList:
        """Return all items on the user's shopping list, grouped by source."""
        return await get_context(ctx).session.get_shopping_list()

    @mcp.tool()
    async def add_recipes_to_shopping_list(ctx: ToolContext, recipe_ids: list[str]) -> str:
        """Add all ingredients of one or more recipes to the shopping list."""
        added = await get_context(ctx).session.add_recipes_to_shopping_list(recipe_ids)
        return (
            f"Added ingredients of {len(recipe_ids)} recipe(s); "
            f"{added} new item(s) appended to the list."
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
