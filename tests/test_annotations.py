"""Tests for the guided-cooking annotation inferrer."""

from __future__ import annotations

import pytest

from cookidough_mcp.annotation_models import (
    BrowningModeAnnotation,
    BrowningModeData,
    BrowningPower,
    DoughModeAnnotation,
    DoughModeData,
    IngredientAnnotationData,
    MixDirection,
    SteamingModeAnnotation,
    SteamingModeData,
    TemperatureData,
    TtsAnnotationData,
)
from cookidough_mcp.annotations import (
    AnnotationInferrer,
    BrowningStrategy,
    DoughStrategy,
    IngredientStrategy,
    TtsStrategy,
)


class TestTtsStrategy:
    @pytest.fixture
    def strategy(self) -> TtsStrategy:
        return TtsStrategy()

    def test_matches_seconds_only_span(self, strategy: TtsStrategy) -> None:
        text = "Haselnüsse in den Mixtopf geben und 3 Sek./Stufe 5 mit dem Mixmesser hacken."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert annotation.type == "TTS"
        assert annotation.offset == 36
        assert annotation.length == 14
        assert annotation.data == TtsAnnotationData(speed="5", time=3)

    def test_matches_minutes_and_seconds_span(self, strategy: TtsStrategy) -> None:
        text = "3 EL Haselnussöl in den Mixtopf geben, 3 Min. 50 Sek./Stufe 1 erhitzen."

        result = strategy.detect(text, [])

        assert any(
            annotation.offset == 39
            and annotation.length == 22
            and annotation.data == TtsAnnotationData(speed="1", time=230)
            for annotation in result
        )

    def test_matches_english_minutes_and_speed(self, strategy: TtsStrategy) -> None:
        text = "Stir 5 min / speed 4 until smooth."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(speed="4", time=300)

    def test_matches_soft_speed_compound(self, strategy: TtsStrategy) -> None:
        """``Sanftrührstufe`` (compound) maps to ``speed="soft"`` with temp + reverse."""
        text = "Spargel garen 10 Min./120 °C/Linkslauf/Sanftrührstufe."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="soft",
            time=600,
            temperature=TemperatureData(value="120"),
            direction=MixDirection.COUNTER_CLOCKWISE,
        )

    def test_matches_separated_soft_speed_token(self, strategy: TtsStrategy) -> None:
        """``Stufe sanft`` and ``speed soft`` are normalised to ``speed="soft"``."""
        text = "Sauce 2 Min./100 °C/Stufe sanft verrühren."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="soft",
            time=120,
            temperature=TemperatureData(value="100"),
        )

    def test_matches_english_soft_speed(self, strategy: TtsStrategy) -> None:
        text = "Heat 3 min / 90 °C / speed soft."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="soft", time=180, temperature=TemperatureData(value="90")
        )

    def test_soft_compound_span_covers_whole_compound(self, strategy: TtsStrategy) -> None:
        """The annotated length must include the full ``Sanftrührstufe`` token."""
        text = "30 Sek/100 °C/Linkslauf/Sanftrührstufe schwenken."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        span = text[annotation.offset : annotation.offset + annotation.length]
        assert span.endswith("Sanftrührstufe")

    def test_matches_anbratstufe_compound(self, strategy: TtsStrategy) -> None:
        """``Anbratstufe`` (TM7 browning-mode stir) maps to ``speed="anbrat"``."""
        text = "Spargel 2 Min./120 °C/Anbratstufe ohne Messbecher erhitzen."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="anbrat",
            time=120,
            temperature=TemperatureData(value="120"),
        )

    def test_matches_anbratstufe_with_reverse_direction(self, strategy: TtsStrategy) -> None:
        text = "Spargel 10 Min./120 °C/Anbratstufe/Linkslauf goldgelb braten."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="anbrat",
            time=600,
            temperature=TemperatureData(value="120"),
            direction=MixDirection.COUNTER_CLOCKWISE,
        )

    def test_returns_empty_on_plain_prose(self, strategy: TtsStrategy) -> None:
        text = "Salat auf Tellern anrichten und servieren."

        assert strategy.detect(text, []) == []

    def test_emits_one_annotation_per_match(self, strategy: TtsStrategy) -> None:
        text = "Erst 30 Sek./Stufe 4, dann nochmal 5 Min./Stufe 2 weiterrühren."

        result = strategy.detect(text, [])

        assert [a.data for a in result] == [
            TtsAnnotationData(speed="4", time=30),
            TtsAnnotationData(speed="2", time=300),
        ]

    def test_drops_match_without_time(self, strategy: TtsStrategy) -> None:
        """A bare ``/Stufe N`` (no minutes/seconds) is not a usable TTS span."""
        text = "Zwiebeln in die Schüssel / Stufe 4 mischen."

        assert strategy.detect(text, []) == []

    def test_captures_optional_temperature(self, strategy: TtsStrategy) -> None:
        text = "Sauce 10 Min./90 °C/Stufe 2 köcheln."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="2", time=600, temperature=TemperatureData(value="90")
        )

    def test_captures_temperature_and_reverse_blade_direction(self, strategy: TtsStrategy) -> None:
        text = "Sauce 2 Min./100 °C/Linkslauf/Stufe 1 verrühren."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="1",
            time=120,
            temperature=TemperatureData(value="100"),
            direction=MixDirection.COUNTER_CLOCKWISE,
        )

    def test_matches_reverse_blade_without_temperature(self, strategy: TtsStrategy) -> None:
        """Cookidoo cold-folding pattern (no temperature, reverse blade)."""
        text = "Eischnee 30 Sek./Linkslauf/Stufe 2 unterheben."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="2", time=30, direction=MixDirection.COUNTER_CLOCKWISE
        )

    @pytest.mark.parametrize(
        "reverse_token",
        ["counter-clockwise", "counterclockwise", "counter clockwise", "anticlockwise"],
    )
    def test_recognises_english_reverse_direction_synonyms(
        self, strategy: TtsStrategy, reverse_token: str
    ) -> None:
        text = f"Stir 2 min/90 °C/{reverse_token}/speed 1."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert result[0].data == TtsAnnotationData(
            speed="1",
            time=120,
            temperature=TemperatureData(value="90"),
            direction=MixDirection.COUNTER_CLOCKWISE,
        )

    def test_varoma_steaming_inherits_reverse_blade_direction(self, strategy: TtsStrategy) -> None:
        """``Linkslauf`` between Varoma and Stufe must propagate to STEAMING.data.direction."""
        text = "Reis 15 Min./Varoma/Linkslauf/Stufe 1 garen."

        result = strategy.detect(text, [])

        annotation = result[0]
        assert isinstance(annotation, SteamingModeAnnotation)
        assert annotation.data.direction is MixDirection.COUNTER_CLOCKWISE

    def test_varoma_span_yields_steaming_mode(self, strategy: TtsStrategy) -> None:
        text = "Kartoffeln 25 Min./Varoma/Stufe 1 dämpfen."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert isinstance(annotation, SteamingModeAnnotation)
        assert annotation.data == SteamingModeData(time=1500, speed="1")


