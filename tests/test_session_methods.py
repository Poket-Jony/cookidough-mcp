"""Behavioural tests for the remaining CookidoughSession methods using a fake client."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cookidoo_api.exceptions import CookidooRequestException

from cookidough_mcp.errors import NotFoundError, UpstreamApiError
from cookidough_mcp.models import (
    AdditionalItemRename,
    CustomRecipeDraft,
    RecipeStep,
    ShoppingItemOwnershipUpdate,
)
from cookidough_mcp.session import CookidoughSession


class _NS:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_collection() -> Any:
    return _NS(
        id="c",
        name="N",
        description=None,
        chapters=[_NS(name="x", recipes=[_NS()])],
    )


def _mock_collections(fake: Any, *, managed: list[Any], custom: list[Any]) -> None:
    """Stub the collection-listing endpoints for the suggestion tests.

    ``_collect_recipe_ids`` now drains every page via ``count_*_collections``;
    the listing endpoints are still called per page. ``(0, 1)`` means
    'one page exists' so a single ``get_*_collections(page=0)`` is issued.
    """
    fake.count_managed_collections = AsyncMock(return_value=(len(managed), 1 if managed else 0))
    fake.count_custom_collections = AsyncMock(return_value=(len(custom), 1 if custom else 0))
    fake.get_managed_collections = AsyncMock(return_value=managed)
    fake.get_custom_collections = AsyncMock(return_value=custom)


def _make_calendar_day() -> Any:
    return _NS(
        id="2026-05-21",
        title="Thursday",
        recipes=[
            _NS(
                id="r",
                name="n",
                total_time=10,
                url="u",
                thumbnail=None,
                image=None,
            )
        ],
        customer_recipe_ids=[],
    )


@pytest.fixture
def patched_session(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> tuple[CookidoughSession, Any]:
    session = CookidoughSession(settings)
    fake_client = AsyncMock()

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    return session, fake_client


async def test_get_user_profile(patched_session: tuple[CookidoughSession, Any]) -> None:
    session, fake = patched_session
    fake.get_user_info = AsyncMock(
        return_value=_NS(id="u-1", username="u", description="d", picture="p")
    )
    profile = await session.get_user_profile()
    assert profile.id == "u-1"
    assert profile.username == "u"


async def test_get_recipe_details_maps_categories_collections_and_nutrition(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_recipe_details = AsyncMock(
        return_value=_NS(
            id="r1",
            name="Sample",
            url="https://cookidoo.de/recipes/r1",
            thumbnail=None,
            image=None,
            difficulty="easy",
            serving_size=4,
            active_time=600,
            total_time=1800,
            utensils=[],
            notes=[],
            ingredients=[],
            categories=[_NS(id="cat1", name="Hauptgerichte", notes="")],
            collections=[_NS(id="col1", name="Wochenplan-Hits", total_recipes=12)],
            nutrition_groups=[
                _NS(
                    name="",
                    recipe_nutritions=[
                        _NS(
                            quantity=1,
                            unit_notation="Portion",
                            nutritions=[
                                _NS(number=350.0, type="kcal", unittype="kcal"),
                                _NS(number=12.5, type="protein", unittype="g"),
                            ],
                        )
                    ],
                )
            ],
        )
    )

    details = await session.get_recipe_details("r1")

    assert details.categories[0].id == "cat1"
    assert details.categories[0].notes is None
    assert details.collections[0].total_recipes == 12
    assert details.nutrition[0].quantity == 1
    assert details.nutrition[0].unit_notation == "Portion"
    assert details.nutrition[0].values[0].type == "kcal"
    assert details.nutrition[0].values[0].value == 350.0
    assert details.nutrition[0].values[1].unit == "g"


async def test_custom_recipes_url_logs_in_on_first_use(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Regression: a fresh session that goes straight into a custom-recipe
    operation (e.g. ``import_web_recipe`` → upload, with no prior session-
    touching tool call) used to fail with ``UpstreamApiError("Session is not
    logged in.")`` because ``_custom_recipes_url`` called ``_require_client``
    without first triggering login. After the fix the URL helper must
    trigger ``_ensure_logged_in`` itself."""
    session = CookidoughSession(settings)
    assert session._client is None  # baseline: fresh session

    login_calls = {"n": 0}
    fake_client = _NS(localization=_NS(url="https://cookidoo.de", language="de-DE"))

    async def _login() -> Any:
        login_calls["n"] += 1
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    url = await session._custom_recipes_url()
    assert url == "https://cookidoo.de/created-recipes/de-DE"
    assert login_calls["n"] == 1


