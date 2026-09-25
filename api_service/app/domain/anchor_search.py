"""Shared anchor-phrase word-list search primitives.

Originally lived only inside `generic_matrix_extraction.py` (the numeric
anchor mechanism). Pulled out so every generic (data-driven, not per-code)
extractor added afterwards -- enum-class, compound, table-row-count -- finds
its anchor phrase in a page's word list the same, already-proven way instead
of each reinventing its own tokenizer/matcher. Pure stdlib + `re`, no
SQLAlchemy/DB coupling, so it stays importable from `evaluation/*` reporting
scripts as well as the API service runtime.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .matrix_unit_classifier import normalize_anchor_text

# A number is either self-contained in one PDF word ("4650,91", or even
# "13 060,00" if the PDF embedded a non-breaking thousands separator inside
# one visual token), or split across exactly two adjacent words where the
# second is a bare 3-digit continuation fragment ("17" + "140,2"). This is a
# word-boundary-safe way to support thousands separators: operating on
# fitz's own word list means a separator can only ever bridge two ADJACENT
# WORD TOKENS, never split a token fitz already gave us as one -- so
# "13060,00" (one token) can no longer be misread as a nearby unrelated row
# index's first three digits, a real bug the original numeric-anchor
# mechanism hit and fixed forensically (see CASE10_MATRIX_132_COVERAGE.md).
SELF_CONTAINED_NUMBER_RE = re.compile(r"^-?\d+(?:[  ]\d{3})*(?:[.,]\d+)?$")
BARE_LEAD_DIGITS_RE = re.compile(r"^-?\d{1,3}$")
CONTINUATION_FRAGMENT_RE = re.compile(r"^\d{3}(?:[.,]\d+)?$")

DEFAULT_MAX_LOOKAHEAD_WORDS = 25

# The four generic (data-driven, not per-code) anchor mechanisms built on top
# of this module -- generic_matrix_extraction.py, generic_enum_extraction.py,
# generic_compound_extraction.py, generic_table_row_count.py -- each read one
# object-wide scalar/enum/tuple/count off a summary table (ТЭП, a site-level
# ГПЗУ table, an apartment/parking specification), never a specific room or
# construction element: nothing in the anchor-phrase-plus-nearby-value search
# they share determines which structural element a value belongs to. Per the
# organizer's own gold-check convention (`location_type: "SITE"`, e.g.
# SPZU-027 in `all_gold_checks.jsonl`), a parameter with no location concept
# is scored against this literal sentinel, never an empty string and never an
# invented room/element name these mechanisms have no way to actually
# determine (see CASE10_MATRIX_132_COVERAGE.md).
GENERIC_SITE_LOCATION = "SITE"


def to_decimal(token: str) -> Decimal | None:
    cleaned = token.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def numbers_after(
    words: list[dict[str, Any]], start_index: int, max_lookahead: int = DEFAULT_MAX_LOOKAHEAD_WORDS,
) -> list[tuple[str, int, int]]:
    """Numbers found in the `max_lookahead` words after `start_index`, as
    (raw_text, first_word_index, last_word_index) triples, in the order they
    appear. See the module-level comment above for the merge rule."""
    end = min(len(words), start_index + max_lookahead)
    results: list[tuple[str, int, int]] = []
    i = start_index
    while i < end:
        raw = normalize_word(words[i].get("text"))
        # Check the thousands-continuation merge BEFORE treating a bare 1-3
        # digit word as already-complete: both "17" (start of "17 140,2")
        # and "1" (an unrelated "№" column value) match `BARE_LEAD_DIGITS_RE`
        # on their own, so only actually looking at the *next* word tells
        # them apart -- see the module-level comment on this merge rule.
        if BARE_LEAD_DIGITS_RE.match(raw) and i + 1 < end:
            next_raw = normalize_word(words[i + 1].get("text"))
            if CONTINUATION_FRAGMENT_RE.match(next_raw):
                results.append((raw + next_raw, i, i + 1))
                i += 2
                continue
        if SELF_CONTAINED_NUMBER_RE.match(raw):
            results.append((raw, i, i))
            i += 1
            continue
        i += 1
    return results

# How many extra words may appear between two consecutive anchor words (real
# tables insert a qualifier, e.g. catalog "Площадь здания" appearing as
# "Площадь жилого здания" on a residential project).
DEFAULT_MAX_ANCHOR_GAP_WORDS = 2


def normalize_word(text: object) -> str:
    return str(text or "").lower().replace("ё", "е").strip("   .,;:")


# A catalog `parameter_name` routinely ends in a parenthesized qualifier that
# disambiguates it from a sibling code ("Строительный объем (Общий)" vs.
# "... (Подземный)"/"... (Надземный)") but a real document's own table label
# almost never repeats that qualifier verbatim (real LOS3A RD table row:
# "Строительный объём" alone) -- 28/132 catalog parameter names carry one.
# Same pattern `derive_title_candidates` already strips from source-hint
# text, widened past its 12-char cap (tuned for a short discipline-code
# annotation like "(АР)") because a qualifier here is a whole clarifying
# phrase, e.g. "(Максимальная численность)".
_TRAILING_QUALIFIER_RE = re.compile(r"\s*\([^)]{1,40}\)\s*$")


def anchor_word_variants(anchor_phrase: str) -> list[list[str]]:
    """Candidate anchor word-sequences to try, most specific first: the full
    phrase; then, only if it has enough words to stay distinctive without it,
    the phrase with its leading word dropped, for the common case where a
    real document's leading qualifier is a synonym the anchor text does not
    literally contain (e.g. "Площадь жилого здания" for catalog "Общая
    площадь здания"); then the same two with any trailing parenthesized
    qualifier stripped (see `_TRAILING_QUALIFIER_RE`). Each later variant is
    only tried once every earlier, more specific one failed to match
    anywhere; duplicates (no qualifier/leading-word to strip) are skipped."""
    seen: set[tuple[str, ...]] = set()
    variants: list[list[str]] = []

    def add(phrase: str) -> None:
        words = [w for w in normalize_anchor_text(phrase).split(" ") if w]
        key = tuple(words)
        if not words or key in seen:
            return
        seen.add(key)
        variants.append(words)

    for phrase in (anchor_phrase, _TRAILING_QUALIFIER_RE.sub("", str(anchor_phrase or ""))):
        add(phrase)
        words = [w for w in normalize_anchor_text(phrase).split(" ") if w]
        if len(words) >= 3:
            add(" ".join(words[1:]))
    return variants


# A real numbered table/spec row conventionally opens with its own item
# number ("5.", "11") immediately before the label; a descriptive preamble
# sentence that happens to repeat the same anchor words in passing does not.
# Used only as a TIE-breaker between two matches that already have the exact
# same `total_gap` (see `find_anchor_end_word_index`) -- deliberately narrow
# (bare 1-3 digit token, optional trailing period, already stripped by
# `normalize_word`) so it can only ever discriminate a genuine collision,
# never misfire on ordinary prose that happens to start a sentence with a
# small number.
_ROW_ITEM_PREFIX_RE = re.compile(r"^\d{1,3}$")


def _row_item_prefix_before(norm_words: list[str], start_index: int) -> bool:
    return start_index > 0 and bool(_ROW_ITEM_PREFIX_RE.match(norm_words[start_index - 1]))


def find_anchor_end_word_index(
    norm_words: list[str], anchor_words: list[str], max_gap: int = DEFAULT_MAX_ANCHOR_GAP_WORDS,
) -> int | None:
    """Among every position where `anchor_words` occurs, in order, as a
    subsequence of `norm_words` with at most `max_gap` extra words between
    any two consecutive anchor words, returns the index right after the
    TIGHTEST such match (smallest total gap across every consecutive pair;
    ties broken by whether a real table row's own item-number prefix
    immediately precedes the match -- see `_row_item_prefix_before` --  and
    only then by earliest position) -- not simply the first one found by
    reading order.

    Real forensic finding (a genuine LOS3A PD-stage page, see the live-
    candidate-tagger checkpoint memory): the 2-word fallback variant
    ["площадь", "здания"] (catalog "Общая площадь здания" with its leading
    qualifier dropped, see `anchor_word_variants`) matched TWO real
    positions on one page -- a coincidental, gap-2 co-occurrence inside an
    unrelated running-prose clause about a DIFFERENT sub-quantity
    ("...площадь подземной части здания..."), positioned earlier in the
    document than the genuine, gap-0 table label ("Площадь здания
    (по...)"). Read-order-first-match picked the wrong (earlier, looser)
    one and silently returned an unrelated row's number. A real table
    label's words sit directly next to each other; a same-words collision
    inside flowing prose almost never does -- preferring tightness over
    position is a strictly more specific criterion, so it can only ever
    fix such a false positive, never turn a real single match into a miss
    (with only one match, gap-comparison is moot and the old result is
    unchanged).

    A second, real forensic finding (LOS3A PD page 14, PZ-004, "Строительный
    объем"): the phrase can ALSO tie at total_gap=0 twice on the same page --
    once inside a descriptive preamble sentence listing what the section
    covers ("...площадь застройки, общая площадь, строительный объем (в т.ч.
    подземной части), ...") and once in the real numbered table row ("5.
    Строительный объем в т.ч. 88264,00 м3 ..."). Gap comparison alone cannot
    break this tie (both are exactly as tight); the row-item-prefix check
    can, since only the real row's own line is preceded by its item number
    ("5."). Purely a tie-break on top of the existing gap comparison -- a
    strictly tighter (smaller-gap) match elsewhere on the page still always
    wins regardless of either side's prefix, so this can only change the
    outcome of an already-ambiguous tie, never override a genuinely tighter
    match."""
    if not anchor_words:
        return None
    n = len(norm_words)
    best_key: tuple[int, int, int] | None = None  # (total_gap, lacks_row_prefix, start)
    best_end: int | None = None
    for start in range(n):
        if norm_words[start] != anchor_words[0]:
            continue
        pos = start
        total_gap = 0
        matched = True
        for target in anchor_words[1:]:
            next_pos = None
            for candidate in range(pos + 1, min(pos + 2 + max_gap, n)):
                if norm_words[candidate] == target:
                    next_pos = candidate
                    break
            if next_pos is None:
                matched = False
                break
            total_gap += next_pos - pos - 1
            pos = next_pos
        if not matched:
            continue
        lacks_row_prefix = 0 if _row_item_prefix_before(norm_words, start) else 1
        key = (total_gap, lacks_row_prefix, start)
        if best_key is None or key < best_key:
            best_key, best_end = key, pos + 1
    return best_end


def find_anchor_end_index_for_phrase(
    words: list[dict[str, Any]], anchor_phrase: str, *, max_gap: int = DEFAULT_MAX_ANCHOR_GAP_WORDS,
) -> int | None:
    """Convenience wrapper combining `anchor_word_variants` +
    `find_anchor_end_word_index` over a raw fitz-style word list (each a
    dict with a "text" key) -- the common entry point every extractor in
    this family actually calls. When no exact/qualifier-tolerant word
    sequence matches anywhere on the page, falls back to
    `semantic_anchor_fallback_index` (TZ 9.1's "семантические якоря") before
    giving up -- a no-op (returns the same `None`) whenever the optional
    embedding model is unavailable, so this is purely additive."""
    norm_words = [normalize_word(w.get("text")) for w in words]
    for anchor_words in anchor_word_variants(anchor_phrase):
        index = find_anchor_end_word_index(norm_words, anchor_words, max_gap)
        if index is not None:
            return index
    return semantic_anchor_fallback_index(words, anchor_phrase)


def semantic_anchor_fallback_index(words: list[dict[str, Any]], anchor_phrase: str) -> int | None:
    """Fallback for when the anchor phrase does not appear on the page as a
    literal (or one-qualifier-tolerant) word sequence at all -- e.g. a real
    document rewords a catalog label past what `anchor_word_variants`'
    "drop the leading word" heuristic can bridge. Y-clusters the page into
    text rows (`cluster_rows`, the same primitive `extract_table_fingerprint`
    uses) and asks `semantic_similarity.best_row_text_similarity` which row
    reads closest to `anchor_phrase`; on a hit, returns the flat-word-list
    index of that row's OWN first word, so the caller's existing
    forward-number-scan (`numbers_after`, unchanged) picks up the row's value
    exactly as it would after a literal anchor match -- real ТЭП-style rows
    keep a label and its value on one line (verified forensically, see
    CASE10_MATRIX_132_COVERAGE.md), so scanning forward from the row start
    finds it. Returns `None` (never raises) whenever the embedding model is
    unavailable, the phrase/page has no usable text, or nothing clears
    `semantic_similarity.SEMANTIC_MIN_SIMILARITY` -- lazy-imports the ML
    dependency so a caller that never needs this path (deterministic match
    already succeeded, or the model is disabled) never pays for it."""
    if not words or not str(anchor_phrase or "").strip():
        return None
    try:
        from .semantic_similarity import best_row_text_similarity, is_anchor_fallback_enabled

        if not is_anchor_fallback_enabled():
            return None
    except Exception:
        return None
    rows = cluster_rows(words)
    if not rows:
        return None
    # Real regression (checkpoint 41, see test_case10_anchor_search.py's
    # `SemanticAnchorFallbackDisabledByDefaultTests`): a site-area row
    # ("Общая площадь участка ...") scores close enough to a building-area
    # query ("Общая площадь здания") on embedding similarity alone that it
    # can outrank the genuine row -- both share most of their vocabulary by
    # construction (same table TYPE, different SCOPE). Raising the
    # similarity threshold cannot fix this: the genuine match and this
    # near-miss occupy overlapping score ranges. A cheap, targeted lexical
    # gate can: reject any row whose own scope word (building vs.
    # site/territory) contradicts the anchor phrase's, BEFORE ranking by
    # embedding score -- narrows the candidate pool the embedding then picks
    # from, it never invents a match the embedding alone would not also
    # have to clear.
    candidate_positions = [i for i, row in enumerate(rows) if _scope_compatible(anchor_phrase, row)]
    if not candidate_positions:
        return None
    row_texts = [" ".join(str(w.get("text") or "") for w in rows[i]) for i in candidate_positions]
    try:
        best_local_index, _score = best_row_text_similarity(anchor_phrase, row_texts)
    except Exception:
        return None
    if best_local_index is None:
        return None
    row = rows[candidate_positions[best_local_index]]
    if not row:
        return None
    first_word = row[0]
    return next((i for i, w in enumerate(words) if w is first_word), None)


# Scope nouns that distinguish an object/building-level ТЭП row from a
# site/land-plot-level one -- the two table "kinds" this project's own
# documents routinely place side by side (see module docstring above and
# CASE10_MATRIX_132_COVERAGE.md's semantic-fallback finding). Deliberately
# small and inflected-form-explicit (not a stemmer) -- a false NEGATIVE here
# (missing an inflection) just falls through to "no scope word", which is
# always treated as compatible, never a false rejection.
_BUILDING_SCOPE_WORDS = frozenset({"здание", "здания", "зданию", "здании", "зданием", "домовладение", "домовладения"})
_SITE_SCOPE_WORDS = frozenset({
    "участок", "участка", "участку", "участке", "участком",
    "территория", "территории", "территорию", "территорией",
})


def _row_scope(text: str) -> frozenset[str]:
    tokens = {normalize_row_word(token) for token in str(text or "").split()}
    scopes = set()
    if tokens & _BUILDING_SCOPE_WORDS:
        scopes.add("BUILDING")
    if tokens & _SITE_SCOPE_WORDS:
        scopes.add("SITE")
    return frozenset(scopes)


def _scope_compatible(anchor_phrase: str, row: list[dict[str, Any]]) -> bool:
    """`True` unless the anchor phrase names exactly one scope (building XOR
    site) and the row's own text names only the other -- an unscoped anchor
    or an unscoped/ambiguous row is always compatible (this gate only ever
    narrows a genuine, known collision, never invents a new restriction)."""
    anchor_scopes = _row_scope(anchor_phrase)
    if not anchor_scopes:
        return True
    row_text = " ".join(str(w.get("text") or "") for w in row)
    row_scopes = _row_scope(row_text)
    if not row_scopes:
        return True
    return bool(anchor_scopes & row_scopes)


def row_text_at_word(words: list[dict[str, Any]], word_index: int | None) -> str | None:
    """Joins every word on the same Y-clustered visual row as
    `words[word_index]`, left-to-right -- typically a table row's own label
    AND its value together (verified forensically: a real ТЭП row like "3
    Площадь жилого здания, в т. ч.: кв. м - 17 140,2" keeps both on one
    line), unlike a fixed word-count context window centered on just the
    matched value, which can cut the label off entirely. Used as the text
    scored for semantic re-ranking/filtering (see `cross_stage_localization.
    pick_best_candidate`), never for the evidence-card `context` field shown
    to inspectors (that stays the existing value-centered window,
    unchanged)."""
    if not words or word_index is None or not (0 <= word_index < len(words)):
        return None
    anchor_word = words[word_index]
    rows = cluster_rows(words)
    row = next((r for r in rows if any(w is anchor_word for w in r)), None)
    if row is None:
        return None
    return " ".join(str(w.get("text") or "") for w in row)


def source_hints(param: Any) -> dict[str, str]:
    """Recovers the original `source_pd`/`source_rd`/`source_id` catalog
    *text* (e.g. "Раздел АР: Сводная экспликация квартир") for a runtime
    `Param` row. `Param.source_pd`/`source_rd`/`source_id` are booleans (is
    this stage required at all) -- the importer
    (`official_dataset.py::_param_from_catalog_row`) keeps the original text
    only inside `other_normative`, as a JSON blob alongside other catalog
    diagnostics. Shared (not table-row-count-specific despite living
    alongside `derive_title_candidates`) because `build_semantic_query` needs
    the same per-stage catalog text for every anchor mechanism."""
    import json

    raw = getattr(param, "other_normative", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "PD": str(parsed.get("source_pd") or ""),
        "RD": str(parsed.get("source_rd") or ""),
        "ID": str(parsed.get("source_id") or ""),
    }


def build_semantic_query(param: Any, stage: str) -> str:
    """The text embedded to represent a parameter for a given stage's
    semantic re-ranking/filtering: its own catalog name plus that stage's
    `source_pd`/`source_rd`/`source_id` hint text when available (e.g.
    "Общая площадь здания. Раздел АР: Лист \"Общие данные\", Сводная
    экспликация" for PZ-002/RD) -- the hint text names the real document
    section a genuine match should sit in, which measurably sharpens
    discrimination over the bare parameter name alone (see
    CASE10_MATRIX_132_COVERAGE.md)."""
    name = str(getattr(param, "parameter_name", "") or "").strip()
    hint = source_hints(param).get(stage, "").strip()
    if not hint:
        return name
    return f"{name}. {hint}"


# Real forensic finding (LOS3A PD-stage document, PZ-002 "Общая площадь
# здания"): a single document can carry TWO independently anchor-matching
# rows for the same phrase that are only distinguishable by which normative
# document each one's own text cites next to its value -- "Площадь здания
# (по СП 54.13330.2016, прил. А.1.2)" (the catalog's own intended
# definition) vs. "Площадь помещений здания (по СП 118.13330.2012, прил.
# Г.5)" (a related-but-distinct quantity from a different code). Neither
# `semantic_similarity.py`'s embedding (both share most of their vocabulary)
# nor plain anchor/gap matching (both are equally tight, unambiguous
# matches) can tell these apart; the normative citation each row's own
# nearby text names is the one textual signal that reliably does.
NORMATIVE_REFERENCE_RE = re.compile(
    r"(?:СП|СНиП|ГОСТ(?:\s*Р)?)\s+\d+(?:\.\d+){1,2}(?:-\d+)?", re.IGNORECASE,
)


def extract_normative_references(text: str | None) -> frozenset[str]:
    """Every "СП xx.xxxxx.xxxx"/"СНиП ..."/"ГОСТ [Р] ..." style normative
    citation literally present in `text`, normalized (whitespace-collapsed,
    uppercased) for set comparison -- an empty result means "no citation
    found in this text", never "this text names an empty/absent normative
    document", the same "no signal" convention every other optional
    discriminator in this module follows."""
    if not text:
        return frozenset()
    return frozenset(" ".join(match.split()).upper() for match in NORMATIVE_REFERENCE_RE.findall(str(text)))


def expected_normative_references(param: Any) -> frozenset[str]:
    """The normative reference(s) a parameter's OWN catalog row declares it
    should be measured against (`Param.sp_reference`/`gost_reference`/
    `fz_reference`), used as the comparison target for
    `extract_normative_references`'s result on a candidate's own nearby
    text (see `cross_stage_localization.StageCandidate.normative_mismatch`).
    Returns an empty set -- "no signal", not "candidate is wrong" -- whenever
    none of those catalog fields are populated; the official 132-parameter
    catalog import (`official_dataset.py`) does not currently set them for
    any parameter (see CASE10_MATRIX_132_COVERAGE.md), so this is dormant
    infrastructure until a catalog source populates them, exactly like
    `build_semantic_query`'s `source_pd`/`source_rd`/`source_id` hints were
    before this session -- never a silent no-op passed off as "checked"."""
    combined = " ".join(
        str(value) for value in (
            getattr(param, "sp_reference", None),
            getattr(param, "gost_reference", None),
            getattr(param, "fz_reference", None),
        ) if value
    )
    found = extract_normative_references(combined)
    if found:
        return found
    # A populated field that does not match the "СП/СНиП/ГОСТ NN.NNNNN"
    # shape at all (e.g. just "54.13330.2016", or a bare ФЗ number) is still
    # a real, catalog-declared expectation -- fall back to its own stripped,
    # normalized text rather than silently treating it as no signal.
    bare = " ".join(combined.split()).upper()
    return frozenset({bare}) if bare else frozenset()


def indexed_words(raw_words: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[tuple[int, int]]]:
    """Join a word list into one space-separated string plus, for each word,
    the (start, end) character offsets of its own token in that string --
    lets a regex be run once over normal flowing text (needed for
    multi-word/punctuation-tolerant patterns like class codes or grade
    marks) while still mapping a match straight back to the word(s), and
    therefore the bbox, it came from."""
    words = [word for word in raw_words if str(word.get("text") or "").strip()]
    parts, offsets, cursor = [], [], 0
    for word in words:
        token = str(word["text"])
        if parts:
            cursor += 1
        start = cursor
        parts.append(token)
        cursor += len(token)
        offsets.append((start, cursor))
    return " ".join(parts), words, offsets


def word_index_at(offsets: list[tuple[int, int]], position: int) -> int | None:
    for index, (start, end) in enumerate(offsets):
        if start <= position <= end:
            return index
    return None


def union_bbox(boxes: Any) -> list[float] | None:
    rows = [list(map(float, box)) for box in boxes if box and len(box) == 4]
    if not rows:
        return None
    return [min(r[0] for r in rows), min(r[1] for r in rows), max(r[2] for r in rows), max(r[3] for r in rows)]


def normalized_bbox(bbox: list[float], width: float, height: float) -> list[float]:
    width = width or 1.0
    height = height or 1.0
    return [
        max(0.0, min(1.0, bbox[0] / width)),
        max(0.0, min(1.0, bbox[1] / height)),
        max(0.0, min(1.0, bbox[2] / width)),
        max(0.0, min(1.0, bbox[3] / height)),
    ]


_TITLE_PREFIX_RE = re.compile(
    r"^\s*раздел\s+[^:.;]{0,30}[:.]\s*|^\s*лист\s+[^:.;]{0,30}[:.]\s*", re.IGNORECASE,
)


# Row/column structural fingerprinting -- shared by the numeric/enum/compound
# anchor mechanisms (via `extract_table_fingerprint`, called on the word index
# of whatever value each mechanism itself found) and by
# `generic_table_row_count.py` (which builds a `TableFingerprint` directly
# from its own already-detected header row, since a title anchor sits ABOVE
# its table rather than inside one -- see that module). Used to tell a real
# structural match ("same table columns, same order") apart from a page that
# merely repeats the anchor phrase's own words -- see
# CASE10_MATRIX_132_COVERAGE.md for the cross-stage-localization task this
# was built for.
ROW_Y_TOLERANCE = 3.5

# Column-header vocabulary for a ТЭП-style (technical-economic indicators)
# table, broadened past `generic_table_row_count.py`'s own narrower
# `_HEADER_MARKERS` (which is tuned for spec/exposition tables specifically)
# to also cover the label/unit/value header row of a plain numeric ТЭП table.
TEP_HEADER_MARKERS = frozenset({
    "№", "n", "п/п", "наименование", "показатель", "показателя", "показателей",
    "ед", "изм", "измерения", "единица", "величина", "значение", "количество",
    "кол-во", "поз", "поз.", "тип", "марка", "примечание", "номер", "площадь",
    "проектн", "проектное", "проектируемое", "рабочее", "рабоч", "факт",
    "фактич", "фактическое", "принято", "принят",
})


def normalize_row_word(text: object) -> str:
    return str(text or "").lower().replace("ё", "е").strip(" .,;:()")


def cluster_rows(words: list[dict[str, Any]], *, y_tolerance: float = ROW_Y_TOLERANCE) -> list[list[dict[str, Any]]]:
    """Bands a page's own words into text-line rows by Y-proximity, each row
    sorted left-to-right by X. Pure Y/X clustering (no PyMuPDF table-detection
    API involved) -- see `generic_table_row_count.py`'s module docstring for
    why this whole extractor family avoids `page.find_tables()`. Word dict
    references are preserved (never copied), so a caller can locate "the row
    containing this specific word" via identity comparison."""
    ordered = sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0]))
    rows: list[list[dict[str, Any]]] = []
    for word in ordered:
        y_center = (word["bbox"][1] + word["bbox"][3]) / 2
        if rows:
            reference = (rows[-1][0]["bbox"][1] + rows[-1][0]["bbox"][3]) / 2
            if abs(y_center - reference) <= y_tolerance:
                rows[-1].append(word)
                continue
        rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["bbox"][0])
    return rows


def cluster_cells(row: list[dict[str, Any]], *, x_gap: float = 12.0) -> list[list[dict[str, Any]]]:
    """Groups one row's words into "cells" (multi-word column labels/values)
    by X-gap -- a large horizontal gap between adjacent words marks a column
    boundary in a real table, whereas ordinary word spacing within one label
    does not. Used only to count columns for a fingerprint, never to read a
    cell's own text as a value."""
    ordered = sorted(row, key=lambda w: w["bbox"][0])
    cells: list[list[dict[str, Any]]] = []
    for word in ordered:
        if cells and word["bbox"][0] - cells[-1][-1]["bbox"][2] <= x_gap:
            cells[-1].append(word)
            continue
        cells.append([word])
    return cells


def _row_looks_like_header(row: list[dict[str, Any]]) -> bool:
    tokens = {normalize_row_word(w.get("text")) for w in row}
    if tokens & TEP_HEADER_MARKERS:
        return True
    if len(row) < 2:
        return False
    # No recognized header word matched -- fall back to a strict shape
    # check: EVERY token in the row must be free of digit characters. A
    # typical ТЭП data row ("2 | Площадь участка | га | 1,306") already has
    # two non-numeric cells (label, unit) out of four purely by its own
    # shape, so a majority-based threshold misclassifies ordinary data rows
    # as headers; requiring the whole row to be numeral-free is strict
    # enough to only match a genuine label-only header row.
    return all(token and not any(ch.isdigit() for ch in token) for token in tokens)


class TableFingerprint:
    """A lightweight structural signature for "the table this value/row
    belongs to": the column-header vocabulary plus a column count, compared
    with `fingerprint_similarity` rather than exact equality (a real RD table
    routinely renames/reorders a header slightly relative to its PD
    counterpart)."""

    __slots__ = ("header_tokens", "column_count")

    def __init__(self, header_tokens: frozenset[str], column_count: int) -> None:
        self.header_tokens = header_tokens
        self.column_count = column_count

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return f"TableFingerprint(header_tokens={sorted(self.header_tokens)!r}, column_count={self.column_count})"


def table_fingerprint_from_row(row: list[dict[str, Any]]) -> TableFingerprint | None:
    """Builds a fingerprint directly from an already-identified row (e.g. a
    table-row-count match's own detected header row) -- no anchor/word-index
    lookup involved."""
    if not row:
        return None
    tokens = frozenset(
        token for w in row
        if (token := normalize_row_word(w.get("text"))) and not token.isdigit()
    )
    if not tokens:
        return None
    return TableFingerprint(header_tokens=tokens, column_count=len(cluster_cells(row)))


def extract_table_fingerprint(
    words: list[dict[str, Any]], word_index: int | None, *, max_rows_above: int = 8,
) -> TableFingerprint | None:
    """Locates the row containing `words[word_index]` (typically the value or
    anchor word a mechanism just matched), then searches up to
    `max_rows_above` rows ABOVE it for the nearest header-like row (real ТЭП
    tables repeat their own header only once, above every data row) and
    fingerprints that row; falls back to the anchor's own row when no header
    row is found above it (still better than no structural signal at all).
    Returns None when `word_index` is absent or the page has no recoverable
    row structure."""
    if not words or word_index is None or not (0 <= word_index < len(words)):
        return None
    anchor_word = words[word_index]
    rows = cluster_rows(words)
    anchor_row_idx = next((idx for idx, row in enumerate(rows) if any(w is anchor_word for w in row)), None)
    if anchor_row_idx is None:
        return None
    header_row = None
    for idx in range(anchor_row_idx, max(-1, anchor_row_idx - max_rows_above - 1), -1):
        if _row_looks_like_header(rows[idx]):
            header_row = rows[idx]
            break
    return table_fingerprint_from_row(header_row if header_row is not None else rows[anchor_row_idx])


def fingerprint_similarity(a: TableFingerprint | None, b: TableFingerprint | None) -> float:
    """Jaccard similarity of header vocabularies (dominant term) plus a
    column-count closeness term (secondary) -- 0.0 whenever either side has
    no usable header tokens, so an unfingerprintable candidate never
    outranks one that structurally matches."""
    if a is None or b is None or not a.header_tokens or not b.header_tokens:
        return 0.0
    union = a.header_tokens | b.header_tokens
    intersection = a.header_tokens & b.header_tokens
    jaccard = len(intersection) / len(union) if union else 0.0
    max_columns = max(a.column_count, b.column_count, 1)
    column_score = 1.0 - (abs(a.column_count - b.column_count) / max_columns)
    return 0.7 * jaccard + 0.3 * column_score


def derive_title_candidates(source_hint: str, *, min_words: int = 2, min_chars: int = 8) -> list[str]:
    """A `source_pd`/`source_rd`/`source_id` catalog cell is a short
    human-readable pointer to a document/table, e.g. "Раздел АР: Сводная
    экспликация квартир" or "Ведомость проемов в преградах (ППМ)" -- often
    several alternatives separated by ";". This strips the generic
    "Раздел X:"/"Лист Y:" prefix (not part of any real page's table title)
    and splits on ";" to get independent candidate title phrases, longest
    (most distinctive) first, filtered to ones actually specific enough to
    be searched for as a literal anchor rather than colliding with unrelated
    pages."""
    text = str(source_hint or "").strip()
    if not text:
        return []
    candidates = []
    for part in text.split(";"):
        cleaned = _TITLE_PREFIX_RE.sub("", part).strip(" .")
        # A parenthesized trailing discipline code ("(АР)", "(ППМ)") is a
        # catalog-only annotation, never part of a real page's own title text.
        cleaned = re.sub(r"\s*\([^)]{1,12}\)\s*$", "", cleaned).strip(" .")
        if not cleaned:
            continue
        normalized = normalize_anchor_text(cleaned)
        if len(normalized) < min_chars or len(normalized.split(" ")) < min_words:
            continue
        candidates.append(cleaned)
    return sorted(dict.fromkeys(candidates), key=len, reverse=True)
