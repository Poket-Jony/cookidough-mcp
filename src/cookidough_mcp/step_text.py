"""Plain-text rendering of the ``formattedText`` catalogue recipe steps.

Catalogue steps are presentational HTML around the appliance's step notation, whose
icons live in the Unicode private-use area. Vorwerk publishes no legend for
those code points; the mapping below was read off 120 fr-FR catalogue steps:
``U+E003`` always sits in the reverse-blade slot (``30 sec/<glyph>/vitesse 5``)
and ``U+E002`` always follows a bare ``vitesse``/``Stufe`` (soft speed). Every
other glyph seen after a blank is a mode icon behind the mode already named in
words (``Pétrin``, ``Anbraten``, ``Slow Cooking``, ``Vorspülen``, ...), so it is
dropped silently; an unknown glyph anywhere else is dropped and logged.
"""

import html
import logging
import re

_LOGGER = logging.getLogger(__name__)

SOFT_SPEED_GLYPH = "\ue002"
REVERSE_BLADE_GLYPH = "\ue003"

_DEFAULT_LANGUAGE = "en"

_GLYPH_LABELS: dict[str, dict[str, str]] = {
    SOFT_SPEED_GLYPH: {"en": "soft", "de": "sanft", "fr": "mijotage"},
    REVERSE_BLADE_GLYPH: {"en": "reverse", "de": "Linkslauf", "fr": "sens inverse"},
}

# The leading blank goes with the glyph so a dropped icon leaves no "Pétrin /40 sec".
_PRIVATE_USE = re.compile(r"(?P<space>[^\S\r\n]?)(?P<glyph>[\ue000-\uf8ff])")
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACE_RUN = re.compile(r"[ \t]{2,}")


def plain_step_text(formatted_text: str, language: str = _DEFAULT_LANGUAGE) -> str:
    primary = language.partition("-")[0].lower()

    def _replace(match: re.Match[str]) -> str:
        glyph, space = match.group("glyph"), match.group("space")
        labels = _GLYPH_LABELS.get(glyph)
        if labels is not None:
            return space + labels.get(primary, labels[_DEFAULT_LANGUAGE])
        if not space:
            _LOGGER.warning("Dropping unknown appliance glyph U+%04X", ord(glyph))
        return ""

    text = _PRIVATE_USE.sub(_replace, formatted_text)
    text = _TAG.sub("", _LINE_BREAK.sub("\n", text))
    return _SPACE_RUN.sub(" ", html.unescape(text)).strip()
