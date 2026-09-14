"""Repository facade over `cookidoo-api` plus undocumented custom-recipe endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import quote, quote_plus, urlencode, urlsplit

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    CookieJar,
    FormData,
    TCPConnector,
)
from cookidoo_api import (
    Cookidoo,
    CookidooConfig,
    CookidooLocalizationConfig,
    get_localization_options,
)
from cookidoo_api.exceptions import (
    CookidooAuthException,
    CookidooConfigException,
    CookidooException,
    CookidooParseException,
    CookidooRequestException,
)

from .annotation_models import StepAnnotation
from .annotations import AnnotationInferrer
from .china_client import CHINA_LOCALIZATION, ChinaCookidoo
from .constants import (
    CLOUDINARY_API_KEY,
    CLOUDINARY_UPLOAD_PRESET,
    CLOUDINARY_UPLOAD_SOURCE,
    CLOUDINARY_UPLOAD_URL,
    CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS,
    CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MAX_RECIPE_IMAGE_BYTES,
    SUGGEST_MIN_INGREDIENT_LENGTH,
    SUGGEST_RECIPE_FETCH_CONCURRENCY,
    SUGGEST_SEARCH_CANDIDATE_FACTOR,
)
from .errors import AuthenticationError, NotFoundError, UpstreamApiError
from .models import (
    AdditionalItemRename,
    CalendarDay,
    CalendarRecipe,
    CalendarShoppingSummary,
    CollectionPage,
    CollectionSummary,
    CookedRecipe,
    CustomRecipeDetails,
    CustomRecipeDraft,
    CustomRecipeImageResult,
    CustomRecipeSummary,
    Ingredient,
    NutritionInfo,
    NutritionValue,
    RecipeCategory,
    RecipeCollectionRef,
    RecipeDetails,
    RecipeImage,
    RecipeInteractions,
    RecipeSearchResult,
    RecipeStep,
    RecipeSuggestion,
    ShoppingItemOwnershipUpdate,
    ShoppingItemSource,
    ShoppingList,
    ShoppingListItem,
    ShoppingListRecipe,
    Subscription,
    UserProfile,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from .config import Settings


_LOGGER = logging.getLogger(__name__)

# Cookidoo error bodies are diagnostic JSON that may echo back tokens, emails
# or other PII. We surface only a truncated, redacted excerpt to the caller.
_ERROR_BODY_LIMIT = 200
# ``\b`` anchors keep substrings like ``my_csrf`` or ``request_id_token`` from
# triggering a match; only the exact key tokens are recognised. The optional
# enclosing quotes are anchored as a capturing group and matched again via
# a backref so the closing quote must mirror the opening one (and the regex
# can't accidentally swallow a neighbouring JSON value's quote on output).
_TOKEN_REDACT_PATTERN = re.compile(
    r'(?i)("?)\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|'
    r"session[_-]?id|csrf|authorization|bearer)\b\1"
    r'\s*[:=]\s*("?)[^",\s}]+\2'
)
_BEARER_PLAINTEXT_PATTERN = re.compile(r"(?i)\bbearer\s+[\w.\-+/=]+")
_JWT_PATTERN = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b")
_EMAIL_REDACT_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+")
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class CookidoughSessionProtocol(Protocol):
    """Public surface of the Cookidoo session used by tools and tests.

    Lifecycle methods (``__aenter__``/``__aexit__``) live on the concrete
    `CookidoughSession` only; the lifespan in `server.build_server` calls
    `aclose` directly, so the protocol covers exactly the surface that tools
    consume.
    """

    async def get_user_profile(self) -> UserProfile: ...
    async def get_subscription(self) -> Subscription | None: ...
    async def get_recipe_details(self, recipe_id: str) -> RecipeDetails: ...
    async def get_recipe_images(self, recipe_id: str) -> list[RecipeImage]: ...
    async def get_custom_recipe_details(self, recipe_id: str) -> CustomRecipeDetails: ...
    async def list_managed_collections(self, page: int = 0) -> CollectionPage: ...
    async def add_managed_collection(self, collection_id: str) -> CollectionSummary: ...
    async def remove_managed_collection(self, collection_id: str) -> None: ...
    async def list_custom_collections(self, page: int = 0) -> CollectionPage: ...
    async def create_custom_collection(self, name: str) -> CollectionSummary: ...
    async def delete_custom_collection(self, collection_id: str) -> None: ...
    async def add_recipes_to_custom_collection(
        self, collection_id: str, recipe_ids: list[str]
    ) -> CollectionSummary: ...
    async def remove_recipe_from_custom_collection(
        self, collection_id: str, recipe_id: str
    ) -> None: ...
    async def get_shopping_list(self) -> ShoppingList: ...
    async def add_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int: ...
    async def remove_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None: ...
    async def add_additional_items(self, names: list[str]) -> list[ShoppingListItem]: ...
    async def remove_additional_items(self, item_ids: list[str]) -> None: ...
    async def clear_shopping_list(self) -> None: ...
    async def get_calendar_week(self, day: date) -> list[CalendarDay]: ...
    async def add_recipes_to_calendar(self, day: date, recipe_ids: list[str]) -> CalendarDay: ...
    async def remove_recipe_from_calendar(self, day: date, recipe_id: str) -> CalendarDay: ...
    async def add_custom_recipes_to_calendar(
        self, day: date, recipe_ids: list[str]
    ) -> CalendarDay: ...
    async def remove_custom_recipe_from_calendar(
        self, day: date, recipe_id: str
    ) -> CalendarDay: ...
    async def list_custom_recipes(self) -> list[CustomRecipeSummary]: ...
    async def upload_custom_recipe(self, draft: CustomRecipeDraft) -> tuple[str, str]: ...
    async def update_custom_recipe(
        self, recipe_id: str, draft: CustomRecipeDraft
    ) -> tuple[str, str]: ...
    async def set_custom_recipe_image(
        self, recipe_id: str, image_source: str
    ) -> CustomRecipeImageResult: ...
    async def delete_custom_recipe(self, recipe_id: str) -> None: ...
    async def clone_recipe_as_custom(
        self, recipe_id: str, serving_size: int
    ) -> CustomRecipeDetails: ...
    async def add_custom_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int: ...
    async def remove_custom_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None: ...
    async def set_ingredient_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]: ...
    async def set_additional_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]: ...
    async def rename_additional_items(
        self, updates: list[AdditionalItemRename]
    ) -> list[ShoppingListItem]: ...
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
    ) -> list[RecipeSearchResult]: ...
    async def suggest_recipes_from_ingredients(
        self,
        available_ingredients: list[str],
        collection_ids: list[str] | None = None,
        max_results: int = 10,
    ) -> list[RecipeSuggestion]: ...
    async def add_calendar_range_to_shopping_list(
        self, start: date, end: date
    ) -> CalendarShoppingSummary: ...
    async def rate_recipe(self, recipe_id: str, stars: int) -> None: ...
    async def set_recipe_bookmark(self, recipe_id: str, bookmarked: bool) -> None: ...
    async def set_recipe_note(self, recipe_id: str, text: str | None) -> None: ...
    async def mark_recipe_cooked(self, recipe_id: str, is_custom: bool = False) -> None: ...
    async def get_cooking_history(self, limit: int = 20) -> list[CookedRecipe]: ...
    async def get_recipe_interactions(self, recipe_id: str) -> RecipeInteractions: ...
    async def get_recipe_recommendations(
        self, recipe_id: str | None = None, limit: int = 10
    ) -> list[RecipeSearchResult]: ...
    async def list_bookmarked_recipes(self) -> list[RecipeSearchResult]: ...
    async def get_user_devices(self) -> tuple[list[str], list[str]]: ...
    async def aclose(self) -> None: ...


class CookidoughSession:
    """High-level repository for the Cookidoo platform.

    Tools should always go through this class. The underlying ``cookidoo-api``
    client and any direct-HTTP fallbacks are hidden behind a stable interface
    so the upstream dependency can be swapped without touching tool code.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http: ClientSession | None = None
        self._client: Cookidoo | None = None
        self._login_lock = asyncio.Lock()
        # Monotonic counter incremented on every successful login. A caller
        # that hits a 401 snapshots the current generation; if another
        # coroutine re-logged in meanwhile, ``_relogin`` becomes a no-op
        # instead of redundantly rotating the cookie jar.
        self._session_generation = 0
        # Latched once ``aclose`` runs. Any subsequent ``_ensure_logged_in``
        # call should fail loud rather than silently spinning up a fresh
        # session (the MCP server lifespan treats ``aclose`` as terminal).
        self._closed = False

    async def __aenter__(self) -> Self:
        await self._ensure_logged_in()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        # Holding ``_login_lock`` blocks until any in-flight login finishes
        # (so we don't tear down a half-initialized client) and prevents a
        # new login from racing the close. The latched ``_closed`` flag
        # combined with the ``http is None`` guard makes ``aclose`` itself
        # idempotent under concurrent invocations.
        async with self._login_lock:
            self._closed = True
            http = self._http
            if http is None:
                return
            try:
                await http.close()
            finally:
                self._http = None
                self._client = None

    @property
    def session_generation(self) -> int:
        """Read-only accessor for the session-generation counter.

        Used by tests and diagnostics to verify the re-login race
        protection; ``_run`` reads ``self._session_generation`` directly.
        """
        return self._session_generation

    async def _ensure_logged_in(self) -> Cookidoo:
        if self._closed:
            raise UpstreamApiError("Session is closed.")
        if self._client is not None:
            return self._client

        async with self._login_lock:
            if self._closed:
                raise UpstreamApiError("Session is closed.")
            if self._client is not None:
                return self._client

            # China is absent from the global locale catalogue; its deployment
            # is fixed to one country/language pair.
            if self._settings.is_china_market:
                localization = CHINA_LOCALIZATION
            else:
                options = await get_localization_options(country=self._settings.country_code)
                matched = _match_localization(options, self._settings.language_code)
                if matched is None:
                    raise AuthenticationError(
                        f"No Cookidoo locale matches country={self._settings.country_code!r} "
                        f"language={self._settings.language_code!r}."
                    )
                localization = matched

            # ``CookieJar(unsafe=True)`` is required because the browser
            # OAuth2 login chain crosses domains (``cookidoo.<tld>`` → CIAM
            # → login-srv → callback). aiohttp's default jar drops cookies
            # set on a different host than the request origin, which would
            # break the session before login completes.
            http = ClientSession(
                connector=TCPConnector(),
                cookie_jar=CookieJar(unsafe=True),
                timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
            )
            try:
                client = self._build_client(http, localization)
                if self._try_load_token(client):
                    # Restoring tokens sets the Bearer header but leaves the
                    # cookie jar empty, so the ``_authed_http`` endpoints 401
                    # until ``_relogin`` runs a full login. The cookidoo-api
                    # paths work right away.
                    _LOGGER.info("Restored Cookidoo session token; skipping login.")
                else:
                    await client.login()
                    self._persist_token(client)
            except CookidooAuthException as e:
                await http.close()
                raise AuthenticationError(str(e)) from e
            except CookidooException as e:
                await http.close()
                raise UpstreamApiError(str(e)) from e
            except BaseException:
                await http.close()
                raise

            self._http = http
            self._client = client
            self._session_generation += 1
            _LOGGER.info(
                "Authenticated as %s on Cookidoo (%s)",
                _redact_email(self._settings.email),
                localization.url,
            )
            return client

    async def _relogin(self, observed_generation: int) -> Cookidoo:
        """Re-run the browser OAuth2 login on the existing HTTP session.

        Drops the stale cookie jar and runs ``login()`` again. The
        generation snapshot prevents parallel callers that all observed
        the same 401 from re-logging in N times.
        """
        async with self._login_lock:
            if self._closed:
                raise UpstreamApiError("Session is closed.")
            if observed_generation != self._session_generation:
                client = self._client
                if client is None:
                    raise UpstreamApiError("Session was closed during re-login.")
                return client
            client = self._client
            http = self._http
            if client is None or http is None:
                raise UpstreamApiError("Session is not initialized.")
            http.cookie_jar.clear()
            try:
                await client.login()
            except CookidooAuthException as e:
                raise AuthenticationError(str(e)) from e
            except CookidooException as e:
                raise UpstreamApiError(str(e)) from e
            self._persist_token(client)
            self._session_generation += 1
            return client

    def _build_client(
        self, http: ClientSession, localization: CookidooLocalizationConfig
    ) -> Cookidoo:
        password = self._settings.password.get_secret_value()
        if self._settings.is_china_market:
            return ChinaCookidoo(session=http, phone_number=self._settings.email, password=password)
        return Cookidoo(
            session=http,
            cfg=CookidooConfig(
                email=self._settings.email,
                password=password,
                localization=localization,
            ),
        )

    def _try_load_token(self, client: Cookidoo) -> bool:
        """Restore the OAuth2 tokens from disk; True when a login was restored."""
        path = self._settings.token_file
        if path is None or not path.is_file():
            return False
        try:
            client.load_token(path)
        except CookidooConfigException as e:
            _LOGGER.warning("Ignoring unreadable token file %s: %s", path, e)
            return False
        return True

    def _persist_token(self, client: Cookidoo) -> None:
        path = self._settings.token_file
        if path is None:
            return
        try:
            # ``save_token`` writes in place, so narrow the mode beforehand.
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            client.save_token(path)
        except (OSError, CookidooConfigException) as e:
            # Persisting is an optimization; a read-only directory must
            # never break the login itself.
            _LOGGER.warning("Could not persist Cookidoo token to %s: %s", path, e)

    async def _run[T](self, op: Callable[[Cookidoo], Awaitable[T]]) -> T:
        client = await self._ensure_logged_in()
        observed_generation = self._session_generation

        async def _invoke(c: Cookidoo) -> T:
            try:
                return await op(c)
            except (CookidooParseException, CookidooRequestException) as e:
                raise UpstreamApiError(str(e)) from e

        try:
            return await _invoke(client)
        except CookidooAuthException:
            client = await self._relogin(observed_generation)
            try:
                return await _invoke(client)
            except CookidooAuthException as e:
                # A second auth failure after re-login means the credentials
                # are no longer accepted. Map to our domain error so callers
                # outside ``session.py`` never see raw ``cookidoo_api``
                # exceptions — that's the entire reason this facade exists.
                raise AuthenticationError(str(e)) from e

    async def get_user_profile(self) -> UserProfile:
        info = await self._run(lambda c: c.get_user_info())
        return UserProfile(
            id=info.id,
            username=info.username,
            description=info.description,
            picture=info.picture,
        )

    async def get_subscription(self) -> Subscription | None:
        sub = await self._run(lambda c: c.get_active_subscription())
        if sub is None:
            return None
        return Subscription(
            active=sub.active,
            status=sub.status,
            subscription_level=sub.subscription_level,
            subscription_source=sub.subscription_source,
            type=sub.type,
            extended_type=sub.extended_type,
            start_date=sub.start_date,
            expires=sub.expires,
        )

    async def get_recipe_details(self, recipe_id: str) -> RecipeDetails:
        try:
            details = await self._run(lambda c: c.get_recipe_details(recipe_id))
        except UpstreamApiError as e:
            # cookidoo-api lumps 404 and 5xx into the same exception. We
            # preserve the original message so the caller can tell missing
            # recipes apart from transient upstream failures.
            raise NotFoundError(f"Recipe {recipe_id!r} not available: {e}") from e
        return RecipeDetails(
            id=details.id,
            name=details.name,
            url=details.url,
            thumbnail=details.thumbnail,
            image=details.image,
            difficulty=details.difficulty,
            serving_size=details.serving_size,
            active_time_seconds=details.active_time,
            total_time_seconds=details.total_time,
            utensils=list(details.utensils),
            notes=list(details.notes),
            ingredients=[
                Ingredient(id=i.id, name=i.name, description=getattr(i, "description", None))
                for i in details.ingredients
            ],
            # ``getattr`` with empty defaults keeps pre-0.15 cookidoo-api
            # payload shapes (and slim test fakes) from sinking the request.
            categories=[
                RecipeCategory(id=c.id, name=c.name, notes=getattr(c, "notes", None) or None)
                for c in getattr(details, "categories", []) or []
            ],
            collections=[
                RecipeCollectionRef(
                    id=col.id,
                    name=col.name,
                    total_recipes=getattr(col, "total_recipes", 0) or 0,
                )
                for col in getattr(details, "collections", []) or []
            ],
            nutrition=_nutrition_to_dtos(getattr(details, "nutrition_groups", []) or []),
        )

    async def get_recipe_images(self, recipe_id: str) -> list[RecipeImage]:
        """Return every photo of a catalogue recipe (recipes carry several).

        cookidoo-api keeps only the first asset, so this reads the raw
        recipe payload through the same endpoint the library uses.
        """
        client = await self._ensure_logged_in()
        localization = client.localization
        language_path = quote(localization.language, safe="")
        url = (
            f"{_localization_origin(localization.url)}"
            f"/recipes/recipe/{language_path}/{quote(recipe_id, safe='')}"
        )
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        return _recipe_images_from_payload(payload)

    async def get_custom_recipe_details(self, recipe_id: str) -> CustomRecipeDetails:
        try:
            recipe = await self._run(lambda c: c.get_custom_recipe(recipe_id))
        except UpstreamApiError as e:
            raise NotFoundError(f"Custom recipe {recipe_id!r} not available: {e}") from e
        return CustomRecipeDetails(
            id=recipe.id,
            name=recipe.name,
            url=recipe.url,
            serving_size=recipe.serving_size,
            active_time_seconds=recipe.active_time,
            total_time_seconds=recipe.total_time,
            tools=list(recipe.tools),
            ingredients=list(recipe.ingredients),
            instructions=list(recipe.instructions),
            thumbnail=recipe.thumbnail,
            image=recipe.image,
        )

    async def list_managed_collections(self, page: int = 0) -> CollectionPage:
        # The count call rides alongside the page fetch so pagination
        # metadata is consistent on every page without extra wall-clock cost.
        collections, (total_elements, total_pages) = await asyncio.gather(
            self._run(lambda c: c.get_managed_collections(page=page)),
            self._run(lambda c: c.count_managed_collections()),
        )
        return CollectionPage(
            items=[_collection_to_dto(item) for item in collections],
            page=page,
            total_pages=total_pages,
            total_elements=total_elements,
        )

    async def add_managed_collection(self, collection_id: str) -> CollectionSummary:
        collection = await self._run(lambda c: c.add_managed_collection(collection_id))
        return _collection_to_dto(collection)

    async def remove_managed_collection(self, collection_id: str) -> None:
        await self._run(lambda c: c.remove_managed_collection(collection_id))

    async def list_custom_collections(self, page: int = 0) -> CollectionPage:
        collections, (total_elements, total_pages) = await asyncio.gather(
            self._run(lambda c: c.get_custom_collections(page=page)),
            self._run(lambda c: c.count_custom_collections()),
        )
        return CollectionPage(
            items=[_collection_to_dto(item) for item in collections],
            page=page,
            total_pages=total_pages,
            total_elements=total_elements,
        )

    async def create_custom_collection(self, name: str) -> CollectionSummary:
        collection = await self._run(lambda c: c.add_custom_collection(name))
        return _collection_to_dto(collection)

    async def delete_custom_collection(self, collection_id: str) -> None:
        await self._run(lambda c: c.remove_custom_collection(collection_id))

    async def add_recipes_to_custom_collection(
        self, collection_id: str, recipe_ids: list[str]
    ) -> CollectionSummary:
        collection = await self._run(
            lambda c: c.add_recipes_to_custom_collection(collection_id, recipe_ids)
        )
        return _collection_to_dto(collection)

    async def remove_recipe_from_custom_collection(
        self, collection_id: str, recipe_id: str
    ) -> None:
        await self._run(lambda c: c.remove_recipe_from_custom_collection(collection_id, recipe_id))

    async def get_shopping_list(self) -> ShoppingList:
        ingredients, additional, recipes = await asyncio.gather(
            self._run(lambda c: c.get_ingredient_items()),
            self._run(lambda c: c.get_additional_items()),
            self._run(lambda c: c.get_shopping_list_recipes()),
        )
        return ShoppingList(
            ingredient_items=[
                ShoppingListItem(
                    id=item.id,
                    name=item.name,
                    description=getattr(item, "description", None),
                    is_owned=item.is_owned,
                    source=ShoppingItemSource.RECIPE,
                )
                for item in ingredients
            ],
            additional_items=[
                ShoppingListItem(
                    id=item.id,
                    name=item.name,
                    description=None,
                    is_owned=item.is_owned,
                    source=ShoppingItemSource.ADDITIONAL,
                )
                for item in additional
            ],
            recipes=[
                ShoppingListRecipe(
                    id=r.id,
                    name=r.name,
                    url=r.url,
                    thumbnail=r.thumbnail,
                    image=r.image,
                    ingredients=[
                        Ingredient(
                            id=i.id, name=i.name, description=getattr(i, "description", None)
                        )
                        for i in r.ingredients
                    ],
                )
                for r in recipes
            ],
        )

    async def add_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int:
        added = await self._run(lambda c: c.add_ingredient_items_for_recipes(recipe_ids))
        return len(added)

    async def remove_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None:
        await self._run(lambda c: c.remove_ingredient_items_for_recipes(recipe_ids))

    async def add_additional_items(self, names: list[str]) -> list[ShoppingListItem]:
        items = await self._run(lambda c: c.add_additional_items(names))
        return [
            ShoppingListItem(
                id=item.id,
                name=item.name,
                description=None,
                is_owned=item.is_owned,
                source=ShoppingItemSource.ADDITIONAL,
            )
            for item in items
        ]

    async def remove_additional_items(self, item_ids: list[str]) -> None:
        await self._run(lambda c: c.remove_additional_items(item_ids))

    async def clear_shopping_list(self) -> None:
        await self._run(lambda c: c.clear_shopping_list())

    async def get_calendar_week(self, day: date) -> list[CalendarDay]:
        days = await self._run(lambda c: c.get_recipes_in_calendar_week(day))
        return [_calendar_to_dto(d) for d in days]

    async def add_recipes_to_calendar(self, day: date, recipe_ids: list[str]) -> CalendarDay:
        updated = await self._run(lambda c: c.add_recipes_to_calendar(day, recipe_ids))
        return _calendar_to_dto(updated)

    async def remove_recipe_from_calendar(self, day: date, recipe_id: str) -> CalendarDay:
        updated = await self._run(lambda c: c.remove_recipe_from_calendar(day, recipe_id))
        return _calendar_to_dto(updated)

    async def add_custom_recipes_to_calendar(self, day: date, recipe_ids: list[str]) -> CalendarDay:
        updated = await self._run(lambda c: c.add_custom_recipes_to_calendar(day, recipe_ids))
        return _calendar_to_dto(updated)

    async def remove_custom_recipe_from_calendar(self, day: date, recipe_id: str) -> CalendarDay:
        updated = await self._run(lambda c: c.remove_custom_recipe_from_calendar(day, recipe_id))
        return _calendar_to_dto(updated)

    async def add_custom_recipes_to_shopping_list(self, recipe_ids: list[str]) -> int:
        added = await self._run(lambda c: c.add_ingredient_items_for_custom_recipes(recipe_ids))
        return len(added)

    async def remove_custom_recipes_from_shopping_list(self, recipe_ids: list[str]) -> None:
        await self._run(lambda c: c.remove_ingredient_items_for_custom_recipes(recipe_ids))

    async def set_ingredient_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        # cookidoo-api accepts only the id and is_owned fields on the
        # CookidooIngredientItem dataclass; name/description are placeholders
        # the upstream ignores when patching ownership.
        from cookidoo_api.types import CookidooIngredientItem

        items = [
            CookidooIngredientItem(id=u.id, name="", is_owned=u.is_owned, description="")
            for u in updates
        ]
        result = await self._run(lambda c: c.edit_ingredient_items_ownership(items))
        return [
            ShoppingListItem(
                id=item.id,
                name=item.name,
                description=getattr(item, "description", None),
                is_owned=item.is_owned,
                source=ShoppingItemSource.RECIPE,
            )
            for item in result
        ]

    async def set_additional_items_ownership(
        self, updates: list[ShoppingItemOwnershipUpdate]
    ) -> list[ShoppingListItem]:
        from cookidoo_api.types import CookidooAdditionalItem

        items = [CookidooAdditionalItem(id=u.id, name="", is_owned=u.is_owned) for u in updates]
        result = await self._run(lambda c: c.edit_additional_items_ownership(items))
        return [
            ShoppingListItem(
                id=item.id,
                name=item.name,
                description=None,
                is_owned=item.is_owned,
                source=ShoppingItemSource.ADDITIONAL,
            )
            for item in result
        ]

    async def rename_additional_items(
        self, updates: list[AdditionalItemRename]
    ) -> list[ShoppingListItem]:
        from cookidoo_api.types import CookidooAdditionalItem

        items = [CookidooAdditionalItem(id=u.id, name=u.name, is_owned=False) for u in updates]
        result = await self._run(lambda c: c.edit_additional_items(items))
        return [
            ShoppingListItem(
                id=item.id,
                name=item.name,
                description=None,
                is_owned=item.is_owned,
                source=ShoppingItemSource.ADDITIONAL,
            )
            for item in result
        ]

    async def clone_recipe_as_custom(
        self, recipe_id: str, serving_size: int
    ) -> CustomRecipeDetails:
        # Write op: a 4xx/5xx is not "not found" — let UpstreamApiError surface unchanged.
        recipe = await self._run(lambda c: c.add_custom_recipe_from(recipe_id, serving_size))
        return CustomRecipeDetails(
            id=recipe.id,
            name=recipe.name,
            url=recipe.url,
            serving_size=recipe.serving_size,
            active_time_seconds=recipe.active_time,
            total_time_seconds=recipe.total_time,
            tools=list(recipe.tools),
            ingredients=list(recipe.ingredients),
            instructions=list(recipe.instructions),
            thumbnail=recipe.thumbnail,
            image=recipe.image,
        )

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
        limit = max(1, min(limit, 50))
        client = await self._ensure_logged_in()
        localization = client.localization
        origin = _localization_origin(localization.url)
        # The Cookidoo search API isn't exposed by cookidoo-api, but the
        # public web app hits the same /search/{language} endpoint. The
        # session cookies from client.login() authenticate it, not the
        # Bearer token.
        #
        # Encoding discipline: language goes into the path so reserved chars
        # MUST be percent-encoded (``safe=""``). The query parameters use
        # ``quote_plus`` so a literal ``+`` survives the round-trip — without
        # it, ``"A+B Sauce"`` would be forwarded as ``"A B Sauce"`` because
        # the upstream decodes ``+`` as a space.
        language_path = quote(localization.language, safe="")
        query_params: dict[str, Any] = {
            "query": query,
            "context": "recipes",
            "countries": localization.country_code,
            "limit": limit,
        }
        # Filter params mirror the Cookidoo web UI's /search query string:
        # totalTime in seconds, list values comma-joined, unset filters
        # omitted entirely.
        if max_total_minutes is not None:
            query_params["totalTime"] = max(1, max_total_minutes) * 60
        if difficulty is not None:
            query_params["difficulty"] = difficulty
        if categories:
            query_params["categories"] = ",".join(categories)
        if ingredients:
            query_params["ingredients"] = ",".join(ingredients)
        if exclude_ingredients:
            query_params["excludeIngredients"] = ",".join(exclude_ingredients)
        if min_rating is not None:
            query_params["ratings"] = int(min_rating)
        if portions is not None:
            query_params["portions"] = portions
        if thermomix_version is not None:
            query_params["tmv"] = thermomix_version
        if accessories:
            query_params["accessories"] = ",".join(accessories)
        if sort_by is not None:
            query_params["sortby"] = sort_by
        params = urlencode(query_params, quote_via=quote_plus)
        url = f"{origin}/search/{language_path}?{params}"
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [item for item in (_search_item_to_dto(raw) for raw in data) if item is not None]

    async def suggest_recipes_from_ingredients(
        self,
        available_ingredients: list[str],
        collection_ids: list[str] | None = None,
        max_results: int = 10,
    ) -> list[RecipeSuggestion]:
        # Short tokens explode the substring matcher (``"oil"`` → ``"soil"``,
        # ``"egg"`` → ``"eggplant"``). Drop them at the boundary instead of
        # silently inflating every recipe's score. Check this BEFORE walking
        # collections so a useless ingredient list never triggers HTTP work.
        available_lower = {
            ing.lower().strip()
            for ing in available_ingredients
            if len(ing.strip()) >= SUGGEST_MIN_INGREDIENT_LENGTH
        }
        if not available_lower:
            return []

        if collection_ids is None:
            recipe_ids = await self._search_candidate_recipe_ids(available_lower, max_results)
            if not recipe_ids:
                # The ingredients filter is an undocumented search feature;
                # if it yields nothing, fall back to the user's own
                # collections so the tool never regresses below the old
                # behaviour.
                recipe_ids = await self._collect_recipe_ids(None)
        else:
            recipe_ids = await self._collect_recipe_ids(collection_ids)
        if not recipe_ids:
            return []

        semaphore = asyncio.Semaphore(SUGGEST_RECIPE_FETCH_CONCURRENCY)

        async def _fetch(rid: str) -> RecipeDetails | None:
            async with semaphore:
                try:
                    return await self.get_recipe_details(rid)
                except NotFoundError as e:
                    # Custom-recipe IDs that slipped in via a custom collection
                    # 404 here. That's expected; everything else (auth,
                    # network) propagates so the caller sees the real problem.
                    _LOGGER.debug("Skipping recipe %s during suggestion: %s", rid, e)
                    return None

        fetched = await asyncio.gather(*(_fetch(rid) for rid in recipe_ids))

        suggestions: list[RecipeSuggestion] = []
        for details in fetched:
            if details is None:
                continue
            ingredient_names = [i.name.lower().strip() for i in details.ingredients]
            matching = [
                name
                for name in ingredient_names
                if any(avail in name or name in avail for avail in available_lower)
            ]
            if not matching:
                continue
            missing = [name for name in ingredient_names if name not in matching]
            total = max(len(ingredient_names), 1)
            suggestions.append(
                RecipeSuggestion(
                    recipe=details,
                    score=round(len(matching) / total, 2),
                    matching_ingredients=matching,
                    missing_ingredients=missing,
                    total_ingredients=len(ingredient_names),
                )
            )

        suggestions.sort(key=lambda s: s.score, reverse=True)
        return suggestions[:max_results]

    async def _search_candidate_recipe_ids(
        self, available_lower: set[str], max_results: int
    ) -> list[str]:
        candidate_limit = min(max(max_results * SUGGEST_SEARCH_CANDIDATE_FACTOR, 10), 50)
        sorted_ingredients = sorted(available_lower)
        results = await self.search_recipes(
            query=" ".join(sorted_ingredients),
            limit=candidate_limit,
            ingredients=sorted_ingredients,
        )
        return [r.id for r in results]

    async def add_calendar_range_to_shopping_list(
        self, start: date, end: date
    ) -> CalendarShoppingSummary:
        # ``get_calendar_week`` returns whole weeks; probing every 7 days
        # (plus the end date, whose week the stride can overshoot) covers the
        # range with the minimum number of calls. Days are deduplicated by id
        # because adjacent probes may resolve to the same week.
        probe_days = [start]
        cursor = start + timedelta(days=7)
        while cursor <= end:
            probe_days.append(cursor)
            cursor += timedelta(days=7)
        if probe_days[-1] != end:
            probe_days.append(end)
        weeks = await asyncio.gather(*(self.get_calendar_week(d) for d in probe_days))

        seen_days: dict[str, CalendarDay] = {}
        for week in weeks:
            for calendar_day in week:
                seen_days.setdefault(calendar_day.id, calendar_day)

        recipe_ids: dict[str, None] = {}
        custom_ids: dict[str, None] = {}
        for calendar_day in seen_days.values():
            day_date = _coerce_iso_date(calendar_day.id)
            if day_date is None:
                # Upstream day ids are ISO dates today. If that ever changes
                # we must not blindly push recipes outside the requested
                # range onto the list — this is a write operation.
                _LOGGER.debug("Skipping calendar day with non-date id %r", calendar_day.id)
                continue
            if not start <= day_date <= end:
                continue
            for recipe in calendar_day.recipes:
                recipe_ids.setdefault(recipe.id, None)
            for custom_id in calendar_day.custom_recipe_ids:
                custom_ids.setdefault(custom_id, None)

        item_count = 0
        if recipe_ids:
            item_count += await self.add_recipes_to_shopping_list(list(recipe_ids))
        if custom_ids:
            item_count += await self.add_custom_recipes_to_shopping_list(list(custom_ids))
        return CalendarShoppingSummary(
            recipe_ids=list(recipe_ids),
            custom_recipe_ids=list(custom_ids),
            item_count=item_count,
        )

    async def _collect_recipe_ids(self, collection_ids: list[str] | None) -> list[str]:
        """Resolve a list of collection IDs (or every collection) to recipe IDs.

        Walks every page of both the managed and custom collection endpoints.
        Hard-coding ``page=0`` here would silently drop recipes that live on a
        second page for any reasonably-sized library.
        """
        managed, custom = await asyncio.gather(
            self._drain_managed_collections(),
            self._drain_custom_collections(),
        )
        collections: list[Any] = [*managed, *custom]
        if collection_ids is not None:
            wanted = set(collection_ids)
            collections = [c for c in collections if getattr(c, "id", None) in wanted]

        seen: dict[str, None] = {}
        for collection in collections:
            for chapter in getattr(collection, "chapters", []) or []:
                for recipe in getattr(chapter, "recipes", []) or []:
                    rid = getattr(recipe, "id", None)
                    if isinstance(rid, str) and rid and rid not in seen:
                        seen[rid] = None
        return list(seen)

    async def _drain_managed_collections(self) -> list[Any]:
        _, pages = await self._run(lambda c: c.count_managed_collections())
        if pages <= 0:
            return []

        async def _page(page_index: int) -> list[Any]:
            return await self._run(lambda c: c.get_managed_collections(page=page_index))

        page_results = await asyncio.gather(*(_page(i) for i in range(pages)))
        return [item for page in page_results for item in page]

    async def _drain_custom_collections(self) -> list[Any]:
        _, pages = await self._run(lambda c: c.count_custom_collections())
        if pages <= 0:
            return []

        async def _page(page_index: int) -> list[Any]:
            return await self._run(lambda c: c.get_custom_collections(page=page_index))

        page_results = await asyncio.gather(*(_page(i) for i in range(pages)))
        return [item for page in page_results for item in page]

    async def list_custom_recipes(self) -> list[CustomRecipeSummary]:
        url = await self._custom_recipes_url()
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        if not isinstance(payload, dict):
            return []
        items = payload.get("items", [])
        if not isinstance(items, list):
            return []
        result: list[CustomRecipeSummary] = []
        for item in items:
            dto = _custom_recipe_item_to_dto(item)
            if dto is not None:
                result.append(dto)
        return result

    async def upload_custom_recipe(self, draft: CustomRecipeDraft) -> tuple[str, str]:
        _LOGGER.info(
            "upload_custom_recipe: creating stub (name=%r, %d ingredients, %d steps)",
            draft.name,
            len(draft.ingredients),
            len(draft.steps),
        )
        try:
            recipe_id = await asyncio.wait_for(
                self._create_empty_custom_recipe(draft.name),
                timeout=CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS,
            )
        except TimeoutError as e:
            # Cancellation may have raced the POST: a stub *might* have been
            # created on Cookidoo's side without our seeing the ID. We cannot
            # roll it back. The user can clean up manually via the Cookidoo UI.
            raise UpstreamApiError(
                f"Custom recipe creation timed out after "
                f"{CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS:.0f} s; "
                f"check the Cookidoo UI for an orphaned draft named {draft.name!r}."
            ) from e

        _LOGGER.info("upload_custom_recipe: stub %s created; PATCHing content", recipe_id)
        try:
            await asyncio.sleep(CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS)
            await asyncio.wait_for(
                self._patch_custom_recipe(recipe_id, draft),
                timeout=CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS,
            )
        except TimeoutError as e:
            await self._rollback_custom_recipe(recipe_id)
            raise UpstreamApiError(
                f"Custom recipe content upload timed out after "
                f"{CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS:.0f} s; stub rolled back."
            ) from e
        except BaseException:
            # Best-effort rollback that itself must not stall — a hanging
            # rollback during cancellation would defeat the upper-bound
            # ``wait_for`` above. Half the operation budget is generous for
            # what is just one DELETE.
            try:
                await asyncio.wait_for(
                    self._rollback_custom_recipe(recipe_id),
                    timeout=CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS / 2,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "Rollback of custom recipe stub %s timed out; "
                    "the draft may be orphaned in Cookidoo.",
                    recipe_id,
                )
            raise
        _LOGGER.info("upload_custom_recipe: %s uploaded successfully", recipe_id)
        public_url = await self._custom_recipe_public_url(recipe_id)
        return recipe_id, public_url

    async def update_custom_recipe(
        self, recipe_id: str, draft: CustomRecipeDraft
    ) -> tuple[str, str]:
        """Replace an existing custom recipe's content (full overwrite).

        Reuses the same PATCH the create flow runs after its stub POST. No
        rollback: the recipe already exists, so a failed PATCH leaves the
        previous server state instead of orphaning a stub.
        """
        _LOGGER.info(
            "update_custom_recipe: PATCHing %s (name=%r, %d ingredients, %d steps)",
            recipe_id,
            draft.name,
            len(draft.ingredients),
            len(draft.steps),
        )
        try:
            await asyncio.wait_for(
                self._patch_custom_recipe(recipe_id, draft),
                timeout=CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS,
            )
        except TimeoutError as e:
            raise UpstreamApiError(
                f"Custom recipe update timed out after "
                f"{CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS:.0f} s; the recipe "
                f"{recipe_id!r} may be partially updated — verify it in the Cookidoo UI."
            ) from e
        public_url = await self._custom_recipe_public_url(recipe_id)
        return recipe_id, public_url

    async def set_custom_recipe_image(
        self, recipe_id: str, image_source: str
    ) -> CustomRecipeImageResult:
        """Upload a photo for a custom recipe (path or http(s) URL).

        Global flow verified live (2026-06-05): Cookidoo signs the upload
        params, the file goes directly to Vorwerk's Cloudinary tenant, and the
        returned public id is PATCHed onto the recipe. Each upload yields a
        fresh asset — Cookidoo rejects reusing an asset across recipes. China
        instead uploads to Tencent COS and waits for moderation.
        """
        image_bytes, content_type = await self._load_image_bytes(image_source)
        client = await self._ensure_logged_in()
        if isinstance(client, ChinaCookidoo):
            body = await self._china_image_body(client, image_bytes, content_type)
        else:
            body = await self._cloudinary_image_body(image_bytes, content_type)
        url = f"{await self._custom_recipes_url()}/{quote(recipe_id, safe='')}"
        async with self._authed_http("PATCH", url, json_body=body) as response:
            await response.read()
        details = await self.get_custom_recipe_details(recipe_id)
        if isinstance(client, ChinaCookidoo) and str(body["image"]) not in (details.image or ""):
            raise UpstreamApiError("China Cookidoo did not persist the uploaded recipe image.")
        return CustomRecipeImageResult(
            recipe_id=recipe_id,
            image=details.image,
            thumbnail=details.thumbnail,
            url=details.url,
        )

    async def _cloudinary_image_body(self, image_bytes: bytes, content_type: str) -> dict[str, Any]:
        timestamp = int(time.time())
        signature = await self._request_image_signature(timestamp)
        public_id, image_format = await _upload_image_to_cloudinary(
            image_bytes, content_type, timestamp, signature
        )
        return {"image": f"{public_id}.{image_format}", "isImageOwnedByUser": True}

    async def _china_image_body(
        self, client: ChinaCookidoo, image_bytes: bytes, content_type: str
    ) -> dict[str, Any]:
        image_id = await client.upload_recipe_image(image_bytes, content_type)
        return {
            "image": image_id,
            "isImageOwnedByUser": True,
            "isImageCopyrightOwned": True,
        }

    async def _request_image_signature(self, timestamp: int) -> str:
        client = await self._ensure_logged_in()
        localization = client.localization
        language_path = quote(localization.language, safe="")
        url = (
            f"{_localization_origin(localization.url)}"
            f"/created-recipes/{language_path}/image/signature"
        )
        body = {
            "timestamp": timestamp,
            "upload_preset": CLOUDINARY_UPLOAD_PRESET,
            "source": CLOUDINARY_UPLOAD_SOURCE,
        }
        async with self._authed_http("POST", url, json_body=body) as response:
            payload = await _parse_json(response)
        signature = payload.get("signature") if isinstance(payload, dict) else None
        if not isinstance(signature, str) or not signature:
            raise UpstreamApiError("Cookidoo did not return an image upload signature.")
        return signature

    async def _load_image_bytes(self, image_source: str) -> tuple[bytes, str]:
        scheme = urlsplit(image_source).scheme.lower()
        if scheme in _ALLOWED_URL_SCHEMES:
            image_bytes = await _fetch_image_url(image_source)
        elif scheme in ("", "file"):
            image_bytes = await asyncio.to_thread(
                _read_image_file, image_source.removeprefix("file://")
            )
        else:
            raise ValueError(f"Unsupported image source scheme: {scheme!r}")
        content_type = _sniff_image_type(image_bytes)
        if content_type is None:
            raise ValueError("Image must be JPEG or PNG (content sniffing failed).")
        return image_bytes, content_type

    async def delete_custom_recipe(self, recipe_id: str) -> None:
        await self._run(lambda c: c.remove_custom_recipe(recipe_id))

    async def _create_empty_custom_recipe(self, name: str) -> str:
        url = await self._custom_recipes_url()
        async with self._authed_http("POST", url, json_body={"recipeName": name}) as response:
            data = await _parse_json(response)
        recipe_id = data.get("recipeId") if isinstance(data, dict) else None
        if not isinstance(recipe_id, str) or not recipe_id:
            raise UpstreamApiError("Cookidoo did not return a usable recipeId.")
        return recipe_id

    async def _patch_custom_recipe(self, recipe_id: str, draft: CustomRecipeDraft) -> None:
        url = f"{await self._custom_recipes_url()}/{recipe_id}"
        payload = _draft_to_payload(draft)
        if self._settings.is_china_market:
            payload = ChinaCookidoo.update_payload(payload)
        async with self._authed_http("PATCH", url, json_body=payload) as response:
            # Drain the body so the connection can be safely returned to the
            # keep-alive pool. The PATCH response itself is not consumed.
            await response.read()

    async def _rollback_custom_recipe(self, recipe_id: str) -> None:
        _LOGGER.warning("Rolling back custom recipe stub %s after failure", recipe_id)
        # Best-effort cleanup: a zombie draft is recoverable manually, but the
        # original failure must still be surfaced to the caller.
        with suppress(CookidooException, UpstreamApiError, AuthenticationError):
            await self.delete_custom_recipe(recipe_id)

    # Recipe interactions: undocumented Cookidoo endpoints, not exposed by
    # cookidoo-api. Methods and payload shapes verified against the live API
    # (2026-06-05); parsers stay tolerant so an upstream change degrades a
    # single value instead of the whole call.

    async def rate_recipe(self, recipe_id: str, stars: int) -> None:
        stars = max(1, min(stars, 5))
        url = await self._rating_url(f"user-ratings/recipes/{quote(recipe_id, safe='')}")
        async with self._authed_http("PUT", url, json_body={"rating": stars}) as response:
            await response.read()

    async def set_recipe_bookmark(self, recipe_id: str, bookmarked: bool) -> None:
        url = await self._organize_url("api/bookmark")
        method = "PUT" if bookmarked else "DELETE"
        async with self._authed_http(method, url, json_body={"recipeId": recipe_id}) as response:
            await response.read()

    async def set_recipe_note(self, recipe_id: str, text: str | None) -> None:
        item_url = await self._recipe_notes_url(f"recipes/{quote(recipe_id, safe='')}")
        if text is None or not text.strip():
            # Idempotent delete: a 404 means there was no note to begin with.
            with suppress(NotFoundError):
                async with self._authed_http("DELETE", item_url) as response:
                    await response.read()
            return
        try:
            async with self._authed_http("PUT", item_url, json_body={"text": text}) as response:
                await response.read()
        except NotFoundError:
            create_url = await self._recipe_notes_url("recipes")
            body = {"recipeId": recipe_id, "text": text}
            async with self._authed_http("POST", create_url, json_body=body) as response:
                await response.read()

    async def mark_recipe_cooked(self, recipe_id: str, is_custom: bool = False) -> None:
        url = await self._organize_url("api/cooking-history")
        # Upstream validates recipeType against ^(VorwerkRecipe|CreatedRecipe)$.
        recipe_type = "CreatedRecipe" if is_custom else "VorwerkRecipe"
        body = {"recipeId": recipe_id, "recipeType": recipe_type}
        async with self._authed_http("POST", url, json_body=body) as response:
            await response.read()

    async def get_cooking_history(self, limit: int = 20) -> list[CookedRecipe]:
        limit = max(1, min(limit, 100))
        url = await self._organize_url("api/cooking-history")
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        return _cooking_history_from_payload(payload)[:limit]

    async def get_recipe_interactions(self, recipe_id: str) -> RecipeInteractions:
        encoded_id = quote(recipe_id, safe="")
        own_url = await self._rating_url(f"user-ratings/recipes/{encoded_id}")
        community_url = await self._rating_url(f"aggregated-ratings/recipes/{encoded_id}")
        note_url = await self._recipe_notes_url(f"recipes/{encoded_id}")
        own, community, note = await asyncio.gather(
            self._quiet_json("GET", own_url),
            self._quiet_json("GET", community_url),
            self._quiet_json("GET", note_url),
        )
        average, count = _aggregated_rating_from_payload(community)
        return RecipeInteractions(
            recipe_id=recipe_id,
            own_rating=_own_rating_from_payload(own),
            average_rating=average,
            number_of_ratings=count,
            note=_note_text_from_payload(note),
        )

    async def get_recipe_recommendations(
        self, recipe_id: str | None = None, limit: int = 10
    ) -> list[RecipeSearchResult]:
        limit = max(1, min(limit, 50))
        client = await self._ensure_logged_in()
        localization = client.localization
        origin = _localization_origin(localization.url)
        if recipe_id is None:
            language_path = quote(localization.language, safe="")
            url = f"{origin}/recommender/web/{language_path}/foryou"
        else:
            # The similar-recipes endpoint has no language segment.
            url = f"{origin}/recommender/mobile/simrec/{quote(recipe_id, safe='')}"
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        items = _recommendation_items_from_payload(payload)
        return items[:limit]

    async def list_bookmarked_recipes(self) -> list[RecipeSearchResult]:
        # The my-recipes page only serves HTML; this endpoint returns JSON.
        url = await self._organize_url("api/bookmark")
        async with self._authed_http("GET", url) as response:
            payload = await _parse_json(response)
        return _recommendation_items_from_payload(payload)

    async def get_user_devices(self) -> tuple[list[str], list[str]]:
        """Return ``(devices, accessories)`` linked to the account.

        Both lists degrade to empty on upstream failure — device info is
        profile garnish, not worth failing the whole profile call over.
        """
        client = await self._ensure_logged_in()
        origin = _localization_origin(client.localization.url)
        devices_payload, accessories_payload = await asyncio.gather(
            self._quiet_json("GET", f"{origin}/customer-devices/api/my-devices/versions"),
            self._quiet_json("GET", f"{origin}/customer-devices/api/accessory/ids"),
        )
        return (
            _device_names_from_payload(devices_payload),
            _device_names_from_payload(accessories_payload),
        )

    async def _quiet_json(self, method: str, url: str) -> Any:
        """Fetch JSON, returning ``None`` instead of raising on 404/5xx.

        Used where a missing sub-resource (no rating yet, no note yet) is a
        normal answer, and where one failing endpoint must not sink a
        gathered multi-endpoint read. Auth failures still propagate.
        """
        try:
            async with self._authed_http(method, url) as response:
                return await _parse_json(response)
        except (NotFoundError, UpstreamApiError) as e:
            _LOGGER.debug("Ignoring failed %s %s: %s", method, url, e)
            return None

    async def _organize_url(self, suffix: str) -> str:
        client = await self._ensure_logged_in()
        localization = client.localization
        language_path = quote(localization.language, safe="")
        return f"{_localization_origin(localization.url)}/organize/{language_path}/{suffix}"

    async def _rating_url(self, suffix: str) -> str:
        client = await self._ensure_logged_in()
        localization = client.localization
        language_path = quote(localization.language, safe="")
        return f"{_localization_origin(localization.url)}/rating/{language_path}/{suffix}"

    async def _recipe_notes_url(self, suffix: str) -> str:
        client = await self._ensure_logged_in()
        localization = client.localization
        language_path = quote(localization.language, safe="")
        return f"{_localization_origin(localization.url)}/recipe-notes/{language_path}/{suffix}"

    async def _custom_recipes_url(self) -> str:
        # Both URL helpers need ``localization`` from the upstream client.
        # That field only exists after a successful login, so we trigger one
        # here. Without this, callers that start with a custom-recipe op
        # (e.g. ``upload_custom_recipe`` right after ``import_web_recipe``,
        # no prior session-touching tool call in between) used to fail with
        # ``UpstreamApiError("Session is not logged in.")``.
        client = await self._ensure_logged_in()
        localization = client.localization
        return f"{_localization_origin(localization.url)}/created-recipes/{localization.language}"

    async def _custom_recipe_public_url(self, recipe_id: str) -> str:
        client = await self._ensure_logged_in()
        localization = client.localization
        return f"{_localization_origin(localization.url)}/recipes/custom-recipes/{recipe_id}"

    @asynccontextmanager
    async def _authed_http(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[ClientResponse]:
        await self._ensure_logged_in()
        http = self._http
        if http is None:
            raise UpstreamApiError("HTTP session is not initialized.")

        async def _send() -> ClientResponse:
            # Auth rides on the cookie jar populated by ``Cookidoo.login()``;
            # no ``Authorization`` header is needed (or accepted) by the
            # OAuth2-proxy-fronted ``cookidoo.<tld>`` endpoints.
            headers = {"Accept": "application/json"}
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if self._settings.is_china_market:
                headers["X-Requested-With"] = "xmlhttprequest"
            return await http.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
            )

        observed_generation = self._session_generation
        response = await _safe_send(_send, method, url)
        retried_after_relogin = False
        if response.status == 401:
            response.release()
            await self._relogin(observed_generation)
            response = await _safe_send(_send, method, url)
            retried_after_relogin = True

        try:
            if response.status == 401 and retried_after_relogin:
                raise AuthenticationError(f"Cookidoo {method} {url} still 401 after re-login.")
            if response.status == 404:
                # Surface 404 as NotFoundError so the tool layer can treat a
                # missing resource consistently with the cookidoo-api-backed
                # paths (see ``get_recipe_details`` / ``get_custom_recipe_details``).
                body = await response.text()
                raise NotFoundError(
                    f"Cookidoo {method} {url} returned 404: {_redact_error_body(body)}"
                )
            if response.status >= 400:
                body = await response.text()
                raise UpstreamApiError(
                    f"Cookidoo {method} {url} returned {response.status}: "
                    f"{_redact_error_body(body)}"
                )
            yield response
        finally:
            response.release()


