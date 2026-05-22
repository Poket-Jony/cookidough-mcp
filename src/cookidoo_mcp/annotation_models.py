"""Cookidoo guided-cooking annotation DTOs.

Split out of ``models.py`` because the annotation domain has its own
sprawl of enums, mode-specific ``data`` submodels, the discriminated
``StepAnnotation`` union and the callable Pydantic ``Discriminator``. The
remaining tool-IO DTOs live in ``models.py``.

The classes here are self-contained: they do not import from ``models.py``
to avoid a circular dependency. The internal ``_AnnotationModel`` base
mirrors the ``extra='ignore'`` config used by tool-IO DTOs.

``from __future__ import annotations`` is intentionally omitted — Pydantic 2
and FastMCP introspect concrete types at runtime to build JSON schemas and
the discriminated-union dispatch table.

Adding a new ``MODE`` variant (when Cookidoo introduces one) takes four
changes in this file:

1. Add the lowercase token to ``ModeName``.
2. Add a ``XxxModeData`` Pydantic submodel for the new ``data`` shape.
3. Add a ``XxxModeAnnotation`` class with the matching ``name`` literal.
4. Add an ``Annotated[XxxModeAnnotation, Tag(f"MODE:{ModeName.XXX}")]`` arm
   to the ``StepAnnotation`` discriminated union.

Then add a ``model_validate`` round-trip case to
``test_step_annotation_discriminator_dispatches_every_mode``.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_serializer,
)


class StepAnnotationType(StrEnum):
    """Cookidoo guided-cooking annotation type. Values mirror the upstream API."""

    TTS = "TTS"
    INGREDIENT = "INGREDIENT"
    MODE = "MODE"


class ModeName(StrEnum):
    """Sub-type discriminator for ``MODE`` annotations.

    Values mirror the lowercase tokens Cookidoo's frontend sends in the
    ``annotations[].name`` field.
    """

    BROWNING = "browning"
    STEAMING = "steaming"
    DOUGH = "dough"
    TURBO = "turbo"
    RICE_COOKER = "rice_cooker"
    WARM_UP = "warm_up"
    BLEND = "blend"


class BrowningPower(StrEnum):
    """Heat level for the Thermomix browning mode."""

    INTENSE = "Intense"
    GENTLE = "Gentle"


class SteamingAccessory(StrEnum):
    """Accessory used during a steaming mode span."""

    VAROMA = "Varoma"
    SIMMERING_BASKET = "SimmeringBasket"
    VAROMA_AND_SIMMERING_BASKET = "VaromaAndSimmeringBasket"


class MixDirection(StrEnum):
    """Mixing-blade rotation direction.

    ``CW`` (clockwise) is the default; ``CCW`` (counter-clockwise) is used
    for the "Linkslauf" / "sens inverse" mode.
    """

    CLOCKWISE = "CW"
    COUNTER_CLOCKWISE = "CCW"


class _AnnotationModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _AnnotationDataModel(_AnnotationModel):
    """Base for annotation ``data`` submodels.

    Strips ``None`` fields on serialization so FastMCP tool responses match
    the Cookidoo wire format, which omits absent optional fields rather
    than emitting ``null``.
    """

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: Any) -> Any:
        result = handler(self)
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if v is not None}
        return result


class TemperatureData(_AnnotationDataModel):
    """Temperature value with its unit, as Cookidoo emits it."""

    value: str = Field(min_length=1)
    unit: Literal["C"] = "C"


class TtsAnnotationData(_AnnotationDataModel):
    """Payload of a TTS annotation: Thermomix speed level and duration."""

    speed: str = Field(min_length=1)
    time: int = Field(gt=0)
    temperature: TemperatureData | None = None
    direction: MixDirection | None = None


class IngredientAnnotationData(_AnnotationDataModel):
    """Payload of an INGREDIENT annotation: the referenced ingredient text."""

    description: str = Field(min_length=1)


class BrowningModeData(_AnnotationDataModel):
    """Payload of a ``MODE/browning`` annotation."""

    time: int = Field(gt=0)
    temperature: TemperatureData
    power: BrowningPower


class SteamingModeData(_AnnotationDataModel):
    """Payload of a ``MODE/steaming`` annotation."""

    time: int = Field(gt=0)
    speed: str = Field(min_length=1)
    direction: MixDirection = MixDirection.CLOCKWISE
    accessory: SteamingAccessory = SteamingAccessory.VAROMA


class DoughModeData(_AnnotationDataModel):
    """Payload of a ``MODE/dough`` annotation."""

    time: int = Field(gt=0)


class TurboModeData(_AnnotationDataModel):
    """Payload of a ``MODE/turbo`` annotation.

    ``time`` is a float because Cookidoo emits sub-second pulse durations
    (e.g. ``0.5``); ``pulseCount`` is the number of repeated bursts.
    """

    time: float = Field(gt=0)
    pulseCount: int = Field(ge=1)  # noqa: N815 — matches Cookidoo wire field


class RiceCookerModeData(_AnnotationDataModel):
    """Payload of a ``MODE/rice_cooker`` annotation. Always empty."""


class WarmUpModeData(_AnnotationDataModel):
    """Payload of a ``MODE/warm_up`` annotation.

    ``time`` is optional — Cookidoo emits warm-up spans without a duration
    when the user did not specify one in the source recipe.
    """

    speed: str = Field(min_length=1)
    temperature: TemperatureData
    time: int | None = Field(default=None, gt=0)


class BlendModeData(_AnnotationDataModel):
    """Payload of a ``MODE/blend`` annotation."""

    speed: str = Field(min_length=1)
    time: int = Field(gt=0)


class _AnnotationBase(_AnnotationModel):
    offset: int = Field(ge=0)
    length: int = Field(gt=0)


class TtsAnnotation(_AnnotationBase):
    """Thermomix instruction span (time/speed) that the app can dispatch."""

    type: Literal[StepAnnotationType.TTS] = StepAnnotationType.TTS
    data: TtsAnnotationData


class IngredientAnnotation(_AnnotationBase):
    """Ingredient reference span highlighted in the step text."""

    type: Literal[StepAnnotationType.INGREDIENT] = StepAnnotationType.INGREDIENT
    data: IngredientAnnotationData


class BrowningModeAnnotation(_AnnotationBase):
    """Browning-mode span (time + temperature + power level)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.BROWNING] = ModeName.BROWNING
    data: BrowningModeData


