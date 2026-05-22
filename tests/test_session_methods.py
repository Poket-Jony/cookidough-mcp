"""Behavioural tests for the remaining CookidooSession methods using a fake client."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cookidoo_api.exceptions import CookidooRequestException

from cookidoo_mcp.errors import NotFoundError
from cookidoo_mcp.models import CustomRecipeDraft, RecipeStep
from cookidoo_mcp.session import CookidooSession


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
    from cookidoo_mcp.errors import UpstreamApiError
    from cookidoo_mcp.models import CustomRecipeDraft, RecipeStep

    session = CookidooSession(settings)

    # Pretend the create step never returns. wait_for must cancel it well
    # before the test-suite default timeout.
    async def _never_returns(_name: str) -> str:
        await asyncio.sleep(3600)
        return "should never get here"

    monkeypatch.setattr(session, "_create_empty_custom_recipe", _never_returns)
    # Shrink the bound so the test stays fast.
    monkeypatch.setattr("cookidoo_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

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
    from cookidoo_mcp.errors import UpstreamApiError
    from cookidoo_mcp.models import CustomRecipeDraft, RecipeStep

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
    monkeypatch.setattr("cookidoo_mcp.session.CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS", 0)
    monkeypatch.setattr("cookidoo_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

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
    monkeypatch.setattr("cookidoo_mcp.session.asyncio.sleep", AsyncMock())

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
    monkeypatch.setattr("cookidoo_mcp.session.CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS", 0)
    monkeypatch.setattr("cookidoo_mcp.session.CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS", 0.05)

    draft = CustomRecipeDraft(
        name="N", ingredients=["A"], steps=[RecipeStep(text="Mix 5 min / speed 4.")]
    )
    # The original error must propagate; the hanging rollback must NOT
    # hold the call hostage past the per-step budget.
    with pytest.raises(RuntimeError, match="patch boom"):
        await session.upload_custom_recipe(draft)