def _match_localization(
    options: list[CookidooLocalizationConfig], wanted: str
) -> CookidooLocalizationConfig | None:
    """Find the localization for ``wanted``, tolerating non-canonical tags.

    Cookidoo does not publish every market under a ``lang-REGION`` tag: Poland
    and Czechia use a bare ``pl``/``cs``, and Chinese carries a script subtag
    (``zh-Hans``). Matching the canonical tag that `Settings.language_code`
    builds would leave those markets unreachable, so fall back to the primary
    subtag.
    """
    exact = wanted.casefold()
    primary = exact.partition("-")[0]
    for option in options:
        if option.language.casefold() == exact:
            return option
    for option in options:
        if option.language.casefold().partition("-")[0] == primary:
            return option
    return None


def _read_image_file(raw_path: str) -> bytes:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"Image file not found: {raw_path!r}")
    if path.stat().st_size > MAX_RECIPE_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_RECIPE_IMAGE_BYTES // (1024 * 1024)} MB.")
    return path.read_bytes()


def _sniff_image_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


async def _fetch_image_url(url: str) -> bytes:
    """Download an image over plain HTTP with a hard size cap.

    Streams into the cap so a hostile or mislinked URL cannot exhaust
    memory before the limit check.
    """
    try:
        async with (
            ClientSession(timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as plain,
            plain.get(url) as response,
        ):
            if response.status >= 400:
                raise UpstreamApiError(f"Image download failed with HTTP {response.status}.")
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > MAX_RECIPE_IMAGE_BYTES:
                    raise ValueError(f"Image exceeds {MAX_RECIPE_IMAGE_BYTES // (1024 * 1024)} MB.")
                chunks.append(chunk)
            return b"".join(chunks)
    except ClientError as e:
        raise UpstreamApiError(f"Image download failed: {e}") from e


async def _upload_image_to_cloudinary(
    image_bytes: bytes, content_type: str, timestamp: int, signature: str
) -> tuple[str, str]:
    """Upload the image to Vorwerk's Cloudinary tenant; return (public_id, format).

    Runs on a dedicated plain session — the Cookidoo cookie jar must never
    reach a third-party host.
    """
    form = FormData()
    form.add_field("upload_preset", CLOUDINARY_UPLOAD_PRESET)
    form.add_field("source", CLOUDINARY_UPLOAD_SOURCE)
    form.add_field("timestamp", str(timestamp))
    form.add_field("signature", signature)
    form.add_field("api_key", CLOUDINARY_API_KEY)
    extension = "png" if content_type == "image/png" else "jpg"
    form.add_field("file", image_bytes, filename=f"upload.{extension}", content_type=content_type)
    try:
        async with (
            ClientSession(timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS)) as plain,
            plain.post(CLOUDINARY_UPLOAD_URL, data=form) as response,
        ):
            raw = await response.text()
            if response.status >= 400:
                raise UpstreamApiError(
                    f"Image upload failed with HTTP {response.status}: {_redact_error_body(raw)}"
                )
    except ClientError as e:
        raise UpstreamApiError(f"Image upload failed: {e}") from e
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise UpstreamApiError("Image upload returned a non-JSON payload.") from e
    public_id = payload.get("public_id") if isinstance(payload, dict) else None
    image_format = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(public_id, str) or not public_id:
        raise UpstreamApiError("Image upload did not return a usable public_id.")
    if not isinstance(image_format, str) or not image_format:
        raise UpstreamApiError("Image upload did not return the stored format.")
    return public_id, image_format


