"""Tests for the semantic-search additions to `anchor_search.py`:
`row_text_at_word`, `source_hints`, `build_semantic_query`, and the
disabled-by-default `semantic_anchor_fallback_index`/`find_anchor_end_index_
for_phrase` fallback path. No real document excerpt or gold value appears
here -- all fixtures are synthetic, mirroring `test_case10_cross_stage_
localization.py`'s own fixture style.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.domain.anchor_search import (
    build_semantic_query,
    expected_normative_references,
    extract_normative_references,
    find_anchor_end_index_for_phrase,
    find_anchor_end_word_index,
    row_text_at_word,
    semantic_anchor_fallback_index,
    source_hints,
)


def _row_snapshot(rows: list[list[str]], *, row_height: float = 14.0, col_width: float = 70.0, start_y: float = 100.0) -> list[dict]:
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


class RowTextAtWordTests(unittest.TestCase):
    def test_joins_every_word_on_the_same_row(self):
        words = _row_snapshot([["3", "Площадь", "жилого", "здания,"], ["17", "140,2"]])
        text = row_text_at_word(words, 1)  # "Площадь", on row 0
        self.assertEqual(text, "3 Площадь жилого здания,")

    def test_second_row_is_independent(self):
        words = _row_snapshot([["3", "Площадь", "здания"], ["17", "140,2"]])
        text = row_text_at_word(words, 4)  # "140,2", on row 1
        self.assertEqual(text, "17 140,2")

    def test_none_index_yields_none(self):
        words = _row_snapshot([["a", "b"]])
        self.assertIsNone(row_text_at_word(words, None))

    def test_out_of_range_index_yields_none(self):
        words = _row_snapshot([["a", "b"]])
        self.assertIsNone(row_text_at_word(words, 99))

    def test_empty_words_yields_none(self):
        self.assertIsNone(row_text_at_word([], 0))


class SourceHintsAndSemanticQueryTests(unittest.TestCase):
    def _param(self, *, parameter_name: str, other_normative: dict | None) -> SimpleNamespace:
        return SimpleNamespace(
            parameter_name=parameter_name,
            other_normative=json.dumps(other_normative) if other_normative is not None else None,
        )

    def test_recovers_per_stage_hint_text(self):
        param = self._param(parameter_name="Общая площадь здания", other_normative={
            "source_pd": "Раздел ПЗ: Таблица ТЭП (текстовая часть)",
            "source_rd": "Раздел АР: Лист \"Общие данные\", Сводная экспликация",
            "source_id": "Технический план БТИ; ЗОС",
        })
        hints = source_hints(param)
        self.assertEqual(hints["PD"], "Раздел ПЗ: Таблица ТЭП (текстовая часть)")
        self.assertEqual(hints["RD"], "Раздел АР: Лист \"Общие данные\", Сводная экспликация")
        self.assertEqual(hints["ID"], "Технический план БТИ; ЗОС")

    def test_missing_other_normative_yields_empty_hints(self):
        param = self._param(parameter_name="X", other_normative=None)
        self.assertEqual(source_hints(param), {})

    def test_malformed_json_yields_empty_hints_not_an_error(self):
        param = SimpleNamespace(parameter_name="X", other_normative="{not json")
        self.assertEqual(source_hints(param), {})

    def test_build_semantic_query_combines_name_and_hint(self):
        param = self._param(parameter_name="Общая площадь здания", other_normative={"source_pd": "Раздел ПЗ: Таблица ТЭП"})
        self.assertEqual(build_semantic_query(param, "PD"), "Общая площадь здания. Раздел ПЗ: Таблица ТЭП")

    def test_build_semantic_query_falls_back_to_bare_name_without_a_hint(self):
        param = self._param(parameter_name="Общая площадь здания", other_normative=None)
        self.assertEqual(build_semantic_query(param, "PD"), "Общая площадь здания")


class SemanticAnchorFallbackDisabledByDefaultTests(unittest.TestCase):
    """Real regression testing (see `test_case10_generic_extraction.py`'s
    site-table-collision cases, and `semantic_similarity.py`'s module
    comment) found this fallback confuses two different same-domain ТЭП
    tables -- it ships disabled by default, so both entry points must be a
    clean no-op regardless of what the embedding model would say."""

    def test_fallback_index_is_none_when_disabled(self):
        words = _row_snapshot([["Общая", "площадь", "участка", "13060,00"]])
        self.assertIsNone(semantic_anchor_fallback_index(words, "Общая площадь здания"))

    def test_wrapper_still_returns_none_when_literal_match_and_fallback_both_fail(self):
        words = _row_snapshot([["Совершенно", "другой", "текст"]])
        self.assertIsNone(find_anchor_end_index_for_phrase(words, "Общая площадь здания"))


class SemanticAnchorFallbackScopeGateTests(unittest.TestCase):
    """Proves the fix added after the collision described above: even with
    the fallback enabled and the embedding model mocked to prefer the site
    row (exactly the real checkpoint-41 finding), the scope-word gate keeps
    a site-scoped row from ever being offered as a match for a
    building-scoped query -- it is filtered out of the candidate pool
    `best_row_text_similarity` itself sees, not merely outscored."""

    def test_site_row_is_excluded_from_a_building_query_even_when_it_would_score_highest(self):
        words = _row_snapshot([
            ["Общая", "площадь", "участка", "13060,00"],
            ["Площадь", "здания", "25036,27"],
        ])

        def fake_best(query_text, row_texts):
            self.assertNotIn("участка", " ".join(row_texts))
            return 0, 0.9

        with patch("app.domain.semantic_similarity.is_anchor_fallback_enabled", return_value=True), \
             patch("app.domain.semantic_similarity.best_row_text_similarity", side_effect=fake_best):
            index = semantic_anchor_fallback_index(words, "Общая площадь здания")
        self.assertIsNotNone(index)
        self.assertEqual(words[index]["text"], "Площадь")

    def test_building_row_is_excluded_from_a_site_query_even_when_it_would_score_highest(self):
        words = _row_snapshot([
            ["Площадь", "здания", "25036,27"],
            ["Общая", "площадь", "участка", "13060,00"],
        ])

        def fake_best(query_text, row_texts):
            self.assertNotIn("здания", " ".join(row_texts))
            return 0, 0.9

        with patch("app.domain.semantic_similarity.is_anchor_fallback_enabled", return_value=True), \
             patch("app.domain.semantic_similarity.best_row_text_similarity", side_effect=fake_best):
            index = semantic_anchor_fallback_index(words, "Площадь участка")
        self.assertIsNotNone(index)
        self.assertEqual(words[index]["text"], "Общая")

    def test_scope_less_anchor_is_unaffected_by_the_gate(self):
        words = _row_snapshot([["Толщина", "фундаментной", "плиты", "300", "мм"]])
        with patch("app.domain.semantic_similarity.is_anchor_fallback_enabled", return_value=True), \
             patch("app.domain.semantic_similarity.best_row_text_similarity", return_value=(0, 0.9)):
            index = semantic_anchor_fallback_index(words, "Толщина фундаментной плиты")
        self.assertEqual(index, 0)

    def test_all_candidates_scope_incompatible_yields_none_not_a_wrong_match(self):
        words = _row_snapshot([["Общая", "площадь", "участка", "13060,00"]])
        with patch("app.domain.semantic_similarity.is_anchor_fallback_enabled", return_value=True), \
             patch("app.domain.semantic_similarity.best_row_text_similarity") as mocked:
            index = semantic_anchor_fallback_index(words, "Общая площадь здания")
        mocked.assert_not_called()
        self.assertIsNone(index)


class FindAnchorEndWordIndexRowPrefixTieBreakTests(unittest.TestCase):
    """Generalized (non-LOS3A-specific) reproduction of a real forensic
    finding: LOS3A PD page 14, PZ-004 ("Строительный объем") -- a
    descriptive preamble sentence listing what a section covers and the
    real numbered table row can both match an anchor phrase at the exact
    same (zero) total gap. See `test_case10_generic_extraction.py`'s
    `test_prefers_the_real_numbered_row_over_a_tied_gap_preamble_mention_
    on_a_real_los3a_page` for the byte-real page-14 fixture this
    generalizes to prove the fix is not hardcoded to that one document."""

    def test_prefers_match_preceded_by_a_numeric_row_prefix_on_a_zero_gap_tie(self):
        norm_words = [
            "раздел", "описывает", "площадь", "участка", "общая", "масса",
            "конструкции", "и", "запас", "прочности", "7", "общая",
            "масса", "4520,00", "кг",
        ]
        end = find_anchor_end_word_index(norm_words, ["общая", "масса"])
        # The preamble's "общая масса" (index 4) also ties at gap 0, but is
        # not preceded by a row item number -- the real row (index 11,
        # preceded by "7") wins.
        self.assertEqual(end, 13)

    def test_falls_back_to_earliest_position_when_neither_side_has_a_row_prefix(self):
        norm_words = ["общая", "масса", "x", "y", "общая", "масса"]
        # Neither occurrence has a numeric prefix -- unchanged pre-existing
        # "earliest tie" behaviour.
        end = find_anchor_end_word_index(norm_words, ["общая", "масса"])
        self.assertEqual(end, 2)

    def test_does_not_override_a_strictly_tighter_match_elsewhere(self):
        # The row-prefix signal is a tie-breaker ONLY, applied after (never
        # instead of) the existing gap comparison -- a strictly smaller gap
        # elsewhere on the page always wins regardless of either side's
        # prefix.
        norm_words = ["7", "общая", "с", "с", "масса", "общая", "масса"]
        end = find_anchor_end_word_index(norm_words, ["общая", "масса"])
        self.assertEqual(end, 7)

    def test_single_match_is_unaffected_regardless_of_prefix(self):
        self.assertEqual(find_anchor_end_word_index(["общая", "масса"], ["общая", "масса"]), 2)
        self.assertEqual(find_anchor_end_word_index(["7", "общая", "масса"], ["общая", "масса"]), 3)


class NormativeReferenceTests(unittest.TestCase):
    def test_extracts_sp_reference_from_free_text(self):
        text = "Площадь здания (по СП 54.13330.2016, прил. А.1.2) м² 25036,27"
        self.assertEqual(extract_normative_references(text), frozenset({"СП 54.13330.2016"}))

    def test_extracts_multiple_distinct_references(self):
        text = "См. СП 54.13330.2016 и СП 118.13330.2012"
        self.assertEqual(
            extract_normative_references(text), frozenset({"СП 54.13330.2016", "СП 118.13330.2012"}),
        )

    def test_no_reference_in_text_is_an_empty_set_not_none(self):
        self.assertEqual(extract_normative_references("просто текст без ссылок на нормативы"), frozenset())

    def test_blank_or_missing_text_is_an_empty_set(self):
        self.assertEqual(extract_normative_references(None), frozenset())
        self.assertEqual(extract_normative_references(""), frozenset())

    def test_gost_with_optional_r_is_recognized(self):
        self.assertEqual(extract_normative_references("согласно ГОСТ Р 21.101-2020"), frozenset({"ГОСТ Р 21.101-2020"}))

    def test_expected_references_read_from_catalog_fields(self):
        param = SimpleNamespace(sp_reference="СП 54.13330.2016", gost_reference=None, fz_reference=None)
        self.assertEqual(expected_normative_references(param), frozenset({"СП 54.13330.2016"}))

    def test_expected_references_combine_every_populated_field(self):
        param = SimpleNamespace(sp_reference="СП 54.13330.2016", gost_reference="ГОСТ Р 21.101-2020", fz_reference=None)
        self.assertEqual(
            expected_normative_references(param), frozenset({"СП 54.13330.2016", "ГОСТ Р 21.101-2020"}),
        )

    def test_expected_references_empty_when_catalog_fields_unset(self):
        # The real 132-parameter catalog import does not currently populate
        # these fields for any parameter -- this is the actual "no signal"
        # state today, and must never be mistaken for "candidate is wrong".
        param = SimpleNamespace(sp_reference=None, gost_reference=None, fz_reference=None)
        self.assertEqual(expected_normative_references(param), frozenset())

    def test_expected_references_falls_back_to_bare_text_without_a_recognized_prefix(self):
        param = SimpleNamespace(sp_reference="54.13330.2016", gost_reference=None, fz_reference=None)
        self.assertEqual(expected_normative_references(param), frozenset({"54.13330.2016"}))


if __name__ == "__main__":
    unittest.main()
