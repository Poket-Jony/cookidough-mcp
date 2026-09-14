"""Cookidoo client for the mainland-China deployment.

China runs its own identity provider and a phone-number login form, but the
same OAuth2 authorization-code + PKCE flow as the global site. Only the four
CIAM-specific steps are overridden here; discovery, the PKCE pair, the code
exchange and token storage come from ``Cookidoo`` so this client authenticates
with a ``Bearer`` token like any other.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import parse_qs, urljoin, urlparse

from cookidoo_api import Cookidoo, CookidooConfig, CookidooLocalizationConfig
from cookidoo_api.const import LOGIN_HEADERS, OAUTH_SCOPE
from cookidoo_api.exceptions import (
    CookidooAuthException,
    CookidooParseException,
    CookidooRequestException,
)
from cookidoo_api.helpers import cookidoo_custom_recipe_from_json
from cookidoo_api.types import CookidooCustomRecipe

from .constants import (
    CHINA_CIAM_BASE_URL,
    CHINA_COOKIDOO_ORIGIN,
    CHINA_COS_TIMEOUT_SECONDS,
    CHINA_COUNTRY_CODE,
    CHINA_IMAGE_AUDIT_POLL_ATTEMPTS,
    CHINA_IMAGE_AUDIT_POLL_INTERVAL_SECONDS,
    CHINA_LANGUAGE_CODE,
    CHINA_OIDC_DISCOVERY_URL,
)

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGIN_FORM_ACTION_RE: Final = re.compile(
    r'<form[^>]+action=["\']([^"\']*loginbyphone[^"\']*)', re.I
)
_LOGIN_STATE_RE: Final = re.compile(
    r'(?:<input[^>]+name=["\']login_auth_state["\'][^>]+value=["\']|'
    r'login_auth_state\s*=\s*["\'])([^"\']+)',
    re.I,
)
_BOOTSTRAP_ACTION_RE: Final = re.compile(
    r'form\.action\s*=\s*["\']([^"\']*/oidc/auth/[^"\']+/login)["\']', re.I
)

CHINA_LOCALIZATION: Final = CookidooLocalizationConfig(
    country_code=CHINA_COUNTRY_CODE,
    language=CHINA_LANGUAGE_CODE,
    url=f"{CHINA_COOKIDOO_ORIGIN}/foundation/{CHINA_LANGUAGE_CODE}",
)


class ChinaCookidoo(Cookidoo):
    """Cookidoo client for the official mainland-China phone/password login."""

    def __init__(self, session: ClientSession, *, phone_number: str, password: str) -> None:
        super().__init__(
            session=session,
            cfg=CookidooConfig(
                email=phone_number,
                password=password,
                localization=CHINA_LOCALIZATION,
            ),
        )

    async def _discovery(self) -> dict[str, str]:
        if self._oidc is None:
            async with self._session.get(
                CHINA_OIDC_DISCOVERY_URL, headers=LOGIN_HEADERS
            ) as response:
                response.raise_for_status()
                self._oidc = cast(dict[str, str], await response.json())
        return self._oidc

    @staticmethod
    def _assert_ciam_origin(url: str) -> None:
        origin = urlparse(url)
        expected = urlparse(CHINA_CIAM_BASE_URL)
        if (origin.scheme, origin.netloc) != (expected.scheme, expected.netloc):
            raise CookidooAuthException(f"Login flow redirected off the China CIAM host: {url}")

    @staticmethod
    def extract_login_form(login_html: str, page_url: str) -> tuple[str, str]:
        """Return the phone-login POST target and its one-time state."""
        action_match = _LOGIN_FORM_ACTION_RE.search(login_html)
        if action_match is None:
            raise CookidooParseException("China login page did not expose its phone-password form.")
        state_match = _LOGIN_STATE_RE.search(login_html)
        if state_match is None:
            raise CookidooParseException("China login page did not expose login_auth_state.")
        return urljoin(page_url, action_match.group(1)), state_match.group(1)

    async def resolve_login_form(self, login_html: str, page_url: str) -> tuple[str, str]:
        """Follow the CIAM JavaScript bootstrap page to the password form."""
        if _LOGIN_FORM_ACTION_RE.search(login_html):
            return self.extract_login_form(login_html, page_url)
        bootstrap_match = _BOOTSTRAP_ACTION_RE.search(login_html)
        state_match = _LOGIN_STATE_RE.search(login_html)
        if bootstrap_match is None or state_match is None:
            raise CookidooParseException(
                "China login page did not expose its password-login route."
            )
        target = urljoin(page_url, bootstrap_match.group(1))
        self._assert_ciam_origin(target)
        async with self._session.post(
            target,
            data={"login_auth_state": state_match.group(1)},
            headers=LOGIN_HEADERS,
            allow_redirects=True,
        ) as response:
            if response.status != HTTPStatus.OK:
                raise CookidooAuthException(f"China login form returned HTTP {response.status}.")
            return self.extract_login_form(await response.text(), str(response.url))

    async def _submit_phone_credentials(
        self, form_action: str, login_state: str, state: str
    ) -> str:
        """POST the phone credentials and capture the authorization code."""
        url = form_action
        data: dict[str, str] | None = {
            "login_auth_state": login_state,
            "phonenumber": self._cfg.email,
            "password": self._cfg.password,
        }
        method = "post"
        code: str | None = None
        for _ in range(10):
            async with self._session.request(
                method,
                url,
                data=data,
                headers=LOGIN_HEADERS,
                allow_redirects=False,
            ) as response:
                location = response.headers.get("Location")
                if response.status in (301, 302, 303, 307, 308) and location:
                    if location.startswith(self._cfg.redirect_uri):
                        query = parse_qs(urlparse(location).query)
                        if query.get("state") != [state]:
                            raise CookidooAuthException("OAuth state mismatch.")
                        codes = query.get("code")
                        code = codes[0] if codes else None
                        break
                    url = urljoin(url, location)
                    self._assert_ciam_origin(url)
                    method, data = "get", None
                    continue
                break
        if code is None:
            raise CookidooAuthException(
                "China login failed: no authorization code returned. Please check "
                "the phone number and password."
            )
        return code

    async def login(self) -> None:
        """Run the China OIDC/PKCE flow and store the resulting tokens."""
        self._assert_oauth_client()
        oidc = await self._discovery()
        verifier, challenge = self._pkce_pair()
        state = secrets.token_urlsafe(12)
        params = {
            "response_type": "code",
            "client_id": self._cfg.client_id,
            "redirect_uri": self._cfg.redirect_uri,
            "market": CHINA_COUNTRY_CODE,
            "scope": OAUTH_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "ui_locales": CHINA_LANGUAGE_CODE,
        }
        async with self._session.get(
            oidc["authorization_endpoint"],
            params=params,
            headers=LOGIN_HEADERS,
            allow_redirects=True,
        ) as response:
            self._check_login_page_status(response.status)
            login_html = await response.text()
            page_url = str(response.url)

        form_action, login_state = await self.resolve_login_form(login_html, page_url)
        code = await self._submit_phone_credentials(form_action, login_state, state)
        await self._exchange_code(oidc["token_endpoint"], code, verifier)
        self._logged_in = True

    @staticmethod
    def normalize_custom_recipe(payload: dict[str, Any]) -> dict[str, Any]:
        """Map the China API's custom-recipe field names onto the global shape."""
        normalized = dict(payload)
        content = dict(payload.get("recipeContent") or {})
        content["ingredients"] = list(content.get("recipeIngredient") or [])
        content["instructions"] = list(content.get("recipeInstructions") or [])
        content["tools"] = list(content.get("tool") or [])
        content.setdefault("totalTime", "PT0S")
        content.setdefault("prepTime", "PT0S")
        normalized["recipeContent"] = content
        return normalized

    async def get_custom_recipe(self, id: str) -> CookidooCustomRecipe:
        """Fetch a custom recipe, mapping the China field names before parsing."""
        await self._ensure_endpoints()
        url = self.api_endpoint / self._path("customer-recipes:recipe-details").format(
            **self._cfg.localization.__dict__, id=id
        )
        result = self._ensure_mapping(
            await self._request_json("get", url, "loading custom recipe"),
            "loading custom recipe",
        )
        normalized = self.normalize_custom_recipe(dict(result))
        return self._parse_result(
            "loading custom recipe",
            lambda: cookidoo_custom_recipe_from_json(cast(Any, normalized), self._cfg.localization),
        )

    @staticmethod
    def update_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Translate a generic draft payload into the China editor's PATCH body."""
        return {
            "name": payload["name"],
            "tools": list(payload.get("tools") or []),
            "yield": payload.get("yield") or {},
            "ingredients": payload.get("ingredients") or [],
            "instructions": payload.get("instructions") or [],
            "recipeMetadata": payload.get("recipeMetadata") or {},
        }

    async def upload_recipe_image(self, image_bytes: bytes, content_type: str) -> str:
        """Upload an image to Tencent COS and wait for Cookidoo's moderation."""
        object_id, param_map, folder = await self._allocate_image_slot(content_type)
        token_map = await self._request_cos_token(object_id)
        await self._put_to_cos(object_id, param_map, folder, token_map, image_bytes, content_type)
        await self._await_image_audit(object_id)
        return object_id

    def _moderation_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Requested-With": "xmlhttprequest",
            "correlation-id": str(uuid.uuid4()),
        }

    async def _allocate_image_slot(self, content_type: str) -> tuple[str, dict[str, Any], str]:
        image_format = "png" if content_type == "image/png" else "jpg"
        url = self.api_endpoint / (
            f"content-moderation/customer_recipe/upload/objectId?format={image_format}"
        )
        async with self._session.get(url, headers=self._moderation_headers()) as response:
            response.raise_for_status()
            allocation = await response.json()
        object_id = allocation.get("objectId")
        param_map = allocation.get("paramMap")
        folder = allocation.get("folder")
        if not isinstance(object_id, str) or not isinstance(param_map, dict):
            raise CookidooRequestException("China image upload did not return an object id.")
        if not isinstance(folder, str):
            raise CookidooRequestException("China image upload did not return a target folder.")
        return object_id, param_map, folder

    async def _request_cos_token(self, object_id: str) -> dict[str, Any]:
        url = self.api_endpoint / f"content-moderation/customer_recipe/upload/{object_id}/token"
        async with self._session.get(url, headers=self._moderation_headers()) as response:
            response.raise_for_status()
            token_map = (await response.json()).get("tokenMap")
        if not isinstance(token_map, dict):
            raise CookidooRequestException("China image upload did not return COS credentials.")
        return token_map

    async def _put_to_cos(
        self,
        object_id: str,
        param_map: dict[str, Any],
        folder: str,
        token_map: dict[str, Any],
        image_bytes: bytes,
        content_type: str,
    ) -> None:
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise CookidooRequestException(
                "China image upload needs the optional Tencent COS SDK. Install it "
                "with: pip install 'cookidough-mcp[china]'."
            ) from exc
        try:
            cos = CosS3Client(
                CosConfig(
                    Region=param_map["region"],
                    SecretId=token_map["tmpSecretId"],
                    SecretKey=token_map["tmpSecretKey"],
                    Token=token_map["sessionToken"],
                    Scheme="https",
                    Timeout=CHINA_COS_TIMEOUT_SECONDS,
                )
            )
            await asyncio.to_thread(
                cos.put_object,
                Bucket=param_map["bucket"],
                Key=folder + object_id,
                Body=image_bytes,
                ContentType=content_type,
            )
        except (KeyError, TypeError) as exc:
            raise CookidooRequestException("China image upload credentials were invalid.") from exc

    async def _await_image_audit(self, object_id: str) -> None:
        url = self.api_endpoint / f"content-moderation/customer_recipe/audit/img/{object_id}"
        for _ in range(CHINA_IMAGE_AUDIT_POLL_ATTEMPTS):
            await asyncio.sleep(CHINA_IMAGE_AUDIT_POLL_INTERVAL_SECONDS)
            async with self._session.get(url, headers=self._moderation_headers()) as response:
                response.raise_for_status()
                status = (await response.json()).get("status")
            if status == "Success":
                return
            if status == "Fail":
                raise CookidooRequestException("China image moderation rejected the upload.")
        raise CookidooRequestException("China image moderation timed out.")
