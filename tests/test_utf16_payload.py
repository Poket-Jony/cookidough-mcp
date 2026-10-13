"""Cookidoo payload positions use UTF-16; draft positions remain code points."""

from cookidough_mcp.annotation_models import (
    IngredientAnnotation,
    IngredientAnnotationData,
    TtsAnnotation,
    TtsAnnotationData,
)
from cookidough_mcp.models import CustomRecipeDraft, RecipeStep
from cookidough_mcp.session import _draft_to_payload


def test_explicit_annotations_convert_offsets_without_mutating_the_draft() -> None:
    text = "🍲 Mix 🍚 now"
    annotation = TtsAnnotation(data=TtsAnnotationData(time=30, speed="1"), offset=2, length=5)
    draft = CustomRecipeDraft(
        name="Unicode recipe",
        ingredients=["water"],
        steps=[RecipeStep(text=text, annotations=[annotation])],
    )
    original = draft.model_dump()

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert instruction["annotations"][0]["position"] == {"offset": 3, "length": 6}
    assert instruction["text"] == text
    assert draft.model_dump() == original


def test_ingredient_annotation_span_can_include_a_non_bmp_character() -> None:
    text = "Add 🍚 rice now"
    annotation = IngredientAnnotation(
        data=IngredientAnnotationData(description="🍚 rice"), offset=4, length=6
    )
    draft = CustomRecipeDraft(
        name="Rice",
        ingredients=["🍚 rice"],
        steps=[RecipeStep(text=text, annotations=[annotation])],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]

    assert instruction["annotations"][0]["position"] == {"offset": 4, "length": 7}
    assert instruction["annotations"][0]["data"] == {"description": "🍚 rice"}


def test_inferred_ingredient_and_machine_spans_round_trip_after_emoji() -> None:
    text = "🍲 Zucker zugeben und 10 Sek./Stufe 1"
    draft = CustomRecipeDraft(
        name="Sugar",
        ingredients=["20 g Zucker"],
        steps=[RecipeStep(text=text)],
    )

    instruction = _draft_to_payload(draft)["instructions"][0]
    annotations = instruction["annotations"]

    assert [annotation["type"] for annotation in annotations] == ["INGREDIENT", "TTS"]
    assert annotations[0]["position"] == {"offset": 3, "length": 6}
    encoded = text.encode("utf-16-le")
    spans = [
        encoded[
            annotation["position"]["offset"] * 2 : (
                annotation["position"]["offset"] + annotation["position"]["length"]
            )
            * 2
        ].decode("utf-16-le")
        for annotation in annotations
    ]
    assert spans == ["Zucker", "10 Sek./Stufe 1"]
