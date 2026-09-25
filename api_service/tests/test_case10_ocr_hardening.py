"""Coverage for the OCR-fallback hardening pass (see evaluation/
OCR_GOLD_REVIEW_PASS1_REPORT.md): tesseract-failure observability, the broken-ToUnicode
-CMap detector + forced-OCR substitution in `extract_original_pages`, and the Word/Symbol
PUA glyph normalization table. Corruption/clean text fixtures below are drawn from the
real character-level signatures confirmed in that report (control-byte garbage, IPA/Latin
-Extended look-alike Cyrillic, literal `(cid:N)` tokens) and from the calibration sample
described there (0 false positives on 300 random public-corpus PDF_TEXT_LAYER pages) --
not invented arbitrarily.
"""
from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.domain import dataset_sources


class NormalizePuaGlyphsTests(unittest.TestCase):
    def test_bullet_glyph_is_normalized(self):
        # Confirmed by rendering the actual glyph from its embedded "Symbol" font in the
        # public corpus (F0176 p21): 0xF0B7 draws as a bullet, matching the report's
        # primary named case.
        self.assertEqual(dataset_sources._normalize_pua_glyphs(" Данные"), "• Данные")

    def test_dash_glyphs_from_different_font_subsets_both_normalize(self):
        # 0xF02D and 0xF0BE render as the identical dash glyph in two different embedded
        # font subsets in the corpus -- a plain byte-position table would only catch one.
        self.assertEqual(dataset_sources._normalize_pua_glyphs(" «глухих»"), "– «глухих»")
        self.assertEqual(dataset_sources._normalize_pua_glyphs("3 В целях"), "3–– В целях")

    def test_greek_letter_glyphs_used_as_engineering_symbols(self):
        self.assertEqual(dataset_sources._normalize_pua_glyphs("м"), "ρм")  # rho + Cyrillic "м"
        self.assertEqual(dataset_sources._normalize_pua_glyphs(" p"), "Σ Δp")  # Sigma Delta-p

    def test_unmapped_characters_pass_through_unchanged(self):
        text = "обычный текст без PUA-глифов 123"
        self.assertEqual(dataset_sources._normalize_pua_glyphs(text), text)

    def test_empty_string_is_returned_as_is(self):
        self.assertEqual(dataset_sources._normalize_pua_glyphs(""), "")

    def test_bracket_piece_glyphs_are_deliberately_left_unmapped(self):
        # 0xF8EB/0xF8F6 etc. are stacked-bracket *pieces*, not standalone characters --
        # mapping each to "(" would print redundant parens, so they must stay untouched.
        text = ""
        self.assertEqual(dataset_sources._normalize_pua_glyphs(text), text)


class LooksLikeBrokenCmapTextTests(unittest.TestCase):
    def test_literal_cid_tokens_are_flagged(self):
        text = "- (cid:617)(cid:618)(cid:610) применение кабелей с большей пропускной способностью"
        self.assertTrue(dataset_sources._looks_like_broken_cmap_text(text))

    def test_control_byte_garbage_is_flagged(self):
        # Real signature from the report's F0150/F0146 pages: the font's own byte codes
        # leak through as raw C0 control characters instead of real characters.
        text = "\x14\x15 \x14$&\x14 \x15 &\x14 %&$ &\x15 ,%&\x16\x14 \x17 $ \x14\x14 % \x16/ >F 17.02.2023" * 3
        self.assertTrue(dataset_sources._looks_like_broken_cmap_text(text))

    def test_lookalike_cyrillic_via_ipa_extensions_is_flagged(self):
        # Real signature from the report's F0153 page: each Cyrillic letter is
        # substituted 1:1 with an IPA-Extensions look-alike by a broken ToUnicode CMap.
        text = "ɉɪɢɦɟɧɟɧɢɟ ɫɨɜɪɟɦɟɧɧɨɣ ɜɨɞɨɫɛɟɪɟɝɚɸɳɟɣ ɫɚɧɢɬɚɪɧɨ-ɬɟɯɧɢɱɟɫɤɨɣ ɚɪɦɚɬɭɪɵ ɫ ɤɟɪɚɦɢɱɟɫɤɢɦɢ"
        self.assertTrue(dataset_sources._looks_like_broken_cmap_text(text))

    def test_ordinary_clean_cyrillic_text_is_not_flagged(self):
        text = (
            "Проектируемый пристенный дренаж по проекту ООО «Энергоиндустрия», "
            "точка подключения к сети теплоснабжения №Т-УП 1-01-211006/1 от 24.11.2021 г."
        )
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text(text))

    def test_ordinary_clean_ascii_table_text_is_not_flagged(self):
        # Codes/tables mixing digits, Latin letters and dashes are common and legitimate
        # (e.g. "IOS4-078", "01-07/22-14-П-АР1") -- must not trip the detector on their own.
        text = "IOS4-078 KR-055 PZ-009 01-07/22-14-АР1-кор1-ПЗ лист 12 из 45, изм. 3"
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text(text))

    def test_short_text_is_never_flagged_regardless_of_content(self):
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text("(cid:1)"))

    def test_empty_text_is_not_flagged(self):
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text(""))

    def test_occasional_newlines_do_not_count_as_control_characters(self):
        text = "\n".join(["Лист согласования проекта"] * 10)
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text(text))


