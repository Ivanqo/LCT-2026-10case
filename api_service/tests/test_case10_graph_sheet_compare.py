"""GRAPH: sheet pairing, raster comparison and the GRAPHIC_DIFF hypothesis method.

Pure functions on dicts, raster tests on synthetic vector PDFs (skipped without OpenCV), service / API tests on two generated PDFs in
the synthetic project. The viewing mode is behind CASE10_SHEET_COMPARE_ENABLED (default off) and GRAPHIC_DIFF is not a default method."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fitz

from app.db.models import DocumentVersion, InspectionProcess
from app.domain.free_hypotheses import base as hyp_base
from app.domain.free_hypotheses import graphic_diff
from app.domain.sheet_compare import config as cfg
from app.domain.sheet_compare import matching, meta as sheet_meta, raster, service
from test_case10_s5_workbench import WorkbenchTestBase

needs_cv = unittest.skipUnless(raster.available(), "OpenCV is not installed")
FONT = next((f for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf") if Path(f).is_file()), None)
needs_font = unittest.skipUnless(FONT, "no Cyrillic TrueType font on this machine")


def put(page, point, text, size):
    if FONT:
        page.insert_text(point, text, fontsize=size, fontfile=FONT, fontname="cyr")
    else:
        page.insert_text(point, text, fontsize=size)


# ---------------------------------------------------------------- builders
def sheet_pdf(*, title: str = "План 1 этажа", rooms=("101", "102", "103", "104"), extra_box: bool = False, shift: float = 0.0, size=(1190.0, 842.0),
              level: str | None = "+3.000") -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    w, h = size
    for i in range(6):
        x = 120 + i * 140 + shift
        page.draw_line((x, 80 + shift), (x, h - 260), width=1.2)
    for j in range(5):
        y = 80 + j * 90 + shift
        page.draw_line((120 + shift, y), (w - 260 + shift, y), width=1.2)
    page.draw_rect(fitz.Rect(200 + shift, 150 + shift, 330 + shift, 230 + shift), width=1.5)
    page.draw_rect(fitz.Rect(520 + shift, 330 + shift, 640 + shift, 410 + shift), width=1.5)
    for index, room in enumerate(rooms):
        put(page, (150 + index * 140 + shift, 130 + shift), room, 12)
    if extra_box:
        page.draw_rect(fitz.Rect(800, 120, 900, 200), width=2.0)
        page.draw_line((800, 120), (900, 200), width=2.0)
    put(page, (w - 330, h - 50), title + (f" на отм. {level}" if level else ""), 14)
    put(page, (w - 330, h - 30), "Лист", 8)
    put(page, (w - 280, h - 30), "Листов", 8)
    return doc.tobytes()


def meta_of(pdf: bytes) -> dict:
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return sheet_meta.page_meta(doc[0])


def pm(**kw):
    base = {"width": 1190.0, "height": 842.0, "title": None, "kind": None, "rooms": [], "labels": [], "levels": [], "floor": None, "floor_status": "not_found",
            "building": None, "building_status": "not_found", "cipher": None, "sheet_no": None}
    base.update(kw)
    return base


class PairingTests(unittest.TestCase):
    def test_identity_reads_floor_section_axes_and_level(self):
        got = matching.identity("План 2 этажа секции С1 в осях 1-5 на отм. +6,000")
        self.assertEqual(got["floor"], "2")
        self.assertEqual(got["section"], "с1")
        self.assertEqual(got["axes"], "1-5")
        self.assertEqual(got["level"], "+6.000")

    def test_different_floor_section_or_axes_reject_the_pair_even_with_equal_rooms(self):
        rooms = [str(100 + i) for i in range(12)]
        a = pm(title="План 1 этажа", kind="plan", rooms=rooms)
        for other in ("План 2 этажа", "План 1 этажа секции 2", "План 1 этажа в осях 1-5"):
            b = pm(title=other, kind="plan", rooms=rooms)
            if other.endswith("осях 1-5"):
                a2 = pm(title="План 1 этажа в осях 6-10", kind="plan", rooms=rooms)
                result = matching.score_pair(a2, b, use_embeddings=False)
            else:
                a2 = a if "секции" not in other else pm(title="План 1 этажа секции 1", kind="plan", rooms=rooms)
                result = matching.score_pair(a2, b, use_embeddings=False)
            self.assertIsNotNone(result["reject"], other)
            self.assertEqual(result["confidence"], 0.0)

    def test_disjoint_level_marks_of_two_plans_reject(self):
        a = pm(title="План", kind="plan", levels=["+3.000"], rooms=["1", "2", "3"])
        b = pm(title="План", kind="plan", levels=["+6.000"], rooms=["1", "2", "3"])
        self.assertIn("отметки", matching.score_pair(a, b, use_embeddings=False)["reject"])

    def test_one_piece_of_evidence_is_capped(self):
        a, b = pm(title="План типового этажа", kind="plan"), pm(title="План типового этажа", kind="plan")
        result = matching.score_pair(a, b, use_embeddings=False)
        self.assertLessEqual(result["confidence"], matching.EVIDENCE_CAP)
        self.assertEqual(result["evidence"], 1)

    def test_shared_rooms_levels_and_title_give_a_confident_pair(self):
        rooms = [str(100 + i) for i in range(10)]
        a = pm(title="План на отм. +3.000", kind="plan", levels=["+3.000"], rooms=rooms, labels=[f"л{i}ab" for i in range(10)])
        b = pm(title="План на отм. +3.000 секции", kind="plan", levels=["+3.000"], rooms=rooms, labels=[f"л{i}ab" for i in range(9)])
        result = matching.score_pair(a, b, use_embeddings=False)
        self.assertGreater(result["confidence"], 0.8)
        self.assertGreaterEqual(result["evidence"], 3)

    def test_matching_is_mutual_best_and_flags_ties(self):
        rooms1 = [str(100 + i) for i in range(10)]
        rooms2 = [str(300 + i) for i in range(10)]
        left = [{"key": 1, "meta": pm(title="План на отм. +3.000", kind="plan", levels=["+3.000"], rooms=rooms1)},
                {"key": 2, "meta": pm(title="План на отм. +6.000", kind="plan", levels=["+6.000"], rooms=rooms2)}]
        right = [{"key": 11, "meta": pm(title="План на отм. +6.000", kind="plan", levels=["+6.000"], rooms=rooms2)},
                 {"key": 10, "meta": pm(title="План на отм. +3.000", kind="plan", levels=["+3.000"], rooms=rooms1)}]
        pairs = matching.match_sheets(left, right, use_embeddings=False)
        self.assertEqual({(p["left"], p["right"]) for p in pairs}, {(1, 10), (2, 11)})
        twin = [{"key": 20, "meta": right[1]["meta"]}, {"key": 21, "meta": right[1]["meta"]}]
        tied = matching.match_sheets(left[:1], twin, use_embeddings=False)
        self.assertEqual(len(tied), 1)
        self.assertTrue(tied[0]["ambiguous"])
        self.assertLess(tied[0]["confidence"], tied[0]["raw_confidence"])

    def test_same_series_cipher_decides_revisions(self):
        a = pm(cipher="23.009-Р-КЖ", sheet_no="4", title="Схема", kind="scheme")
        b = pm(cipher="23.009-Р-КЖ", sheet_no="4", title="Схема", kind="scheme")
        c = pm(cipher="23.009-Р-КЖ", sheet_no="5", title="Схема", kind="scheme")
        same = matching.score_pair(a, b, same_series=True, use_embeddings=False)
        other = matching.score_pair(a, c, same_series=True, use_embeddings=False)
        self.assertGreater(same["confidence"], other["confidence"])
        self.assertEqual(same["components"]["cipher"], 1.0)

    def test_pair_confidence_is_deterministic(self):
        a = pm(title="План 1 этажа", kind="plan", rooms=["101", "102", "103"])
        self.assertEqual(matching.score_pair(a, a, use_embeddings=False), matching.score_pair(a, a, use_embeddings=False))


class MetaTests(unittest.TestCase):
    @needs_font
    def test_synthetic_sheet_title_rooms_and_level(self):
        meta = meta_of(sheet_pdf())
        self.assertEqual(meta["kind"], "plan")
        self.assertIn("План 1 этажа", meta["title"])
        self.assertTrue({"101", "102", "103"} <= set(meta["rooms"]))
        self.assertIn("+3.000", meta["levels"])
        self.assertTrue(sheet_meta.is_drawing(meta))

    def test_meta_cache_is_per_hash_and_stable(self):
        pdf = sheet_pdf()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_CACHE_DIR": tmp}):
            first = sheet_meta.document_meta(pdf)
            second = sheet_meta.document_meta(pdf)
            self.assertEqual(first, second)
            self.assertEqual(len(list((Path(tmp) / "meta").glob("*.json"))), 1)


@needs_cv
class RasterTests(unittest.TestCase):
    def test_identical_sheets_are_fully_similar_without_regions(self):
        pdf = sheet_pdf()
        result = raster.compare_images(pdf, 1, pdf, 1)
        self.assertTrue(result["comparable"])
        self.assertGreaterEqual(result["similarity"], 0.99)
        self.assertEqual(result["regions"], [])
        self.assertEqual(result["layout_match"], "same")

    def test_added_drawing_is_found_in_its_place_and_only_on_the_right(self):
        result = raster.compare_images(sheet_pdf(), 1, sheet_pdf(extra_box=True), 1)
        self.assertLess(result["similarity"], 0.99)
        self.assertGreater(result["similarity"], 0.8)
        hit = [r for r in result["regions"] if r["bbox_left"][0] < 910 and r["bbox_left"][2] > 790 and r["bbox_left"][1] < 210 and r["bbox_left"][3] > 110]
        self.assertTrue(hit, result["regions"])
        self.assertEqual(hit[0]["kind"], "only_right")
        self.assertEqual(hit[0]["only_left_px"], 0)

    def test_a_small_shift_is_aligned_away(self):
        result = raster.compare_images(sheet_pdf(), 1, sheet_pdf(shift=6.0), 1)
        self.assertGreater(result["similarity"], 0.9, result["alignment"])

    def test_other_format_is_not_rastered(self):
        result = raster.compare_images(sheet_pdf(), 1, sheet_pdf(size=(842.0, 1190.0)), 1)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["reason"], "format_mismatch")
        self.assertIsNone(result["similarity"])

    def test_two_runs_are_identical_and_the_cache_returns_the_same(self):
        a, b = sheet_pdf(), sheet_pdf(extra_box=True)
        first = raster.compare_images(a, 1, b, 1, want_overlay=True)
        second = raster.compare_images(a, 1, b, 1, want_overlay=True)
        self.assertEqual(first.pop("_overlay"), second.pop("_overlay"))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_CACHE_DIR": tmp}):
            sha_a, sha_b = hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()
            cold, overlay = raster.compare_cached(a, sha_a, 1, b, sha_b, 1, want_overlay=True)
            with mock.patch.object(raster, "compare_images", side_effect=AssertionError("cache miss")):
                warm, overlay2 = raster.compare_cached(a, sha_a, 1, b, sha_b, 1, want_overlay=True)
            self.assertEqual(cold, warm)
            self.assertEqual(overlay, overlay2)
            self.assertTrue(overlay.startswith(b"\x89PNG"))

    def test_similarity_is_not_the_pair_confidence(self):
        pdf = sheet_pdf()
        compared = raster.compare_images(pdf, 1, sheet_pdf(extra_box=True), 1)
        pairing = matching.score_pair(meta_of(pdf), meta_of(sheet_pdf(extra_box=True)), use_embeddings=False)
        self.assertNotEqual(compared["similarity"], pairing["confidence"])
        self.assertNotIn("deviation", compared)

    def test_without_opencv_the_feature_reports_unavailable(self):
        with mock.patch.object(raster, "cv2", None):
            self.assertFalse(raster.available())
            self.assertFalse(raster.status()["available"])
            with self.assertRaises(RuntimeError):
                raster.compare_images(sheet_pdf(), 1, sheet_pdf(), 1)


def fake_doc(doc_id, stage, name, *, revision=None):
    return SimpleNamespace(id=doc_id, filename=name, dataset_file_id=f"F{doc_id}", dataset_stage=stage, doc_stage={"PD": "project", "RD": "working"}[stage],
                           dataset_section="АР", discipline="АР", document_code=name, file_hash=f"{doc_id:064x}", content_hash=f"{doc_id:064x}", revision=revision,
                           approval_status="UNKNOWN", dataset_metadata=None)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        from app.domain.comparison_gate import GateContext

        self.pd = fake_doc(1, "PD", "01-03-00-01-АР.pdf")
        self.rd = fake_doc(2, "RD", "19-0322-ОК-1_Н-АР1. План_Изм.3.pdf")
        self.rd_old = fake_doc(3, "RD", "19-0322-ОК-1_Н-АР1. План_Изм.2.pdf")
        self.ov = fake_doc(4, "RD", "19-0322-ОК-1_Н-ОВ1. План.pdf")
        self.ov.dataset_section = self.ov.discipline = "ОВ"
        self.docs = [self.pd, self.rd, self.rd_old, self.ov]
        self.gate = GateContext(self.docs)
        self.pdfs = {1: sheet_pdf(), 2: sheet_pdf(extra_box=True), 3: sheet_pdf(), 4: sheet_pdf()}
        patcher = mock.patch.object(service, "_bytes", side_effect=lambda d: (self.pdfs[int(d.id)], f"{int(d.id):064x}"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_document_pairs_stay_inside_one_discipline_and_one_series(self):
        pairs = service.document_pairs(self.docs, self.gate)
        stage = {(int(p["left"].id), int(p["right"].id)) for p in pairs if p["kind"] == "stage"}
        self.assertEqual(stage, {(1, 2), (1, 3)})                      # the ОВ file is never paired with the АР volume
        revisions = [(int(p["left"].id), int(p["right"].id)) for p in pairs if p["kind"] == "revision"]
        self.assertEqual(revisions, [(3, 2)])                          # the older revision is on the left

    @needs_font
    def test_sheet_pairs_lists_matched_sheets_with_confidence_only(self):
        listing = service.sheet_pairs(self.docs, self.gate, kinds=("stage",))
        self.assertTrue(listing["pairs"])
        first = listing["pairs"][0]
        self.assertIn("pair_confidence", first)
        self.assertNotIn("similarity", first)                          # the picture is compared on demand, never mixed into the pairing
        self.assertEqual(first["left"]["stage"], "PD")

    @needs_cv
    def test_compare_sheets_keeps_pairing_and_picture_apart(self):
        result, overlay = service.compare_sheets(self.pd, 1, self.rd, 1, want_overlay=True)
        self.assertIn("confidence", result["pairing"])
        self.assertIn("similarity", result["picture"])
        self.assertIsNotNone(overlay)
        self.assertNotEqual(result["pairing"]["confidence"], result["picture"]["similarity"])
        self.assertIn("не определяются", result["note"])


class GraphicDiffTests(unittest.TestCase):
    def test_not_a_default_method_and_off_by_default(self):
        self.assertNotIn(hyp_base.METHOD_GRAPHIC, hyp_base.DEFAULT_METHODS)
        self.assertIn(hyp_base.METHOD_GRAPHIC, hyp_base.ALL_METHODS)
        with mock.patch.dict(os.environ, {"CASE10_FREE_HYPOTHESES_METHODS": ""}):
            self.assertNotIn(hyp_base.METHOD_GRAPHIC, hyp_base.enabled_methods())
        with mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_ENABLED": "", "CASE10_GRAPHIC_DIFF_FORCE": ""}):
            self.assertFalse(cfg.sheet_compare_enabled())
            candidates, stats = graphic_diff.detect([], None, object_id="O")
            self.assertEqual(candidates, [])
            self.assertEqual(stats["skipped"], "sheet_compare_disabled")

    def _run(self, *, confidence=0.9, ambiguous=False, layout="same", similarity=0.9, area=2000.0, regions=3):
        pd, rd = fake_doc(1, "PD", "a.pdf"), fake_doc(2, "RD", "b.pdf")
        pair = {"kind": "stage", "discipline": "АР", "pair_confidence": confidence, "ambiguous": ambiguous, "components": {"title": 1.0}, "basis": [],
                "left": {"document_id": 1, "page": 3, "title": "План 1 этажа"}, "right": {"document_id": 2, "page": 5, "title": "План 1 этажа"}}
        picture = {"comparable": True, "layout_match": layout, "similarity": similarity, "left_size_pt": [1190.0, 842.0], "right_size_pt": [1190.0, 842.0],
                   "alignment": {"method": "identity"},
                   "regions": [{"id": i + 1, "bbox_left": [100 + 50 * i, 100, 200 + 50 * i, 180], "bbox_right": [100 + 50 * i, 100, 200 + 50 * i, 180],
                                "area_mm2": area, "only_left_px": 10, "only_right_px": 400, "kind": "only_right", "share": 0.3} for i in range(regions)]}
        env = {"CASE10_SHEET_COMPARE_ENABLED": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(raster, "available", return_value=True), \
                mock.patch.object(service, "sheet_pairs", return_value={"pairs": [pair], "total": 1, "document_pairs": 1, "truncated_document_pairs": 0}), \
                mock.patch.object(service, "compare_sheets", return_value=({"picture": picture}, None)):
            return graphic_diff.detect([pd, rd], None, object_id="OBJ")

    def test_a_significant_region_becomes_a_hypothesis_with_both_sources_and_boxes(self):
        out, stats = self._run(regions=1)
        self.assertEqual(len(out), 1)
        cand = out[0]
        self.assertEqual(cand.method, "GRAPHIC_DIFF")
        self.assertEqual([r.stage for r in cand.evidence], ["PD", "RD"])
        self.assertTrue(all(r.bbox_pdf and r.page_width for r in cand.evidence))
        self.assertIsNone(cand.details["deviation"])
        self.assertEqual(cand.details["pair_confidence"], 0.9)
        self.assertEqual(cand.details["picture_similarity"], 0.9)
        self.assertIn("решает инспектор", cand.description)
        self.assertNotIn("нарушен", cand.description.lower().replace("нарушение", "").replace("нарушением", ""))
        self.assertLess(cand.confidence, 1.0)

    def test_limits_ambiguity_layout_and_identity_filters(self):
        self.assertEqual(len(self._run(regions=6)[0]), cfg.GRAPHIC_MAX_PER_PAIR)           # per-pair limit
        self.assertEqual(self._run(ambiguous=True)[0], [])                                 # ambiguous pair: no hypothesis
        self.assertEqual(self._run(layout="different")[0], [])                             # different composition: no localised difference
        self.assertEqual(self._run(similarity=0.995)[0], [])                               # (almost) identical sheets
        self.assertEqual(self._run(area=100.0)[0], [])                                     # region too small
        _, stats = self._run(layout="partial")
        self.assertEqual(stats["different_layout"], 1)

    def test_per_document_limit(self):
        with mock.patch.object(cfg, "GRAPHIC_MAX_PER_DOCUMENT", 1), mock.patch.object(cfg, "GRAPHIC_MAX_PER_PAIR", 3):
            out, stats = self._run(regions=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(stats.get("cut_by_document_limit"), 2)

    def test_dedup_key_is_stable(self):
        a, _ = self._run(regions=1)
        b, _ = self._run(regions=1)
        self.assertEqual(a[0].dedup_key, b[0].dedup_key)


class ApiTests(WorkbenchTestBase):
    def _document(self, directory: str, name: str, stage: str, data: bytes, *, object_id="OBJ-GRAPH") -> DocumentVersion:
        path = Path(directory) / name
        path.write_bytes(data)
        doc = DocumentVersion(project_id=self.project.id, organization_id=self.org_id, source_type="upload", object_id=object_id, dataset_file_id=name, dataset_stage=stage,
                              dataset_section="АР", filename=name, doc_stage={"PD": "project", "RD": "working"}[stage], document_stage=stage, discipline="АР",
                              document_code=name, file_hash=hashlib.sha256(data).hexdigest(), file_path=str(path))
        self.db.add(doc)
        self.db.commit()
        return doc

    def _setup(self, tmp):
        self.pd = self._document(tmp, "01-03-00-01-АР.pdf", "PD", sheet_pdf())
        self.rd = self._document(tmp, "АР1.pdf", "RD", sheet_pdf(extra_box=True))
        self.db.add(InspectionProcess(id="proc-graph", project_id=self.project.id, organization_id=self.org_id, object_id="OBJ-GRAPH", matrix_version="official-132-v1",
                                      dataset_version="synthetic-v1", upload_scenario="FULL", status="READY"))
        self.db.commit()

    def test_switched_off_by_default(self):
        with mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_ENABLED": ""}):
            status = self.client.get("/api/case10/sheet-compare/status").json()
            self.assertFalse(status["enabled"])
            self.assertEqual(self.client.get("/api/case10/sheet-compare", params={"left_document_id": 1, "left_page": 1, "right_document_id": 2, "right_page": 1}).status_code, 404)
            self.assertEqual(self.client.get("/api/case10/processes/proc-graph/sheet-pairs").status_code, 404)

    @needs_cv
    @needs_font
    def test_pairs_compare_and_overlay_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_ENABLED": "1", "CASE10_SHEET_COMPARE_CACHE_DIR": os.path.join(tmp, "cache")}):
            self._setup(tmp)
            self.assertTrue(self.client.get("/api/case10/sheet-compare/status").json()["enabled"])
            listing = self.client.get("/api/case10/processes/proc-graph/sheet-pairs?kind=stage")
            self.assertEqual(listing.status_code, 200, listing.text)
            pairs = listing.json()["pairs"]
            self.assertEqual(len(pairs), 1)
            self.assertEqual((pairs[0]["left"]["document_id"], pairs[0]["right"]["document_id"]), (self.pd.id, self.rd.id))
            query = {"left_document_id": self.pd.id, "left_page": 1, "right_document_id": self.rd.id, "right_page": 1}
            result = self.client.get("/api/case10/sheet-compare", params=query)
            self.assertEqual(result.status_code, 200, result.text)
            body = result.json()
            self.assertLess(body["picture"]["similarity"], 0.99)
            self.assertTrue(body["picture"]["regions"])
            self.assertNotEqual(body["pairing"]["confidence"], body["picture"]["similarity"])
            overlay = self.client.get("/api/case10/sheet-compare/overlay.png", params=query)
            self.assertEqual(overlay.status_code, 200)
            self.assertEqual(overlay.headers["content-type"], "image/png")
            self.assertEqual(self.client.get("/api/case10/sheet-compare", params={**query, "right_page": 9}).status_code, 422)

    @needs_cv
    def test_other_organization_cannot_read_the_documents(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CASE10_SHEET_COMPARE_ENABLED": "1"}):
            self._setup(tmp)
            self.current_user = self.outsider
            query = {"left_document_id": self.pd.id, "left_page": 1, "right_document_id": self.rd.id, "right_page": 1}
            self.assertEqual(self.client.get("/api/case10/sheet-compare", params=query).status_code, 404)
            self.assertEqual(self.client.get("/api/case10/processes/proc-graph/sheet-pairs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
