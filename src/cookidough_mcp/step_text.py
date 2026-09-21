"""Plain-text rendering of Cookidoo's formatted recipe steps.

Catalogue steps arrive as ``formattedText``: presentational HTML
(``<nobr>``, ``<strong>``) wrapped around Thermomix notation whose icons
live in the Unicode private-use area. Neither survives the trip to an MCP
client — the icons render as empty boxes and the markup is pure noise.
``plain_step_text`` maps the icons back to the words Cookidoo itself uses
when it spells the notation out, then strips the markup.

Vorwerk publishes no legend for those code points, so the meanings below
were read off the payloads. Across 25 recipes / 120 steps of the fr-FR
catalogue:

- ``U+E003`` occurs in 18 steps (15 %), always in the slot between the
  temperature and the speed — ``30 sec/<glyph>/vitesse 5``. That is the
  slot Cookidoo fills with ``Linkslauf`` / ``sens inverse`` when it uses
  words. The 15 % rate rules out a marker carried by nearly every step
  (the measuring cup), and five of those steps say "sans le gobelet
  doseur" in prose, which would contradict that reading outright.
- ``U+E002`` occurs in 9 steps, always directly after a bare
  ``vitesse`` / ``Stufe`` carrying no number — the shape the soft speed
  ("Sanftrührstufe", "vitesse mijotage") takes.
- ``U+E001`` occurs in 2 steps, always right after the dough mode has
  already been named in words ("activez le mode Pétrin <glyph>/40 sec"),
  so it is dropped rather than duplicated.

Unknown private-use code points are dropped and logged: an empty box in a
recipe step is worse than a missing icon.
"""

from __future__ import annotations

import html
import logging
import re

_LOGGER = logging.getLogger(__name__)

DOUGH_MODE_GLYPH = "\ue001"
SOFT_SPEED_GLYPH = "\ue002"
REVERSE_BLADE_GLYPH = "\ue003"

DEFAULT_LANGUAGE = "en"

# Labels are picked so the rewritten notation is the one ``annotations.py``
# parses back: "/sens inverse/vitesse 5", "Stufe sanft", "speed soft".
_GLYPH_LABELS: dict[str, dict[str, str]] = {
    SOFT_SPEED_GLYPH: {"en": "soft", "de": "sanft", "fr": "mijotage"},
    REVERSE_BLADE_GLYPH: {"en": "reverse", "de": "Linkslauf", "fr": "sens inverse"},
}

# Spelled out by the surrounding sentence in every payload seen so far.
_DROPPED_GLYPHS = frozenset({DOUGH_MODE_GLYPH})

# The optional leading blank is captured with the glyph so a dropped icon
# doesn't leave "Pétrin /40 sec" behind. Newlines are excluded so a step
# that starts on a fresh line keeps its break.
_PRIVATE_USE = re.compile(r"(?P<space>[^\S\r\n]?)(?P<glyph>[\ue000-\uf8ff])")
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACE_RUN = re.compile(r"[ \t]{2,}")


def plain_step_text(formatted_text: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Render one ``formattedText`` as plain text for an MCP client.

    ``language`` accepts a BCP-47 tag or a bare language code; only the
    primary subtag is used, and an unknown one falls back to English.
    """
    primary = language.partition("-")[0].lower() or DEFAULT_LANGUAGE

    def _replace(match: re.Match[str]) -> str:
        glyph = match.group("glyph")
        if glyph in _DROPPED_GLYPHS:
            return ""
        labels = _GLYPH_LABELS.get(glyph)
        if labels is None:
            _LOGGER.warning("Dropping unknown Thermomix glyph U+%04X", ord(glyph))
            return ""
        return match.group("space") + labels.get(primary, labels[DEFAULT_LANGUAGE])

    text = _PRIVATE_USE.sub(_replace, formatted_text)
    text = _LINE_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _SPACE_RUN.sub(" ", text)
    return text.strip()