def _document_for(relative: str, sha256: str = "deadbeef") -> SimpleNamespace:
    return SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative}},
        file_hash=sha256,
        content_hash=sha256,
    )


class ExtractOriginalPagesForcedOcrTests(unittest.TestCase):
    def setUp(self):
        self.document = _document_for("case/forced_ocr.pdf")

    def _patch_snapshots(self, snapshots_by_page: dict[int, dict]):
        return patch.object(
            dataset_sources, "_original_page_snapshots",
            return_value=tuple(snapshots_by_page.values()),
        )

    def test_corrupted_page_is_substituted_with_ocr_result_when_available(self):
        corrupted = {
            "page": 1, "width": 600.0, "height": 800.0,
            "text": "(cid:617)(cid:618)(cid:610)(cid:614)(cid:607) применение кабелей",
            "words": [], "cmap_corrupted": True,
        }
        ocr_result = {"page": 1, "width": 600.0, "height": 800.0, "text": "применение кабелей", "words": [], "source": "ocr_fallback"}
        with self._patch_snapshots({1: corrupted}), \
                patch.object(dataset_sources, "ocr_page_snapshot", return_value=ocr_result) as mocked_ocr:
            pages = dataset_sources.extract_original_pages(self.document, [1])
        mocked_ocr.assert_called_once_with(self.document, 1)
        self.assertEqual(pages[1]["source"], "ocr_fallback")
        self.assertEqual(pages[1]["text"], "применение кабелей")

    def test_corrupted_page_keeps_original_and_is_marked_when_ocr_unavailable(self):
        corrupted = {
            "page": 1, "width": 600.0, "height": 800.0,
            "text": "(cid:617)(cid:618)(cid:610)(cid:614)(cid:607) применение кабелей",
            "words": [], "cmap_corrupted": True,
        }
        with self._patch_snapshots({1: corrupted}), \
                patch.object(dataset_sources, "ocr_page_snapshot", return_value=None):
            pages = dataset_sources.extract_original_pages(self.document, [1])
        # Never silently dropped -- the original (garbled) text is still returned, but
        # flagged so a consumer can tell the difference from a page that was actually clean.
        self.assertTrue(pages[1].get("cmap_corruption_suspected"))
        self.assertIn("(cid:", pages[1]["text"])

    def test_clean_page_never_triggers_an_ocr_call(self):
        clean = {
            "page": 1, "width": 600.0, "height": 800.0,
            "text": "Проектируемый пристенный дренаж по проекту", "words": [], "cmap_corrupted": False,
        }
        with self._patch_snapshots({1: clean}), \
                patch.object(dataset_sources, "ocr_page_snapshot") as mocked_ocr:
            pages = dataset_sources.extract_original_pages(self.document, [1])
        mocked_ocr.assert_not_called()
        self.assertNotIn("cmap_corruption_suspected", pages[1])

    def test_forced_ocr_substitutions_are_capped_per_call(self):
        corrupted_text = "(cid:1)(cid:2)(cid:3)(cid:4)(cid:5)(cid:6)(cid:7)(cid:8)(cid:9)(cid:10)"
        many_pages = {
            page: {
                "page": page, "width": 600.0, "height": 800.0,
                "text": corrupted_text, "words": [], "cmap_corrupted": True,
            }
            for page in range(1, dataset_sources._MOJIBAKE_FORCED_OCR_MAX_PAGES_PER_CALL + 10)
        }
        with self._patch_snapshots(many_pages), \
                patch.object(dataset_sources, "ocr_page_snapshot", return_value=None) as mocked_ocr:
            dataset_sources.extract_original_pages(self.document, list(many_pages.keys()))
        self.assertEqual(mocked_ocr.call_count, dataset_sources._MOJIBAKE_FORCED_OCR_MAX_PAGES_PER_CALL)


