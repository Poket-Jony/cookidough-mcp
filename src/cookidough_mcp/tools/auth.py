"""Account / identity tools.

Login itself is performed lazily on the first call to any session-backed tool
(``CookidoughSession._ensure_logged_in`` is invoked by ``_run`` and
``_authed_http``), so there is no separate "connect" step exposed here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..context import ToolContext, get_context
from ..models import Subscription, UserProfile

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    async def get_user_profile(ctx: ToolContext, include_devices: bool = False) -> UserProfile:
        """Return the authenticated user's profile.

        Calling this also triggers the lazy login on the first invocation,
        so it doubles as a "credentials still work?" probe.

        With ``include_devices=true`` the profile also lists the
        ``devices`` and ``accessories`` linked to the account — useful for
        picking matching ``thermomix_version``/``accessories`` search filters.
        """
        session = get_context(ctx).session
        if not include_devices:
            return await session.get_user_profile()
        profile, (devices, accessories) = await asyncio.gather(
            session.get_user_profile(),
            session.get_user_devices(),
        )
        return profile.model_copy(update={"devices": devices, "accessories": accessories})

    @mcp.tool()
    async def get_subscription(ctx: ToolContext) -> Subscription | None:
        """Return the active subscription, or null if none is active."""
        return await get_context(ctx).session.get_subscription()
