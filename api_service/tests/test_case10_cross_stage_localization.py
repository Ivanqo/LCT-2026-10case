"""Cross-stage localization engine (`cross_stage_localization.py`) and its
table-fingerprint primitives (`anchor_search.py`): the new signal added on
top of the four generic (data-driven) extraction mechanisms to prefer a
document-manifest-narrowed, structurally-matching RD/ID page over the
pre-existing "first page the keyword-tagger ranked highest" pick -- see
CASE10_MATRIX_132_COVERAGE.md's cross-stage-localization section for the
forensic background this was built to address. Almost every fixture here is
synthetic (hand-built word/bbox grids); the one exception is
`RealPz002NormativeMismatchEndToEndTests`' two page excerpts, pulled
directly from the real LOS3A PD-stage document via `fitz`'s own
`get_text("words", sort=True)` -- no object_id/file_id/GOLD label from the
competition dataset is attached to them, and no assertion compares against
a known gold value.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.domain.anchor_search import (
    TableFingerprint,
    cluster_cells,
    cluster_rows,
    extract_table_fingerprint,
    fingerprint_similarity,
    table_fingerprint_from_row,
)
from app.domain.cross_stage_localization import (
    StageCandidate,
    narrow_candidate_fragments,
    pick_best_candidate,
    resolve_stage_round,
)
from app.domain.generic_matrix_extraction import _match_numeric
from app.domain.semantic_similarity import SEMANTIC_MIN_SIMILARITY


def _flat_word_snapshot(text: str, *, width: float = 595.2, height: float = 841.92, page: int = 1) -> dict:
    """A single reading-order row of words -- sufficient for anchor/gap
    matching and the word-index-based context window `is_empty_value`/
    `normative_mismatch` are computed from (see `generic_matrix_extraction.
    _match_numeric`), which do not depend on visual row layout."""
    words = []
    cursor = 10.0
    for token in text.split():
        token_width = max(8.0, len(token) * 6.0)
        words.append({"text": token, "bbox": [cursor, 500.0, cursor + token_width, 510.0]})
        cursor += token_width + 3.0
    return {"page": page, "width": width, "height": height, "text": " ".join(t["text"] for t in words), "words": words}


def _tep_snapshot(rows: list[list[str]], *, width: float = 1191.0, height: float = 1684.0, page: int = 1,
                   row_height: float = 14.0, col_width: float = 70.0, start_y: float = 100.0) -> dict:
    """A synthetic multi-row, multi-column page: each entry of `rows` is one
    table row (a list of column texts), banded downward by `row_height` --
    close enough to real fitz word-bbox geometry to exercise `cluster_rows`'
    Y-banding and `cluster_cells`' X-gap column detection."""
    words = []
    y = start_y
    for row in rows:
        x = 40.0
        for cell in row:
            cell_width = max(col_width, len(cell) * 7.0)
            words.append({"text": cell, "bbox": [x, y, x + cell_width, y + 10.0]})
            x += cell_width + 20.0
        y += row_height
    return {"page": page, "width": width, "height": height, "text": " ".join(w["text"] for w in words), "words": words}


_TEP_HEADER = ["№", "Наименование показателей", "Ед.изм.", "Значение"]
_TEP_ROW_BUILDING_AREA = ["1", "Общая площадь здания", "м²", "11618,27"]
_TEP_ROW_SITE_AREA = ["2", "Площадь участка", "га", "1,306"]

# A DIFFERENT table shape (different header vocabulary/column count) that
# happens to also contain a page mentioning "Общая площадь здания" -- used to
# prove structural fingerprinting prefers the REAL ТЭП table over a page that
# merely repeats the anchor phrase.
_UNRELATED_TABLE_HEADER = ["Раздел", "Примечание"]
_UNRELATED_ROW_MENTIONING_PHRASE = ["Общая площадь здания", "см. приложение"]


