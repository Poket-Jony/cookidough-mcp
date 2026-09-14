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

# Maximum number of concurrent recipe-detail fetches issued by
# ``suggest_recipes_from_ingredients``. Larger collections would otherwise
# serialize N HTTP round-trips through ``get_recipe_details`` and easily blow
# past the MCP client's tool-call timeout. 5 keeps per-call latency low without
# hammering the Cookidoo API.
SUGGEST_RECIPE_FETCH_CONCURRENCY: Final[int] = 5

# Minimum length of an ``available_ingredients`` token in the suggestion tool.
# The matcher uses a bidirectional ``substring`` containment which trivially
# matches very short tokens against unrelated ingredient names (``"oil"`` →
# ``"soil"``, ``"egg"`` → ``"eggplant"``). Three chars is a pragmatic floor
# that still lets common short head nouns like ``"rice"`` / ``"salt"`` through.
SUGGEST_MIN_INGREDIENT_LENGTH: Final[int] = 3

# How many search candidates the library-wide suggestion path requests, as a
# multiple of ``max_results``. The local scorer re-ranks by ingredient match,
# so it needs more candidates than results — 3x gives it slack without
# fetching details for the whole result set.
SUGGEST_SEARCH_CANDIDATE_FACTOR: Final[int] = 3

# Upper bound for the calendar→shopping-list date range. Four weeks covers a
# realistic meal-plan horizon; anything larger is more likely a typo'd date
# and would fan out into dozens of write calls against the shopping list.
CALENDAR_SHOPPING_MAX_RANGE_DAYS: Final[int] = 28

# Custom-recipe image upload (verified against the live flow, 2026-06-05):
# Cookidoo signs {timestamp, upload_preset, source} via its /image/signature
# endpoint, the file goes directly to Vorwerk's Cloudinary tenant, and the
# returned public_id is PATCHed onto the recipe. The api_key is Cloudinary's
# public identifier for that tenant — not a secret.
CLOUDINARY_UPLOAD_URL: Final[str] = (
    "https://api-eu.cloudinary.com/v1_1/vorwerk-users-gc/image/upload"
)
CLOUDINARY_UPLOAD_PRESET: Final[str] = "prod-customer-recipe-signed"
CLOUDINARY_UPLOAD_SOURCE: Final[str] = "uw"
CLOUDINARY_API_KEY: Final[str] = "993585863591145"

# Upload cap for recipe images. Cookidoo's own web client accepts photos in
# this range; the moderation pipeline additionally requires >=80px per side.
MAX_RECIPE_IMAGE_BYTES: Final[int] = 10 * 1024 * 1024

# Mainland China runs a separate deployment behind its own identity provider,
# so none of the global hosts resolve for it. Selected by COOKIDOUGH_COUNTRY=cn.
CHINA_COUNTRY_CODE: Final[str] = "cn"
CHINA_LANGUAGE_CODE: Final[str] = "zh-Hans-CN"
CHINA_COOKIDOO_ORIGIN: Final[str] = "https://cookidoo.com.cn"
CHINA_CIAM_BASE_URL: Final[str] = "https://ciam.production-cn.cookidoo.tmecosys.cn"
CHINA_OIDC_DISCOVERY_URL: Final[str] = f"{CHINA_CIAM_BASE_URL}/.well-known/openid-configuration"

# China moderates recipe images asynchronously; 60 s total covers the queue's
# typical 20-40 s without hanging the tool call.
CHINA_IMAGE_AUDIT_POLL_ATTEMPTS: Final[int] = 30
CHINA_IMAGE_AUDIT_POLL_INTERVAL_SECONDS: Final[float] = 2.0
CHINA_COS_TIMEOUT_SECONDS: Final[int] = 120
