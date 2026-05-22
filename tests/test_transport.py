"""Tests for the transport strategy selection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from cookidoo_mcp.config import Settings, TransportMode
from cookidoo_mcp.transport import (
    HttpTransport,
    StdioTransport,
    transport_from_settings,
)


def _make_settings(mode: TransportMode) -> Settings:
    return Settings(
        email="x@example.com",
        password=SecretStr("pw"),
        mcp_mode=mode,
        mcp_host="0.0.0.0",
        mcp_port=12345,
    )


def test_transport_factory_returns_stdio() -> None:
    transport = transport_from_settings(_make_settings(TransportMode.STDIO))
    assert isinstance(transport, StdioTransport)


def test_transport_factory_returns_http_with_settings() -> None:
    transport = transport_from_settings(_make_settings(TransportMode.HTTP))
    assert isinstance(transport, HttpTransport)
    assert transport.host == "0.0.0.0"
    assert transport.port == 12345


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (StdioTransport(), "stdio"),
        (HttpTransport(host="127.0.0.1", port=9000), "streamable-http"),
    ],
)
def test_transport_invokes_fastmcp_with_correct_kind(
    transport: StdioTransport | HttpTransport, expected: str
) -> None:
    mcp = MagicMock()
    if isinstance(transport, HttpTransport):
        mcp.settings = MagicMock()
    transport.run(mcp)
    mcp.run.assert_called_once_with(transport=expected)
