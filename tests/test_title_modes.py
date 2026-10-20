"""Tests for modes written as "<name>/<values>" in plain step text."""

import json
from pathlib import Path
from typing import Any

import pytest

from cookidough_mcp.annotation_models import StepAnnotationType
from cookidough_mcp.annotations import AnnotationInferrer, TitleModeStrategy

_FIXTURE = Path(__file__).parent / "fixtures" / "editor_dialog_annotations.json"
_EDITOR_CASES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]

# How each fixture step is written in plain text, in fixture order.
_WRITTEN = [
    "Teig Teig kneten/2 Min. fertig.",
    "Zerkleinern Turbo/1 Sek. fertig.",
    "Fein Pürieren/1 Min./Stufe 8 fertig.",
    "Suppe Erwärmen/65°C/Stufe 1 fertig.",
    "Reis [Reis kochen] fertig.",
    "Gemüse 20 Min./Varoma/Linkslauf/Stufe 1 fertig.",
    "Gemüse Dampfgaren/20 Min./Stufe 1/Linkslauf/im Gareinsatz fertig.",
    "Zwiebeln Anbraten/10 Min./160°C/Intensiv fertig.",
    "Nüsse Turbo/1 Sek./3x fertig.",
    "Suppe Erwärmen/70°C/Sanftrührstufe fertig.",
]


def test_every_fixture_case_has_a_written_form() -> None:
    assert len(_WRITTEN) == len(_EDITOR_CASES)


@pytest.mark.parametrize(("text", "case"), list(zip(_WRITTEN, _EDITOR_CASES, strict=True)))
def test_written_modes_give_what_the_editor_produces(text: str, case: dict[str, Any]) -> None:
    expected = case["annotation"]

    annotations, rejected = AnnotationInferrer.for_tools(case["tools"]).infer_checked(text, [])

    assert rejected == []
    assert len(annotations) == 1
    annotation = annotations[0]
    assert annotation.type == expected["type"]
    assert getattr(annotation, "name", None) == expected.get("name")
    assert annotation.data.model_dump(mode="json") == expected["data"]


@pytest.mark.parametrize(
    "text",
    [
        "Den Reis kochen lassen.",
        "Turbo ist zu stark.",
        "Reis kochen",
        "Teig kneten, bis er glatt ist.",
    ],
)
def test_names_without_values_stay_text(text: str) -> None:
    assert TitleModeStrategy().detect(text, []) == []


def test_missing_required_value_gives_no_mode() -> None:
    assert TitleModeStrategy().detect("Pürieren/1 Min. bis glatt.", []) == []


def test_english_names_are_recognised() -> None:
    result = TitleModeStrategy().detect("Blend/1 min/speed 7.5 until smooth.", [])

    assert len(result) == 1
    assert result[0].data.model_dump(mode="json") == {"speed": "7.5", "time": 60}


def test_half_second_turbo() -> None:
    result = TitleModeStrategy().detect("Turbo/0,5 Sek. hacken.", [])

    assert result[0].data.model_dump(mode="json") == {"time": 0.5}


def test_mode_unavailable_for_the_editor_leaves_no_tts_behind() -> None:
    inferrer = AnnotationInferrer.for_tools(["TM6"])

    annotations, rejected = inferrer.infer_checked("Dampfgaren/20 Min./Stufe 1 garen.", [])

    assert [a for a in annotations if a.type != StepAnnotationType.INGREDIENT] == []
    assert len(rejected) == 1
    assert "TM7 editor only" in rejected[0].reason


def test_turbo_pulses_need_the_tm7_editor() -> None:
    tm6, rejected = AnnotationInferrer.for_tools(["TM6"]).infer_checked("Turbo/1 Sek./3x", [])
    tm7, _ = AnnotationInferrer.for_tools(["TM7"]).infer_checked("Turbo/1 Sek./3x", [])

    assert tm6 == []
    assert "TM7 editor only" in rejected[0].reason
    assert tm7[0].data.model_dump(mode="json") == {"time": 1.0, "pulseCount": 3}
