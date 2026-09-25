"""Generic (data-driven, not per-code) numeric anchor extractor added to close
part of the 132-parameter matrix coverage gap: `matrix_unit_classifier` +
`generic_matrix_extraction` + the `_upsert_generic_group` wiring in
`official_evidence.py`. Three of the text fixtures below (`_TEP_TABLE_TEXT`,
`_SITE_TABLE_TEXT`, `_LOS3A_PD_PAGE14_TEXT`) are anonymized excerpts of real
ТЭП-table page text (the last one byte-real, pulled directly from the real
LOS3A PDF via `fitz`) used only to prove the tokenizer/regex survives real
multi-column PDF word-order artifacts -- no object_id/file_id/page
number/GOLD label from the competition dataset is attached to them, and no
assertion here compares against a known gold value; they are treated
exactly like any other hand-written parser fixture. Every other fixture is
synthetic.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain.anchor_search import GENERIC_SITE_LOCATION
from app.domain.matrix_unit_classifier import classify_unit, is_generic_anchor_eligible
from app.domain.generic_matrix_extraction import (
    GENERIC_EXTRACTOR_VERSION,
    GenericObservation,
    _match_numeric,
    collect_generic_observations,
    eligible_params,
    extract_generic_observation_for_page,
    find_anchor_numeric_value,
    new_generic_budget,
    values_equal,
)
from app.domain.official_evidence import _upsert_generic_group


# Real ТЭП (technical-economic indicators) table text, as returned by
# fitz's word-sorted text extraction on a real ПЗ (explanatory note) page.
# Multi-column tables get their "№" index column re-interleaved with the
# value column in reading order -- e.g. "Площадь застройки 2. 4650,91" --
# which is exactly the row-index-vs-value ambiguity the extractor must
# resolve correctly.
_TEP_TABLE_TEXT = (
    "показатели проектируемого Технико-экономические объекта; ПОКАЗАТЕЛИ "
    "ТЕХНИКО-ЭКОНОМИЧЕСКИЕ № П Ед. Наименование показателей Значение "
    "Примечание № изм. п/п 1 2 3 4 5 га Площадь участка 1. 1,306 м ² "
    "Площадь застройки 2. 4650,91 Суммарная поэтажная площадь м ² 3. "
    "12598,4 объекта в габаритах наружных стен м Габаритные размеры "
    "здания в осях 4. 69,645х90,710 Количество мест 5. 600 Количество "
    "классов 6. 24 м ² Общая площадь здания, в т.ч.: 11618,27 выше отм. "
    "0,000 7. - 11114,27 ниже отм. - 0,000 504,0 м ²/место Общая площадь "
    "на место 8. 1 17,5 м ² 8010,1 Расчётная площадь здания 9. м ²/место "
    "Расчётная площадь на 1 место 10. 13,3 м ³ Строительный объём здания, "
    "в т.ч.: 60997,93 подземная часть 11. - 10066,8 надземная часть - "
    "50931,13"
)

# A DIFFERENT real table (site/ГПЗУ technical indicators, not the building)
# on a different sheet, whose own "Общая площадь" row is the SITE area
# (13060 m2), not the BUILDING area -- deliberately kept as a separate
# fixture to prove a full-phrase anchor ("Общая площадь здания") does not
# collide with this table's partial-phrase "Общая площадь" row.
_SITE_TABLE_TEXT = (
    "10 показатели земельного участка. 2.5. Технико-экономические № "
    "Наименование Ед. изм. Кол-во п/п Общая площадь м2 1 13060,00 "
    "Площадь застройки, в том числе: м2 2 4629,70 Здание школы м2 2.1 "
    "4620,90"
)

# Byte-real word-sorted text of LOS3A PD page 14 ("1. 01-01-00-02-ПЗ.pdf",
# the exact document/page the task's own success criterion names), pulled
# directly from the real PDF via `fitz`'s own `get_text("words", sort=True)`
# -- no object_id/file_id/GOLD label from the competition dataset is
# attached, matching this file's existing real-excerpt fixtures above. This
# is the real forensic reproduction of the PZ-004 ("Строительный объем")
# tied-gap collision (CASE10_MATRIX_132_COVERAGE.md,
# `los3a_forensic_deep_dive_pz001_pz002_pz004`): the section's own preamble
# sentence ("...площадь застройки, общая площадь, строительный объем (в том
# числе подземной части)...") and the real numbered table row ("5.
# Строительный объем в т.ч. 88264,00 м3 ...") both match the anchor phrase
# at the exact same (zero) total gap -- confirmed pre-fix to make
# `find_anchor_numeric_value` return `0.5120` (row 1's own "Площадь участка
# по ГПЗУ" value, picked up from the preamble match's lookahead window,
# exactly as documented).
_LOS3A_PD_PAGE14_TEXT = (
    "14 Изымаемые земли во временное или постоянно пользование "
    "отсутствуют. л. Сведения об использованных в проекте изобретениях и о "
    "результатах проведенных патентных исследований. В составе проекта не "
    "были использованы изобретения и патентные исследования, требующие "
    "согласование правообладателя. м. Технико-экономические показатели "
    "проектируемых объектов капитального строительства, в том числе "
    "площадь застройки, общая площадь, строительный объем (в том числе "
    "подземной части), количество этажей (в том числе подземных) и "
    "протяженность (для линейных объектов). № Наименование Количество 1. "
    "Площадь участка по ГПЗУ 0,5120 Га 1076,49 м2 2. Площадь застройки (по "
    "СП 54.13330.2022 прил. А.1.1. с учетом выступающих частей здания, "
    "консольно выступающие за плоскость стены на высоте менее 4,5 м) 2 "
    "220,73 м² Площадь застройки подземной части (по СП 54.13330.2022 "
    "прил. А.1.1. выходящая за контур надземной части площадь подземной "
    "части здания, которая определяется как площадь горизонтального "
    "сечения по внешнему контуру подземных ограждающих конструкций) 3. "
    "Площадь застройки (без учета выступающих частей 1061,49 м2 балконов) "
    "4. Площадь здания (по СП 54.13330.2016, прил. А.1.2) 25036,27 м2 В "
    "т.ч. площадь подземной части 3196,44 м2 5. Строительный объем в т.ч. "
    "88264,00 м3 выше отм. 0.000 74301,82 м3 ниже отм. 0.000 13962,18 м3 № "
    "6. Высота + 82,8 м 7. Пожарно-техническая высота здания (в "
    "соответствии с 74,990 м Взам.инв п.3.1. СП 1.13130.2020). Секция С1 "
    "74,990 м Секция С2 63,290 м 8. Количество этажей 21-25 +1 подземный "
    "дата этаж и 10. Количество секций 2 Подп. подл. Лист Инв.№ "
    "19-0322-ОК-1/Н-ПЗ 12 Изм. Кол.уч. Лист №док. Подпись Дата"
)


class MatrixUnitClassifierTests(unittest.TestCase):
    def test_simple_physical_units_are_numeric_simple(self):
        for unit in ("м", "мм", "м²", "м³", "%", "мм²"):
            self.assertEqual(classify_unit(unit), "NUMERIC_SIMPLE", unit)

    def test_compound_units_are_not_simple(self):
        for unit in ("шт. / м", "м³/ч / Па / кВт", "% / °", "Марка / Толщина"):
            self.assertEqual(classify_unit(unit), "NUMERIC_COMPOUND", unit)

    def test_counts_and_enums_are_distinguished_from_measurements(self):
        self.assertEqual(classify_unit("шт."), "NUMERIC_COUNT")
        self.assertEqual(classify_unit("Марка (B)"), "ENUM_CLASS")
        self.assertEqual(classify_unit("Класс (А)"), "ENUM_CLASS")

    def test_dash_and_empty_unit_is_none(self):
        self.assertEqual(classify_unit("—"), "NONE")
        self.assertEqual(classify_unit(""), "NONE")
        self.assertEqual(classify_unit(None), "NONE")

    def test_eligibility_requires_multi_word_distinctive_name(self):
        self.assertTrue(is_generic_anchor_eligible(
            code="PZ-002", unit="м²", parameter_name="Общая площадь здания", excluded_codes=frozenset(),
        ))
        self.assertFalse(is_generic_anchor_eligible(
            code="PZ-999", unit="м", parameter_name="Высота", excluded_codes=frozenset(),
        ))

    def test_official_rule_pack_codes_are_never_eligible_even_with_a_simple_unit(self):
        self.assertFalse(is_generic_anchor_eligible(
            code="KR-058", unit="мм", parameter_name="Толщина монолитной фундаментной плиты",
            excluded_codes=frozenset({"KR-058"}),
        ))

    def test_compound_and_enum_units_are_never_eligible(self):
        self.assertFalse(is_generic_anchor_eligible(
            code="X-1", unit="шт. / м", parameter_name="Спецификация ограждения", excluded_codes=frozenset(),
        ))
        self.assertFalse(is_generic_anchor_eligible(
            code="X-2", unit="Марка (B)", parameter_name="Класс прочности бетона", excluded_codes=frozenset(),
        ))


def _word_snapshot(text: str, *, width: float = 1191.0, height: float = 842.0, page: int = 1) -> dict:
    words = []
    cursor = 10.0
    for token in text.split():
        token_width = max(8.0, len(token) * 6.0)
        words.append({"text": token, "bbox": [cursor, 500.0, cursor + token_width, 510.0]})
        cursor += token_width + 3.0
    return {"page": page, "width": width, "height": height, "text": " ".join(t["text"] for t in words), "words": words}


class AnchorNumericExtractionTests(unittest.TestCase):
    def test_finds_value_immediately_after_full_phrase_skipping_row_index(self):
        match = find_anchor_numeric_value(_word_snapshot(_TEP_TABLE_TEXT), "Площадь застройки")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "4650.91")

    def test_full_phrase_disambiguates_building_area_from_a_longer_label(self):
        match = find_anchor_numeric_value(_word_snapshot(_TEP_TABLE_TEXT), "Общая площадь здания")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "11618.27")

    def test_full_phrase_does_not_collide_with_a_shorter_look_alike_label(self):
        # The site table's row is literally "Общая площадь" (site area); the
        # matrix parameter is "Общая площадь здания" (building area). The
        # full phrase must not match the shorter, semantically different label.
        match = find_anchor_numeric_value(_word_snapshot(_SITE_TABLE_TEXT), "Общая площадь здания")
        self.assertIsNone(match)

    def test_shorter_anchor_correctly_reads_the_site_tables_own_value(self):
        match = find_anchor_numeric_value(_word_snapshot(_SITE_TABLE_TEXT), "Общая площадь")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "13060.00")

    def test_no_match_returns_none(self):
        self.assertIsNone(find_anchor_numeric_value(_word_snapshot(_TEP_TABLE_TEXT), "Толщина фундаментной плиты"))

    def test_case_and_whitespace_insensitive(self):
        text = "прочее ПЛОЩАДЬ ЗАСТРОЙКИ 4650,91 прочее"
        match = find_anchor_numeric_value(_word_snapshot(text), "площадь застройки")
        self.assertEqual(match.normalized_value, "4650.91")

    def test_empty_anchor_or_snapshot_is_a_clean_none(self):
        self.assertIsNone(find_anchor_numeric_value(_word_snapshot(""), "Площадь застройки"))
        self.assertIsNone(find_anchor_numeric_value(_word_snapshot(_TEP_TABLE_TEXT), ""))

    def test_inserted_qualifier_word_between_anchor_words_is_tolerated(self):
        # Real residential-project wording inserts a qualifier the catalog
        # name does not have ("жилого" between "площадь" and "здания").
        text = "2 контур надземной части Площадь жилого здания, в т. ч.: 52731,4 кв. м"
        match = find_anchor_numeric_value(_word_snapshot(text), "Общая площадь здания")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "52731.4")

    def test_dropped_leading_qualifier_still_finds_the_right_row(self):
        # The catalog's own leading qualifier ("Общая") is entirely absent
        # from this real label -- only the fallback (full phrase minus its
        # first word) can find it, and it must still require every remaining
        # anchor word (here "здания") to be literally present.
        text = "Площадь жилого здания, в т. ч.: 52731,4 кв. м"
        match = find_anchor_numeric_value(_word_snapshot(text), "Общая площадь здания")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "52731.4")

    def test_dropped_leading_qualifier_fallback_does_not_revive_the_site_table_collision(self):
        # Even with the leading-word-dropped fallback active, the site table
        # (a different, unrelated label) must still not be mistaken for the
        # building area -- "здания" (genitive) is simply never on that page.
        match = find_anchor_numeric_value(_word_snapshot(_SITE_TABLE_TEXT), "Общая площадь здания")
        self.assertIsNone(match)

    def test_dropped_leading_qualifier_prefers_the_tighter_of_two_real_matches(self):
        # Real forensic finding (a genuine LOS3A PD-stage page, see the
        # live-candidate-tagger checkpoint memory): the 2-word fallback
        # ["площадь", "здания"] matched an EARLIER, gap-2 coincidence inside
        # unrelated flowing prose ("...площадь подземной части здания...",
        # a different sub-quantity) before ever reaching the LATER, genuine,
        # gap-0 table label ("Площадь здания (по...) 25036,27"). Reading-
        # order-first-match returned the wrong row's number (1061.49, an
        # unrelated site-area value); preferring the tighter (smaller-gap)
        # match fixes it without needing to know which one comes "first".
        text = (
            "выходящая за контур надземной части площадь подземной части здания, "
            "которая определяется как площадь сечения) 1061,49 м2 "
            "4. Площадь здания (по СП 54.13330.2016, прил. А.1.2) 25036,27 м2"
        )
        match = find_anchor_numeric_value(_word_snapshot(text), "Общая площадь здания")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "25036.27")

    def test_thousands_separator_split_across_two_words_is_merged(self):
        # A real value ("17 140,2") rendered as two adjacent PDF words: a
        # bare leading digit group, then an exact 3-digit continuation.
        text = "Площадь жилого здания, в т. ч.: 17 140,2 кв. м"
        match = find_anchor_numeric_value(_word_snapshot(text), "Общая площадь здания")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "17140.2")

    def test_row_index_next_to_an_unrelated_complete_number_is_not_merged(self):
        # "1" (a "№" column value) sits right before "13060,00", which is
        # already a complete, self-contained word -- must not be misread as
        # if "1" were a thousands-prefix of it.
        text = "Общая площадь м2 1 13060,00 Площадь застройки"
        match = find_anchor_numeric_value(_word_snapshot(text), "Общая площадь")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "13060.00")

    def test_trailing_parenthesized_qualifier_is_stripped_for_a_real_los3a_row(self):
        # Real catalog name PZ-004 is "Строительный объем (Общий)"; the real
        # LOS3A RD table row (see NEW_OBJECTS_ANALYSE.md section 6) reads
        # just "Строительный объём" with no "(Общий)" suffix at all -- one of
        # 28/132 catalog parameter names carrying a disambiguating trailing
        # qualifier a real document never repeats verbatim.
        text = "5 Строительный объём, в т.ч.: 88264,00 м³"
        match = find_anchor_numeric_value(_word_snapshot(text), "Строительный объем (Общий)")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "88264.00")

    def test_trailing_qualifier_stripping_does_not_revive_the_site_table_collision(self):
        # Stripping "(Общий)" must not make an unrelated, differently
        # qualified sibling code match either.
        match = find_anchor_numeric_value(_word_snapshot(_SITE_TABLE_TEXT), "Строительный объем (Общий)")
        self.assertIsNone(match)

    def test_prefers_the_real_numbered_row_over_a_tied_gap_preamble_mention_on_a_real_los3a_page(self):
        # Real forensic finding, LOS3A PD page 14 (see `_LOS3A_PD_PAGE14_
        # TEXT`'s own comment): the section's own descriptive preamble and
        # the real numbered ТЭП row both match "Строительный объем" at the
        # exact same (zero) total gap. Pre-fix, this returned the preamble's
        # own nearby number (`0.5120`, row 1's unrelated "Площадь участка по
        # ГПЗУ" value) -- confirmed by re-running this exact assertion
        # against the code before the row-item-prefix tie-break was added.
        # The real RD-side value for this same code is `88264.00`
        # (CASE10_MATRIX_132_COVERAGE.md), so this is not just "a" fix but
        # THE fix that makes this real PD/RD pair agree end to end.
        match = find_anchor_numeric_value(_word_snapshot(_LOS3A_PD_PAGE14_TEXT), "Строительный объем (Общий)")
        self.assertIsNotNone(match)
        self.assertEqual(match.normalized_value, "88264.00")


class GenericObservationExtractionTests(unittest.TestCase):
    def test_returns_none_when_no_anchor_match(self):
        snapshot = _word_snapshot("Ничего релевантного здесь нет")
        doc = SimpleNamespace(id=1)
        self.assertIsNone(extract_generic_observation_for_page("Площадь застройки", doc, 1, snapshot, None))

    def test_extracts_value_with_normalized_bbox_and_higher_confidence_for_decimal_values(self):
        snapshot = _word_snapshot("Площадь застройки 4650,91 кв.м")
        doc = SimpleNamespace(id=7)
        observation = extract_generic_observation_for_page("Площадь застройки", doc, 3, snapshot, None)
        self.assertIsInstance(observation, GenericObservation)
        self.assertEqual(observation.normalized_value, "4650.91")
        self.assertEqual(observation.page, 3)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in observation.bbox_normalized))
        x1, y1, x2, y2 = observation.bbox_normalized
        self.assertLess(x1, x2)
        self.assertLess(y1, y2)
        self.assertGreater(observation.confidence, 0.5)

    def test_integer_only_match_gets_lower_confidence_than_a_decimal_match(self):
        snapshot = _word_snapshot("Количество этажей 9 надземных")
        doc = SimpleNamespace(id=8)
        observation = extract_generic_observation_for_page("Количество этажей", doc, 1, snapshot, None)
        self.assertIsNotNone(observation)
        self.assertLess(observation.confidence, 0.5)


class MatchNumericExistenceSignalsTests(unittest.TestCase):
    """`_match_numeric`'s own contribution to `cross_stage_localization.py`'s
    signal 4 (module docstring there): flags a found value as an empty
    placeholder and/or a normative-reference mismatch, on top of the
    pre-existing fingerprint/semantic_text tuple. Generalized (any document,
    any code with this same structural pattern), not hardcoded to LOS3A --
    the real LOS3A/PZ-002 reproduction lives in
    `test_case10_cross_stage_localization.py`."""

    def _param(self, *, name: str = "Площадь застройки", sp_reference=None, gost_reference=None, fz_reference=None) -> SimpleNamespace:
        return SimpleNamespace(parameter_name=name, sp_reference=sp_reference, gost_reference=gost_reference, fz_reference=fz_reference)

    def _fragment(self, doc_id: int = 1, page: int = 1) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=0.5)

    def test_zero_value_is_flagged_as_empty(self):
        # A real forensic finding (PZ-001, CASE10_MATRIX_132_COVERAGE.md):
        # an unfilled certificate/template field reads as a literal zero
        # placeholder, not a genuine measurement.
        snapshot = _word_snapshot("Площадь застройки 0,00 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, self._param())
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, is_empty_value, _mismatch = outcome
        self.assertTrue(is_empty_value)

    def test_nonzero_value_is_not_flagged_as_empty(self):
        snapshot = _word_snapshot("Площадь застройки 4650,91 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, self._param())
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, is_empty_value, _mismatch = outcome
        self.assertFalse(is_empty_value)

    def test_no_mismatch_when_catalog_declares_no_expected_reference(self):
        # Today's real 132-parameter catalog import never populates
        # sp_reference/gost_reference/fz_reference -- this must stay a
        # complete no-op ("no signal"), not a false mismatch.
        snapshot = _word_snapshot("Общая площадь здания (по СП 118.13330.2012, прил. Г.5) 16867,90 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        param = self._param(name="Общая площадь здания", sp_reference=None)
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, param)
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, _empty, normative_mismatch = outcome
        self.assertFalse(normative_mismatch)

    def test_mismatch_flagged_when_found_text_cites_a_different_normative_document(self):
        snapshot = _word_snapshot("Общая площадь здания (по СП 118.13330.2012, прил. Г.5) 16867,90 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        param = self._param(name="Общая площадь здания", sp_reference="СП 54.13330.2016")
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, param)
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, _empty, normative_mismatch = outcome
        self.assertTrue(normative_mismatch)

    def test_no_mismatch_when_found_text_cites_the_expected_normative_document(self):
        snapshot = _word_snapshot("Общая площадь здания (по СП 54.13330.2016, прил. А.1.2) 25036,27 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        param = self._param(name="Общая площадь здания", sp_reference="СП 54.13330.2016")
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, param)
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, _empty, normative_mismatch = outcome
        self.assertFalse(normative_mismatch)

    def test_no_mismatch_when_found_text_names_no_normative_reference_at_all(self):
        # No citation found near the value is "no signal" for THIS
        # candidate, not "definitely wrong" -- a real table row without any
        # normative citation at all must not be penalized just because the
        # catalog happens to declare an expected one.
        snapshot = _word_snapshot("Общая площадь здания 25036,27 м²")
        doc = SimpleNamespace(id=1, discipline=None)
        param = self._param(name="Общая площадь здания", sp_reference="СП 54.13330.2016")
        outcome = _match_numeric(snapshot, self._fragment(), doc, 1, param)
        self.assertIsNotNone(outcome)
        _observation, _fp, _semantic_text, _empty, normative_mismatch = outcome
        self.assertFalse(normative_mismatch)


class ValuesEqualTests(unittest.TestCase):
    def test_exact_equality(self):
        self.assertTrue(values_equal(Decimal("4650.91"), Decimal("4650.91")))

    def test_small_rounding_jitter_is_tolerated(self):
        self.assertTrue(values_equal(Decimal("4650.91"), Decimal("4650.905")))

    def test_real_difference_is_not_tolerated(self):
        self.assertFalse(values_equal(Decimal("4650.91"), Decimal("4600.00")))


def _observation(value: str, *, doc_id: int, page: int, confidence: float = 0.55) -> GenericObservation:
    return GenericObservation(
        value=value,
        normalized_value=value,
        decimal_value=Decimal(value),
        confidence=confidence,
        document=SimpleNamespace(
            id=doc_id, dataset_file_id=f"SYN-{doc_id}", file_hash="hash", content_hash="hash",
            discipline="PZ", document_code="DOC", revision="1", approval_status="UNKNOWN",
        ),
        page=page,
        bbox_normalized=[0.1, 0.1, 0.2, 0.12],
        bbox_pdf=[10.0, 500.0, 40.0, 510.0],
        page_width=1000.0,
        page_height=800.0,
        extractor="generic_anchor_numeric",
        context="synthetic context",
        source_fragment=None,
    )


class UpsertGenericGroupTests(unittest.TestCase):
    """Mirrors the existing `_upsert_rule_groups` test style: PD+RD required,
    ID optional bonus, and every result must carry the generic extractor's
    own version tag so it is never confused with a tuned official rule pack."""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(
            id="proc-generic", project_id=1, organization_id=1, object_id="OBJ-SYNTHETIC",
            matrix_version="official-132-v1", dataset_version="synthetic-v1", upload_scenario="FULL",
        )
        self.param = SimpleNamespace(
            id=101, code="PZ-002", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True,
        )

    def tearDown(self):
        self.db.close()

    def test_pd_missing_produces_no_group(self):
        key = _upsert_generic_group(self.db, self.process, self.param, {"RD": _observation("100", doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_rd_missing_produces_no_group(self):
        key = _upsert_generic_group(self.db, self.process, self.param, {"PD": _observation("100", doc_id=1, page=1)})
        self.assertIsNone(key)

    def test_equal_values_are_negative_verified(self):
        from app.db.models import EvidenceGroup

        key = _upsert_generic_group(self.db, self.process, self.param, {
            "PD": _observation("100", doc_id=1, page=1),
            "RD": _observation("100", doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:anchor")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "NEGATIVE_VERIFIED")
        self.assertEqual(group.model_version, GENERIC_EXTRACTOR_VERSION)
        self.assertFalse(group.delta["gold_validated"])

    def test_delta_carries_the_site_location_sentinel(self):
        # Regression: `delta` used to omit "location" entirely, so
        # `evidence_group_to_prediction` always exported "" for this
        # mechanism's findings and they could never align with a
        # location-scoped gold check -- see test_case10_evaluation.py's
        # GenericTierLocationExportTests for the full exporter/metrics proof
        # (before: never matches; after: matches).
        from app.db.models import EvidenceGroup

        _upsert_generic_group(self.db, self.process, self.param, {
            "PD": _observation("100", doc_id=1, page=1),
            "RD": _observation("100", doc_id=2, page=1),
        })
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.delta["location"], GENERIC_SITE_LOCATION)
        self.assertNotEqual(group.delta["location"], "")

    def test_different_values_are_candidate_not_auto_confirmed(self):
        from app.db.models import EvidenceGroup

        key = _upsert_generic_group(self.db, self.process, self.param, {
            "PD": _observation("100", doc_id=1, page=1),
            "RD": _observation("90", doc_id=2, page=1),
        })
        self.assertEqual(key, "generic:anchor")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.finding_status, "CANDIDATE")
        self.assertNotEqual(group.finding_status, "CONFIRMED_VIOLATION")

    def test_id_present_is_preferred_over_rd_as_the_actual_value(self):
        from app.db.models import EvidenceGroup

        key = _upsert_generic_group(self.db, self.process, self.param, {
            "PD": _observation("100", doc_id=1, page=1),
            "RD": _observation("100", doc_id=2, page=1),
            "ID": _observation("95", doc_id=3, page=1),
        })
        self.assertEqual(key, "generic:anchor")
        group = self.db.query(EvidenceGroup).filter_by(object_id="OBJ-SYNTHETIC").one()
        self.assertEqual(group.actual_value, "95")
        self.assertEqual(group.finding_status, "CANDIDATE")

    def test_upsert_engine_is_reused_so_a_repeat_run_does_not_duplicate_the_group(self):
        from app.db.models import EvidenceGroup

        stages = {"PD": _observation("100", doc_id=1, page=1), "RD": _observation("100", doc_id=2, page=1)}
        _upsert_generic_group(self.db, self.process, self.param, stages)
        _upsert_generic_group(self.db, self.process, self.param, stages)
        self.assertEqual(self.db.query(EvidenceGroup).count(), 1)

    def test_never_produces_not_applicable_or_missing_evidence(self):
        # This mechanism only ever runs once PD+RD candidates already exist
        # (see collect_generic_observations); when it can't compare, the
        # caller in official_evidence.py falls through to the existing
        # annotation-context MISSING_EVIDENCE/NOT_COMPARABLE path instead of
        # this module inventing a NOT_APPLICABLE/MISSING_EVIDENCE status of
        # its own -- verified across every finding_status this class of test
        # can produce (equal, different, PD-only, RD-only).
        from app.db.models import EvidenceGroup

        cases = [
            {"PD": _observation("100", doc_id=1, page=1), "RD": _observation("100", doc_id=2, page=1)},
            {"PD": _observation("100", doc_id=1, page=1), "RD": _observation("90", doc_id=2, page=1)},
            {"PD": _observation("100", doc_id=1, page=1)},
            {"RD": _observation("100", doc_id=1, page=1)},
        ]
        for i, stages in enumerate(cases):
            param = SimpleNamespace(id=200 + i, code=f"PZ-{200 + i}", review_priority="HIGH", source_pd=True, source_rd=True, source_id=True)
            _upsert_generic_group(self.db, self.process, param, stages)
        statuses = {g.finding_status for g in self.db.query(EvidenceGroup).all()}
        self.assertTrue(statuses <= {"CANDIDATE", "NEGATIVE_VERIFIED"})
        self.assertNotIn("NOT_APPLICABLE", statuses)
        self.assertNotIn("MISSING_EVIDENCE", statuses)


class CollectGenericObservationsTests(unittest.TestCase):
    """No DB needed: `collect_generic_observations` operates on plain
    fragment/document objects passed in by the caller."""

    def _param(self, param_id: int, code: str, name: str, unit: str = "м²") -> SimpleNamespace:
        return SimpleNamespace(id=param_id, code=code, parameter_name=name, unit=unit, source_pd=True, source_rd=True, source_id=False)

    def _fragment(self, doc_id: int, page: int, confidence: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(document_version_id=doc_id, page=page, confidence=confidence)

    def test_shared_page_answers_multiple_parameters_from_one_render(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {1: pd_doc, 2: rd_doc}
        params = [
            self._param(1, "PZ-001", "Площадь застройки"),
            self._param(2, "PZ-002", "Общая площадь здания"),
        ]
        by_code = {
            "PZ-001": [self._fragment(1, 1), self._fragment(2, 1)],
            "PZ-002": [self._fragment(1, 1), self._fragment(2, 1)],
        }
        call_count = {"n": 0}

        def fake_extract(document, pages):
            call_count["n"] += 1
            return {p: _word_snapshot(_TEP_TABLE_TEXT, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = new_generic_budget(pages=100)
            result = collect_generic_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"}, budget=budget,
            )
        # One document open per distinct document, not per (param, stage) pair.
        self.assertEqual(call_count["n"], 2)
        self.assertEqual(budget["pages"], 98)  # one page consumed per document
        self.assertIn(1, result)
        self.assertIn(2, result)
        self.assertEqual(result[1]["PD"].normalized_value, "4650.91")
        self.assertEqual(result[2]["PD"].normalized_value, "11618.27")

    def test_exhausted_budget_yields_no_observations_without_erroring(self):
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        by_id = {1: pd_doc}
        params = [self._param(1, "PZ-001", "Площадь застройки")]
        by_code = {"PZ-001": [self._fragment(1, 1)]}

        with patch("app.domain.cross_stage_localization.extract_original_pages") as mocked:
            budget = new_generic_budget(pages=0)
            result = collect_generic_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"}, budget=budget,
            )
        mocked.assert_not_called()
        self.assertEqual(result, {})

    def test_no_group_created_per_param_beyond_one_bounded_result_each(self):
        # "No candidate explosion": several parameters sharing the same two
        # pages must never produce more than one observation set (PD+RD)
        # each. Real ТЭП tables are not perfectly regular -- a value that
        # happens to be serialized BEFORE its own label in a scrambled
        # multi-column read order (documented in CASE10_MATRIX_132_COVERAGE.md)
        # simply finds nothing rather than a wrong number, which is exactly
        # the safe failure mode this asserts: bounded, never duplicated, and
        # never invented when the pattern does not hold.
        pd_doc = SimpleNamespace(id=1, dataset_stage="PD", doc_stage="project")
        rd_doc = SimpleNamespace(id=2, dataset_stage="RD", doc_stage="working")
        by_id = {1: pd_doc, 2: rd_doc}
        names = [
            "Площадь застройки", "Общая площадь здания", "Расчётная площадь здания",
            "Строительный объём здания",
        ]
        params = [self._param(i, f"PZ-{i:03d}", name) for i, name in enumerate(names, start=1)]
        by_code = {p.code: [self._fragment(1, 1), self._fragment(2, 1)] for p in params}

        def fake_extract(document, pages):
            return {p: _word_snapshot(_TEP_TABLE_TEXT, page=p) for p in pages}

        with patch("app.domain.cross_stage_localization.extract_original_pages", side_effect=fake_extract):
            budget = new_generic_budget(pages=100)
            result = collect_generic_observations(
                params, by_code, by_id, stage_codes={"project": "PD", "working": "RD", "as_built": "ID"}, budget=budget,
            )
        # Never more than PD+RD per parameter, whether or not a hit was found.
        for stages in result.values():
            self.assertLessEqual(len(stages), 2)
        # The reliably-ordered anchors in this real table must still resolve.
        reliable = {"PZ-001": "Площадь застройки", "PZ-002": "Общая площадь здания"}
        by_code_lookup = {p.code: p.id for p in params}
        for code in reliable:
            self.assertIn(by_code_lookup[code], result)


class GenericAnchorExcludesOfficialRuleCodesTests(unittest.TestCase):
    def test_supported_rule_pack_codes_never_enter_the_generic_candidate_pool(self):
        from app.domain.official_rule_packs import SUPPORTED_RULE_CODES

        params = [
            SimpleNamespace(id=1, code="KR-058", unit="мм", parameter_name="Толщина монолитной фундаментной плиты"),
            SimpleNamespace(id=2, code="PZ-002", unit="м²", parameter_name="Общая площадь здания"),
        ]
        result = eligible_params(params, excluded_codes=frozenset(SUPPORTED_RULE_CODES))
        self.assertEqual([p.code for p in result], ["PZ-002"])


if __name__ == "__main__":
    unittest.main()
