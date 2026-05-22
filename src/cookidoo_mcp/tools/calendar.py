"""Meal plan / calendar tools."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..models import CalendarDay

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_calendar_week(ctx: ToolContext, day: date) -> list[CalendarDay]:
        """Return the meal plan for the calendar week containing the given date."""
        return await get_context(ctx).session.get_calendar_week(day)

    @mcp.tool()
    async def add_recipes_to_calendar(
        ctx: ToolContext, day: date, recipe_ids: list[str]
    ) -> CalendarDay:
        """Schedule one or more recipes for a specific date in the meal plan."""
        return await get_context(ctx).session.add_recipes_to_calendar(day, recipe_ids)

    @mcp.tool()
    async def remove_recipe_from_calendar(
        ctx: ToolContext, day: date, recipe_id: str
    ) -> CalendarDay:
        """Remove a single planned recipe from the given date."""
        return await get_context(ctx).session.remove_recipe_from_calendar(day, recipe_id)
