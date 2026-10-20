"""Device rules of the platform's recipe editor for guided-cooking annotations.

The web editor exists in two variants. As soon as a recipe's ``tools`` contain
``"TM7"`` it runs as TM7 editor (``cr-edit-tabs[tm7-editor]``); for every other
combination, ``["TM5"]`` included, it runs as TM6 editor. The variants differ in
what a step can carry (verified against the editor page and its settings dialog,
``pl-customer-recipes`` 1.174.12, on 2026-10-20):

- ``browning`` and ``steaming`` are offered by the TM7 editor only (their mode
  labels carry ``tm7-mode`` and are hidden otherwise).
- ``Varoma`` is a TTS temperature in the TM6 editor only; the TM7 editor offers
  the ``steaming`` mode instead.
"""

from collections.abc import Iterable
from enum import StrEnum

from .annotation_models import ModeName

VAROMA_TEMPERATURE = "varoma"


class EditorVariant(StrEnum):
    """Variant of the platform's recipe editor a recipe is edited in."""

    TM6 = "TM6"
    TM7 = "TM7"


TM7_ONLY_MODES: frozenset[ModeName] = frozenset({ModeName.BROWNING, ModeName.STEAMING})


def editor_variant(tools: Iterable[str]) -> EditorVariant:
    """Return the editor variant the platform uses for a recipe with these ``tools``."""
    return EditorVariant.TM7 if "TM7" in tools else EditorVariant.TM6


def mode_available(name: ModeName, variant: EditorVariant) -> bool:
    """Whether the editor variant offers the ``MODE`` annotation ``name``."""
    return variant is EditorVariant.TM7 or name not in TM7_ONLY_MODES


def varoma_is_temperature(variant: EditorVariant) -> bool:
    """Whether ``Varoma`` is a TTS temperature (TM6 editor) rather than steaming (TM7)."""
    return variant is EditorVariant.TM6


__all__ = [
    "TM7_ONLY_MODES",
    "VAROMA_TEMPERATURE",
    "EditorVariant",
    "editor_variant",
    "mode_available",
    "varoma_is_temperature",
]
