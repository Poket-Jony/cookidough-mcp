"""Adapter that turns external recipe URLs into `CustomRecipeDraft`s.

Uses `recipe-scrapers` (200+ supported sites) to parse the remote HTML, then
maps the result into the internal draft model. Any guided-cooking annotations
must still be added manually before upload — the importer only carries raw
step text and ingredient lines through.

Note: image URLs are not propagated to `CustomRecipeDraft` because the
Cookidoo upload endpoint does not accept arbitrary external image URLs; the
official apps upload binary blobs after the recipe is created. Adding image
upload is intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from recipe_scrapers import scrape_me

from .errors import WebImportError
from .models import CustomRecipeDraft, RecipeStep

_LOGGER = logging.getLogger(__name__)

# Split instructions on hard newlines or on prose sentence boundaries (a
# lower-case letter, then ``. ``, then an upper-case letter). Using a
# fixed-width lookbehind on ``letter+period`` keeps the period attached to the
# preceding step. The lower→upper transition keeps TM7-style annotations like
# ``5 min. / 100 °C`` together (next char is ``/``, not a capital) while still
# splitting normal prose like ``Chop the onion. Add it to the pan.``.
_STEP_SPLIT_PATTERN = re.compile(r"\n+|(?<=[a-zäöüß]\.)\s+(?=[A-ZÄÖÜ])")

_DEFAULT_SERVINGS = 4
# When the upstream recipe lists only a total time, we estimate prep as a
# third of it — a common rule of thumb across the supported recipe sites.
_PREP_FROM_TOTAL_DIVISOR = 3


class RecipeScraper(Protocol):
    """Subset of `recipe-scrapers` API used by this adapter."""

    def title(self) -> str: ...
    def ingredients(self) -> list[str]: ...
    def instructions(self) -> str: ...
    def yields(self) -> str | None: ...
    def total_time(self) -> int | None: ...
    def prep_time(self) -> int | None: ...


ScraperFactory = Callable[[str], RecipeScraper]


class WebRecipeImporter:
    """Fetches a recipe URL and converts it to a `CustomRecipeDraft`.

    The factory is injectable so tests can substitute a fake scraper without
    real network access.
    """

    def __init__(self, scraper_factory: ScraperFactory = scrape_me) -> None:
        self._factory = scraper_factory

    async def fetch(self, url: str, name_override: str | None = None) -> CustomRecipeDraft:
        _LOGGER.info("Importing external recipe from %s", url)
        try:
            scraper = await asyncio.to_thread(self._factory, url)
        except Exception as e:
            raise WebImportError(f"Could not scrape {url!r}: {e}") from e
        return self._map(scraper, name_override)

    def _map(self, scraper: RecipeScraper, name_override: str | None) -> CustomRecipeDraft:
        # ``name_override is not None`` rather than truthy: an explicit empty
        # string from the caller is an input bug, not a request to fall back
        # to the scraper's title.
        if name_override is not None:
            title = name_override
        else:
            title = _safe_call(scraper.title) or "Imported recipe"
        raw_ingredients = _safe_call(scraper.ingredients) or []
        ingredients = [str(item).strip() for item in raw_ingredients if str(item).strip()]
        if not ingredients:
            raise WebImportError("Scraped recipe has no ingredients.")
        steps = _split_instructions(_safe_call(scraper.instructions) or "")
        if not steps:
            raise WebImportError("Scraped recipe has no instructions.")
        parsed_yield = _parse_yield(_safe_call(scraper.yields))
        # ``or`` would also fall through on ``0``, but a legitimate (if rare)
        # scraper response of ``0`` should not be silently overwritten with
        # the default. Treat only ``None`` / non-positive as "missing".
        servings = parsed_yield if parsed_yield and parsed_yield > 0 else _DEFAULT_SERVINGS
        total = int(_safe_call(scraper.total_time) or 0)
        prep_value = _safe_call(scraper.prep_time)
        prep = (
            int(prep_value) if prep_value is not None else max(0, total // _PREP_FROM_TOTAL_DIVISOR)
        )
        # CustomRecipeDraft enforces total >= prep; if we only have prep,
        # treat it as the total instead of producing inconsistent values.
        total_minutes = total if total >= prep else prep
        return CustomRecipeDraft(
            name=title,
            ingredients=ingredients,
            steps=[RecipeStep(text=step) for step in steps],
            servings=servings,
            prep_minutes=prep,
            total_minutes=total_minutes,
        )


def _safe_call(getter: Callable[[], Any]) -> Any:
    """Invoke a `recipe-scrapers` getter, returning ``None`` on missing data.

    Catches the small set of exceptions ``recipe-scrapers`` raises for
    fields the site does not expose. ``NotImplementedError`` propagates so
    a test fake that forgot to implement a getter surfaces loudly instead
    of being mistaken for "site has no ingredients".
    """
    try:
        value = getter()
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _split_instructions(text: str) -> list[str]:
    return [step.strip() for step in _STEP_SPLIT_PATTERN.split(text) if step.strip()]


def _parse_yield(value: Any) -> int | None:
    """Extract the first integer from a yield string, e.g. ``"4-6 servings" -> 4``.

    The lower bound of a range is returned because Cookidoo's custom-recipe
    schema only accepts a single integer; users can edit the value after the
    upload if they need the upper bound instead.
    """
    if value is None:
        return None
    text = str(value)
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None
