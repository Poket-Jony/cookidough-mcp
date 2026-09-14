"""China deployment: login flow, payload mapping and image moderation."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from cookidoo_api.exceptions import (
    CookidooAuthException,
    CookidooParseException,
    CookidooRequestException,
)
from pydantic import SecretStr

from cookidough_mcp.china_client import ChinaCookidoo
from cookidough_mcp.config import Settings
from cookidough_mcp.errors import UpstreamApiError
from cookidough_mcp.models import CustomRecipeDetails
from cookidough_mcp.session import CookidoughSession

_REDIRECT_URI = "com.vorwerk.cookidoo://code-grant"
_CIAM = "https://ciam.production-cn.cookidoo.tmecosys.cn"
_LOGIN_HTML = """
<form action="/oidc/auth/login/loginbyphone?type=phonenumberLogin" method="POST">
  <input name="login_auth_state" value="state-123" type="hidden">
  <input name="phonenumber" type="tel">
  <input name="password" type="password">
</form>
"""
_BOOTSTRAP_HTML = """
<script>form.action = "/oidc/auth/abc123/login";</script>
<input name="login_auth_state" value="boot-state" type="hidden">
"""


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        json_data: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        self.status = status
        self._json = json_data
        self._text = text
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def json(self) -> Any:
        return self._json

    async def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise CookidooRequestException(f"HTTP {self.status}")


class _FakeSession:
    """Serves canned responses by URL substring and records every request."""

    def __init__(self, routes: list[tuple[str, _FakeResponse]]) -> None:
        self._routes = routes
        self.requests: list[tuple[str, str, Any]] = []

    def _match(self, method: str, url: str, data: Any) -> _FakeResponse:
        self.requests.append((method, url, data))
        for marker, response in self._routes:
            if marker in url:
                return response
        raise AssertionError(f"unrouted {method} {url}")

    def get(self, url: Any, **kwargs: Any) -> _FakeResponse:
        return self._match("GET", str(url), kwargs.get("params"))

    def post(self, url: Any, **kwargs: Any) -> _FakeResponse:
        return self._match("POST", str(url), kwargs.get("data"))

    def request(self, method: str, url: Any, **kwargs: Any) -> _FakeResponse:
        return self._match(method.upper(), str(url), kwargs.get("data"))


def _oidc_routes(code_location: str) -> list[tuple[str, _FakeResponse]]:
    return [
        (
            ".well-known/openid-configuration",
            _FakeResponse(
                json_data={
                    "authorization_endpoint": f"{_CIAM}/oidc/auth",
                    "token_endpoint": f"{_CIAM}/oidc/token",
                }
            ),
        ),
        (
            "/oidc/token",
            _FakeResponse(
                json_data={
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_in": 3600,
                }
            ),
        ),
        ("loginbyphone", _FakeResponse(status=302, headers={"Location": code_location})),
        ("/oidc/auth", _FakeResponse(text=_LOGIN_HTML, url=f"{_CIAM}/oidc/auth")),
    ]


def _client(routes: list[tuple[str, _FakeResponse]]) -> ChinaCookidoo:
    return ChinaCookidoo(
        session=_FakeSession(routes),  # type: ignore[arg-type]
        phone_number="13800000000",
        password="pw",
    )


async def test_login_exchanges_the_code_for_bearer_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_oidc_routes(f"{_REDIRECT_URI}?code=AUTH_CODE&state=STATE"))
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "STATE")

    await client.login()

    assert client.auth_data is not None
    assert client.auth_data.access_token == "AT"
    assert client.auth_data.refresh_token == "RT"


async def test_login_satisfies_the_token_gate_guarding_every_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cookie-only login leaves `_ensure_token` without a refresh token, which
    fails all 42 cookidoo-api methods on 0.18."""
    client = _client(_oidc_routes(f"{_REDIRECT_URI}?code=AUTH_CODE&state=STATE"))
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "STATE")

    await client.login()
    await client._ensure_token()


async def test_login_rejects_a_mismatched_oauth_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_oidc_routes(f"{_REDIRECT_URI}?code=AUTH_CODE&state=ATTACKER"))
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "STATE")

    with pytest.raises(CookidooAuthException, match="state mismatch"):
        await client.login()


