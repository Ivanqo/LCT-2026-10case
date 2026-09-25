"""Tests for `live_candidate_tagger.py` -- the ТЗ 9.1 item 2 live candidate
scanner (see project_case10_new_objects_no_tagger_gap memory / NEW_OBJECTS_
ANALYSE.md for why this module exists: without it, every extraction
mechanism structurally finds 0 candidates on any object lacking organizer-
supplied `learning_annotation` training data). No real document/gold value
appears here -- every fixture is synthetic, mirroring `test_case10_generic_
extraction.py`'s and `test_case10_anchor_search.py`'s own fixture style;
`extract_original_pages` is always mocked so no real PDF is opened."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DocumentVersion, SourceFragment
from app.domain.live_candidate_tagger import (
    LIVE_TAGGER_SOURCE_SYSTEM,
    _already_scanned,
    _applicable_anchors,
    _build_param_anchors,
    _canonical_discipline,
    _confidence,
    _document_stage,
    tag_live_candidates,
)
from app.domain.official_evidence import inference_annotation
from app.domain.official_rule_packs import is_inference_locator


def _param(code: str, name: str, *, pd=True, rd=True, id_=True, hint_rd: str | None = None) -> SimpleNamespace:
    import json

    other_normative = json.dumps({"source_rd": hint_rd}) if hint_rd else None
    return SimpleNamespace(
        code=code, parameter_name=name, source_pd=pd, source_rd=rd, source_id=id_,
        other_normative=other_normative,
    )


def _row_words(rows: list[list[str]], *, row_height: float = 14.0, col_width: float = 70.0, start_y: float = 100.0) -> list[dict]:
    words = []
    y = start_y
    for row in rows:
        x = 40.0
        for cell in row:
            cell_width = max(col_width, len(cell) * 7.0)
            words.append({"text": cell, "bbox": [x, y, x + cell_width, y + 10.0]})
            x += cell_width + 20.0
        y += row_height
    return words


class DocumentStageAndDisciplineTests(unittest.TestCase):
    def test_pd_rd_id_pass_through(self):
        for stage in ("PD", "RD", "ID"):
            doc = SimpleNamespace(dataset_stage=stage)
            self.assertEqual(_document_stage(doc), stage)

    def test_rd_id_mixed_is_excluded_not_remapped(self):
        doc = SimpleNamespace(dataset_stage="RD_ID_MIXED")
        self.assertIsNone(_document_stage(doc))

    def test_unknown_or_missing_stage_is_none(self):
        self.assertIsNone(_document_stage(SimpleNamespace(dataset_stage=None)))
        self.assertIsNone(_document_stage(SimpleNamespace(dataset_stage="whatever")))

    def test_canonical_discipline_aliases(self):
        self.assertEqual(_canonical_discipline("КЖ"), "КР")
        self.assertEqual(_canonical_discipline("kr"), "КР")
        self.assertIsNone(_canonical_discipline(None))
        self.assertIsNone(_canonical_discipline(""))


class ApplicableAnchorsTests(unittest.TestCase):
    def test_hint_less_param_applies_to_any_discipline(self):
        anchors = _build_param_anchors([_param("PZ-001", "Площадь застройки")])["RD"]
        self.assertEqual(len(_applicable_anchors(anchors, "АР")), 1)
        self.assertEqual(len(_applicable_anchors(anchors, None)), 1)

    def test_hinted_param_only_applies_to_matching_discipline(self):
        anchors = _build_param_anchors([_param("PZ-002", "Общая площадь здания", hint_rd="Раздел АР: Лист")])["RD"]
        self.assertEqual(len(_applicable_anchors(anchors, "АР")), 1)
        self.assertEqual(len(_applicable_anchors(anchors, "КР")), 0)
        self.assertEqual(len(_applicable_anchors(anchors, None)), 0)

    def test_short_or_blank_names_are_excluded_as_unsafe_anchors(self):
        anchors = _build_param_anchors([_param("X-1", "N"), _param("X-2", "")])["RD"]
        self.assertEqual(anchors, [])


class ConfidenceTests(unittest.TestCase):
    def test_no_semantic_signal_is_base_confidence(self):
        self.assertEqual(_confidence(None), 0.5)

    def test_higher_semantic_score_raises_confidence_capped(self):
        self.assertGreater(_confidence(0.9), _confidence(0.5))
        self.assertLessEqual(_confidence(0.99), 0.9)


class TagLiveCandidatesTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def _make_doc(self, *, stage="RD", discipline="АР", pdf_pages=1) -> DocumentVersion:
        doc = DocumentVersion(
            project_id=1, organization_id=1, source_type="case10_dataset",
            filename=f"synthetic-{stage}.pdf", dataset_stage=stage, discipline=discipline,
            dataset_metadata={"document_manifest": {"pdf_pages": pdf_pages, "relative_path": "synthetic.pdf"}},
            file_hash="synthetic-hash",
        )
        self.db.add(doc)
        self.db.flush()
        return doc

    def _budget(self, **overrides):
        # Documents/pages only: the tagger's limits are deterministic caps,
        # never wall-clock (see live_candidate_tagger.new_live_tagger_budget).
        budget = {"documents": 100.0, "pages": 1000.0}
        budget.update(overrides)
        return budget

    def test_literal_anchor_match_creates_a_live_tagger_fragment_visible_to_both_consumers(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Площадь", "застройки", "4650,91"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot), \
             patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None):
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())

        self.assertEqual(diagnostics["fragments_created"], 1)
        fragment = self.db.query(SourceFragment).one()
        self.assertEqual(fragment.source_system, LIVE_TAGGER_SOURCE_SYSTEM)
        self.assertEqual(fragment.metadata_json["code"], "PZ-001")
        self.assertEqual(fragment.metadata_json["status"], "AUTO_FIELD_CANDIDATE")
        self.assertIsNone(fragment.metadata_json["check_id"])
        self.assertTrue(inference_annotation(fragment))
        self.assertTrue(is_inference_locator(fragment))

    def test_no_literal_match_creates_nothing(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Совершенно", "другой", "текст"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot):
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        self.assertEqual(diagnostics["fragments_created"], 0)
        self.assertEqual(self.db.query(SourceFragment).count(), 0)

    def test_low_semantic_score_rejects_an_otherwise_literal_match(self):
        # Same failure class as checkpoint 40's "Стена в грунте" false
        # positive -- a literal word match on a page whose row reads
        # nothing like the parameter it matched.
        doc = self._make_doc()
        params = [_param("PZ-002", "Общая площадь здания")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Общая", "площадь", "здания", "11618,27"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot), \
             patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=0.10):
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        self.assertEqual(diagnostics["fragments_created"], 0)
        self.assertEqual(diagnostics["semantic_rejections"], 1)

    def test_high_semantic_score_keeps_the_match(self):
        doc = self._make_doc()
        params = [_param("PZ-002", "Общая площадь здания")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Общая", "площадь", "здания", "11618,27"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot), \
             patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=0.73):
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        self.assertEqual(diagnostics["fragments_created"], 1)

    def test_rd_id_mixed_document_is_never_scanned(self):
        doc = self._make_doc(stage="RD_ID_MIXED")
        params = [_param("PZ-001", "Площадь застройки")]
        with patch("app.domain.live_candidate_tagger.extract_original_pages") as mocked:
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        mocked.assert_not_called()
        self.assertEqual(diagnostics["documents_scanned"], 0)

    def test_discipline_mismatch_document_is_never_scanned(self):
        doc = self._make_doc(discipline="КР")
        params = [_param("PZ-002", "Общая площадь здания", hint_rd="Раздел АР: Лист")]
        with patch("app.domain.live_candidate_tagger.extract_original_pages") as mocked:
            tag_live_candidates(self.db, [doc], params, budget=self._budget())
        mocked.assert_not_called()

    def test_discipline_hint_narrows_which_documents_open_but_not_which_anchors_are_searched(self):
        # Real regression, forensically found on LOS3A RD page 11 (see the
        # live-candidate-tagger checkpoint memory): PZ-001's own catalog
        # source_rd hint parses to discipline "ГП", but the real ТЭП table
        # restating it sits on an АР "Общие данные" sheet alongside PZ-002
        # (no hint) -- the document is worth opening because of PZ-002, and
        # once open, PZ-001 must still be found on it despite its own
        # mismatched hint. A КР-discipline document with NEITHER param
        # applicable must still never be opened at all.
        ar_doc = self._make_doc(discipline="АР")
        kr_doc = self._make_doc(discipline="КР", pdf_pages=1)
        params = [
            _param("PZ-001", "Площадь застройки", hint_rd="Раздел ПП (ГП): Лист"),
            _param("PZ-002", "Общая площадь здания", hint_rd="Раздел АР: Лист"),
        ]
        ar_snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([
            ["Площадь", "застройки", "4650,91"],
            ["Общая", "площадь", "здания", "11618,27"],
        ])}}

        def fake_extract(document, pages):
            if document.id == ar_doc.id:
                return ar_snapshot
            raise AssertionError("the КР-discipline document must never be opened")

        with patch("app.domain.live_candidate_tagger.extract_original_pages", side_effect=fake_extract), \
             patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None):
            diagnostics = tag_live_candidates(self.db, [ar_doc, kr_doc], params, budget=self._budget())

        self.assertEqual(diagnostics["documents_scanned"], 1)
        codes = {f.metadata_json["code"] for f in self.db.query(SourceFragment).all()}
        self.assertEqual(codes, {"PZ-001", "PZ-002"})

    def test_second_call_skips_an_already_tagged_document(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Площадь", "застройки", "4650,91"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot):
            tag_live_candidates(self.db, [doc], params, budget=self._budget())
            self.assertTrue(_already_scanned(doc))
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        self.assertEqual(diagnostics["documents_scanned"], 0)
        self.assertEqual(diagnostics["documents_skipped_already_tagged"], 1)
        self.assertEqual(self.db.query(SourceFragment).count(), 1)

    def test_force_rescans_an_already_tagged_document(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Площадь", "застройки", "4650,91"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot):
            tag_live_candidates(self.db, [doc], params, budget=self._budget())
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget(), force=True)
        self.assertEqual(diagnostics["documents_scanned"], 1)
        # Same (document, page, code) external_id -- upsert-shaped identity,
        # never a duplicate row.
        self.assertEqual(self.db.query(SourceFragment).count(), 1)

    def test_exhausted_document_budget_stops_cleanly_without_scanning(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        with patch("app.domain.live_candidate_tagger.extract_original_pages") as mocked:
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget(documents=0.0))
        mocked.assert_not_called()
        self.assertEqual(diagnostics["documents_scanned"], 0)

    def test_disabled_flag_is_a_clean_no_op(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки")]
        with patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_ENABLED", False), \
             patch("app.domain.live_candidate_tagger.extract_original_pages") as mocked:
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        mocked.assert_not_called()
        self.assertEqual(diagnostics["fragments_created"], 0)

    def test_shared_page_answers_multiple_params_from_one_render(self):
        doc = self._make_doc()
        params = [_param("PZ-001", "Площадь застройки"), _param("PZ-002", "Общая площадь здания")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([
            ["Площадь", "застройки", "4650,91"],
            ["Общая", "площадь", "здания", "11618,27"],
        ])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot) as mocked, \
             patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None):
            diagnostics = tag_live_candidates(self.db, [doc], params, budget=self._budget())
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(diagnostics["fragments_created"], 2)

    def test_organizer_and_live_tagger_fragments_coexist_additively(self):
        # An object that already has organizer tags keeps using them; the
        # live tagger's own rows are additive, never a replacement.
        doc = self._make_doc()
        organizer_fragment = SourceFragment(
            document_version_id=doc.id, source_system="learning_annotation", external_id="ANN:1",
            page=1, metadata_json={"code": "PZ-001", "annotation_type": "MATRIX_FIELD", "status": "AUTO_FIELD_CANDIDATE", "check_id": None},
            confidence=0.6,
        )
        self.db.add(organizer_fragment)
        self.db.flush()
        params = [_param("PZ-001", "Площадь застройки")]
        snapshot = {1: {"page": 1, "width": 595.0, "height": 842.0, "words": _row_words([["Площадь", "застройки", "4650,91"]])}}
        with patch("app.domain.live_candidate_tagger.extract_original_pages", return_value=snapshot):
            tag_live_candidates(self.db, [doc], params, budget=self._budget())
        fragments = self.db.query(SourceFragment).all()
        self.assertEqual(len(fragments), 2)
        systems = {f.source_system for f in fragments}
        self.assertEqual(systems, {"learning_annotation", "live_tagger"})
        self.assertTrue(all(inference_annotation(f) for f in fragments))


if __name__ == "__main__":
    unittest.main()
