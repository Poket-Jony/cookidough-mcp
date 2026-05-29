"""Tests for the MCP tool modules exercised via direct handler calls."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from cookidough_mcp.context import AppContext
from cookidough_mcp.errors import QualityGateError
from cookidough_mcp.models import (
    AdditionalItemRename,
    CustomRecipeDraft,
    RecipeStep,
    ShoppingItemOwnershipUpdate,
)
from cookidough_mcp.tools import register_all

from ._mcp_internals import get_tool_fn as _tool_fn


def _steps(*texts: str) -> list[RecipeStep]:
    return [RecipeStep(text=text) for text in texts]


@pytest.fixture
def registered_mcp() -> FastMCP:
    mcp = FastMCP(name="test-cookidoo")
    register_all(mcp)
    return mcp


def _low_quality_draft() -> CustomRecipeDraft:
    return CustomRecipeDraft(
        name="Bare",
        ingredients=["Rice", "Saffron", "Cardamom", "Pistachio"],
        steps=_steps(
            "Combine rice and water.",
            "Cook.",
            "Wait.",
            "Eat.",
        ),
    )


async def test_get_user_profile_returns_profile(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    profile = await _tool_fn(registered_mcp, "get_user_profile")(fake_mcp_context)
    assert profile.username == "alice"


async def test_get_subscription_returns_active(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    sub = await _tool_fn(registered_mcp, "get_subscription")(fake_mcp_context)
    assert sub is not None
    assert sub.active is True


async def test_get_recipe_details(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    details = await _tool_fn(registered_mcp, "get_recipe_details")(fake_mcp_context, recipe_id="r1")
    assert details.id == "r1"


async def test_list_managed_collections(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    collections = await _tool_fn(registered_mcp, "list_managed_collections")(fake_mcp_context)
    assert collections[0].id == "mc1"


async def test_add_recipes_to_shopping_list_returns_message(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    message = await _tool_fn(registered_mcp, "add_recipes_to_shopping_list")(
        fake_mcp_context, recipe_ids=["r1", "r2"]
    )
    assert "2 recipe(s)" in message
    assert "new item(s) appended" in message
    assert fake_session.calls.add_recipes_to_shopping_list == [["r1", "r2"]]


async def test_clear_shopping_list(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    message = await _tool_fn(registered_mcp, "clear_shopping_list")(fake_mcp_context)
    assert message == "Shopping list cleared."


async def test_get_calendar_week(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    days = await _tool_fn(registered_mcp, "get_calendar_week")(
        fake_mcp_context, day=date(2026, 5, 21)
    )
    assert days[0].id == "2026-05-21"


async def test_generate_recipe_structure(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    draft = await _tool_fn(registered_mcp, "generate_recipe_structure")(
        fake_mcp_context,
        name="Soup",
        ingredients=["Water"],
        steps=["Boil water for 5 min / 100 °C / speed 1 with the simmering basket."],
    )
    assert draft.name == "Soup"


async def test_validate_recipe_quality_returns_report(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    draft = CustomRecipeDraft(
        name="Bare",
        ingredients=["Water"],
        steps=_steps("Boil water."),
    )
    report = await _tool_fn(registered_mcp, "validate_recipe_quality")(
        fake_mcp_context, draft=draft
    )
    assert 0 <= report.score <= 100


async def test_upload_custom_recipe_refuses_low_quality(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    draft = _low_quality_draft()
    with pytest.raises(QualityGateError):
        await _tool_fn(registered_mcp, "upload_custom_recipe")(
            fake_mcp_context, draft=draft, force=False
        )


async def test_upload_custom_recipe_force_uploads(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    draft = _low_quality_draft()
    result = await _tool_fn(registered_mcp, "upload_custom_recipe")(
        fake_mcp_context, draft=draft, force=True
    )
    assert result.recipe_id == "new-id"
    assert fake_session.calls.upload_drafts


def _high_quality_draft() -> CustomRecipeDraft:
    return CustomRecipeDraft(
        name="Carbonara",
        ingredients=["200 g Spaghetti", "100 g Pancetta", "2 Eier", "50 g Parmesan"],
        steps=_steps(
            "Spaghetti in den Mixtopf geben, 1500 g Wasser dazu, "
            "10 Min / 100 °C / Stufe 1 mit Spatel kochen.",
            "Pancetta im Varoma-Aufsatz 8 Min / Varoma / Stufe 1 anbraten, "
            "danach mit dem Spatel umrühren.",
            "Eier und Parmesan im Messbecher verquirlen, dann über die "
            "Spaghetti geben, 30 Sek / Stufe 3 vermengen.",
        ),
        servings=2,
        prep_minutes=10,
        total_minutes=25,
    )


async def test_import_web_recipe_returns_draft_when_blocked(
    registered_mcp: FastMCP, fake_mcp_context: Any, app_context: AppContext, fake_session: Any
) -> None:
    """A low-quality scrape returns the draft + quality report, never uploads."""
    low = _low_quality_draft()
    app_context.importer.fetch = AsyncMock(return_value=low)  # type: ignore[method-assign]

    result = await _tool_fn(registered_mcp, "import_web_recipe")(
        fake_mcp_context, url="https://example.com/recipe", force=False
    )
    assert result.draft.name == low.name
    assert result.draft.steps == low.steps
    assert result.quality.meets_bar is False
    assert result.upload is None
    assert result.blocked_reason is not None
    assert "upload_custom_recipe" in result.blocked_reason
    assert not fake_session.calls.upload_drafts


async def test_import_web_recipe_uploads_when_quality_passes(
    registered_mcp: FastMCP, fake_mcp_context: Any, app_context: AppContext, fake_session: Any
) -> None:
    high = _high_quality_draft()
    app_context.importer.fetch = AsyncMock(return_value=high)  # type: ignore[method-assign]

    result = await _tool_fn(registered_mcp, "import_web_recipe")(
        fake_mcp_context, url="https://example.com/recipe", force=False
    )
    assert result.quality.meets_bar is True
    assert result.upload is not None
    assert result.upload.recipe_id == "new-id"
    assert result.blocked_reason is None
    assert fake_session.calls.upload_drafts == [high]


async def test_import_web_recipe_force_uploads_low_quality(
    registered_mcp: FastMCP, fake_mcp_context: Any, app_context: AppContext, fake_session: Any
) -> None:
    """`force=True` uploads even when the gate would have blocked."""
    low = _low_quality_draft()
    app_context.importer.fetch = AsyncMock(return_value=low)  # type: ignore[method-assign]

    result = await _tool_fn(registered_mcp, "import_web_recipe")(
        fake_mcp_context, url="https://example.com/recipe", force=True
    )
    assert result.quality.meets_bar is False
    assert result.upload is not None
    assert result.upload.recipe_id == "new-id"
    assert result.blocked_reason is None
    assert fake_session.calls.upload_drafts == [low]


async def test_list_custom_recipes(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    items = await _tool_fn(registered_mcp, "list_custom_recipes")(fake_mcp_context)
    assert items[0].recipe_id == "cr1"


async def test_delete_custom_recipe(registered_mcp: FastMCP, fake_mcp_context: Any) -> None:
    message = await _tool_fn(registered_mcp, "delete_custom_recipe")(
        fake_mcp_context, recipe_id="cr1"
    )
    assert "cr1" in message


async def test_clone_recipe_as_custom(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    result = await _tool_fn(registered_mcp, "clone_recipe_as_custom")(
        fake_mcp_context, recipe_id="r42", serving_size=2
    )
    assert result.id == "clone-of-r42"
    assert result.serving_size == 2
    assert fake_session.calls.clone_recipe_as_custom == [("r42", 2)]


async def test_add_custom_recipes_to_calendar(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    day = date(2026, 6, 1)
    result = await _tool_fn(registered_mcp, "add_custom_recipes_to_calendar")(
        fake_mcp_context, day=day, recipe_ids=["cr1", "cr2"]
    )
    assert result.custom_recipe_ids == ["cr1", "cr2"]
    assert fake_session.calls.add_custom_recipes_to_calendar == [(day, ["cr1", "cr2"])]


async def test_remove_custom_recipe_from_calendar(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    day = date(2026, 6, 1)
    result = await _tool_fn(registered_mcp, "remove_custom_recipe_from_calendar")(
        fake_mcp_context, day=day, recipe_id="cr1"
    )
    assert result.id == day.isoformat()


async def test_add_custom_recipes_to_shopping_list_returns_message(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    message = await _tool_fn(registered_mcp, "add_custom_recipes_to_shopping_list")(
        fake_mcp_context, recipe_ids=["cr1", "cr2"]
    )
    assert "2 custom recipe(s)" in message
    assert fake_session.calls.add_custom_recipes_to_shopping_list == [["cr1", "cr2"]]


async def test_remove_custom_recipes_from_shopping_list(
    registered_mcp: FastMCP, fake_mcp_context: Any
) -> None:
    message = await _tool_fn(registered_mcp, "remove_custom_recipes_from_shopping_list")(
        fake_mcp_context, recipe_ids=["cr1"]
    )
    assert "1 custom recipe(s)" in message


async def test_set_ingredient_items_ownership(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    items = await _tool_fn(registered_mcp, "set_ingredient_items_ownership")(
        fake_mcp_context,
        updates=[
            ShoppingItemOwnershipUpdate(id="i1", is_owned=True),
            ShoppingItemOwnershipUpdate(id="i2", is_owned=False),
        ],
    )
    assert [item.id for item in items] == ["i1", "i2"]
    assert items[0].is_owned is True
    assert fake_session.calls.set_ingredient_ownership[0][0].id == "i1"


async def test_set_additional_items_ownership(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    items = await _tool_fn(registered_mcp, "set_additional_items_ownership")(
        fake_mcp_context,
        updates=[ShoppingItemOwnershipUpdate(id="a1", is_owned=True)],
    )
    assert items[0].source == "additional"
    assert fake_session.calls.set_additional_ownership


async def test_rename_additional_items(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    items = await _tool_fn(registered_mcp, "rename_additional_items")(
        fake_mcp_context,
        updates=[AdditionalItemRename(id="a1", name="Sea salt")],
    )
    assert items[0].name == "Sea salt"
    assert fake_session.calls.rename_additional[0][0].name == "Sea salt"


async def test_search_recipes(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    results = await _tool_fn(registered_mcp, "search_recipes")(
        fake_mcp_context, query="pasta", limit=5
    )
    assert results[0].id == "s1"
    assert "pasta" in results[0].name
    assert fake_session.calls.search_recipes == [("pasta", 5)]


async def test_suggest_recipes_from_ingredients(
    registered_mcp: FastMCP, fake_mcp_context: Any, fake_session: Any
) -> None:
    results = await _tool_fn(registered_mcp, "suggest_recipes_from_ingredients")(
        fake_mcp_context,
        available_ingredients=["rice"],
        collection_ids=None,
        max_results=5,
    )
    assert results[0].score == 1.0
    assert results[0].matching_ingredients == ["rice"]
    assert fake_session.calls.suggest_calls == [(["rice"], None, 5)]
