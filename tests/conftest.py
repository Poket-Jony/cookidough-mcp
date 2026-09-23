"""Shared fixtures and stand-ins for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from cookidough_mcp.config import Settings
from cookidough_mcp.context import AppContext
from cookidough_mcp.models import (
    AdditionalItemRename,
    CalendarDay,
    CalendarRecipe,
    CalendarShoppingSummary,
    CollectionPage,
    CollectionSummary,
    CookedRecipe,
    CustomRecipeDetails,
    CustomRecipeImageResult,
    CustomRecipeSummary,
    Ingredient,
    RecipeDetails,
    RecipeImage,
    RecipeInteractions,
    RecipeSearchResult,
    RecipeSuggestion,
    ShoppingItemOwnershipUpdate,
    ShoppingItemSource,
    ShoppingList,
    ShoppingListItem,
    Subscription,
    UserProfile,
)
from cookidough_mcp.quality import QualityScorer
from cookidough_mcp.session import CookidoughSessionProtocol
from cookidough_mcp.web_import import WebRecipeImporter


@dataclass
class _Calls:
    add_recipes_to_shopping_list: list[list[str]] = field(default_factory=list)
    upload_drafts: list[Any] = field(default_factory=list)
    update_drafts: list[tuple[str, Any]] = field(default_factory=list)
    add_custom_recipes_to_shopping_list: list[list[str]] = field(default_factory=list)
    add_custom_recipes_to_calendar: list[tuple[date, list[str]]] = field(default_factory=list)
    set_ingredient_ownership: list[list[ShoppingItemOwnershipUpdate]] = field(default_factory=list)
    set_additional_ownership: list[list[ShoppingItemOwnershipUpdate]] = field(default_factory=list)
    rename_additional: list[list[AdditionalItemRename]] = field(default_factory=list)
    clone_recipe_as_custom: list[tuple[str, int]] = field(default_factory=list)
    search_recipes: list[tuple[str, int]] = field(default_factory=list)
    search_filters: list[dict[str, Any]] = field(default_factory=list)
    suggest_calls: list[tuple[list[str], list[str] | None, int]] = field(default_factory=list)
    calendar_shopping_ranges: list[tuple[date, date]] = field(default_factory=list)
    set_image: list[tuple[str, str]] = field(default_factory=list)
    rate_recipe: list[tuple[str, int]] = field(default_factory=list)
    set_bookmark: list[tuple[str, bool]] = field(default_factory=list)
    set_note: list[tuple[str, str | None]] = field(default_factory=list)
    mark_cooked: list[tuple[str, bool, datetime | None]] = field(default_factory=list)
    recommendation_calls: list[tuple[str | None, int]] = field(default_factory=list)


class FakeSession:
    """Stand-in for `CookidoughSession` with deterministic responses."""

    def __init__(self) -> None:
        self.calls = _Calls()

    async def get_user_profile(self) -> UserProfile:
        return UserProfile(id="user-1", username="alice", description=None, picture=None)

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

    async def get_recipe_images(self, recipe_id: str) -> list[RecipeImage]:
        base = f"https://assets.tmecosys.com/image/upload/t_full/img/{recipe_id}"
        return [
            RecipeImage(square=f"{base}/a", portrait=f"{base}/a-p", landscape=f"{base}/a-l"),
            RecipeImage(square=f"{base}/b", portrait=None, landscape=None),
        ]

    async def get_custom_recipe_details(self, recipe_id: str) -> CustomRecipeDetails:
        return CustomRecipeDetails(
            id=recipe_id,
            name="Custom",
            url=f"https://cookidoo.de/recipes/custom-recipes/{recipe_id}",
            serving_size=4,
            active_time_seconds=600,
            total_time_seconds=1800,
        )

    async def list_managed_collections(self, page: int = 0) -> CollectionPage:
        return CollectionPage(
            items=[CollectionSummary(id="mc1", name="Quick meals", recipe_count=5)],
            page=page,
            total_pages=1,
            total_elements=1,
        )

    async def add_managed_collection(self, collection_id: str) -> CollectionSummary:
        return CollectionSummary(id=collection_id, name="Added", recipe_count=0)

    async def remove_managed_collection(self, collection_id: str) -> None:
        return None

    async def list_custom_collections(self, page: int = 0) -> CollectionPage:
        return CollectionPage(
            items=[CollectionSummary(id="cc1", name="My picks", recipe_count=2)],
            page=page,
            total_pages=1,
            total_elements=1,
        )

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

    async def update_custom_recipe(self, recipe_id: str, draft: Any) -> tuple[str, str]:
        self.calls.update_drafts.append((recipe_id, draft))
        return recipe_id, f"https://cookidoo.de/recipes/custom-recipes/{recipe_id}"

    async def set_custom_recipe_image(
        self, recipe_id: str, image_source: str
    ) -> CustomRecipeImageResult:
        self.calls.set_image.append((recipe_id, image_source))
        return CustomRecipeImageResult(
            recipe_id=recipe_id,
            image="https://ugc.assets.tmecosys.com/image/upload/t_x/prod/img/customer-recipe/a.jpg",
            thumbnail="https://ugc.assets.tmecosys.com/image/upload/t_y/prod/img/customer-recipe/a.jpg",
            url=f"https://cookidoo.de/recipes/custom-recipes/{recipe_id}",
        )

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

    async def search_recipes(
        self,
        query: str,
        limit: int = 10,
        *,
        max_total_minutes: int | None = None,
        difficulty: str | None = None,
        categories: list[str] | None = None,
        ingredients: list[str] | None = None,
        exclude_ingredients: list[str] | None = None,
        min_rating: float | None = None,
        portions: int | None = None,
        thermomix_version: str | None = None,
        accessories: list[str] | None = None,
        sort_by: str | None = None,
    ) -> list[RecipeSearchResult]:
        self.calls.search_recipes.append((query, limit))
        self.calls.search_filters.append(
            {
                "max_total_minutes": max_total_minutes,
                "difficulty": difficulty,
                "categories": categories,
                "ingredients": ingredients,
                "exclude_ingredients": exclude_ingredients,
                "min_rating": min_rating,
                "portions": portions,
                "thermomix_version": thermomix_version,
                "accessories": accessories,
                "sort_by": sort_by,
            }
        )
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

    async def add_calendar_range_to_shopping_list(
        self, start: date, end: date
    ) -> CalendarShoppingSummary:
        self.calls.calendar_shopping_ranges.append((start, end))
        return CalendarShoppingSummary(
            recipe_ids=["r1", "r2"],
            custom_recipe_ids=["c1"],
            item_count=9,
        )

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

    async def rate_recipe(self, recipe_id: str, stars: int) -> None:
        self.calls.rate_recipe.append((recipe_id, stars))

    async def set_recipe_bookmark(self, recipe_id: str, bookmarked: bool) -> None:
        self.calls.set_bookmark.append((recipe_id, bookmarked))

    async def set_recipe_note(self, recipe_id: str, text: str | None) -> None:
        self.calls.set_note.append((recipe_id, text))

    async def mark_recipe_cooked(
        self, recipe_id: str, is_custom: bool = False, cooked_at: datetime | None = None
    ) -> None:
        self.calls.mark_cooked.append((recipe_id, is_custom, cooked_at))

    async def get_cooking_history(self, limit: int = 20) -> list[CookedRecipe]:
        return [
            CookedRecipe(
                cooked_at="2026-06-04T13:42:55.188Z",
                recipe=RecipeSearchResult(id="hist1", name="Proteinshake"),
            )
        ][:limit]

    async def get_recipe_interactions(self, recipe_id: str) -> RecipeInteractions:
        return RecipeInteractions(
            recipe_id=recipe_id,
            own_rating=4,
            average_rating=4.3,
            number_of_ratings=12,
            note="Weniger Salz nehmen.",
        )

    async def get_recipe_recommendations(
        self, recipe_id: str | None = None, limit: int = 10
    ) -> list[RecipeSearchResult]:
        self.calls.recommendation_calls.append((recipe_id, limit))
        return [RecipeSearchResult(id="rec1", name="Recommended", rating=4.8)]

    async def list_bookmarked_recipes(self) -> list[RecipeSearchResult]:
        return [RecipeSearchResult(id="bm1", name="Bookmarked")]

    async def get_user_devices(self) -> tuple[list[str], list[str]]:
        return ["TM6"], ["Varoma"]

    async def aclose(self) -> None:
        return None


# Static conformity guard: if the protocol grows a method, this assignment
# breaks at type-check time so the fake can never silently fall out of sync.
_PROTOCOL_GUARD: CookidoughSessionProtocol = FakeSession()


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
