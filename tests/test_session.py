"""Tests for the CookidooSession repository facade."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from cookidoo_api.exceptions import CookidooAuthException, CookidooRequestException

from cookidoo_mcp.annotation_models import (
    BlendModeAnnotation,
    BlendModeData,
    BrowningModeAnnotation,
    BrowningModeData,
    BrowningPower,
    DoughModeAnnotation,
    DoughModeData,
    IngredientAnnotation,
    IngredientAnnotationData,
    MixDirection,
    RiceCookerModeAnnotation,
    SteamingAccessory,
    SteamingModeAnnotation,
    SteamingModeData,
    TemperatureData,
    TtsAnnotation,
    TtsAnnotationData,
    TurboModeAnnotation,
    TurboModeData,
    WarmUpModeAnnotation,
    WarmUpModeData,
)
from cookidoo_mcp.errors import AuthenticationError, NotFoundError, UpstreamApiError
from cookidoo_mcp.models import CustomRecipeDraft, RecipeStep
from cookidoo_mcp.session import (
    CookidooSession,
    _calendar_to_dto,
    _collection_to_dto,
    _custom_recipe_item_to_dto,
    _draft_to_payload,
    _localization_origin,
    _parse_duration_seconds,
    _redact_email,
    _redact_error_body,
)


class _NS:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeCookieJar:
    def __init__(self) -> None:
        self.cleared = 0

    # Mirrors ``aiohttp.CookieJar.clear(predicate=None)``: the optional arg
    # keeps the fake honest if production code ever switches to predicated
    # clearing instead of a full wipe.
    def clear(self, predicate: Any = None) -> None:
        del predicate
        self.cleared += 1


class _FakeHttp:
    def __init__(self) -> None:
        self.cookie_jar = _FakeCookieJar()


class _FakeClient:
    def __init__(self) -> None:
        self.logins = 0
        self.detail_calls = 0
        self.fail_first = False

    async def get_user_info(self) -> Any:
        return _NS(username="alice", description=None, picture=None)

    async def login(self) -> None:
        self.logins += 1

    async def get_recipe_details(self, recipe_id: str) -> Any:
        self.detail_calls += 1
        if self.fail_first:
            self.fail_first = False
            raise CookidooAuthException("expired")
        return _NS(
            id=recipe_id,
            name="Sample",
            url="https://example.com",
            thumbnail=None,
            image=None,
            difficulty="easy",
            serving_size=4,
            active_time=600,
            total_time=1800,
            utensils=[],
            notes=[],
            ingredients=[],
        )


@pytest.fixture
def session_with_client(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Any:
    session = CookidooSession(settings)
    fake = _FakeClient()
    session._client = fake  # type: ignore[assignment]
    session._http = _FakeHttp()  # type: ignore[assignment]
    session._session_generation = 1

    async def _no_login() -> Any:
        return fake

    monkeypatch.setattr(session, "_ensure_logged_in", _no_login)
    return session, fake


async def test_run_retries_after_auth_failure_and_op_runs_twice(
    session_with_client: Any,
) -> None:
    session, fake = session_with_client
    fake.fail_first = True
    details = await session.get_recipe_details("123")
    assert details.id == "123"
    assert fake.logins == 1
    assert fake.detail_calls == 2


async def test_run_uses_pre_invocation_generation_snapshot(
    session_with_client: Any,
) -> None:
    """If the session generation advanced *between* the failed call and the
    re-login, `_relogin` must observe the pre-call generation and skip the
    redundant login. We simulate this by bumping the counter in the middle
    of the failing op."""
    session, fake = session_with_client

    async def bump(_recipe_id: str) -> Any:
        fake.detail_calls += 1
        # Another coroutine refreshed the cookie jar after we sent the
        # request but before we got back the 401.
        session._session_generation += 5
        raise CookidooAuthException("expired")

    fake.get_recipe_details = bump
    # Both calls raise auth → first raw, second wrapped to the domain error
    # (see `test_run_maps_auth_exception_on_retry_to_domain_error`). Either
    # raising as ``AuthenticationError`` is the contract here.
    with pytest.raises(AuthenticationError):
        await session.get_recipe_details("123")
    # Because the snapshot was taken before the call, the retry decided not
    # to re-login — the fake's ``login`` must never have been touched.
    assert fake.logins == 0


async def test_ensure_logged_in_refuses_after_aclose(settings: Any) -> None:
    """Regression: once ``aclose`` has latched, a stale tool call must not
    silently bootstrap a fresh login behind the back of the lifespan that
    has already torn the session down."""
    session = CookidooSession(settings)
    await session.aclose()
    with pytest.raises(UpstreamApiError, match="Session is closed"):
        await session._ensure_logged_in()


async def test_aclose_is_idempotent(settings: Any) -> None:
    """Lifespan shutdown should be safe to invoke multiple times."""
    session = CookidooSession(settings)
    await session.aclose()
    await session.aclose()


async def test_run_wraps_request_exception_as_upstream(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = CookidooSession(settings)

    class _Boom:
        async def some_call(self) -> None:
            raise CookidooRequestException("boom")

    async def _login() -> Any:
        return _Boom()

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    with pytest.raises(UpstreamApiError):
        await session._run(lambda c: c.some_call())  # type: ignore[attr-defined]


async def test_run_maps_request_exception_on_retry(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a 401, a downstream `CookidooRequestException` on the retry must
    still be mapped to `UpstreamApiError`."""
    session = CookidooSession(settings)
    session._http = _FakeHttp()  # type: ignore[assignment]
    calls = {"n": 0}

    class _RetryBoom:
        async def login(self) -> None:
            return None

        async def fetch(self) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise CookidooAuthException("expired")
            raise CookidooRequestException("upstream blew up")

    client = _RetryBoom()
    session._client = client  # type: ignore[assignment]

    async def _login() -> Any:
        return client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    with pytest.raises(UpstreamApiError, match="upstream blew up"):
        await session._run(lambda c: c.fetch())  # type: ignore[attr-defined]
    assert calls["n"] == 2


