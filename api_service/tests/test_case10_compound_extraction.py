"""Generic (data-driven, not per-code) compound-unit extractor -- the third
new tier added alongside the numeric anchor and enum-class mechanisms (see
`test_case10_generic_extraction.py`, `test_case10_enum_class_extraction.py`):
`matrix_unit_classifier.compound_unit_components`/`is_compound_eligible` +
`generic_compound_extraction` + the `_upsert_compound_group` wiring in
`official_evidence.py`. Every fixture below is synthetic (no real document
excerpt was available for a compound-unit table this session -- the one
code with real organizer gold, SPZU-029, is on the hidden test object and is
deliberately never read/opened for tuning; see
CASE10_MATRIX_132_COVERAGE.md and the Phase D validation notes).
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain.anchor_search import GENERIC_SITE_LOCATION
from app.domain.matrix_unit_classifier import compound_unit_components, is_compound_eligible
from app.domain.generic_compound_extraction import (
    GENERIC_COMPOUND_EXTRACTOR_VERSION,
    CompoundObservation,
    collect_compound_observations,
    compare_components,
    eligible_compound_params,
    extract_compound_observation_for_page,
    find_anchor_compound_values,
    new_compound_budget,
    values_close,
)
from app.domain.official_evidence import _upsert_compound_group


def _word_snapshot(text: str, *, width: float = 1191.0, height: float = 842.0, page: int = 1) -> dict:
    words = []
    cursor = 10.0
    for token in text.split():
        token_width = max(8.0, len(token) * 6.0)
        words.append({"text": token, "bbox": [cursor, 500.0, cursor + token_width, 510.0]})
        cursor += token_width + 3.0
    return {"page": page, "width": width, "height": height, "text": " ".join(t["text"] for t in words), "words": words}


class CompoundUnitComponentsTests(unittest.TestCase):
    def test_splits_two_component_unit(self):
        self.assertEqual(compound_unit_components("шт. / компл."), ["шт.", "компл."])

    def test_splits_three_component_unit_with_an_internal_bare_slash(self):
        # The first component ("м3/ч") has its OWN bare "/" that must not be
        # split further -- only " / " (space-slash-space) separates components.
        self.assertEqual(compound_unit_components("м3/ч / м / кВт"), ["м3/ч", "м", "кВт"])

    def test_bare_slash_rate_unit_is_not_a_compound(self):
        self.assertIsNone(compound_unit_components("м³/ч"))
        self.assertIsNone(compound_unit_components("л/с"))

    def test_empty_unit_is_none(self):
        self.assertIsNone(compound_unit_components(""))
        self.assertIsNone(compound_unit_components(None))


class CompoundEligibilityTests(unittest.TestCase):
    def test_all_numeric_components_are_eligible(self):
        self.assertTrue(is_compound_eligible(
            code="SPZU-029", unit="шт. / компл.", parameter_name="Спецификация МАФ", excluded_codes=frozenset(),
        ))

    def test_text_component_marks_it_ineligible(self):
        self.assertFalse(is_compound_eligible(
            code="AR-052", unit="RAL / Артикул", parameter_name="Цветовое решение и материалы облицовки фасадов",
            excluded_codes=frozenset(),
        ))
        self.assertFalse(is_compound_eligible(
            code="KR-066", unit="Марка / Толщина", parameter_name="Антикоррозионная и огнезащитная обработка конструкций",
            excluded_codes=frozenset(),
        ))

    def test_bare_slash_rate_unit_is_ineligible(self):
        self.assertFalse(is_compound_eligible(
            code="PZ-016", unit="м³/сут", parameter_name="Суточный расход водопотребления", excluded_codes=frozenset(),
        ))

    def test_excluded_rule_pack_code_is_never_eligible(self):
        self.assertFalse(is_compound_eligible(
            code="X-1", unit="шт. / компл.", parameter_name="Спецификация чего-либо", excluded_codes=frozenset({"X-1"}),
        ))


class FindAnchorCompoundValuesTests(unittest.TestCase):
    def test_finds_two_sequential_numbers_after_anchor(self):
        text = "Спецификация МАФ по проекту 48 12 позиций установлено"
        match = find_anchor_compound_values(_word_snapshot(text), "Спецификация МАФ", 2)
        self.assertIsNotNone(match)
        self.assertEqual(match.raw_values, ["48", "12"])
        self.assertEqual(match.normalized_values, [Decimal("48"), Decimal("12")])

    def test_slash_separator_repeated_from_the_source_text_is_skipped(self):
        text = "Спецификация МАФ 48 / 12 по ведомости"
        match = find_anchor_compound_values(_word_snapshot(text), "Спецификация МАФ", 2)
        self.assertIsNotNone(match)
        self.assertEqual(match.raw_values, ["48", "12"])

    def test_returns_none_when_fewer_numbers_than_components_are_found(self):
        text = "Спецификация МАФ 48 позиций установлено без второго числа"
        match = find_anchor_compound_values(_word_snapshot(text), "Спецификация МАФ", 2)
        self.assertIsNone(match)

    def test_returns_none_without_anchor(self):
        text = "Не относящийся текст 48 12"
        match = find_anchor_compound_values(_word_snapshot(text), "Спецификация МАФ", 2)
        self.assertIsNone(match)

    def test_three_component_unit(self):
        text = "Характеристики насосных станций хоз-питьевого водоснабжения 120 4,5 15"
        match = find_anchor_compound_values(
            _word_snapshot(text), "Характеристики насосных станций хоз-питьевого водоснабжения", 3,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_values, [Decimal("120"), Decimal("4.5"), Decimal("15")])


class ExtractCompoundObservationTests(unittest.TestCase):
    def test_extracts_observation_with_display_value_and_bbox(self):
        text = "Спецификация МАФ 48 12 позиций установлено"
        snapshot = _word_snapshot(text)
        doc = SimpleNamespace(id=9)
        obs = extract_compound_observation_for_page("Спецификация МАФ", ["шт.", "компл."], doc, 2, snapshot, None)
        self.assertIsInstance(obs, CompoundObservation)
        self.assertEqual(obs.display_value, "48 / 12")
        self.assertEqual(obs.page, 2)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in obs.bbox_normalized))

    def test_returns_none_without_full_match(self):
        snapshot = _word_snapshot("Спецификация МАФ 48 без второго числа")
        doc = SimpleNamespace(id=1)
        self.assertIsNone(extract_compound_observation_for_page("Спецификация МАФ", ["шт.", "компл."], doc, 1, snapshot, None))


class CompareComponentsTests(unittest.TestCase):
    def _obs(self, values: list[str], labels: list[str]) -> CompoundObservation:
        return CompoundObservation(
            raw_values=values,
            normalized_values=[Decimal(v) for v in values],
            component_labels=labels,
            confidence=0.5,
            document=SimpleNamespace(id=1, dataset_file_id="SYN-1", file_hash="h", content_hash="h", discipline="PP", document_code="D", revision="1", approval_status="U"),
            page=1,
            bbox_normalized=[0.1, 0.1, 0.2, 0.12],
            bbox_pdf=[10.0, 500.0, 40.0, 510.0],
            page_width=1000.0,
            page_height=800.0,
            extractor="generic_anchor_compound",
            context="synthetic",
            source_fragment=None,
        )

    def test_all_components_equal(self):
        left = self._obs(["48", "12"], ["шт.", "компл."])
        right = self._obs(["48", "12"], ["шт.", "компл."])
        comparisons = compare_components(left, right)
        self.assertTrue(all(c.equal for c in comparisons))

    def test_single_component_mismatch_is_detected_even_when_the_other_matches(self):
        # This is the core reason component-wise comparison exists: a
        # whole-string compare of "48 / 12" vs "30 / 12" would still show a
        # difference, but only component-wise output correctly attributes it
        # to the FIRST field (шт.), not the second (компл.).
        left = self._obs(["48", "12"], ["шт.", "компл."])
        right = self._obs(["30", "12"], ["шт.", "компл."])
        comparisons = compare_components(left, right)
        self.assertFalse(comparisons[0].equal)
        self.assertTrue(comparisons[1].equal)

    def test_values_close_tolerates_small_rounding_jitter(self):
        self.assertTrue(values_close(Decimal("48"), Decimal("48.001")))
        self.assertFalse(values_close(Decimal("48"), Decimal("30")))


def _observation(values: list[str], *, doc_id: int, page: int, labels: list[str] = None, confidence: float = 0.5) -> CompoundObservation:
    labels = labels or ["шт.", "компл."]
    return CompoundObservation(
        raw_values=values,
        normalized_values=[Decimal(v) for v in values],
        component_labels=labels,
        confidence=confidence,
        document=SimpleNamespace(
            id=doc_id, dataset_file_id=f"SYN-{doc_id}", file_hash="hash", content_hash="hash",
            discipline="PP", document_code="DOC", revision="1", approval_status="UNKNOWN",
        ),
        page=page,
        bbox_normalized=[0.1, 0.1, 0.2, 0.12],
        bbox_pdf=[10.0, 500.0, 40.0, 510.0],
        page_width=1000.0,
        page_height=800.0,
        extractor="generic_anchor_compound",
        context="synthetic context",
        source_fragment=None,
    )


class UpsertCompoundGroupTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-compound", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(
            id=501, code="SPZU-029", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True,
        )

    def tearDown(self):
        self.db.close()

    def test_pd_missing_produces_no_group(self):
        key = _upsert_compound_group(self.db, self.process, self.param, {"RD": _observation(["48", "12"], doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_rd_missing_produces_no_group(self):
        key = _upsert_compound_group(self.db, self.process, self.param, {"PD": _observation(["48", "12"], doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_all_components_equal_is_negative_verified(self):
        from app.db.models import EvidenceGroup

        key = _upsert_compound_group(self.db, self.process, self.param, {
            "PD": _observation(["48", "12"], doc_id=1, page=1),
            "RD": _observation(["48", "12"], doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:compound")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")
        self.assertEqual(group.model_version, GENERIC_COMPOUND_EXTRACTOR_VERSION)
        self.assertFalse(group.delta["gold_validated"])
        self.assertEqual(len(group.delta["components"]), 2)

    def test_delta_carries_the_site_location_sentinel(self):
        # Regression: `delta` used to omit "location" entirely, so
        # `evidence_group_to_prediction` always exported "" for this
        # mechanism's findings and they could never align with a
        # location-scoped gold check (see test_case10_evaluation.py's
        # GenericTierLocationExportTests for the full exporter/metrics proof).
        from app.db.models import EvidenceGroup

        _upsert_compound_group(self.db, self.process, self.param, {
            "PD": _observation(["48", "12"], doc_id=1, page=1),
            "RD": _observation(["48", "12"], doc_id=2, page=1),
        })
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.delta["location"], GENERIC_SITE_LOCATION)
        self.assertNotEqual(group.delta["location"], "")

    def test_one_component_mismatch_is_candidate_not_auto_confirmed(self):
        from app.db.models import EvidenceGroup

        key = _upsert_compound_group(self.db, self.process, self.param, {
            "PD": _observation(["48", "12"], doc_id=1, page=1),
            "RD": _observation(["30", "12"], doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:compound")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertNotEqual(group.finding_status, "CONFIRMED_VIOLATION")
        components = group.delta["components"]
        self.assertFalse(components[0]["equal"])
        self.assertTrue(components[1]["equal"])

    def test_id_present_is_preferred_over_rd_as_the_actual_value(self):
        from app.db.models import EvidenceGroup

        key = _upsert_compound_group(self.db, self.process, self.param, {
            "PD": _observation(["48", "12"], doc_id=1, page=1),
            "RD": _observation(["48", "12"], doc_id=2, page=1),
            "ID": _observation(["48", "9"], doc_id=3, page=1),
        })
        self.assertEqual(key, "generic:compound")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.actual_value, "48 / 9")
        self.assertEqual(group.finding_status, "CANDIDATE")

    def test_never_produces_not_applicable_or_missing_evidence(self):
        from app.db.models import EvidenceGroup

        cases = [
            {"PD": _observation(["48", "12"], doc_id=1, page=1), "RD": _observation(["48", "12"], doc_id=2, page=1)},
            {"PD": _observation(["48", "12"], doc_id=1, page=1), "RD": _observation(["30", "12"], doc_id=2, page=1)},
            {"PD": _observation(["48", "12"], doc_id=1, page=1)},
            {"RD": _observation(["48", "12"], doc_id=1, page=1)},
        ]
        for i, stages in enumerate(cases):
            param = SimpleNamespace(id=600 + i, code=f"SPZU-{600 + i}", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True)
            _upsert_compound_group(self.db, self.process, param, stages)
        statuses = {g.finding_status for g in self.db.query(EvidenceGroup).all()}
        self.assertTrue(statuses <= {"CANDIDATE", "NEGATIVE_VERIFIED"})
        self.assertNotIn("NOT_APPLICABLE", statuses)
        self.assertNotIn("MISSING_EVIDENCE", statuses)


class CollectCompoundObservationsTests(unittest.TestCase):
    def _param(self, param_id: int, code: str, name: str, unit: str = "шт. / компл.") -> SimpleNamespace:
        return SimpleNamespace(id=param_id, code=code, parameter_name=name, unit=unit, source_pd=True, source_rd=True, source_id=False)

    def _fragment(self, doc_id: int, page: int, confidence: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def test_pd_and_rd_stage_hits_from_one_shared_page(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {1: pd_doc, 2: rd_doc}
        params = [self._param(1, "SPZU-029", "Спецификация МАФ")]
        by_code = {"SPZU-029": [self._fragment(1, 1), self._fragment(2, 1)]}
        text = "Спецификация МАФ по проекту 48 12 позиций"

        def fake_extract(document, pages):
            return {p: _word_snapshot(text, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = new_compound_budget(pages=100)
            result = collect_compound_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        self.assertIn(1, result)
        self.assertEqual(result[1]["PD"].raw_values, ["48", "12"])
        self.assertEqual(result[1]["RD"].raw_values, ["48", "12"])

    def test_exhausted_budget_yields_no_observations_without_erroring(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        by_id = {1: pd_doc}
        params = [self._param(1, "SPZU-029", "Спецификация МАФ")]
        by_code = {"SPZU-029": [self._fragment(1, 1)]}

        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            budget = new_compound_budget(pages=0)
            result = collect_compound_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_ineligible_text_component_param_never_enters_the_plan(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        by_id = {1: pd_doc}
        params = [self._param(1, "KR-066", "Антикоррозионная и огнезащитная обработка конструкций", unit="Марка / Толщина")]
        by_code = {"KR-066": [self._fragment(1, 1)]}
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            result = collect_compound_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=new_compound_budget(pages=100), excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})


class EligibleCompoundParamsTests(unittest.TestCase):
    def test_pairs_each_eligible_param_with_its_component_labels(self):
        params = [
            SimpleNamespace(id=1, code="SPZU-029", unit="шт. / компл.", parameter_name="Спецификация МАФ"),
            SimpleNamespace(id=2, code="KR-066", unit="Марка / Толщина", parameter_name="Антикоррозионная и огнезащитная обработка конструкций"),
            SimpleNamespace(id=3, code="PZ-016", unit="м³/сут", parameter_name="Суточный расход водопотребления"),
        ]
        result = eligible_compound_params(params, excluded_codes=frozenset())
        self.assertEqual([p.code for p, _labels in result], ["SPZU-029"])
        self.assertEqual(result[0][1], ["шт.", "компл."])


if __name__ == "__main__":
    unittest.main()
