"""Behavioural tests for the remaining CookidooSession methods using a fake client."""

from __future__ import annotations

import asyncio
from datetime import date
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
from cookidough_mcp.session import CookidooSession


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
def patched_session(monkeypatch: pytest.MonkeyPatch, settings: Any) -> tuple[CookidooSession, Any]:
    session = CookidooSession(settings)
    fake_client = AsyncMock()

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    return session, fake_client


async def test_get_user_profile(patched_session: tuple[CookidooSession, Any]) -> None:
    session, fake = patched_session
    fake.get_user_info = AsyncMock(return_value=_NS(username="u", description="d", picture="p"))
    profile = await session.get_user_profile()
    assert profile.username == "u"


async def test_custom_recipes_url_logs_in_on_first_use(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Regression: a fresh session that goes straight into a custom-recipe
    operation (e.g. ``import_web_recipe`` → upload, with no prior session-
    touching tool call) used to fail with ``UpstreamApiError("Session is not
    logged in.")`` because ``_custom_recipes_url`` called ``_require_client``
    without first triggering login. After the fix the URL helper must
    trigger ``_ensure_logged_in`` itself."""
    session = CookidooSession(settings)
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

    session = CookidooSession(settings)

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

    session = CookidooSession(settings)
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
    session = CookidooSession(settings)
    fake_client = _NS(localization=_NS(url="https://cookidoo.de", language="de-DE"))

    async def _login() -> Any:
        return fake_client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)

    url = await session._custom_recipe_public_url("r123")
    assert url == "https://cookidoo.de/recipes/custom-recipes/r123"


async def test_get_subscription_returns_none(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_active_subscription = AsyncMock(return_value=None)
    assert await session.get_subscription() is None


async def test_get_subscription_maps_fields(
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_custom_recipe = AsyncMock(side_effect=CookidooRequestException("404"))
    with pytest.raises(NotFoundError):
        await session.get_custom_recipe_details("missing")


async def test_collection_methods(patched_session: tuple[CookidooSession, Any]) -> None:
    session, fake = patched_session
    fake.get_managed_collections = AsyncMock(return_value=[_make_collection()])
    fake.add_managed_collection = AsyncMock(return_value=_make_collection())
    fake.remove_managed_collection = AsyncMock(return_value=None)
    fake.get_custom_collections = AsyncMock(return_value=[_make_collection()])
    fake.add_custom_collection = AsyncMock(return_value=_make_collection())
    fake.remove_custom_collection = AsyncMock(return_value=None)
    fake.add_recipes_to_custom_collection = AsyncMock(return_value=_make_collection())
    fake.remove_recipe_from_custom_collection = AsyncMock(return_value=None)

    assert (await session.list_managed_collections())[0].id == "c"
    assert (await session.add_managed_collection("c")).id == "c"
    await session.remove_managed_collection("c")
    assert (await session.list_custom_collections())[0].id == "c"
    assert (await session.create_custom_collection("name")).id == "c"
    await session.delete_custom_collection("c")
    assert (await session.add_recipes_to_custom_collection("c", ["r1"])).id == "c"
    await session.remove_recipe_from_custom_collection("c", "r1")


async def test_shopping_list_methods(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.get_ingredient_items = AsyncMock(
        return_value=[_NS(id="i", name="n", description="d", is_owned=False)]
    )
    fake.get_additional_items = AsyncMock(return_value=[_NS(id="a", name="n2", is_owned=True)])
    fake.add_ingredient_items_for_recipes = AsyncMock(return_value=[1, 2, 3])
    fake.remove_ingredient_items_for_recipes = AsyncMock(return_value=None)
    fake.add_additional_items = AsyncMock(return_value=[_NS(id="x", name="n", is_owned=False)])
    fake.remove_additional_items = AsyncMock(return_value=None)
    fake.clear_shopping_list = AsyncMock(return_value=None)

    shopping = await session.get_shopping_list()
    assert shopping.ingredient_items[0].source == "recipe"
    assert shopping.additional_items[0].source == "additional"
    assert await session.add_recipes_to_shopping_list(["r"]) == 3
    await session.remove_recipes_from_shopping_list(["r"])
    items = await session.add_additional_items(["Salt"])
    assert items[0].name == "n"
    await session.remove_additional_items(["x"])
    await session.clear_shopping_list()


async def test_calendar_methods(patched_session: tuple[CookidooSession, Any]) -> None:
    session, fake = patched_session
    fake.get_recipes_in_calendar_week = AsyncMock(return_value=[_make_calendar_day()])
    fake.add_recipes_to_calendar = AsyncMock(return_value=_make_calendar_day())
    fake.remove_recipe_from_calendar = AsyncMock(return_value=_make_calendar_day())

    assert (await session.get_calendar_week(date(2026, 5, 21)))[0].id == "2026-05-21"
    assert (await session.add_recipes_to_calendar(date(2026, 5, 21), ["r"])).id == "2026-05-21"
    assert (await session.remove_recipe_from_calendar(date(2026, 5, 21), "r")).id == "2026-05-21"


async def test_delete_custom_recipe_delegates_to_client(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_custom_recipe = AsyncMock(return_value=None)
    await session.delete_custom_recipe("cr1")
    fake.remove_custom_recipe.assert_awaited_once_with("cr1")


async def test_upload_custom_recipe_rolls_back_on_patch_failure(
    monkeypatch: pytest.MonkeyPatch,
    patched_session: tuple[CookidooSession, Any],
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


async def test_upload_custom_recipe_rollback_is_itself_bounded_on_hang(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Regression: a PATCH failure used to be followed by an unbounded
    rollback ``delete_custom_recipe`` call. Under cancellation that
    rollback could itself stall, defeating the upper-bound ``wait_for``
    around the PATCH. The rollback now has its own hard deadline (half
    the operation budget) and surfaces the original error rather than
    the rollback's hang."""
    session = CookidooSession(settings)
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.add_custom_recipes_to_calendar = AsyncMock(return_value=_make_calendar_day())
    result = await session.add_custom_recipes_to_calendar(date(2026, 5, 21), ["cr1"])
    assert result.id == "2026-05-21"
    fake.add_custom_recipes_to_calendar.assert_awaited_once_with(date(2026, 5, 21), ["cr1"])


async def test_remove_custom_recipe_from_calendar(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_custom_recipe_from_calendar = AsyncMock(return_value=_make_calendar_day())
    result = await session.remove_custom_recipe_from_calendar(date(2026, 5, 21), "cr1")
    assert result.id == "2026-05-21"


async def test_add_custom_recipes_to_shopping_list_counts_items(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.add_ingredient_items_for_custom_recipes = AsyncMock(return_value=[1, 2, 3, 4])
    assert await session.add_custom_recipes_to_shopping_list(["cr1"]) == 4


async def test_remove_custom_recipes_from_shopping_list_delegates(
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.remove_ingredient_items_for_custom_recipes = AsyncMock(return_value=None)
    await session.remove_custom_recipes_from_shopping_list(["cr1", "cr2"])
    fake.remove_ingredient_items_for_custom_recipes.assert_awaited_once_with(["cr1", "cr2"])


async def test_set_ingredient_items_ownership_maps_response(
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    fake.edit_additional_items = AsyncMock(
        return_value=[_NS(id="a1", name="Sea salt", is_owned=False)]
    )
    items = await session.rename_additional_items([AdditionalItemRename(id="a1", name="Sea salt")])
    assert items[0].name == "Sea salt"


async def test_clone_recipe_as_custom_maps_response(
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    chapter = _NS(name="ch", recipes=[_NS(id="r1"), _NS(id="r2")])
    collection = _NS(id="c1", chapters=[chapter])
    _mock_collections(fake, managed=[], custom=[collection])

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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, _ = patched_session
    assert await session.suggest_recipes_from_ingredients([]) == []


async def test_suggest_recipes_filters_by_collection_ids(
    patched_session: tuple[CookidooSession, Any],
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

    session = CookidooSession(settings)
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


async def test_search_recipes_drops_rows_without_title(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from contextlib import asynccontextmanager

    session = CookidooSession(settings)
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

    session = CookidooSession(settings)
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

    session = CookidooSession(settings)
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

    session = CookidooSession(settings)
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    collection = _NS(id="c", chapters=[_NS(name="x", recipes=[_NS(id="r1")])])
    _mock_collections(fake, managed=[], custom=[collection])

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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    session, fake = patched_session
    collection = _NS(id="c", chapters=[_NS(name="x", recipes=[_NS(id="ok"), _NS(id="boom")])])
    _mock_collections(fake, managed=[], custom=[collection])

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
    patched_session: tuple[CookidooSession, Any],
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

    async def _details(_rid: str) -> Any:
        raise UpstreamApiError("Session is closed.")

    session.get_recipe_details = _details  # type: ignore[method-assign,assignment]
    with pytest.raises(UpstreamApiError, match="closed"):
        await session.suggest_recipes_from_ingredients(["rice"])


async def test_suggest_recipes_drops_short_ingredient_tokens(
    patched_session: tuple[CookidooSession, Any],
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
    patched_session: tuple[CookidooSession, Any],
) -> None:
    """A user with multiple pages of collections must have every page
    walked, not just page 0."""
    session, fake = patched_session
    page0 = _NS(id="c0", chapters=[_NS(name="x", recipes=[_NS(id="r0")])])
    page1 = _NS(id="c1", chapters=[_NS(name="x", recipes=[_NS(id="r1")])])

    fake.count_managed_collections = AsyncMock(return_value=(2, 2))
    fake.count_custom_collections = AsyncMock(return_value=(0, 0))

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
