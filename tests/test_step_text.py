"""Plain-text rendering of Cookidoo's ``formattedText`` recipe steps."""

from __future__ import annotations

import logging

import pytest

from cookidough_mcp.step_text import (
    DOUGH_MODE_GLYPH,
    REVERSE_BLADE_GLYPH,
    SOFT_SPEED_GLYPH,
    plain_step_text,
)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("fr-FR", "mixez 30 sec/sens inverse/vitesse 5."),
        ("de-DE", "mixez 30 sec/Linkslauf/vitesse 5."),
        # Unknown language falls back to English rather than leaking a glyph.
        ("sv-SE", "mixez 30 sec/reverse/vitesse 5."),
    ],
)
def test_reverse_glyph_takes_the_slot_it_sits_in(language: str, expected: str) -> None:
    formatted = f"mixez <nobr>30 sec/{REVERSE_BLADE_GLYPH}/vitesse 5</nobr>."
    assert plain_step_text(formatted, language) == expected


def test_soft_speed_glyph_keeps_the_space_before_it() -> None:
    formatted = f"cuire <nobr>5 min/120°C/vitesse {SOFT_SPEED_GLYPH}</nobr>."
    assert plain_step_text(formatted, "fr-FR") == "cuire 5 min/120°C/vitesse mijotage."


def test_dough_mode_glyph_is_dropped_with_its_leading_space() -> None:
    # The mode is already named in words right before the icon; repeating it
    # would read as a second instruction.
    formatted = f"activez le mode <nobr>Pétrin {DOUGH_MODE_GLYPH}/40 sec</nobr>."
    assert plain_step_text(formatted, "fr-FR") == "activez le mode Pétrin/40 sec."


def test_unknown_private_use_glyph_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert plain_step_text("versez  la crème") == "versez la crème"
    assert "U+E0FF" in caplog.text


def test_markup_is_stripped_and_entities_resolved() -> None:
    formatted = "<strong>Insérez le fouet</strong><br>Caf&eacute; &amp; lait"
    assert plain_step_text(formatted) == "Insérez le fouet\nCafé & lait"


def test_markup_only_step_renders_empty() -> None:
    assert plain_step_text("<nobr></nobr>  ") == ""