class ClusterRowsAndCellsTests(unittest.TestCase):
    def test_rows_banded_by_y_proximity(self):
        snapshot = _tep_snapshot([_TEP_HEADER, _TEP_ROW_BUILDING_AREA])
        rows = cluster_rows(snapshot["words"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([w["text"] for w in rows[0]], _TEP_HEADER)
        self.assertEqual([w["text"] for w in rows[1]], _TEP_ROW_BUILDING_AREA)

    def test_cells_split_on_x_gap(self):
        snapshot = _tep_snapshot([_TEP_HEADER])
        cells = cluster_cells(snapshot["words"])
        self.assertEqual(len(cells), len(_TEP_HEADER))


class TableFingerprintFromRowTests(unittest.TestCase):
    def test_builds_header_tokens_and_column_count(self):
        row = _tep_snapshot([_TEP_HEADER])["words"]
        fingerprint = table_fingerprint_from_row(row)
        self.assertIsInstance(fingerprint, TableFingerprint)
        self.assertIn("наименование показателей", fingerprint.header_tokens)
        self.assertEqual(fingerprint.column_count, 4)

    def test_empty_row_yields_none(self):
        self.assertIsNone(table_fingerprint_from_row([]))

    def test_all_numeric_row_yields_none(self):
        row = _tep_snapshot([["1", "2", "3"]])["words"]
        self.assertIsNone(table_fingerprint_from_row(row))


class ExtractTableFingerprintTests(unittest.TestCase):
    def test_finds_header_row_above_the_anchor_word(self):
        snapshot = _tep_snapshot([_TEP_HEADER, _TEP_ROW_SITE_AREA, _TEP_ROW_BUILDING_AREA])
        # word_index of "11618,27" (last word of the building-area row).
        value_index = len(_TEP_HEADER) + len(_TEP_ROW_SITE_AREA) + len(_TEP_ROW_BUILDING_AREA) - 1
        fingerprint = extract_table_fingerprint(snapshot["words"], value_index)
        self.assertIsNotNone(fingerprint)
        self.assertIn("наименование показателей", fingerprint.header_tokens)

    def test_falls_back_to_anchor_row_when_no_header_row_found_above(self):
        snapshot = _tep_snapshot([_UNRELATED_ROW_MENTIONING_PHRASE])
        fingerprint = extract_table_fingerprint(snapshot["words"], 0)
        self.assertIsNotNone(fingerprint)
        self.assertNotIn("наименование показателей", fingerprint.header_tokens)

    def test_none_word_index_yields_none(self):
        snapshot = _tep_snapshot([_TEP_HEADER])
        self.assertIsNone(extract_table_fingerprint(snapshot["words"], None))

    def test_out_of_range_word_index_yields_none(self):
        snapshot = _tep_snapshot([_TEP_HEADER])
        self.assertIsNone(extract_table_fingerprint(snapshot["words"], 999))

    def test_empty_words_yields_none(self):
        self.assertIsNone(extract_table_fingerprint([], 0))


class FingerprintSimilarityTests(unittest.TestCase):
    def test_identical_headers_score_high(self):
        a = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        b = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        self.assertGreater(fingerprint_similarity(a, b), 0.9)

    def test_unrelated_headers_score_low(self):
        a = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        b = table_fingerprint_from_row(_tep_snapshot([_UNRELATED_TABLE_HEADER])["words"])
        self.assertLess(fingerprint_similarity(a, b), 0.3)

    def test_either_side_none_scores_zero(self):
        a = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        self.assertEqual(fingerprint_similarity(a, None), 0.0)
        self.assertEqual(fingerprint_similarity(None, a), 0.0)
        self.assertEqual(fingerprint_similarity(None, None), 0.0)


class NarrowCandidateFragmentsTests(unittest.TestCase):
    def _fragment(self, doc_id: int, page: int, confidence: float) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def test_no_reference_document_is_plain_confidence_sort_and_cap(self):
        by_id = {1: SimpleNamespace(discipline=None), 2: SimpleNamespace(discipline=None)}
        fragments = [self._fragment(1, 1, 0.3), self._fragment(2, 1, 0.9)]
        result = narrow_candidate_fragments(fragments, by_id, reference_document=None, limit=10)
        self.assertEqual([f.document_version_id for f in result], [2, 1])

    def test_reference_without_discipline_is_unchanged(self):
        by_id = {1: SimpleNamespace(discipline="AR"), 2: SimpleNamespace(discipline="KR")}
        fragments = [self._fragment(1, 1, 0.3), self._fragment(2, 1, 0.9)]
        reference = SimpleNamespace(discipline=None)
        result = narrow_candidate_fragments(fragments, by_id, reference_document=reference, limit=10)
        self.assertEqual([f.document_version_id for f in result], [2, 1])

    def test_same_discipline_candidates_are_ranked_ahead_despite_lower_confidence(self):
        by_id = {1: SimpleNamespace(discipline="AR"), 2: SimpleNamespace(discipline="KR")}
        low_confidence_same_discipline = self._fragment(1, 1, 0.2)
        high_confidence_other_discipline = self._fragment(2, 1, 0.9)
        reference = SimpleNamespace(discipline="AR")
        result = narrow_candidate_fragments(
            [high_confidence_other_discipline, low_confidence_same_discipline], by_id, reference_document=reference, limit=10,
        )
        self.assertEqual([f.document_version_id for f in result], [1, 2])

    def test_empty_narrow_pool_falls_back_to_the_wide_pool(self):
        by_id = {2: SimpleNamespace(discipline="KR")}
        fragments = [self._fragment(2, 1, 0.9)]
        reference = SimpleNamespace(discipline="AR")  # no candidate shares this discipline
        result = narrow_candidate_fragments(fragments, by_id, reference_document=reference, limit=10)
        self.assertEqual([f.document_version_id for f in result], [2])

    def test_respects_limit_after_narrowing(self):
        by_id = {1: SimpleNamespace(discipline="AR")}
        fragments = [self._fragment(1, page, 0.5) for page in range(5)]
        reference = SimpleNamespace(discipline="AR")
        result = narrow_candidate_fragments(fragments, by_id, reference_document=reference, limit=2)
        self.assertEqual(len(result), 2)


class PickBestCandidateTests(unittest.TestCase):
    def _candidate(
        self, page: int, fingerprint, *, semantic_score=None, is_empty_value=False, normative_mismatch=False,
    ) -> StageCandidate:
        return StageCandidate(
            fragment=SimpleNamespace(page=page), document=SimpleNamespace(id=1), page=page,
            payload=f"payload-{page}", fingerprint=fingerprint, semantic_score=semantic_score,
            is_empty_value=is_empty_value, normative_mismatch=normative_mismatch,
        )

    def test_no_candidates_is_none(self):
        self.assertIsNone(pick_best_candidate([], None))

    def test_no_reference_fingerprint_returns_first_in_order(self):
        candidates = [self._candidate(1, None), self._candidate(2, None)]
        self.assertIs(pick_best_candidate(candidates, None), candidates[0])

    def test_reference_prefers_the_best_structural_match_even_if_later_in_order(self):
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        unrelated_fp = table_fingerprint_from_row(_tep_snapshot([_UNRELATED_TABLE_HEADER])["words"])
        first_but_wrong = self._candidate(1, unrelated_fp)
        second_but_right = self._candidate(2, header_fp)
        best = pick_best_candidate([first_but_wrong, second_but_right], header_fp)
        self.assertIs(best, second_but_right)

    def test_fingerprint_less_candidate_never_beats_a_matching_one(self):
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        no_fingerprint = self._candidate(1, None)
        matching = self._candidate(2, header_fp)
        self.assertIs(pick_best_candidate([no_fingerprint, matching], header_fp), matching)

    def test_ties_prefer_earlier_original_order(self):
        candidates = [self._candidate(1, None), self._candidate(2, None)]
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        # Neither candidate has a fingerprint -- both score (-1.0, ...), tie broken by index.
        self.assertIs(pick_best_candidate(candidates, header_fp), candidates[0])

    def test_semantic_filter_drops_the_lone_candidate_below_threshold(self):
        # The whole point of the checkpoint-40 "Стена в грунте" finding: a
        # single tagged candidate whose own row reads nothing like the
        # parameter being searched for must not win by default -- an honest
        # "nothing found" beats a wrong value, even with no alternative.
        below_floor = self._candidate(1, None, semantic_score=SEMANTIC_MIN_SIMILARITY - 0.05)
        self.assertIsNone(pick_best_candidate([below_floor], None))

    def test_semantic_filter_keeps_a_candidate_at_or_above_threshold(self):
        at_floor = self._candidate(1, None, semantic_score=SEMANTIC_MIN_SIMILARITY)
        self.assertIs(pick_best_candidate([at_floor], None), at_floor)

    def test_candidates_without_a_semantic_score_are_never_filtered(self):
        # None means "no signal" (feature disabled, or this entry supplied
        # no query_text) -- never treated as "irrelevant".
        no_signal = self._candidate(1, None, semantic_score=None)
        self.assertIs(pick_best_candidate([no_signal], None), no_signal)

    def test_semantic_filter_removes_the_irrelevant_one_and_keeps_the_relevant_one(self):
        relevant = self._candidate(1, None, semantic_score=0.7)
        irrelevant = self._candidate(2, None, semantic_score=0.1)
        self.assertIs(pick_best_candidate([irrelevant, relevant], None), relevant)

    def test_semantic_score_breaks_a_fingerprint_tie(self):
        # Both candidates equally fingerprint-less (score -1.0 each) -- the
        # one whose row text is semantically closer to the parameter wins,
        # instead of falling through to plain original-order tie-breaking.
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        earlier_but_less_relevant = self._candidate(1, None, semantic_score=0.5)
        later_but_more_relevant = self._candidate(2, None, semantic_score=0.9)
        self.assertIs(
            pick_best_candidate([earlier_but_less_relevant, later_but_more_relevant], header_fp),
            later_but_more_relevant,
        )


class ExistenceSignalRankingTests(unittest.TestCase):
    """`StageCandidate.is_empty_value`/`normative_mismatch` (module
    docstring, signal 4) -- generalized (synthetic, no LOS3A-specific data)
    proof that a flagged candidate is demoted below an unflagged one
    regardless of confidence order, fingerprint, or semantic score, but
    never dropped outright when it is the only candidate available. Real
    forensic-data end-to-end reproductions (PZ-001-style empty-value
    collision, PZ-002's real normative-mismatch collision) live in
    `ResolveStageRoundTests` below."""

    def _candidate(
        self, page: int, fingerprint=None, *, semantic_score=None, is_empty_value=False, normative_mismatch=False,
    ) -> StageCandidate:
        return StageCandidate(
            fragment=SimpleNamespace(page=page), document=SimpleNamespace(id=1), page=page,
            payload=f"payload-{page}", fingerprint=fingerprint, semantic_score=semantic_score,
            is_empty_value=is_empty_value, normative_mismatch=normative_mismatch,
        )

    def test_empty_candidate_never_beats_a_nonempty_one_with_no_reference(self):
        # Real forensic shape, PZ-001: the empty candidate ranks FIRST by
        # confidence/original order (no PD reference exists yet in round 1),
        # but the non-empty one must still win.
        empty_but_first = self._candidate(1, is_empty_value=True)
        nonempty_but_second = self._candidate(2, is_empty_value=False)
        best = pick_best_candidate([empty_but_first, nonempty_but_second], None)
        self.assertIs(best, nonempty_but_second)

    def test_normative_mismatch_never_beats_a_matching_one_with_no_reference(self):
        mismatched_but_first = self._candidate(1, normative_mismatch=True)
        matching_but_second = self._candidate(2, normative_mismatch=False)
        best = pick_best_candidate([mismatched_but_first, matching_but_second], None)
        self.assertIs(best, matching_but_second)

    def test_empty_candidate_still_wins_when_it_is_the_only_one(self):
        # A demotion, never a hard filter -- an honest (if empty) result
        # beats no result at all when nothing else was found.
        only = self._candidate(1, is_empty_value=True)
        self.assertIs(pick_best_candidate([only], None), only)

    def test_nonempty_beats_empty_even_with_a_worse_fingerprint(self):
        # Real forensic shape, PZ-001: existence trumps structural
        # resemblance -- an empty candidate whose table structure happens to
        # match PD better must still lose to a genuine value with a worse
        # (or absent) fingerprint match.
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        unrelated_fp = table_fingerprint_from_row(_tep_snapshot([_UNRELATED_TABLE_HEADER])["words"])
        empty_with_matching_fingerprint = self._candidate(1, header_fp, is_empty_value=True)
        nonempty_with_unrelated_fingerprint = self._candidate(2, unrelated_fp, is_empty_value=False)
        best = pick_best_candidate([empty_with_matching_fingerprint, nonempty_with_unrelated_fingerprint], header_fp)
        self.assertIs(best, nonempty_with_unrelated_fingerprint)

    def test_when_all_survivors_are_flagged_the_old_fingerprint_ranking_still_applies(self):
        header_fp = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])
        unrelated_fp = table_fingerprint_from_row(_tep_snapshot([_UNRELATED_TABLE_HEADER])["words"])
        first_but_wrong = self._candidate(1, unrelated_fp, is_empty_value=True)
        second_but_right = self._candidate(2, header_fp, is_empty_value=True)
        best = pick_best_candidate([first_but_wrong, second_but_right], header_fp)
        self.assertIs(best, second_but_right)

    def test_unflagged_candidates_are_completely_unaffected(self):
        # Both signals default False -- when neither is ever set, behaviour
        # is byte-identical to the pre-signal-4 code path.
        candidates = [self._candidate(1), self._candidate(2)]
        self.assertIs(pick_best_candidate(candidates, None), candidates[0])


