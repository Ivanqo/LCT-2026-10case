"""Read originals by manifest path and hash, never annotated answer overlays."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import logging
import os
import re
from io import BytesIO
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from typing import Any
from zipfile import ZipFile
from types import SimpleNamespace

from ..config import settings
from ..metrics import (
    ocr_cmap_corruption_detected_total,
    ocr_tesseract_available,
    ocr_tesseract_calls_total,
)
from .anchor_search import cluster_rows
from .official_dataset import find_dataset_paths

logger = logging.getLogger(__name__)


def _log_ocr_event(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


@lru_cache(maxsize=2)
def _archive_members(path: str, mtime_ns: int) -> tuple[str, ...]:
    with ZipFile(path, metadata_encoding="cp866") as archive:
        return tuple(archive.namelist())


def _visible_page_dims(width: float, height: float, rotation: int) -> tuple[float, float]:
    """Dimensions of the page as a viewer displays it after applying /Rotate
    (TZ 9.1: coordinates are normalized against the visible area, not the raw
    content stream). fitz's word/text extraction always returns raw,
    unrotated coordinates regardless of the page's rotation setting, so the
    90/270 case needs its width and height swapped to match."""
    return (height, width) if int(rotation) % 360 in (90, 270) else (width, height)


def _bbox_to_visible_frame(bbox, width: float, height: float, rotation: int) -> list[float]:
    """Map a bbox from fitz's raw (unrotated content-stream, top-left origin,
    y-down) frame into the frame a PDF viewer shows after applying /Rotate.
    `width`/`height` are the RAW (unrotated) page dimensions the bbox was
    measured against. Corner-transforming and re-deriving min/max (instead of
    hand-picking which coordinate becomes x1 vs x2) keeps this correct for
    all four rotations without special-casing any one of them."""
    rotation = int(rotation) % 360
    x1, y1, x2, y2 = bbox
    corners = [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]
    if rotation == 90:
        points = [(height - y, x) for x, y in corners]
    elif rotation == 180:
        points = [(width - x, height - y) for x, y in corners]
    elif rotation == 270:
        points = [(y, width - x) for x, y in corners]
    else:
        points = corners
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


# Word/Symbol-font Private Use Area codepoints that turn up in this corpus's PDF text
# layer instead of the real character (evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md
# finding #2's second bug: PDF producers embedding a "Symbol"/"SymbolMT" font whose own
# ToUnicode CMap maps each glyph to 0xF000 + its legacy 8-bit Symbol-encoding position,
# instead of the actual Unicode character). Each entry below was found by scanning a
# random 500-page sample of the public corpus's PDF_TEXT_LAYER pages for PUA characters,
# then confirmed by rendering that exact glyph (from its real embedded font, at 6x zoom)
# and reading it visually -- not guessed from a generic Symbol-encoding table, since two
# different codepoints here (0xF02D, 0xF0BE) turned out to render as the same dash glyph
# in different font subsets, which a byte-position table alone would not have caught.
# Deliberately NOT included: 0xF8E5-0xF8F8 (seen in F0172), which are stacked bracket
# *pieces* (top/extender/bottom of a multi-line parenthesis drawn by an equation editor)
# -- normalizing each piece to "(" or ")" would print 2-3 redundant parens per formula,
# which is worse than leaving the PUA codepoint for a human/reviewer to recognize as a
# rendering artifact.
_PUA_GLYPH_NORMALIZATION: dict[int, str] = {
    0xF020: " ",
    0xF028: "(",
    0xF029: ")",
    0xF02B: "+",
    0xF02D: "–",  # EN DASH -- Symbol "minus" glyph used as a list/prefix dash
    0xF03D: "=",
    0xF044: "Δ",  # GREEK CAPITAL LETTER DELTA (engineering "difference", e.g. dP)
    0xF04B: "K",  # Symbol "Kappa" position, used here as a Latin coefficient letter
    0xF053: "Σ",  # GREEK CAPITAL LETTER SIGMA (as a plain letter, e.g. "SUM dP")
    0xF065: "ε",  # GREEK SMALL LETTER EPSILON
    0xF067: "γ",  # GREEK SMALL LETTER GAMMA
    0xF072: "ρ",  # GREEK SMALL LETTER RHO (density, e.g. "rho_M")
    0xF074: "τ",  # GREEK SMALL LETTER TAU
    0xF0A2: "′",  # PRIME (e.g. n', used for a modified variable in a formula)
    0xF0B4: "×",  # MULTIPLICATION SIGN
    0xF0B7: "•",  # BULLET -- the report's primary confirmed case
    0xF0BE: "–",  # EN DASH -- same dash glyph as 0xF02D, different font subset
    0xF0D8: "•",  # arrow-style list bullet, normalized to a plain bullet
    0xF0E5: "∑",  # N-ARY SUMMATION SIGN (distinct from the Sigma *letter* above)
    0xE72E: "□",  # WHITE SQUARE -- CAD "square/rectangular hollow section" marker
    0xE729: "□",  # same square-profile marker, different codepoint variant
}
_PUA_GLYPH_TRANSLATION = str.maketrans({codepoint: value for codepoint, value in _PUA_GLYPH_NORMALIZATION.items()})


def _normalize_pua_glyphs(text: str) -> str:
    return text.translate(_PUA_GLYPH_TRANSLATION) if text else text


# Detects a page whose embedded font has a broken/missing ToUnicode CMap (evaluation/
# OCR_GOLD_REVIEW_PASS1_REPORT.md finding #2, confirmed on real pages F0146/F0150/F0153):
# fitz then extracts either raw C0 control bytes, "look-alike" glyphs from the Latin
# Extended/IPA/Greek Unicode blocks standing in for Cyrillic, or (on other extraction
# tools, defensively covered here too) a literal "(cid:N)" placeholder -- never a clean
# transcription. Thresholds were calibrated against a random 300-page sample of this
# corpus's own PDF_TEXT_LAYER pages the organizer marked `needs_ocr: false` (0 pages
# incorrectly flagged) and confirmed to catch the two structurally distinct real
# corruption patterns found in that same sample (control-byte garbage and look-alike
# Cyrillic). NOT caught by design: a pure ASCII-substitution mojibake variant seen on
# F0146 (e.g. "Содержание" -> "%>45D64=<5") that uses only ordinary printable ASCII --
# statistically indistinguishable from a legitimate alphanumeric code/table without
# language modeling; a known, documented gap rather than a silent one.
_CID_TOKEN_RE = re.compile(r"\(cid:\d+\)")
_MOJIBAKE_MIN_TEXT_LENGTH = 20
_MOJIBAKE_CONTROL_CHAR_RATIO = 0.03
_MOJIBAKE_LOOKALIKE_RATIO = 0.10
_MOJIBAKE_WHITESPACE_CONTROL_CODEPOINTS = frozenset({0x09, 0x0A, 0x0D})
_MOJIBAKE_LOOKALIKE_UNICODE_RANGES = (
    (0x0100, 0x017F),  # Latin Extended-A
    (0x0180, 0x024F),  # Latin Extended-B
    (0x0250, 0x02AF),  # IPA Extensions
    (0x0370, 0x03FF),  # Greek and Coptic
)


def _looks_like_broken_cmap_text(text: str) -> bool:
    if not text or len(text) < _MOJIBAKE_MIN_TEXT_LENGTH:
        return False
    if _CID_TOKEN_RE.search(text):
        return True
    control = sum(
        1 for ch in text if ord(ch) <= 0x1F and ord(ch) not in _MOJIBAKE_WHITESPACE_CONTROL_CODEPOINTS
    )
    if control / len(text) > _MOJIBAKE_CONTROL_CHAR_RATIO:
        return True
    lookalike = sum(
        1 for ch in text
        if any(low <= ord(ch) <= high for low, high in _MOJIBAKE_LOOKALIKE_UNICODE_RANGES)
    )
    return lookalike / len(text) > _MOJIBAKE_LOOKALIKE_RATIO


def original_document_bytes(document) -> bytes:
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("\\", "/")
    expected_hash = document.file_hash or document.content_hash
    return _original_document_bytes(relative, expected_hash)


def document_original_ref(document) -> tuple[str, str] | None:
    """`(relative_path, sha256)` of a dataset document's original PDF straight
    from its imported manifest row, or `None` when there is no readable PDF
    original (no manifest path, a non-PDF file, no recorded hash). The single
    definition of "can this document's pages be read at all" shared by
    `extract_original_pages` and the live tagger's planning phase, so a
    document the latter schedules is exactly one the former can open."""
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("\\", "/")
    sha256 = document.file_hash or document.content_hash
    if not relative.lower().endswith(".pdf") or not sha256:
        return None
    return relative, str(sha256)


@lru_cache(maxsize=4)
def _original_document_bytes(relative: str, expected_hash: str) -> bytes:
    parts = PurePosixPath(relative).parts
    if not relative or ".." in parts or relative.startswith("/") or ":" in relative:
        raise ValueError("Invalid manifest source path")
    # Explicit alternative root for a corpus extracted outside the mounted
    # participant package (evaluation harnesses over the SILVER new objects,
    # tests). Read from the ENVIRONMENT at call time, not from `settings`, so
    # it is inherited by the live tagger's spawned worker processes -- an
    # in-process monkeypatch of `original_document_bytes` is invisible to them.
    # SHA-256 is verified exactly as for the mounted package.
    override_root = os.environ.get("CASE10_ORIGINALS_ROOT", "").strip()
    if override_root:
        root = Path(override_root).resolve()
        source = (root / relative).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Source is outside participant package")
        if source.is_file():
            data = source.read_bytes()
            if not expected_hash or hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError("Original document SHA-256 does not match manifest")
            return data
        # A service can read a freshly uploaded package from CASE10_ORIGINALS_ROOT while its inspector demo also
        # contains seeded corpus documents. Let those documents continue through the normal mounted-dataset path.
    manifest = find_dataset_paths().get("document_manifest")
    if not manifest:
        raise FileNotFoundError("Participant document manifest is not mounted")
    package = manifest.parents[2]
    source = (package / "01_ДОКУМЕНТАЦИЯ" / relative).resolve()
    if not source.is_relative_to(package.resolve()):
        raise ValueError("Source is outside participant package")
    if source.is_file():
        data = source.read_bytes()
    else:
        case_root = next((parent for parent in manifest.parents if parent.name == "case_data"), None)
        archives = sorted(case_root.glob("01_*.zip")) if case_root else []
        data = None
        for archive_path in archives:
            names = _archive_members(str(archive_path), archive_path.stat().st_mtime_ns)
            matches = [name for name in names if name.endswith("/01_ДОКУМЕНТАЦИЯ/" + relative)]
            if len(matches) == 1:
                with ZipFile(archive_path, metadata_encoding="cp866") as archive:
                    data = archive.read(matches[0])
                break
        if data is None:
            raise FileNotFoundError("Original document is unavailable in participant package")
    if not expected_hash or hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("Original document SHA-256 does not match manifest")
    return data


@lru_cache(maxsize=32)
def _original_page_snapshots(relative: str, sha256: str, page_numbers: tuple[int, ...]) -> tuple[dict, ...]:
    import fitz

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative}},
        file_hash=sha256,
        content_hash=sha256,
    )
    snapshots = []
    with fitz.open(stream=original_document_bytes(document), filetype="pdf") as pdf:
        for page_number in page_numbers:
            if page_number < 1 or page_number > len(pdf):
                continue
            page = pdf[page_number - 1]
            rotation = int(page.rotation)
            raw_width, raw_height = float(page.cropbox.width), float(page.cropbox.height)
            width, height = _visible_page_dims(raw_width, raw_height, rotation)
            words = [
                {
                    "bbox": _bbox_to_visible_frame(
                        [float(word[0]), float(word[1]), float(word[2]), float(word[3])],
                        raw_width, raw_height, rotation,
                    ),
                    "text": _normalize_pua_glyphs(str(word[4])),
                }
                for word in page.get_text("words", sort=True)
                if len(word) >= 5 and str(word[4]).strip()
            ]
            if rotation % 360 != 0:
                # `sort=True` orders words in fitz's own RAW (unrotated
                # content-stream) frame -- correct reading order for a
                # 0deg page, but scrambled for a rotated one: a table's
                # label/value cells end up nowhere near each other in the
                # flat list even though their (now-corrected) visible bboxes
                # are right. Verified forensically on a real 90deg-rotated
                # LOS3A RD sheet: literal anchor-phrase matching
                # (`find_anchor_end_word_index`, which assumes its anchor's
                # words are adjacent in this list) found 0/132 matrix
                # parameters pre-fix and 5/132 post-fix on the same page,
                # including the literal ТЭП-table row this was built to
                # find. Re-clustering by the already-corrected visible bbox
                # (same Y/X banding `cluster_rows` uses for row
                # fingerprinting/semantic scoring elsewhere in this module
                # family) restores true reading order for any rotation;
                # left untouched for the far more common 0deg case, where
                # fitz's own order is already correct and already covered
                # by existing tests/fixtures.
                words = [word for row in cluster_rows(words) for word in row]
            # Corruption detection reads the plain-text extraction, not the words-joined
            # text above: fitz's "words" tokenizer treats raw C0 control-byte glyphs
            # (one of the two confirmed broken-CMap signatures -- see
            # `_looks_like_broken_cmap_text`) as word-boundary whitespace and silently
            # drops them, so a page like F0150 that is genuinely garbled shows *zero*
            # control characters once tokenized into words even though `get_text("text")`
            # on the same page has them at >3% density. Checking the untokenized text
            # instead of re-deriving it from `words` is what actually catches that case.
            cmap_corrupted = _looks_like_broken_cmap_text(page.get_text("text"))
            snapshots.append({
                "page": page_number,
                "width": width,
                "height": height,
                "text": " ".join(word["text"] for word in words),
                "words": words,
                "cmap_corrupted": cmap_corrupted,
            })
    return tuple(snapshots)


# Bounds how many pages a single `extract_original_pages()` call will force through full
# -page OCR for corruption, independent of the (much larger) fallback-discovery OCR
# budget in official_rule_packs.py -- callers here (e.g. the up-front IOS4-078/079 scan)
# can request up to ~220 pages of one document in one call, and a document with
# systemic font corruption across most of its pages should not turn one such call into
# dozens of uncapped tesseract invocations.
_MOJIBAKE_FORCED_OCR_MAX_PAGES_PER_CALL = 20


def document_page_count_hint(document, default: int = 60) -> int:
    """Approximate page count straight from the already-imported manifest
    row (`pdf_pages`/`source_page_count`) -- zero-cost (no fitz open) way to
    bound a page-range scan before any snapshot has been rendered. Shared by
    the rule-pack bounded fallback discovery and the live candidate tagger,
    both of which need to pick a page range to scan structurally, without a
    pre-existing tag to locate one."""
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    try:
        return int(row.get("pdf_pages") or row.get("source_page_count") or default)
    except (TypeError, ValueError):
        return default


def _mark_ocr_page_low_quality(snapshot: dict, reason: str) -> None:
    snapshot["cmap_corruption_suspected"] = True
    snapshot["quality_status"] = "LOW_QUALITY"
    snapshot["page_quality"] = "LOW_QUALITY"
    reasons = set(snapshot.get("quality_reasons") or [])
    reasons.add(reason)
    snapshot["quality_reasons"] = sorted(reasons)


def extract_original_pages(document, page_numbers) -> dict[int, dict]:
    ref = document_original_ref(document)
    pages = tuple(sorted({int(page) for page in page_numbers if page and int(page) > 0}))
    if ref is None or not pages:
        return {}
    relative, sha256 = ref
    snapshots = {row["page"]: row for row in _original_page_snapshots(relative, sha256, pages)}
    forced_ocr_budget = _MOJIBAKE_FORCED_OCR_MAX_PAGES_PER_CALL
    for page_number, snapshot in sorted(snapshots.items()):
        if forced_ocr_budget <= 0:
            for skipped_page, skipped in sorted(snapshots.items()):
                if skipped_page >= page_number and skipped.get("cmap_corrupted"):
                    _mark_ocr_page_low_quality(skipped, "BROKEN_TEXT_LAYER_OCR_PAGE_BUDGET_EXCEEDED")
            break
        if not snapshot.get("cmap_corrupted"):
            continue
        forced_ocr_budget -= 1
        ocr_snapshot = ocr_page_snapshot(document, page_number)
        if ocr_snapshot:
            ocr_cmap_corruption_detected_total.labels(resolution="forced_ocr_substituted").inc()
            _log_ocr_event(
                logging.WARNING, "cmap_corruption_forced_ocr",
                relative=relative, page=page_number,
            )
            snapshots[page_number] = ocr_snapshot
        else:
            ocr_cmap_corruption_detected_total.labels(resolution="ocr_fallback_unavailable").inc()
            _log_ocr_event(
                logging.ERROR, "cmap_corruption_ocr_fallback_unavailable",
                relative=relative, page=page_number,
            )
            _mark_ocr_page_low_quality(snapshot, "BROKEN_TEXT_LAYER_OCR_UNAVAILABLE")
    return snapshots


@lru_cache(maxsize=256)
def _ocr_original_clip(relative: str, sha256: str, page_number: int, clip_values: tuple[float, ...], lang: str) -> str:
    import fitz
    from PIL import Image

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative}},
        file_hash=sha256,
        content_hash=sha256,
    )
    with fitz.open(stream=original_document_bytes(document), filetype="pdf") as pdf:
        if page_number < 1 or page_number > len(pdf):
            return ""
        page = pdf[page_number - 1]
        clip = fitz.Rect(clip_values) & page.rect
        if clip.is_empty:
            return ""
        zoom = ocr_zoom_for_page_size(clip.width, clip.height)
        png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False, annots=False).tobytes("png")
    image = Image.open(BytesIO(png)).convert("L")
    image = image.point(lambda value: 0 if value < 190 else 255)
    prepared = BytesIO()
    image.save(prepared, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", lang, "--psm", "11"],
            input=prepared.getvalue(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_TESSERACT_EMERGENCY_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        ocr_tesseract_calls_total.labels(outcome=f"exception:{type(exc).__name__}").inc()
        _log_ocr_event(logging.WARNING, "ocr_clip_stdin_invocation_failed", lang=lang, error=repr(exc))
        return ""
    if result.returncode == 0 and result.stdout:
        ocr_tesseract_calls_total.labels(outcome="success").inc()
        return result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        ocr_tesseract_calls_total.labels(outcome="nonzero_exit").inc()
        _log_ocr_event(
            logging.WARNING, "ocr_clip_stdin_invocation_failed", lang=lang,
            returncode=result.returncode, stderr=result.stderr.decode("utf-8", errors="replace")[:500],
        )
    return _ocr_original_clip_via_tempfile(prepared.getvalue(), lang)


def _ocr_original_clip_via_tempfile(png: bytes, lang: str) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="case10_ocr_") as tmp_dir:
            image_path = Path(tmp_dir) / "clip.png"
            output_base = Path(tmp_dir) / "clip"
            image_path.write_bytes(png)
            result = subprocess.run(
                ["tesseract", str(image_path), str(output_base), "-l", lang, "--psm", "11"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_TESSERACT_EMERGENCY_TIMEOUT_SECONDS,
                check=False,
            )
            output_path = output_base.with_suffix(".txt")
            if result.returncode == 0 and output_path.exists():
                text = output_path.read_text(encoding="utf-8", errors="replace")
                ocr_tesseract_calls_total.labels(outcome="success" if text.strip() else "empty_success").inc()
                return text
            ocr_tesseract_calls_total.labels(outcome="nonzero_exit").inc()
            _log_ocr_event(
                logging.WARNING, "ocr_clip_tempfile_invocation_failed", lang=lang,
                returncode=result.returncode, stderr=result.stderr.decode("utf-8", errors="replace")[:500],
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        ocr_tesseract_calls_total.labels(outcome=f"exception:{type(exc).__name__}").inc()
        _log_ocr_event(logging.WARNING, "ocr_clip_tempfile_invocation_failed", lang=lang, error=repr(exc))
        return ""
    return ""


def ocr_original_clip(document, page_number: int, bbox_pdf, *, lang: str | None = None) -> str:
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("\\", "/")
    sha256 = document.file_hash or document.content_hash
    if not relative.lower().endswith(".pdf") or not sha256 or not bbox_pdf or len(bbox_pdf) != 4:
        return ""
    clip = tuple(round(float(value), 2) for value in bbox_pdf)
    return _ocr_original_clip(relative, sha256, int(page_number), clip, lang or settings.OCR_LANG)


_OCR_PAGE_ZOOM_MAX = 3.0
_OCR_PAGE_MAX_PIXELS_SIDE = 3500.0
_OCR_PAGE_MAX_PIXELS_AREA = _OCR_PAGE_MAX_PIXELS_SIDE * _OCR_PAGE_MAX_PIXELS_SIDE
_TESSERACT_EMERGENCY_TIMEOUT_SECONDS = 600


def ocr_zoom_for_page_size(width: float, height: float) -> float:
    """Deterministic OCR zoom bounded by both raster side and total pixel area."""
    width, height = max(float(width), 1.0), max(float(height), 1.0)
    longest_side = max(width, height)
    area = width * height
    return max(
        0.001,
        min(
            _OCR_PAGE_ZOOM_MAX,
            _OCR_PAGE_MAX_PIXELS_SIDE / longest_side,
            (_OCR_PAGE_MAX_PIXELS_AREA / area) ** 0.5,
        ),
    )


@lru_cache(maxsize=128)
def _ocr_page_words(relative: str, sha256: str, page_number: int, lang: str) -> tuple[dict, ...] | None:
    """Full-page OCR fallback used only when the text layer has no match. Returns
    word boxes already converted to PDF point space (same frame as text-layer
    `page.get_text('words')`), never a guessed or synthetic bbox."""
    import fitz
    from PIL import Image

    document = SimpleNamespace(
        dataset_metadata={"document_manifest": {"relative_path": relative}},
        file_hash=sha256,
        content_hash=sha256,
    )
    with fitz.open(stream=original_document_bytes(document), filetype="pdf") as pdf:
        if page_number < 1 or page_number > len(pdf):
            return ()
        page = pdf[page_number - 1]
        # Large-format drawing sheets (A1/A0) at a fixed zoom become 40+ megapixel
        # images that make tesseract time out; cap the rendered side instead of
        # using one zoom for every page size.
        zoom = ocr_zoom_for_page_size(page.rect.width, page.rect.height)
        png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=False).tobytes("png")
    image = Image.open(BytesIO(png)).convert("L")
    pixel_words = _ocr_words_pixel_space(image, lang)
    if pixel_words is None:
        return None
    words = []
    for word in pixel_words:
        px_left, px_top, px_right, px_bottom = word["bbox"]
        words.append({
            "text": word["text"],
            "bbox": [
                round(px_left / zoom, 3),
                round(px_top / zoom, 3),
                round(px_right / zoom, 3),
                round(px_bottom / zoom, 3),
            ],
            "confidence": word.get("confidence"),
        })
    return tuple(words)


def _ocr_words_pixel_space(image, lang: str) -> list[dict] | None:
    """Dispatches to the configured OCR engine (`settings.OCR_ENGINE`), returning word
    boxes in the RAW PIXEL space of `image` -- `_ocr_page_words` alone knows the zoom
    factor needed to convert those into PDF points, so that conversion stays there and
    this function is engine-agnostic. `CASE10_OCR_ENGINE=paddleocr` falling back to
    tesseract on a load/inference failure is a loud, logged event (never a silent
    accuracy drop) -- same "never degrade silently" rule as the tesseract health check
    itself."""
    if settings.OCR_ENGINE == "paddleocr":
        from .ocr_paddle import recognize_words

        words = recognize_words(image, settings.OCR_PADDLE_LANG)
        if words is not None:
            return words
        _log_ocr_event(
            logging.WARNING, "ocr_paddleocr_unavailable_falling_back_to_tesseract",
            paddle_lang=settings.OCR_PADDLE_LANG, tesseract_lang=lang,
        )
    return _run_tesseract_words(image, lang)


def _run_tesseract_words(image, lang: str) -> list[dict] | None:
    tsv_text = _run_tesseract_tsv(image, lang)
    if tsv_text is None:
        return None
    if not tsv_text:
        return []
    words = []
    for line in tsv_text.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) < 12:
            continue
        level, _page, _block, _par, _line, _word, left, top, width, height, conf, text = columns[:12]
        text = text.strip()
        if level != "5" or not text:
            continue
        try:
            conf_value = float(conf)
        except ValueError:
            conf_value = -1.0
        if conf_value < 0:
            continue
        try:
            px_left, px_top, px_width, px_height = float(left), float(top), float(width), float(height)
        except ValueError:
            continue
        words.append({
            "text": text,
            "bbox": [px_left, px_top, px_left + px_width, px_top + px_height],
            "confidence": conf_value,
        })
    return words


def _run_tesseract_tsv(image, lang: str) -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix="case10_ocr_page_") as tmp_dir:
            image_path = Path(tmp_dir) / "page.png"
            output_base = Path(tmp_dir) / "page"
            image.save(image_path, format="PNG")
            result = subprocess.run(
                ["tesseract", str(image_path), str(output_base), "-l", lang, "--psm", "11", "tsv"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_TESSERACT_EMERGENCY_TIMEOUT_SECONDS,
                check=False,
            )
            output_path = output_base.with_suffix(".tsv")
            if result.returncode == 0 and output_path.exists():
                text = output_path.read_text(encoding="utf-8", errors="replace")
                ocr_tesseract_calls_total.labels(outcome="success" if text.strip() else "empty_success").inc()
                return text
            ocr_tesseract_calls_total.labels(outcome="nonzero_exit").inc()
            _log_ocr_event(
                logging.WARNING, "ocr_page_tsv_invocation_failed", lang=lang,
                returncode=result.returncode, stderr=result.stderr.decode("utf-8", errors="replace")[:500],
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        ocr_tesseract_calls_total.labels(outcome=f"exception:{type(exc).__name__}").inc()
        _log_ocr_event(logging.WARNING, "ocr_page_tsv_invocation_failed", lang=lang, error=repr(exc))
        return None
    return None


def ocr_page_snapshot(document, page_number: int, *, lang: str | None = None) -> dict | None:
    """Build a text-layer-shaped snapshot (page/width/height/text/words) from a
    full-page OCR pass, so existing regex extractors can run against it unchanged.
    Returns None (never a fabricated snapshot) when OCR finds nothing usable."""
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("\\", "/")
    sha256 = document.file_hash or document.content_hash
    if not relative.lower().endswith(".pdf") or not sha256:
        return None
    words = _ocr_page_words(relative, sha256, int(page_number), lang or settings.OCR_LANG)
    if not words:
        return None
    geometries = _original_page_geometry_visible(relative, sha256)
    index = int(page_number) - 1
    if index < 0 or index >= len(geometries):
        return None
    width, height = geometries[index]
    return {
        "page": int(page_number),
        "width": width,
        "height": height,
        "text": " ".join(word["text"] for word in words),
        "words": [{"bbox": word["bbox"], "text": word["text"]} for word in words],
        "source": "ocr_fallback",
    }


_TESSERACT_HEALTH_CHECK_PNG: bytes | None = None
_tesseract_health_cache: dict[str, Any] = {"available": None, "version": None, "error": "never checked"}


def _tesseract_health_check_image() -> bytes:
    """A tiny synthetic image with real text, built once, used only to prove the
    installed tesseract binary accepts the exact `--psm` (double-dash) flag syntax
    `dataset_sources.py` calls it with elsewhere in this module. `tesseract --version`
    alone does not catch this: Tesseract 3.02 (confirmed broken in the audit environment
    that produced evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md) reports a version
    successfully and only fails on the real OCR call, which the old code silently
    swallowed (`except (FileNotFoundError, subprocess.TimeoutExpired): return ""`)."""
    global _TESSERACT_HEALTH_CHECK_PNG
    if _TESSERACT_HEALTH_CHECK_PNG is None:
        from PIL import Image, ImageDraw

        image = Image.new("L", (200, 60), 255)
        ImageDraw.Draw(image).text((10, 10), "HEALTH", fill=0)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        _TESSERACT_HEALTH_CHECK_PNG = buffer.getvalue()
    return _TESSERACT_HEALTH_CHECK_PNG


def check_tesseract_health() -> dict[str, Any]:
    """Startup/ops health check for the OCR fallback: actually invokes tesseract with
    the same `--psm` flag production calls use, and caches the result for
    `tesseract_health_status()` (e.g. the `/health` endpoint) to read without spawning a
    subprocess on every poll. Never raises -- the rest of the API does not depend on the
    OCR fallback, so a broken tesseract is loud (CRITICAL log, `ocr_tesseract_available`
    gauge set to 0) but not fatal to startup."""
    result: dict[str, Any] = {"available": False, "version": None, "error": None}
    try:
        version_proc = subprocess.run(
            ["tesseract", "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10, check=False,
        )
        result["version"] = (
            version_proc.stdout.decode("utf-8", errors="replace").splitlines()[0]
            if version_proc.stdout else None
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result["error"] = f"tesseract binary not runnable: {exc!r}"
        ocr_tesseract_available.set(0)
        _log_ocr_event(logging.CRITICAL, "ocr_tesseract_health_check_failed", **result)
        _tesseract_health_cache.update(result)
        return result

    try:
        probe = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "11"],
            input=_tesseract_health_check_image(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result["error"] = f"--psm probe call failed: {exc!r}"
        ocr_tesseract_available.set(0)
        _log_ocr_event(logging.CRITICAL, "ocr_tesseract_health_check_failed", **result)
        _tesseract_health_cache.update(result)
        return result

    if probe.returncode != 0:
        result["error"] = (
            f"--psm probe exited {probe.returncode}: "
            f"{probe.stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
        ocr_tesseract_available.set(0)
        _log_ocr_event(logging.CRITICAL, "ocr_tesseract_health_check_failed", **result)
        _tesseract_health_cache.update(result)
        return result

    result["available"] = True
    ocr_tesseract_available.set(1)
    _log_ocr_event(logging.INFO, "ocr_tesseract_health_check_ok", **result)
    _tesseract_health_cache.update(result)
    return result


def tesseract_health_status() -> dict[str, Any]:
    """Last-known result of `check_tesseract_health()` (run at API startup), for cheap
    reuse by e.g. the `/health` endpoint -- reading this never spawns a subprocess."""
    return dict(_tesseract_health_cache)


def render_evidence_page(document, fragment) -> bytes:
    import fitz
    from PIL import Image, ImageDraw

    data = original_document_bytes(document)
    with fitz.open(stream=data, filetype="pdf") as pdf:
        page_number = int(fragment.page or 0)
        if page_number < 1 or page_number > len(pdf):
            raise ValueError("Evidence page is outside original document")
        page = pdf[page_number - 1]
        scale = min(2.0, 1800 / max(page.rect.width, page.rect.height))
        png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=False).tobytes("png")

    bbox = fragment.bbox_pdf
    if not bbox or len(bbox) != 4:
        return png
    # `bbox` is already in the visible (post-/Rotate) frame -- the same frame
    # `page.rect`/the pixmap above use since rotation is left native. Drawing
    # on the raster (instead of `page.draw_rect` + rotated render) avoids
    # relying on fitz's shape-vs-rotation coordinate conventions, which live
    # in the raw content-stream frame and would double-rotate an already
    # visible-frame box.
    image = Image.open(BytesIO(png)).convert("RGB")
    x1, y1, x2, y2 = (value * scale for value in bbox)
    ImageDraw.Draw(image).rectangle([x1, y1, x2, y2], outline=(230, 31, 26), width=2)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@lru_cache(maxsize=256)
def _original_page_geometry(relative: str, sha256: str) -> tuple[tuple[float, float], ...]:
    import fitz

    document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": relative}}, file_hash=sha256, content_hash=sha256)
    with fitz.open(stream=original_document_bytes(document), filetype="pdf") as pdf:
        return tuple((page.cropbox.width, page.cropbox.height) for page in pdf)


@lru_cache(maxsize=256)
def _original_page_geometry_visible(relative: str, sha256: str) -> tuple[tuple[float, float], ...]:
    """Same as `_original_page_geometry` but swaps width/height for a 90/270
    rotated page, matching the visible-frame convention `_ocr_page_words`
    renders in (native rotation) and `_original_page_snapshots` normalizes
    against -- kept separate from `_original_page_geometry` because that one
    also backs `normalized_original_geometry`'s fallback for externally
    supplied fragments whose bbox convention this fix does not assume."""
    import fitz

    document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": relative}}, file_hash=sha256, content_hash=sha256)
    with fitz.open(stream=original_document_bytes(document), filetype="pdf") as pdf:
        return tuple(
            _visible_page_dims(float(page.cropbox.width), float(page.cropbox.height), int(page.rotation))
            for page in pdf
        )


def normalized_original_geometry(document, fragment):
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = row.get("relative_path") or row.get("source_relative_path")
    if not relative or not str(relative).lower().endswith(".pdf") or not fragment.bbox_pdf or not fragment.page:
        return None
    x1, y1, x2, y2 = fragment.bbox_pdf
    geometries = []
    if fragment.page_width and fragment.page_height:
        geometries = [(fragment.page_width, fragment.page_height), (fragment.page_height, fragment.page_width)]
    for width, height in geometries:
        if 0 <= x1 < x2 <= width + 1 and 0 <= y1 < y2 <= height + 1:
            return [x1 / width, y1 / height, min(1, x2 / width), min(1, y2 / height)], width, height
    try:
        pages = _original_page_geometry(relative, document.file_hash or document.content_hash)
    except (FileNotFoundError, ValueError):
        return None
    if fragment.page < 1 or fragment.page > len(pages):
        return None
    width, height = pages[fragment.page - 1]
    if 0 <= x1 < x2 <= width + 1 and 0 <= y1 < y2 <= height + 1:
        return [x1 / width, y1 / height, min(1, x2 / width), min(1, y2 / height)], width, height
    return None
