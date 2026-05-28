"""Tests for the FastMCP assembly."""

from __future__ import annotations

import re
from pathlib import Path

from cookidoo_mcp.config import Settings
from cookidoo_mcp.server import build_server

_EXPECTED_TOOL_NAMES = frozenset(
    {
        "get_user_profile",
        "get_subscription",
        "get_recipe_details",
        "get_custom_recipe_details",
        "list_managed_collections",
        "add_managed_collection",
        "remove_managed_collection",
        "list_custom_collections",
        "create_custom_collection",
        "delete_custom_collection",
        "add_recipes_to_custom_collection",
        "remove_recipe_from_custom_collection",
        "get_shopping_list",
        "add_recipes_to_shopping_list",
        "remove_recipes_from_shopping_list",
        "add_additional_items",
        "remove_additional_items",
        "clear_shopping_list",
        "get_calendar_week",
        "add_recipes_to_calendar",
        "remove_recipe_from_calendar",
        "generate_recipe_structure",
        "validate_recipe_quality",
        "upload_custom_recipe",
        "list_custom_recipes",
        "delete_custom_recipe",
        "import_web_recipe",
        "clone_recipe_as_custom",
        "add_custom_recipes_to_calendar",
        "remove_custom_recipe_from_calendar",
        "add_custom_recipes_to_shopping_list",
        "remove_custom_recipes_from_shopping_list",
        "set_ingredient_items_ownership",
        "set_additional_items_ownership",
        "rename_additional_items",
        "search_recipes",
        "suggest_recipes_from_ingredients",
    }
)


async def test_build_server_registers_all_tools(settings: Settings) -> None:
    mcp = build_server(settings)
    tool_names = {tool.name for tool in await mcp.list_tools()}
    # Use equality (not subset) so a stray tool registration or an
    # accidental rename surfaces immediately. The README references this
    # exact tool count — see `test_readme_tool_count_matches_registration`.
    assert tool_names == _EXPECTED_TOOL_NAMES


_README_PATH = Path(__file__).resolve().parent.parent / "README.md"


def _readme_tool_count() -> int:
    """Pull the ``N`` from the README's "N MCP tools" advertisement."""
    match = re.search(r"(\d+)\s+MCP tools", _README_PATH.read_text())
    assert match is not None, "README no longer advertises an MCP tool count"
    return int(match.group(1))


async def test_readme_tool_count_matches_registration(settings: Settings) -> None:
    """Guard the README's "N MCP tools" claim against silent drift."""
    mcp = build_server(settings)
    registered = {tool.name for tool in await mcp.list_tools()}
    claimed = _readme_tool_count()
    assert claimed == len(registered), (
        f"README claims {claimed} MCP tools but {len(registered)} are registered"
    )