class OriginalPageSnapshotsCorruptionFlagTests(unittest.TestCase):
    """Regression coverage for a real bug found while Docker-verifying the corruption
    detector against the actual corpus: fitz's `get_text("words")` tokenizer treats raw
    C0 control-byte glyphs as word-boundary whitespace and drops them, so a page like the
    report's F0150 (genuinely garbled, >3% control bytes in `get_text("text")`) shows
    *zero* control characters once tokenized into words and joined -- silently defeating
    the control-byte signal if corruption detection reads that joined text instead of the
    untokenized page text. `_original_page_snapshots` must compute `cmap_corrupted` from
    `page.get_text("text")` directly, not from the words-joined string it also builds."""

    def test_corruption_flag_uses_plain_text_extraction_not_the_words_join(self):
        control_byte_text = "\x14\x15 \x14$&\x14 \x15 &\x14 %&$ &\x15 ,%&\x16\x14 \x17 $ \x14\x14 % \x16/ >F 17.02.2023" * 3

        class FakePage:
            rotation = 0
            cropbox = SimpleNamespace(width=600.0, height=800.0)

            def get_text(self, mode, sort=False):
                if mode == "text":
                    return control_byte_text
                # The tokenizer silently drops the control bytes -- this is the real,
                # observed fitz behavior this test locks in, not a hypothetical.
                return [(0.0, 0.0, 10.0, 10.0, "clean", 0, 0, 0)]

        class FakePdf:
            def __len__(self):
                return 1

            def __getitem__(self, index):
                return FakePage()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        dataset_sources._original_page_snapshots.cache_clear()
        with patch.object(dataset_sources, "original_document_bytes", return_value=b"%PDF-fake"), \
                patch("fitz.open", return_value=FakePdf()):
            snapshots = dataset_sources._original_page_snapshots("case/control_bytes.pdf", "sha", (1,))
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0]["cmap_corrupted"])
        # And the words-joined text on its own would NOT have tripped the same detector
        # -- confirming this is testing the actual fix, not a redundant assertion.
        self.assertFalse(dataset_sources._looks_like_broken_cmap_text(snapshots[0]["text"]))


def _counter_value(counter, **labels) -> float:
    child = counter.labels(**labels) if labels else counter
    return child._value.get()


class TesseractObservabilityTests(unittest.TestCase):
    def setUp(self):
        dataset_sources._ocr_original_clip.cache_clear()

    def test_missing_binary_is_logged_and_counted_without_raising(self):
        before = _counter_value(dataset_sources.ocr_tesseract_calls_total, outcome="exception:FileNotFoundError")
        with patch.object(dataset_sources, "original_document_bytes") as mocked_bytes, \
                patch("subprocess.run", side_effect=FileNotFoundError("no tesseract")):
            import fitz
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            mocked_bytes.return_value = doc.tobytes()
            doc.close()
            with self.assertLogs(dataset_sources.logger, level="WARNING"):
                result = dataset_sources._ocr_original_clip(
                    "case/missing_binary.pdf", "sha", 1, (0.0, 0.0, 100.0, 100.0), "eng",
                )
        self.assertEqual(result, "")
        after = _counter_value(dataset_sources.ocr_tesseract_calls_total, outcome="exception:FileNotFoundError")
        self.assertEqual(after, before + 1)

    def test_nonzero_exit_falls_through_to_tempfile_attempt_and_is_logged(self):
        import fitz
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        pdf_bytes = doc.tobytes()
        doc.close()

        failing_stdin = SimpleNamespace(returncode=1, stdout=b"", stderr=b"unknown flag --psm")
        with patch.object(dataset_sources, "original_document_bytes", return_value=pdf_bytes), \
                patch("subprocess.run", return_value=failing_stdin) as mocked_run, \
                patch.object(dataset_sources, "_ocr_original_clip_via_tempfile", return_value="") as mocked_tempfile, \
                self.assertLogs(dataset_sources.logger, level="WARNING"):
            result = dataset_sources._ocr_original_clip(
                "case/bad_flags.pdf", "sha", 1, (0.0, 0.0, 100.0, 100.0), "eng",
            )
        mocked_run.assert_called_once()
        mocked_tempfile.assert_called_once()
        self.assertEqual(result, "")


class TesseractHealthCheckTests(unittest.TestCase):
    def test_missing_binary_reports_unavailable(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no tesseract")), \
                self.assertLogs(dataset_sources.logger, level="CRITICAL"):
            result = dataset_sources.check_tesseract_health()
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(dataset_sources.tesseract_health_status()["available"], False)

    def test_version_ok_but_psm_flag_rejected_reports_unavailable(self):
        # Exactly the confirmed real failure mode: Tesseract 3.02 answers `--version`
        # fine and only fails on the actual `--psm` invocation used in production.
        version_ok = SimpleNamespace(returncode=0, stdout=b"tesseract 3.02\n")
        psm_rejected = SimpleNamespace(returncode=1, stdout=b"", stderr=b"Unknown argument: --psm")
        with patch("subprocess.run", side_effect=[version_ok, psm_rejected]), \
                self.assertLogs(dataset_sources.logger, level="CRITICAL"):
            result = dataset_sources.check_tesseract_health()
        self.assertFalse(result["available"])
        self.assertIn("3.02", result["version"])

    def test_working_tesseract_reports_available(self):
        version_ok = SimpleNamespace(returncode=0, stdout=b"tesseract 5.5.0\n")
        psm_ok = SimpleNamespace(returncode=0, stdout=b"HEALTH\n", stderr=b"")
        with patch("subprocess.run", side_effect=[version_ok, psm_ok]):
            result = dataset_sources.check_tesseract_health()
        self.assertTrue(result["available"])
        self.assertEqual(dataset_sources.tesseract_health_status()["available"], True)


if __name__ == "__main__":
    unittest.main()
