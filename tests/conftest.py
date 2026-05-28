"""Shared fixtures and stand-ins for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from cookidoo_mcp.config import Settings
from cookidoo_mcp.context import AppContext
from cookidoo_mcp.models import (
    AdditionalItemRename,
    CalendarDay,
    CalendarRecipe,
    CollectionSummary,
    CustomRecipeDetails,
    CustomRecipeSummary,
    Ingredient,
    RecipeDetails,
    RecipeSearchResult,
    RecipeSuggestion,
    ShoppingItemOwnershipUpdate,
    ShoppingItemSource,
    ShoppingList,
    ShoppingListItem,
    Subscription,
    UserProfile,
)
from cookidoo_mcp.quality import QualityScorer
from cookidoo_mcp.session import CookidooSessionProtocol
from cookidoo_mcp.web_import import WebRecipeImporter


@dataclass
class _Calls:
    add_recipes_to_shopping_list: list[list[str]] = field(default_factory=list)
    upload_drafts: list[Any] = field(default_factory=list)
    add_custom_recipes_to_shopping_list: list[list[str]] = field(default_factory=list)
    add_custom_recipes_to_calendar: list[tuple[date, list[str]]] = field(default_factory=list)
    set_ingredient_ownership: list[list[ShoppingItemOwnershipUpdate]] = field(default_factory=list)
    set_additional_ownership: list[list[ShoppingItemOwnershipUpdate]] = field(default_factory=list)
    rename_additional: list[list[AdditionalItemRename]] = field(default_factory=list)
    clone_recipe_as_custom: list[tuple[str, int]] = field(default_factory=list)
    search_recipes: list[tuple[str, int]] = field(default_factory=list)
    suggest_calls: list[tuple[list[str], list[str] | None, int]] = field(default_factory=list)


class FakeSession:
    """Stand-in for `CookidooSession` with deterministic responses."""

    def __init__(self) -> None:
        self.calls = _Calls()

    async def get_user_profile(self) -> UserProfile:
        return UserProfile(username="alice", description=None, picture=None)

    async def get_subscription(self) -> Subscription | None:
        return Subscription(
            active=True,
            status="ACTIVE",
            subscription_level="PREMIUM",
            subscription_source="STORE",
            type="MONTHLY",
            extended_type="MONTHLY",
            start_date="2025-01-01",
            expires="2026-01-01",
        )

    async def get_recipe_details(self, recipe_id: str) -> RecipeDetails:
        return RecipeDetails(
            id=recipe_id,
            name="Sample",
            url=f"https://cookidoo.de/recipes/{recipe_id}",
            serving_size=4,
            active_time_seconds=600,
            total_time_seconds=1800,
            ingredients=[Ingredient(id="i1", name="Salt", description="1 tsp")],
        )

    async def get_custom_recipe_details(self, recipe_id: str) -> CustomRecipeDetails:
        return CustomRecipeDetails(
            id=recipe_id,
            name="Custom",
            url=f"https://cookidoo.de/recipes/custom-recipes/{recipe_id}",
            serving_size=4,
            active_time_seconds=600,
            total_time_seconds=1800,
        )

    async def list_managed_collections(self, page: int = 0) -> list[CollectionSummary]:
        return [CollectionSummary(id="mc1", name="Quick meals", recipe_count=5)]

    async def add_managed_collection(self, collection_id: str) -> CollectionSummary:
        return CollectionSummary(id=collection_id, name="Added", recipe_count=0)

    async def remove_managed_collection(self, collection_id: str) -> None:
        return None

    async def list_custom_collections(self, page: int = 0) -> list[CollectionSummary]:
        return [CollectionSummary(id="cc1", name="My picks", recipe_count=2)]

    async def create_custom_collection(self, name: str) -> CollectionSummary:
        return CollectionSummary(id="cc-new", name=name)

    async def delete_custom_collection(self, collection_id: str) -> None:
        return None

    async def add_recipes_to_custom_collection(
        self, collection_id: str, recipe_ids: list[str]
    ) -> CollectionSummary:
        return CollectionSummary(id=collection_id, name="Updated", recipe_count=len(recipe_ids))

    async def remove_recipe_from_custom_collection(
        self, collection_id: str, recipe_id: str
    ) -> None:
        return None

    async def get_shopping_list(self) -> ShoppingList:
        return ShoppingList(
            ingredient_items=[
                ShoppingListItem(
                    id="i1", name="Tomato", description="2", source=ShoppingItemSource.RECIPE
                )
            ],
            additional_items=[
                ShoppingListItem(id="a1", name="Salt", source=ShoppingItemSource.ADDITIONAL)
            ],
        )

    async def add_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int:
        self.calls.add_recipes_to_shopping_list.append(list(recipe_ids))
        return len(recipe_ids) * 3

    async def remove_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None:
        return None

    async def add_additional_items(self, names: list[str]) -> list[ShoppingListItem]:
        return [
            ShoppingListItem(id=f"a-{i}", name=name, source=ShoppingItemSource.ADDITIONAL)
            for i, name in enumerate(names)
        ]

    async def remove_additional_items(self, item_ids: list[str]) -> None:
        return None

    async def clear_shopping_list(self) -> None:
        return None

    async def get_calendar_week(self, day: date) -> list[CalendarDay]:
        return [
            CalendarDay(
                id=day.isoformat(),
                title=day.strftime("%A"),
                recipes=[
                    CalendarRecipe(
                        id="r1",
                        name="Pasta",
                        total_time_seconds=1200,
                        url="https://cookidoo.de/recipes/r1",
                    )
                ],
            )
        ]

    async def add_recipes_to_calendar(self, day: date, recipe_ids: list[str]) -> CalendarDay:
        return CalendarDay(id=day.isoformat(), title="Monday")

    async def remove_recipe_from_calendar(self, day: date, recipe_id: str) -> CalendarDay:
        return CalendarDay(id=day.isoformat(), title="Monday")

    async def add_custom_recipes_to_calendar(self, day: date, recipe_ids: list[str]) -> CalendarDay:
        self.calls.add_custom_recipes_to_calendar.append((day, list(recipe_ids)))
        return CalendarDay(id=day.isoformat(), title="Monday", custom_recipe_ids=list(recipe_ids))

    async def remove_custom_recipe_from_calendar(self, day: date, recipe_id: str) -> CalendarDay:
        return CalendarDay(id=day.isoformat(), title="Monday")

    async def list_custom_recipes(self) -> list[CustomRecipeSummary]:
        return [CustomRecipeSummary(recipe_id="cr1", name="Test")]

    async def upload_custom_recipe(self, draft: Any) -> tuple[str, str]:
        self.calls.upload_drafts.append(draft)
        return "new-id", "https://cookidoo.de/recipes/custom-recipes/new-id"

    async def delete_custom_recipe(self, recipe_id: str) -> None:
        return None

    async def clone_recipe_as_custom(
        self, recipe_id: str, serving_size: int
    ) -> CustomRecipeDetails:
        self.calls.clone_recipe_as_custom.append((recipe_id, serving_size))
        return CustomRecipeDetails(
            id=f"clone-of-{recipe_id}",
            name="Cloned",
            url=f"https://cookidoo.de/recipes/custom-recipes/clone-of-{recipe_id}",
            serving_size=serving_size,
        )

    async def add_custom_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int:
        self.calls.add_custom_recipes_to_shopping_list.append(list(recipe_ids))
        return len(recipe_ids) * 2

    async def remove_custom_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None:
        return None

    async def set_ingredient_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        self.calls.set_ingredient_ownership.append(list(updates))
        return [
            ShoppingListItem(
                id=u.id, name="x", is_owned=u.is_owned, source=ShoppingItemSource.RECIPE
            )
            for u in updates
        ]

    async def set_additional_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        self.calls.set_additional_ownership.append(list(updates))
        return [
            ShoppingListItem(
                id=u.id, name="x", is_owned=u.is_owned, source=ShoppingItemSource.ADDITIONAL
            )
            for u in updates
        ]

    async def rename_additional_items(
        self, updates: list[AdditionalItemRename]
    ) -> list[ShoppingListItem]:
        self.calls.rename_additional.append(list(updates))
        return [
            ShoppingListItem(id=u.id, name=u.name, source=ShoppingItemSource.ADDITIONAL)
            for u in updates
        ]

    async def search_recipes(self, query: str, limit: int = 10) -> list[RecipeSearchResult]:
        self.calls.search_recipes.append((query, limit))
        return [
            RecipeSearchResult(
                id="s1",
                name=f"Result for {query}",
                rating=4.5,
                number_of_ratings=10,
                total_time_seconds=1800,
                image=None,
            )
        ]

    async def suggest_recipes_from_ingredients(
        self,
        available_ingredients: list[str],
        collection_ids: list[str] | None = None,
        max_results: int = 10,
    ) -> list[RecipeSuggestion]:
        self.calls.suggest_calls.append(
            (
                list(available_ingredients),
                list(collection_ids) if collection_ids else None,
                max_results,
            )
        )
        return [
            RecipeSuggestion(
                recipe=RecipeDetails(
                    id="sug1",
                    name="Suggested",
                    url="https://cookidoo.de/recipes/sug1",
                    ingredients=[Ingredient(id="i", name=available_ingredients[0])],
                ),
                score=1.0,
                matching_ingredients=[available_ingredients[0]],
                missing_ingredients=[],
                total_ingredients=1,
            )
        ]

    async def aclose(self) -> None:
        return None


# Static conformity guard: if the protocol grows a method, this assignment
# breaks at type-check time so the fake can never silently fall out of sync.
_PROTOCOL_GUARD: CookidooSessionProtocol = FakeSession()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        email="test@example.com",
        password=SecretStr("hunter2"),
        country="de",
        language="de",
        quality_bar=70,
    )


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def app_context(settings: Settings, fake_session: FakeSession) -> AppContext:
    return AppContext(
        settings=settings,
        session=fake_session,
        scorer=QualityScorer(threshold=settings.quality_bar),
        importer=WebRecipeImporter(scraper_factory=_NoOpScraperFactory()),
    )


@pytest.fixture
def fake_mcp_context(app_context: AppContext) -> Iterator[Any]:
    """A minimal `Context`-shaped object exposing `request_context.lifespan_context`."""

    class _RequestContext:
        def __init__(self, lifespan_context: AppContext) -> None:
            self.lifespan_context = lifespan_context

    class _Context:
        def __init__(self, lifespan_context: AppContext) -> None:
            self.request_context = _RequestContext(lifespan_context)
            self.info = AsyncMock()
            self.error = AsyncMock()

    yield _Context(app_context)


class _NoOpScraperFactory:
    """Fallback factory used when tests do not exercise web import."""

    def __call__(self, url: str) -> Any:  # pragma: no cover - never called by default
        raise NotImplementedError("Inject a scraper factory in the importing test.")
