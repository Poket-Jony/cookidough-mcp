"""Tests for the TM7 quality scoring rules."""

from __future__ import annotations

import pytest

from cookidoo_mcp.annotation_models import (
    BlendModeAnnotation,
    BlendModeData,
    BrowningModeAnnotation,
    BrowningModeData,
    BrowningPower,
    IngredientAnnotation,
    IngredientAnnotationData,
    TemperatureData,
)
from cookidoo_mcp.models import CustomRecipeDraft, QualityIssueSeverity, RecipeStep
from cookidoo_mcp.quality import (
    AccessoryRule,
    IngredientReferenceRule,
    ParallelizationRule,
    QualityScorer,
    TemperatureRule,
    TimeAndSpeedAnnotationRule,
)


def _steps(*texts: str) -> list[RecipeStep]:
    return [RecipeStep(text=text) for text in texts]


@pytest.fixture
def annotated_draft() -> CustomRecipeDraft:
    return CustomRecipeDraft(
        name="Tomato Risotto",
        ingredients=["Tomato", "Rice", "Onion", "Olive oil"],
        steps=_steps(
            "Put onion and olive oil into mixing bowl, chop 5 sec / speed 5 with spatula.",
            "Add rice and tomato, cook 18 min / 100 °C / speed 1 with the simmering basket.",
            "Meanwhile prepare the garnish.",
            "Serve immediately.",
        ),
    )


@pytest.fixture
def bare_draft() -> CustomRecipeDraft:
    return CustomRecipeDraft(
        name="Plain rice",
        ingredients=["Rice", "Salt"],
        steps=_steps("Put rice in pot.", "Boil it.", "Wait.", "Eat."),
    )


def test_time_step_rule_passes_for_annotated_steps(
    annotated_draft: CustomRecipeDraft,
) -> None:
    result = TimeAndSpeedAnnotationRule().evaluate(annotated_draft)
    assert result.deduction == 0
    assert result.issues == []


def test_time_step_rule_penalises_missing_annotations(
    bare_draft: CustomRecipeDraft,
) -> None:
    result = TimeAndSpeedAnnotationRule().evaluate(bare_draft)
    assert result.deduction > 0
    assert all(i.severity is QualityIssueSeverity.WARNING for i in result.issues)


def test_time_step_rule_skips_step_with_explicit_mode_annotation() -> None:
    """A structured MODE annotation excuses the free-text time/speed warning."""
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Mehl"],
        steps=[
            RecipeStep(
                text="Mehl mit Wasser verkneten.",
                annotations=[
                    BlendModeAnnotation(data=BlendModeData(speed="6", time=90), offset=0, length=26)
                ],
            )
        ],
    )

    result = TimeAndSpeedAnnotationRule().evaluate(draft)

    assert result.deduction == 0
    assert result.issues == []


def test_time_step_rule_still_penalises_ingredient_only_annotation() -> None:
    """An INGREDIENT-only annotation does not express a guided-cooking intent."""
    draft = CustomRecipeDraft(
        name="X",
        ingredients=["Salz"],
        steps=[
            RecipeStep(
                text="cook the rice with the salt",
                annotations=[
                    IngredientAnnotation(
                        data=IngredientAnnotationData(description="salt"),
                        offset=23,
                        length=4,
                    )
                ],
            )
        ],
    )

    result = TimeAndSpeedAnnotationRule().evaluate(draft)

    assert result.deduction > 0
    assert result.issues


def test_temperature_rule_only_triggers_for_cooking_steps(
    annotated_draft: CustomRecipeDraft,
) -> None:
    assert TemperatureRule().evaluate(annotated_draft).deduction == 0


def test_temperature_rule_triggers_when_cooking_lacks_temperature() -> None:
    draft = CustomRecipeDraft(
        name="No temp",
        ingredients=["Water"],
        steps=_steps("Boil water until done."),
    )
    result = TemperatureRule().evaluate(draft)
    assert result.deduction == TemperatureRule.weight