class ResolveStageRoundTests(unittest.TestCase):
    """End-to-end proof that the shared engine (a) reduces to the
    pre-existing "first match wins" behaviour with no reference, and (b)
    actually prefers a same-discipline, structurally-matching page over a
    higher-confidence but wrong page once a reference is supplied -- the two
    additive signals this whole module exists to add."""

    def _fragment(self, doc_id: int, page: int, confidence: float) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def _match_numeric_like(self, snapshot, fragment, doc, page, extra):
        # Minimal stand-in for a mechanism's own match_fn: succeeds whenever
        # the snapshot contains the marker text `extra`, fingerprinting off
        # the last word's index (mirrors how the real mechanisms anchor off
        # a matched value's own row).
        words = snapshot.get("words") or []
        texts = [w["text"] for w in words]
        if extra not in texts:
            return None
        word_index = len(words) - 1
        fingerprint = extract_table_fingerprint(words, word_index)
        return (f"value-from-doc-{doc.id}-page-{page}", fingerprint, None)

    def test_with_no_reference_first_successful_candidate_in_confidence_order_wins(self):
        by_id = {1: SimpleNamespace(id=1, discipline=None), 2: SimpleNamespace(id=2, discipline=None)}
        low = self._fragment(1, 1, 0.2)
        high = self._fragment(2, 1, 0.9)
        entries = [("param-1", [low, high], None, None, "MARK", None)]

        def fake_extract(document, pages):
            return {p: _tep_snapshot([["MARK"]], page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=self._match_numeric_like,
            )
        self.assertIn("param-1", result)
        # Higher-confidence fragment (doc 2) is tried first -- unchanged
        # pre-existing behaviour when there is no PD reference.
        self.assertEqual(result["param-1"].document.id, 2)

    def test_reference_document_and_fingerprint_pick_the_structurally_correct_page_over_a_higher_confidence_wrong_one(self):
        reference_doc = SimpleNamespace(id=99, discipline="AR")
        # A high-confidence page from an unrelated-discipline document whose
        # table looks nothing like PD's, and a lower-confidence page from a
        # same-discipline document whose table structure matches PD's real
        # ТЭП header -- the narrowing + fingerprint signal together must
        # prefer the second one.
        wrong_doc = SimpleNamespace(id=1, discipline="KR")
        right_doc = SimpleNamespace(id=2, discipline="AR")
        by_id = {1: wrong_doc, 2: right_doc}
        wrong_fragment = self._fragment(1, 1, 0.9)
        right_fragment = self._fragment(2, 1, 0.1)

        reference_fingerprint = table_fingerprint_from_row(_tep_snapshot([_TEP_HEADER])["words"])

        def fake_extract(document, pages):
            if document.id == 1:
                return {p: _tep_snapshot([_UNRELATED_TABLE_HEADER, _UNRELATED_ROW_MENTIONING_PHRASE], page=p) for p in pages}
            return {p: _tep_snapshot([_TEP_HEADER, _TEP_ROW_BUILDING_AREA], page=p) for p in pages}

        def match_fn(snapshot, fragment, doc, page, extra):
            words = snapshot.get("words") or []
            texts = [w["text"] for w in words]
            if "Общая площадь здания" not in texts:
                return None
            word_index = texts.index("Общая площадь здания")
            fingerprint = extract_table_fingerprint(words, word_index)
            return (f"doc-{doc.id}", fingerprint, None)

        entries = [("PZ-002", [wrong_fragment, right_fragment], reference_doc, reference_fingerprint, None, None)]

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=match_fn,
            )
        self.assertIn("PZ-002", result)
        self.assertEqual(result["PZ-002"].payload, "doc-2")

    def test_exhausted_budget_yields_no_results_without_erroring(self):
        by_id = {1: SimpleNamespace(id=1, discipline=None)}
        entries = [("param-1", [self._fragment(1, 1, 0.5)], None, None, "MARK", None)]
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 0}, match_fn=self._match_numeric_like,
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_shared_document_page_is_rendered_once_across_entries(self):
        by_id = {1: SimpleNamespace(id=1, discipline=None)}
        fragment_a = self._fragment(1, 1, 0.5)
        fragment_b = self._fragment(1, 1, 0.5)
        entries = [
            ("param-a", [fragment_a], None, None, "MARK", None),
            ("param-b", [fragment_b], None, None, "MARK", None),
        ]
        call_count = {"n": 0}

        def fake_extract(document, pages):
            call_count["n"] += 1
            return {p: _tep_snapshot([["MARK"]], page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = {"pages": 100}
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget=budget, match_fn=self._match_numeric_like,
            )
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(budget["pages"], 99)
        self.assertIn("param-a", result)
        self.assertIn("param-b", result)

    def test_semantic_filter_end_to_end_reproduces_the_stena_v_grunte_finding(self):
        """Synthetic regression case for checkpoint 40's real finding
        (CASE10_MATRIX_132_COVERAGE.md): the only RD-stage candidate tagged
        for PZ-002 ("Общая площадь здания") on OBJ-NOVOSLOBODSKAYA was a
        keyword-tagger false positive -- document F0137, a "Стена в грунте"
        (wall-in-ground) construction-note sheet that merely shares the word
        "площади" (there meaning cross-sectional pile area, not building
        area). The real page text excerpt and the real genuine-match row
        text below are exactly what was pulled from the two real documents
        forensically before writing this module (see
        CASE10_MATRIX_132_COVERAGE.md) -- only `semantic_text_similarity` is
        mocked, with the real cosine scores this session measured for this
        exact pair via `sentence-transformers/paraphrase-multilingual-
        MiniLM-L12-v2` (0.62 vs 0.7286 for genuine positives measured
        elsewhere; the stena-v-grunte excerpt itself scored 0.3951 against
        this same PZ-002/RD query) -- so this proves the PLUMBING acts
        correctly on a realistic score, independent of whether the real
        model is installed in the test environment; `test_case10_semantic_
        similarity.py` separately proves the model itself produces scores
        like these."""
        query_text = "Общая площадь здания. Раздел АР: Лист \"Общие данные\", Сводная экспликация"
        genuine_row_text = "3 Площадь жилого здания, в т. ч.: кв. м - 17 140,2"
        stena_v_grunte_excerpt = (
            "Для армированных свай и конструкций «стены в грунте» во избежание "
            "возникновения дефектов сплошности ствола сваи в процессе бетонирования"
        )
        measured_scores = {genuine_row_text: 0.62, stena_v_grunte_excerpt: 0.3951}

        wrong_doc = SimpleNamespace(id=137, discipline="OTHER")  # real F0137's own discipline
        by_id = {137: wrong_doc}
        # Only one tagged candidate exists -- exactly checkpoint 40's finding
        # (0 other RD-stage candidates for this code/object).
        lone_fragment = self._fragment(137, 1, 0.62)
        entries = [("PZ-002", [lone_fragment], None, None, "MARK", query_text)]

        def fake_extract(document, pages):
            return {p: _tep_snapshot([[stena_v_grunte_excerpt]], page=p) for p in pages}

        def match_fn(snapshot, fragment, doc, page, extra):
            # The keyword tagger matched this page; the row text handed back
            # for semantic scoring is the real irrelevant excerpt.
            return ("wrong-value-from-stena-v-grunte-page", None, stena_v_grunte_excerpt)

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract), \
             patch("app.domain.cross_stage_localization.semantic_text_similarity", side_effect=lambda q, t: measured_scores.get(t)):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=match_fn,
            )
        # The spurious candidate is filtered out entirely -- an honest
        # "nothing found" for RD, not a wrong value, even though it was the
        # only tagged candidate.
        self.assertEqual(result, {})

    def test_semantic_filter_end_to_end_keeps_a_genuine_match(self):
        """Same shape as the stena-v-grunte case above, but the tagged
        candidate's row text is the real genuine PZ-002 match -- proves the
        filter does not also suppress legitimate results."""
        query_text = "Общая площадь здания. Раздел ПЗ: Таблица ТЭП (текстовая часть)"
        genuine_row_text = "3 Площадь жилого здания, в т. ч.: кв. м - 17 140,2"
        doc = SimpleNamespace(id=101, discipline="PZ")
        by_id = {101: doc}
        fragment = self._fragment(101, 9, 0.55)
        entries = [("PZ-002", [fragment], None, None, "MARK", query_text)]

        def fake_extract(document, pages):
            return {p: _tep_snapshot([[genuine_row_text]], page=p) for p in pages}

        def match_fn(snapshot, fragment, doc, page, extra):
            return ("17140.2", None, genuine_row_text)

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract), \
             patch("app.domain.cross_stage_localization.semantic_text_similarity", return_value=0.7286):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=match_fn,
            )
        self.assertIn("PZ-002", result)
        self.assertEqual(result["PZ-002"].payload, "17140.2")