async def test_login_reports_rejected_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _oidc_routes("")
    routes[2] = ("loginbyphone", _FakeResponse(status=200, text="wrong password"))
    client = _client(routes)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "STATE")

    with pytest.raises(CookidooAuthException, match="no authorization code"):
        await client.login()


async def test_login_refuses_a_redirect_off_the_china_ciam_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = _oidc_routes("https://evil.example/steal")
    client = _client(routes)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "STATE")

    with pytest.raises(CookidooAuthException, match="redirected off"):
        await client.login()


def test_extract_login_form_returns_action_and_state() -> None:
    action, state = ChinaCookidoo.extract_login_form(_LOGIN_HTML, "https://cookidoo.com.cn")

    assert action == "https://cookidoo.com.cn/oidc/auth/login/loginbyphone?type=phonenumberLogin"
    assert state == "state-123"


def test_extract_login_form_rejects_a_page_without_state() -> None:
    with pytest.raises(CookidooParseException, match="login_auth_state"):
        ChinaCookidoo.extract_login_form(
            '<form action="/oidc/auth/login/loginbyphone"></form>', "https://cookidoo.com.cn"
        )


async def test_resolve_login_form_follows_the_javascript_bootstrap() -> None:
    client = _client(
        [("/oidc/auth/abc123/login", _FakeResponse(text=_LOGIN_HTML, url=f"{_CIAM}/x"))]
    )

    action, state = await client.resolve_login_form(_BOOTSTRAP_HTML, _CIAM)

    assert action.endswith("loginbyphone?type=phonenumberLogin")
    assert state == "state-123"


async def test_resolve_login_form_refuses_a_bootstrap_off_the_ciam_host() -> None:
    client = _client([])
    html = '<script>form.action = "https://evil.example/oidc/auth/x/login";</script>'
    html += '<input name="login_auth_state" value="s">'

    with pytest.raises(CookidooAuthException, match="redirected off"):
        await client.resolve_login_form(html, _CIAM)


def test_normalize_custom_recipe_maps_china_field_names() -> None:
    payload = {
        "recipeId": "recipe-1",
        "recipeContent": {
            "name": "Rice Ice Cream",
            "recipeIngredient": ["300 g rice"],
            "recipeInstructions": ["Blend until smooth."],
            "tool": ["TM6"],
        },
    }

    content = ChinaCookidoo.normalize_custom_recipe(payload)["recipeContent"]

    assert content["ingredients"] == ["300 g rice"]
    assert content["instructions"] == ["Blend until smooth."]
    assert content["tools"] == ["TM6"]
    assert content["totalTime"] == "PT0S"


