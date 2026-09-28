"""Phase 12 / S1: generic technical-economic-indicators row parsing."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import generic_matrix_extraction as gme  # noqa: E402
from app.domain import table_parser as tp  # noqa: E402


def word(text: str, x: float, y: float, width: float | None = None) -> dict:
    return {"text": text, "bbox": [x, y, x + (width if width is not None else max(8, len(text) * 5)), y + 10]}


def tep_snapshot(*rows: list[str]) -> dict:
    words = [word("Технико-экономические", 10, 10), word("показатели", 130, 10)]
    for row_num, tokens in enumerate(rows):
        y = 40 + row_num * 20
        x = 10
        for token in tokens:
            words.append(word(token, x, y))
            x += max(12, len(token) * 5 + 5)
    return {"page": 1, "width": 600, "height": 850, "words": words}


class TepRowParserTests(unittest.TestCase):
    def test_titled_table_uses_value_near_expected_unit_not_cited_year(self):
        snapshot = tep_snapshot(
            ["2", "Площадь", "застройки", "(по", "СП", "54.13330.2022", "прил.", "А)", "м²", "1076,49"]
        )
        match = tp.find_tep_value_match(snapshot, ["Площадь застройки"], expected_units=["м²"])
        self.assertIsNotNone(match)
        self.assertEqual(match.value, "1076,49")
        self.assertEqual(match.row_text, "2 Площадь застройки (по СП 54.13330.2022 прил. А) м² 1076,49")

    def test_word_layer_can_supply_title_marker_when_flat_text_order_is_scrambled(self):
        snapshot = tep_snapshot(["Площадь", "застройки", "объекта", "м²", "3009,4"])
        snapshot["text"] = "показатели проектируемого объекта"
        match = tp.find_tep_value_match(snapshot, ["Площадь застройки объекта"], expected_units=["м²"])
        self.assertIsNotNone(match)
        self.assertEqual(match.value, "3009,4")

    def test_requires_tep_title_and_same_visual_row(self):
        no_title = {"page": 1, "width": 600, "height": 850, "words": [word("Площадь", 10, 40), word("застройки", 60, 40), word("1076,49", 220, 40)]}
        split_row = tep_snapshot(["Площадь", "застройки"], ["1076,49"])
        narrative = {
            "page": 1, "width": 600, "height": 850,
            "words": [word("-", 10, 10), word("технико-экономических", 20, 10), word("показателей", 150, 10),
                      word("(ТЭП);", 220, 10), word("Площадь", 10, 40), word("застройки", 60, 40), word("1076,49", 220, 40)],
        }
        nominative_reference = {
            "page": 1, "width": 600, "height": 850,
            "words": [word("В", 10, 10), word("разделе", 20, 10), word("приведены", 60, 10),
                      word("технико-экономические", 115, 10), word("показатели", 240, 10), word("здания", 300, 10),
                      word("Площадь", 10, 40), word("застройки", 60, 40), word("1076,49", 220, 40)],
        }
        heading_cue_in_sentence = {
            "page": 1, "width": 600, "height": 850,
            "words": [word("Смотрите", 10, 10), word("основные", 60, 10), word("технико-экономические", 110, 10),
                      word("показатели", 240, 10), word("здания", 310, 10), word("Площадь", 10, 40),
                      word("застройки", 60, 40), word("1076,49", 220, 40)],
        }
        self.assertIsNone(tp.find_tep_value_match(no_title, ["Площадь застройки"], expected_units=["м²"]))
        self.assertIsNone(tp.find_tep_value_match(split_row, ["Площадь застройки"], expected_units=["м²"]))
        self.assertIsNone(tp.find_tep_value_match(narrative, ["Площадь застройки"], expected_units=["м²"]))
        self.assertIsNone(tp.find_tep_value_match(nominative_reference, ["Площадь застройки"], expected_units=["м²"]))
        self.assertIsNone(tp.find_tep_value_match(heading_cue_in_sentence, ["Площадь застройки"], expected_units=["м²"]))

    def test_generic_numeric_match_uses_vocab_phrase_in_a_tep_row(self):
        snapshot = tep_snapshot(["Площадь", "застройки", "объекта", "м²", "3009,4"])
        param = SimpleNamespace(
            code="PZ-001", parameter_name="Площадь застройки", unit="м²",
            source_pd=None, source_rd=None, source_id=None,
            sp_reference=None, gost_reference=None, fz_reference=None,
        )
        doc = SimpleNamespace(dataset_stage="PD", id=1)
        fragment = SimpleNamespace(document_version_id=1, page=1)
        with patch.object(gme, "expected_normative_references", return_value=set()), patch.object(
            gme, "unit_evidence", return_value={"compatible": True}
        ):
            outcome = gme._match_numeric(snapshot, fragment, doc, 1, param)
        self.assertIsNotNone(outcome)
        observation = outcome[0]
        self.assertEqual(observation.value, "3009,4")
        self.assertIn("Площадь застройки объекта", observation.context)

    def test_specific_volume_code_does_not_take_unqualified_total_volume(self):
        snapshot = tep_snapshot(["5", "Строительный", "объем", "в", "т.ч.", "м³", "88264,00"])
        self.assertIsNone(tp.find_tep_value_match(snapshot, ["Строительный объем (Подземный)"], expected_units=["м³"]))
        param = SimpleNamespace(
            code="PZ-005", parameter_name="Строительный объем (Подземный)", unit="м³",
            source_pd=None, source_rd=None, source_id=None,
            sp_reference=None, gost_reference=None, fz_reference=None,
        )
        doc = SimpleNamespace(dataset_stage="PD", id=1)
        fragment = SimpleNamespace(document_version_id=1, page=1)
        self.assertIsNone(gme._match_numeric(snapshot, fragment, doc, 1, param))

    def test_qualified_volume_codes_use_above_and_below_zero_rows(self):
        cases = [
            ("PZ-005", "Строительный объем (Подземный)", ["ниже", "отм.", "0.000", "13962,18", "м³"], "13962,18"),
            ("PZ-006", "Строительный объем (Надземный)", ["выше", "отм.", "0.000", "74301,82", "м³"], "74301,82"),
        ]
        for code, name, row, expected in cases:
            with self.subTest(code=code):
                snapshot = tep_snapshot(row)
                match = tp.find_tep_value_match(
                    snapshot, gme.anchor_phrases(code, name, "PD"),
                    expected_units=("м³", *gme.unit_spellings(code)),
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.value, expected)

    def test_height_alias_matches_unqualified_tep_row_but_not_modified_height(self):
        anchors = gme.anchor_phrases("PZ-008", "Высота здания", "PD")
        unit_list = ("м", *gme.unit_spellings("PZ-008"))
        plain = tep_snapshot(["6.", "Высота", "+", "82,8", "м"])
        modified = tep_snapshot(["Предельная", "высота", "здания", "до", "80", "м"])
        self.assertEqual(tp.find_tep_value_match(plain, anchors, expected_units=unit_list).value, "82,8")
        self.assertIsNone(tp.find_tep_value_match(modified, anchors, expected_units=unit_list))

    def test_height_modifier_is_not_treated_as_the_unqualified_building_height(self):
        snapshot = tep_snapshot(["Предельная", "высота", "здания", "до", "80", "м"])
        self.assertIsNone(tp.find_tep_value_match(snapshot, ["Высота здания"], expected_units=["м"]))


if __name__ == "__main__":
    unittest.main()