async def _safe_send(
    sender: Callable[[], Awaitable[ClientResponse]], method: str, url: str
) -> ClientResponse:
    try:
        return await sender()
    except ClientError as e:
        raise UpstreamApiError(f"Cookidoo {method} {url} failed: {e}") from e


async def _parse_json(response: ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except (ClientError, ValueError) as e:
        raise UpstreamApiError(f"Cookidoo returned non-JSON payload: {e}") from e


def _coerce_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _nutrition_to_dtos(nutrition_groups: Any) -> list[NutritionInfo]:
    """Flatten Cookidoo's nested nutrition groups into one entry per quantity.

    Upstream shape: ``nutrition_groups[].recipe_nutritions[].nutritions[]``.
    Each ``recipe_nutritions`` row carries the serving quantity its values
    apply to, so one ``NutritionInfo`` per row keeps that context without
    forcing the LLM through three levels of nesting.
    """
    result: list[NutritionInfo] = []
    for group in nutrition_groups:
        for row in getattr(group, "recipe_nutritions", []) or []:
            values = [
                NutritionValue(
                    type=n.type,
                    value=float(n.number),
                    unit=getattr(n, "unittype", "") or "",
                )
                for n in getattr(row, "nutritions", []) or []
            ]
            result.append(
                NutritionInfo(
                    group=getattr(group, "name", "") or "",
                    quantity=getattr(row, "quantity", None),
                    unit_notation=getattr(row, "unit_notation", None),
                    values=values,
                )
            )
    return result


def _collection_to_dto(collection: Any) -> CollectionSummary:
    chapters = getattr(collection, "chapters", []) or []
    recipe_count = sum(len(getattr(c, "recipes", []) or []) for c in chapters)
    return CollectionSummary(
        id=collection.id,
        name=collection.name,
        description=getattr(collection, "description", None),
        chapter_count=len(chapters),
        recipe_count=recipe_count,
    )


def _calendar_to_dto(day: Any) -> CalendarDay:
    return CalendarDay(
        id=day.id,
        title=day.title,
        recipes=[
            CalendarRecipe(
                id=r.id,
                name=r.name,
                total_time_seconds=r.total_time,
                url=r.url,
                thumbnail=r.thumbnail,
                image=r.image,
            )
            for r in day.recipes
        ],
        # Upstream calls these "customer_recipe_ids" (Cookidoo's own typo for
        # "custom"). We expose them under the corrected name in our DTO and
        # also accept the corrected upstream name in case Cookidoo ever fixes
        # the typo on their side without an announcement.
        custom_recipe_ids=list(
            getattr(day, "customer_recipe_ids", None)
            or getattr(day, "custom_recipe_ids", None)
            or []
        ),
    )


_ISO_DURATION_PATTERN = re.compile(r"^PT(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _parse_duration_seconds(value: Any) -> int | None:
    """Coerce Cookidoo's time fields into total seconds.

    Cookidoo's `/created-recipes` listing returns `totalTime`/`prepTime`
    as ISO-8601 duration strings (`"PT35M"`) since approx. 2026-05.
    Older traffic — and our own integration tests — still use raw integer
    seconds, so accept both.

    Only the cooking-relevant subset of ISO-8601 durations is supported:
    ``PT[<H>H][<M>M][<S>S]`` with at least one component. Day-spanning
    durations (``P1DT2H``) and unparseable strings return ``None`` so a
    bad ``totalTime`` cannot crash the whole listing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _ISO_DURATION_PATTERN.match(value)
        if match is None:
            return None
        hours, minutes, seconds = match.groups()
        return (
            (int(hours) if hours else 0) * 3600
            + (int(minutes) if minutes else 0) * 60
            + (int(seconds) if seconds else 0)
        )
    return None


_IMAGE_TRANSFORMATION_PLACEHOLDER = "{transformation}"
_IMAGE_TRANSFORMATION_DEFAULT = "t_web_shared_recipe_221x240"
_IMAGE_TRANSFORMATION_FULL = "t_web_rdp_recipe_584x480_1_5x"


def _resolved_image_url(
    url: Any, transformation: str = _IMAGE_TRANSFORMATION_DEFAULT
) -> str | None:
    """Resolve the CDN ``{transformation}`` placeholder; None for non-URLs."""
    if not isinstance(url, str) or not url:
        return None
    return url.replace(_IMAGE_TRANSFORMATION_PLACEHOLDER, transformation)


def _recipe_images_from_payload(payload: Any) -> list[RecipeImage]:
    """Map ``descriptiveAssets`` to DTOs, resolving the CDN placeholder.

    Each asset carries square/portrait/landscape variants of one photo;
    non-image entries (e.g. videos) are skipped.
    """
    if not isinstance(payload, dict):
        return []
    assets = payload.get("descriptiveAssets")
    if not isinstance(assets, list):
        return []
    images: list[RecipeImage] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_type = asset.get("type")
        if isinstance(asset_type, str) and asset_type != "image":
            continue
        variants: dict[str, str | None] = {}
        for variant in ("square", "portrait", "landscape"):
            variants[variant] = _resolved_image_url(asset.get(variant), _IMAGE_TRANSFORMATION_FULL)
        if any(variants.values()):
            images.append(RecipeImage(**variants))
    return images


def _search_item_to_dto(item: Any) -> RecipeSearchResult | None:
    if not isinstance(item, dict):
        return None
    recipe_id = item.get("id")
    if not isinstance(recipe_id, str) or not recipe_id:
        return None
    name = item.get("title")
    if not isinstance(name, str) or not name:
        # A search hit with no usable title would surface as a blank entry
        # to the LLM. Dropping it is safer than passing an empty string up.
        return None
    return RecipeSearchResult(
        id=recipe_id,
        name=name,
        rating=_coerce_number(item.get("rating")),
        number_of_ratings=_coerce_int(item.get("numberOfRatings")),
        total_time_seconds=_parse_duration_seconds(item.get("totalTime")),
        image=_resolved_image_url(item.get("image")),
    )


def _coerce_number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` if it isn't a real number.

    ``bool`` is excluded explicitly because ``isinstance(True, int)`` is true
    in Python, and we don't want ``True``/``False`` to silently become ``1.0``
    / ``0.0`` if the upstream ever reuses a numeric field for a flag.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as an int. Accepts ``int`` AND ``float`` (some JSON
    producers serialize integer counts as ``42.0``); rejects ``bool``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _first_number(payload: Any, keys: tuple[str, ...]) -> float | None:
    """Pull the first numeric value found under any of ``keys``.

    The interaction endpoints are undocumented; key candidates cover the
    naming conventions observed across Cookidoo's other APIs. One level of
    nesting under a same-named wrapper (``{"rating": {"value": 4}}``) is
    unwrapped before giving up.
    """
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload:
            value = payload[key]
            number = _coerce_number(value)
            if number is not None:
                return number
            if isinstance(value, dict):
                nested = _first_number(value, keys)
                if nested is not None:
                    return nested
    return None


def _own_rating_from_payload(payload: Any) -> int | None:
    number = _first_number(payload, ("rating", "value", "userRating", "stars"))
    if number is None:
        return None
    rating = int(number)
    return rating if 1 <= rating <= 5 else None


def _aggregated_rating_from_payload(payload: Any) -> tuple[float | None, int | None]:
    # Live shape (2026-06): {"aggregatedRating": 4.81, "numberOfRatings": 6139, ...}
    average = _first_number(
        payload,
        ("aggregatedRating", "average", "averageRating", "rating", "ratingAverage", "value"),
    )
    count_number = _first_number(
        payload, ("count", "numberOfRatings", "totalRatings", "ratingCount")
    )
    count = int(count_number) if count_number is not None else None
    return average, count


def _note_text_from_payload(payload: Any) -> str | None:
    if isinstance(payload, list):
        for entry in payload:
            text = _note_text_from_payload(entry)
            if text is not None:
                return text
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("text", "note", "content", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = _note_text_from_payload(value)
            if nested is not None:
                return nested
    notes = payload.get("notes")
    if isinstance(notes, list):
        return _note_text_from_payload(notes)
    return None


def _device_names_from_payload(payload: Any) -> list[str]:
    """Extract human-usable device/accessory identifiers from an
    undocumented customer-devices payload (strings, or dicts with one of
    the usual name-ish keys)."""
    if isinstance(payload, dict):
        for key in ("data", "items", "devices", "accessories", "versions", "ids"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    names: list[str] = []
    for raw in payload:
        if isinstance(raw, str) and raw:
            names.append(raw)
        elif isinstance(raw, dict):
            for key in ("name", "model", "version", "deviceType", "id"):
                value = raw.get(key)
                if isinstance(value, str) and value:
                    names.append(value)
                    break
    return names


def _recommendation_items_from_payload(payload: Any) -> list[RecipeSearchResult]:
    """Map a recommender/bookmark payload to search-result DTOs.

    Live shapes (2026-06):

    - foryou: ``{"stripes": [{"recipes": [{id, title, averageRating,
      numRating, totalTime, descriptiveAssets: [{square}]}]}]}``
    - simrec: bare list of ``{id, title, averageRating, numRating,
      totalTime, imageSquare}``
    - bookmarks: ``{"bookmarks": [{"recipe": {id, title, totalTime, ...}}]}``
    """
    items: list[Any] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("stripes"), list):
            for stripe in payload["stripes"]:
                if isinstance(stripe, dict) and isinstance(stripe.get("recipes"), list):
                    items.extend(stripe["recipes"])
        elif isinstance(payload.get("bookmarks"), list):
            items = [
                entry.get("recipe") for entry in payload["bookmarks"] if isinstance(entry, dict)
            ]
        else:
            for key in ("data", "items", "recipes", "results"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
    elif isinstance(payload, list):
        items = payload
    results: list[RecipeSearchResult] = []
    for raw in items:
        item = _feed_item_to_dto(raw)
        if item is not None:
            results.append(item)
    return results


def _cooking_history_from_payload(payload: Any) -> list[CookedRecipe]:
    """Map the cooking-history payload (``entries[].recipe`` + timestamp)."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    history: list[CookedRecipe] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        recipe = _feed_item_to_dto(entry.get("recipe"))
        if recipe is None:
            continue
        details = entry.get("details")
        cooked_at = details.get("timestamp") if isinstance(details, dict) else None
        history.append(
            CookedRecipe(
                cooked_at=cooked_at if isinstance(cooked_at, str) else None,
                recipe=recipe,
            )
        )
    return history


def _feed_item_to_dto(item: Any) -> RecipeSearchResult | None:
    if not isinstance(item, dict):
        return None
    recipe_id = item.get("id") or item.get("recipeId")
    name = item.get("title") or item.get("name")
    if not isinstance(recipe_id, str) or not recipe_id:
        return None
    if not isinstance(name, str) or not name:
        return None
    image = None
    for key in ("imageSquare", "squareImage", "image"):
        image = _resolved_image_url(item.get(key))
        if image is not None:
            break
    if image is None:
        assets = item.get("descriptiveAssets")
        if isinstance(assets, list) and assets and isinstance(assets[0], dict):
            image = _resolved_image_url(assets[0].get("square"))
    # Explicit None-coalescing instead of ``or``: a legitimate 0 rating /
    # 0-ratings count must not fall through to the alternate key.
    rating = _coerce_number(item.get("averageRating"))
    if rating is None:
        rating = _coerce_number(item.get("rating"))
    number_of_ratings = _coerce_int(item.get("numRating"))
    if number_of_ratings is None:
        number_of_ratings = _coerce_int(item.get("numberOfRatings"))
    return RecipeSearchResult(
        id=recipe_id,
        name=name,
        rating=rating,
        number_of_ratings=number_of_ratings,
        total_time_seconds=_parse_duration_seconds(item.get("totalTime")),
        image=image,
    )


def _custom_recipe_item_to_dto(item: Any) -> CustomRecipeSummary | None:
    if not isinstance(item, dict):
        return None
    recipe_id = item.get("recipeId")
    if not isinstance(recipe_id, str) or not recipe_id:
        return None
    content = item.get("recipeContent")
    content = content if isinstance(content, dict) else {}
    yield_block = content.get("recipeYield")
    yield_block = yield_block if isinstance(yield_block, dict) else {}
    name = content.get("name")
    return CustomRecipeSummary(
        recipe_id=recipe_id,
        name=name if isinstance(name, str) else "",
        created_at=item.get("createdAt"),
        total_time_seconds=_parse_duration_seconds(content.get("totalTime")),
        servings=yield_block.get("value"),
    )


def _localization_origin(url: str) -> str:
    """Normalize Cookidoo's localization URL down to a clean ``scheme://host`` origin.

    Upstream sometimes returns a fully-qualified URL (``https://cookidoo.de/...``)
    and sometimes a bare host (``cookidoo.de``); we always emit ``https://host``
    and reject any non-HTTP scheme to prevent downstream callers from being
    coaxed into ``javascript:`` / ``file:`` requests.
    """
    parsed = urlsplit(url)
    if not parsed.scheme:
        parsed = urlsplit(f"https://{url}")
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise UpstreamApiError(f"Refusing non-HTTP Cookidoo localization scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise UpstreamApiError(f"Unparseable Cookidoo localization URL: {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _redact_email(email: str) -> str:
    """Mask an email so only the first character and the domain remain."""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def _redact_error_body(body: str) -> str:
    """Strip likely-sensitive tokens from a Cookidoo error response body.

    Three passes:
    1. Key/value pairs (``"access_token": "abc"``, ``authorization=Bearer xyz``)
    2. Naked ``Bearer xxx`` headers echoed back in the body
    3. Standalone JWTs (``eyJhbGc…``) regardless of context
    Plus email addresses in any position.
    """
    # Truncate the raw upstream body BEFORE redaction so a cut never falls
    # inside a ``<redacted…>`` placeholder (which would reveal half a marker
    # plus whatever follows). Worst case we redact a single oversize JWT
    # tail; the visible output still has every secret either masked or
    # gone-with-the-truncation.
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "...<truncated>"
    # Tokens first so a credential-shaped key/value pair is replaced as a
    # whole (``"authorization": "user@x.com"`` -> ``<redacted>``); orphan
    # emails elsewhere in the body are caught by the final email pass.
    redacted = _TOKEN_REDACT_PATTERN.sub("<redacted>", body)
    redacted = _BEARER_PLAINTEXT_PATTERN.sub("<redacted>", redacted)
    redacted = _JWT_PATTERN.sub("<redacted-jwt>", redacted)
    return _EMAIL_REDACT_PATTERN.sub("<redacted-email>", redacted)


def _draft_to_payload(
    draft: CustomRecipeDraft, inferrer: AnnotationInferrer | None = None
) -> dict[str, Any]:
    annotation_inferrer = inferrer if inferrer is not None else AnnotationInferrer()
    # No image keys: the PATCH endpoint leaves omitted fields untouched
    # (verified live), so an update never wipes a previously uploaded photo.
    # Sending "image": null would reset it to the placeholder.
    return {
        "name": draft.name,
        "tools": list(draft.tools),
        "yield": {"value": draft.servings, "unitText": "portion"},
        "prepTime": draft.prep_minutes * 60,
        # Cookidoo's UI separates prep, cook and total time but our draft model
        # only carries prep + total. ``CustomRecipeDraft`` validates total>=prep
        # at construction time so ``cookTime`` is always non-negative.
        "cookTime": (draft.total_minutes - draft.prep_minutes) * 60,
        "totalTime": draft.total_minutes * 60,
        "ingredients": [{"type": "INGREDIENT", "text": item} for item in draft.ingredients],
        "instructions": [
            _step_to_payload(step, draft.ingredients, annotation_inferrer) for step in draft.steps
        ],
        "hints": "\n".join(draft.hints),
        "workStatus": "PRIVATE",
        "recipeMetadata": {"requiresAnnotationsCheck": False},
    }


def _step_to_payload(
    step: RecipeStep, ingredients: list[str], inferrer: AnnotationInferrer
) -> dict[str, Any]:
    annotations = step.annotations or inferrer.infer(step.text, ingredients)
    payload: dict[str, Any] = {"type": "STEP", "text": step.text}
    if annotations:
        payload["annotations"] = [_annotation_to_payload(annotation) for annotation in annotations]
    return payload


def _annotation_to_payload(annotation: StepAnnotation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": annotation.type,
        "data": annotation.data.model_dump(),
        "position": {"offset": annotation.offset, "length": annotation.length},
    }
    mode_name = getattr(annotation, "name", None)
    if mode_name is not None:
        payload["name"] = mode_name
    return payload


__all__ = ["CookidoughSession", "CookidoughSessionProtocol"]
