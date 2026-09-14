"""Runtime configuration loaded from environment variables."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import CHINA_COUNTRY_CODE


class TransportMode(StrEnum):
    """Supported MCP transports."""

    STDIO = "stdio"
    HTTP = "http"


class Settings(BaseSettings):
    """Server configuration."""

    model_config = SettingsConfigDict(
        env_prefix="COOKIDOUGH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    email: str = Field(
        min_length=3,
        description="Cookidoo account email, or a phone number when country='cn'.",
    )
    password: SecretStr = Field(description="Cookidoo account password.")
    country: str = Field(
        default="de",
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 country code; case-insensitive.",
    )
    language: str = Field(
        default="de",
        min_length=2,
        description=(
            "ISO 639-1 short form (e.g. 'de') paired with ``country`` into a "
            "BCP-47 tag, or an explicit BCP-47 tag ('de-DE'). Case is "
            "normalized to ``lang-REGION``."
        ),
    )

    mcp_mode: TransportMode = TransportMode.STDIO
    mcp_host: str = "127.0.0.1"
    mcp_port: Annotated[int, Field(gt=0, lt=65536)] = 8765

    token_file: Path | None = Field(
        default=None,
        description=(
            "Optional path for persisting the OAuth2 tokens across restarts, "
            "skipping the login while the refresh token is still valid. The "
            "file contains live credentials — treat it like a password."
        ),
    )

    quality_bar: Annotated[int, Field(ge=0, le=100)] = 70

    @classmethod
    def from_env(cls) -> Self:
        """Build settings purely from environment variables.

        Wraps the implicit `cls()` call so the unavoidable ``type: ignore`` for
        pydantic-settings' env-driven instantiation lives in exactly one place.
        """
        return cls()  # type: ignore[call-arg]

    @property
    def country_code(self) -> str:
        return self.country.lower()

    @property
    def is_china_market(self) -> bool:
        """Whether this account lives on Cookidoo's separate China deployment."""
        return self.country_code == CHINA_COUNTRY_CODE

    @property
    def language_code(self) -> str:
        """Return language as a BCP-47 ``lang-REGION`` tag.

        Cookidoo's locale lookup is case-sensitive — only ``de-DE`` matches,
        ``de`` and ``de-de`` do not. We canonicalize whatever the user
        provided so the documented short form (``de``) keeps working, while
        explicit BCP-47 tags (``de-DE``, ``en-GB``) survive unchanged.
        """
        raw = self.language
        if "-" in raw:
            primary, _, region = raw.partition("-")
            return f"{primary.lower()}-{region.upper()}"
        return f"{raw.lower()}-{self.country.upper()}"
