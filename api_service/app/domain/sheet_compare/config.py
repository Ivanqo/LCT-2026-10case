"""Switches and conventions of the sheet comparison ("Сравнение листов", ТЗ 9.1 CV part, experts' reference МГСУ.pdf pp. 6, 8-11).

Everything here is a viewing aid: it never states a violation. Three numbers are always kept apart:
  * `pair_confidence`   how sure the pairing of the two sheets is (title / rooms / labels / floor evidence), NOT a similarity;
  * `similarity`        the share of drawn strokes that coincide after alignment (a picture measure), NOT a deviation;
  * the deviation       is not computed here at all (no value is read from the raster).
"""
from __future__ import annotations

import os

MODEL_VERSION = "sheet-compare-v1"


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def sheet_compare_enabled() -> bool:
    """Master switch of the viewing mode (API routes + workbench tab). Off by default until the acceptance gate is passed."""
    return _flag("CASE10_SHEET_COMPARE_ENABLED", DEFAULT_ENABLED)


def graphic_diff_enabled() -> bool:
    """GRAPHIC_DIFF hypotheses additionally need the method to be named in CASE10_FREE_HYPOTHESES_METHODS."""
    return sheet_compare_enabled() or _flag("CASE10_GRAPHIC_DIFF_FORCE")


DEFAULT_ENABLED = "0"

# pairing: a pair is offered to the inspector from this confidence (a convention, not fitted to a labelled example)
MIN_PAIR_CONFIDENCE = _float("CASE10_SHEET_PAIR_MIN_CONFIDENCE", 0.55)
# the best candidate must lead the runner-up by this margin or the pair is flagged ambiguous
AMBIGUITY_MARGIN = _float("CASE10_SHEET_PAIR_AMBIGUITY_MARGIN", 0.06)

# raster comparison
MAX_RENDER_SIDE = _int("CASE10_SHEET_COMPARE_MAX_SIDE", 1800)      # px, long side of the working image
MAX_DPI = 150.0
MIN_DPI = 40.0
TOLERANCE_MM = _float("CASE10_SHEET_COMPARE_TOLERANCE_MM", 1.2)    # strokes closer than this count as coinciding
MIN_REGION_MM2 = _float("CASE10_SHEET_COMPARE_MIN_REGION_MM2", 120.0)
MAX_REGIONS = _int("CASE10_SHEET_COMPARE_MAX_REGIONS", 40)
ASPECT_TOLERANCE = 0.12                                             # sheets of other formats are not rastered together
MIN_ALIGNMENT_QUALITY = 0.35

# GRAPHIC_DIFF
GRAPHIC_PAIR_CONFIDENCE = _float("CASE10_GRAPHIC_DIFF_MIN_PAIR_CONFIDENCE", 0.75)
GRAPHIC_MAX_DOCUMENT_PAIRS = _int("CASE10_GRAPHIC_DIFF_MAX_DOCUMENT_PAIRS", 40)
GRAPHIC_MAX_PAIRS = _int("CASE10_GRAPHIC_DIFF_MAX_PAIRS", 40)
GRAPHIC_MAX_PER_DOCUMENT = _int("CASE10_GRAPHIC_DIFF_MAX_PER_DOCUMENT", 5)
GRAPHIC_MAX_PER_PAIR = _int("CASE10_GRAPHIC_DIFF_MAX_PER_PAIR", 2)
GRAPHIC_MIN_REGION_MM2 = _float("CASE10_GRAPHIC_DIFF_MIN_REGION_MM2", 900.0)
GRAPHIC_MAX_SIMILARITY = _float("CASE10_GRAPHIC_DIFF_MAX_SIMILARITY", 0.97)   # an almost identical sheet is not worth a hypothesis
