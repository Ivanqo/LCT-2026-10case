"""Generic (data-driven, not per-code) table-row-count extractor -- the
fourth new tier added alongside the numeric, enum-class and compound
mechanisms (see the sibling `test_case10_*_extraction.py` files):
`matrix_unit_classifier.TABLE_ROW_COUNT_SEMANTIC_CODES`/
`is_table_count_eligible` + `generic_table_row_count` + the
`_upsert_table_count_group` wiring in `official_evidence.py`. All fixtures
are synthetic 2D word grids built to resemble a real specification/
exposition table's row/column layout -- no real document excerpt or gold
value from the competition dataset appears here.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain.anchor_search import GENERIC_SITE_LOCATION, derive_title_candidates
from app.domain.matrix_unit_classifier import TABLE_ROW_COUNT_SEMANTIC_CODES, is_table_count_eligible
from app.domain.generic_table_row_count import (
    GENERIC_TABLE_COUNT_EXTRACTOR_VERSION,
    TableCountObservation,
    collect_table_count_observations,
    counts_equal,
    extract_table_count_observation_for_page,
    find_table_row_count,
    new_table_count_budget,
    source_hints,
    title_candidates_for_stage,
)
from app.domain.official_evidence import _upsert_table_count_group


def _grid_snapshot(
    title: str,
    rows: list[list[str]],
    *,
    width: float = 1191.0,
    height: float = 1684.0,
    page: int = 1,
    row_height: float = 14.0,
    col_width: float = 60.0,
    title_y: float = 100.0,
) -> dict:
    """Builds a synthetic page: `title` as one text line, then each entry of
    `rows` as its own table row (a list of column texts), banded downward by
    `row_height` -- close enough to real fitz word-bbox geometry for the
    Y/X-clustering fallback to exercise its row-banding logic."""
    words = []
    cursor_x = 40.0
    for token in title.split():
        token_width = max(8.0, len(token) * 6.0)
        words.append({"text": token, "bbox": [cursor_x, title_y, cursor_x + token_width, title_y + 10.0]})
        cursor_x += token_width + 3.0
    y = title_y + row_height * 2
    for row in rows:
        x = 40.0
        for cell in row:
            cell_width = max(col_width, len(cell) * 7.0)
            words.append({"text": cell, "bbox": [x, y, x + cell_width, y + 10.0]})
            x += cell_width + 10.0
        y += row_height
    return {"page": page, "width": width, "height": height, "text": " ".join(w["text"] for w in words), "words": words}


class DeriveTitleCandidatesTests(unittest.TestCase):
    def test_strips_section_prefix(self):
        self.assertEqual(derive_title_candidates("Раздел АР: Сводная экспликация квартир"), ["Сводная экспликация квартир"])

    def test_splits_on_semicolon_and_strips_trailing_discipline_code(self):
        candidates = derive_title_candidates("Детальные чертежи санузлов; Спецификация (АР)")
        self.assertIn("Детальные чертежи санузлов", candidates)
        # "Спецификация" alone is too short/generic (1 word) to be kept.
        self.assertNotIn("Спецификация", candidates)

    def test_empty_hint_yields_no_candidates(self):
        self.assertEqual(derive_title_candidates(""), [])
        self.assertEqual(derive_title_candidates(None), [])

    def test_short_generic_phrase_is_filtered(self):
        self.assertEqual(derive_title_candidates("Раздел ПЗ: Таблица"), [])


class EligibilityTests(unittest.TestCase):
    def test_allowlisted_count_code_is_eligible(self):
        self.assertTrue(is_table_count_eligible(code="PZ-010", unit="шт.", excluded_codes=frozenset()))

    def test_non_allowlisted_count_code_is_not_eligible(self):
        # PZ-007 (Этажность) is NUMERIC_COUNT by unit but a single ТЭП
        # scalar, not an enumerable table -- deliberately excluded.
        self.assertFalse(is_table_count_eligible(code="PZ-007", unit="ед.", excluded_codes=frozenset()))
        self.assertFalse(is_table_count_eligible(code="POS-086", unit="чел.", excluded_codes=frozenset()))

    def test_non_count_unit_is_not_eligible_even_if_code_is_on_the_list(self):
        code = next(iter(TABLE_ROW_COUNT_SEMANTIC_CODES))
        self.assertFalse(is_table_count_eligible(code=code, unit="м²", excluded_codes=frozenset()))

    def test_excluded_rule_pack_code_is_never_eligible(self):
        code = next(iter(TABLE_ROW_COUNT_SEMANTIC_CODES))
        self.assertFalse(is_table_count_eligible(code=code, unit="шт.", excluded_codes=frozenset({code})))


class SourceHintsTests(unittest.TestCase):
    def _param_with_other_normative(self, **sources) -> SimpleNamespace:
        payload = {"source_pd": sources.get("pd", ""), "source_rd": sources.get("rd", ""), "source_id": sources.get("id", "")}
        return SimpleNamespace(other_normative=json.dumps(payload, ensure_ascii=False))

    def test_recovers_original_source_text_from_other_normative_json(self):
        param = self._param_with_other_normative(rd="Раздел АР: Сводная экспликация квартир")
        hints = source_hints(param)
        self.assertEqual(hints["RD"], "Раздел АР: Сводная экспликация квартир")

    def test_missing_or_invalid_other_normative_yields_empty_hints(self):
        self.assertEqual(source_hints(SimpleNamespace(other_normative=None)), {})
        self.assertEqual(source_hints(SimpleNamespace(other_normative="not json")), {})

    def test_title_candidates_for_stage_combines_both_helpers(self):
        param = self._param_with_other_normative(rd="Раздел АР: Сводная экспликация квартир")
        self.assertEqual(title_candidates_for_stage(param, "RD"), ["Сводная экспликация квартир"])
        self.assertEqual(title_candidates_for_stage(param, "PD"), [])


class FindTableRowCountTests(unittest.TestCase):
    def test_counts_content_rows_below_matched_title_skipping_header(self):
        snapshot = _grid_snapshot(
            "Сводная экспликация квартир",
            rows=[
                ["№", "Тип", "Площадь"],
                ["1", "1-комн", "34.5"],
                ["2", "2-комн", "52.1"],
                ["3", "студия", "24.0"],
            ],
        )
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNotNone(match)
        self.assertTrue(match.table_found)
        self.assertEqual(match.header_row_count, 1)
        self.assertEqual(match.row_count, 3)

    def test_real_zero_rows_is_distinguished_from_table_not_found(self):
        # Header row present (table structure confirmed) but genuinely no
        # content rows below it -- a real value of 0, not an abstention.
        snapshot = _grid_snapshot("Сводная экспликация квартир", rows=[["№", "Тип", "Площадь"]])
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNotNone(match)
        self.assertTrue(match.table_found)
        self.assertEqual(match.row_count, 0)

    def test_no_title_match_returns_none(self):
        snapshot = _grid_snapshot("Совершенно другой заголовок листа", rows=[["1", "2-комн", "52.1"]])
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNone(match)

    def test_title_matched_but_nothing_follows_is_not_a_confident_table(self):
        snapshot = _grid_snapshot("Сводная экспликация квартир", rows=[])
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNotNone(match)
        self.assertFalse(match.table_found)

    def test_table_end_marker_stops_counting(self):
        snapshot = _grid_snapshot(
            "Сводная экспликация квартир",
            rows=[
                ["№", "Тип", "Площадь"],
                ["1", "1-комн", "34.5"],
                ["2", "2-комн", "52.1"],
                ["Лист", "1", "Формат", "А1"],
                ["стрей", "не", "относящийся", "текст"],
            ],
        )
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNotNone(match)
        self.assertEqual(match.row_count, 2)

    def test_single_word_rows_below_the_table_are_not_counted_as_content(self):
        snapshot = _grid_snapshot(
            "Сводная экспликация квартир",
            rows=[["№", "Тип", "Площадь"], ["1", "1-комн", "34.5"], ["стр.5"]],
        )
        match = find_table_row_count(snapshot, ["Сводная экспликация квартир"])
        self.assertIsNotNone(match)
        self.assertEqual(match.row_count, 1)


class ExtractTableCountObservationTests(unittest.TestCase):
    def test_extracts_observation_with_row_count_and_bbox(self):
        snapshot = _grid_snapshot(
            "Сводная экспликация квартир",
            rows=[["№", "Тип", "Площадь"], ["1", "1-комн", "34.5"], ["2", "студия", "24.0"]],
        )
        doc = SimpleNamespace(id=3)
        obs = extract_table_count_observation_for_page(["Сводная экспликация квартир"], doc, 5, snapshot, None)
        self.assertIsInstance(obs, TableCountObservation)
        self.assertEqual(obs.row_count, 2)
        self.assertEqual(obs.display_value, "2")
        self.assertEqual(obs.page, 5)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in obs.bbox_normalized))

    def test_no_titles_yields_none(self):
        snapshot = _grid_snapshot("Что угодно", rows=[["1", "2"]])
        self.assertIsNone(extract_table_count_observation_for_page([], SimpleNamespace(id=1), 1, snapshot, None))

    def test_no_match_yields_none(self):
        snapshot = _grid_snapshot("Другой заголовок", rows=[["1", "2"]])
        self.assertIsNone(extract_table_count_observation_for_page(["Сводная экспликация квартир"], SimpleNamespace(id=1), 1, snapshot, None))


class CountsEqualTests(unittest.TestCase):
    def test_equal_counts(self):
        self.assertTrue(counts_equal(3, 3))

    def test_different_counts(self):
        self.assertFalse(counts_equal(3, 2))


def _observation(row_count: int, *, doc_id: int, page: int, confidence: float = 0.5) -> TableCountObservation:
    return TableCountObservation(
        row_count=row_count,
        confidence=confidence,
        document=SimpleNamespace(
            id=doc_id, dataset_file_id=f"SYN-{doc_id}", file_hash="hash", content_hash="hash",
            discipline="AR", document_code="DOC", revision="1", approval_status="UNKNOWN",
        ),
        page=page,
        bbox_normalized=[0.1, 0.1, 0.4, 0.3],
        bbox_pdf=[10.0, 500.0, 200.0, 600.0],
        page_width=1000.0,
        page_height=1400.0,
        extractor="generic_table_row_count",
        context="synthetic context",
        source_fragment=None,
    )


class UpsertTableCountGroupTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-count", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(
            id=701, code="PZ-010", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True,
        )

    def tearDown(self):
        self.db.close()

    def test_pd_missing_produces_no_group(self):
        key = _upsert_table_count_group(self.db, self.process, self.param, {"RD": _observation(3, doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_rd_missing_produces_no_group(self):
        key = _upsert_table_count_group(self.db, self.process, self.param, {"PD": _observation(3, doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_equal_counts_are_negative_verified(self):
        from app.db.models import EvidenceGroup

        key = _upsert_table_count_group(self.db, self.process, self.param, {
            "PD": _observation(3, doc_id=1, page=1),
            "RD": _observation(3, doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:table_count")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")
        self.assertEqual(group.model_version, GENERIC_TABLE_COUNT_EXTRACTOR_VERSION)
        self.assertFalse(group.delta["gold_validated"])

    def test_different_counts_are_candidate_not_auto_confirmed(self):
        from app.db.models import EvidenceGroup

        key = _upsert_table_count_group(self.db, self.process, self.param, {
            "PD": _observation(3, doc_id=1, page=1),
            "RD": _observation(2, doc_id=2, page=1),
        })
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertNotEqual(group.finding_status, "CONFIRMED_VIOLATION")

    def test_delta_carries_the_site_location_sentinel(self):
        # Regression: `delta` used to omit "location" entirely, so
        # `evidence_group_to_prediction` always exported "" for this
        # mechanism's findings and they could never align with a
        # location-scoped gold check (see test_case10_evaluation.py's
        # GenericTierLocationExportTests for the full exporter/metrics proof).
        from app.db.models import EvidenceGroup

        _upsert_table_count_group(self.db, self.process, self.param, {
            "PD": _observation(3, doc_id=1, page=1),
            "RD": _observation(3, doc_id=2, page=1),
        })
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.delta["location"], GENERIC_SITE_LOCATION)
        self.assertNotEqual(group.delta["location"], "")

    def test_real_zero_actual_count_is_a_genuine_comparable_finding(self):
        # A real "table found, 0 rows" observation on the RD side must still
        # be usable for comparison, distinct from "no observation at all".
        from app.db.models import EvidenceGroup

        key = _upsert_table_count_group(self.db, self.process, self.param, {
            "PD": _observation(3, doc_id=1, page=1),
            "RD": _observation(0, doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:table_count")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.actual_value, "0")
        self.assertEqual(group.finding_status, "CANDIDATE")

    def test_never_produces_not_applicable_or_missing_evidence(self):
        from app.db.models import EvidenceGroup

        cases = [
            {"PD": _observation(3, doc_id=1, page=1), "RD": _observation(3, doc_id=2, page=1)},
            {"PD": _observation(3, doc_id=1, page=1), "RD": _observation(2, doc_id=2, page=1)},
            {"PD": _observation(3, doc_id=1, page=1)},
            {"RD": _observation(3, doc_id=1, page=1)},
        ]
        for i, stages in enumerate(cases):
            param = SimpleNamespace(id=800 + i, code=f"PZ-{800 + i}", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True)
            _upsert_table_count_group(self.db, self.process, param, stages)
        statuses = {g.finding_status for g in self.db.query(EvidenceGroup).all()}
        self.assertTrue(statuses <= {"CANDIDATE", "NEGATIVE_VERIFIED"})
        self.assertNotIn("NOT_APPLICABLE", statuses)
        self.assertNotIn("MISSING_EVIDENCE", statuses)


class CollectTableCountObservationsTests(unittest.TestCase):
    def _param(self, param_id: int, code: str, *, rd_hint: str, pd_hint: str = "", unit: str = "шт.") -> SimpleNamespace:
        other_normative = json.dumps({"source_pd": pd_hint, "source_rd": rd_hint, "source_id": ""}, ensure_ascii=False)
        return SimpleNamespace(
            id=param_id, code=code, unit=unit, other_normative=other_normative,
            source_pd=bool(pd_hint), source_rd=True, source_id=False,
        )

    def _fragment(self, doc_id: int, page: int, confidence: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def test_rd_stage_hit_from_title_derived_anchor(self):
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {2: rd_doc}
        params = [self._param(1, "PZ-010", rd_hint="Раздел АР: Сводная экспликация квартир")]
        by_code = {"PZ-010": [self._fragment(2, 1)]}
        snapshot = _grid_snapshot(
            "Сводная экспликация квартир",
            rows=[["№", "Тип", "Площадь"], ["1", "1-комн", "34.5"], ["2", "студия", "24.0"]],
        )

        def fake_extract(document, pages):
            return {p: snapshot for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = new_table_count_budget(pages=100)
            result = collect_table_count_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        self.assertIn(1, result)
        self.assertEqual(result[1]["RD"].row_count, 2)

    def test_exhausted_budget_yields_no_observations_without_erroring(self):
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {2: rd_doc}
        params = [self._param(1, "PZ-010", rd_hint="Раздел АР: Сводная экспликация квартир")]
        by_code = {"PZ-010": [self._fragment(2, 1)]}
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            budget = new_table_count_budget(pages=0)
            result = collect_table_count_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_ineligible_code_never_enters_the_plan(self):
        # PZ-007 (Этажность) is NUMERIC_COUNT but not on the semantic
        # allowlist -- must never be scanned.
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {2: rd_doc}
        params = [self._param(1, "PZ-007", rd_hint="Раздел АР: Заглавные листы", unit="ед.")]
        by_code = {"PZ-007": [self._fragment(2, 1)]}
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            result = collect_table_count_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=new_table_count_budget(pages=100), excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_param_with_no_derivable_title_is_skipped_without_scanning(self):
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {2: rd_doc}
        params = [self._param(1, "PZ-010", rd_hint="")]
        by_code = {"PZ-010": [self._fragment(2, 1)]}
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            result = collect_table_count_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=new_table_count_budget(pages=100), excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
