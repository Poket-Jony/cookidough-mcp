"""Entrypoint that selects the configured transport and runs the server."""

from __future__ import annotations

import logging
import sys

from .config import Settings
from .server import build_server
from .transport import transport_from_settings


def main() -> None:
    _configure_logging()
    settings = Settings.from_env()
    server = build_server(settings)
    transport = transport_from_settings(settings)
    transport.run(server)


def _configure_logging() -> None:
    """Send our INFO+ logs to stderr; mute chatty upstream libraries.

    Stdio MCP transports share stdout with the MCP protocol stream, so any
    stray log line on stdout would corrupt the wire format. ``basicConfig``
    defaults to stderr but we set it explicitly to make the contract visible.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("aiohttp", "aiohttp.access", "aiohttp.client", "cookidoo_api"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
