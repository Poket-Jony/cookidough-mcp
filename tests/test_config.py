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