class TestBrowningStrategy:
    @pytest.fixture
    def strategy(self) -> BrowningStrategy:
        return BrowningStrategy()

    def test_matches_intensive_browning_span(self, strategy: BrowningStrategy) -> None:
        text = "Hähnchen 8 Min./150 °C/Intensiv anbraten."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert isinstance(annotation, BrowningModeAnnotation)
        assert annotation.data == BrowningModeData(
            time=480,
            temperature=TemperatureData(value="150"),
            power=BrowningPower.INTENSE,
        )

    def test_matches_gentle_browning_in_english(self, strategy: BrowningStrategy) -> None:
        text = "Pan-fry 6 min/145 °C/Gentle until brown."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert isinstance(annotation, BrowningModeAnnotation)
        assert annotation.data.power is BrowningPower.GENTLE

    def test_drops_unsupported_temperature(self, strategy: BrowningStrategy) -> None:
        """Cookidoo only accepts 140..160 °C in 5 °C steps for browning."""
        text = "Hähnchen 5 Min./130 °C/Intensiv anbraten."

        assert strategy.detect(text, []) == []

    def test_drops_excessive_duration(self, strategy: BrowningStrategy) -> None:
        """Time above the 30-minute ceiling is rejected upstream."""
        text = "Hähnchen 35 Min./150 °C/Intensiv anbraten."

        assert strategy.detect(text, []) == []


