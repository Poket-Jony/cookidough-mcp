"""Repository facade over `cookidoo-api` plus undocumented custom-recipe endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import date
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import urlsplit

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    CookieJar,
    TCPConnector,
)
from cookidoo_api import (
    Cookidoo,
    CookidooConfig,
    get_localization_options,
)
from cookidoo_api.exceptions import (
    CookidooAuthException,
    CookidooException,
    CookidooParseException,
    CookidooRequestException,
)

from .annotation_models import StepAnnotation
from .annotations import AnnotationInferrer
from .constants import (
    CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS,
    CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS,
    HTTP_TIMEOUT_SECONDS,
)
from .errors import AuthenticationError, NotFoundError, UpstreamApiError
from .models import (
    CalendarDay,
    CalendarRecipe,
    CollectionSummary,
    CustomRecipeDetails,
    CustomRecipeDraft,
    CustomRecipeSummary,
    Ingredient,
    RecipeDetails,
    RecipeStep,
    ShoppingItemSource,
    ShoppingList,
    ShoppingListItem,
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


class CookidooSessionProtocol(Protocol):
    """Public surface of the Cookidoo session used by tools and tests.

    Lifecycle methods (``__aenter__``/``__aexit__``) live on the concrete
    `CookidooSession` only; the lifespan in `server.build_server` calls
    `aclose` directly, so the protocol covers exactly the surface that tools
    consume.
    """

    async def get_user_profile(self) -> UserProfile: ...
    async def get_subscription(self) -> Subscription | None: ...
    async def get_recipe_details(self, recipe_id: str) -> RecipeDetails: ...
    async def get_custom_recipe_details(self, recipe_id: str) -> CustomRecipeDetails: ...
    async def list_managed_collections(self, page: int = 0) -> list[CollectionSummary]: ...
    async def add_managed_collection(self, collection_id: str) -> CollectionSummary: ...
    async def remove_managed_collection(self, collection_id: str) -> None: ...
    async def list_custom_collections(self, page: int = 0) -> list[CollectionSummary]: ...
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
    async def list_custom_recipes(self) -> list[CustomRecipeSummary]: ...
    async def upload_custom_recipe(self, draft: CustomRecipeDraft) -> tuple[str, str]: ...
    async def delete_custom_recipe(self, recipe_id: str) -> None: ...
    async def aclose(self) -> None: ...


class CookidooSession:
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
        # session (the FastMCP lifespan treats ``aclose`` as terminal).
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

            options = await get_localization_options(
                country=self._settings.country_code,
                language=self._settings.language_code,
            )
            if not options:
                raise AuthenticationError(
                    f"No Cookidoo locale matches country={self._settings.country_code!r} "
                    f"language={self._settings.language_code!r}."
                )

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
                config = CookidooConfig(
                    email=self._settings.email,
                    password=self._settings.password.get_secret_value(),
                    localization=options[0],
                )
                client = Cookidoo(session=http, cfg=config)
                await client.login()
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
                options[0].url,
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
            self._session_generation += 1
            return client

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
        )

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

    async def list_managed_collections(self, page: int = 0) -> list[CollectionSummary]:
        collections = await self._run(lambda c: c.get_managed_collections(page=page))
        return [_collection_to_dto(item) for item in collections]

    async def add_managed_collection(self, collection_id: str) -> CollectionSummary:
        collection = await self._run(lambda c: c.add_managed_collection(collection_id))
        return _collection_to_dto(collection)

    async def remove_managed_collection(self, collection_id: str) -> None:
        await self._run(lambda c: c.remove_managed_collection(collection_id))

    async def list_custom_collections(self, page: int = 0) -> list[CollectionSummary]:
        collections = await self._run(lambda c: c.get_custom_collections(page=page))
        return [_collection_to_dto(item) for item in collections]

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
        ingredients, additional = await asyncio.gather(
            self._run(lambda c: c.get_ingredient_items()),
            self._run(lambda c: c.get_additional_items()),
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
    return {
        "name": draft.name,
        "image": None,
        "isImageOwnedByUser": False,
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


__all__ = ["CookidooSession", "CookidooSessionProtocol"]