async def test_get_custom_recipe_normalizes_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The China API returns `recipeIngredient`/`tool`; the parser wants the
    global names, so normalization has to happen in between."""
    client = _client([])
    seen: dict[str, Any] = {}

    async def _endpoints() -> None:
        return None

    async def _request_json(method: str, url: Any, operation: str) -> Any:
        return {
            "recipeId": "cr1",
            "recipeContent": {
                "name": "Rice Ice Cream",
                "recipeIngredient": ["300 g rice"],
                "recipeInstructions": ["Blend."],
                "tool": ["TM6"],
            },
        }

    monkeypatch.setattr(client, "_ensure_endpoints", _endpoints)
    monkeypatch.setattr(client, "_request_json", _request_json)
    monkeypatch.setattr(client, "_path", lambda _name: "created-recipes/{language}/{id}")
    monkeypatch.setattr(
        "cookidough_mcp.china_client.cookidoo_custom_recipe_from_json",
        lambda payload, _loc: seen.setdefault("payload", payload),
    )

    await client.get_custom_recipe("cr1")

    assert seen["payload"]["recipeContent"]["ingredients"] == ["300 g rice"]
    assert seen["payload"]["recipeContent"]["tools"] == ["TM6"]


def test_update_payload_keeps_guided_annotations() -> None:
    annotation = {"type": "TTS", "position": {"offset": 0, "length": 14}}
    payload = {
        "name": "Rice Ice Cream",
        "tools": ["TM6"],
        "yield": {"value": 4, "unitText": "portion"},
        "ingredients": [{"type": "INGREDIENT", "text": "300 g rice"}],
        "instructions": [{"type": "STEP", "text": "Blend", "annotations": [annotation]}],
    }

    converted = ChinaCookidoo.update_payload(payload)

    assert converted["instructions"][0]["annotations"] == [annotation]
    assert converted["recipeMetadata"] == {}


async def test_image_upload_reports_a_moderation_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        [
            (
                "upload/objectId",
                _FakeResponse(
                    json_data={
                        "objectId": "obj-1",
                        "folder": "f/",
                        "paramMap": {"region": "ap", "bucket": "b"},
                    }
                ),
            ),
            (
                "/token",
                _FakeResponse(
                    json_data={
                        "tokenMap": {
                            "tmpSecretId": "i",
                            "tmpSecretKey": "k",
                            "sessionToken": "t",
                        }
                    }
                ),
            ),
            ("audit/img", _FakeResponse(json_data={"status": "Fail"})),
        ]
    )
    monkeypatch.setattr(ChinaCookidoo, "_put_to_cos", _noop_put)
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)

    with pytest.raises(CookidooRequestException, match="rejected"):
        await client.upload_recipe_image(b"bytes", "image/png")


async def test_image_upload_names_the_optional_extra_when_the_sdk_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "qcloud_cos", None)
    client = _client([])

    with pytest.raises(CookidooRequestException, match=r"cookidough-mcp\[china\]"):
        await client._put_to_cos("obj", {}, "f/", {}, b"bytes", "image/png")


async def test_image_upload_rejects_an_incomplete_allocation() -> None:
    client = _client([("upload/objectId", _FakeResponse(json_data={"objectId": None}))])

    with pytest.raises(CookidooRequestException, match="object id"):
        await client.upload_recipe_image(b"bytes", "image/png")


async def test_session_attaches_the_moderated_image_id_for_china(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, bodies = _china_image_session(monkeypatch, persisted_image="moderated.png")

    result = await session.set_custom_recipe_image("cr1", "https://img.example/a.png")

    assert bodies[0] == {
        "image": "moderated.png",
        "isImageOwnedByUser": True,
        "isImageCopyrightOwned": True,
    }
    assert result.image is not None


async def test_session_fails_when_china_drops_the_uploaded_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _china_image_session(monkeypatch, persisted_image=None)

    with pytest.raises(UpstreamApiError, match="did not persist"):
        await session.set_custom_recipe_image("cr1", "https://img.example/a.png")


def _china_image_session(
    monkeypatch: pytest.MonkeyPatch, *, persisted_image: str | None
) -> tuple[CookidoughSession, list[Any]]:
    from contextlib import asynccontextmanager

    settings = Settings(
        email="13800000000", password=SecretStr("pw"), country="cn", language="zh-Hans-CN"
    )
    session = CookidoughSession(settings)
    client = _client([])
    bodies: list[Any] = []

    async def _login() -> Any:
        return client

    async def _upload(image_bytes: bytes, content_type: str) -> str:
        return "moderated.png"

    async def _details(recipe_id: str) -> Any:
        return CustomRecipeDetails(
            id=recipe_id,
            name="Smoothie",
            url="https://cookidoo.com.cn/r/cr1",
            image=f"https://cdn.example/{persisted_image}" if persisted_image else None,
        )

    @asynccontextmanager
    async def _authed(method: str, url: str, json_body: Any = None) -> Any:
        bodies.append(json_body)
        yield _NSRead()

    monkeypatch.setattr(session, "_ensure_logged_in", _login)
    monkeypatch.setattr(client, "upload_recipe_image", _upload)
    monkeypatch.setattr(session, "get_custom_recipe_details", _details)
    monkeypatch.setattr(session, "_authed_http", _authed)
    monkeypatch.setattr(session, "_custom_recipes_url", _recipes_url)
    monkeypatch.setattr("cookidough_mcp.session._fetch_image_url", lambda _url: _png_bytes())
    return session, bodies


class _NSRead:
    async def read(self) -> bytes:
        return b""


async def _recipes_url() -> str:
    return "https://cookidoo.com.cn/created-recipes/zh-Hans-CN"


async def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _noop_put(*args: Any, **kwargs: Any) -> None:
    return None


async def _instant_sleep(_seconds: float) -> None:
    return None
