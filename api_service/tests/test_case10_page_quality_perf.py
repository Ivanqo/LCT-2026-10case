"""`_quality_for_page` fast path == pre-optimisation reference, plus the optional on-disk result cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fitz

from app.domain import dataset_sources, official_evidence, page_quality_cache
from quality_reference_impl import reference_quality


def _png(width: int = 40, height: int = 40, shade: int = 200) -> bytes:
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, width, height), False)
    pix.set_rect(pix.irect, (shade,))
    return pix.tobytes("png")


def _words(page, x0: float, y0: float, count: int, step: float = 30.0) -> None:
    for index in range(count):
        page.insert_text((x0 + index * step, y0), f"w{index}x", fontsize=10)


def _pdf_bytes(builder, pages: int = 1) -> bytes:
    pdf = fitz.open()
    for _ in range(pages):
        page = pdf.new_page(width=600, height=800)
        builder(page)
    data = pdf.tobytes()
    pdf.close()
    return data


def _b_image_over_text(page):
    _words(page, 100, 100, 5)
    page.insert_image(fitz.Rect(95, 85, 260, 110), stream=_png())


def _b_image_over_two_words(page):
    _words(page, 100, 100, 2)
    _words(page, 100, 300, 3)
    page.insert_image(fitz.Rect(95, 85, 170, 110), stream=_png())


def _b_big_image_over_text(page):
    _words(page, 100, 100, 5)
    page.insert_image(fitz.Rect(0, 0, 600, 400), stream=_png())


def _b_duplicate_placements(page):
    _words(page, 100, 100, 3)
    png = _png()
    page.insert_image(fitz.Rect(300, 600, 340, 640), stream=png)
    page.insert_image(fitz.Rect(300, 600, 340, 640), stream=png)
    page.insert_image(fitz.Rect(500, 700, 540, 740), stream=_png(shade=90))


def _b_duplicate_over_text(page):
    _words(page, 100, 100, 5)
    png = _png()
    for _ in range(3):
        page.insert_image(fitz.Rect(95, 85, 260, 110), stream=png)


def _b_stamp_over_text(page):
    _words(page, 100, 100, 3)
    page.add_stamp_annot(fitz.Rect(90, 80, 200, 120), stamp=0)


def _b_stamp_away_from_text(page):
    # the stamp's appearance carries its own words, so even a stamp far from the body text counts (old behaviour)
    _words(page, 100, 100, 3)
    page.add_stamp_annot(fitz.Rect(400, 500, 500, 560), stamp=0)


def _b_other_annots_over_text(page):
    _words(page, 100, 100, 3)
    page.add_rect_annot(fitz.Rect(90, 80, 300, 120))
    page.add_text_annot((100, 100), "note")
    page.add_highlight_annot(fitz.Rect(100, 90, 150, 105))


def _b_no_text(page):
    page.draw_rect(fitz.Rect(50, 50, 300, 300))


def _b_plain_text(page):
    _words(page, 100, 100, 6)


def _b_stamp_and_image(page):
    _words(page, 100, 100, 5)
    page.insert_image(fitz.Rect(95, 85, 260, 110), stream=_png())
    page.add_stamp_annot(fitz.Rect(90, 80, 200, 120), stamp=1)


BUILDERS = {
    "image_over_text": _b_image_over_text,
    "image_over_two_words": _b_image_over_two_words,
    "big_image_over_text": _b_big_image_over_text,
    "duplicate_placements": _b_duplicate_placements,
    "duplicate_over_text": _b_duplicate_over_text,
    "stamp_over_text": _b_stamp_over_text,
    "stamp_away_from_text": _b_stamp_away_from_text,
    "other_annots_over_text": _b_other_annots_over_text,
    "no_text": _b_no_text,
    "plain_text": _b_plain_text,
    "stamp_and_image": _b_stamp_and_image,
}
EXPECTED = {
    "image_over_text": ["IMAGE_STAMP_OVER_TEXT"],
    "image_over_two_words": [],
    "big_image_over_text": [],
    "duplicate_placements": [],
    "duplicate_over_text": ["IMAGE_STAMP_OVER_TEXT"],
    "stamp_over_text": ["STAMP_OVER_TEXT"],
    "stamp_away_from_text": ["STAMP_OVER_TEXT"],
    "other_annots_over_text": [],
    "no_text": ["NO_TEXT_LAYER_OCR_UNAVAILABLE"],
    "plain_text": [],
    "stamp_and_image": ["IMAGE_STAMP_OVER_TEXT", "STAMP_OVER_TEXT"],
}


def _no_ocr(*_args, **_kwargs):
    raise RuntimeError("OCR unavailable in tests")


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        official_evidence._QUALITY_PAGE_CACHE.clear()
        self.addCleanup(official_evidence._QUALITY_PAGE_CACHE.clear)
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("CASE10_QUALITY_CACHE_DIR", "CASE10_LIVE_TAGGER_CACHE_DIR"):
            os.environ.pop(key, None)
        patcher = patch.object(dataset_sources, "_ocr_page_words", side_effect=_no_ocr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def document(self, data: bytes, name: str = "doc.pdf"):
        digest = hashlib.sha256(data).hexdigest()
        patcher = patch.object(dataset_sources, "original_document_bytes", return_value=data)
        patcher.start()
        self.addCleanup(patcher.stop)
        return SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": f"case/{name}"}},
                               file_hash=digest, content_hash=digest, dataset_stage="PD")


class FastPathEquivalenceTests(_Env):
    def test_matches_reference_on_synthetic_pages(self):
        for name, builder in BUILDERS.items():
            with self.subTest(name):
                official_evidence._QUALITY_PAGE_CACHE.clear()
                data = _pdf_bytes(builder)
                new = official_evidence._quality_for_page(self.document(data, f"{name}.pdf"), 1)
                with fitz.open(stream=data, filetype="pdf") as pdf:
                    old = reference_quality(pdf, 1, ocr_words=_no_ocr)
                    deduped = reference_quality(pdf, 1, dedupe=True, ocr_words=_no_ocr)
                self.assertEqual(new, old)
                self.assertEqual(new, deduped)
                self.assertEqual(new["reasons"], EXPECTED[name], new)

    def test_rotated_page_and_out_of_range(self):
        def rotated(page):
            _words(page, 100, 100, 4)
            page.set_rotation(90)

        data = _pdf_bytes(rotated)
        document = self.document(data)
        new = official_evidence._quality_for_page(document, 1)
        self.assertEqual(new["reasons"], ["ROTATED_PAGE"])
        self.assertEqual(new["rotation"], 90)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            self.assertEqual(new, reference_quality(pdf, 1, ocr_words=_no_ocr))
        out = official_evidence._quality_for_page(document, 5)
        self.assertEqual(out["reasons"], ["PAGE_OUT_OF_RANGE"])

    def test_many_duplicate_placements_equal_to_reference(self):
        def tiled(page):
            _words(page, 20, 100, 12, step=40)
            png = _png(8, 8)
            for index in range(300):
                page.insert_image(fitz.Rect(20 + index % 20 * 25, 300 + index // 20 * 20,
                                            30 + index % 20 * 25, 310 + index // 20 * 20), stream=png)

        data = _pdf_bytes(tiled)
        new = official_evidence._quality_for_page(self.document(data), 1)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            self.assertEqual(new, reference_quality(pdf, 1, dedupe=True, ocr_words=_no_ocr))

    def test_vectorised_overlap_matches_scalar_predicate(self):
        # 30% of a word covered counts, a touching edge or an empty image does not.
        class Page:
            def __init__(self, bbox):
                self._bbox = bbox

            def get_image_info(self, xrefs=False):
                return [{"bbox": self._bbox, "xref": 7}]

        words = [fitz.Rect(0, 0, 10, 10)] * 3
        self.assertTrue(official_evidence._image_stamp_over_text(Page((0, 0, 3, 10)), words, 10000.0))
        self.assertFalse(official_evidence._image_stamp_over_text(Page((0, 0, 2.9, 10)), words, 10000.0))
        self.assertFalse(official_evidence._image_stamp_over_text(Page((10, 0, 20, 10)), words, 10000.0))
        self.assertFalse(official_evidence._image_stamp_over_text(Page((0, 0, 100, 100)), words, 20000.0))  # >= 35%
        self.assertFalse(official_evidence._image_stamp_over_text(Page((0, 0, 0, 10)), words, 10000.0))  # empty
        self.assertFalse(official_evidence._image_stamp_over_text(Page((0, 0, 10, 10)), words[:2], 10000.0))  # 2 words


class QualityDiskCacheTests(_Env):
    def _cache_dir(self) -> Path:
        directory = self.root / "qcache"
        os.environ["CASE10_QUALITY_CACHE_DIR"] = str(directory)
        return directory

    def test_cold_and_warm_results_are_identical_and_warm_skips_the_pdf(self):
        directory = self._cache_dir()
        document = self.document(_pdf_bytes(_b_image_over_text, pages=2))
        cold = [official_evidence._quality_for_page(document, page) for page in (1, 2)]
        self.assertEqual(len(list(directory.rglob("*.json"))), 2)
        official_evidence._QUALITY_PAGE_CACHE.clear()
        with patch.object(dataset_sources, "original_document_bytes", side_effect=AssertionError("read the PDF")):
            warm = [official_evidence._quality_for_page(document, page) for page in (1, 2)]
        self.assertEqual(cold, warm)
        self.assertEqual(json.dumps(cold), json.dumps(warm))
        official_evidence._QUALITY_PAGE_CACHE.clear()
        os.environ.pop("CASE10_QUALITY_CACHE_DIR")
        self.assertEqual(cold, [official_evidence._quality_for_page(document, page) for page in (1, 2)])

    def test_directory_falls_back_to_tagger_cache_subdir(self):
        os.environ["CASE10_LIVE_TAGGER_CACHE_DIR"] = str(self.root / "tagger")
        self.assertEqual(page_quality_cache.cache_dir(), self.root / "tagger" / "quality")
        os.environ["CASE10_QUALITY_CACHE_DIR"] = str(self.root / "explicit")
        self.assertEqual(page_quality_cache.cache_dir(), self.root / "explicit")

    def test_ocr_unavailable_result_is_not_cached(self):
        directory = self._cache_dir()
        document = self.document(_pdf_bytes(_b_no_text))
        result = official_evidence._quality_for_page(document, 1)
        self.assertEqual(result["reasons"], ["NO_TEXT_LAYER_OCR_UNAVAILABLE"])
        self.assertEqual(list(directory.rglob("*.json")) if directory.exists() else [], [])

    def test_read_failure_is_not_cached(self):
        directory = self._cache_dir()
        document = self.document(b"%PDF-not-a-pdf")
        result = official_evidence._quality_for_page(document, 1)
        self.assertTrue(result["reasons"][0].startswith("PAGE_QUALITY_READ_FAILED"))
        self.assertEqual(list(directory.rglob("*.json")) if directory.exists() else [], [])

    def test_corrupt_or_foreign_files_read_as_a_miss(self):
        directory = self._cache_dir()
        document = self.document(_pdf_bytes(_b_stamp_over_text))
        first = official_evidence._quality_for_page(document, 1)
        (path,) = list(directory.rglob("*.json"))
        key = page_quality_cache._cache_key(dataset_sources.settings.OCR_ENGINE, dataset_sources.settings.OCR_LANG)
        foreign = {"schema": 1, "key": key, "sha256": "other", "page": 1, "result": first}
        for payload in ("{not json", "[]", json.dumps({"schema": 1, "key": "x", "result": first}), json.dumps(foreign)):
            path.write_text(payload, encoding="utf-8")
            official_evidence._QUALITY_PAGE_CACHE.clear()
            self.assertEqual(official_evidence._quality_for_page(document, 1), first)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["result"], first)  # rewritten after the miss

    def test_unwritable_directory_does_not_fail(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        os.environ["CASE10_QUALITY_CACHE_DIR"] = str(blocker / "sub")
        document = self.document(_pdf_bytes(_b_stamp_over_text))
        self.assertEqual(official_evidence._quality_for_page(document, 1)["reasons"], ["STAMP_OVER_TEXT"])

    def test_key_depends_on_ocr_engine_language_and_algorithm_version(self):
        keys = {page_quality_cache._cache_key("tesseract", "rus+eng"), page_quality_cache._cache_key("paddleocr", "rus+eng"),
                page_quality_cache._cache_key("tesseract", "eng")}
        with patch.object(page_quality_cache, "QUALITY_ALGORITHM_VERSION", "other"):
            keys.add(page_quality_cache._cache_key("tesseract", "rus+eng"))
        self.assertEqual(len(keys), 4)


if __name__ == "__main__":
    unittest.main()
