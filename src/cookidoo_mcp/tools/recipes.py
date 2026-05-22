"""Recipe tools: lookup, custom-recipe lifecycle, and web import.

Combines public Cookidoo recipes (``get_recipe_details``) with the full
custom-recipe workflow (``generate`` → ``validate`` → ``upload``, plus list /
delete and the recipe-scrapers-backed ``import_web_recipe``). All tools in
this module live under the "Recipes" section in the README's tool reference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import ThermomixTool
from ..context import AppContext, ToolContext, get_context
from ..errors import QualityGateError
from ..models import (
    CustomRecipeDetails,
    CustomRecipeDraft,
    CustomRecipeSummary,
    QualityReport,
    RecipeDetails,
    RecipeStep,
    UploadResult,
    WebImportResult,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_recipe_details(ctx: ToolContext, recipe_id: str) -> RecipeDetails:
        """Fetch full details of a Cookidoo recipe by its ID."""
        return await get_context(ctx).session.get_recipe_details(recipe_id)

    @mcp.tool()
    async def get_custom_recipe_details(ctx: ToolContext, recipe_id: str) -> CustomRecipeDetails:
        """Fetch full details of the authenticated user's custom recipe by its ID."""
        return await get_context(ctx).session.get_custom_recipe_details(recipe_id)

    @mcp.tool()
    async def generate_recipe_structure(
        ctx: ToolContext,
        name: str,
        ingredients: list[str],
        steps: list[str | RecipeStep],
        servings: int = 4,
        prep_minutes: int = 30,
        total_minutes: int = 60,
        tools: list[ThermomixTool] | None = None,
        hints: list[str] | None = None,
    ) -> CustomRecipeDraft:
        """Build a validated custom-recipe draft ready for quality check and upload.

        ``tools`` lists the Thermomix device generations the recipe is
        compatible with — allowed values are ``"TM5"``, ``"TM6"``, ``"TM7"``
        and nothing else. It is NOT a list of bowl accessories (Mixtopf,
        Spatel, Varoma, Schmetterling, ...). Pick the lowest TM model that
        can still run every step (e.g. ``Sanftrührstufe`` / ``speed="soft"``
        requires TM6+, ``rice_cooker`` mode is TM7-only). Default: all three.

        Each step may be either a plain string or a ``RecipeStep`` carrying
        explicit ``annotations``. Supported annotation kinds:

        - ``TTS`` (time/speed instruction, optionally with temperature and
          rotation direction)
        - ``INGREDIENT`` (highlights a span as an ingredient reference)
        - ``MODE`` with ``name`` ∈ {``browning``, ``steaming``, ``dough``,
          ``turbo``, ``rice_cooker``, ``warm_up``, ``blend``}; each mode has
          its own ``data`` shape.

        Plain strings are kept as-is and the server infers ``TTS`` (incl.
        ``speed="soft"`` from ``Sanftrührstufe`` / ``Stufe sanft``),
        ``INGREDIENT``, ``MODE/browning``, ``MODE/steaming`` and
        ``MODE/dough`` (from ``Teigstufe`` / ``dough mode``) spans on
        upload. The remaining MODE kinds (``turbo``, ``rice_cooker``,
        ``warm_up``, ``blend``) must be supplied explicitly. See the
        README's "Guided-cooking annotations" section for the full ``data``
        schemas.
        """
        del ctx  # required-but-unused FastMCP tool argument
        payload: dict[str, Any] = {
            "name": name,
            "ingredients": ingredients,
            "steps": steps,
            "servings": servings,
            "prep_minutes": prep_minutes,
            "total_minutes": total_minutes,
            "hints": hints or [],
        }
        if tools is not None:
            payload["tools"] = tools
        return CustomRecipeDraft.model_validate(payload)

    @mcp.tool()
    async def validate_recipe_quality(ctx: ToolContext, draft: CustomRecipeDraft) -> QualityReport:
        """Score a draft against the TM7 guided-cooking quality bar without uploading."""
        return get_context(ctx).scorer.score(draft)

    @mcp.tool()
    async def upload_custom_recipe(
        ctx: ToolContext, draft: CustomRecipeDraft, force: bool = False
    ) -> UploadResult:
        """Upload a custom recipe; refuses when below the configured quality bar.

        Pass ``force=true`` only when the user has explicitly accepted a
        sub-threshold upload. Failed uploads are rolled back automatically.

        For draft construction, see ``generate_recipe_structure`` or the
        README's "Guided-cooking annotations" section — every annotation
        kind (TTS, INGREDIENT, all seven MODE variants) is accepted here
        verbatim and forwarded to Cookidoo.
        """
        app = get_context(ctx)
        report = app.scorer.score(draft)
        if not report.meets_bar and not force:
            raise QualityGateError(
                (
                    f"Quality score {report.score} is below the threshold "
                    f"{report.threshold}. Pass force=true to override."
                ),
                score=report.score,
                threshold=report.threshold,
            )
        recipe_id, url = await app.session.upload_custom_recipe(draft)
        return UploadResult(recipe_id=recipe_id, url=url, quality=report)

    @mcp.tool()
    async def list_custom_recipes(ctx: ToolContext) -> list[CustomRecipeSummary]:
        """List all custom recipes owned by the authenticated user."""
        return await get_context(ctx).session.list_custom_recipes()

    @mcp.tool()
    async def delete_custom_recipe(ctx: ToolContext, recipe_id: str) -> str:
        """Delete a custom recipe by its ID."""
        await get_context(ctx).session.delete_custom_recipe(recipe_id)
        return f"Deleted custom recipe {recipe_id}."

    @mcp.tool()
    async def import_web_recipe(
        ctx: ToolContext,
        url: str,
        name_override: str | None = None,
        force: bool = False,
    ) -> WebImportResult:
        """Scrape a recipe from a supported website and return it as a draft.

        The scraped ``draft`` and ``quality`` report are **always** returned,
        even when the quality bar blocks the upload. That lets the caller —
        typically an LLM — read the recipe, rewrite the step text into TM7
        guided-cooking annotations (e.g. "5 min / 100 °C / speed 3"), and
        resubmit via ``upload_custom_recipe``.

        When the gate passes (or ``force=true`` is set), the recipe is also
        uploaded and ``upload`` is populated. Otherwise ``upload`` is null
        and ``blocked_reason`` explains what to do next.
        """
        app = get_context(ctx)
        draft = await app.importer.fetch(url, name_override)
        return await _score_and_maybe_upload(app, draft=draft, force=force)


async def _score_and_maybe_upload(
    app: AppContext, *, draft: CustomRecipeDraft, force: bool
) -> WebImportResult:
    report = app.scorer.score(draft)
    if not report.meets_bar and not force:
        return WebImportResult(
            draft=draft,
            quality=report,
            upload=None,
            blocked_reason=(
                f"Quality score {report.score} is below the threshold "
                f"{report.threshold}. The scraped draft is returned for "
                f"editing — rewrite the steps with TM7 guided-cooking "
                f"annotations and resubmit via upload_custom_recipe, or "
                f"call import_web_recipe again with force=true to upload "
                f"the draft as-is."
            ),
        )
    recipe_id, public_url = await app.session.upload_custom_recipe(draft)
    return WebImportResult(
        draft=draft,
        quality=report,
        upload=UploadResult(recipe_id=recipe_id, url=public_url, quality=report),
    )
