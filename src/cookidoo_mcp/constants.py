"""Project-wide constants shared between modules.

Centralized to avoid magic strings or numbers leaking into business logic. See
each constant's comment for the reasoning behind its value.
"""

from typing import Final, Literal

# Thermomix device generations that Cookidoo accepts in the recipe ``tools``
# field. The field is a compatibility list (which TM models the recipe is
# designed for), not a list of accessories. Only these three tokens are valid
# in the wire payload — anything else is silently dropped by Cookidoo and
# excludes the recipe from model-specific filters.
ThermomixTool = Literal["TM5", "TM6", "TM7"]

DEFAULT_THERMOMIX_TOOLS: Final[tuple[ThermomixTool, ...]] = ("TM7", "TM6", "TM5")

# Cookidoo's `/created-recipes/{locale}` endpoint is eventually consistent: a
# PATCH issued immediately after the POST tends to race the backend's draft
# materialization. Empirically 3 s is enough to make the PATCH succeed reliably
# without a polling loop. Bump this if 5xx errors start appearing again.
CUSTOM_RECIPE_PROPAGATION_DELAY_SECONDS: Final[float] = 3.0

# Hard upper bound for any single HTTP call against Cookidoo. The MCP transport
# layer expects tool calls to return promptly; without an explicit timeout
# aiohttp would inherit a 5-minute default that masquerades as a hung tool.
HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

# Hard upper bound around each of the two HTTP steps in ``upload_custom_recipe``
# (POST stub, PATCH content). Without it, a combination of slow login, 401
# retry-with-re-login and slow upstream response can sum up well past 3
# minutes and trip Claude Desktop's 4-minute MCP-client timeout. 60 s is
# enough for login (~2 s) + POST/PATCH (~1 s) with comfortable headroom but
# bails out fast when something is actually wrong.
CUSTOM_RECIPE_OPERATION_TIMEOUT_SECONDS: Final[float] = 60.0
