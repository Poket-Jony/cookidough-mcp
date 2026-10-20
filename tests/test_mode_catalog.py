"""Tests for reading the mode catalog from the recipe editor and using it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from cookidough_mcp.annotations import AnnotationInferrer
from cookidough_mcp.config import Settings
from cookidough_mcp.editor_rules import EditorVariant
from cookidough_mcp.mode_catalog import BUILTIN_CATALOG, available_modes, parse_editor_html
from cookidough_mcp.models import CustomRecipeSummary, ModeCatalog, ThermomixMode
from cookidough_mcp.session import CookidoughSession
from cookidough_mcp.tools import register_all

from ._mcp_internals import get_tool_fn

_FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["editor_tm6.html", "editor_tm7.html"])
def test_editor_page_gives_the_builtin_snapshot(page: str) -> None:
    catalog = parse_editor_html(_page(page))

    assert catalog.source == "live"
    assert catalog.model_copy(update={"source": "builtin"}) == BUILTIN_CATALOG


def test_catalog_marks_tm7_only_modes() -> None:
    catalog = parse_editor_html(_page("editor_tm6.html"))

    assert {mode.name for mode in catalog.modes if mode.tm7_only} == {"browning", "steaming"}
    assert catalog.steaming_accessories["SimmeringBasket"] == "im Gareinsatz"
    assert catalog.browning_powers == {"Gentle": "Leicht", "Intense": "Intensiv"}
    assert catalog.turbo_times == ["0.5", "1", "2"]


def test_page_without_dialog_is_rejected() -> None:
    with pytest.raises(ValueError, match="no Thermomix modes"):
        parse_editor_html("<html><body>Please log in</body></html>")


def test_available_modes_per_editor_variant() -> None:
    tm6 = available_modes(BUILTIN_CATALOG, EditorVariant.TM6)
    tm7 = available_modes(BUILTIN_CATALOG, EditorVariant.TM7)

    tm6_names = [mode.name for mode in tm6.modes]
    assert tm6_names == ["dough", "turbo", "blend", "warm_up", "rice_cooker"]
    assert tm6.unavailable == ["Anbraten", "Dampfgaren"]
    assert len(tm7.modes) == 7
    assert tm7.unavailable == []
    assert tm6.modes[0].example == "Teig kneten/2 Min."


def _catalog_with(*modes: ThermomixMode) -> ModeCatalog:
    return BUILTIN_CATALOG.model_copy(update={"modes": list(modes), "source": "live"})


def test_new_platform_mode_is_reported_as_unsupported() -> None:
    catalog = _catalog_with(
        *BUILTIN_CATALOG.modes, ThermomixMode(name="sous_vide", title="Sous-vide", tm7_only=True)
    )

    assert available_modes(catalog, EditorVariant.TM7).unsupported == ["Sous-vide"]


def test_inferrer_learns_the_platforms_mode_names() -> None:
    catalog = _catalog_with(ThermomixMode(name="dough", title="Pétrin", tm7_only=False))

    annotations = AnnotationInferrer.for_tools(["TM6"], catalog).infer("Pétrin/2 Min.", [])

    assert [getattr(annotation, "name", None) for annotation in annotations] == ["dough"]


def test_inferrer_rejects_modes_the_platform_no_longer_offers() -> None:
    catalog = _catalog_with(ThermomixMode(name="dough", title="Teig kneten", tm7_only=False))

    annotations, rejected = AnnotationInferrer.for_tools(["TM6"], catalog).infer_checked(
        "Turbo/1 Sek. hacken.", []
    )

    assert annotations == []
    assert "no longer offers" in rejected[0].reason


async def test_get_thermomix_modes_tool(fake_mcp_context: Any) -> None:
    mcp = MCPServer(name="test-cookidough")
    register_all(mcp)

    result = await get_tool_fn(mcp, "get_thermomix_modes")(fake_mcp_context, tools=["TM6"])

    assert result.editor_variant == "TM6"
    assert "Anbraten" in result.unavailable


class _Response:
    def __init__(self, text: str) -> None:
        self._text = text

    async def text(self) -> str:
        return self._text


def _session_reading(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, page: str | Exception
) -> tuple[CookidoughSession, list[tuple[str, str]]]:
    session = CookidoughSession(settings)
    requests: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _fake_authed_http(
        method: str, url: str, json_body: Any = None, accept: str = "application/json"
    ) -> AsyncIterator[_Response]:
        requests.append((url, accept))
        if isinstance(page, Exception):
            raise page
        yield _Response(page)

    async def _custom_recipes_url() -> str:
        return "https://cookidoo.de/created-recipes/de-DE"

    monkeypatch.setattr(session, "_authed_http", _fake_authed_http)
    monkeypatch.setattr(session, "_custom_recipes_url", _custom_recipes_url)
    monkeypatch.setattr(
        session,
        "list_custom_recipes",
        AsyncMock(return_value=[CustomRecipeSummary(recipe_id="cr1", name="Curry")]),
    )
    return session, requests


async def test_session_reads_the_catalog_once_from_the_editor_page(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    session, requests = _session_reading(monkeypatch, settings, _page("editor_tm7.html"))

    first = await session.get_mode_catalog()
    second = await session.get_mode_catalog()

    assert first.source == "live"
    assert second is first
    assert requests == [
        (
            "https://cookidoo.de/created-recipes/de-DE/cr1/edit/ingredients-and-preparation-steps",
            "text/html",
        )
    ]


@pytest.mark.parametrize("page", ["<html>login</html>", RuntimeError("network down")])
async def test_session_falls_back_to_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, page: str | Exception
) -> None:
    session, _ = _session_reading(monkeypatch, settings, page)

    assert await session.get_mode_catalog() == BUILTIN_CATALOG


async def test_session_without_own_recipes_uses_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    session, requests = _session_reading(monkeypatch, settings, _page("editor_tm6.html"))
    monkeypatch.setattr(session, "list_custom_recipes", AsyncMock(return_value=[]))

    assert await session.get_mode_catalog() == BUILTIN_CATALOG
    assert requests == []


async def test_china_market_uses_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    china = Settings(
        email="13800000000", password=SecretStr("x"), country="cn", language="zh-Hans-CN"
    )
    session, requests = _session_reading(monkeypatch, china, _page("editor_tm6.html"))

    assert await session.get_mode_catalog() == BUILTIN_CATALOG
    assert requests == []
