"""Plain-text rendering of the ``formattedText`` catalogue recipe steps."""

import logging

import pytest

from cookidough_mcp.step_text import (
    REVERSE_BLADE_GLYPH,
    SOFT_SPEED_GLYPH,
    plain_step_text,
)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("fr-FR", "mixez 30 sec/sens inverse/vitesse 5."),
        ("de-DE", "mixez 30 sec/Linkslauf/vitesse 5."),
        ("sv-SE", "mixez 30 sec/reverse/vitesse 5."),
    ],
)
def test_reverse_glyph_is_spelled_out_in_the_account_language(language: str, expected: str) -> None:
    formatted = f"mixez <nobr>30 sec/{REVERSE_BLADE_GLYPH}/vitesse 5</nobr>."
    assert plain_step_text(formatted, language) == expected


def test_soft_speed_glyph_keeps_the_space_before_it() -> None:
    formatted = f"cuire <nobr>5 min/120°C/vitesse {SOFT_SPEED_GLYPH}</nobr>."
    assert plain_step_text(formatted, "fr-FR") == "cuire 5 min/120°C/vitesse mijotage."


@pytest.mark.parametrize(
    ("formatted", "language", "expected"),
    [
        (
            "activez le mode <nobr>Pétrin \ue001/40 sec</nobr>.",
            "fr-FR",
            "activez le mode Pétrin/40 sec.",
        ),
        (
            "Zucker zugeben, <nobr>Anbraten \ue026</nobr> und umfüllen.",
            "de-DE",
            "Zucker zugeben, Anbraten und umfüllen.",
        ),
        (
            "Nudeln vermischen und <nobr>Slow Cooking \ue018/40 Min./95°C</nobr>.",
            "de-DE",
            "Nudeln vermischen und Slow Cooking/40 Min./95°C.",
        ),
    ],
)
def test_mode_glyph_is_dropped_silently_with_its_leading_space(
    formatted: str, language: str, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert plain_step_text(formatted, language) == expected
    assert not caplog.records


def test_unknown_glyph_outside_a_mode_position_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert plain_step_text("mixez 30 sec/\ue0ff/vitesse 5") == "mixez 30 sec//vitesse 5"
    assert "U+E0FF" in caplog.text


def test_markup_is_stripped_and_entities_resolved() -> None:
    formatted = "<strong>Insérez le fouet</strong><br>Caf&eacute; &amp; lait"
    assert plain_step_text(formatted) == "Insérez le fouet\nCafé & lait"
