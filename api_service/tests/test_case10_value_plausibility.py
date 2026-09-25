"""Unit evidence next to an extracted number (Phase 10, prompt B, 2(в)).

Fixtures are the real one-line contexts the numeric/compound mechanisms cited on the SILVER objects, turned into
word lists -- the cases the review labelled "not a value" / "another quantity" must be told apart from the ones it
labelled genuine. No label is read here; the expectations are written by hand from the page text.
"""
from __future__ import annotations

import unittest

from app.domain import value_plausibility as vp


def _words(text: str) -> list[dict]:
    return [{"text": token, "bbox": [i * 10.0, 0.0, i * 10.0 + 9.0, 10.0]} for i, token in enumerate(text.split())]


def _evidence(text: str, value_token: str, unit: str, *, span: int = 1, row: str | None = None) -> dict:
    words = _words(text)
    index = next(i for i, w in enumerate(words) if w["text"] == value_token)
    return vp.unit_evidence(words, index, index + span - 1, unit, row_text=row if row is not None else text)


class CatalogUnitTests(unittest.TestCase):
    def test_families_of_catalog_units(self):
        self.assertEqual(vp.catalog_unit_family("м²"), "AREA_M2")
        self.assertEqual(vp.catalog_unit_family("м³"), "VOLUME_M3")
        self.assertEqual(vp.catalog_unit_family("м"), "LENGTH_M")
        self.assertEqual(vp.catalog_unit_family("мм"), "LENGTH_MM")
        self.assertEqual(vp.catalog_unit_family("%"), "PERCENT")
        self.assertIsNone(vp.catalog_unit_family("шт."))
        self.assertIsNone(vp.catalog_unit_family("Класс (КМ)"))

    def test_token_families_understand_spellings(self):
        for token, family in (("м2", "AREA_M2"), ("м²", "AREA_M2"), ("м3", "VOLUME_M3"), ("м³.", "VOLUME_M3"), ("мм", "LENGTH_MM"), ("%", "PERCENT")):
            self.assertEqual(vp.token_unit_family(token), family, token)
        self.assertEqual(vp.token_unit_family("шт."), "OTHER")
        self.assertIsNone(vp.token_unit_family("Геотекстиль"))


class NumericEvidenceTests(unittest.TestCase):
    def test_unit_right_after_the_number(self):
        e = _evidence("Строительный объем в т.ч. 88264,00 м3 выше отм. 0.000", "88264,00", "м³")
        self.assertEqual((e["verdict"], e["adjacent_side"]), (vp.ADJACENT, "after"))

    def test_unit_right_before_the_number(self):
        e = _evidence("автостоянка Строительный объем в т.ч. м³ 88264,00 Технониколь СТО", "88264,00", "м³")
        self.assertEqual((e["verdict"], e["adjacent_side"]), (vp.ADJACENT, "before"))

    def test_area_row_with_the_unit_before_the_number(self):
        self.assertEqual(_evidence("Степень Площадь здания м² 25036,27 1 5 Фрагменты", "25036,27", "м²")["verdict"], vp.ADJACENT)

    def test_two_word_spelling_of_square_metres(self):
        # the thousands merge ("17" + "140,2") makes the number span two words; "кв. м" sits before it
        self.assertEqual(_evidence("3 Площадь жилого здания, в т. ч.: кв. м - 17 140,2", "17", "м²", span=2)["verdict"], vp.ADJACENT)

    def test_clause_number_is_not_a_measurement(self):
        e = _evidence("Для выполнения требований п 7.19 с СП 7.13130.2013 исполнительные механизмы", "7.19", "м", row="Для выполнения требований п 7.19 с СП 7.13130.2013")
        self.assertEqual(e["verdict"], vp.ABSENT)

    def test_list_marker_is_not_a_measurement(self):
        e = _evidence("иметь запоров, препятствующих их свободному открыванию 8. к коробке. Наполнитель", "8.", "м")
        self.assertEqual(e["verdict"], vp.ABSENT)

    def test_date_is_not_a_measurement(self):
        self.assertEqual(_evidence("Зам 17763-25 04.25 «Жилой дом с инженерными сетями", "04.25", "м")["verdict"], vp.ABSENT)

    def test_height_next_to_a_volume_parameter_is_a_mismatch(self):
        # the "отметка вместо объёма" case
        e = _evidence("отметка (Высота) 40,85м 9 Предельная высота 40,95 м Площадь квартир", "40,95", "м³")
        self.assertEqual((e["verdict"], e["adjacent_family"]), (vp.MISMATCH, "LENGTH_M"))

    def test_same_dimension_other_scale_is_not_a_mismatch(self):
        e = _evidence("Ширина дверного полотна 900 мм в свету", "900", "м")
        self.assertEqual((e["verdict"], e["adjacent_family"]), (vp.ADJACENT, "LENGTH_MM"))

    def test_unit_far_away_only_counts_when_it_is_on_the_label_row(self):
        text = "Взамен 158.98 Геотекстиль GEO PRO 200"
        self.assertEqual(_evidence(text, "158.98", "м³", row=text)["verdict"], vp.ABSENT)
        self.assertEqual(_evidence(text, "158.98", "м³", row="Объем котлована 7 940 м³ Взамен 158.98 Геотекстиль")["verdict"], vp.IN_ROW)

    def test_parameter_without_a_physical_unit_is_not_gated(self):
        e = _evidence("Количество квартир 120", "120", "шт.")
        self.assertEqual(e["verdict"], vp.NOT_APPLICABLE)
        self.assertTrue(vp.backed(e))

    def test_backed_helper(self):
        self.assertTrue(vp.backed({"verdict": vp.ADJACENT}))
        self.assertTrue(vp.backed({"verdict": vp.IN_ROW}))
        self.assertFalse(vp.backed({"verdict": vp.MISMATCH}))
        self.assertFalse(vp.backed({"verdict": vp.ABSENT}))
        self.assertTrue(vp.backed(None))


