"""Device rules and value limits of the platform's recipe editor.

The web editor exists in two variants. As soon as a recipe's ``tools`` contain
``"TM7"`` it runs as TM7 editor (``cr-edit-tabs[tm7-editor]``); for every other
combination, ``["TM5"]`` included, it runs as TM6 editor. The variants differ in
what a step can carry (verified against the editor page and its settings dialog,
``pl-customer-recipes`` 1.174.12, on 2026-10-20):

- ``browning`` and ``steaming`` are offered by the TM7 editor only (their mode
  labels carry ``tm7-mode`` and are hidden otherwise).
- ``Varoma`` is a TTS temperature in the TM6 editor only; the TM7 editor offers
  the ``steaming`` mode instead.

The value limits below are the choices the settings dialog offers. The API checks
only some of them itself (an unknown TTS temperature or speed, or a turbo time of
3 s, fail the whole PATCH with 400; a blend speed of 10 is stored as is), so
annotations are checked here before upload.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from .annotation_models import (
    BlendModeAnnotation,
    BrowningModeAnnotation,
    DoughModeAnnotation,
    ModeName,
    SteamingModeAnnotation,
    StepAnnotation,
    TtsAnnotation,
    TurboModeAnnotation,
    WarmUpModeAnnotation,
)

VAROMA_TEMPERATURE = "varoma"


class EditorVariant(StrEnum):
    """Variant of the platform's recipe editor a recipe is edited in."""

    TM6 = "TM6"
    TM7 = "TM7"


TM7_ONLY_MODES: Final[frozenset[ModeName]] = frozenset({ModeName.BROWNING, ModeName.STEAMING})

SPEEDS: Final[tuple[str, ...]] = (
    "soft", "0.5", "1", "1.5", "2", "2.5", "3", "3.5", "4", "4.5", "5",
    "5.5", "6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10",
)  # fmt: skip
TTS_TEMPERATURES: Final[tuple[str, ...]] = (
    "37", "40", "45", "50", "55", "60", "65", "70", "75", "80", "85", "90",
    "95", "98", "100", "105", "110", "115", "120",
)  # fmt: skip
MAX_HEATING_SPEED: Final[float] = 6.0
MAX_TIME_SECONDS: Final[int] = 99 * 60

BLEND_SPEEDS: Final[tuple[str, ...]] = ("6", "6.5", "7", "7.5", "8")
BLEND_TIME_SECONDS: Final[tuple[int, int]] = (10, 300)
DOUGH_TIME_SECONDS: Final[tuple[int, int]] = (1, 1200)
TURBO_TIMES_SECONDS: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)
TURBO_PULSE_COUNTS: Final[tuple[int, int]] = (1, 9)
WARM_UP_SPEEDS: Final[tuple[str, ...]] = ("soft", "1", "2")
WARM_UP_TEMPERATURES: Final[tuple[str, ...]] = TTS_TEMPERATURES[: TTS_TEMPERATURES.index("90") + 1]
STEAMING_SPEEDS: Final[tuple[str, ...]] = SPEEDS[: SPEEDS.index("5") + 1]
BROWNING_TEMPERATURES: Final[tuple[str, ...]] = ("140", "145", "150", "155", "160")
BROWNING_TIME_SECONDS: Final[tuple[int, int]] = (1, 1800)

# Time grid the editor rounds to: seconds up to 1 min, 10 s steps up to 5 min,
# 30 s steps up to 20 min, whole minutes above.
_TIME_GRID: Final[tuple[tuple[int, int], ...]] = ((60, 1), (300, 10), (1200, 30), (5940, 60))


def editor_variant(tools: Iterable[str]) -> EditorVariant:
    """Return the editor variant the platform uses for a recipe with these ``tools``."""
    return EditorVariant.TM7 if "TM7" in tools else EditorVariant.TM6


def mode_available(name: ModeName, variant: EditorVariant) -> bool:
    """Whether the editor variant offers the ``MODE`` annotation ``name``."""
    return variant is EditorVariant.TM7 or name not in TM7_ONLY_MODES


def varoma_is_temperature(variant: EditorVariant) -> bool:
    """Whether ``Varoma`` is a TTS temperature (TM6 editor) rather than steaming (TM7)."""
    return variant is EditorVariant.TM6


def off_time_grid(seconds: int) -> bool:
    """Whether the editor would round ``seconds`` to a different time."""
    for limit, step in _TIME_GRID:
        if seconds <= limit:
            return seconds % step != 0
    return True