class RealPz002NormativeMismatchEndToEndTests(unittest.TestCase):
    """Real forensic finding (LOS3A PD-stage document "9. 01-09-00-01-15-
    ПБ.1.pdf", the fire-safety explanatory note, see CASE10_MATRIX_132_
    COVERAGE.md's `los3a_forensic_deep_dive_pz001_pz002_pz004`, PZ-002):
    this single real document independently anchor-matches "Общая площадь
    здания" on TWO different pages -- page 9's own ТЭП table row ("Площадь
    здания (по СП 54.13330.2016, прил. А.1.2)" = 25036.27, the catalog's
    own intended definition, matching NEW_OBJECTS_ANALYSE.md section 6's
    manual reading exactly) and page 10's row for a related-but-distinct
    quantity ("Площадь помещений здания (по СП 118.13330.2012, прил. Г.5)"
    = 16867.90). Both fixtures below are the real, trimmed (still
    contiguous, unedited) word-sorted text of those two real pages --
    verified to reproduce `find_anchor_numeric_value("Общая площадь
    здания", ...)` returning the exact same values as the full,
    untrimmed real page text before writing this test.

    Round 1 (PD) resolves with NO reference fingerprint yet (see
    `generic_matrix_extraction.collect_generic_observations`) -- confidence
    order alone decided the outcome pre-fix. This test deliberately tags
    the WRONG page (10) with the HIGHER tagger confidence -- the worst case
    for the pre-existing "first in confidence order wins" behaviour -- to
    prove the new normative-mismatch signal, not a lucky confidence
    ordering, is what makes the correct page win."""

    _PAGE9_EXCERPT = (
        "показатели Технико-экономические № Наименование Ед. Показатель "
        "Примечание П/П Изм. Площадь застройки м² 1 1076,49 (по СП "
        "54.13330.2022 прил. А.1.1. с учетом выступающих частей балконов) "
        "Площадь застройки м² 1061,49 (без учета выступающих частей "
        "балконов) Площадь здания м² 2 25036,27 (по СП 54.13330.2016, "
        "прил. А.1.2) В т.ч. подземная часть м² 3196,44 Строительный "
        "объем м3 3 88264,00"
    )
    _PAGE10_EXCERPT = (
        "Коэффициент (Отношение жилой площади к 10 0,77 общей площади "
        "надземной части) Площадь помещений здания м² 11 16 867,90 (по "
        "СП 118.13330.2012, прил. Г.5) Общественная часть"
    )

    def test_normative_mismatch_signal_picks_the_correct_page_despite_worse_confidence(self):
        param = SimpleNamespace(parameter_name="Общая площадь здания", sp_reference="СП 54.13330.2016", gost_reference=None, fz_reference=None)
        correct_doc = SimpleNamespace(id=9, discipline="PB")
        wrong_doc = SimpleNamespace(id=10, discipline="PB")
        by_id = {9: correct_doc, 10: wrong_doc}
        correct_fragment = SimpleNamespace(document_version_id=9, page=9, confidence=0.3)
        wrong_fragment = SimpleNamespace(document_version_id=10, page=10, confidence=0.9)
        # No query_text -- isolates the existence-signal fix from signal 3's
        # semantic filter, which is a separate, already-tested mechanism.
        entries = [("PZ-002", [wrong_fragment, correct_fragment], None, None, param, None)]

        def fake_extract(document, pages):
            excerpt = self._PAGE9_EXCERPT if document.id == 9 else self._PAGE10_EXCERPT
            return {p: _flat_word_snapshot(excerpt, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=_match_numeric,
            )
        self.assertIn("PZ-002", result)
        self.assertEqual(result["PZ-002"].payload.normalized_value, "25036.27")
        self.assertEqual(result["PZ-002"].document.id, 9)

    def test_without_the_expected_reference_populated_the_old_confidence_ordering_still_applies(self):
        # Honest boundary: with no catalog-declared sp_reference (today's
        # real state for every one of the 132 official parameters, see
        # `anchor_search.expected_normative_references`), the mismatch
        # signal is a no-op and the wrong, higher-confidence page still
        # wins -- this signal is real, tested infrastructure, not a silent
        # fix for PZ-002 as currently cataloged.
        param = SimpleNamespace(parameter_name="Общая площадь здания", sp_reference=None, gost_reference=None, fz_reference=None)
        correct_doc = SimpleNamespace(id=9, discipline="PB")
        wrong_doc = SimpleNamespace(id=10, discipline="PB")
        by_id = {9: correct_doc, 10: wrong_doc}
        correct_fragment = SimpleNamespace(document_version_id=9, page=9, confidence=0.3)
        wrong_fragment = SimpleNamespace(document_version_id=10, page=10, confidence=0.9)
        entries = [("PZ-002", [wrong_fragment, correct_fragment], None, None, param, None)]

        def fake_extract(document, pages):
            excerpt = self._PAGE9_EXCERPT if document.id == 9 else self._PAGE10_EXCERPT
            return {p: _flat_word_snapshot(excerpt, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            result = resolve_stage_round(
                entries=entries, by_id=by_id, pages_per_stage=15, budget={"pages": 100}, match_fn=_match_numeric,
            )
        self.assertIn("PZ-002", result)
        self.assertEqual(result["PZ-002"].payload.normalized_value, "16867.90")


if __name__ == "__main__":
    unittest.main()