class TestDoughStrategy:
    @pytest.fixture
    def strategy(self) -> DoughStrategy:
        return DoughStrategy()

    def test_matches_teigstufe_compound(self, strategy: DoughStrategy) -> None:
        text = "Hefeteig 2 Min./Teigstufe kneten."

        result = strategy.detect(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert isinstance(annotation, DoughModeAnnotation)
        assert annotation.data == DoughModeData(time=120)

    def test_matches_separated_stufe_teig(self, strategy: DoughStrategy) -> None:
        text = "Pizzateig 3 Min./Stufe Teig kneten."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert isinstance(result[0], DoughModeAnnotation)
        assert result[0].data.time == 180

    @pytest.mark.parametrize("phrase", ["dough mode", "knead mode", "kneading mode"])
    def test_matches_english_dough_phrases(self, strategy: DoughStrategy, phrase: str) -> None:
        text = f"Knead 2 min/{phrase} until elastic."

        result = strategy.detect(text, [])

        assert len(result) == 1
        assert isinstance(result[0], DoughModeAnnotation)

    def test_does_not_match_bare_dough_reference(self, strategy: DoughStrategy) -> None:
        """The dough verb alone, without the canonical token, must not match."""
        text = "Den Teig 5 Minuten ruhen lassen, dann ausrollen."

        assert strategy.detect(text, []) == []


class TestIngredientStrategy:
    @pytest.fixture
    def strategy(self) -> IngredientStrategy:
        return IngredientStrategy()

    def test_matches_head_noun_inside_step_text(self, strategy: IngredientStrategy) -> None:
        """When step text repeats the quantity inline, the span covers it too."""
        text = "3 EL Haselnussöl in den Mixtopf geben."

        result = strategy.detect(text, ["3 EL Haselnussöl"])

        assert len(result) == 1
        annotation = result[0]
        assert text[annotation.offset : annotation.offset + annotation.length] == "3 EL Haselnussöl"
        assert annotation.data == IngredientAnnotationData(description="3 EL Haselnussöl")

    def test_matches_bare_noun_when_step_omits_quantity(self, strategy: IngredientStrategy) -> None:
        """The realistic case: step says ``Zwiebel``, ingredient line carries the count."""
        text = "Zwiebel halbieren und in den Mixtopf geben."

        result = strategy.detect(text, ["1 Zwiebel"])

        assert len(result) == 1
        annotation = result[0]
        assert annotation.offset == 0
        assert text[annotation.offset : annotation.offset + annotation.length] == "Zwiebel"
        assert annotation.data == IngredientAnnotationData(description="1 Zwiebel")

    def test_matches_single_token_head_with_strict_word_boundary(
        self, strategy: IngredientStrategy
    ) -> None:
        """Single-token heads (``Salz``, ``Öl``) match where they appear standalone."""
        text = "Salz und Pfeffer zugeben."

        result = strategy.detect(text, ["1 TL Salz", "1 Prise Pfeffer"])

        spans = sorted((a.offset, text[a.offset : a.offset + a.length]) for a in result)
        assert spans == [(0, "Salz"), (9, "Pfeffer")]

    def test_matches_compound_prefix(self, strategy: IngredientStrategy) -> None:
        """A long head (>=5 chars) matches inside a compound that starts with it."""
        text = "Petersilienblättchen fein hacken."

        result = strategy.detect(text, ["4 Stiele Petersilie"])

        assert len(result) == 1
        annotation = result[0]
        span = text[annotation.offset : annotation.offset + annotation.length]
        assert span == "Petersilienblättchen"
        assert annotation.data == IngredientAnnotationData(description="4 Stiele Petersilie")

    def test_matches_plural_form_of_long_head(self, strategy: IngredientStrategy) -> None:
        """``Zwiebel`` matches ``Zwiebeln`` (compound-prefix rule covers plurals too)."""
        text = "Zwiebeln in den Mixtopf geben."

        result = strategy.detect(text, ["1 Zwiebel"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "Zwiebeln"

    def test_does_not_match_head_as_compound_suffix(self, strategy: IngredientStrategy) -> None:
        """``Öl`` must not match inside ``Olivenöl`` — that is a different ingredient."""
        text = "Olivenöl in die Pfanne geben."

        assert strategy.detect(text, ["2 EL Öl"]) == []

    def test_matches_short_head_with_known_plural_suffix(
        self, strategy: IngredientStrategy
    ) -> None:
        """3-4-char heads match exact form + known German plural endings."""
        text = "Salze gleichmäßig verteilen."

        result = strategy.detect(text, ["1 TL Salz"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "Salze"

    def test_short_head_does_not_match_unrelated_compound(
        self, strategy: IngredientStrategy
    ) -> None:
        """3-4-char heads do NOT engage compound matching (no false ``Reis``→``Reisebus``)."""
        text = "Salzwasser aufkochen."

        assert strategy.detect(text, ["1 TL Salz"]) == []

    def test_very_short_head_matches_exact_only(self, strategy: IngredientStrategy) -> None:
        """2-char heads (``Ei``, ``Öl``) match exactly — no plural extension."""
        text = "Ein Topf wird benötigt, dann das Ei zugeben."

        result = strategy.detect(text, ["2 Eier"])

        # ``Eier`` (4-char head, ends with ``er``) matches only itself; ``Ei``
        # alone is matched by neither the head ``Eier`` nor the prefix rule.
        assert result == []

    def test_very_short_head_matches_when_text_uses_exact_head(
        self, strategy: IngredientStrategy
    ) -> None:
        """Count-only quantity (no unit) is still captured into the span."""
        text = "Salz, Pfeffer und 1 Ei in die Schüssel."

        result = strategy.detect(text, ["1 Ei"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "1 Ei"

    def test_returns_empty_when_ingredient_missing_from_text(
        self, strategy: IngredientStrategy
    ) -> None:
        text = "Wasser aufkochen."

        assert strategy.detect(text, ["200 g Mehl"]) == []

    def test_strips_trailing_comma_description(self, strategy: IngredientStrategy) -> None:
        """The descriptor after the comma is not part of the head."""
        text = "Weißwein in den Mixtopf geben."

        result = strategy.detect(text, ["100 ml Weißwein, trocken"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "Weißwein"

    def test_emits_one_annotation_per_occurrence(self, strategy: IngredientStrategy) -> None:
        text = "Öl erhitzen, dann etwas mehr Öl hinzugeben."

        result = strategy.detect(text, ["2 EL Öl"])

        assert [text[a.offset : a.offset + a.length] for a in result] == ["Öl", "Öl"]

    def test_includes_quantity_prefix_when_present_in_step_text(
        self, strategy: IngredientStrategy
    ) -> None:
        """A ``<qty> <unit>`` immediately before the head is pulled into the span."""
        text = "20 g Haselnüsse in den Mixtopf geben."

        result = strategy.detect(text, ["20 g Haselnüsse (oder Walnüsse oder Pinienkerne)"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "20 g Haselnüsse"

    def test_includes_multi_word_unit_quantity_prefix(self, strategy: IngredientStrategy) -> None:
        """``1 Prise Salz`` (multi-word unit ``Prise``) is captured as a unit."""
        text = "1 Prise Salz und 1 Prise Pfeffer zugeben."

        result = strategy.detect(text, ["1 Prise Salz", "1 Prise Pfeffer"])

        spans = sorted((a.offset, text[a.offset : a.offset + a.length]) for a in result)
        assert spans == [(0, "1 Prise Salz"), (17, "1 Prise Pfeffer")]

    def test_quantity_prefix_optional_when_step_omits_it(
        self, strategy: IngredientStrategy
    ) -> None:
        """Without a measurement in step text, only the head is annotated."""
        text = "Petersilie und Schnittlauch fein hacken."

        result = strategy.detect(text, ["4 Stiele Petersilie", "0,5 Bund Schnittlauch"])

        spans = sorted((a.offset, text[a.offset : a.offset + a.length]) for a in result)
        assert spans == [(0, "Petersilie"), (15, "Schnittlauch")]

    def test_strips_compound_portion_suffix_for_secondary_head(
        self, strategy: IngredientStrategy
    ) -> None:
        """``Spargelstücke`` (head) ALSO matches as ``Spargel`` standalone."""
        text = "zum Spargel in den Mixtopf geben"

        result = strategy.detect(text, ["500 g Spargelstücke"])

        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "Spargel"
        assert result[0].data == IngredientAnnotationData(description="500 g Spargelstücke")

    def test_compound_portion_suffix_does_not_double_annotate(
        self, strategy: IngredientStrategy
    ) -> None:
        """Primary and secondary head must not both emit for the same span."""
        text = "500 g Spargelstücke schälen."

        result = strategy.detect(text, ["500 g Spargelstücke"])

        # Both ``Spargelstücke`` (primary) and ``Spargel`` (suffix-stripped)
        # would match ``Spargelstücke``; dedupe keeps one.
        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "500 g Spargelstücke"

    def test_multi_word_head_falls_back_to_last_word(self, strategy: IngredientStrategy) -> None:
        """``weißer Spargel`` (adj + noun) must also match ``Spargel`` standalone.

        Step text often uses a different case of the adjective (``weißen``)
        or drops it altogether. Matching just the noun keeps coverage.
        """
        text = "500 g weißen Spargel schälen, dann Spargelstücke beiseitestellen."

        result = strategy.detect(text, ["500 g weißer Spargel"])

        spans = sorted((a.offset, text[a.offset : a.offset + a.length]) for a in result)
        # The primary ``weißer Spargel`` cannot match ``weißen Spargel``
        # (declension mismatch), but the last-word fallback ``Spargel``
        # catches both the standalone occurrence and the compound
        # (``Spargelstücke``). The adjective ``weißen`` itself is not
        # recovered into the span.
        assert spans == [(13, "Spargel"), (35, "Spargelstücke")]
        assert all(
            a.data == IngredientAnnotationData(description="500 g weißer Spargel") for a in result
        )

    def test_multi_word_head_skips_short_last_word(self, strategy: IngredientStrategy) -> None:
        """A < 5-char last word (``olive oil`` → ``oil``) is too risky to use
        as a fallback head — exact-only matching would over-fire."""
        text = "Pour some olive oil into the pan."

        result = strategy.detect(text, ["3 EL olive oil"])

        # Primary head ``olive oil`` matches verbatim; ``oil`` (3 chars) is
        # *not* used as a secondary head, so we still get exactly one span.
        assert len(result) == 1
        assert text[result[0].offset : result[0].offset + result[0].length] == "olive oil"

    @pytest.mark.parametrize(
        ("ingredient", "step_text", "expected_span"),
        [
            # German: newly added unit tokens
            ("2 Blätter Basilikum", "Basilikum unter den Salat heben.", "Basilikum"),
            ("3 Rispen Cocktailtomaten", "Cocktailtomaten halbieren.", "Cocktailtomaten"),
            ("1 Kugel Mozzarella", "Mozzarella in Scheiben schneiden.", "Mozzarella"),
            ("1 Klacks Butter", "Butter in der Pfanne zerlassen.", "Butter"),
            # English
            ("2 cloves garlic", "Mince garlic and set aside.", "garlic"),
            ("1 pinch salt", "Add salt to taste.", "salt"),
            ("1 head lettuce", "Wash lettuce thoroughly.", "lettuce"),
            # French (linker ``de`` stripped)
            ("1 cuillère de farine", "Tamiser la farine.", "farine"),
            ("1 boîte de tomates", "Égoutter les tomates.", "tomates"),
            # Italian (linker ``di`` stripped)
            ("2 spicchi di aglio", "Tritare l aglio finemente.", "aglio"),
            ("3 foglie di basilico", "Aggiungere il basilico.", "basilico"),
            # Spanish (linker ``de`` stripped)
            ("2 dientes de ajo", "Picar el ajo en trozos pequeños.", "ajo"),
            ("1 manojo de cilantro", "Lavar el cilantro.", "cilantro"),
            # Dutch (step starts with capitalised noun; match is case-preserving)
            ("2 tenen knoflook", "Knoflook fijnhakken.", "Knoflook"),
            ("3 takjes peterselie", "Peterselie wassen en hacken.", "Peterselie"),
        ],
    )
    def test_extracts_head_across_locales(
        self,
        strategy: IngredientStrategy,
        ingredient: str,
        step_text: str,
        expected_span: str,
    ) -> None:
        result = strategy.detect(step_text, [ingredient])

        assert len(result) == 1
        assert step_text[result[0].offset : result[0].offset + result[0].length] == expected_span


class TestAnnotationInferrer:
    def test_orders_ingredient_and_tts_by_offset(self) -> None:
        inferrer = AnnotationInferrer()
        text = "3 EL Haselnussöl in den Mixtopf geben, 3 Min. 50 Sek./Stufe 1 erhitzen."

        result = inferrer.infer(text, ["3 EL Haselnussöl"])

        assert [annotation.type for annotation in result] == ["INGREDIENT", "TTS"]
        # Span now includes the ``3 EL `` quantity prefix → offset 0, len 16.
        # TTS span starts at 39.
        assert [annotation.offset for annotation in result] == [0, 39]

    def test_returns_empty_for_steps_without_recognised_patterns(self) -> None:
        inferrer = AnnotationInferrer()

        assert inferrer.infer("Salat anrichten und servieren.", []) == []

    def test_browning_wins_over_tts_on_overlap(self) -> None:
        """A BROWNING span subsumes the time/temperature an inner TTS would match."""
        inferrer = AnnotationInferrer()
        text = "Hähnchen 8 Min./150 °C/Intensiv anbraten."

        result = inferrer.infer(text, [])

        assert len(result) == 1
        annotation = result[0]
        assert isinstance(annotation, BrowningModeAnnotation)

    def test_browning_and_steaming_coexist_on_disjoint_spans(self) -> None:
        """A step with both a browning and a varoma span must emit both, in order."""
        inferrer = AnnotationInferrer()
        text = "Hähnchen 8 Min./150 °C/Intensiv, dann Reis 15 Min./Varoma/Stufe 1."

        result = inferrer.infer(text, [])

        assert [type(annotation) for annotation in result] == [
            BrowningModeAnnotation,
            SteamingModeAnnotation,
        ]
        offsets = [annotation.offset for annotation in result]
        assert offsets == sorted(offsets)
