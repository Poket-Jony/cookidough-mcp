"""Tests for the web-import adapter."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cookidough_mcp.errors import WebImportError
from cookidough_mcp.web_import import WebRecipeImporter


class _FakeScraper:
    def __init__(
        self,
        title: str = "Web Risotto",
        ingredients: list[str] | None = None,
        instructions: str = "Chop onion. Cook rice. Serve.",
        yields: str | None = "4 servings",
        total: int | None = 30,
        prep: int | None = 10,
    ) -> None:
        self._title = title
        self._ingredients = ingredients if ingredients is not None else ["Rice 200 g", "Onion 1"]
        self._instructions = instructions
        self._yields = yields
        self._total = total
        self._prep = prep

    def title(self) -> str:
        return self._title

    def ingredients(self) -> list[str]:
        return self._ingredients

    def instructions(self) -> str:
        return self._instructions

    def yields(self) -> str | None:
        return self._yields

    def total_time(self) -> int | None:
        return self._total

    def prep_time(self) -> int | None:
        return self._prep


def _factory(scraper: _FakeScraper) -> Callable[[str], _FakeScraper]:
    def _f(_url: str) -> _FakeScraper:
        return scraper

    return _f


async def test_fetch_maps_scraper_output_to_draft() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper()))

    draft = await importer.fetch("https://example.com/recipe")

    assert draft.name == "Web Risotto"
    assert draft.ingredients == ["Rice 200 g", "Onion 1"]
    assert draft.step_texts == ["Chop onion.", "Cook rice.", "Serve."]
    assert draft.servings == 4
    assert draft.total_minutes == 30
    assert draft.prep_minutes == 10


async def test_fetch_uses_name_override() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper()))
    draft = await importer.fetch("https://example.com", name_override="Custom name")
    assert draft.name == "Custom name"


async def test_fetch_raises_when_ingredients_missing() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(ingredients=[])))
    with pytest.raises(WebImportError):
        await importer.fetch("https://example.com")


async def test_fetch_raises_when_instructions_missing() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(instructions="")))
    with pytest.raises(WebImportError):
        await importer.fetch("https://example.com")


async def test_fetch_falls_back_when_times_missing() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(total=None, prep=None)))
    draft = await importer.fetch("https://example.com")
    assert draft.total_minutes == 0
    assert draft.prep_minutes == 0


async def test_fetch_estimates_prep_from_total_when_only_total_given() -> None:
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(total=60, prep=None)))
    draft = await importer.fetch("https://example.com")
    assert draft.total_minutes == 60
    assert draft.prep_minutes == 20


async def test_fetch_preserves_tm7_annotations_with_periods() -> None:
    """TM7 annotations like ``5 min. / 100 °C`` must not be split into pieces."""
    instructions = (
        "Mix 5 min. / 100 °C / Stufe 3 with the spatula.\nAdd salt and stir 1 min. / speed 4."
    )
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(instructions=instructions)))
    draft = await importer.fetch("https://example.com")
    assert len(draft.steps) == 2
    assert "5 min. / 100 °C / Stufe 3" in draft.step_texts[0]
    assert "1 min. / speed 4" in draft.step_texts[1]


async def test_fetch_total_at_least_prep() -> None:
    """When only prep is given, total falls back to prep so the draft validator
    that requires total >= prep is satisfied."""
    importer = WebRecipeImporter(scraper_factory=_factory(_FakeScraper(total=None, prep=15)))
    draft = await importer.fetch("https://example.com")
    assert draft.prep_minutes == 15
    assert draft.total_minutes == 15


async def test_fetch_propagates_scraper_failure() -> None:
    def _boom(_url: str) -> _FakeScraper:
        raise RuntimeError("network unreachable")

    importer = WebRecipeImporter(scraper_factory=_boom)
    with pytest.raises(WebImportError):
        await importer.fetch("https://example.com")
