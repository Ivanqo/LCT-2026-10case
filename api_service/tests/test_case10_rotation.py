"""TZ 9.1 requires bbox normalization relative to the visible page area after
CropBox/MediaBox/Rotate. fitz's word/text extraction always returns raw,
unrotated content-stream coordinates regardless of /Rotate, so a page
authored with a nonzero /Rotate needs an explicit transform before
normalizing -- otherwise the normalized bbox matches the raw content stream,
not what a PDF viewer (and therefore a human annotator) actually sees.
These tests build a synthetic PDF with a known word at a known raw bbox,
mark it with /Rotate via pypdf (independent of fitz's own rotation-setting
code path), and verify the extracted, normalized geometry matches the
*visible* (rotated) page instead.
"""
from __future__ import annotations

import hashlib
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from pypdf import PdfReader, PdfWriter
from PIL import Image

from app.domain import dataset_sources

# Ground truth for the raw (unrotated content-stream) word bbox produced by
# `page.insert_text((50, 700), "HELLO", fontsize=24)` on a 600x800 page, as
# returned by fitz's own `get_text("words")` -- confirmed independently
# against MuPDF's pixel-level rendering of the same bytes before writing
# this fix, not derived from the code under test.
_RAW_BBOX = (50.0, 674.2000122070312, 128.69601440429688, 707.176025390625)
_RAW_WIDTH, _RAW_HEIGHT = 600.0, 800.0


def _build_pdf_with_rotation(rotation: int) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=_RAW_WIDTH, height=_RAW_HEIGHT)
    page.insert_text((50, 700), "HELLO", fontsize=24)
    base = doc.tobytes()
    doc.close()

    if rotation % 360 == 0:
        return base

    reader = PdfReader(BytesIO(base))
    writer = PdfWriter()
    for pdf_page in reader.pages:
        pdf_page.rotate(rotation)
        writer.add_page(pdf_page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _document_for(relative: str, pdf_bytes: bytes) -> SimpleNamespace:
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    return SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative}},
        file_hash=sha256,
        content_hash=sha256,
    )


