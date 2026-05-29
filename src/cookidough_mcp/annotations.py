"""Thermomix guided-cooking annotation inference for plain-text recipe steps.

When a step lacks explicit annotations, an `AnnotationInferrer` scans the
step text and produces a best-effort list of `StepAnnotation`s. Strategies
are conservative — emitting nothing is preferred over emitting wrong spans.

``Anbraten``, ``Dampfgaren``, ``Pürieren`` etc. as bare verbs are *not*
matched as MODE spans; the inferrer requires the canonical Cookidoo phrase
(``<n> Min./<temp> °C/(Leicht|Intensiv)`` for browning, ``<n>/Varoma/Stufe
<n>`` for steaming, ``<n> Min./Teigstufe`` for dough/knead). The remaining
MODEs (``turbo``, ``rice_cooker``, ``warm_up``, ``blend``) have no
text-pattern detector — the LLM must supply them explicitly.

Soft speed (``Sanftrührstufe``, ``Stufe sanft``, ``speed soft``) is
recognised and emitted as a TTS span with ``speed="soft"``. The TM7
browning-mode stir pattern (``Anbratstufe`` / ``Bratstufe``) is recognised
and emitted as TTS with ``speed="anbrat"``. Reverse-blade direction
matches ``Linkslauf``, ``sens inverse``, ``reverse``, ``counterclockwise``
(with or without hyphen), and ``anticlockwise``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from .annotation_models import (
    BrowningModeAnnotation,
    BrowningModeData,
    BrowningPower,
    DoughModeAnnotation,
    DoughModeData,
    IngredientAnnotation,
    IngredientAnnotationData,
    MixDirection,
    SteamingModeAnnotation,
    SteamingModeData,
    StepAnnotation,
    TemperatureData,
    TtsAnnotation,
    TtsAnnotationData,
)

_TTS_PATTERN = re.compile(
    # Require at least one time component up front so a bare "/Stufe N"
    # cannot match and be dropped later by the runtime guard.
    r"(?=\d)"
    r"(?:(?P<minutes>\d+)\s*(?:Min\.?|minutes?|minuten?)\s*)?"
    r"(?:(?P<seconds>\d+)\s*(?:Sek\.?|seconds?|sekunden?)\s*)?"
    r"/\s*"
    r"(?:(?P<temperature>\d+)\s*°\s*C|(?P<varoma>Varoma))?"
    # The separator between temperature/varoma and reverse is independent
    # of whether either preceded — Cookidoo wording like "30 Sek./Linkslauf/
    # Stufe 2" omits temperature but still has the slash before the
    # reverse-blade token. Modelling the slash as its own optional segment
    # (rather than wired into the reverse group) lets both forms match.
    r"\s*/?\s*"
    r"(?:\b(?P<reverse>Linkslauf|sens\s+inverse|reverse|counter[-\s]?clockwise|anticlockwise)\b)?"
    r"\s*/?\s*"
    r"(?:"
    # Canonical "Stufe N" / "speed N" with numeric or soft token.
    r"\b(?:Stufe|speed|level)\b\s*(?P<speed>\d+(?:[.,]\d+)?|sanft\b|soft\b)"
    r"|"
    # German compound shorthand that already carries the "soft" meaning.
    r"\b(?P<soft_compound>Sanftrührstufe)\b"
    r"|"
    # TM7 browning-mode stir pattern (``Anbratstufe`` / ``Bratstufe``).
    # Emitted as TTS with ``speed="anbrat"`` so the wire payload stays
    # distinguishable from a normal numeric speed and from soft mode.
    r"\b(?P<brown_compound>Anbratstufe|Bratstufe)\b"
    r")"
    # Cookidoo and LLM-generated wording is inconsistent about where the
    # reverse-blade indicator sits relative to the speed: both
    # ``/Linkslauf/Stufe 1`` (canonical, captured by ``reverse`` above) and
    # ``/Stufe 1/Linkslauf`` (frequent in modern TM7 recipes) appear. We
    # try a trailing slot too so the span and direction are preserved
    # either way.
    r"(?:\s*/?\s*\b(?P<reverse_after>Linkslauf|sens\s+inverse|reverse|counter[-\s]?clockwise|anticlockwise)\b)?",
    re.IGNORECASE,
)

_SOFT_SPEED_TOKENS = frozenset({"sanft", "soft"})
_SOFT_SPEED = "soft"
_BROWN_SPEED = "anbrat"

_BROWNING_PATTERN = re.compile(
    r"(?P<minutes>\d+)\s*Min\.?"
    r"\s*/\s*(?P<temperature>\d+)\s*°\s*C"
    r"\s*/\s*(?P<power>Leicht|Intensiv|Gentle|Intense)",
    re.IGNORECASE,
)

_DOUGH_PATTERN = re.compile(
    r"(?P<minutes>\d+)\s*(?:Min\.?|minutes?|minuten?)"
    r"\s*/\s*"
    r"\b(?:Teigstufe|Stufe\s+Teig|dough\s+mode|knead(?:ing)?\s+mode)\b",
    re.IGNORECASE,
)

_SECONDS_PER_MINUTE = 60
_BROWNING_TEMPERATURES = frozenset({"140", "145", "150", "155", "160"})
_BROWNING_TIME_RANGE_SECONDS = (1, 1800)

# Unit tokens that may precede the head noun of an ingredient line. Matched
# case-insensitively, with an optional trailing period stripped before lookup.
# Multi-locale coverage (DE/EN/FR/IT/ES/NL) follows Cookidoo's supported
# storefronts. Generic adjectives ("groß", "klein", "frisch", "fresh", "fresco")
# are *not* listed because we only want to drop a unit, never an adjective that
# is actually part of the head noun.
_INGREDIENT_UNIT_TOKENS = frozenset(
    {
        # German: weight / volume
        "g", "gr", "gramm", "kg", "kilogramm", "mg", "ml", "milliliter",
        "cl", "l", "liter", "dl",
        # German: spoons / pinches / drops
        "el", "esslöffel", "tl", "teelöffel", "msp", "messerspitze",
        "messerspitzen", "prise", "prisen", "schuss", "spritzer", "tropfen",
        "handvoll", "klacks", "schluck",
        # German: counted / packaged
        "stk", "stück", "stücke", "pck", "päckchen", "pkg", "packung",
        "packungen", "dose", "dosen", "becher", "tasse", "tassen", "glas",
        "gläser", "tüte", "tüten", "beutel", "schale", "schalen", "tafel",
        "tafeln", "riegel", "block", "blöcke", "kanne", "kannen", "kelle",
        "kellen", "karton", "kartons", "flasche", "flaschen", "kugel",
        "kugeln", "portion", "portionen", "würfel", "schälchen", "schüssel",
        # German: produce / herbs
        "bund", "bünde", "bündel", "sträußchen", "strauß", "zweig", "zweige",
        "stiel", "stiele", "stängel", "stange", "stangen", "blatt", "blätter",
        "scheibe", "scheiben", "kopf", "köpfe", "zehe", "zehen", "knolle",
        "knollen", "rispe", "rispen", "büschel", "körner", "flocken", "ähre",
        "ähren",
        # English: weight / volume
        "oz", "lb", "lbs", "fl",
        # English: spoons / pinches
        "tsp", "tbsp", "tbs", "pinch", "pinches", "dash", "dashes", "drop",
        "drops", "handful", "knob",
        # English: counted / packaged
        "can", "cans", "jar", "jars", "packet", "packets", "bag", "bags",
        "box", "boxes", "carton", "cartons", "bottle", "bottles", "cup",
        "cups", "glass", "glasses", "stick", "sticks", "slice", "slices",
        "piece", "pieces", "blocks", "loaf", "loaves",
        # English: produce
        "bunch", "bunches", "sprig", "sprigs", "stalk", "stalks", "stem",
        "stems", "clove", "cloves", "head", "heads", "leaf", "leaves",
        # French: weight / volume
        "gramme", "grammes", "litre", "litres",
        # French: spoons / pinches
        "cuillère", "cuillères", "cuillerée", "cuillerées", "càs", "càc",
        "pincée", "pincées", "goutte", "gouttes", "poignée", "poignées",
        # French: counted / packaged
        "boîte", "boîtes", "sachet", "sachets", "paquet", "paquets", "verre",
        "verres", "bouteille", "bouteilles", "pot", "pots", "bocal", "bocaux",
        "tranche", "tranches", "morceau", "morceaux", "tablette", "tablettes",
        # French: produce
        "botte", "bottes", "brin", "brins", "branche", "branches", "gousse",
        "gousses", "tige", "tiges", "feuille", "feuilles", "tête", "têtes",
        "bouquet", "bouquets", "grappe", "grappes",
        # Italian: weight / volume (mostly shares g/kg/ml with German)
        "grammo", "grammi", "litro", "litri",
        # Italian: spoons / pinches
        "cucchiaio", "cucchiai", "cucchiaino", "cucchiaini", "pizzico",
        "pizzichi", "manciata", "manciate", "goccia", "gocce",
        # Italian: counted / packaged
        "scatola", "scatole", "barattolo", "barattoli", "bustina", "bustine",
        "confezione", "confezioni", "bicchiere", "bicchieri", "tazza", "tazze",
        "bottiglia", "bottiglie", "vasetto", "vasetti", "fetta", "fette",
        "pezzo", "pezzi", "tavoletta", "tavolette",
        # Italian: produce
        "mazzo", "mazzi", "mazzetto", "mazzetti", "ramo", "rami", "rametto",
        "rametti", "spicchio", "spicchi", "gambo", "gambi", "foglia", "foglie",
        "testa", "teste", "grappolo", "grappoli", "ciuffo", "ciuffi",
        # Spanish: weight / volume
        "gramo", "gramos", "litros",
        # Spanish: spoons / pinches
        "cucharada", "cucharadas", "cucharadita", "cucharaditas", "pizca",
        "pizcas", "puñado", "puñados", "gota", "gotas", "chorro", "chorros",
        # Spanish: counted / packaged
        "lata", "latas", "bote", "botes", "sobre", "sobres", "paquete",
        "paquetes", "vaso", "vasos", "taza", "tazas", "botella", "botellas",
        "frasco", "frascos", "tableta", "tabletas", "rodaja", "rodajas",
        "trozo", "trozos",
        # Spanish: produce
        "manojo", "manojos", "rama", "ramas", "ramita", "ramitas", "diente",
        "dientes", "tallo", "tallos", "hoja", "hojas", "cabeza", "cabezas",
        "racimo", "racimos",
        # Dutch: weight / volume (g/kg/ml shared)
        "gram", "kilo",
        # Dutch: spoons / pinches
        "eetlepel", "eetlepels", "theelepel", "theelepels", "snufje",
        "snufjes", "scheutje", "scheutjes", "handje", "handjes",
        # Dutch: counted / packaged
        "blikje", "blikjes", "blik", "blikken", "zakje", "zakjes", "pakje",
        "pakjes", "pak", "pakken", "fles", "flessen", "glazen",
        "kop", "koppen", "kopje", "kopjes", "plak", "plakken", "stuk",
        "stukken", "reep", "repen",
        # Dutch: produce
        "bos", "bosjes", "bosje", "tak", "takken", "takje", "takjes", "teen",
        "tenen", "blad", "bladen", "bladeren", "stengel", "stengels", "krop",
        "kroppen",
    }
)  # fmt: skip

# Linker / preposition words that follow a unit in many Romance languages and
# in English. After unit stripping, one such word is consumed greedily so that
# ``1 cuillère de farine`` reduces to head ``farine``. Articles like ``la`` /
# ``les`` are deliberately *not* listed — they are too short and would risk
# truncating two-word ingredient names.
_INGREDIENT_LINKER_TOKENS = frozenset(
    {
        "de", "du", "des", "di", "dei", "del", "dello", "della", "delle",
        "degli", "of", "van",
    }
)  # fmt: skip

# Quantity prefix: an integer/decimal (with optional fraction-slash or range
# dash), or a Unicode vulgar fraction, optionally followed by a mixed-fraction
# component. Covers ``1``, ``1,5``, ``1.5``, ``1/4``, ``2-3``, ``½``, ``1 ½``.
_VULGAR_FRACTIONS = "½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
_INGREDIENT_PAREN_PATTERN = re.compile(r"\([^)]*\)")
_INGREDIENT_QUANTITY_PATTERN = re.compile(
    r"^\s*"
    rf"(?:\d+(?:[.,/\-]\d+)?|[{_VULGAR_FRACTIONS}])"
    rf"(?:\s+[{_VULGAR_FRACTIONS}])?"
    r"\s*"
)
# Match the next whitespace-separated word. ``[^\W\d_]`` is "Unicode letter"
# (re's word class minus digits and underscore), so French/Italian/Spanish/
# Dutch accented forms (``cuillère``, ``boîte``, ``puñado``, ...) all parse.
_INGREDIENT_UNIT_HEAD_PATTERN = re.compile(r"^([^\W\d_]+)\.?\s+(.+)$")

# Minimum head length above which compound-suffix matching kicks in (``\w*``).
# Below it, only known German plural / case suffixes are accepted to avoid
# matching unrelated short words (e.g. ``Ei`` → ``Eintopf``).
_INGREDIENT_COMPOUND_MIN_LENGTH = 5
_INGREDIENT_SHORT_SUFFIX_MIN_LENGTH = 3
# German noun inflection endings ordered longest-first so the regex prefers
# the most specific match. ``\b<head>(?:nen|ern|en|...)?\b`` then catches
# ``Zwiebel`` → ``Zwiebeln`` without over-extending into unrelated text.
_GERMAN_NOUN_SUFFIXES = ("nen", "ern", "en", "er", "es", "n", "s", "e")

# Alternation of every known unit token, sorted longest-first so the regex
# engine prefers a multi-char unit over a substring that happens to match a
# shorter one. Used inside the optional quantity prefix of the match pattern.
_UNIT_ALTERNATION = "|".join(
    re.escape(u) for u in sorted(_INGREDIENT_UNIT_TOKENS, key=len, reverse=True)
)

# Optional ``<quantity> [<unit>]`` prefix that the match pattern can absorb
# before the head. The unit is itself optional so count-only quantities work
# (``1 Ei``, ``2 Zwiebeln`` in step text). ``[.,/-]`` covers ``1,5``, ``1.5``,
# ``1/4`` and ranges like ``2-3``.
_QUANTITY_PREFIX = (
    rf"(?:\d+(?:[.,/\-]\d+)?|[{_VULGAR_FRACTIONS}])"
    rf"(?:\s+[{_VULGAR_FRACTIONS}])?"
)

# Portion words that may *end* a German compound ingredient name (e.g.
# ``Spargelstücke`` = ``Spargel`` + ``stücke``, ``Ziegenkäsescheiben`` =
# ``Ziegenkäse`` + ``scheiben``). When the primary head ends with one of
# these and the remainder is long enough to still trigger compound-prefix
# matching, the parser also yields the shorter form as a secondary head.
# Length >= 5 chars keeps common bare-unit tokens (``g``, ``ml``, ``EL``)
# out of the suffix list — those should never be stripped from a head.
_COMPOUND_PORTION_SUFFIXES = tuple(
    sorted(
        (u for u in _INGREDIENT_UNIT_TOKENS if len(u) >= _INGREDIENT_COMPOUND_MIN_LENGTH),
        key=len,
        reverse=True,
    )
)


class AnnotationStrategy(Protocol):
    def detect(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]: ...


class BrowningStrategy:
    """Detects ``<n> Min./<temp> °C/(Leicht|Intensiv)`` spans and emits MODE/BROWNING.

    Cookidoo only accepts a small whitelist of browning temperatures
    (140..160 °C in 5 °C steps); spans outside that range are skipped.
    """

    def detect(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]:
        del ingredients
        annotations: list[StepAnnotation] = []
        for match in _BROWNING_PATTERN.finditer(text):
            seconds = int(match.group("minutes")) * _SECONDS_PER_MINUTE
            low, high = _BROWNING_TIME_RANGE_SECONDS
            if not low <= seconds <= high:
                continue
            temperature_value = match.group("temperature")
            if temperature_value not in _BROWNING_TEMPERATURES:
                continue
            annotations.append(
                BrowningModeAnnotation(
                    data=BrowningModeData(
                        time=seconds,
                        temperature=TemperatureData(value=temperature_value),
                        power=_browning_power(match.group("power")),
                    ),
                    offset=match.start(),
                    length=match.end() - match.start(),
                )
            )
        return annotations


class TtsStrategy:
    """Detects ``<time>/Stufe <speed>`` spans, optionally carrying temperature.

    A Varoma span is emitted as ``MODE/STEAMING``; everything else with an
    explicit time + speed becomes a ``TTS`` annotation. The optional
    ``°C`` / ``Linkslauf`` blocks are captured but not required.
    """

    def detect(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]:
        del ingredients
        annotations: list[StepAnnotation] = []
        for match in _TTS_PATTERN.finditer(text):
            total_seconds = _combined_time_seconds(match.group("minutes"), match.group("seconds"))
            if total_seconds <= 0:
                continue
            speed = _resolve_speed(
                match.group("speed"),
                match.group("soft_compound"),
                match.group("brown_compound"),
            )
            offset = match.start()
            length = match.end() - match.start()
            reverse_matched = bool(match.group("reverse") or match.group("reverse_after"))
            if match.group("varoma"):
                # Steaming.direction is required (defaults to CW). Linkslauf
                # flips it to CCW; absent reverse keeps the CW default.
                annotations.append(
                    SteamingModeAnnotation(
                        data=SteamingModeData(
                            time=total_seconds,
                            speed=speed,
                            direction=(
                                MixDirection.COUNTER_CLOCKWISE
                                if reverse_matched
                                else MixDirection.CLOCKWISE
                            ),
                        ),
                        offset=offset,
                        length=length,
                    )
                )
                continue
            temperature_value = match.group("temperature")
            temperature = (
                TemperatureData(value=temperature_value) if temperature_value is not None else None
            )
            annotations.append(
                TtsAnnotation(
                    data=TtsAnnotationData(
                        speed=speed,
                        time=total_seconds,
                        temperature=temperature,
                        # TTS.direction is optional — only set on reverse-blade,
                        # otherwise the wire payload omits the field entirely.
                        direction=MixDirection.COUNTER_CLOCKWISE if reverse_matched else None,
                    ),
                    offset=offset,
                    length=length,
                )
            )
        return annotations


class DoughStrategy:
    """Detects ``<n> Min./Teigstufe`` spans and emits ``MODE/dough``.

    Cookidoo's dough/knead mode has no temperature or speed parameters — the
    span only carries a duration. The pattern is intentionally narrow
    (``Teigstufe``, ``Stufe Teig``, English ``dough mode`` / ``knead mode``)
    to avoid matching unrelated dough references in prose.
    """

    def detect(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]:
        del ingredients
        annotations: list[StepAnnotation] = []
        for match in _DOUGH_PATTERN.finditer(text):
            seconds = int(match.group("minutes")) * _SECONDS_PER_MINUTE
            annotations.append(
                DoughModeAnnotation(
                    data=DoughModeData(time=seconds),
                    offset=match.start(),
                    length=match.end() - match.start(),
                )
            )
        return annotations


class IngredientStrategy:
    """Annotates ingredient-list mentions inside step text.

    For each ingredient line the strategy extracts one or more candidate
    heads (the primary head from quantity/unit/parenthesis stripping, plus
    a shorter secondary head when the primary ends with a portion word
    like ``Spargel`` + ``stücke``). Each head is matched in the step text
    with a length-gated pattern:

    - **≥ 5 characters**: compound-prefix tolerant. ``Petersilie`` matches
      inside ``Petersilienblättchen``; ``Zwiebel`` matches ``Zwiebeln``.
      The annotated span covers the **whole** compound.
    - **3-4 characters**: only known German noun inflection endings are
      accepted (``Salz`` → ``Salzen``). Compound matching is disabled to
      avoid false positives on shared stems (``Reis`` → ``Reisebus``).
    - **≤ 2 characters**: exact match only. Very short heads (``Ei``,
      ``Öl``) would otherwise collide with unrelated function words.

    Each match also pulls in any ``<quantity> [<unit>]`` prefix that
    immediately precedes the head in the step text — so the annotated
    span becomes ``"20 g Haselnüsse"`` (not just ``"Haselnüsse"``) when
    the measurement is repeated inline. When no measurement precedes the
    noun, only the noun is highlighted.

    The leading word boundary is always strict: a head never matches as
    the *suffix* of a compound (``Öl`` does not match inside ``Olivenöl``
    — that would refer to a different ingredient).

    The ``description`` of the emitted annotation is the **full canonical
    ingredient line**, not the matched substring — Cookidoo expects the
    canonical entry there so the app can resolve quantities.
    """

    def detect(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]:
        annotations: list[StepAnnotation] = []
        for ingredient in ingredients:
            # Per-ingredient dedupe: a primary and a secondary head can match
            # the very same span in step text (e.g. both ``Spargelstücke`` and
            # ``Spargel`` resolve to ``Spargelstücke``); only emit one
            # annotation in that case.
            seen_spans: set[tuple[int, int]] = set()
            for head in _extract_ingredient_heads(ingredient):
                pattern = re.compile(_ingredient_match_pattern(head), re.IGNORECASE)
                for match in pattern.finditer(text):
                    span = (match.start(), match.end() - match.start())
                    if span in seen_spans:
                        continue
                    seen_spans.add(span)
                    annotations.append(
                        IngredientAnnotation(
                            data=IngredientAnnotationData(description=ingredient),
                            offset=match.start(),
                            length=match.end() - match.start(),
                        )
                    )
        return annotations


class AnnotationInferrer:
    """Aggregates annotation strategies and removes overlapping spans."""

    def __init__(self, strategies: tuple[AnnotationStrategy, ...] | None = None) -> None:
        self._strategies = strategies if strategies is not None else default_strategies()

    def infer(self, text: str, ingredients: Sequence[str]) -> list[StepAnnotation]:
        candidates: list[StepAnnotation] = []
        for strategy in self._strategies:
            candidates.extend(strategy.detect(text, ingredients))
        return _drop_overlaps(candidates)


def default_strategies() -> tuple[AnnotationStrategy, ...]:
    """Return the default strategy set.

    Order matters only for the overlap tie-breaker: MODE strategies
    (BROWNING, DOUGH) are checked before TTS so that a MODE span wins over
    any partial TTS match starting at the same offset.
    """
    return (BrowningStrategy(), DoughStrategy(), IngredientStrategy(), TtsStrategy())


def _combined_time_seconds(minutes: str | None, seconds: str | None) -> int:
    return (int(minutes) if minutes else 0) * _SECONDS_PER_MINUTE + (int(seconds) if seconds else 0)


def _ingredient_match_pattern(head: str) -> str:
    """Build the length-gated regex pattern for an ingredient head.

    The leading ``\\b`` is always strict — a head never matches as the
    suffix of a compound. The pattern optionally consumes a ``<quantity>
    [<unit>]`` prefix when one immediately precedes the head in the step
    text, so the annotated span includes the measurement. The trailing
    edge is gated:

    - ``>= 5`` chars: any word-character continuation (compound prefix).
    - ``3-4`` chars: optional known German inflection ending only.
    - ``<= 2`` chars: exact match only.
    """
    escaped = re.escape(head)
    prefix = rf"(?:{_QUANTITY_PREFIX}\s+(?:(?:{_UNIT_ALTERNATION})\.?\s+)?)?"
    if len(head) >= _INGREDIENT_COMPOUND_MIN_LENGTH:
        tail = r"\w*"
    elif len(head) >= _INGREDIENT_SHORT_SUFFIX_MIN_LENGTH:
        suffixes = "|".join(_GERMAN_NOUN_SUFFIXES)
        tail = rf"(?:{suffixes})?"
    else:
        tail = ""
    return rf"\b{prefix}{escaped}{tail}\b"


def _extract_ingredient_heads(ingredient: str) -> list[str]:
    """Return every candidate head for an ingredient.

    Always includes the primary head from
    :func:`_extract_ingredient_head`. Two secondary heads are added when
    they yield meaningful alternatives:

    - **Compound-portion strip**: when the head ends with a known portion
      word (``Spargelstücke`` ends with ``stücke``, ``Ziegenkäsescheiben``
      ends with ``scheiben``), the shorter form (``Spargel``,
      ``Ziegenkäse``) is also yielded.
    - **Last-word fallback**: when the head is multiple whitespace-
      separated words (``"weißer Spargel"`` — adjective + noun), the last
      word is yielded *if* it is long enough for compound matching
      (≥ 5 chars). This lets ingredient lines with declined adjectives
      still match step text that uses only the noun
      (``"weißen Spargel"`` / ``"Spargelstücke"`` / ``"Spargel"``).
    """
    primary = _extract_ingredient_head(ingredient)
    if not primary:
        return []
    heads = [primary]
    short = _strip_compound_portion_suffix(primary)
    if short and short != primary and short not in heads:
        heads.append(short)
    last_word = _last_word_if_compound_matchable(primary)
    if last_word and last_word not in heads:
        heads.append(last_word)
    return heads


def _strip_compound_portion_suffix(head: str) -> str | None:
    """Strip a known portion-word suffix from ``head`` if doing so leaves a
    prefix that is still long enough for compound matching. Returns
    ``None`` when no suffix matches (most ingredient heads)."""
    lower = head.lower()
    for suffix in _COMPOUND_PORTION_SUFFIXES:
        if lower.endswith(suffix) and len(head) - len(suffix) >= _INGREDIENT_COMPOUND_MIN_LENGTH:
            return head[: -len(suffix)]
    return None


def _last_word_if_compound_matchable(head: str) -> str | None:
    """Return the last whitespace-separated word of ``head`` when the head
    is multi-word and the last word is at least
    :data:`_INGREDIENT_COMPOUND_MIN_LENGTH` characters long. In German
    recipes a multi-word head is typically *adjective + noun* (``weißer
    Spargel``), and the inflected adjective varies with case in step text
    while the noun stays stable — matching just the noun keeps coverage."""
    parts = head.split()
    if len(parts) < 2:
        return None
    last = parts[-1]
    if len(last) < _INGREDIENT_COMPOUND_MIN_LENGTH:
        return None
    return last


def _extract_ingredient_head(ingredient: str) -> str | None:
    """Return the noun-phrase head of an ingredient line, or ``None``.

    Strips parentheticals, a leading quantity, a recognised unit, an
    optional Romance/English linker word (``de``, ``di``, ``of``, ...) and
    any trailing comma description:

    - ``"100 ml Weißwein, trocken"`` → ``"Weißwein"``
    - ``"1 cuillère de farine"`` → ``"farine"``
    - ``"2 spicchi di aglio"`` → ``"aglio"``

    Returns ``None`` for empty / unparseable inputs.
    """
    cleaned = _INGREDIENT_PAREN_PATTERN.sub("", ingredient).strip()
    if not cleaned:
        return None
    quantity_match = _INGREDIENT_QUANTITY_PATTERN.match(cleaned)
    rest = cleaned[quantity_match.end() :] if quantity_match else cleaned
    if quantity_match:
        rest = _strip_leading_token(rest, _INGREDIENT_UNIT_TOKENS)
        rest = _strip_leading_token(rest, _INGREDIENT_LINKER_TOKENS)
    head = rest.split(",", maxsplit=1)[0].strip()
    return head or None


def _strip_leading_token(text: str, allowlist: frozenset[str]) -> str:
    """Drop the first whitespace-separated word from ``text`` iff it is in
    ``allowlist`` (case-insensitive, trailing ``.`` ignored). Returns the
    text unchanged otherwise so ingredient nouns are never truncated."""
    match = _INGREDIENT_UNIT_HEAD_PATTERN.match(text)
    if match and match.group(1).rstrip(".").lower() in allowlist:
        return match.group(2)
    return text


def _resolve_speed(
    numeric: str | None, soft_compound: str | None, brown_compound: str | None
) -> str:
    if soft_compound is not None:
        return _SOFT_SPEED
    if brown_compound is not None:
        return _BROWN_SPEED
    assert numeric is not None, "regex guarantees one of the three speed groups matches"
    return _SOFT_SPEED if numeric.lower() in _SOFT_SPEED_TOKENS else numeric


def _browning_power(raw: str) -> BrowningPower:
    return BrowningPower.INTENSE if raw.lower() in {"intensiv", "intense"} else BrowningPower.GENTLE


def _drop_overlaps(annotations: list[StepAnnotation]) -> list[StepAnnotation]:
    annotations.sort(key=lambda a: (a.offset, -a.length))
    result: list[StepAnnotation] = []
    last_end = -1
    for annotation in annotations:
        if annotation.offset < last_end:
            continue
        result.append(annotation)
        last_end = annotation.offset + annotation.length
    return result


__all__ = [
    "AnnotationInferrer",
    "AnnotationStrategy",
    "BrowningStrategy",
    "DoughStrategy",
    "IngredientStrategy",
    "TtsStrategy",
    "default_strategies",
]
