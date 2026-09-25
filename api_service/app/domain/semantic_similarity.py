"""Optional sentence-embedding layer for the generic anchor-search family
(TZ 9.1, item 2: "поиск ... с использованием регулярных выражений и
семантических якорей. Используется модель Sentence-BERT (all-MiniLM-L6-v2)
или её совместимые аналоги"). Sits entirely ON TOP of the existing
deterministic anchor matching in `anchor_search.py` -- never replaces it, and
degrades to a silent no-op (returning `None`) whenever the model cannot be
loaded (missing `sentence-transformers`/`torch`, no cached weights, disabled
by config), so every caller's pre-existing behaviour is unchanged when this
module is unavailable.

**Model choice, empirically verified rather than assumed** (see
CASE10_MATRIX_132_COVERAGE.md for the full comparison): the literal
`all-MiniLM-L6-v2` named in the TZ is English-trained and fails to reliably
separate two different-but-lexically-related Russian construction
parameters on real project text (measured 0.53 vs 0.49 cosine similarity for
a genuine match vs. a different area parameter -- a 0.04 margin, not usable
as a filter). `paraphrase-multilingual-MiniLM-L12-v2` -- already the
production embedding model in this same repo's `rag_service` for the same
Russian-language document corpus -- separates the same real pair by 0.62 vs.
0.41-0.49, a much safer margin, and is what this module actually uses as the
"compatible analogue" the TZ explicitly allows. Trade-off taken deliberately:
~460MB on disk vs. the TZ's informal "~80MB", because a same-size model that
cannot tell two Russian phrases apart would not close the actual gap this
task exists to close.

Both use points from the task brief are implemented as callers of this
module, not here: (a) `anchor_search.find_anchor_end_index_for_phrase`'s
semantic fallback (row-level match when literal/qualifier-tolerant anchor
text is not found at all), and (b) `cross_stage_localization.
pick_best_candidate`'s semantic re-ranking/filtering of already-rendered
candidates (row-text vs. the parameter's own name + catalog source hint).
"""
from __future__ import annotations

from functools import lru_cache
import os
from typing import Any

SEMANTIC_MODEL_NAME = os.getenv(
    "CASE10_SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
# Empirically derived (see module docstring / CASE10_MATRIX_132_COVERAGE.md):
# real genuine-match row text scored >=0.62 against its own parameter's query
# text; the real "Стена в грунте" false-positive excerpt (checkpoint 40)
# scored <=0.41 against every real parameter query tried, and unrelated
# matrix parameters cross-scored up to 0.41 against each other. 0.45 sits in
# the gap with margin on both sides.
SEMANTIC_MIN_SIMILARITY = float(os.getenv("CASE10_SEMANTIC_MIN_SIMILARITY", "0.45"))
_ENABLED = os.getenv("CASE10_SEMANTIC_SIMILARITY_ENABLED", "1").strip().lower() not in ("0", "false", "no")

# Two independent gates, not one, because the two use points this module has
# have opposite risk profiles. Re-ranking/filtering (`cross_stage_
# localization.pick_best_candidate`) only ever narrows a candidate pool a
# TEXTUAL anchor/regex match already independently corroborated -- it can
# demote or drop a candidate, never invent one, so it is safe to default on.
# The anchor-location fallback (`anchor_search.semantic_anchor_fallback_
# index`) can INVENT a match purely from embedding similarity where no
# textual anchor exists at all -- real regression testing (see
# `test_case10_generic_extraction.py`'s `..._does_not_collide_with_a_
# shorter_look_alike_label`/`..._does_not_revive_the_site_table_collision`)
# caught it confusing two DIFFERENT ТЭП tables' own "Площадь" rows (a
# building-area parameter vs. a site-area parameter on an unrelated table):
# both are genuinely close in embedding space because they share most of
# their vocabulary by construction, not because they are the same fact. That
# is a real, reproducible precision cost, not a tuning artifact, so this
# fallback defaults OFF; the infrastructure is real and tested, ops can
# enable it deliberately once they have accepted that trade-off (see
# CASE10_MATRIX_132_COVERAGE.md).
ANCHOR_FALLBACK_ENABLED = os.getenv("CASE10_SEMANTIC_ANCHOR_FALLBACK_ENABLED", "0").strip().lower() not in ("0", "false", "no")

_model: Any = None
_model_load_failed = False


def is_enabled() -> bool:
    return _ENABLED


def is_anchor_fallback_enabled() -> bool:
    return _ENABLED and ANCHOR_FALLBACK_ENABLED


def _get_model() -> Any:
    """Lazy singleton: the heavy `sentence-transformers`/`torch` import only
    happens the first time a caller actually needs a semantic score, and
    never at all when this feature is disabled or every caller's
    deterministic match already succeeds. Any load failure (missing
    dependency, no cached weights and no network, corrupt cache) is cached
    as a permanent "unavailable" for the rest of the process -- we do not
    retry a slow failure on every subsequent call."""
    global _model, _model_load_failed
    if not _ENABLED or _model_load_failed:
        return None
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer

        from .gpu import preferred_device

        _model = SentenceTransformer(SEMANTIC_MODEL_NAME, device=preferred_device())
    except Exception:
        _model_load_failed = True
        _model = None
    return _model


@lru_cache(maxsize=4096)
def _embed_cached(text: str):
    model = _get_model()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True)


def embed(text: str | None):
    """Unit-normalized embedding for `text`, or `None` when the text is
    blank or the model is unavailable. Cached by exact (stripped) text --
    the same row/label text recurring across pages or parameters within one
    process is only ever embedded once, which is what keeps this cheap when
    several parameters land on the same rendered page (see module
    docstring's point (b))."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return None
    return _embed_cached(normalized)


def cosine(a, b) -> float:
    # Both vectors are already unit-normalized by `model.encode(...,
    # normalize_embeddings=True)`, so the dot product alone is the cosine
    # similarity -- no need to re-divide by norms.
    return float((a * b).sum())


def similarity(text_a: str | None, text_b: str | None) -> float | None:
    """Cosine similarity of two texts' embeddings, or `None` when either is
    blank or the model is unavailable -- callers must treat `None` as "no
    signal", never as a similarity of 0.0 (a real 0.0 is a legitimate,
    informative score; unavailability is not)."""
    vec_a, vec_b = embed(text_a), embed(text_b)
    if vec_a is None or vec_b is None:
        return None
    return cosine(vec_a, vec_b)


def best_row_text_similarity(query_text: str | None, row_texts: list[str]) -> tuple[int | None, float]:
    """Scores `query_text` against each of `row_texts` (typically a page's
    own Y-clustered text rows), returning the (index, score) of the best
    match -- index `None` when nothing clears `SEMANTIC_MIN_SIMILARITY` (or
    the model/query is unavailable), in which case `score` is the best raw
    score seen (0.0 if none)."""
    query_vec = embed(query_text)
    if query_vec is None:
        return None, 0.0
    best_index: int | None = None
    best_score = -1.0
    for index, text in enumerate(row_texts):
        vec = embed(text)
        if vec is None:
            continue
        score = cosine(query_vec, vec)
        if score > best_score:
            best_index, best_score = index, score
    if best_index is None:
        return None, 0.0
    if best_score < SEMANTIC_MIN_SIMILARITY:
        return None, best_score
    return best_index, best_score