async def test_upload_custom_recipe_times_out_cleanly_on_hanging_create(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Regression: previously a hung POST to ``/created-recipes/{lang}``
    combined with the 30 s per-request timeout plus a 401 retry could push
    a single upload past Claude Desktop's 4-minute MCP-client timeout. The
    upload now has a hard per-step ``asyncio.wait_for`` upper bound and
    surfaces ``UpstreamApiError`` once it trips."""
    from cookidough_mcp.errors import UpstreamApiError
    from cookidough_mcp.models import CustomRecipeDraft, RecipeStep

    session = CookidoughSession(settings)

    # Pretend the create step never returns. wait_for must cancel it well
    # before the test-suite default timeout.
    async def _never_returns(_name: str) -> str:
        await asyncio.sleep(3600)
        return "should never get here"

    monkeypatch.setattr(session, "_create_empty_custom_recipe", _never_returns)
    # Shrink the bound so the test stays fast.
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

    draft = CustomRecipeDraft(
        name="x",
        ingredients=["a"],
        steps=[RecipeStep(text="b")],
        servings=1,
        prep_minutes=0,
        total_minutes=0,
    )
    with pytest.raises(UpstreamApiError, match="timed out"):
        await session.upload_custom_recipe(draft)


async def test_upload_custom_recipe_times_out_and_rolls_back_on_hanging_patch(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """If the PATCH step hangs, the stub created by the POST must be rolled
    back via ``delete_custom_recipe`` before the timeout error is surfaced."""
    from cookidough_mcp.errors import UpstreamApiError
    from cookidough_mcp.models import CustomRecipeDraft, RecipeStep

    session = CookidoughSession(settings)
    rollbacks: list[str] = []

    async def _create_ok(_name: str) -> str:
        return "stub-id-42"

    async def _patch_never_returns(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    async def _delete(recipe_id: str) -> None:
        rollbacks.append(recipe_id)

    monkeypatch.setattr(session, "_create_empty_custom_recipe", _create_ok)
    monkeypatch.setattr(session, "_patch_custom_recipe", _patch_never_returns)
    monkeypatch.setattr(session, "delete_custom_recipe", _delete)
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS", 0)
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

    draft = CustomRecipeDraft(
        name="x",
        ingredients=["a"],
        steps=[RecipeStep(text="b")],
        servings=1,
        prep_minutes=0,
        total_minutes=0,
    )
    with pytest.raises(UpstreamApiError, match="rolled back"):
        await session.upload_custom_recipe(draft)
    assert rollbacks == ["stub-id-42"]


async def test_custom_recipe_public_url_logs_in_on_first_use(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session = CookidoughSession(settings)
    fake_client = _NS(localization=_NS(url="https://cookidoo.de", language="de-DE"))

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    url = await session._custom_recipe_public_url("r123")
    assert url == "https://cookidoo.de/recipes/custom-recipes/r123"


async def test_get_subscription_returns_none(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_active_subscription = AsyncMock(return_value=None)
    assert await session.get_subscription() is None


async def test_get_subscription_maps_fields(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_active_subscription = AsyncMock(
        return_value=_NS(
            active=True,
            status="A",
            subscription_level="P",
            subscription_source="STORE",
            type="T",
            extended_type="E",
            start_date="2025",
            expires="2026",
        )
    )
    sub = await session.get_subscription()
    assert sub is not None
    assert sub.subscription_level == "P"
    assert sub.subscription_source == "STORE"


async def test_get_custom_recipe_details_not_found(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_custom_recipe = AsyncMock(side_effect=CookidooRequestException("404"))
    with pytest.raises(NotFoundError):
        await session.get_custom_recipe_details("missing")


async def test_collection_methods(patched_session: tuple[CookidoughSession, Any]) -> None:
    session, fake = patched_session
    fake.get_managed_collections = AsyncMock(return_value=[_make_collection()])
    fake.count_managed_collections = AsyncMock(return_value=(7, 2))
    fake.add_managed_collection = AsyncMock(return_value=_make_collection())
    fake.remove_managed_collection = AsyncMock(return_value=None)
    fake.get_custom_collections = AsyncMock(return_value=[_make_collection()])
    fake.count_custom_collections = AsyncMock(return_value=(3, 1))
    fake.add_custom_collection = AsyncMock(return_value=_make_collection())
    fake.remove_custom_collection = AsyncMock(return_value=None)
    fake.add_recipes_to_custom_collection = AsyncMock(return_value=_make_collection())
    fake.remove_recipe_from_custom_collection = AsyncMock(return_value=None)

    managed = await session.list_managed_collections()
    assert managed.items[0].id == "c"
    assert managed.page == 0
    assert managed.total_pages == 2
    assert managed.total_elements == 7
    assert (await session.add_managed_collection("c")).id == "c"
    await session.remove_managed_collection("c")
    custom = await session.list_custom_collections(page=1)
    assert custom.items[0].id == "c"
    assert custom.page == 1
    assert custom.total_pages == 1
    assert custom.total_elements == 3
    fake.get_custom_collections.assert_awaited_once_with(page=1)
    assert (await session.create_custom_collection("name")).id == "c"
    await session.delete_custom_collection("c")
    assert (await session.add_recipes_to_custom_collection("c", ["r1"])).id == "c"
    await session.remove_recipe_from_custom_collection("c", "r1")


async def test_shopping_list_methods(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_ingredient_items = AsyncMock(
        return_value=[_NS(id="i", name="n", description="d", is_owned=False)]
    )
    fake.get_additional_items = AsyncMock(return_value=[_NS(id="a", name="n2", is_owned=True)])
    fake.get_shopping_list_recipes = AsyncMock(
        return_value=[
            _NS(
                id="r1",
                name="Pasta",
                url="https://cookidoo.de/recipes/r1",
                thumbnail=None,
                image=None,
                ingredients=[_NS(id="i", name="Tomato", description="2 pcs")],
            )
        ]
    )
    fake.add_ingredient_items_for_recipes = AsyncMock(return_value=[1, 2, 3])
    fake.remove_ingredient_items_for_recipes = AsyncMock(return_value=None)
    fake.add_additional_items = AsyncMock(return_value=[_NS(id="x", name="n", is_owned=False)])
    fake.remove_additional_items = AsyncMock(return_value=None)
    fake.clear_shopping_list = AsyncMock(return_value=None)

    shopping = await session.get_shopping_list()
    assert shopping.ingredient_items[0].source == "recipe"
    assert shopping.additional_items[0].source == "additional"
    assert shopping.recipes[0].id == "r1"
    assert shopping.recipes[0].ingredients[0].name == "Tomato"
    assert await session.add_recipes_to_shopping_list(["r"]) == 3
    await session.remove_recipes_from_shopping_list(["r"])
    items = await session.add_additional_items(["Salt"])
    assert items[0].name == "n"
    await session.remove_additional_items(["x"])
    await session.clear_shopping_list()


async def test_calendar_methods(patched_session: tuple[CookidoughSession, Any]) -> None:
    session, fake = patched_session
    fake.get_recipes_in_calendar_week = AsyncMock(return_value=[_make_calendar_day()])
    fake.add_recipes_to_calendar = AsyncMock(return_value=_make_calendar_day())
    fake.remove_recipe_from_calendar = AsyncMock(return_value=_make_calendar_day())

    assert (await session.get_calendar_week(date(2026, 5, 21)))[0].id == "2026-05-21"
    assert (await session.add_recipes_to_calendar(date(2026, 5, 21), ["r"])).id == "2026-05-21"
    assert (await session.remove_recipe_from_calendar(date(2026, 5, 21), "r")).id == "2026-05-21"


async def test_delete_custom_recipe_delegates_to_client(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_custom_recipe = AsyncMock(return_value=None)
    await session.delete_custom_recipe("cr1")
    fake.remove_custom_recipe.assert_awaited_once_with("cr1")


async def test_upload_custom_recipe_rolls_back_on_patch_failure(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_custom_recipe = AsyncMock(return_value=None)
    monkeypatch.setattr(session, "_create_empty_custom_recipe", AsyncMock(return_value="rid"))

    async def _bad_patch(*_: Any, **__: Any) -> None:
        raise RuntimeError("patch boom")

    monkeypatch.setattr(session, "_patch_custom_recipe", _bad_patch)
    monkeypatch.setattr("cookidough_mcp.session.asyncio.sleep", AsyncMock())

    draft = CustomRecipeDraft(
        name="N",
        ingredients=["A"],
        steps=[RecipeStep(text="Mix everything 5 min / speed 4 with the spatula.")],
    )
    with pytest.raises(RuntimeError, match="patch boom"):
        await session.upload_custom_recipe(draft)
    fake.remove_custom_recipe.assert_awaited_once_with("rid")


async def test_update_custom_recipe_patches_and_returns_public_url(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    del fake
    patched: list[tuple[str, Any]] = []

    async def _patch(recipe_id: str, draft: Any) -> None:
        patched.append((recipe_id, draft))

    monkeypatch.setattr(session, "_patch_custom_recipe", _patch)
    monkeypatch.setattr(
        session,
        "_custom_recipe_public_url",
        AsyncMock(return_value="https://cookidoo.de/recipes/custom-recipes/cr1"),
    )

    draft = CustomRecipeDraft(
        name="N",
        ingredients=["A"],
        steps=[RecipeStep(text="Mix everything 5 min / speed 4 with the spatula.")],
    )
    recipe_id, url = await session.update_custom_recipe("cr1", draft)

    assert recipe_id == "cr1"
    assert url.endswith("/cr1")
    assert patched == [("cr1", draft)]


async def test_update_custom_recipe_times_out_without_rollback(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """A hanging PATCH must surface as UpstreamApiError. Unlike the create
    flow there is no stub to clean up, so no delete may be issued."""
    session = CookidoughSession(settings)
    deletes: list[str] = []

    async def _hanging_patch(*_: Any, **__: Any) -> None:
        await asyncio.sleep(3600)

    async def _record_delete(recipe_id: str) -> None:
        deletes.append(recipe_id)

    monkeypatch.setattr(session, "_patch_custom_recipe", _hanging_patch)
    monkeypatch.setattr(session, "delete_custom_recipe", _record_delete)
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

    draft = CustomRecipeDraft(
        name="N", ingredients=["A"], steps=[RecipeStep(text="Mix 5 min / speed 4.")]
    )
    with pytest.raises(UpstreamApiError, match="timed out"):
        await session.update_custom_recipe("cr1", draft)
    assert deletes == []


async def test_upload_custom_recipe_rollback_is_itself_bounded_on_hang(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Regression: a PATCH failure used to be followed by an unbounded
    rollback ``delete_custom_recipe`` call. Under cancellation that
    rollback could itself stall, defeating the upper-bound ``wait_for``
    around the PATCH. The rollback now has its own hard deadline (half
    the operation budget) and surfaces the original error rather than
    the rollback's hang."""
    session = CookidoughSession(settings)
    monkeypatch.setattr(session, "_create_empty_custom_recipe", AsyncMock(return_value="rid"))

    async def _bad_patch(*_: Any, **__: Any) -> None:
        raise RuntimeError("patch boom")

    async def _hanging_delete(_recipe_id: str) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(session, "_patch_custom_recipe", _bad_patch)
    monkeypatch.setattr(session, "delete_custom_recipe", _hanging_delete)
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS", 0)
    monkeypatch.setattr("cookidough_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

    draft = CustomRecipeDraft(
        name="N", ingredients=["A"], steps=[RecipeStep(text="Mix 5 min / speed 4.")]
    )
    # The original error must propagate; the hanging rollback must NOT
    # hold the call hostage past the per-step budget.
    with pytest.raises(RuntimeError, match="patch boom"):
        await session.upload_custom_recipe(draft)


async def test_add_custom_recipes_to_calendar(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.add_custom_recipes_to_calendar = AsyncMock(return_value=_make_calendar_day())
    result = await session.add_custom_recipes_to_calendar(date(2026, 5, 21), ["cr1"])
    assert result.id == "2026-05-21"
    fake.add_custom_recipes_to_calendar.assert_awaited_once_with(date(2026, 5, 21), ["cr1"])


async def test_remove_custom_recipe_from_calendar(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_custom_recipe_from_calendar = AsyncMock(return_value=_make_calendar_day())
    result = await session.remove_custom_recipe_from_calendar(date(2026, 5, 21), "cr1")
    assert result.id == "2026-05-21"


async def test_add_custom_recipes_to_shopping_list_counts_items(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.add_ingredient_items_for_custom_recipes = AsyncMock(return_value=[1, 2, 3, 4])
    assert await session.add_custom_recipes_to_shopping_list(["cr1"]) == 4


async def test_remove_custom_recipes_from_shopping_list_delegates(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_ingredient_items_for_custom_recipes = AsyncMock(return_value=None)
    await session.remove_custom_recipes_from_shopping_list(["cr1", "cr2"])
    fake.remove_ingredient_items_for_custom_recipes.assert_awaited_once_with(["cr1", "cr2"])


async def test_set_ingredient_items_ownership_maps_response(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.edit_ingredient_items_ownership = AsyncMock(
        return_value=[_NS(id="i1", name="Tomato", description="d", is_owned=True)]
    )
    items = await session.set_ingredient_items_ownership(
        [ShoppingItemOwnershipUpdate(id="i1", is_owned=True)]
    )
    assert items[0].is_owned is True
    assert items[0].source == "recipe"


async def test_set_additional_items_ownership_maps_response(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.edit_additional_items_ownership = AsyncMock(
        return_value=[_NS(id="a1", name="Sea salt", is_owned=False)]
    )
    items = await session.set_additional_items_ownership(
        [ShoppingItemOwnershipUpdate(id="a1", is_owned=False)]
    )
    assert items[0].source == "additional"


async def test_rename_additional_items_maps_response(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.edit_additional_items = AsyncMock(
        return_value=[_NS(id="a1", name="Sea salt", is_owned=False)]
    )
    items = await session.rename_additional_items([AdditionalItemRename(id="a1", name="Sea salt")])
    assert items[0].name == "Sea salt"


async def test_clone_recipe_as_custom_maps_response(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    fake.add_custom_recipe_from = AsyncMock(
        return_value=_NS(
            id="new",
            name="Cloned",
            url="https://cookidoo.de/recipes/custom-recipes/new",
            serving_size=4,
            active_time=600,
            total_time=1800,
            tools=["TM7"],
            ingredients=["i"],
            instructions=["s"],
            thumbnail=None,
            image=None,
        )
    )
    result = await session.clone_recipe_as_custom("r1", 4)
    assert result.id == "new"
    fake.add_custom_recipe_from.assert_awaited_once_with("r1", 4)


async def test_clone_recipe_as_custom_propagates_upstream_errors(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """A Cookidoo write failure (validation, non-cloneable recipe, transient
    5xx) must surface as ``UpstreamApiError`` with the original upstream
    message — not be remapped to ``NotFoundError``. Mapping it to 404 would
    tell the LLM the source recipe does not exist, even when it does."""
    session, fake = patched_session
    fake.add_custom_recipe_from = AsyncMock(
        side_effect=CookidooRequestException("Add custom recipe failed due to request exception.")
    )
    with pytest.raises(UpstreamApiError, match="request exception"):
        await session.clone_recipe_as_custom("missing", 4)


async def test_suggest_recipes_from_ingredients_collects_and_scores(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """Without collection_ids and with an empty search result the tool must
    fall back to walking the user's collections (old behaviour)."""
    session, fake = patched_session
    chapter = _NS(name="ch", recipes=[_NS(id="r1"), _NS(id="r2")])
    collection = _NS(id="c1", chapters=[chapter])
    _mock_collections(fake, managed=[], custom=[collection])
    session.search_recipes = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _details(rid: str) -> Any:
        from cookidough_mcp.models import Ingredient, RecipeDetails

        if rid == "r1":
            return RecipeDetails(
                id="r1",
                name="Rice bowl",
                url="https://cookidoo.de/recipes/r1",
                ingredients=[Ingredient(id="i1", name="Rice"), Ingredient(id="i2", name="Tomato")],
            )
        return RecipeDetails(
            id="r2",
            name="Cabbage soup",
            url="https://cookidoo.de/recipes/r2",
            ingredients=[Ingredient(id="i3", name="Cabbage")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]

    suggestions = await session.suggest_recipes_from_ingredients(["rice"])
    assert len(suggestions) == 1
    assert suggestions[0].recipe.id == "r1"
    assert suggestions[0].matching_ingredients == ["rice"]
    assert suggestions[0].missing_ingredients == ["tomato"]


async def test_suggest_recipes_returns_empty_when_no_ingredients(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, _ = patched_session
    assert await session.suggest_recipes_from_ingredients([]) == []


async def test_suggest_recipes_uses_search_candidates_library_wide(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """Without collection_ids the candidates come from the server-side
    ingredient search; the collections must not be walked at all."""
    from cookidough_mcp.models import Ingredient, RecipeDetails, RecipeSearchResult

    session, fake = patched_session
    fake.count_managed_collections = AsyncMock(
        side_effect=AssertionError("collections must not be walked")
    )
    fake.count_custom_collections = AsyncMock(
        side_effect=AssertionError("collections must not be walked")
    )
    search_mock = AsyncMock(
        return_value=[
            RecipeSearchResult(id="s1", name="Rice bowl"),
            RecipeSearchResult(id="s2", name="Cabbage soup"),
        ]
    )
    session.search_recipes = search_mock  # type: ignore[method-assign]

    async def _details(rid: str) -> Any:
        if rid == "s1":
            return RecipeDetails(
                id="s1",
                name="Rice bowl",
                url="https://cookidoo.de/recipes/s1",
                ingredients=[Ingredient(id="i1", name="Rice")],
            )
        return RecipeDetails(
            id="s2",
            name="Cabbage soup",
            url="https://cookidoo.de/recipes/s2",
            ingredients=[Ingredient(id="i3", name="Cabbage")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]

    suggestions = await session.suggest_recipes_from_ingredients(["rice"], max_results=5)

    assert [s.recipe.id for s in suggestions] == ["s1"]
    search_mock.assert_awaited_once_with(query="rice", limit=15, ingredients=["rice"])


async def test_suggest_recipes_filters_by_collection_ids(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    collection_a = _NS(id="a", chapters=[_NS(name="x", recipes=[_NS(id="r-a")])])
    collection_b = _NS(id="b", chapters=[_NS(name="x", recipes=[_NS(id="r-b")])])
    _mock_collections(fake, managed=[collection_a], custom=[collection_b])

    seen: list[str] = []

    async def _details(rid: str) -> Any:
        from cookidough_mcp.models import Ingredient, RecipeDetails

        seen.append(rid)
        return RecipeDetails(
            id=rid,
            name=rid,
            url=f"https://cookidoo.de/recipes/{rid}",
            ingredients=[Ingredient(id="i", name="Rice")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    await session.suggest_recipes_from_ingredients(["rice"], collection_ids=["b"])
    assert seen == ["r-b"]


async def test_search_recipes_calls_upstream_and_parses(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """``search_recipes`` builds the right URL and parses the upstream payload."""
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(
            url="https://cookidoo.de",
            language="de-DE",
            country_code="de",
        )
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        captured["method"] = method
        captured["url"] = url
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {
            "data": [
                {
                    "id": "rid1",
                    "title": "Tomatensuppe",
                    "rating": 4.7,
                    "numberOfRatings": 42,
                    "totalTime": "PT30M",
                    "image": "https://x/{transformation}/img.jpg",
                },
                {"id": "rid2", "title": "Bad rating", "rating": None},
                "not-a-dict",
            ]
        }

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)

    results = await session.search_recipes("tomate", limit=5)

    assert "search/de-DE" in captured["url"]
    assert "query=tomate" in captured["url"]
    assert "countries=de" in captured["url"]
    assert "limit=5" in captured["url"]
    assert results[0].id == "rid1"
    assert results[0].rating == 4.7
    assert results[0].total_time_seconds == 30 * 60
    assert results[0].image is not None
    assert "{transformation}" not in results[0].image
    # rid2 has a title but rating=None — kept, with rating preserved as None.
    assert len(results) == 2
    assert results[1].rating is None


async def test_search_recipes_appends_only_set_filters(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Set filters land in the query string; unset ones must stay out so the
    default call is byte-identical to the pre-filter behaviour."""
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        captured["url"] = url
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {"data": []}

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)

    await session.search_recipes(
        "pasta",
        limit=5,
        max_total_minutes=30,
        difficulty="easy",
        ingredients=["tomate", "basilikum"],
        exclude_ingredients=["sahne"],
        min_rating=4.0,
        thermomix_version="TM6",
        sort_by="rating",
    )

    url = captured["url"]
    assert "totalTime=1800" in url
    assert "difficulty=easy" in url
    assert "ingredients=tomate%2Cbasilikum" in url
    assert "excludeIngredients=sahne" in url
    assert "ratings=4" in url
    assert "tmv=TM6" in url
    assert "sortby=rating" in url
    assert "categories" not in url
    assert "portions" not in url
    assert "accessories" not in url

    captured.clear()
    await session.search_recipes("pasta", limit=5)
    assert "totalTime" not in captured["url"]
    assert "difficulty" not in captured["url"]


async def test_add_calendar_range_to_shopping_list_dedupes_and_filters(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    from cookidough_mcp.models import CalendarDay, CalendarRecipe

    session, fake = patched_session
    del fake

    def _day(day_id: str, recipe_ids: list[str], custom_ids: list[str]) -> CalendarDay:
        return CalendarDay(
            id=day_id,
            title=day_id,
            recipes=[
                CalendarRecipe(id=rid, name=rid, url=f"https://cookidoo.de/recipes/{rid}")
                for rid in recipe_ids
            ],
            custom_recipe_ids=custom_ids,
        )

    week = [
        _day("2026-06-01", ["r1"], ["c1"]),
        _day("2026-06-02", ["r1", "r2"], []),  # r1 repeats → dedupe
        _day("2026-06-04", ["r3"], ["c2"]),  # outside the range → dropped
        _day("not-a-date", ["r9"], []),  # unparseable id → skipped (write op!)
    ]
    week_calls: list[date] = []

    async def _get_week(day: date) -> list[CalendarDay]:
        week_calls.append(day)
        return week

    monkeypatch.setattr(session, "get_calendar_week", _get_week)
    add_recipes = AsyncMock(return_value=5)
    add_custom = AsyncMock(return_value=2)
    monkeypatch.setattr(session, "add_recipes_to_shopping_list", add_recipes)
    monkeypatch.setattr(session, "add_custom_recipes_to_shopping_list", add_custom)

    summary = await session.add_calendar_range_to_shopping_list(date(2026, 6, 1), date(2026, 6, 2))

    assert summary.recipe_ids == ["r1", "r2"]
    assert summary.custom_recipe_ids == ["c1"]
    assert summary.item_count == 7
    add_recipes.assert_awaited_once_with(["r1", "r2"])
    add_custom.assert_awaited_once_with(["c1"])
    # Start plus the end date (whose week the 7-day stride overshoots).
    assert week_calls == [date(2026, 6, 1), date(2026, 6, 2)]


async def test_add_calendar_range_skips_writes_when_nothing_planned(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    del fake

    async def _get_week(_day: date) -> list[Any]:
        return []

    monkeypatch.setattr(session, "get_calendar_week", _get_week)
    add_recipes = AsyncMock()
    add_custom = AsyncMock()
    monkeypatch.setattr(session, "add_recipes_to_shopping_list", add_recipes)
    monkeypatch.setattr(session, "add_custom_recipes_to_shopping_list", add_custom)

    summary = await session.add_calendar_range_to_shopping_list(date(2026, 6, 1), date(2026, 6, 14))

    assert summary.item_count == 0
    add_recipes.assert_not_awaited()
    add_custom.assert_not_awaited()


async def test_search_recipes_drops_rows_without_title(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {
            "data": [
                {"id": "rid1"},  # title missing
                {"id": "rid2", "title": ""},  # title empty
                {"id": "rid3", "title": "Good", "numberOfRatings": 42.0},
            ]
        }

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)
    results = await session.search_recipes("x")
    assert [r.id for r in results] == ["rid3"]
    # numberOfRatings sent as a float must still be accepted (some JSON
    # producers serialise integer counts as 42.0).
    assert results[0].number_of_ratings == 42


async def test_search_recipes_url_encodes_query_plus_sign(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Without quote_plus, a literal '+' in the query is forwarded as '+'
    which Cookidoo's search decodes as a space — a silent UX bug."""
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        captured["url"] = url
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {"data": []}

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)
    await session.search_recipes("A+B Sauce")
    # quote_plus encodes '+' as %2B and space as '+'.
    assert "query=A%2BB+Sauce" in captured["url"]


async def test_search_recipes_returns_empty_on_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {"meta": {}}  # missing "data"

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)
    assert await session.search_recipes("x") == []


async def test_search_recipes_clamps_limit(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        captured["url"] = url
        yield _NS()

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        return {"data": []}

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)
    await session.search_recipes("x", limit=9999)
    assert "limit=50" in captured["url"]

    # And the lower bound:
    captured.clear()
    await session.search_recipes("x", limit=0)
    assert "limit=1" in captured["url"]


async def test_suggest_recipes_skips_recipes_with_no_match(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    collection = _NS(id="c", chapters=[_NS(name="x", recipes=[_NS(id="r1")])])
    _mock_collections(fake, managed=[], custom=[collection])
    session.search_recipes = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _details(_rid: str) -> Any:
        from cookidough_mcp.models import Ingredient, RecipeDetails

        return RecipeDetails(
            id="r1",
            name="Nothing in common",
            url="https://cookidoo.de/recipes/r1",
            ingredients=[Ingredient(id="i", name="Pufferfish")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    assert await session.suggest_recipes_from_ingredients(["rice"]) == []


async def test_suggest_recipes_tolerates_individual_recipe_errors(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    session, fake = patched_session
    collection = _NS(id="c", chapters=[_NS(name="x", recipes=[_NS(id="ok"), _NS(id="boom")])])
    _mock_collections(fake, managed=[], custom=[collection])
    session.search_recipes = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _details(rid: str) -> Any:
        from cookidough_mcp.models import Ingredient, RecipeDetails

        if rid == "boom":
            # NotFoundError is expected for ID-look-up misses (e.g. a custom
            # recipe id smuggled into a chapter under a managed-collection
            # endpoint). It gets swallowed; everything else propagates.
            raise NotFoundError("nope")
        return RecipeDetails(
            id=rid,
            name="Good",
            url=f"https://cookidoo.de/recipes/{rid}",
            ingredients=[Ingredient(id="i", name="Rice")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    suggestions = await session.suggest_recipes_from_ingredients(["rice"])
    assert [s.recipe.id for s in suggestions] == ["ok"]


async def test_suggest_recipes_propagates_unexpected_upstream_errors(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """A non-NotFound failure (e.g. session closed mid-flight) must propagate.

    The previous implementation caught both NotFoundError and UpstreamApiError,
    which silently absorbed the 'Session is closed.' signal that
    ``_ensure_logged_in`` raises after ``aclose``. Now only NotFoundError
    is swallowed.
    """
    session, fake = patched_session
    collection = _NS(id="c", chapters=[_NS(name="x", recipes=[_NS(id="boom")])])
    _mock_collections(fake, managed=[], custom=[collection])
    session.search_recipes = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _details(_rid: str) -> Any:
        raise UpstreamApiError("Session is closed.")

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    with pytest.raises(UpstreamApiError, match="closed"):
        await session.suggest_recipes_from_ingredients(["rice"])


async def test_suggest_recipes_drops_short_ingredient_tokens(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """Single/double-letter tokens are ignored — they were producing spurious
    matches via the bidirectional substring matcher (e.g. 'oil' → 'soil')."""
    session, fake = patched_session
    # No collections are even queried because the available_ingredients set
    # is empty after short tokens are filtered out.
    fake.count_managed_collections = AsyncMock()
    fake.count_custom_collections = AsyncMock()
    assert await session.suggest_recipes_from_ingredients(["a", "oi"]) == []
    fake.count_managed_collections.assert_not_called()


async def test_suggest_recipes_drains_all_collection_pages(
    patched_session: tuple[CookidoughSession, Any],
) -> None:
    """A user with multiple pages of collections must have every page
    walked, not just page 0."""
    session, fake = patched_session
    page0 = _NS(id="c0", chapters=[_NS(name="x", recipes=[_NS(id="r0")])])
    page1 = _NS(id="c1", chapters=[_NS(name="x", recipes=[_NS(id="r1")])])

    fake.count_managed_collections = AsyncMock(return_value=(2, 2))
    fake.count_custom_collections = AsyncMock(return_value=(0, 0))
    session.search_recipes = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def _get_managed(page: int = 0) -> list[Any]:
        return [page0] if page == 0 else [page1]

    fake.get_managed_collections = _get_managed

    seen: list[str] = []

    async def _details(rid: str) -> Any:
        from cookidough_mcp.models import Ingredient, RecipeDetails

        seen.append(rid)
        return RecipeDetails(
            id=rid,
            name=rid,
            url=f"https://cookidoo.de/recipes/{rid}",
            ingredients=[Ingredient(id="i", name="Rice")],
        )

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    await session.suggest_recipes_from_ingredients(["rice"])
    assert sorted(seen) == ["r0", "r1"]


def _interaction_session(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> tuple[CookidoughSession, list[tuple[str, str, Any]], dict[str, Any]]:
    """Session whose ``_authed_http`` records calls and serves canned JSON.

    ``responses`` maps a URL substring to either a payload or an exception.
    """
    from contextlib import asynccontextmanager

    session = CookidoughSession(settings)
    fake_client = _NS(
        localization=_NS(url="https://cookidoo.de", language="de-DE", country_code="de")
    )

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    calls: list[tuple[str, str, Any]] = []
    responses: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_authed_http(method: str, url: str, json_body: Any = None) -> Any:
        calls.append((method, url, json_body))
        for marker, outcome in responses.items():
            if marker in url and isinstance(outcome, Exception):
                raise outcome

        async def _read() -> bytes:
            return b""

        yield _NS(read=_read)

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)

    async def _fake_parse_json(_response: Any) -> Any:
        method, url, _ = calls[-1]
        del method
        for marker, outcome in responses.items():
            if marker in url and not isinstance(outcome, Exception):
                return outcome
        return None

    monkeypatch.setattr("cookidough_mcp.session._parse_json", _fake_parse_json)
    return session, calls, responses


async def test_rate_recipe_puts_clamped_stars(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, _ = _interaction_session(monkeypatch, settings)
    await session.rate_recipe("r1", 9)
    method, url, body = calls[0]
    assert method == "PUT"
    assert url == "https://cookidoo.de/rating/de-DE/user-ratings/recipes/r1"
    assert body == {"rating": 5}


async def test_set_recipe_bookmark_uses_put_and_delete(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, _ = _interaction_session(monkeypatch, settings)
    await session.set_recipe_bookmark("r1", True)
    await session.set_recipe_bookmark("r1", False)
    assert [(m, b) for m, _, b in calls] == [
        ("PUT", {"recipeId": "r1"}),
        ("DELETE", {"recipeId": "r1"}),
    ]
    assert all(u == "https://cookidoo.de/organize/de-DE/api/bookmark" for _, u, _ in calls)


async def test_set_recipe_note_put_falls_back_to_post_on_404(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["recipe-notes/de-DE/recipes/r1"] = NotFoundError("no note yet")
    await session.set_recipe_note("r1", "Mehr Salz.")
    assert [(m, u) for m, u, _ in calls] == [
        ("PUT", "https://cookidoo.de/recipe-notes/de-DE/recipes/r1"),
        ("POST", "https://cookidoo.de/recipe-notes/de-DE/recipes"),
    ]
    assert calls[1][2] == {"recipeId": "r1", "text": "Mehr Salz."}


async def test_set_recipe_note_empty_text_deletes_idempotently(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["recipe-notes"] = NotFoundError("nothing to delete")
    await session.set_recipe_note("r1", "")  # must not raise
    assert calls[0][0] == "DELETE"


async def test_mark_recipe_cooked_posts_to_cooking_history(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, _ = _interaction_session(monkeypatch, settings)
    await session.mark_recipe_cooked("r1")
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://cookidoo.de/organize/de-DE/api/cooking-history")
    assert body == {"recipeId": "r1", "recipeType": "VorwerkRecipe"}


async def test_get_recipe_interactions_gathers_and_tolerates_failures(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["user-ratings"] = {"rating": 4}
    responses["aggregated-ratings"] = {"aggregatedRating": 4.6, "numberOfRatings": 128}
    responses["recipe-notes"] = NotFoundError("no note")

    interactions = await session.get_recipe_interactions("r1")

    assert interactions.recipe_id == "r1"
    assert interactions.own_rating == 4
    assert interactions.average_rating == 4.6
    assert interactions.number_of_ratings == 128
    assert interactions.note is None


async def test_get_recipe_interactions_parses_alternate_shapes(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["user-ratings"] = {"rating": {"value": 3}}
    responses["aggregated-ratings"] = {"averageRating": 3.9, "numberOfRatings": 7}
    responses["recipe-notes"] = {"notes": [{"text": "Weniger Zucker."}]}

    interactions = await session.get_recipe_interactions("r1")

    assert interactions.own_rating == 3
    assert interactions.average_rating == 3.9
    assert interactions.number_of_ratings == 7
    assert interactions.note == "Weniger Zucker."


async def test_get_recipe_recommendations_foryou_and_simrec_urls(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["recommender"] = {
        "stripes": [
            {
                "stripeTopic": "trending",
                "recipes": [
                    {
                        "id": "rec1",
                        "title": "Vorschlag",
                        "averageRating": 4.1,
                        "numRating": 108,
                        "totalTime": 9000,
                        "descriptiveAssets": [{"square": "https://x/{transformation}/i.jpg"}],
                    },
                    {"id": "rec2", "name": "Anders geformt"},
                ],
            }
        ]
    }

    foryou = await session.get_recipe_recommendations()
    similar = await session.get_recipe_recommendations("r9", limit=1)

    assert calls[0][1] == "https://cookidoo.de/recommender/web/de-DE/foryou"
    assert calls[1][1] == "https://cookidoo.de/recommender/mobile/simrec/r9"
    assert [r.id for r in foryou] == ["rec1", "rec2"]
    assert foryou[0].rating == 4.1
    assert foryou[0].number_of_ratings == 108
    assert foryou[0].total_time_seconds == 9000
    assert foryou[0].image is not None
    assert "{transformation}" not in foryou[0].image
    assert [r.id for r in similar] == ["rec1"]  # limit applied client-side


async def test_get_recipe_recommendations_surfaces_upstream_failure(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["recommender"] = UpstreamApiError("gateway said no")
    with pytest.raises(UpstreamApiError, match="gateway said no"):
        await session.get_recipe_recommendations()


async def test_list_bookmarked_recipes_parses_bookmark_payload(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["api/bookmark"] = {
        "bookmarks": [
            {
                "created": "2026-06-04T19:07:56.524Z",
                "id": "FAV-abc",
                "recipe": {
                    "id": "r758688",
                    "title": "Quinoasalat",
                    "totalTime": 1800,
                    "squareImage": "https://x/{transformation}/img.jpg",
                },
            }
        ]
    }
    bookmarks = await session.list_bookmarked_recipes()
    assert calls[0][:2] == ("GET", "https://cookidoo.de/organize/de-DE/api/bookmark")
    assert [b.id for b in bookmarks] == ["r758688"]
    assert bookmarks[0].total_time_seconds == 1800
    assert bookmarks[0].image is not None
    assert "{transformation}" not in bookmarks[0].image


async def test_list_bookmarked_recipes_surfaces_upstream_failure(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["api/bookmark"] = UpstreamApiError("Cookidoo returned non-JSON payload")
    with pytest.raises(UpstreamApiError, match="non-JSON"):
        await session.list_bookmarked_recipes()


async def test_get_user_devices_parses_mixed_shapes(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["my-devices/versions"] = {"data": [{"name": "TM6"}, {"version": "TM5"}]}
    responses["accessory/ids"] = ["varoma", "cutter"]

    devices, accessories = await session.get_user_devices()

    assert devices == ["TM6", "TM5"]
    assert accessories == ["varoma", "cutter"]
    urls = sorted(u for _, u, _ in calls)
    assert urls == [
        "https://cookidoo.de/customer-devices/api/accessory/ids",
        "https://cookidoo.de/customer-devices/api/my-devices/versions",
    ]


async def test_get_user_devices_degrades_to_empty_on_failure(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["customer-devices"] = UpstreamApiError("not available")
    assert await session.get_user_devices() == ([], [])


def _make_png(width: int = 100, height: int = 100) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        raw = tag + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\xc8\x78\x28" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


async def test_set_custom_recipe_image_signs_uploads_and_patches(
    monkeypatch: pytest.MonkeyPatch, settings: Any, tmp_path: Any
) -> None:
    from cookidough_mcp.models import CustomRecipeDetails

    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["image/signature"] = {"signature": "sig123"}

    uploaded: list[tuple[bytes, str, int, str]] = []

    async def _fake_upload(
        image_bytes: bytes, content_type: str, timestamp: int, signature: str
    ) -> tuple[str, str]:
        uploaded.append((image_bytes, content_type, timestamp, signature))
        return "prod/img/customer-recipe/abc123", "jpg"

    monkeypatch.setattr("cookidough_mcp.session._upload_image_to_cloudinary", _fake_upload)
    monkeypatch.setattr(
        session,
        "get_custom_recipe_details",
        AsyncMock(
            return_value=CustomRecipeDetails(
                id="cr1",
                name="Smoothie",
                url="https://cookidoo.de/recipes/custom-recipes/cr1",
                image="https://ugc.assets.tmecosys.com/image/upload/t_x/abc123.jpg",
                thumbnail="https://ugc.assets.tmecosys.com/image/upload/t_y/abc123.jpg",
            )
        ),
    )

    png_path = tmp_path / "photo.png"
    png_path.write_bytes(_make_png())

    result = await session.set_custom_recipe_image("cr1", str(png_path))

    sig_method, sig_url, sig_body = calls[0]
    assert (sig_method, sig_url) == (
        "POST",
        "https://cookidoo.de/created-recipes/de-DE/image/signature",
    )
    assert sig_body["upload_preset"] == "prod-customer-recipe-signed"
    assert sig_body["source"] == "uw"
    assert isinstance(sig_body["timestamp"], int)

    assert uploaded[0][1] == "image/png"
    assert uploaded[0][3] == "sig123"

    patch_method, patch_url, patch_body = calls[1]
    assert (patch_method, patch_url) == (
        "PATCH",
        "https://cookidoo.de/created-recipes/de-DE/cr1",
    )
    assert patch_body == {
        "image": "prod/img/customer-recipe/abc123.jpg",
        "isImageOwnedByUser": True,
    }
    assert result.recipe_id == "cr1"
    assert result.image is not None


async def test_load_image_bytes_validates_input(
    monkeypatch: pytest.MonkeyPatch, settings: Any, tmp_path: Any
) -> None:
    session = CookidoughSession(settings)

    png_path = tmp_path / "ok.png"
    png_path.write_bytes(_make_png())
    image_bytes, content_type = await session._load_image_bytes(str(png_path))
    assert content_type == "image/png"
    assert image_bytes.startswith(b"\x89PNG")

    text_path = tmp_path / "not-an-image.png"
    text_path.write_text("hello")
    with pytest.raises(ValueError, match="JPEG or PNG"):
        await session._load_image_bytes(str(text_path))

    with pytest.raises(ValueError, match="not found"):
        await session._load_image_bytes(str(tmp_path / "missing.jpg"))

    with pytest.raises(ValueError, match="scheme"):
        await session._load_image_bytes("ftp://example.com/a.jpg")

    monkeypatch.setattr("cookidough_mcp.session.MAX_RECIPE_IMAGE_BYTES", 10)
    with pytest.raises(ValueError, match="exceeds"):
        await session._load_image_bytes(str(png_path))


def test_sniff_image_type_detects_jpeg_png_and_rejects_rest() -> None:
    from cookidough_mcp.session import _sniff_image_type

    assert _sniff_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert _sniff_image_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert _sniff_image_type(b"GIF89a") is None


async def test_get_recipe_images_parses_descriptive_assets(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["recipes/recipe"] = {
        "id": "r1",
        "descriptiveAssets": [
            {
                "type": "image",
                "square": "https://assets.tmecosys.com/image/upload/{transformation}/img/a",
                "portrait": "https://assets.tmecosys.com/image/upload/{transformation}/img/a-p",
                "landscape": None,
            },
            {"type": "video", "square": "https://assets.tmecosys.com/video/v"},
            {
                "square": "https://assets.tmecosys.com/image/upload/{transformation}/img/b",
            },
            "not-a-dict",
        ],
    }

    images = await session.get_recipe_images("r1")

    assert calls[0][:2] == ("GET", "https://cookidoo.de/recipes/recipe/de-DE/r1")
    assert len(images) == 2  # video and junk entries skipped
    assert images[0].square is not None
    assert "{transformation}" not in images[0].square
    assert "t_web_rdp_recipe_584x480_1_5x" in images[0].square
    assert images[0].portrait is not None
    assert images[0].landscape is None
    assert images[1].square is not None
    assert images[1].square.endswith("/img/b")


async def test_get_recipe_images_returns_empty_on_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, _, responses = _interaction_session(monkeypatch, settings)
    responses["recipes/recipe"] = {"id": "r1"}
    assert await session.get_recipe_images("r1") == []


async def test_mark_recipe_cooked_supports_custom_recipes(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, _ = _interaction_session(monkeypatch, settings)
    await session.mark_recipe_cooked("cr1", is_custom=True)
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://cookidoo.de/organize/de-DE/api/cooking-history")
    assert body == {"recipeId": "cr1", "recipeType": "CreatedRecipe"}


async def test_mark_recipe_cooked_sends_timestamp_in_utc(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, _ = _interaction_session(monkeypatch, settings)
    cooked_at = datetime(2026, 9, 21, 13, 0, tzinfo=timezone(timedelta(hours=-3)))
    await session.mark_recipe_cooked("r1", cooked_at=cooked_at)
    _, _, body = calls[0]
    assert body == {
        "recipeId": "r1",
        "recipeType": "VorwerkRecipe",
        "timestamp": "2026-09-21T16:00:00Z",
    }


async def test_get_cooking_history_parses_entries(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    session, calls, responses = _interaction_session(monkeypatch, settings)
    responses["cooking-history"] = {
        "userId": "u-1",
        "entries": [
            {
                "details": {"timestamp": "2026-06-04T13:42:55.188Z"},
                "recipe": {
                    "id": "r1",
                    "title": "Proteinshake",
                    "totalTime": 300,
                    "squareImage": "https://x/{transformation}/img.jpg",
                },
            },
            {"details": {}, "recipe": {"id": "", "title": "broken"}},
            "junk",
        ],
    }

    history = await session.get_cooking_history(limit=5)

    assert calls[0][:2] == ("GET", "https://cookidoo.de/organize/de-DE/api/cooking-history")
    assert len(history) == 1
    assert history[0].cooked_at == "2026-06-04T13:42:55.188Z"
    assert history[0].recipe.id == "r1"
    assert history[0].recipe.total_time_seconds == 300
    assert history[0].recipe.image is not None
    assert "{transformation}" not in history[0].recipe.image


def test_feed_item_to_dto_keeps_zero_ratings() -> None:
    """A legitimate 0 rating / 0-ratings count must not fall through to the
    alternate key (falsy-zero regression)."""
    from cookidough_mcp.session import _feed_item_to_dto

    dto = _feed_item_to_dto(
        {"id": "r1", "title": "Unrated", "averageRating": 0, "numRating": 0, "rating": 4.5}
    )
    assert dto is not None
    assert dto.rating == 0.0
    assert dto.number_of_ratings == 0