def _table(rows: list[list[tuple[float, str]]], row_height: float = 12.0) -> list[dict]:
    """Word list of a small table: each row is [(x, text), ...]; y grows downwards like a PDF page."""
    words = []
    for r, cells in enumerate(rows):
        for x, text in cells:
            words.append({"text": text, "bbox": [x, r * row_height, x + 6.0 * len(text), r * row_height + 9.0]})
    return words


class ColumnHeaderUnitTests(unittest.TestCase):
    """A ведомость whose header says "Количество, м³" and whose rows carry bare numbers: the unit is in the column."""

    def setUp(self):
        self.words = _table([
            [(20, "Наименование"), (200, "Количество,"), (270, "м³")],
            [(20, "Выемка"), (220, "1491")],
            [(20, "Насыпь"), (220, "393")],
            [(20, "Примечание:"), (60, "см."), (90, "лист"), (120, "2")],
        ])

    def _index(self, text):
        return next(i for i, w in enumerate(self.words) if w["text"] == text)

    def test_bare_number_under_a_unit_header_is_backed_by_its_column(self):
        i = self._index("1491")
        e = vp.unit_evidence(self.words, i, i, "м³", row_text="Выемка 1491")
        self.assertEqual(e["verdict"], vp.IN_COLUMN)
        self.assertTrue(vp.backed(e))
        self.assertIn("м³", vp.column_text_above(self.words, i, i))

    def test_a_number_in_another_column_does_not_borrow_the_header(self):
        i = self._index("2")          # "см. лист 2" far left of the quantity column
        self.assertEqual(vp.unit_evidence(self.words, i, i, "м³", row_text="Примечание: см. лист 2")["verdict"], vp.ABSENT)

    def test_a_header_below_the_number_does_not_count(self):
        words = _table([[(220, "1491")], [(200, "Количество,"), (270, "м³")]])
        self.assertEqual(vp.unit_evidence(words, 0, 0, "м³", row_text="1491")["verdict"], vp.ABSENT)

    def test_compound_labels_are_also_found_in_the_column(self):
        words = _table([[(200, "Кол-во,"), (240, "шт.")], [(20, "Скамья"), (220, "12")]])
        i = next(k for k, w in enumerate(words) if w["text"] == "12")
        self.assertEqual(vp.component_unit_evidence(words, i, i, "шт.", row_text="Скамья 12")["verdict"], vp.IN_COLUMN)


class ComponentEvidenceTests(unittest.TestCase):
    def test_labels_of_a_compound_unit_must_be_on_the_row(self):
        text = "ПлГН1- м3 расхода материалов стен и 644,97 1,35 870,7095 4% 34,828"
        words = _words(text)
        first = next(i for i, w in enumerate(words) if w["text"] == "644,97")
        second = next(i for i, w in enumerate(words) if w["text"] == "1,35")
        self.assertEqual(vp.component_unit_evidence(words, first, first, "м³", row_text=text)["verdict"], vp.IN_ROW)
        self.assertEqual(vp.component_unit_evidence(words, second, second, "т", row_text=text)["verdict"], vp.ABSENT)

    def test_two_bare_ordinals_carry_no_unit(self):
        text = "Ведомость расхода материалов см. на листе 2. 2. План металлических колонн см. Том КМ."
        words = _words(text)
        for label in ("м³", "т"):
            first = next(i for i, w in enumerate(words) if w["text"] in {"2.", "2"})
            self.assertEqual(vp.component_unit_evidence(words, first, first, label, row_text=text)["verdict"], vp.ABSENT, label)

    def test_count_labels_are_matched_literally(self):
        text = "Количество МАФ 12 шт. 3 компл."
        words = _words(text)
        self.assertEqual(vp.component_unit_evidence(words, 2, 2, "шт.", row_text=text)["verdict"], vp.ADJACENT)
        self.assertEqual(vp.component_unit_evidence(words, 4, 4, "компл.", row_text=text)["verdict"], vp.ADJACENT)
        self.assertEqual(vp.component_unit_evidence(words, 4, 4, "зоны", row_text=text)["verdict"], vp.ABSENT)


if __name__ == "__main__":
    unittest.main()