def test_temperature_rule_accepts_tm7_bratfunktion_token() -> None:
    """``Bratfunktion intensiv`` carries an implicit 140-160 °C setting and
    counts as a temperature signal — the TM7 wording for browning mode."""
    draft = CustomRecipeDraft(
        name="Bratfunktion",
        ingredients=["Spargel"],
        steps=_steps("Spargel 8 Min./Bratfunktion intensiv/Linkslauf/Sanftrührstufe anbraten."),
    )
    assert TemperatureRule().evaluate(draft).deduction == 0


def test_temperature_rule_accepts_mode_annotation_temperature() -> None:
    """A structured MODE/browning annotation excuses the free-text °C token."""
    draft = CustomRecipeDraft(
        name="Annotated browning",
        ingredients=["Chicken"],
        steps=[
            RecipeStep(
                text="Chicken anbraten",
                annotations=[
                    BrowningModeAnnotation(
                        data=BrowningModeData(
                            time=480,
                            temperature=TemperatureData(value="150"),
                            power=BrowningPower.INTENSE,
                        ),
                        offset=0,
                        length=16,
                    )
                ],
            )
        ],
    )
    assert TemperatureRule().evaluate(draft).deduction == 0


def test_time_step_rule_accepts_sanftruehrstufe_token() -> None:
    """``Sanftrührstufe`` (compound) is the TM7 soft-speed token and must
    pass the speed-pattern check — consistent with the inferrer."""
    draft = CustomRecipeDraft(
        name="Soft stir",
        ingredients=["Sahne"],
        steps=_steps("Sahne 5 Min./Linkslauf/Sanftrührstufe verrühren."),
    )
    assert TimeAndSpeedAnnotationRule().evaluate(draft).deduction == 0


def test_parallelization_rule_recognises_waehrenddessen_compound() -> None:
    """``Währenddessen`` (single word, no inner boundary) was missed by the
    old ``\\bwährend\\b`` pattern. It must be accepted as a parallel hint."""
    draft = CustomRecipeDraft(
        name="Compound parallel",
        ingredients=["Water", "Salt", "Pepper", "Onion"],
        steps=_steps(
            "Step A.",
            "Step B.",
            "Step C.",
            "Währenddessen die Zwiebel hacken.",
            "Step E.",
        ),
    )
    assert ParallelizationRule().evaluate(draft).deduction == 0


def test_accessory_rule_penalises_recipe_without_accessories() -> None:
    draft = CustomRecipeDraft(
        name="No accessory",
        ingredients=["Salt"],
        steps=_steps("Mix everything together."),
    )
    result = AccessoryRule().evaluate(draft)
    assert result.deduction == AccessoryRule.weight


def test_parallelization_rule_only_triggers_for_longer_recipes() -> None:
    short = CustomRecipeDraft(
        name="Short",
        ingredients=["Water"],
        steps=_steps("A.", "B."),
    )
    assert ParallelizationRule().evaluate(short).deduction == 0


def test_ingredient_reference_rule_flags_unused_ingredients() -> None:
    draft = CustomRecipeDraft(
        name="Forgotten",
        ingredients=["Rice", "Saffron"],
        steps=_steps("Cook the rice for 10 min."),
    )
    result = IngredientReferenceRule().evaluate(draft)
    assert result.deduction > 0
    assert "Saffron" in result.issues[0].message


def test_ingredient_reference_rule_rejects_substring_false_positive() -> None:
    """``1 TL Salz`` must not be considered ``mentioned`` just because the
    step text contains ``Salat`` (substring inflation in the old heuristic)."""
    draft = CustomRecipeDraft(
        name="Salat ohne Salz",
        ingredients=["1 TL Salz"],
        steps=_steps("Den Salat auf Tellern anrichten und servieren."),
    )
    result = IngredientReferenceRule().evaluate(draft)
    assert result.deduction > 0
    assert "Salz" in result.issues[0].message


def test_scorer_returns_high_score_for_annotated_draft(
    annotated_draft: CustomRecipeDraft,
) -> None:
    report = QualityScorer(threshold=70).score(annotated_draft)
    assert report.score >= 70
    assert report.meets_bar is True


def test_scorer_returns_low_score_for_bare_draft(
    bare_draft: CustomRecipeDraft,
) -> None:
    report = QualityScorer(threshold=70).score(bare_draft)
    assert report.score < 70
    assert report.meets_bar is False
    assert report.issues
