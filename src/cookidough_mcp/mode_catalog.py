"""The modes the platform's recipe editor offers, read from its settings dialog.

The editor page ``/created-recipes/{language}/{id}/edit/ingredients-and-preparation-steps``
is rendered on the server and carries the settings dialog as plain HTML: every
mode in ``<cr-tts-mode>`` (value, localised ``header-title``, and ``tm7-mode`` on
the label of TM7-only modes), the speeds, temperatures, turbo times, pulse counts,
steaming accessories and browning powers. Reading it lets the server follow the
platform when a mode is added, renamed or removed. Ranges per mode are not part of
the HTML; they live in :mod:`.editor_rules`.
"""

from html.parser import HTMLParser

from .annotation_models import ModeName
from .editor_rules import (
    BLEND_SPEEDS,
    BLEND_TIME_SECONDS,
    BROWNING_TEMPERATURES,
    DOUGH_TIME_SECONDS,
    STEAMING_SPEEDS,
    TTS_TEMPERATURES,
    VAROMA_TEMPERATURE,
    WARM_UP_SPEEDS,
    WARM_UP_TEMPERATURES,
    EditorVariant,
    mode_available,
)
from .models import AvailableModes, ModeCatalog, ModeSpelling, ThermomixMode

_SECTIONS = frozenset(
    {
        "cr-tts-mode",
        "cr-tts-speed",
        "cr-tts-temperature",
        "cr-tts-time",
        "cr-tts-pulse-count",
        "cr-tts-steaming-accessory",
        "cr-tts-browning-power",
    }
)


class _EditorDialogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section: str | None = None
        self.radios: dict[str, list[tuple[str, str, str]]] = {name: [] for name in _SECTIONS}
        self.tm7_labels: set[str] = set()
        self._last_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag in _SECTIONS:
            self.section = tag
        elif tag == "input" and self.section and attributes.get("type") == "radio":
            title = attributes.get("header-title") or self._last_text.strip()
            self.radios[self.section].append(
                (attributes.get("value", ""), title, attributes.get("id", ""))
            )
        elif tag == "label" and "tm7-mode" in attributes:
            self.tm7_labels.add(attributes.get("for", ""))
        if tag == "span":
            self._last_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == self.section:
            self.section = None

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._last_text = data


def parse_editor_html(html: str) -> ModeCatalog:
    """Read the mode catalog from the editor page.

    Raises:
        ValueError: If the page carries no settings dialog (e.g. a login page).
    """
    parser = _EditorDialogParser()
    parser.feed(html)
    modes = parser.radios["cr-tts-mode"]
    if not modes:
        raise ValueError("The recipe editor page contains no Thermomix modes.")

    def values(section: str) -> list[str]:
        return [value for value, _, _ in parser.radios[section]]

    def titled(section: str) -> dict[str, str]:
        return {value: title for value, title, _ in parser.radios[section]}

    return ModeCatalog(
        modes=[
            ThermomixMode(name=value, title=title, tm7_only=element_id in parser.tm7_labels)
            for value, title, element_id in modes
        ],
        speeds=[v for v in values("cr-tts-speed") if v not in ("CW", "CCW")],
        temperatures=[v for v in values("cr-tts-temperature") if v != "OFF"],
        turbo_times=values("cr-tts-time"),
        pulse_counts=values("cr-tts-pulse-count"),
        steaming_accessories=titled("cr-tts-steaming-accessory"),
        browning_powers=titled("cr-tts-browning-power"),
        source="live",
    )


BUILTIN_CATALOG = ModeCatalog(
    modes=[
        ThermomixMode(name="dough", title="Teig kneten", tm7_only=False),
        ThermomixMode(name="browning", title="Anbraten", tm7_only=True),
        ThermomixMode(name="turbo", title="Turbo", tm7_only=False),
        ThermomixMode(name="steaming", title="Dampfgaren", tm7_only=True),
        ThermomixMode(name="blend", title="Pürieren", tm7_only=False),
        ThermomixMode(name="warm_up", title="Erwärmen", tm7_only=False),
        ThermomixMode(name="rice_cooker", title="Reis kochen", tm7_only=False),
    ],
    speeds=["soft", *(str(n / 2).removesuffix(".0") for n in range(1, 21))],
    temperatures=[*TTS_TEMPERATURES, *BROWNING_TEMPERATURES, VAROMA_TEMPERATURE],
    turbo_times=["0.5", "1", "2"],
    pulse_counts=[str(n) for n in range(1, 10)],
    steaming_accessories={
        "Varoma": "im Varoma",
        "SimmeringBasket": "im Gareinsatz",
        "VaromaAndSimmeringBasket": "in beidem",
    },
    browning_powers={"Gentle": "Leicht", "Intense": "Intensiv"},
    source="builtin",
)
"""Snapshot of cookidoo.de (de-DE) on 2026-10-20, used when the page cannot be read."""

_SPELLING: dict[ModeName, tuple[str, str]] = {
    ModeName.DOUGH: (
        "{title}/2 Min.",
        f"{DOUGH_TIME_SECONDS[0]} s to {DOUGH_TIME_SECONDS[1] // 60} min",
    ),
    ModeName.TURBO: ("{title}/1 Sek.", "0.5, 1 or 2 s; TM7 also '/3x' pulses (1-9)"),
    ModeName.BLEND: (
        "{title}/1 Min./Stufe 8",
        f"speed {', '.join(BLEND_SPEEDS)}; {BLEND_TIME_SECONDS[0]} s to "
        f"{BLEND_TIME_SECONDS[1] // 60} min",
    ),
    ModeName.WARM_UP: (
        "{title}/65°C/Stufe 1",
        f"{WARM_UP_TEMPERATURES[0]}-{WARM_UP_TEMPERATURES[-1]} °C; speed "
        f"{', '.join(WARM_UP_SPEEDS)} (default soft)",
    ),
    ModeName.RICE_COOKER: ("[{title}]", "no values"),
    ModeName.STEAMING: (
        "{title}/20 Min./Stufe 1/im Varoma",
        f"speed {STEAMING_SPEEDS[0]}-{STEAMING_SPEEDS[-1]}, optional Linkslauf; im Varoma "
        "(default), im Gareinsatz or in beidem; up to 99 min",
    ),
    ModeName.BROWNING: (
        "{title}/10 Min./160°C/Leicht",
        f"{BROWNING_TEMPERATURES[0]}-{BROWNING_TEMPERATURES[-1]} °C in 5 °C steps; "
        "up to 30 min (default 10); Leicht (default) or Intensiv",
    ),
}


def available_modes(catalog: ModeCatalog, variant: EditorVariant) -> AvailableModes:
    """Split the catalog into the modes usable in ``variant`` and those that are not."""
    known = {mode.value for mode in ModeName}
    spellings: list[ModeSpelling] = []
    unavailable: list[str] = []
    unsupported: list[str] = []
    for mode in catalog.modes:
        if mode.name not in known:
            unsupported.append(mode.title)
            continue
        name = ModeName(mode.name)
        if not mode_available(name, variant) or (
            mode.tm7_only and variant is not EditorVariant.TM7
        ):
            unavailable.append(mode.title)
            continue
        example, limits = _SPELLING[name]
        spellings.append(
            ModeSpelling(
                name=name, title=mode.title, example=example.format(title=mode.title), limits=limits
            )
        )
    return AvailableModes(
        editor_variant="TM7" if variant is EditorVariant.TM7 else "TM6",
        modes=spellings,
        unavailable=unavailable,
        unsupported=unsupported,
        source=catalog.source,
    )


__all__ = ["BUILTIN_CATALOG", "available_modes", "parse_editor_html"]
