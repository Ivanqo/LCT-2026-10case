"""Generic (data-driven, not per-code) enum-class extractor -- the second
new tier added alongside the numeric anchor mechanism (see
`test_case10_generic_extraction.py`) to close part of the 132-parameter
matrix coverage gap: `enum_class_vocabularies` + `generic_enum_extraction` +
the `_upsert_enum_group` wiring in `official_evidence.py`. Every fixture
below is synthetic/anonymized, built from public GOST/SP normative
vocabulary (concrete/steel/rebar grade codes, energy/fire class letters) --
no object_id/file_id/page/GOLD value from the competition dataset appears
here, matching the same convention `test_case10_generic_extraction.py` uses.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain.enum_class_vocabularies import (
    CONCRETE_GRADE,
    ENERGY_EFFICIENCY_LETTER,
    FIRE_HAZARD_BUILDING_CLASS,
    REBAR_CLASS_MODERN,
    STEEL_GRADE,
    find_enum_value,
    resolve_enum_families,
)
from app.domain.anchor_search import GENERIC_SITE_LOCATION
from app.domain.matrix_unit_classifier import is_enum_class_eligible
from app.domain.generic_enum_extraction import (
    GENERIC_ENUM_EXTRACTOR_VERSION,
    EnumObservation,
    collect_enum_observations,
    eligible_enum_params,
    enum_values_equal,
    extract_enum_observation_for_page,
    find_anchor_enum_value,
    new_enum_budget,
)
from app.domain.official_evidence import _upsert_enum_group


def _word_snapshot(text: str, *, width: float = 1191.0, height: float = 842.0, page: int = 1) -> dict:
    words = []
    cursor = 10.0
    for token in text.split():
        token_width = max(8.0, len(token) * 6.0)
        words.append({"text": token, "bbox": [cursor, 500.0, cursor + token_width, 510.0]})
        cursor += token_width + 3.0
    return {"page": page, "width": width, "height": height, "text": " ".join(t["text"] for t in words), "words": words}


class EnumFamilyResolutionTests(unittest.TestCase):
    def test_steel_grade_family_resolves_by_keyword(self):
        families = resolve_enum_families(parameter_name="Марка стали и класс прочности металлопроката")
        self.assertIn(STEEL_GRADE, families)

    def test_rebar_family_resolves_by_keyword(self):
        families = resolve_enum_families(parameter_name="Марка и класс прочности рабочей арматуры")
        self.assertIn(REBAR_CLASS_MODERN, families)

    def test_unrelated_marka_unit_resolves_to_no_family(self):
        # Pipe material grade ("Марка") has no small closed normative
        # vocabulary this module builds -- must resolve to nothing, not a
        # guessed family, so it honestly stays CONFIG_ONLY.
        families = resolve_enum_families(parameter_name="Материал и класс давления напорных труб В1/Т3")
        self.assertEqual(families, [])

    def test_status_unit_resolves_to_no_family(self):
        families = resolve_enum_families(parameter_name='Регистрация лимитов в АИС "ОСИГ"')
        self.assertEqual(families, [])

    def test_energy_letter_family_matches_double_plus_and_plain_letter(self):
        self.assertEqual(find_enum_value("класс A++ присвоен", ENERGY_EFFICIENCY_LETTER), ("A++", 6, 9))
        self.assertEqual(find_enum_value("класс C присвоен", ENERGY_EFFICIENCY_LETTER)[0], "C")

    def test_fire_hazard_building_class_matches(self):
        self.assertEqual(find_enum_value("здание класса С1", FIRE_HAZARD_BUILDING_CLASS)[0], "C1")

    def test_concrete_grade_family_normalizes_comma_decimal(self):
        self.assertEqual(find_enum_value("бетон B7,5 применен", CONCRETE_GRADE)[0], "B7.5")


class EligibilityTests(unittest.TestCase):
    def test_enum_class_unit_with_resolvable_family_is_eligible(self):
        self.assertTrue(is_enum_class_eligible(
            code="KR-056", unit="Марка (С)", parameter_name="Марка стали и класс прочности металлопроката",
            excluded_codes=frozenset(),
        ))

    def test_enum_class_unit_without_a_family_is_not_eligible(self):
        self.assertFalse(is_enum_class_eligible(
            code="IOS2-072", unit="Марка", parameter_name="Материал и класс давления напорных труб В1/Т3",
            excluded_codes=frozenset(),
        ))

    def test_non_enum_unit_is_not_eligible_even_with_a_matching_keyword(self):
        self.assertFalse(is_enum_class_eligible(
            code="X-1", unit="м²", parameter_name="Площадь арматурного цеха", excluded_codes=frozenset(),
        ))

    def test_excluded_rule_pack_code_is_never_eligible(self):
        self.assertFalse(is_enum_class_eligible(
            code="KR-055", unit="Марка (B)", parameter_name="Класс прочности бетона монолитных конструкций",
            excluded_codes=frozenset({"KR-055"}),
        ))


class FindAnchorEnumValueTests(unittest.TestCase):
    def test_finds_steel_grade_after_anchor(self):
        text = "Марка стали и класс прочности металлопроката принята С345 для колонн каркаса"
        match = find_anchor_enum_value(_word_snapshot(text), "Марка стали и класс прочности металлопроката", STEEL_GRADE)
        self.assertIsNotNone(match)
        self.assertEqual(match.canonical_value, "C345")

    def test_no_match_when_anchor_absent(self):
        text = "Прочий текст без нужной метки С345"
        match = find_anchor_enum_value(_word_snapshot(text), "Марка стали и класс прочности металлопроката", STEEL_GRADE)
        self.assertIsNone(match)

    def test_no_match_when_anchor_present_but_no_class_code_follows(self):
        text = "Марка стали и класс прочности металлопроката уточняется дополнительно позже"
        match = find_anchor_enum_value(_word_snapshot(text), "Марка стали и класс прочности металлопроката", STEEL_GRADE)
        self.assertIsNone(match)

    def test_rebar_legacy_roman_numeral_notation(self):
        text = "Марка и класс прочности рабочей арматуры принята А-III по проекту"
        match = find_anchor_enum_value(_word_snapshot(text), "Марка и класс прочности рабочей арматуры", REBAR_CLASS_MODERN)
        # REBAR_CLASS_MODERN alone should not match legacy roman notation.
        self.assertIsNone(match)


class ExtractEnumObservationTests(unittest.TestCase):
    def test_extracts_observation_with_normalized_bbox(self):
        text = "Марка стали и класс прочности металлопроката принята С345 для колонн"
        snapshot = _word_snapshot(text)
        doc = SimpleNamespace(id=5)
        obs = extract_enum_observation_for_page(
            "Марка стали и класс прочности металлопроката", STEEL_GRADE, doc, 4, snapshot, None,
        )
        self.assertIsInstance(obs, EnumObservation)
        self.assertEqual(obs.canonical_value, "C345")
        self.assertEqual(obs.page, 4)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in obs.bbox_normalized))

    def test_returns_none_without_a_match(self):
        snapshot = _word_snapshot("Ничего релевантного здесь нет")
        doc = SimpleNamespace(id=1)
        self.assertIsNone(extract_enum_observation_for_page("Марка стали", STEEL_GRADE, doc, 1, snapshot, None))


class EnumValuesEqualTests(unittest.TestCase):
    def test_exact_match_is_equal(self):
        self.assertTrue(enum_values_equal("C345", "C345"))

    def test_different_grade_is_not_equal(self):
        self.assertFalse(enum_values_equal("C345", "C245"))


def _observation(value: str, *, doc_id: int, page: int, family: str = "STEEL_GRADE", confidence: float = 0.5) -> EnumObservation:
    return EnumObservation(
        value=value,
        canonical_value=value,
        family=family,
        confidence=confidence,
        document=SimpleNamespace(
            id=doc_id, dataset_file_id=f"SYN-{doc_id}", file_hash="hash", content_hash="hash",
            discipline="KR", document_code="DOC", revision="1", approval_status="UNKNOWN",
        ),
        page=page,
        bbox_normalized=[0.1, 0.1, 0.2, 0.12],
        bbox_pdf=[10.0, 500.0, 40.0, 510.0],
        page_width=1000.0,
        page_height=800.0,
        extractor="generic_anchor_enum",
        context="synthetic context",
        source_fragment=None,
    )


class UpsertEnumGroupTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-enum", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(
            id=301, code="KR-056", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True,
        )

    def tearDown(self):
        self.db.close()

    def test_pd_missing_produces_no_group(self):
        key = _upsert_enum_group(self.db, self.process, self.param, {"RD": _observation("C345", doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_rd_missing_produces_no_group(self):
        key = _upsert_enum_group(self.db, self.process, self.param, {"PD": _observation("C345", doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_equal_grades_are_negative_verified(self):
        from app.db.models import EvidenceGroup

        key = _upsert_enum_group(self.db, self.process, self.param, {
            "PD": _observation("C345", doc_id=1, page=1),
            "RD": _observation("C345", doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:enum")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")
        self.assertEqual(group.model_version, GENERIC_ENUM_EXTRACTOR_VERSION)
        self.assertFalse(group.delta["gold_validated"])

    def test_delta_carries_the_site_location_sentinel(self):
        # Regression: `delta` used to omit "location" entirely, so
        # `evidence_group_to_prediction` always exported "" for this
        # mechanism's findings and they could never align with a
        # location-scoped gold check (see test_case10_evaluation.py's
        # GenericTierLocationExportTests for the full exporter/metrics proof).
        from app.db.models import EvidenceGroup

        _upsert_enum_group(self.db, self.process, self.param, {
            "PD": _observation("C345", doc_id=1, page=1),
            "RD": _observation("C345", doc_id=2, page=1),
        })
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.delta["location"], GENERIC_SITE_LOCATION)
        self.assertNotEqual(group.delta["location"], "")

    def test_downgraded_grade_is_candidate_not_auto_confirmed(self):
        from app.db.models import EvidenceGroup

        key = _upsert_enum_group(self.db, self.process, self.param, {
            "PD": _observation("C345", doc_id=1, page=1),
            "RD": _observation("C245", doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:enum")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertNotEqual(group.finding_status, "CONFIRMED_VIOLATION")

    def test_id_present_is_preferred_over_rd_as_the_actual_value(self):
        from app.db.models import EvidenceGroup

        key = _upsert_enum_group(self.db, self.process, self.param, {
            "PD": _observation("C345", doc_id=1, page=1),
            "RD": _observation("C345", doc_id=2, page=1),
            "ID": _observation("C245", doc_id=3, page=1),
        })
        self.assertEqual(key, "generic:enum")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.actual_value, "C245")
        self.assertEqual(group.finding_status, "CANDIDATE")

    def test_never_produces_not_applicable_or_missing_evidence(self):
        from app.db.models import EvidenceGroup

        cases = [
            {"PD": _observation("C345", doc_id=1, page=1), "RD": _observation("C345", doc_id=2, page=1)},
            {"PD": _observation("C345", doc_id=1, page=1), "RD": _observation("C245", doc_id=2, page=1)},
            {"PD": _observation("C345", doc_id=1, page=1)},
            {"RD": _observation("C345", doc_id=1, page=1)},
        ]
        for i, stages in enumerate(cases):
            param = SimpleNamespace(id=400 + i, code=f"KR-{400 + i}", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True)
            _upsert_enum_group(self.db, self.process, param, stages)
        statuses = {g.finding_status for g in self.db.query(EvidenceGroup).all()}
        self.assertTrue(statuses <= {"CANDIDATE", "NEGATIVE_VERIFIED"})
        self.assertNotIn("NOT_APPLICABLE", statuses)
        self.assertNotIn("MISSING_EVIDENCE", statuses)


class CollectEnumObservationsTests(unittest.TestCase):
    """No DB needed: `collect_enum_observations` operates on plain
    fragment/document objects passed in by the caller, same shape as
    `CollectGenericObservationsTests` in the numeric-mechanism test file."""

    def _param(self, param_id: int, code: str, name: str, unit: str = "Марка (С)") -> SimpleNamespace:
        return SimpleNamespace(id=param_id, code=code, parameter_name=name, unit=unit, section=None, source_pd=True, source_rd=True, source_id=False)

    def _fragment(self, doc_id: int, page: int, confidence: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def test_pd_and_rd_stage_hits_from_one_shared_page(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {1: pd_doc, 2: rd_doc}
        params = [self._param(1, "KR-056", "Марка стали и класс прочности металлопроката")]
        by_code = {"KR-056": [self._fragment(1, 1), self._fragment(2, 1)]}
        text = "Марка стали и класс прочности металлопроката принята С345 для колонн"

        def fake_extract(document, pages):
            return {p: _word_snapshot(text, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = new_enum_budget(pages=100)
            result = collect_enum_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        self.assertIn(1, result)
        self.assertEqual(result[1]["PD"].canonical_value, "C345")
        self.assertEqual(result[1]["RD"].canonical_value, "C345")

    def test_exhausted_budget_yields_no_observations_without_erroring(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        by_id = {1: pd_doc}
        params = [self._param(1, "KR-056", "Марка стали и класс прочности металлопроката")]
        by_code = {"KR-056": [self._fragment(1, 1)]}

        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            budget = new_enum_budget(pages=0)
            result = collect_enum_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=budget, excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_ineligible_param_never_enters_the_plan(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        by_id = {1: pd_doc}
        # "Марка" with no resolvable family: must not be scanned at all.
        params = [self._param(1, "IOS2-072", "Материал и класс давления напорных труб В1/Т3", unit="Марка")]
        by_code = {"IOS2-072": [self._fragment(1, 1)]}
        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            result = collect_enum_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"},
                budget=new_enum_budget(pages=100), excluded_codes=frozenset(),
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})


class EligibleEnumParamsTests(unittest.TestCase):
    def test_pairs_each_eligible_param_with_its_resolved_family(self):
        params = [
            SimpleNamespace(id=1, code="KR-056", unit="Марка (С)", parameter_name="Марка стали и класс прочности металлопроката", section=None),
            SimpleNamespace(id=2, code="KR-055", unit="Марка (B)", parameter_name="Класс прочности бетона монолитных конструкций", section=None),
            SimpleNamespace(id=3, code="IOS2-072", unit="Марка", parameter_name="Материал и класс давления напорных труб В1/Т3", section=None),
        ]
        result = eligible_enum_params(params, excluded_codes=frozenset({"KR-055"}))
        self.assertEqual([p.code for p, _family in result], ["KR-056"])
        self.assertEqual(result[0][1], STEEL_GRADE)


if __name__ == "__main__":
    unittest.main()