class RotationAwareNormalizationTests(unittest.TestCase):
    def setUp(self):
        for cached in (
            dataset_sources._original_page_snapshots,
            dataset_sources._original_page_geometry,
            dataset_sources._original_page_geometry_visible,
            dataset_sources._ocr_original_clip,
            dataset_sources._ocr_page_words,
            dataset_sources._original_document_bytes,
        ):
            cached.cache_clear()

    def test_rotate_90_normalizes_against_the_visible_swapped_page(self):
        pdf_bytes = _build_pdf_with_rotation(90)
        document = _document_for("rotation_case/rotate_90.pdf", pdf_bytes)

        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes):
            pages = dataset_sources.extract_original_pages(document, [1])

        snapshot = pages[1]
        # A /Rotate 90 page displays with width/height swapped relative to
        # the raw content stream -- 800x600, not 600x800.
        self.assertEqual((snapshot["width"], snapshot["height"]), (_RAW_HEIGHT, _RAW_WIDTH))

        words = {word["text"]: word["bbox"] for word in snapshot["words"]}
        self.assertIn("HELLO", words)
        x1, y1, x2, y2 = words["HELLO"]

        # Expected via the same corner-rotation transform, independently
        # cross-checked by rendering the identical bytes with fitz's own
        # get_pixmap() (native rotation) and confirming the word's visible
        # pixel position lines up (see manual verification in the PR).
        rx1, ry1, rx2, ry2 = _RAW_BBOX
        expected_x1, expected_y1 = _RAW_HEIGHT - ry2, rx1
        expected_x2, expected_y2 = _RAW_HEIGHT - ry1, rx2
        self.assertAlmostEqual(x1, expected_x1, places=3)
        self.assertAlmostEqual(y1, expected_y1, places=3)
        self.assertAlmostEqual(x2, expected_x2, places=3)
        self.assertAlmostEqual(y2, expected_y2, places=3)

        # The bbox must not have been left in the raw frame: raw x2 (128.7)
        # would be nonsensical as a y-coordinate on an 800-tall raw page, but
        # this guards the actual regression -- the raw and visible bboxes
        # must differ for a rotated page.
        self.assertNotEqual([round(v, 1) for v in (x1, y1, x2, y2)], [round(v, 1) for v in _RAW_BBOX])

        # And it must fall inside the visible page bounds.
        self.assertTrue(0 <= x1 < x2 <= snapshot["width"])
        self.assertTrue(0 <= y1 < y2 <= snapshot["height"])

    def test_rotate_0_is_unaffected_raw_equals_visible(self):
        pdf_bytes = _build_pdf_with_rotation(0)
        document = _document_for("rotation_case/rotate_0.pdf", pdf_bytes)

        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes):
            pages = dataset_sources.extract_original_pages(document, [1])

        snapshot = pages[1]
        self.assertEqual((snapshot["width"], snapshot["height"]), (_RAW_WIDTH, _RAW_HEIGHT))
        x1, y1, x2, y2 = {word["text"]: word["bbox"] for word in snapshot["words"]}["HELLO"]
        for actual, expected in zip((x1, y1, x2, y2), _RAW_BBOX):
            self.assertAlmostEqual(actual, expected, places=3)

    def test_rotate_180_reflects_both_axes_dims_unchanged(self):
        pdf_bytes = _build_pdf_with_rotation(180)
        document = _document_for("rotation_case/rotate_180.pdf", pdf_bytes)

        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes):
            pages = dataset_sources.extract_original_pages(document, [1])

        snapshot = pages[1]
        self.assertEqual((snapshot["width"], snapshot["height"]), (_RAW_WIDTH, _RAW_HEIGHT))
        x1, y1, x2, y2 = {word["text"]: word["bbox"] for word in snapshot["words"]}["HELLO"]
        rx1, ry1, rx2, ry2 = _RAW_BBOX
        expected = (_RAW_WIDTH - rx2, _RAW_HEIGHT - ry2, _RAW_WIDTH - rx1, _RAW_HEIGHT - ry1)
        for actual, exp in zip((x1, y1, x2, y2), expected):
            self.assertAlmostEqual(actual, exp, places=3)

    def test_rotate_270_normalizes_against_the_visible_swapped_page(self):
        pdf_bytes = _build_pdf_with_rotation(270)
        document = _document_for("rotation_case/rotate_270.pdf", pdf_bytes)

        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes):
            pages = dataset_sources.extract_original_pages(document, [1])

        snapshot = pages[1]
        self.assertEqual((snapshot["width"], snapshot["height"]), (_RAW_HEIGHT, _RAW_WIDTH))
        x1, y1, x2, y2 = {word["text"]: word["bbox"] for word in snapshot["words"]}["HELLO"]
        rx1, ry1, rx2, ry2 = _RAW_BBOX
        expected = (ry1, _RAW_WIDTH - rx2, ry2, _RAW_WIDTH - rx1)
        for actual, exp in zip((x1, y1, x2, y2), expected):
            self.assertAlmostEqual(actual, exp, places=3)

    def test_render_evidence_page_uses_the_same_visible_orientation(self):
        """The Evidence Viewer endpoint (/case10/evidence-fragments/{id}/page.png)
        must render in the same frame the normalized bbox was computed
        against, or the highlight rectangle an inspector sees will not line
        up with the marked region on any rotated page."""
        pdf_bytes = _build_pdf_with_rotation(90)
        document = _document_for("rotation_case/rotate_90_render.pdf", pdf_bytes)

        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes):
            pages = dataset_sources.extract_original_pages(document, [1])
            bbox = pages[1]["words"][0]["bbox"]
            fragment = SimpleNamespace(page=1, bbox_pdf=bbox)
            png_bytes = dataset_sources.render_evidence_page(document, fragment)

        image = Image.open(BytesIO(png_bytes))
        # Visible frame for /Rotate 90 is landscape (800x600 before scaling);
        # the raw content stream is portrait (600x800). Asserting landscape
        # output pins down that rendering did not silently fall back to the
        # raw, unrotated frame.
        self.assertGreater(image.width, image.height)


if __name__ == "__main__":
    unittest.main()