async def test_run_maps_auth_exception_on_retry_to_domain_error(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent auth failure (first call AND retry) must surface as our
    domain ``AuthenticationError``, not as the raw ``CookidooAuthException``
    — otherwise the ``session.py`` facade leaks the upstream library type."""
    session = CookidooSession(settings)
    session._http = _FakeHttp()  # type: ignore[assignment]
    calls = {"n": 0}

    class _StillBad:
        async def login(self) -> None:
            return None

        async def fetch(self) -> None:
            calls["n"] += 1
            raise CookidooAuthException("still expired")

    client = _StillBad()
    session._client = client  # type: ignore[assignment]

    async def _login() -> Any:
        return client

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    with pytest.raises(AuthenticationError, match="still expired"):
        await session._run(lambda c: c.fetch())  # type: ignore[attr-defined]
    assert calls["n"] == 2


async def test_get_recipe_details_translates_request_exception_to_not_found(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = CookidooSession(settings)

    class _Missing:
        async def get_recipe_details(self, _id: str) -> None:
            raise CookidooRequestException("404")

    async def _login() -> Any:
        return _Missing()

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    with pytest.raises(NotFoundError):
        await session.get_recipe_details("xyz")


async def test_ensure_logged_in_translates_auth_exception(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = CookidooSession(settings)

    async def _options(country: str, language: str) -> list[Any]:
        return [_NS(country_code=country, language=language, url="https://x/x")]

    monkeypatch.setattr("cookidoo_mcp.session.get_localization_options", _options)

    class _BadClient:
        async def login(self) -> None:
            raise CookidooAuthException("invalid")

    monkeypatch.setattr("cookidoo_mcp.session.Cookidoo", lambda **_: _BadClient())

    with pytest.raises(AuthenticationError):
        await session._ensure_logged_in()


async def test_relogin_is_skipped_when_generation_advanced(
    settings: Any,
) -> None:
    session = CookidooSession(settings)
    session._http = _FakeHttp()  # type: ignore[assignment]
    session._session_generation = 5
    calls = {"n": 0}

    class _Client:
        async def login(self) -> None:
            calls["n"] += 1

    session._client = _Client()  # type: ignore[assignment]

    # Caller saw generation 3, but the session is already at 5 → no re-login.
    await session._relogin(observed_generation=3)
    assert calls["n"] == 0
    assert session._http.cookie_jar.cleared == 0  # type: ignore[union-attr]


async def test_relogin_runs_once_for_parallel_callers(settings: Any) -> None:
    """Two coroutines that hit a 401 with the same observed generation must
    re-login exactly once, not in serial."""
    session = CookidooSession(settings)
    session._http = _FakeHttp()  # type: ignore[assignment]
    session._session_generation = 1
    calls = {"n": 0}

    class _Client:
        async def login(self) -> None:
            calls["n"] += 1
            await asyncio.sleep(0)

    session._client = _Client()  # type: ignore[assignment]

    await asyncio.gather(
        session._relogin(observed_generation=1),
        session._relogin(observed_generation=1),
    )
    assert calls["n"] == 1
    assert session.session_generation == 2
    assert session._http.cookie_jar.cleared == 1  # type: ignore[union-attr]


async def test_relogin_translates_auth_exception(settings: Any) -> None:
    session = CookidooSession(settings)
    session._http = _FakeHttp()  # type: ignore[assignment]
    session._session_generation = 1

    class _Client:
        async def login(self) -> None:
            raise CookidooAuthException("re-login failed")

    session._client = _Client()  # type: ignore[assignment]

    with pytest.raises(AuthenticationError):
        await session._relogin(observed_generation=1)


def test_collection_to_dto_counts_recipes() -> None:
    collection = _NS(
        id="c1",
        name="N",
        description="d",
        chapters=[
            _NS(name="ch1", recipes=[_NS(), _NS()]),
            _NS(name="ch2", recipes=[_NS()]),
        ],
    )
    dto = _collection_to_dto(collection)
    assert dto.chapter_count == 2
    assert dto.recipe_count == 3


def test_calendar_to_dto_carries_custom_recipe_ids() -> None:
    day = _NS(
        id="d1",
        title="Mon",
        recipes=[_NS(id="r", name="n", total_time=600, url="u", thumbnail=None, image=None)],
        customer_recipe_ids=["cr1"],
    )
    dto = _calendar_to_dto(day)
    assert dto.custom_recipe_ids == ["cr1"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cookidoo.de/foundation/de-DE", "https://cookidoo.de"),
        ("https://cookidoo.com.es/foundation/es", "https://cookidoo.com.es"),
        ("cookidoo.de", "https://cookidoo.de"),
        ("cookidoo.de/foundation/de-DE", "https://cookidoo.de"),
    ],
)
def test_localization_origin_normalizes_url_variants(url: str, expected: str) -> None:
    assert _localization_origin(url) == expected


def test_localization_origin_rejects_empty_host() -> None:
    with pytest.raises(UpstreamApiError):
        _localization_origin("")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "ftp://cookidoo.de/file"],
)
def test_localization_origin_rejects_unsafe_schemes(url: str) -> None:
    with pytest.raises(UpstreamApiError):
        _localization_origin(url)


def test_redact_email_keeps_domain_and_first_char() -> None:
    assert _redact_email("alice@example.com") == "a***@example.com"
    assert _redact_email("no-at-sign") == "***"


def test_redact_error_body_strips_tokens_and_emails() -> None:
    redacted = _redact_error_body('{"access_token": "abc.def.ghi", "email": "leak@example.com"}')
    assert "abc.def.ghi" not in redacted
    assert "leak@example.com" not in redacted
    assert "<redacted>" in redacted
    assert "<redacted-email>" in redacted


def test_redact_error_body_truncates_long_payloads() -> None:
    long_body = "x" * 1000
    redacted = _redact_error_body(long_body)
    assert redacted.endswith("...<truncated>")
    assert len(redacted) <= 1000


def test_redact_error_body_scrubs_naked_jwt() -> None:
    body = "Trace: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abc123 happened"
    redacted = _redact_error_body(body)
    assert "eyJhbGci" not in redacted
    assert "<redacted-jwt>" in redacted


def test_redact_error_body_scrubs_inline_bearer_header() -> None:
    body = "Bearer abc.def.ghi was rejected"
    redacted = _redact_error_body(body)
    assert "abc.def.ghi" not in redacted
    assert "<redacted>" in redacted


def test_redact_error_body_scrubs_extended_token_keys() -> None:
    for key in ("id_token", "api_key", "session_id", "csrf"):
        redacted = _redact_error_body(f'{{"{key}": "secret-value"}}')
        assert "secret-value" not in redacted, key


def test_redact_error_body_keeps_substring_lookalikes_intact() -> None:
    """Regression: the pattern used to match any key whose name *contained*
    a sensitive token (e.g. ``my_csrf``, ``request_id_token``), wrongly
    redacting values that aren't actually credentials. Word-boundary
    anchors must keep those passing through untouched."""
    for harmless_key, value in (
        ("my_csrf_flag", "true"),
        ("request_id_tokenizer", "abc"),
        ("expires_for_session_lookup", "2026-05-22"),
    ):
        body = f'{{"{harmless_key}": "{value}"}}'
        redacted = _redact_error_body(body)
        assert value in redacted, (harmless_key, redacted)
        assert harmless_key in redacted, (harmless_key, redacted)


def test_redact_error_body_treats_token_pair_as_atomic_unit() -> None:
    """An email that sits inside a credential-shaped pair must vanish along
    with the rest of the value (not survive as ``<redacted-email>`` inside
    a ``<redacted>`` block): the whole pair is what we cannot leak."""
    body = '{"authorization": "user@example.com"}'
    redacted = _redact_error_body(body)
    assert "user@example.com" not in redacted
    assert "<redacted>" in redacted
    assert "<redacted-email>" not in redacted


def test_redact_error_body_truncates_before_redacting_so_markers_stay_intact() -> None:
    """Regression: if truncation happened *after* redaction, a 200-char
    cut could fall inside a ``<redacted-jwt>`` / ``<redacted-email>``
    placeholder and reveal half the marker plus whatever followed."""
    body = "x" * 250 + " contact jane@example.com afterwards"
    redacted = _redact_error_body(body)
    # The truncation marker must survive verbatim — proving the cut happens
    # on raw input, not on a string that may contain partial placeholders.
    assert "...<truncated>" in redacted


def test_redact_error_body_keeps_email_visible_outside_token_context() -> None:
    """Standalone emails (e.g. in a free-text error message) should be
    masked to ``<redacted-email>`` so operators can still see *that* an
    email was present without seeing *which* email."""
    body = "Sign-in failed for jane@example.com — retry tomorrow."
    redacted = _redact_error_body(body)
    assert "jane@example.com" not in redacted
    assert "<redacted-email>" in redacted


def test_custom_recipe_item_to_dto_pulls_nested_fields() -> None:
    dto = _custom_recipe_item_to_dto(
        {
            "recipeId": "abc",
            "createdAt": "2026-05-01",
            "recipeContent": {
                "name": "Cake",
                "totalTime": 3600,
                "recipeYield": {"value": 8},
            },
        }
    )
    assert dto is not None
    assert dto.recipe_id == "abc"
    assert dto.name == "Cake"
    assert dto.servings == 8


def test_custom_recipe_item_to_dto_drops_items_without_recipe_id() -> None:
    assert _custom_recipe_item_to_dto({"recipeContent": {"name": "X"}}) is None
    assert _custom_recipe_item_to_dto({"recipeId": ""}) is None
    assert _custom_recipe_item_to_dto("not-a-dict") is None


def test_custom_recipe_item_to_dto_accepts_iso_duration_total_time() -> None:
    """Regression: the `/created-recipes` listing now returns `totalTime` as
    an ISO-8601 duration (`"PT35M"`) instead of integer seconds. Before this
    fix, Pydantic raised ``ValidationError`` and `list_custom_recipes` was
    unusable as soon as the account contained at least one custom recipe."""
    dto = _custom_recipe_item_to_dto(
        {
            "recipeId": "abc",
            "createdAt": "2026-05-22",
            "recipeContent": {
                "name": "Spargelsalat",
                "totalTime": "PT35M",
                "recipeYield": {"value": 4},
            },
        }
    )
    assert dto is not None
    assert dto.total_time_seconds == 35 * 60


def test_parse_duration_seconds_handles_known_inputs() -> None:
    assert _parse_duration_seconds(None) is None
    assert _parse_duration_seconds(3600) == 3600  # legacy integer-seconds form
    assert _parse_duration_seconds("PT35M") == 35 * 60
    assert _parse_duration_seconds("PT1H30M") == 90 * 60
    assert _parse_duration_seconds("PT45S") == 45
    assert _parse_duration_seconds("PT2H15M30S") == 2 * 3600 + 15 * 60 + 30
    # Out-of-contract / unparseable inputs return None instead of crashing
    # the whole listing — the field is `int | None`.
    assert _parse_duration_seconds("P1DT2H") is None
    assert _parse_duration_seconds("PT") is None
    assert _parse_duration_seconds("garbage") is None
    assert _parse_duration_seconds(True) is None
    assert _parse_duration_seconds(3.14) is None


def test_draft_to_payload_serializes_known_fields() -> None:
    # Step text intentionally avoids any token from ``ingredients`` so the
    # head-noun inferrer leaves the step's ``annotations`` empty — this test
    # is about field-level serialization, not annotation inference.
    draft = CustomRecipeDraft(
        name="Test",
        ingredients=["Apple"],
        steps=[RecipeStep(text="Cut and serve.")],
        servings=2,
        prep_minutes=5,
        total_minutes=10,
        tools=["TM7"],
        hints=["Use a sharp knife."],
    )
    payload = _draft_to_payload(draft)
    assert payload["name"] == "Test"
    assert payload["yield"] == {"value": 2, "unitText": "portion"}
    assert payload["prepTime"] == 300
    assert payload["cookTime"] == 300
    assert payload["totalTime"] == 600
    assert payload["ingredients"] == [{"type": "INGREDIENT", "text": "Apple"}]
    assert payload["instructions"] == [{"type": "STEP", "text": "Cut and serve."}]
    assert payload["hints"] == "Use a sharp knife."


def test_custom_recipe_draft_rejects_unknown_tools() -> None:
    """Non-TM model strings (e.g. accessory names) must fail validation
    before reaching the upload payload — Cookidoo silently drops them."""
    with pytest.raises(ValueError, match="tools"):
        CustomRecipeDraft(
            name="Test",
            ingredients=["Apple"],
            steps=[RecipeStep(text="Chop.")],
            tools=["Mixtopf", "Spatel"],  # type: ignore[list-item]
        )


def test_custom_recipe_draft_accepts_known_tools() -> None:
    draft = CustomRecipeDraft(
        name="Test",
        ingredients=["Apple"],
        steps=[RecipeStep(text="Chop.")],
        tools=["TM7", "TM6"],
    )
    assert draft.tools == ["TM7", "TM6"]


def test_custom_recipe_draft_rejects_total_below_prep() -> None:
    with pytest.raises(ValueError, match="total_minutes"):
        CustomRecipeDraft(
            name="X",
            ingredients=["A"],
            steps=[RecipeStep(text="Mix.")],
            prep_minutes=30,
            total_minutes=10,
        )


def test_custom_recipe_draft_coerces_step_strings_to_recipe_steps() -> None:
    draft = CustomRecipeDraft.model_validate({"name": "X", "ingredients": ["A"], "steps": ["Mix."]})

    assert draft.steps == [RecipeStep(text="Mix.")]
    assert draft.step_texts == ["Mix."]


def test_recipe_step_rejects_annotation_exceeding_text_length() -> None:
    with pytest.raises(ValueError, match="exceeds step text length"):
        RecipeStep(
            text="short",
            annotations=[
                TtsAnnotation(data=TtsAnnotationData(speed="1", time=1), offset=0, length=99)
            ],
        )


def test_recipe_step_accepts_annotation_filling_entire_text() -> None:
    step = RecipeStep(
        text="abc",
        annotations=[TtsAnnotation(data=TtsAnnotationData(speed="1", time=1), offset=0, length=3)],
    )
    assert step.annotations[0].length == 3


def test_recipe_step_rejects_annotation_one_past_end() -> None:
    with pytest.raises(ValueError, match="exceeds step text length"):
        RecipeStep(
            text="abc",
            annotations=[
                TtsAnnotation(data=TtsAnnotationData(speed="1", time=1), offset=1, length=3)
            ],
        )


def test_draft_to_payload_passes_explicit_annotations_through() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["3 EL Öl"],
        steps=[
            RecipeStep(
                text="3 EL Öl erhitzen.",
                annotations=[
                    IngredientAnnotation(
                        data=IngredientAnnotationData(description="3 EL Öl"),
                        offset=0,
                        length=7,
                    )
                ],
            )
        ],
    )

    payload = _draft_to_payload(draft)

    assert payload["instructions"] == [
        {
            "type": "STEP",
            "text": "3 EL Öl erhitzen.",
            "annotations": [
                {
                    "type": "INGREDIENT",
                    "data": {"description": "3 EL Öl"},
                    "position": {"offset": 0, "length": 7},
                }
            ],
        }
    ]


def test_draft_to_payload_infers_annotations_when_step_has_none() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["3 EL Haselnussöl"],
        steps=[
            RecipeStep(
                text="3 EL Haselnussöl in den Mixtopf geben, 3 Min. 50 Sek./Stufe 1 erhitzen."
            )
        ],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    annotations = instruction["annotations"]
    assert [a["type"] for a in annotations] == ["INGREDIENT", "TTS"]
    # Step text repeats the ``3 EL `` quantity inline, so the span covers
    # ``3 EL Haselnussöl`` (offset 0, len 16). The canonical full line is
    # carried in ``description``.
    assert annotations[0]["position"] == {"offset": 0, "length": 16}
    assert annotations[0]["data"] == {"description": "3 EL Haselnussöl"}
    assert annotations[1]["data"] == {"speed": "1", "time": 230}
    assert annotations[1]["position"] == {"offset": 39, "length": 22}


def test_draft_to_payload_emits_mode_name_for_browning_annotation() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Chicken"],
        steps=[
            RecipeStep(
                text="Chicken 8 Min./150 °C/Intensiv anbraten.",
                annotations=[
                    BrowningModeAnnotation(
                        data=BrowningModeData(
                            time=480,
                            temperature=TemperatureData(value="150"),
                            power=BrowningPower.INTENSE,
                        ),
                        offset=8,
                        length=23,
                    )
                ],
            )
        ],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert instruction["annotations"] == [
        {
            "type": "MODE",
            "name": "browning",
            "data": {
                "time": 480,
                "temperature": {"value": "150", "unit": "C"},
                "power": "Intense",
            },
            "position": {"offset": 8, "length": 23},
        }
    ]


def test_draft_to_payload_round_trips_all_mode_annotations() -> None:
    """Every supported MODE name reaches the payload with its canonical shape."""
    text = "x" * 200
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Mehl"],
        steps=[
            RecipeStep(
                text=text,
                annotations=[
                    DoughModeAnnotation(data=DoughModeData(time=120), offset=0, length=18),
                    TurboModeAnnotation(
                        data=TurboModeData(time=1, pulseCount=1), offset=19, length=12
                    ),
                    SteamingModeAnnotation(
                        data=SteamingModeData(
                            time=60,
                            speed="soft",
                            accessory=SteamingAccessory.VAROMA_AND_SIMMERING_BASKET,
                        ),
                        offset=32,
                        length=19,
                    ),
                    RiceCookerModeAnnotation(offset=52, length=11),
                    WarmUpModeAnnotation(
                        data=WarmUpModeData(speed="2", temperature=TemperatureData(value="70")),
                        offset=64,
                        length=15,
                    ),
                    BlendModeAnnotation(
                        data=BlendModeData(speed="6", time=90), offset=80, length=25
                    ),
                ],
            )
        ],
    )

    annotations = _draft_to_payload(draft)["instructions"][0]["annotations"]

    by_name = {a.get("name"): a for a in annotations}
    assert by_name["dough"]["data"] == {"time": 120}
    assert by_name["turbo"]["data"] == {"time": 1, "pulseCount": 1}
    assert by_name["steaming"]["data"] == {
        "time": 60,
        "speed": "soft",
        "direction": "CW",
        "accessory": "VaromaAndSimmeringBasket",
    }
    assert by_name["rice_cooker"]["data"] == {}
    assert by_name["warm_up"]["data"] == {
        "speed": "2",
        "temperature": {"value": "70", "unit": "C"},
    }
    assert by_name["blend"]["data"] == {"speed": "6", "time": 90}
    assert all(a["type"] == "MODE" for a in annotations)


def test_warm_up_mode_data_omits_time_when_not_set() -> None:
    """The annotation-data wrap serializer drops ``None`` fields at the source.

    Plain ``model_dump()`` (no kwargs) must already produce the wire shape
    Cookidoo expects, without the caller asking for ``exclude_none``.
    """
    annotation = WarmUpModeAnnotation(
        data=WarmUpModeData(speed="soft", temperature=TemperatureData(value="37")),
        offset=0,
        length=14,
    )

    dumped = annotation.data.model_dump()

    assert "time" not in dumped
    assert dumped == {"speed": "soft", "temperature": {"value": "37", "unit": "C"}}


def test_draft_to_payload_emits_tts_direction_when_set() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Mehl"],
        steps=[
            RecipeStep(
                text="Mahlen 2 Min./Stufe 10 reverse.",
                annotations=[
                    TtsAnnotation(
                        data=TtsAnnotationData(
                            speed="10", time=120, direction=MixDirection.COUNTER_CLOCKWISE
                        ),
                        offset=7,
                        length=15,
                    )
                ],
            )
        ],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert instruction["annotations"][0]["data"] == {
        "speed": "10",
        "time": 120,
        "direction": "CCW",
    }


def test_turbo_mode_supports_sub_second_time() -> None:
    """Cookidoo's frontend emits Turbo durations like ``0.5`` s — DTO must allow floats."""
    annotation = TurboModeAnnotation(
        data=TurboModeData(time=0.5, pulseCount=9),
        offset=0,
        length=14,
    )

    assert annotation.data.model_dump() == {"time": 0.5, "pulseCount": 9}


def test_turbo_mode_coerces_integer_time_to_float() -> None:
    """Integer ``time`` is accepted and surfaces as float on dump.

    Pins the current Pydantic v2 lax-coercion behavior; a future strict-mode
    flip would surface here so the wire shape stays predictable.
    """
    data = TurboModeData(time=1, pulseCount=1)

    assert isinstance(data.time, float)
    assert data.model_dump() == {"time": 1.0, "pulseCount": 1}


def test_steaming_mode_accepts_simmering_basket_only_accessory() -> None:
    annotation = SteamingModeAnnotation(
        data=SteamingModeData(
            time=5,
            speed="5",
            direction=MixDirection.COUNTER_CLOCKWISE,
            accessory=SteamingAccessory.SIMMERING_BASKET,
        ),
        offset=0,
        length=19,
    )

    assert annotation.data.accessory is SteamingAccessory.SIMMERING_BASKET
    assert annotation.data.direction is MixDirection.COUNTER_CLOCKWISE


def test_step_annotation_discriminator_dispatches_every_mode() -> None:
    """Each MODE/<name> tag dispatches to its concrete annotation class
    and the inner ``data`` submodel is fully validated (not coerced into
    a default-state instance)."""
    cases: list[tuple[str, dict[str, Any], type[Any], dict[str, Any]]] = [
        (
            "browning",
            {"time": 300, "temperature": {"value": "150", "unit": "C"}, "power": "Intense"},
            BrowningModeAnnotation,
            {"time": 300, "power": BrowningPower.INTENSE},
        ),
        (
            "steaming",
            {"time": 60, "speed": "1", "direction": "CW", "accessory": "Varoma"},
            SteamingModeAnnotation,
            {"time": 60, "speed": "1", "accessory": SteamingAccessory.VAROMA},
        ),
        ("dough", {"time": 60}, DoughModeAnnotation, {"time": 60}),
        (
            "turbo",
            {"time": 0.5, "pulseCount": 3},
            TurboModeAnnotation,
            {"time": 0.5, "pulseCount": 3},
        ),
        ("rice_cooker", {}, RiceCookerModeAnnotation, {}),
        (
            "warm_up",
            {"speed": "soft", "temperature": {"value": "37", "unit": "C"}},
            WarmUpModeAnnotation,
            {"speed": "soft"},
        ),
        ("blend", {"speed": "6", "time": 90}, BlendModeAnnotation, {"speed": "6", "time": 90}),
    ]

    for name, data, expected_class, expected_fields in cases:
        draft = CustomRecipeDraft.model_validate(
            {
                "name": "X",
                "ingredients": ["x"],
                "steps": [
                    {
                        "text": "x" * 30,
                        "annotations": [
                            {
                                "type": "MODE",
                                "name": name,
                                "data": data,
                                "offset": 0,
                                "length": 5,
                            }
                        ],
                    }
                ],
            }
        )
        annotation = draft.steps[0].annotations[0]
        assert isinstance(annotation, expected_class), f"name={name!r} dispatched wrong class"
        for field, expected_value in expected_fields.items():
            assert getattr(annotation.data, field) == expected_value, (
                f"name={name!r} field={field!r} did not round-trip"
            )


def test_draft_to_payload_accepts_mode_annotation_from_raw_dict() -> None:
    """Wire-format dicts coming through FastMCP are validated via the discriminator."""
    draft = CustomRecipeDraft.model_validate(
        {
            "name": "X",
            "ingredients": ["Mehl"],
            "steps": [
                {
                    "text": "Pürieren /1 Min.",
                    "annotations": [
                        {
                            "type": "MODE",
                            "name": "blend",
                            "data": {"speed": "6", "time": 60},
                            "offset": 0,
                            "length": 16,
                        }
                    ],
                }
            ],
        }
    )

    annotation = _draft_to_payload(draft)["instructions"][0]["annotations"][0]

    assert annotation["name"] == "blend"
    assert annotation["data"] == {"speed": "6", "time": 60}


def test_draft_to_payload_omits_optional_temperature_from_tts() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Apple"],
        steps=[
            RecipeStep(
                text="Chop 5 Sek./Stufe 5.",
                annotations=[
                    TtsAnnotation(
                        data=TtsAnnotationData(speed="5", time=5),
                        offset=5,
                        length=14,
                    )
                ],
            )
        ],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert instruction["annotations"][0]["data"] == {"speed": "5", "time": 5}


def test_draft_to_payload_omits_annotations_when_none_inferred() -> None:
    # Step text deliberately omits any ingredient noun and any TTS/MODE token
    # so no strategy contributes an annotation.
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Apple"],
        steps=[RecipeStep(text="Mix everything together.")],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert "annotations" not in instruction
