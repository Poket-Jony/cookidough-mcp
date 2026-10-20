"""Tests for the MCPServer assembly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cookidough_mcp.config import Settings
from cookidough_mcp.context import AppContext
from cookidough_mcp.server import build_server

_EXPECTED_TOOL_NAMES = frozenset(
    {
        "get_user_profile",
        "get_subscription",
        "get_recipe_details",
        "get_custom_recipe_details",
        "get_thermomix_modes",
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
        "get_recipe_recommendations",
        "set_recipe_interactions",
        "list_bookmarked_recipes",
        "set_custom_recipe_image",
        "get_cooking_history",
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


_EXPECTED_RESOURCE_URIS = frozenset(
    {
        "cookidough://shopping-list",
        "cookidough://calendar/current-week",
        "cookidough://custom-recipes",
        "cookidough://modes",
    }
)


async def test_build_server_registers_resources_and_prompts(settings: Settings) -> None:
    mcp = build_server(settings)
    resource_uris = {str(r.uri) for r in await mcp.list_resources()}
    prompt_names = {p.name for p in await mcp.list_prompts()}
    assert resource_uris == _EXPECTED_RESOURCE_URIS
    assert prompt_names == {"plan_week", "cook_from_pantry"}


async def test_shopping_list_resource_serializes_session_state(
    app_context: AppContext,
) -> None:
    import json

    from mcp.server.mcpserver import MCPServer

    from cookidough_mcp import resources

    from ._mcp_internals import get_resource_fn

    mcp = MCPServer(name="test-cookidough")
    resources.register(mcp, app_context)

    payload = json.loads(await get_resource_fn(mcp, "cookidough://shopping-list")())

    assert payload["ingredient_items"][0]["name"] == "Tomato"


async def test_lifespan_yields_app_context_and_closes_session(settings: Settings) -> None:
    from cookidough_mcp.errors import UpstreamApiError

    from ._mcp_internals import get_lifespan

    mcp = build_server(settings)
    async with get_lifespan(mcp)(mcp) as app:
        assert isinstance(app, AppContext)
        assert app.settings is settings

    with pytest.raises(UpstreamApiError, match="closed"):
        await app.session.get_user_profile()


async def test_resources_share_the_lifespan_app_context(settings: Settings) -> None:
    """Resources close over the same AppContext the lifespan yields."""
    from cookidough_mcp.errors import UpstreamApiError

    from ._mcp_internals import get_lifespan, get_resource_fn

    mcp = build_server(settings)
    async with get_lifespan(mcp)(mcp):
        pass

    # The lifespan closed its session on exit; seeing that here proves the
    # resource handler holds that very context and not a second one.
    with pytest.raises(UpstreamApiError, match="closed"):
        await get_resource_fn(mcp, "cookidough://shopping-list")()


def test_plan_week_prompt_embeds_constraints(settings: Settings) -> None:
    from ._mcp_internals import get_prompt_fn

    mcp = build_server(settings)
    text = get_prompt_fn(mcp, "plan_week")(servings=4, diet="vegetarian")
    assert "4 person(s)" in text
    assert "vegetarian" in text
    assert "add_recipes_to_shopping_list" in text

    pantry = get_prompt_fn(mcp, "cook_from_pantry")(ingredients="rice, tomato")
    assert "rice, tomato" in pantry
    assert "suggest_recipes_from_ingredients" in pantry
