"""Tests for the editor value limits and how inference, validation and quality use them."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from cookidough_mcp.annotation_models import StepAnnotation
from cookidough_mcp.annotations import AnnotationInferrer, RejectedAnnotation
from cookidough_mcp.editor_rules import (
    EditorVariant,
    annotation_problem,
    editor_variant,
    off_time_grid,
)
from cookidough_mcp.models import CustomRecipeDraft, QualityIssueSeverity, RecipeStep
from cookidough_mcp.quality import EditorLimitsRule
from cookidough_mcp.session import _draft_to_payload

_ANNOTATION = TypeAdapter(StepAnnotation)
_FIXTURE = Path(__file__).parent / "fixtures" / "editor_dialog_annotations.json"
_EDITOR_CASES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _annotation(type_: str, data: dict[str, Any], name: str | None = None) -> StepAnnotation:
    raw: dict[str, Any] = {"type": type_, "data": data, "offset": 0, "length": 5}
    if name is not None:
        raw["name"] = name
    return _ANNOTATION.validate_python(raw)


_EDITOR_CASE_IDS = [str(case["annotation"].get("name", "tts")) for case in _EDITOR_CASES]


@pytest.mark.parametrize("case", _EDITOR_CASES, ids=_EDITOR_CASE_IDS)
def test_everything_the_editor_produces_passes(case: dict[str, Any]) -> None:
    annotation = _ANNOTATION.validate_python(case["annotation"])

    assert annotation_problem(annotation, editor_variant(case["tools"])) is None
    step = RecipeStep(text=case["text"], annotations=[annotation])
    CustomRecipeDraft(name="X", ingredients=["1 Zwiebel"], steps=[step], tools=case["tools"])


_TM6, _TM7 = EditorVariant.TM6, EditorVariant.TM7

INVALID: list[tuple[EditorVariant, StepAnnotation, str]] = [
    (_TM6, _annotation("TTS", {"speed": "1", "time": 6, "temperature": {"value": "42"}}), "42 °C"),
    (_TM6, _annotation("TTS", {"speed": "1", "time": 6, "temperature": {"value": "160"}}), "160"),
    (_TM6, _annotation("TTS", {"speed": "11", "time": 60}), "speed '11'"),
    (_TM7, _annotation("TTS", {"speed": "anbrat", "time": 300}), "Anbratstufe"),
    (
        _TM6,
        _annotation("TTS", {"speed": "7", "time": 60, "temperature": {"value": "100"}}),
        "up to speed 6",
    ),
    (_TM6, _annotation("TTS", {"speed": "1", "time": 6000}), "99 min"),
    (
        _TM7,
        _annotation("TTS", {"speed": "1", "time": 60, "temperature": {"value": "varoma"}}),
        "MODE/steaming",
    ),
    (_TM6, _annotation("MODE", {"time": 3}, "turbo"), "0.5, 1 or 2 s"),
    (_TM6, _annotation("MODE", {"time": 1, "pulseCount": 3}, "turbo"), "TM7 editor only"),
    (_TM7, _annotation("MODE", {"time": 1, "pulseCount": 12}, "turbo"), "pulse count"),
    (_TM6, _annotation("MODE", {"speed": "10", "time": 60}, "blend"), "blend speed"),
    (_TM6, _annotation("MODE", {"speed": "8", "time": 5}, "blend"), "blend time"),
    (_TM6, _annotation("MODE", {"time": 1500}, "dough"), "dough time"),
    (
        _TM6,
        _annotation("MODE", {"speed": "1", "temperature": {"value": "100"}}, "warm_up"),
        "warm-up temperature",
    ),
    (
        _TM6,
        _annotation("MODE", {"speed": "3", "temperature": {"value": "65"}}, "warm_up"),
        "warm-up speed",
    ),
    (_TM7, _annotation("MODE", {"time": 60, "speed": "6"}, "steaming"), "steaming speed"),
    (_TM6, _annotation("MODE", {"time": 60, "speed": "1"}, "steaming"), "TM7 editor only"),
    (
        _TM7,
        _annotation(
            "MODE",
            {"time": 60, "temperature": {"value": "170"}, "power": "Gentle"},
            "browning",
        ),
        "browning temperature",
    ),
]


@pytest.mark.parametrize(("variant", "annotation", "reason"), INVALID)
def test_annotation_problem_explains_what_the_editor_cannot_do(
    variant: EditorVariant, annotation: StepAnnotation, reason: str
) -> None:
    problem = annotation_problem(annotation, variant)

    assert problem is not None
    assert reason in problem


def test_soft_speed_may_heat() -> None:
    annotation = _annotation("TTS", {"speed": "soft", "time": 60, "temperature": {"value": "100"}})

    assert annotation_problem(annotation, _TM6) is None


@pytest.mark.parametrize(
    ("seconds", "off_grid"),
    [(45, False), (61, True), (70, False), (300, False), (315, True), (330, False), (1260, False)],
)
def test_time_grid(seconds: int, off_grid: bool) -> None:
    assert off_time_grid(seconds) is off_grid


def test_inferrer_drops_settings_the_editor_cannot_produce() -> None:
    inferrer = AnnotationInferrer.for_tools(["TM6"])

    annotations, rejected = inferrer.infer_checked("Milch 1 Min./42 °C/Stufe 1 erwärmen.", [])

    assert annotations == []
    assert rejected == [RejectedAnnotation(span="1 Min./42 °C/Stufe 1", reason=rejected[0].reason)]
    assert "42 °C" in rejected[0].reason


def test_anbratstufe_is_not_uploaded() -> None:
    draft = CustomRecipeDraft(
        name="Zwiebeln",
        ingredients=["1 Zwiebel"],
        steps=[RecipeStep(text="1 Zwiebel 5 Min./120 °C/Anbratstufe dünsten.")],
    )

    annotations = _draft_to_payload(draft)["instructions"][0].get("annotations", [])

    assert [annotation["type"] for annotation in annotations] == ["INGREDIENT"]


def test_explicit_annotation_outside_the_limits_is_rejected() -> None:
    annotation = _ANNOTATION.validate_python(
        {"type": "TTS", "data": {"speed": "1", "time": 60, "temperature": {"value": "42"}},
         "offset": 6, "length": 19}
    )  # fmt: skip

    with pytest.raises(ValidationError, match=r"'1 Min\./42°C/Stufe 1'.*42 °C"):
        CustomRecipeDraft(
            name="X",
            ingredients=["1 l Milch"],
            steps=[
                RecipeStep(text="Milch 1 Min./42°C/Stufe 1 erwärmen.", annotations=[annotation])
            ],
        )


def test_quality_rule_reports_dropped_settings_and_off_grid_times() -> None:
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["1 l Milch"],
        steps=[
            RecipeStep(text="Milch 1 Min./42 °C/Stufe 1 erwärmen."),
            RecipeStep(text="Dann 7 Min. 15 Sek./100 °C/Stufe 1 köcheln."),
        ],
        tools=["TM6"],
    )

    result = EditorLimitsRule().evaluate(draft)

    assert [(issue.step_index, issue.severity) for issue in result.issues] == [
        (0, QualityIssueSeverity.WARNING),
        (1, QualityIssueSeverity.INFO),
    ]
    assert result.deduction == EditorLimitsRule.weight
