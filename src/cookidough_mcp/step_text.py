"""Plain-text rendering of Cookidoo's ``formattedText`` recipe steps.

Catalogue steps are presentational HTML around Thermomix notation whose
icons live in the Unicode private-use area. Vorwerk publishes no legend for
those code points; the mapping below was read off 120 fr-FR catalogue steps:
``U+E003`` always sits in the reverse-blade slot (``30 sec/<glyph>/vitesse 5``),
``U+E002`` always follows a bare ``vitesse``/``Stufe`` (soft speed), and
``U+E001`` always follows the dough mode already named in words.
"""

import html
import logging
import re

_LOGGER = logging.getLogger(__name__)

DOUGH_MODE_GLYPH = "\ue001"
SOFT_SPEED_GLYPH = "\ue002"
REVERSE_BLADE_GLYPH = "\ue003"

_DEFAULT_LANGUAGE = "en"

# Cookidoo's own wording when it spells the notation out.
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
    """Strip the markup and spell out the Thermomix icons in ``language``."""
    primary = language.partition("-")[0].lower()

    def _replace(match: re.Match[str]) -> str:
        glyph = match.group("glyph")
        if glyph == DOUGH_MODE_GLYPH:
            return ""
        labels = _GLYPH_LABELS.get(glyph)
        if labels is None:
            _LOGGER.warning("Dropping unknown Thermomix glyph U+%04X", ord(glyph))
            return ""
        return match.group("space") + labels.get(primary, labels[_DEFAULT_LANGUAGE])

    text = _PRIVATE_USE.sub(_replace, formatted_text)
    text = _TAG.sub("", _LINE_BREAK.sub("\n", text))
    return _SPACE_RUN.sub(" ", html.unescape(text)).strip()
