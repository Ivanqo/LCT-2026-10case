"""Regression tests for checkpoint 29's evidence-discovery mechanisms:
multi-fragment evidence, bounded confirm-only document fallback, the
generalized KR-058 anchor, the PZ-009 bbox trailing-punctuation fix, and the
adaptive OCR page zoom. Fixtures are synthetic; no real object_id, file_id,
page number or GOLD value from the competition dataset is used anywhere here.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain.dataset_sources import ocr_zoom_for_page_size
from app.domain.official_evidence import _upsert_rule_groups
from app.domain.official_rule_packs import (
    RuleObservation,
    _extract_concrete_classes,
    find_confirming_fallback_observation,
    new_fallback_budget,
    parse_rule_page,
)


def _snapshot(text: str, page: int = 1) -> dict:
    words = []
    cursor = 10.0
    for token in text.split():
        width = max(8.0, len(token) * 4.0)
        words.append({"text": token, "bbox": [cursor, 10.0, cursor + width, 20.0]})
        cursor += width + 3.0
    return {"page": page, "width": 1000.0, "height": 500.0, "text": text, "words": words}


def _document(doc_id: int = 1, stage: str = "PD", pages: int | None = None):
    return SimpleNamespace(
        id=doc_id, dataset_file_id=f"SYN-{doc_id}", filename="synthetic.pdf", file_hash="hash",
        content_hash="hash", discipline="KR", document_code="DOC", revision="1",
        approval_status="UNKNOWN", dataset_stage=stage,
        doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage],
        dataset_metadata={"document_manifest": {"pdf_pages": pages}} if pages else {},
    )


def _observation(code, location, stage, value, *, doc_id, page, confidence=0.9) -> RuleObservation:
    return RuleObservation(
        parameter_code=code, location=location, stage=stage, value=value, normalized_value=value,
        confidence=confidence, document=_document(doc_id, stage), page=page,
        bbox_normalized=[0.1, 0.1, 0.2, 0.2], bbox_pdf=[10.0, 10.0, 20.0, 20.0],
        page_width=1000.0, page_height=500.0, extractor="synthetic", context="",
    )


class KR058GeneralizationTests(unittest.TestCase):
    def test_reordered_and_word_gapped_phrasing_is_extracted(self):
        rows = parse_rule_page(
            "KR-058",
            _snapshot("Плита фундаментная выполнена монолитная толщина принята 1000 мм и 1200 мм по расчету"),
            document=_document(), stage="PD",
        )
        self.assertEqual(rows[0].normalized_value, "1000/1200")

    def test_h_equals_shorthand_is_a_recognized_thickness_anchor(self):
        rows = parse_rule_page(
            "KR-058",
            _snapshot("Железобетонная фундаментная плита h=1200 мм по оси А-Б"),
            document=_document(), stage="RD",
        )
        self.assertEqual(rows[0].normalized_value, "1200")

    def test_unrelated_thickness_far_from_slab_mention_is_ignored(self):
        # "толщина" appears, but for an unrelated element far from any
        # "фундамент"/"плита" mention; must not be picked up as slab thickness.
        long_gap = " ".join(["текст"] * 80)
        rows = parse_rule_page(
            "KR-058",
            _snapshot(f"Фундаментная плита {long_gap} стены толщиной 300 мм"),
            document=_document(), stage="PD",
        )
        self.assertEqual(rows, [])


class PZ009BboxTokenTests(unittest.TestCase):
    def test_value_at_end_of_sentence_still_resolves_a_bbox(self):
        # PDF text extraction attaches the sentence-final period to the last
        # token ("159.95."); the regex already matched "159.95" here before the
        # fix, but the bbox lookup silently failed on the exact-token compare.
        rows = parse_rule_page(
            "PZ-009",
            _snapshot("соответствующая абсолютной отметке 159.95."),
            document=_document(), stage="RD",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].normalized_value, "159.95")
        self.assertTrue(all(0 <= v <= 1 for v in rows[0].bbox_normalized))


class ActSubjectLocationFallbackTests(unittest.TestCase):
    """Checkpoint 30 root cause: a materials/protocol registry attached to a
    hidden-works inspection act (АОСР) lists concrete class codes (e.g. "БСТ
    В25") without repeating the covered element's name anywhere nearby, so
    the per-page nearby-word window in `_extract_concrete_classes` never
    resolves a location there. The act's own standardized field 7
    ("Разрешается производство последующих работ"), read once from the same
    document, supplies it -- generic to the AOSR form, not to any one file."""

    @staticmethod
    def _act_pages(next_work_phrase: str) -> dict[int, dict]:
        return {
            1: _snapshot("Акт освидетельствования скрытых работ № 1 от 1 января 2026г."),
            2: _snapshot(f"7. Разрешается производство последующих работ {next_work_phrase} наименования работ"),
        }

    def test_registry_page_without_local_context_uses_act_declared_location(self):
        registry_page = _snapshot("Документ о качестве бетонной смеси партии БСТ В25 П4F(I)200W8", page=5)
        act_pages = self._act_pages("Устройство стены в грунте")

        def fake_extract(document, pages):
            return {page: act_pages[page] for page in pages if page in act_pages}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            observations = _extract_concrete_classes(registry_page, [], document=_document(1, stage="ID"))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["location"], "Стена в грунте")
        self.assertEqual(observations[0]["normalized_value"], "B25")

    def test_without_the_act_marker_no_location_is_invented(self):
        registry_page = _snapshot("Документ о качестве бетонной смеси партии БСТ В25 П4F(I)200W8", page=5)
        plain_pages = {1: _snapshot("Общий журнал работ, том 1"), 2: _snapshot("страница пуста")}

        def fake_extract(document, pages):
            return {page: plain_pages[page] for page in pages if page in plain_pages}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            observations = _extract_concrete_classes(registry_page, [], document=_document(1, stage="ID"))
        self.assertEqual(observations, [])

    def test_direct_local_context_takes_priority_over_document_level_fallback(self):
        page_with_local_context = _snapshot("Фундаментная плита выполнена из бетона класса B40", page=3)
        act_pages = self._act_pages("Устройство стены в грунте")

        def fake_extract(document, pages):
            return {page: act_pages[page] for page in pages if page in act_pages}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract) as mocked:
            observations = _extract_concrete_classes(page_with_local_context, [], document=_document(1, stage="ID"))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["location"], "Фундаментная плита")
        mocked.assert_not_called()


class OcrZoomTests(unittest.TestCase):
    def test_a4_page_gets_the_maximum_zoom(self):
        self.assertEqual(ocr_zoom_for_page_size(595.0, 842.0), 3.0)

    def test_a0_sheet_is_capped_well_below_the_maximum(self):
        zoom = ocr_zoom_for_page_size(2384.0, 3370.0)
        self.assertLess(zoom, 1.5)
        self.assertGreaterEqual(zoom * 3370.0, 3370.0 * ocr_zoom_for_page_size(2384.0, 3370.0))
        self.assertLessEqual(zoom * 3370.0, 3500.0 + 1e-6)

    def test_degenerate_page_size_does_not_crash_or_invert(self):
        zoom = ocr_zoom_for_page_size(0.0, 0.0)
        self.assertGreaterEqual(zoom, 1.0)

    def test_extreme_page_is_capped_by_deterministic_raster_area(self):
        width, height = 20000.0, 12000.0
        zoom = ocr_zoom_for_page_size(width, height)
        self.assertLessEqual(width * height * zoom * zoom, 3500.0 * 3500.0 + 1e-6)
        self.assertLessEqual(max(width, height) * zoom, 3500.0 + 1e-6)


class MultiFragmentEvidenceTests(unittest.TestCase):
    """Multiple pages independently confirming the same value must all survive
    as evidence fragments, not collapse to a single arbitrary pick."""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-1", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(id=1, code="KR-055", review_priority="MEDIUM", source_pd=True, source_rd=True, source_id=True)

    def tearDown(self):
        self.db.close()

    def test_pd_pages_with_a_different_value_are_not_pulled_in_as_support(self):
        observations = [
            _observation("KR-055", "Стена Б", "PD", "B40", doc_id=201, page=1),
            _observation("KR-055", "Стена Б", "PD", "B25", doc_id=201, page=2),  # unrelated value, same location
            _observation("KR-055", "Стена Б", "RD", "B40", doc_id=202, page=1),
        ]
        from app.db.models import EvidenceGroup

        created = _upsert_rule_groups(self.db, self.process, self.param, observations)
        self.assertTrue(created)
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        pd_fragments = [f for f in group.fragments if f.stage == "project"]
        self.assertEqual(len(pd_fragments), 1)
        self.assertEqual(pd_fragments[0].page, 1)


class MultiFragmentEvidenceCountTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-2", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC-2",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(id=2, code="KR-055", review_priority="MEDIUM", source_pd=True, source_rd=True, source_id=True)

    def tearDown(self):
        self.db.close()

    def test_two_pd_pages_confirming_the_same_value_both_become_fragments(self):
        from app.db.models import EvidenceGroup

        observations = [
            _observation("KR-055", "Стена А", "PD", "B25", doc_id=101, page=5),
            _observation("KR-055", "Стена А", "PD", "B25", doc_id=101, page=9),
            _observation("KR-055", "Стена А", "RD", "B25", doc_id=102, page=3),
        ]
        created = _upsert_rule_groups(self.db, self.process, self.param, observations)
        self.assertTrue(created)
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC-2").one()
        pd_pages = sorted(f.page for f in group.fragments if f.stage == "project")
        self.assertEqual(pd_pages, [5, 9])
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")


class ConfirmOnlyFallbackTests(unittest.TestCase):
    """`find_confirming_fallback_observation` must (a) find a document the
    upstream MATRIX_FIELD tagging never flagged for this code, purely by
    matching stage + the extractor's own parametric anchors, but (b) only ever
    accept a value that already matches what other stages established, and
    (c) never exceed its budget."""

    def _documents(self, count: int, stage: str = "ID", pages: int | None = None) -> dict:
        return {i: _document(i, stage=stage, pages=pages) for i in range(1, count + 1)}

    def test_finds_confirming_value_on_an_untagged_document(self):
        # Realistic per-document page count (real documents always carry their
        # own `pdf_pages` from the manifest) -- large enough that the match on
        # document 2 still needs document 1 to be skipped past, small enough
        # that it does not alone exhaust a single attempt's page share.
        documents = self._documents(3, pages=5)

        def fake_extract(document, pages):
            if document.id == 2:
                return {pages[0]: _snapshot("Фундаментная плита выполнена из бетона класса B25", page=pages[0])}
            return {}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            budget = new_fallback_budget()
            observation, diagnostics = find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=documents, exclude_pages=set(), budget=budget,
            )
        self.assertIsNotNone(observation)
        self.assertEqual(observation.normalized_value, "B25")
        self.assertTrue(diagnostics["found"])

    def test_a_mismatching_value_on_an_untagged_document_is_rejected(self):
        documents = self._documents(2)

        def fake_extract(document, pages):
            # Same location, but a DIFFERENT value than what PD/RD established —
            # must never be surfaced, since that could manufacture a violation.
            return {pages[0]: _snapshot("Фундаментная плита выполнена из бетона класса B15", page=pages[0])}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            budget = new_fallback_budget()
            observation, diagnostics = find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=documents, exclude_pages=set(), budget=budget,
            )
        self.assertIsNone(observation)
        self.assertFalse(diagnostics["found"])

    def test_document_cap_is_enforced(self):
        # One page per document, so the document cap -- not the per-attempt
        # page cap -- is what's actually being exercised here.
        documents = self._documents(50, pages=1)
        call_count = {"n": 0}

        def fake_extract(document, pages):
            call_count["n"] += 1
            return {pages[0]: _snapshot("ничего релевантного здесь нет", page=pages[0])}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            budget = {"documents": 4, "pages": 1000, "ocr_pages": 0}
            observation, diagnostics = find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=documents, exclude_pages=set(), budget=budget,
            )
        self.assertIsNone(observation)
        self.assertLessEqual(call_count["n"], 4)
        self.assertLessEqual(diagnostics["documents_scanned"], 4)
        self.assertEqual(budget["documents"], 0)

    def test_page_budget_is_shared_and_exhausted_across_documents(self):
        documents = self._documents(20)

        def fake_extract(document, pages):
            return {page: _snapshot("ничего релевантного здесь нет", page=page) for page in pages}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            budget = {"documents": 20, "pages": 10, "ocr_pages": 0}
            find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=documents, exclude_pages=set(), budget=budget,
            )
        self.assertEqual(budget["pages"], 0)

    def test_per_attempt_cap_leaves_budget_for_a_later_gap(self):
        # A first, unrelated gap scans many large, irrelevant documents. Without
        # a per-attempt cap this alone could exhaust the whole shared pool
        # (this is exactly what happened in production before this fix: one
        # gap's fallback attempt consumed the entire page budget and a later,
        # ultimately-findable gap never got to run at all).
        unrelated_documents = self._documents(20, pages=50)
        target_documents = {21: _document(21, stage="ID", pages=5)}

        def fake_extract_unrelated(document, pages):
            return {page: _snapshot("ничего релевантного здесь нет", page=page) for page in pages}

        def fake_extract_target(document, pages):
            return {pages[0]: _snapshot("Фундаментная плита выполнена из бетона класса B25", page=pages[0])}

        budget = {"documents": 30, "pages": 120, "ocr_pages": 0}
        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract_unrelated):
            first, _ = find_confirming_fallback_observation(
                "KR-055", "ID", location="Вертикальные конструкции подземной части", expected_normalized_value="B15",
                documents=unrelated_documents, exclude_pages=set(), budget=budget, max_pages_this_attempt=60,
            )
        self.assertIsNone(first)
        self.assertGreater(budget["pages"], 0)

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract_target):
            second, diagnostics = find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=target_documents, exclude_pages=set(), budget=budget, max_pages_this_attempt=60,
            )
        self.assertIsNotNone(second)
        self.assertTrue(diagnostics["found"])

    def test_no_documents_of_the_required_stage_means_nothing_is_scanned(self):
        documents = self._documents(5, stage="RD")  # none are stage ID
        with patch("app.domain.official_rule_packs.extract_original_pages") as mock_extract:
            budget = new_fallback_budget()
            observation, diagnostics = find_confirming_fallback_observation(
                "KR-055", "ID", location="Фундаментная плита", expected_normalized_value="B25",
                documents=documents, exclude_pages=set(), budget=budget,
            )
        mock_extract.assert_not_called()
        self.assertIsNone(observation)
        self.assertEqual(diagnostics["documents_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
