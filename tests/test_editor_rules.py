"""Tests for the editor-variant device rules and their use in inference and validation."""

import pytest
from pydantic import ValidationError

from cookidough_mcp.annotation_models import (
    BrowningModeAnnotation,
    BrowningModeData,
    BrowningPower,
    ModeName,
    RiceCookerModeAnnotation,
    SteamingModeAnnotation,
    SteamingModeData,
    TemperatureData,
    TtsAnnotation,
    TtsAnnotationData,
)
from cookidough_mcp.annotations import AnnotationInferrer, TtsStrategy
from cookidough_mcp.editor_rules import (
    EditorVariant,
    editor_variant,
    mode_available,
    varoma_is_temperature,
)
from cookidough_mcp.models import CustomRecipeDraft, RecipeStep
from cookidough_mcp.session import _draft_to_payload


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["TM7", "TM6", "TM5"], EditorVariant.TM7),
        (["TM6", "TM7"], EditorVariant.TM7),
        (["TM7"], EditorVariant.TM7),
        (["TM6"], EditorVariant.TM6),
        (["TM6", "TM5"], EditorVariant.TM6),
        (["TM5"], EditorVariant.TM6),
    ],
)
def test_editor_variant_follows_tm7_in_tools(tools: list[str], expected: EditorVariant) -> None:
    assert editor_variant(tools) is expected


def test_browning_and_steaming_exist_in_tm7_editor_only() -> None:
    for mode in ModeName:
        assert mode_available(mode, EditorVariant.TM7)
    assert not mode_available(ModeName.BROWNING, EditorVariant.TM6)
    assert not mode_available(ModeName.STEAMING, EditorVariant.TM6)
    for mode in (ModeName.DOUGH, ModeName.TURBO, ModeName.BLEND, ModeName.WARM_UP):
        assert mode_available(mode, EditorVariant.TM6)
    assert mode_available(ModeName.RICE_COOKER, EditorVariant.TM6)


def test_varoma_is_a_temperature_in_tm6_editor_only() -> None:
    assert varoma_is_temperature(EditorVariant.TM6)
    assert not varoma_is_temperature(EditorVariant.TM7)


def test_tm6_varoma_matches_what_the_editor_produces() -> None:
    # Data the TM6 editor dialog produced for "20 Min./Varoma/Linkslauf/Stufe 1".
    expected = {
        "speed": "1",
        "direction": "CCW",
        "time": 1200,
        "temperature": {"value": "varoma", "unit": "C"},
    }

    result = TtsStrategy(EditorVariant.TM6).detect(
        "Gemüse 20 Min./Varoma/Linkslauf/Stufe 1 garen.", []
    )

    assert len(result) == 1
    annotation = result[0]
    assert isinstance(annotation, TtsAnnotation)
    assert annotation.data.model_dump(mode="json") == expected


def test_tm7_varoma_stays_steaming() -> None:
    result = TtsStrategy(EditorVariant.TM7).detect("Reis 15 Min./Varoma/Stufe 1 garen.", [])

    assert [type(annotation) for annotation in result] == [SteamingModeAnnotation]


def test_tm6_inferrer_does_not_emit_browning() -> None:
    text = "Hähnchen 8 Min./150 °C/Intensiv anbraten."

    tm6 = AnnotationInferrer.for_tools(["TM6", "TM5"]).infer(text, [])
    tm7 = AnnotationInferrer.for_tools(["TM7", "TM6"]).infer(text, [])

    assert not any(isinstance(annotation, BrowningModeAnnotation) for annotation in tm6)
    assert [type(annotation) for annotation in tm7] == [BrowningModeAnnotation]


def test_payload_uses_the_editor_variant_of_the_draft() -> None:
    draft = CustomRecipeDraft(
        name="Gemüse",
        ingredients=["500 g Gemüse"],
        steps=[RecipeStep(text="Gemüse 20 Min./Varoma/Stufe 1 garen.")],
        tools=["TM6"],
    )

    annotation = _draft_to_payload(draft)["instructions"][0]["annotations"][-1]

    assert annotation["type"] == "TTS"
    assert annotation["data"]["temperature"] == {"value": "varoma", "unit": "C"}
    assert "name" not in annotation


def _steaming() -> SteamingModeAnnotation:
    return SteamingModeAnnotation(data=SteamingModeData(time=1200, speed="1"), offset=0, length=10)


def _browning() -> BrowningModeAnnotation:
    return BrowningModeAnnotation(
        data=BrowningModeData(
            time=600, temperature=TemperatureData(value="160"), power=BrowningPower.GENTLE
        ),
        offset=0,
        length=10,
    )


def _varoma_tts() -> TtsAnnotation:
    return TtsAnnotation(
        data=TtsAnnotationData(speed="1", time=1200, temperature=TemperatureData(value="varoma")),
        offset=0,
        length=10,
    )


@pytest.mark.parametrize("annotation", [_steaming(), _browning()])
def test_draft_rejects_tm7_only_modes_without_tm7(
    annotation: SteamingModeAnnotation | BrowningModeAnnotation,
) -> None:
    with pytest.raises(ValidationError, match="TM7 editor only"):
        CustomRecipeDraft(
            name="X",
            ingredients=["1 Zwiebel"],
            steps=[RecipeStep(text="Dampfgaren 20 Min.", annotations=[annotation])],
            tools=["TM6"],
        )


def test_draft_rejects_varoma_temperature_with_tm7() -> None:
    with pytest.raises(ValidationError, match="use a MODE/steaming"):
        CustomRecipeDraft(
            name="X",
            ingredients=["1 Zwiebel"],
            steps=[RecipeStep(text="Dampfgaren 20 Min.", annotations=[_varoma_tts()])],
            tools=["TM7", "TM6"],
        )


def test_draft_accepts_editor_conform_annotations() -> None:
    tm6 = CustomRecipeDraft(
        name="X",
        ingredients=["1 Zwiebel"],
        steps=[
            RecipeStep(text="Dampfgaren 20 Min.", annotations=[_varoma_tts()]),
            RecipeStep(
                text="Reis kochen", annotations=[RiceCookerModeAnnotation(offset=0, length=11)]
            ),
        ],
        tools=["TM6"],
    )
    tm7 = CustomRecipeDraft(
        name="X",
        ingredients=["1 Zwiebel"],
        steps=[RecipeStep(text="Dampfgaren 20 Min.", annotations=[_steaming(), _browning()])],
        tools=["TM7"],
    )

    assert tm6.tools == ["TM6"]
    assert tm7.tools == ["TM7"]
