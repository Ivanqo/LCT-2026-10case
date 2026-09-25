"""Tests for `semantic_similarity.py` against the real embedding model (TZ
9.1 item 2, "семантические якоря") -- unlike `test_case10_cross_stage_
localization.py`'s plumbing tests (which mock scores), these load the actual
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` model and prove
it genuinely discriminates real Russian construction-document text pulled
forensically from the project's own official dataset (never a gold label,
only page/annotation text -- see CASE10_MATRIX_132_COVERAGE.md). Skipped
cleanly (not failed) when the model cannot load in this environment (no
network, dependency missing) -- this feature is optional by design and the
whole test suite must stay green either way.
"""
from __future__ import annotations

import unittest

from app.domain import semantic_similarity as sim


def _model_available() -> bool:
    return sim._get_model() is not None  # noqa: SLF001 -- test-only introspection


@unittest.skipUnless(_model_available(), "semantic embedding model unavailable in this environment")
class SimilarityRealModelTests(unittest.TestCase):
    """Real text pulled forensically (per CASE10_MATRIX_132_COVERAGE.md /
    checkpoint 40): OBJ-NOVOSLOBODSKAYA, parameter PZ-002 ("Общая площадь
    здания"). The genuine match is document НВС-2025.03-1.2-ПЗ.pdf page 9's
    own ТЭП table row; the "Стена в грунте" excerpt is the real text of
    document F0137 (2025-12-23 Новослободская _СВГ (1).pdf) page 1, the
    single RD-stage candidate the upstream keyword tagger flagged for this
    parameter at confidence 0.62 -- a genuine false positive (it shares only
    the word "площади", there meaning a bored pile's cross-sectional area,
    not building floor area)."""

    QUERY_PD = "Общая площадь здания. Раздел ПЗ: Таблица ТЭП (текстовая часть)"
    QUERY_RD = "Общая площадь здания. Раздел АР: Лист \"Общие данные\", Сводная экспликация"
    GENUINE_ROW = "3 Площадь жилого здания, в т. ч.: кв. м - 17 140,2"
    STENA_V_GRUNTE_EXCERPT = (
        "Для армированных свай и конструкций «стены в грунте» во избежание "
        "возникновения дефектов сплошности ствола сваи в процессе бетонирования, "
        "собой ряд взаимно пересекающихся по длине и по площади поперечного "
        "сечения буронабивных свай, выполненных в грунте"
    )
    # A DIFFERENT real matrix parameter's own genuine row (PZ-001, "Площадь
    # застройки" -- building footprint, not total area) -- a harder,
    # same-domain negative than the wall-construction excerpt.
    NEAR_MISS_ROW = "2.1 Площадь застройки наземной части 767,0"

    def test_genuine_match_scores_above_the_floor(self):
        score = sim.similarity(self.QUERY_PD, self.GENUINE_ROW)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, sim.SEMANTIC_MIN_SIMILARITY)

    def test_stena_v_grunte_excerpt_scores_below_the_floor(self):
        for query in (self.QUERY_PD, self.QUERY_RD):
            score = sim.similarity(query, self.STENA_V_GRUNTE_EXCERPT)
            self.assertIsNotNone(score)
            self.assertLess(score, sim.SEMANTIC_MIN_SIMILARITY, msg=f"query={query!r} score={score!r}")

    def test_genuine_match_clearly_outscores_the_stena_v_grunte_excerpt(self):
        positive = sim.similarity(self.QUERY_PD, self.GENUINE_ROW)
        negative = sim.similarity(self.QUERY_PD, self.STENA_V_GRUNTE_EXCERPT)
        self.assertGreater(positive - negative, 0.15)

    def test_blank_text_yields_no_signal_not_zero(self):
        self.assertIsNone(sim.similarity(self.QUERY_PD, ""))
        self.assertIsNone(sim.similarity(self.QUERY_PD, None))
        self.assertIsNone(sim.similarity("", self.GENUINE_ROW))

    def test_best_row_text_similarity_picks_the_genuine_row_over_the_excerpt(self):
        rows = [self.STENA_V_GRUNTE_EXCERPT, self.NEAR_MISS_ROW, self.GENUINE_ROW]
        best_index, score = sim.best_row_text_similarity(self.QUERY_PD, rows)
        self.assertEqual(best_index, 2)
        self.assertGreaterEqual(score, sim.SEMANTIC_MIN_SIMILARITY)

    def test_best_row_text_similarity_returns_none_when_every_row_is_irrelevant(self):
        best_index, _score = sim.best_row_text_similarity(self.QUERY_PD, [self.STENA_V_GRUNTE_EXCERPT])
        self.assertIsNone(best_index)

    def test_embedding_cache_returns_identical_vector_for_repeated_text(self):
        first = sim.embed(self.GENUINE_ROW)
        second = sim.embed(self.GENUINE_ROW)
        self.assertIs(first, second)  # lru_cache identity, not just equality


class DisabledFeatureTests(unittest.TestCase):
    """`is_enabled`/`is_anchor_fallback_enabled` reflect module-level config
    read once at import time in this codebase's existing style (same pattern
    as other `os.getenv(...)`-configured modules here) -- these just pin the
    documented defaults so a future change is a deliberate edit, not silent
    drift."""

    def test_anchor_fallback_defaults_to_disabled(self):
        # See semantic_similarity.py's module comment: real regression
        # testing (test_case10_generic_extraction.py's site-table-collision
        # cases) found this fallback confuses two different same-domain ТЭП
        # tables' own "Площадь" rows, so it ships off by default.
        self.assertFalse(sim.ANCHOR_FALLBACK_ENABLED)

    def test_reranking_defaults_to_enabled(self):
        self.assertTrue(sim._ENABLED)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
