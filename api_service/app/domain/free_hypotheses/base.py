"""Free hypothesis search (TZ 9.5): shared shapes, configuration and de-duplication.

A detector turns the documents of one inspection process into `HypothesisCandidate`s. A candidate is a *hypothesis*
about a possible discrepancy that no matrix parameter describes: it carries the discovery method, a ranking score
(NOT a probability of a violation -- no detector here has a verifiable way to compute one), the PD / RD references and
concrete evidence (file, page, box) on each side. The runner (`runner.py`) limits, de-duplicates and stores them.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

METHOD_LOGICAL = "LOGICAL_ANALYSIS"
METHOD_SEMANTIC = "SEMANTIC_DISSONANCE"
METHOD_NORMATIVE = "NORMATIVE_ANALYSIS"
METHOD_ML = "ML_PATTERN_STAT"
METHOD_UNMATCHED = "UNMATCHED_DIFF"
METHOD_CHANGE_SHEET = "CHANGE_SHEET"
METHOD_LAYERS = "LAYER_COMPOSITION"
ALL_METHODS = (METHOD_UNMATCHED, METHOD_CHANGE_SHEET, METHOD_LAYERS, METHOD_SEMANTIC, METHOD_LOGICAL, METHOD_NORMATIVE, METHOD_ML)

# the sentinel the hypotheses hang on: not a matrix parameter, never active (never counted in the 132 / M-0xx matrix)
FREE_SEARCH_PARAM_CODE = "FREE-SEARCH"
FREE_SEARCH_PARAM_NAME = "Гипотеза вне Матрицы (свободный поиск)"
FREE_SEARCH_SECTION = "Свободный поиск гипотез (ТЗ 9.5)"
GROUP_KEY_PREFIX = "hyp:"
MODEL_VERSION = "free-hypotheses-v1"

CRITICALITY_CRITICAL = "КРИТИЧЕСКОЕ"
CRITICALITY_SUBSTANTIAL = "СУЩЕСТВЕННОЕ"
CRITICALITY_MINOR = "НЕЗНАЧИТЕЛЬНОЕ"
_PRIORITY_BY_CRITICALITY = {CRITICALITY_CRITICAL: "HIGH", CRITICALITY_SUBSTANTIAL: "MEDIUM", CRITICALITY_MINOR: "LOW"}
_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def free_hypotheses_enabled() -> bool:
    """Master switch of the new detectors. The three original logical rules (LR-001..003) are not behind it."""
    return _env_flag("CASE10_FREE_HYPOTHESES", DEFAULT_ENABLED)


# Set from the measurement in evaluation/phase12/FINAL_B2_HYPOTHESES_REPORT.md: a detector is on by default only if its
# sampled precision is >= 0.6 and its noise <= 3 hypotheses per 100 pages. UNMATCHED_DIFF and SEMANTIC_DISSONANCE passed;
# NORMATIVE needs admin entries (idle otherwise); the new logic rules (LR-101..106) produced nothing measurable and the
# statistical baseline failed the precision bar, so those two are off until enabled by name (see enabled_methods()).
# Step R added CHANGE_SHEET (sampled precision 7/10, 0.05 hypotheses per 100 pages) and LAYER_COMPOSITION (1/1 on real objects, noise 0.002 per
# 100 pages; n is too small to say more -- see evaluation/phase12/FINAL_R_RECALL_REPORT.md): both on by default.
DEFAULT_ENABLED = "1"


DEFAULT_METHODS = (METHOD_UNMATCHED, METHOD_CHANGE_SHEET, METHOD_LAYERS, METHOD_SEMANTIC, METHOD_NORMATIVE)


def enabled_methods() -> tuple[str, ...]:
    """CASE10_FREE_HYPOTHESES_METHODS (comma-separated) names the set (default: DEFAULT_METHODS); the ML statistical detector
    is an optional baseline (not a trained model) and also runs when CASE10_FREE_HYPOTHESES_ML=1. The new internal-consistency
    logic rules run under LOGICAL_ANALYSIS: CASE10_FREE_HYPOTHESES_METHODS=UNMATCHED_DIFF,SEMANTIC_DISSONANCE,LOGICAL_ANALYSIS,NORMATIVE_ANALYSIS."""
    raw = os.getenv("CASE10_FREE_HYPOTHESES_METHODS", "").strip()
    chosen = {m.strip().upper() for m in raw.split(",") if m.strip()} if raw else set(DEFAULT_METHODS)
    if _env_flag("CASE10_FREE_HYPOTHESES_ML"):
        chosen.add(METHOD_ML)
    return tuple(m for m in ALL_METHODS if m in chosen)


def limit_per_document() -> int:
    return _env_int("CASE10_FREE_HYPOTHESES_MAX_PER_DOCUMENT", 30)


def limit_per_object() -> int:
    return _env_int("CASE10_FREE_HYPOTHESES_MAX_PER_OBJECT", 120)


@dataclass(slots=True)
class EvidenceRef:
    """One cited place: a document page and a box on it (PDF points, as read from the text layer)."""
    document: Any                               # DocumentVersion (or anything with the same attributes)
    page: int
    stage: str                                  # PD | RD | ID
    role: str                                   # expected | actual | context
    bbox_pdf: list[float] | None = None
    page_width: float | None = None
    page_height: float | None = None
    value: str | None = None
    context: str | None = None
    confidence: float | None = None

    def key(self) -> tuple:
        box = tuple(round(v, 1) for v in self.bbox_pdf) if self.bbox_pdf else None
        return (getattr(self.document, "id", None), int(self.page), self.stage, self.role, box)


@dataclass(slots=True)
class HypothesisCandidate:
    method: str
    detector: str                               # sub-detector / rule id, e.g. "unmatched_diff.area", "LR-104"
    dedup_key: str                              # stable, document- and edition-independent identity of the claim
    description: str
    confidence: float                           # ranking score in (0, 1); NOT a probability of a violation
    evidence: list[EvidenceRef]
    criticality: str = CRITICALITY_SUBSTANTIAL
    location: str = ""
    expected: str | None = None                 # the PD side of the claim
    actual: str | None = None                   # the RD / ID side
    normative_base: str | None = None           # only a source that can be checked; None = "не определено"
    details: dict[str, Any] = field(default_factory=dict)
    editions: tuple[int, ...] = ()              # DocumentVersion ids the claim was read from

    @property
    def review_priority(self) -> str:
        return _PRIORITY_BY_CRITICALITY.get(self.criticality, "MEDIUM")

    def sort_key(self) -> tuple:
        return (_PRIORITY_RANK.get(self.review_priority, 1), -float(self.confidence), self.method, self.dedup_key)

    def doc_ids(self) -> list[int]:
        return [int(ref.document.id) for ref in self.evidence if getattr(ref.document, "id", None) is not None]


def norm_token(text: object) -> str:
    """Case-, spacing- and punctuation-insensitive form of a printed label / value (for keys only)."""
    return re.sub(r"[\s.,;:«»\"'()\-–—/№_]+", "", str(text or "").lower().replace("ё", "е"))


def make_dedup_key(object_id: object, method: str, *parts: object) -> str:
    """Identity inside one object: object + method + what is claimed (location, field, the value pair). File ids and
    revisions are not part of it, so the same claim read from comparable editions / byte copies is one hypothesis."""
    raw = "|".join([str(object_id or ""), method, *[norm_token(p) for p in parts]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def group_key_for(candidate: HypothesisCandidate) -> str:
    return f"{GROUP_KEY_PREFIX}{candidate.method.lower()}:{candidate.dedup_key}"


def merge_duplicates(candidates: Iterable[HypothesisCandidate]) -> tuple[list[HypothesisCandidate], int]:
    """TZ 9.5: hypotheses are merged only inside one object (a process is one object) and only when their dedup keys
    coincide, i.e. the same claim; their evidence is united, the best score is kept. Returns (unique, merged_away)."""
    merged: dict[str, HypothesisCandidate] = {}
    dropped = 0
    for candidate in candidates:
        key = group_key_for(candidate)
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        dropped += 1
        seen = {ref.key() for ref in current.evidence}
        current.evidence.extend(ref for ref in candidate.evidence if ref.key() not in seen)
        current.editions = tuple(sorted(set(current.editions) | set(candidate.editions)))
        current.confidence = max(current.confidence, candidate.confidence)
    return list(merged.values()), dropped


def apply_limits(candidates: list[HypothesisCandidate], *, per_document: int, per_object: int) -> tuple[list[HypothesisCandidate], dict[str, int]]:
    """Keep the strongest hypotheses: at most `per_document` per cited RD/ID document (PD for a PD-only claim) and
    `per_object` in all. The cut is deterministic (priority, score, method, key) and reported, never silent."""
    ordered = sorted(candidates, key=lambda c: c.sort_key())
    kept: list[HypothesisCandidate] = []
    by_doc: dict[Any, int] = {}
    cut_document = cut_object = 0
    for candidate in ordered:
        anchor = next((ref for ref in reversed(candidate.evidence) if ref.stage in ("RD", "ID")), None) or (candidate.evidence[0] if candidate.evidence else None)
        doc_key = getattr(anchor.document, "id", None) if anchor else None
        if per_document and doc_key is not None and by_doc.get(doc_key, 0) >= per_document:
            cut_document += 1
            continue
        if per_object and len(kept) >= per_object:
            cut_object += 1
            continue
        kept.append(candidate)
        if doc_key is not None:
            by_doc[doc_key] = by_doc.get(doc_key, 0) + 1
    return kept, {"cut_by_document_limit": cut_document, "cut_by_object_limit": cut_object}


def document_reference(ref: EvidenceRef | None) -> str | None:
    """"file / document code, page" as TZ 9.5 prints a reference ("25-01-АР-ПЭ, л.11, стр.3"); the page is the PDF page."""
    if ref is None:
        return None
    document = ref.document
    name = getattr(document, "document_code", None) or getattr(document, "dataset_file_id", None) or getattr(document, "filename", None)
    bits = [str(name)] if name else []
    if ref.page:
        bits.append(f"стр.{ref.page}")
    return ", ".join(bits) or None