def annotation_problem(annotation: StepAnnotation, variant: EditorVariant) -> str | None:
    """Explain why the editor cannot produce ``annotation``, or ``None`` if it can."""
    if isinstance(annotation, TtsAnnotation):
        return _tts_problem(annotation, variant)
    mode = getattr(annotation, "name", None)
    if isinstance(mode, ModeName) and not mode_available(mode, variant):
        return (
            f"the {mode} MODE exists in the TM7 editor only; "
            "add 'TM7' to tools or use a TTS annotation instead"
        )
    return _mode_problem(annotation, variant)


def _tts_problem(annotation: TtsAnnotation, variant: EditorVariant) -> str | None:
    data = annotation.data
    if data.speed not in SPEEDS:
        if data.speed == "anbrat":
            return (
                "there is no 'Anbratstufe' speed; on the TM7 use the browning MODE "
                "('<n> Min./<°C>/Leicht' or '/Intensiv')"
            )
        return f"speed {data.speed!r} does not exist (allowed: {', '.join(SPEEDS)})"
    if data.time > MAX_TIME_SECONDS:
        return "the time can be at most 99 min"
    temperature = data.temperature.value if data.temperature is not None else None
    if temperature == VAROMA_TEMPERATURE:
        if not varoma_is_temperature(variant):
            return (
                "Varoma is not a TTS temperature in the TM7 editor (tools contain 'TM7'); "
                "use a MODE/steaming annotation instead"
            )
    elif temperature is not None and temperature not in TTS_TEMPERATURES:
        return (
            f"{temperature} °C is not a temperature the appliance offers "
            f"(allowed: {', '.join(TTS_TEMPERATURES)} °C and Varoma)"
        )
    if temperature is not None and data.speed != "soft" and float(data.speed) > MAX_HEATING_SPEED:
        return "heating is only possible up to speed 6"
    return None


def _mode_problem(annotation: StepAnnotation, variant: EditorVariant) -> str | None:
    if isinstance(annotation, DoughModeAnnotation):
        return _range_problem("dough time", annotation.data.time, DOUGH_TIME_SECONDS)
    if isinstance(annotation, TurboModeAnnotation):
        if annotation.data.time not in TURBO_TIMES_SECONDS:
            return "the turbo time must be 0.5, 1 or 2 s"
        pulses = annotation.data.pulseCount
        if pulses is None:
            return None
        if variant is not EditorVariant.TM7:
            return "turbo pulses exist in the TM7 editor only; leave pulseCount out"
        return _range_problem("turbo pulse count", pulses, TURBO_PULSE_COUNTS)
    if isinstance(annotation, BlendModeAnnotation):
        if annotation.data.speed not in BLEND_SPEEDS:
            return f"the blend speed must be one of {', '.join(BLEND_SPEEDS)}"
        return _range_problem("blend time", annotation.data.time, BLEND_TIME_SECONDS)
    if isinstance(annotation, WarmUpModeAnnotation):
        if annotation.data.speed not in WARM_UP_SPEEDS:
            return f"the warm-up speed must be one of {', '.join(WARM_UP_SPEEDS)}"
        if annotation.data.temperature.value not in WARM_UP_TEMPERATURES:
            return f"the warm-up temperature must be one of {', '.join(WARM_UP_TEMPERATURES)} °C"
        return None
    if isinstance(annotation, SteamingModeAnnotation):
        if annotation.data.speed not in STEAMING_SPEEDS:
            return f"the steaming speed must be one of {', '.join(STEAMING_SPEEDS)}"
        return _range_problem("steaming time", annotation.data.time, (1, MAX_TIME_SECONDS))
    if isinstance(annotation, BrowningModeAnnotation):
        if annotation.data.temperature.value not in BROWNING_TEMPERATURES:
            return f"the browning temperature must be one of {', '.join(BROWNING_TEMPERATURES)} °C"
        return _range_problem("browning time", annotation.data.time, BROWNING_TIME_SECONDS)
    return None


def _range_problem(label: str, value: float, bounds: tuple[int, int]) -> str | None:
    low, high = bounds
    if low <= value <= high:
        return None
    return f"the {label} must be between {low} and {high}"


__all__ = [
    "BLEND_SPEEDS",
    "BROWNING_TEMPERATURES",
    "SPEEDS",
    "TM7_ONLY_MODES",
    "TTS_TEMPERATURES",
    "VAROMA_TEMPERATURE",
    "EditorVariant",
    "annotation_problem",
    "editor_variant",
    "mode_available",
    "off_time_grid",
    "varoma_is_temperature",
]