class SteamingModeAnnotation(_AnnotationBase):
    """Varoma steaming-mode span (time + speed + accessory)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.STEAMING] = ModeName.STEAMING
    data: SteamingModeData


class DoughModeAnnotation(_AnnotationBase):
    """Dough-kneading mode span (timed only)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.DOUGH] = ModeName.DOUGH
    data: DoughModeData


class TurboModeAnnotation(_AnnotationBase):
    """Turbo (high-speed pulse) mode span."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.TURBO] = ModeName.TURBO
    data: TurboModeData


class RiceCookerModeAnnotation(_AnnotationBase):
    """Rice-cooker mode span (no parameters in ``data``)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.RICE_COOKER] = ModeName.RICE_COOKER
    data: RiceCookerModeData = Field(default_factory=RiceCookerModeData)


class WarmUpModeAnnotation(_AnnotationBase):
    """Warm-up mode span (speed + temperature, time optional)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.WARM_UP] = ModeName.WARM_UP
    data: WarmUpModeData


class BlendModeAnnotation(_AnnotationBase):
    """Blending mode span (speed + time)."""

    type: Literal[StepAnnotationType.MODE] = StepAnnotationType.MODE
    name: Literal[ModeName.BLEND] = ModeName.BLEND
    data: BlendModeData


_UNKNOWN_ANNOTATION_TAG = "<unknown>"


def _annotation_discriminator(value: Any) -> str:
    """Compute the discriminator tag from ``type`` plus ``name`` for MODE."""
    if isinstance(value, dict):
        annotation_type = value.get("type")
        if annotation_type == StepAnnotationType.MODE:
            return f"MODE:{value.get('name')}"
        return str(annotation_type) if annotation_type is not None else _UNKNOWN_ANNOTATION_TAG
    annotation_type = getattr(value, "type", None)
    if annotation_type == StepAnnotationType.MODE:
        return f"MODE:{getattr(value, 'name', None)}"
    return str(annotation_type) if annotation_type is not None else _UNKNOWN_ANNOTATION_TAG


StepAnnotation = Annotated[
    Annotated[TtsAnnotation, Tag(StepAnnotationType.TTS)]
    | Annotated[IngredientAnnotation, Tag(StepAnnotationType.INGREDIENT)]
    | Annotated[BrowningModeAnnotation, Tag(f"MODE:{ModeName.BROWNING}")]
    | Annotated[SteamingModeAnnotation, Tag(f"MODE:{ModeName.STEAMING}")]
    | Annotated[DoughModeAnnotation, Tag(f"MODE:{ModeName.DOUGH}")]
    | Annotated[TurboModeAnnotation, Tag(f"MODE:{ModeName.TURBO}")]
    | Annotated[RiceCookerModeAnnotation, Tag(f"MODE:{ModeName.RICE_COOKER}")]
    | Annotated[WarmUpModeAnnotation, Tag(f"MODE:{ModeName.WARM_UP}")]
    | Annotated[BlendModeAnnotation, Tag(f"MODE:{ModeName.BLEND}")],
    Discriminator(_annotation_discriminator),
]
