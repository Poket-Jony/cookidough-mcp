"""Settings.language_code canonicalization tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from cookidough_mcp.config import Settings


def _settings(*, country: str, language: str) -> Settings:
    return Settings(
        email="test@example.com",
        password=SecretStr("hunter2"),
        country=country,
        language=language,
    )


@pytest.mark.parametrize(
    ("country", "language", "expected"),
    [
        ("de", "de", "de-DE"),
        ("de", "de-de", "de-DE"),
        ("de", "DE-DE", "de-DE"),
        ("de", "de-DE", "de-DE"),
        ("gb", "en", "en-GB"),
        ("gb", "en-gb", "en-GB"),
        ("us", "en-US", "en-US"),
    ],
)
def test_language_code_canonicalizes_to_bcp47(country: str, language: str, expected: str) -> None:
    assert _settings(country=country, language=language).language_code == expected


def test_country_code_is_always_lowercase() -> None:
    assert _settings(country="DE", language="de").country_code == "de"


def test_token_file_defaults_to_none() -> None:
    assert _settings(country="de", language="de").token_file is None


def test_token_file_parses_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKIDOUGH_TOKEN_FILE", "/tmp/cookidoo-token.json")
    settings = _settings(country="de", language="de")
    assert settings.token_file is not None
    assert settings.token_file.name == "cookidoo-token.json"


def test_is_china_market_tracks_the_country_code() -> None:
    assert _settings(country="cn", language="zh-Hans-CN").is_china_market is True
    assert _settings(country="CN", language="zh-Hans-CN").is_china_market is True
    assert _settings(country="de", language="de").is_china_market is False
