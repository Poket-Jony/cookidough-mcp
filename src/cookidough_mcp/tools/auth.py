"""Account / identity tools.

Login itself is performed lazily on the first call to any session-backed tool
(``CookidooSession._ensure_logged_in`` is invoked by ``_run`` and
``_authed_http``), so there is no separate "connect" step exposed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..models import Subscription, UserProfile

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_user_profile(ctx: ToolContext) -> UserProfile:
        """Return the authenticated user's Cookidoo profile.

        Calling this also triggers the lazy login on the first invocation,
        so it doubles as a "credentials still work?" probe.
        """
        return await get_context(ctx).session.get_user_profile()

    @mcp.tool()
    async def get_subscription(ctx: ToolContext) -> Subscription | None:
        """Return the active Cookidoo subscription, or null if none is active."""
        return await get_context(ctx).session.get_subscription()
