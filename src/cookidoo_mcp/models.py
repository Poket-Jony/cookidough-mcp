"""Data transfer objects exchanged at the MCP tool boundary.

Annotation-specific DTOs (``StepAnnotation``, the mode-specific data
submodels, the discriminated union etc.) live in
``annotation_models.py`` and are imported from there directly.
"""

from enum import StrEnum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .annotation_models import StepAnnotation
from .constants import DEFAULT_THERMOMIX_TOOLS, ThermomixTool


class ShoppingItemSource(StrEnum):
    """Origin of a shopping-list item."""

    RECIPE = "recipe"
    ADDITIONAL = "additional"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class UserProfile(_Model):
    username: str
    description: str | None = None
    picture: str | None = None


class Subscription(_Model):
    active: bool
    status: str
    subscription_level: str
    subscription_source: str
    type: str
    # Trial accounts and some legacy upstream rows return ``null`` for the
    # detail fields below — treating them as required would crash the whole
    # ``get_subscription`` call with a ``ValidationError``.
    extended_type: str | None = None
    start_date: str | None = None
    expires: str | None = None


class Ingredient(_Model):
    id: str = Field(min_length=1)
    name: str
    description: str | None = None


class RecipeDetails(_Model):
    id: str = Field(min_length=1)
    name: str
    url: str
    thumbnail: str | None = None
    image: str | None = None
    difficulty: str | None = None
    # Cookidoo occasionally returns ``null`` for serving size / time fields
    # (no-cook recipes, legacy entries). Stay tolerant at the boundary so a
    # quirky upstream payload doesn't sink the whole request.
    serving_size: int | None = None
    active_time_seconds: int | None = None
    total_time_seconds: int | None = None
    utensils: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    ingredients: list[Ingredient] = Field(default_factory=list)


class CustomRecipeSummary(_Model):
    recipe_id: str = Field(min_length=1)
    name: str
    created_at: str | None = None
    total_time_seconds: int | None = None
    servings: int | None = None


class CustomRecipeDetails(_Model):
    id: str = Field(min_length=1)
    name: str
    url: str
    serving_size: int | None = None
    active_time_seconds: int | None = None
    total_time_seconds: int | None = None
    tools: list[str] = Field(default_factory=list)
    ingredients: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    thumbnail: str | None = None
    image: str | None = None


class CollectionSummary(_Model):
    id: str = Field(min_length=1)
    name: str
    description: str | None = None
    chapter_count: int = 0
    recipe_count: int = 0


class ShoppingListItem(_Model):
    id: str = Field(min_length=1)
    name: str
    description: str | None = None
    is_owned: bool = False
    source: ShoppingItemSource


class ShoppingList(_Model):
    ingredient_items: list[ShoppingListItem] = Field(default_factory=list)
    additional_items: list[ShoppingListItem] = Field(default_factory=list)


class CalendarRecipe(_Model):
    id: str = Field(min_length=1)
    name: str
    total_time_seconds: int | None = None
    url: str
    thumbnail: str | None = None
    image: str | None = None


class CalendarDay(_Model):
    id: str = Field(min_length=1)
    title: str
    recipes: list[CalendarRecipe] = Field(default_factory=list)
    custom_recipe_ids: list[str] = Field(default_factory=list)


class RecipeStep(_Model):
    text: str = Field(min_length=1)
    annotations: list[StepAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _annotations_within_text(self) -> Self:
        text_length = len(self.text)
        for annotation in self.annotations:
            end = annotation.offset + annotation.length
            if end > text_length:
                raise ValueError(
                    f"Annotation span [{annotation.offset}, {end}) exceeds step "
                    f"text length {text_length}."
                )
        return self


class CustomRecipeDraft(_Model):
    name: str = Field(min_length=1)
    ingredients: list[str] = Field(min_length=1)
    steps: list[RecipeStep] = Field(min_length=1)
    servings: int = Field(default=4, ge=1, le=99)
    prep_minutes: int = Field(default=30, ge=0, le=24 * 60)
    total_minutes: int = Field(default=60, ge=0, le=24 * 60)
    tools: list[ThermomixTool] = Field(
        default_factory=lambda: list(DEFAULT_THERMOMIX_TOOLS),
        description=(
            "Thermomix device generations the recipe is compatible with. "
            "Allowed values: 'TM5', 'TM6', 'TM7' — nothing else. "
            "This field is NOT a list of accessories used inside the bowl "
            "(Mixtopf, Spatel, Messbecher, Varoma, Schmetterling, ...); "
            "those belong in the step text and are rejected here. "
            "Pick the lowest TM generation that can actually run every "
            "step: TM5 has no Sanftrührstufe (speed='soft') and no "
            "browning/steaming/dough/warm_up/blend/turbo/rice_cooker MODE; "
            "rice_cooker is TM7-only. When in doubt list multiple "
            "(e.g. ['TM7', 'TM6'])."
        ),
    )
    hints: list[str] = Field(default_factory=list)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_step_strings(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [{"text": item} if isinstance(item, str) else item for item in value]

    @model_validator(mode="after")
    def _total_must_include_prep(self) -> Self:
        if self.total_minutes < self.prep_minutes:
            raise ValueError(
                "total_minutes must be greater than or equal to prep_minutes "
                f"(got total={self.total_minutes}, prep={self.prep_minutes})."
            )
        return self

    @property
    def step_texts(self) -> list[str]:
        return [step.text for step in self.steps]


class QualityIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QualityIssue(_Model):
    step_index: int | None = Field(default=None, ge=0)
    rule: str
    severity: QualityIssueSeverity
    message: str


class QualityReport(_Model):
    score: int = Field(ge=0, le=100)
    threshold: int = Field(ge=0, le=100)
    meets_bar: bool
    issues: list[QualityIssue] = Field(default_factory=list)


class UploadResult(_Model):
    recipe_id: str = Field(min_length=1)
    url: str
    quality: QualityReport


class ShoppingItemOwnershipUpdate(_Model):
    """Pair of (item id, new owned/checked state) used by the ownership tools."""

    id: str = Field(min_length=1)
    is_owned: bool


class AdditionalItemRename(_Model):
    """Pair of (additional-item id, new label) used by ``rename_additional_items``."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class RecipeSearchResult(_Model):
    """Single result from the Cookidoo recipe search."""

    id: str = Field(min_length=1)
    name: str
    rating: float | None = None
    number_of_ratings: int | None = None
    total_time_seconds: int | None = None
    image: str | None = None


class RecipeSuggestion(_Model):
    """A recipe ranked by ingredient match for ``suggest_recipes_from_ingredients``."""

    recipe: RecipeDetails
    score: float = Field(ge=0.0, le=1.0)
    matching_ingredients: list[str] = Field(default_factory=list)
    missing_ingredients: list[str] = Field(default_factory=list)
    total_ingredients: int = Field(ge=0)


class WebImportResult(_Model):
    """Outcome of `import_web_recipe`.

    ``draft`` and ``quality`` are always populated, even when the quality bar
    blocks the upload — that way the LLM caller can read the scraped recipe,
    rework the steps into Thermomix guided-cooking annotations, and resubmit via
    ``upload_custom_recipe``. ``upload`` is populated only when the gate
    passed or the call was forced.
    """

    draft: CustomRecipeDraft
    quality: QualityReport
    upload: UploadResult | None = None
    blocked_reason: str | None = None
