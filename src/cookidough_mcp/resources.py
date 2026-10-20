"""MCP resources and prompts: read-only context plus guided meal-planning workflows.

Resources let MCP clients pull frequently-needed state (shopping list, weekly
plan, custom recipes) without spending a tool call; prompts package the
multi-tool workflows this server is built for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .context import AppContext


def register(mcp: MCPServer, app: AppContext) -> None:
    """Register read-only resources and workflow prompts.

    ``app`` is passed in rather than resolved per request: mcp 2.0 refuses a
    ``Context`` parameter on a static resource URI.
    """

    @mcp.resource("cookidough://shopping-list", mime_type="application/json")
    async def shopping_list() -> str:
        """The current shopping list: items grouped by source, plus the recipes on it."""
        result = await app.session.get_shopping_list()
        return result.model_dump_json(indent=2)

    @mcp.resource("cookidough://calendar/current-week", mime_type="application/json")
    async def calendar_current_week() -> str:
        """The meal plan for the week containing today."""
        today = datetime.now(tz=UTC).astimezone().date()
        days = await app.session.get_calendar_week(today)
        return json.dumps([day.model_dump(mode="json") for day in days], indent=2)

    @mcp.resource("cookidough://custom-recipes", mime_type="application/json")
    async def custom_recipes() -> str:
        """All custom recipes owned by the authenticated user."""
        recipes = await app.session.list_custom_recipes()
        return json.dumps([recipe.model_dump(mode="json") for recipe in recipes], indent=2)

    @mcp.resource("cookidough://modes", mime_type="application/json")
    async def modes() -> str:
        """The appliance modes, speeds and temperatures the platform's recipe editor offers."""
        catalog = await app.session.get_mode_catalog()
        return catalog.model_dump_json(indent=2)

    @mcp.prompt()
    def plan_week(servings: int = 2, diet: str = "", max_minutes_per_meal: int = 45) -> str:
        """Plan a week of dinners and put everything on the shopping list."""
        diet_line = f"- Dietary preference: {diet}\n" if diet else ""
        return (
            f"Plan seven dinners for the coming week for {servings} "
            f"person(s).\n"
            f"Constraints:\n"
            f"{diet_line}"
            f"- Each recipe should take at most {max_minutes_per_meal} minutes "
            f"total time.\n"
            f"- Prefer variety: no main ingredient twice in a row.\n\n"
            f"Workflow:\n"
            f"1. Use search_recipes with matching filters (max_total_minutes, "
            f"ingredients/exclude_ingredients) to find candidates; check "
            f"details via get_recipe_details (several IDs per call) where needed.\n"
            f"2. Present the plan briefly and confirm with me before writing.\n"
            f"3. Schedule the chosen recipes with add_recipes_to_calendar "
            f"(one call per day).\n"
            f"4. Fill the shopping list in one go via "
            f"add_recipes_to_shopping_list with from_date/to_date covering "
            f"the week."
        )

    @mcp.prompt()
    def cook_from_pantry(ingredients: str) -> str:
        """Suggest what to cook tonight from the ingredients on hand."""
        return (
            f"I have these ingredients at home: {ingredients}.\n\n"
            f"Use suggest_recipes_from_ingredients to find recipes "
            f"that match (head nouns work best, e.g. 'chicken' instead of "
            f"'chicken breast'). Rank by match score, show the top three with "
            f"their missing ingredients, and offer to put the missing items "
            f"on my shopping list via add_additional_items."
        )
