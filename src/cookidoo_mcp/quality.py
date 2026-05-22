"""TM7 guided-cooking quality scoring.

Each `QualityRule` evaluates a `CustomRecipeDraft` and emits `QualityIssue`s
with a per-rule weight. The aggregate score is ``max(0, 100 - sum(weights))``.
Rules deliberately operate on the draft only; no network calls.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .annotation_models import ModeName, StepAnnotation, StepAnnotationType
from .annotations import AnnotationInferrer
from .models import (
    CustomRecipeDraft,
    QualityIssue,
    QualityIssueSeverity,
    QualityReport,
)

_GUIDED_COOKING_ANNOTATION_TYPES = frozenset({StepAnnotationType.TTS, StepAnnotationType.MODE})

_TIME_PATTERN = re.compile(
    r"\b\d+([.,]\d+)?\s*(s|sek|sec|seconds?|sekunden?|"
    r"min|minutes?|minuten?|h|hours?|stunden?)\b",
    re.IGNORECASE,
)
# Speed indicator. Accepts the canonical ``Stufe N`` / ``speed N`` form as
# well as the compound shorthands that Cookidoo also emits in step text:
# ``Sanftrührstufe`` (soft mode), ``Teigstufe`` / ``Knetstufe`` (dough mode).
# Kept in sync with the inferrer's TTS / dough patterns so the quality gate
# does not penalise wording that the inferrer would happily annotate.
_SPEED_PATTERN = re.compile(
    r"\b(?:"
    r"(?:stufe|speed|level)\s*(?:[0-9]+(?:[.,][0-9]+)?|sanft|soft|teig|dough)"
    r"|Sanftrührstufe|Teigstufe|Knetstufe"
    r")\b",
    re.IGNORECASE,
)
# Temperature signal. The classic ``<n> °C`` / ``Varoma`` plus the TM7
# ``Bratfunktion <leicht|intensiv>`` mode, which carries an implicit
# 140-160 °C setting and replaces the explicit °C in modern step text.
_TEMP_PATTERN = re.compile(
    r"\b(?:"
    r"varoma|\d{2,3}\s*°?\s*c|\d{2,3}\s*degrees?"
    r"|Bratfunktion\s+(?:leicht|intensiv|gentle|intense)"
    r")\b",
    re.IGNORECASE,
)
_ACCESSORY_PATTERN = re.compile(
    r"\b(messbecher|spatel|spatula|simmering basket|garkorb|"
    r"butterfly|schmetterling|varoma)\b",
    re.IGNORECASE,
)
# Parallelisation hints. ``während(?:dessen)?`` covers both ``während`` and
# the compound ``währenddessen`` (a single word with no internal boundary,
# so a bare ``\bwährend\b`` would miss it).
_PARALLEL_PATTERN = re.compile(
    r"\b(?:während(?:dessen)?|gleichzeitig|zwischenzeitlich|"
    r"in der zwischenzeit|while|meanwhile|parallel|"
    r"at the same time|simultaneously)\b",
    re.IGNORECASE,
)
_OPERATION_PATTERN = re.compile(
    r"\b(chop|blend|mix|cook|heat|stir|process|knead|whisk|grate|grind|"
    r"saute|sauté|fry|boil|simmer|steam|bake|roast|reduce|brown|caramelize|"
    r"hack(?:e[nt]?)?|misch|mahl|kneten?|rühr|brate?n?|köche?ln|"
    r"sieden|dämpfen|dünste?n?|erhitze?n?|garen|schmoren|rösten|"
    r"karamellisier|aufkochen|zerkleiner|püri|aufschlag|reduzier|"
    r"weigh|wieg|emulsify|emulgier)\w*",
    re.IGNORECASE,
)
_PARALLELIZATION_LONG_RECIPE_THRESHOLD = 3


@dataclass(frozen=True)
class RuleResult:
    issues: list[QualityIssue]
    deduction: int


class QualityRule(ABC):
    """A single TM7 quality heuristic."""

    name: ClassVar[str]
    weight: ClassVar[int]
    max_deduction: ClassVar[int]

    @abstractmethod
    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        raise NotImplementedError


class TimeAndSpeedAnnotationRule(QualityRule):
    name = "time_speed_annotation"
    weight = 8
    max_deduction = 50

    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        issues: list[QualityIssue] = []
        missing = 0
        for index, step in enumerate(draft.steps):
            if not _OPERATION_PATTERN.search(step.text):
                continue
            # Only TTS / MODE annotations express a time + speed intent
            # explicitly; an INGREDIENT-only annotation does not excuse a
            # missing free-text guided-cooking phrase.
            if any(a.type in _GUIDED_COOKING_ANNOTATION_TYPES for a in step.annotations):
                continue
            has_time = bool(_TIME_PATTERN.search(step.text))
            has_speed = bool(_SPEED_PATTERN.search(step.text))
            if has_time and has_speed:
                continue
            missing += 1
            issues.append(
                QualityIssue(
                    step_index=index,
                    rule=self.name,
                    severity=QualityIssueSeverity.WARNING,
                    message=(
                        "Step is missing an explicit time and/or speed annotation "
                        "for guided cooking (e.g., '5 min / speed 3')."
                    ),
                )
            )
        return RuleResult(issues=issues, deduction=min(self.weight * missing, self.max_deduction))


class TemperatureRule(QualityRule):
    name = "temperature_or_mode"
    weight = 10
    max_deduction = 10

    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        cooking_steps = [
            (i, step) for i, step in enumerate(draft.steps) if _is_thermal_step(step.text)
        ]
        if not cooking_steps:
            return RuleResult(issues=[], deduction=0)
        # Two ways a thermal step can be considered well-specified:
        # 1) the free text already contains a temperature / Varoma / TM7
        #    Bratfunktion token; or
        # 2) the step carries an annotation that itself encodes a thermal
        #    setting (browning/steaming/warm_up/rice_cooker MODE, or any TTS
        #    whose ``data.temperature`` is set).
        for _, step in cooking_steps:
            if _TEMP_PATTERN.search(step.text):
                return RuleResult(issues=[], deduction=0)
            if any(_annotation_indicates_temperature(a) for a in step.annotations):
                return RuleResult(issues=[], deduction=0)
        return RuleResult(
            issues=[
                QualityIssue(
                    step_index=cooking_steps[0][0],
                    rule=self.name,
                    severity=QualityIssueSeverity.WARNING,
                    message=(
                        "Cooking step(s) lack an explicit temperature or Varoma "
                        "annotation (e.g., '100 °C' or 'Varoma')."
                    ),
                )
            ],
            deduction=self.weight,
        )


class AccessoryRule(QualityRule):
    name = "accessory_mention"
    weight = 6
    max_deduction = 6

    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        if any(_ACCESSORY_PATTERN.search(step) for step in draft.step_texts):
            return RuleResult(issues=[], deduction=0)
        return RuleResult(
            issues=[
                QualityIssue(
                    step_index=None,
                    rule=self.name,
                    severity=QualityIssueSeverity.WARNING,
                    message=(
                        "No Thermomix accessory (butterfly, simmering basket, "
                        "Varoma, spatula) is referenced. Consider naming the "
                        "tool used in each step for clearer guided cooking."
                    ),
                )
            ],
            deduction=self.weight,
        )


class ParallelizationRule(QualityRule):
    name = "parallelization_hint"
    weight = 4
    max_deduction = 4

    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        if len(draft.steps) <= _PARALLELIZATION_LONG_RECIPE_THRESHOLD:
            return RuleResult(issues=[], deduction=0)
        if any(_PARALLEL_PATTERN.search(step) for step in draft.step_texts):
            return RuleResult(issues=[], deduction=0)
        return RuleResult(
            issues=[
                QualityIssue(
                    step_index=None,
                    rule=self.name,
                    severity=QualityIssueSeverity.WARNING,
                    message=(
                        "Recipe has many steps but no parallelization hints "
                        "('meanwhile', 'während dessen'). Adding them helps "
                        "the cook plan their time."
                    ),
                )
            ],
            deduction=self.weight,
        )


class IngredientReferenceRule(QualityRule):
    """Flags ingredients listed but never referenced in any step.

    The check reuses :class:`AnnotationInferrer` so "the rule found it" is
    by construction identical to "the upload would annotate it". That
    avoids the substring inflation of the previous heuristic
    (``"salz" in "salat"`` → false positive) and inherits the inferrer's
    multi-locale head-extraction and compound-prefix tolerance.
    """

    name = "ingredient_step_link"
    weight = 8
    max_deduction = 24

    def __init__(self, inferrer: AnnotationInferrer | None = None) -> None:
        self._inferrer = inferrer if inferrer is not None else AnnotationInferrer()

    def evaluate(self, draft: CustomRecipeDraft) -> RuleResult:
        referenced: set[str] = set()
        for step in draft.steps:
            for annotation in self._inferrer.infer(step.text, draft.ingredients):
                if annotation.type == StepAnnotationType.INGREDIENT:
                    referenced.add(annotation.data.description)
        missing = [ing for ing in draft.ingredients if ing not in referenced]
        if not missing:
            return RuleResult(issues=[], deduction=0)
        sample = ", ".join(missing[:6]) + ("..." if len(missing) > 6 else "")
        message = f"These ingredients are listed but never mentioned in the steps: {sample}"
        return RuleResult(
            issues=[
                QualityIssue(
                    step_index=None,
                    rule=self.name,
                    severity=QualityIssueSeverity.WARNING,
                    message=message,
                )
            ],
            deduction=min(self.weight * len(missing), self.max_deduction),
        )


def default_rules() -> tuple[QualityRule, ...]:
    """Build a fresh tuple of the default rules.

    Returned per call so that callers cannot mutate a shared module-level list
    if a future rule grows internal state.
    """
    return (
        TimeAndSpeedAnnotationRule(),
        TemperatureRule(),
        AccessoryRule(),
        ParallelizationRule(),
        IngredientReferenceRule(),
    )


class QualityScorer:
    """Aggregates rule results into a `QualityReport`."""

    def __init__(self, threshold: int, rules: tuple[QualityRule, ...] | None = None) -> None:
        self._threshold = threshold
        self._rules = rules if rules is not None else default_rules()

    def score(self, draft: CustomRecipeDraft) -> QualityReport:
        issues: list[QualityIssue] = []
        deduction = 0
        for rule in self._rules:
            result = rule.evaluate(draft)
            issues.extend(result.issues)
            deduction += result.deduction
        score = max(0, 100 - deduction)
        return QualityReport(
            score=score,
            threshold=self._threshold,
            meets_bar=score >= self._threshold,
            issues=issues,
        )


# MODE names that carry an implicit thermal setting even when the step's
# free text does not spell out a temperature. ``DOUGH``, ``TURBO`` and
# ``BLEND`` are deliberately excluded — they are mechanical, not thermal.
_THERMAL_MODE_NAMES = frozenset(
    {ModeName.BROWNING, ModeName.STEAMING, ModeName.WARM_UP, ModeName.RICE_COOKER}
)


def _annotation_indicates_temperature(annotation: StepAnnotation) -> bool:
    """True when ``annotation`` itself encodes a thermal setting.

    Used by :class:`TemperatureRule` so a step that already carries a fully
    structured guided-cooking annotation (e.g. ``MODE/browning`` with
    ``temperature`` data) is not penalised for omitting the free-text
    ``<n> °C`` token from the step description.
    """
    if annotation.type == StepAnnotationType.TTS:
        return annotation.data.temperature is not None
    if annotation.type == StepAnnotationType.MODE:
        return getattr(annotation, "name", None) in _THERMAL_MODE_NAMES
    return False


def _is_thermal_step(step: str) -> bool:
    keywords = (
        # German
        "kochen",
        "köcheln",
        "anbraten",
        "braten",
        "dünsten",
        "erhitzen",
        "aufkochen",
        "garen",
        "schmoren",
        "rösten",
        "karamellisieren",
        "dampfgaren",
        "reduzieren",
        # Romance / English
        "boil",
        "simmer",
        "sauté",
        "saute",
        "varoma",
        "steam",
        "bake",
        "roast",
        "brown",
        "caramelize",
        "reduce",
        "heat",
        # TM7-specific
        "bratfunktion",
    )
    lower = step.lower()
    return any(k in lower for k in keywords)  # fmt: skip


__all__ = [
    "AccessoryRule",
    "IngredientReferenceRule",
    "ParallelizationRule",
    "QualityRule",
    "QualityScorer",
    "RuleResult",
    "TemperatureRule",
    "TimeAndSpeedAnnotationRule",
    "default_rules",
]
