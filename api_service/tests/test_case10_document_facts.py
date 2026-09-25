"""Document facts (Phase 10, prompt B, items 2(б) and 4): section/series/revision parsing, byte duplicates,
unreadable and service files, ordered vs conflicting editions.

File names below are real names from the participant packages (no label of any kind is involved): they are
the smallest honest fixtures for the naming habits the parser has to survive.
"""
from __future__ import annotations

import unittest

from app.domain import document_facts as df


def _row(file_id, stage, relative_path, *, section="OTHER", sha=None, pages=10, ext=".pdf", size=1000, **extra):
    return {"file_id": file_id, "stage": stage, "relative_path": relative_path, "section": section,
            "sha256": sha or f"sha-{file_id}", "pdf_pages": pages, "extension": ext, "size_bytes": size, **extra}


def _facts(rows):
    return [df.facts_from_manifest_row(r) for r in rows]


class NameParsingTests(unittest.TestCase):
    def test_section_and_series_of_a_project_cipher(self):
        f = df.facts_from_manifest_row(_row("A1", "PD", "пд/133-0820-ОК-1-АР_v5.pdf"))
        self.assertEqual(f.section_key, "АР")
        self.assertIn("АР", f.tags)
        self.assertEqual(f.family, "1330820ОК1")
        self.assertEqual(dict(f.markers).get("ver"), 5)

    def test_engineering_section_volumes_expand_to_their_discipline(self):
        f = df.facts_from_manifest_row(_row("A1", "PD", "пд/133-0820-ОК-1-ИОС4.2 Корр. 4.pdf"))
        self.assertEqual(f.section_key, "ИОС4.2")
        self.assertTrue({"ИОС4", "ОВ"} <= f.tags)
        self.assertEqual(dict(f.markers).get("korr"), 4)

    def test_working_drawing_marks_map_to_the_structural_discipline(self):
        for name in ("133-0820-ОК-1-КЖ1_изм.1 ЭЦП 19.12.2025.pdf", "П-2025-04-266-КМ 02.07.2026.pdf"):
            self.assertIn("КР", df.facts_from_manifest_row(_row("A", "RD", name)).tags, name)

    def test_cipher_fragments_are_never_read_as_dates_or_years(self):
        # "01_07.22" is a cipher, "0624-2024" is a cipher: neither is a revision marker
        f = df.facts_from_manifest_row(_row("A", "PD", "01_07.22_14_П_АР1.pdf"))
        self.assertEqual(f.markers, ())
        g = df.facts_from_manifest_row(_row("B", "PD", "5.4. Раздел ЖС-РЛ-0624-2024-П-ИОС4 13022025.pdf"))
        self.assertFalse([m for m in g.markers if m[0] == "year"])

    def test_cipher_spelling_variants_share_one_family(self):
        names = ["01-07-22-14-П-ОПЗ.pdf", "01-0722-14-П-ПЗУ.pdf", "01_07.22_14_П_АР1.pdf", "01-07-22-П-14-ПОС-кор3.pdf"]
        families = {df.facts_from_manifest_row(_row(f"F{i}", "PD", n)).family for i, n in enumerate(names)}
        self.assertEqual(families, {"01072214"})

    def test_short_cipher_is_not_mistaken_for_section_numbering(self):
        # "23.009" is the project cipher; "5.4. " / "10.2. " / "5.1.П-" are the package's section numbering
        self.assertEqual(df.facts_from_manifest_row(_row("A", "RD", "23.009-Р-П-КЖ_ГИ-изм1.pdf")).family, "23009")
        self.assertEqual(df.facts_from_manifest_row(_row("B", "PD", "5.1.П-2025-04.266-ИОС1.2-ИТП.ЭОМ (1).pdf")).family, "202504266")
        self.assertEqual(df.facts_from_manifest_row(_row("C", "PD", "10.2. Раздел 12 ЖС-РД-270121-П-БЭО.pdf")).family, "ЖС270121")
        # "Р-1" is part 1 of the working documentation, not another project series
        self.assertEqual(df.facts_from_manifest_row(_row("D", "RD", "23.009-Р-1-КЖ0.1.pdf")).family, "23009")

    def test_building_number_is_not_part_of_the_series(self):
        a = df.facts_from_manifest_row(_row("A", "RD", "01-0723-14-РД-К1-КЖ4.2.pdf"))
        b = df.facts_from_manifest_row(_row("B", "RD", "01-0723-14-РД-К2-КЖ4.2.pdf"))
        self.assertEqual(a.family, b.family)
        self.assertEqual((a.building, b.building), ("1", "2"))

    def test_a_single_sheet_suffix_is_part_of_the_section_key(self):
        f = df.facts_from_manifest_row(_row("A", "RD", "01-0723-14-РД-К1-КЖ4.2_6.pdf"))
        self.assertEqual(f.section_key, "КЖ4.2_6")

    def test_correction_and_variant_markers(self):
        f = df.facts_from_manifest_row(_row("A", "PD", "133-0820-ОК-1-ПЗ2_(Корр.1) РнС.pdf"))
        self.assertEqual(dict(f.markers).get("korr"), 1)
        self.assertIn("РНС", f.variant_tags)
        g = df.facts_from_manifest_row(_row("B", "PD", "01-07-22-14-П-ОПЗ-кор3.pdf"))
        self.assertEqual(dict(g.markers).get("korr"), 3)

    def test_working_drawing_marks_of_engineering_sets(self):
        cases = {
            "19-0322-ОК-1_Н-МЗ. Молниезащита уравнивание потенциалов_изм.3.pdf": "ЭОМ",
            "19-0322-ОК-1_Н-НК2. Наружные сети дождевой канализации_ИЗМ.2.pdf": "ВК",
            "01-07.22-14-П-ИОС 4.4 ТС.pdf": "ОВ",
            "133-0820-ОК-1-АИ1.pdf": "АР",
        }
        for name, tag in cases.items():
            self.assertIn(tag, df.facts_from_manifest_row(_row("A", "RD", name)).tags, name)
        # a series cipher containing the letters is not a section mark
        self.assertNotIn("ВК", df.facts_from_manifest_row(_row("B", "PD", "НВС-2025.03-3-АР.pdf")).tags)

    def test_unknown_and_non_section_files_are_told_apart(self):
        survey = df.facts_from_manifest_row(_row("A", "PD", "02-002-21 ИГИ1 корр 2.pdf"))
        unknown = df.facts_from_manifest_row(_row("B", "PD", "56138 рс.pdf"))
        self.assertEqual(survey.doc_class, "OTHER_KNOWN")
        self.assertEqual(unknown.doc_class, "UNKNOWN")

    def test_manifest_section_wins_where_the_name_says_nothing(self):
        f = df.facts_from_manifest_row(_row("A", "PD", "V2_01-05-04-02-07_Том 5.4.2 ОВ (1).pdf", section="OV"))
        self.assertIn("ОВ", f.tags)


class HintTests(unittest.TestCase):
    def test_hint_reads_only_whole_upper_case_section_words(self):
        # the older parser produced 'ОВ' from the lower-case word "фасадов"
        self.assertEqual(df.hint_tags("Раздел АР/КР: Чертежи фасадов и разрезов"), frozenset({"АР", "КР"}))

    def test_hint_with_engineering_volume_and_alias(self):
        self.assertEqual(df.hint_tags("Принципиальные схемы отопления (ИОС4)"), frozenset({"ИОС4", "ОВ"}))
        self.assertEqual(df.hint_tags('Раздел ПП (ГП): Лист "Общие данные", Таблица ТЭП'), frozenset({"ГП", "ПЗУ"}))

    def test_hint_without_a_recognisable_section_is_unconstrained(self):
        self.assertEqual(df.hint_tags("Технический план БТИ; Акт выноса осей"), frozenset())
        self.assertEqual(df.hint_tags(None), frozenset())


class IntegrityTests(unittest.TestCase):
    def test_exact_duplicates_within_a_stage_collapse_to_the_lowest_file_id(self):
        rows = [_row("ALT-000003", "PD", "a/ОПЗ.pdf", sha="S1"), _row("ALT-000002", "PD", "a/ОПЗ 2024.pdf", sha="S1"),
                _row("ALT-000010", "PD", "a/АР.pdf", sha="S2")]
        report = df.analyze_integrity(_facts(rows))
        self.assertEqual([(e["file_id"], e["reason"], e["duplicate_of"]) for e in report.excluded],
                         [("ALT-000003", df.REASON_EXACT_DUPLICATE, "ALT-000002")])

    def test_natural_order_is_used_for_the_canonical_pick(self):
        rows = [_row("F0100", "PD", "a.pdf", sha="S"), _row("F0099", "PD", "b.pdf", sha="S"), _row("F0101", "PD", "c.pdf", sha="S")]
        report = df.analyze_integrity(_facts(rows))
        self.assertEqual({e["file_id"] for e in report.excluded}, {"F0100", "F0101"})

    def test_identical_files_in_different_stages_are_kept_but_reported(self):
        rows = [_row("A1", "RD", "rd/x.pdf", sha="S"), _row("A2", "ID", "id/x.pdf", sha="S")]
        report = df.analyze_integrity(_facts(rows))
        self.assertEqual(report.excluded, [])
        self.assertEqual(len(report.cross_stage_duplicates), 1)
        self.assertEqual(report.cross_stage_duplicates[0]["stages"], ["ID", "RD"])

    def test_empty_unreadable_and_service_files_are_excluded_without_failing(self):
        rows = [
            _row("A1", "PD", "8. Раздел 8 ООС.pdf", pages=0),
            _row("A2", "ID", "ИД/Thumbs.db", ext=".db", pages=None),
            _row("A3", "ID", "ИД/~$АОСР №1.docx", ext=".docx", pages=None),
            _row("A4", "ID", "ИД/чертёж.dwl2", ext=".dwl2", pages=None),
            _row("A5", "PD", "empty.pdf", pages=3, size=0),
            _row("A6", "PD", "ok.pdf"),
        ]
        report = df.analyze_integrity(_facts(rows))
        reasons = {e["file_id"]: e["reason"] for e in report.excluded}
        self.assertEqual(reasons, {"A1": df.REASON_UNREADABLE, "A2": df.REASON_SERVICE, "A3": df.REASON_SERVICE,
                                   "A4": df.REASON_SERVICE, "A5": df.REASON_UNREADABLE})
        self.assertEqual(report.to_dict()["excluded_count"], 5)

    def test_manifest_flagged_exclusion_is_honoured(self):
        report = df.analyze_integrity(_facts([_row("F0149", "PD", "x.pdf", distribution_status="EXCLUDE")]))
        self.assertEqual(report.excluded[0]["reason"], df.REASON_ORGANIZER_EXCLUDE)

    def test_non_pdf_formats_are_noted_not_dropped(self):
        report = df.analyze_integrity(_facts([_row("A1", "RD", "x.dwg", ext=".dwg", pages=None), _row("A2", "RD", "y.zip", ext=".zip", pages=None)]))
        self.assertEqual(report.excluded, [])
        self.assertEqual(report.to_dict()["non_pdf_files"], 2)


class RevisionTests(unittest.TestCase):
    def _report(self, rows):
        facts = _facts(rows)
        return df.analyze_revisions(facts, exclude_keys=df.analyze_integrity(facts).excluded_keys)

    def test_orderable_corrections_resolve_to_the_newest(self):
        # Речников habit: original + кор2 + кор3 of the same section
        rows = [_row("F1", "PD", "01-0722-14-П-ПЗУ.pdf", section="GP"),
                _row("F2", "PD", "01-07.22-14-П-ПЗУ_кор1.pdf", section="GP"),
                _row("F3", "PD", "01-07.22-14-П-ПЗУ-кор.3(1).pdf", section="GP")]
        report = self._report(rows)
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.superseded, {"F1": "F3", "F2": "F3"})

    def test_numbered_changes_of_a_short_cipher_resolve_to_the_newest(self):
        rows = [_row("K1", "RD", "23.009-Р-П-КЖ_ГИ-изм1.pdf"), _row("K3", "RD", "23.009-Р-П-КЖ_ГИ-изм3 (1).pdf"),
                _row("K0", "RD", "23.009-Р-П-КЖ_ГИ.pdf")]
        report = self._report(rows)
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.superseded, {"K0": "K3", "K1": "K3"})

    def test_unorderable_variants_are_a_conflict(self):
        rows = [_row("P1", "PD", "133-0820-ОК-1-ПЗ2_(Корр.1) РнС.pdf"), _row("P2", "PD", "133-0820-ОК-1-ПЗ2_МГЭ_РнИ+.pdf")]
        report = self._report(rows)
        self.assertEqual([(c.type, c.scope) for c in report.conflicts], [(df.CONFLICT_UNORDERED, "ПЗ2")])
        self.assertEqual(report.superseded, {})

    def test_mixed_marker_kinds_are_a_conflict(self):
        rows = [_row("O1", "PD", "01-07-22-14-П-ОПЗ Изм 2.pdf"), _row("O2", "PD", "01-07-22-14-П-ОПЗ-кор3.pdf"),
                _row("O3", "PD", "01-07-22-14-П-ОПЗ.pdf")]
        self.assertEqual(len(self._report(rows).conflicts), 1)

    def test_two_files_claiming_the_same_revision_are_a_conflict(self):
        rows = [_row("O1", "PD", "01-07-22-П-14-ПОС-кор3.pdf"), _row("O2", "PD", "01-0722-14-П-ПОС-кор3.pdf")]
        self.assertEqual(len(self._report(rows).conflicts), 1)

    def test_buildings_are_not_revisions_of_each_other(self):
        rows = [_row("B1", "RD", "01-0723-14-РД-К1-КЖ4.2.pdf"), _row("B2", "RD", "01-0723-14-РД-К2-КЖ4.2.pdf")]
        self.assertEqual(self._report(rows).conflicts, [])

    def test_a_pdf_and_its_archive_are_not_two_editions(self):
        rows = [_row("G1", "RD", "01-07_23-14-РД-ГП.pdf", section="GP"), _row("G2", "RD", "01-07_23-14-РД-ГП.zip", section="GP", ext=".zip", pages=None)]
        self.assertEqual(self._report(rows).conflicts, [])

    def test_two_project_series_for_one_section_are_mixed_baselines(self):
        rows = []
        for i in range(3):   # a series only counts with >= 3 files
            rows.append(_row(f"N{i}", "PD", f"П-2025-04.266-ИОС5.{i + 1}.pdf"))
            rows.append(_row(f"O{i}", "PD", f"5.1. Раздел ЖС-РД-270121-П-ИОС5.{i + 1}.pdf"))
        conflicts = [c for c in self._report(rows).conflicts if c.type == df.CONFLICT_MIXED_SERIES]
        self.assertEqual([c.scope for c in conflicts], ["ИОС5"])
        self.assertEqual(len(conflicts[0].file_ids), 6)

    def test_path_generation_folders_are_two_baselines(self):
        rows = [_row("D1", "PD", "пд/ПД 2022/01-03-00-01-12 Пол25 АР.pdf"), _row("D2", "PD", "пд/ПД 2025/281025_АР_Полярная_ред_9.pdf")]
        self.assertEqual([c.type for c in self._report(rows).conflicts], [df.CONFLICT_MIXED_SERIES])

    def test_a_duplicate_is_not_a_second_edition(self):
        rows = [_row("A1", "PD", "1. ЖС-РД-270121-П-ОПЗ 2024.pdf", sha="S"), _row("A2", "PD", "1. ЖС-РД-270121-П-ОПЗ.pdf", sha="S")]
        self.assertEqual(self._report(rows).conflicts, [])

    def test_conflicts_for_a_file(self):
        rows = [_row("P1", "PD", "133-0820-ОК-1-ПЗ2_(Корр.1) РнС.pdf"), _row("P2", "PD", "133-0820-ОК-1-ПЗ2_МГЭ_РнИ+.pdf"),
                _row("P3", "PD", "133-0820-ОК-1-КР1.pdf")]
        report = self._report(rows)
        self.assertTrue(report.conflicts_for("P1"))
        self.assertFalse(report.conflicts_for("P3"))


class InventoryTests(unittest.TestCase):
    def test_mixed_stage_files_count_for_rd_and_id_and_excluded_files_do_not_count(self):
        facts = _facts([_row("A", "RD_ID_MIXED", "x.pdf"), _row("B", "PD", "y.pdf"), _row("C", "PD", "z.pdf")])
        inventory = df.stage_inventory(facts, exclude_keys={"C"})
        self.assertEqual({k: len(v) for k, v in inventory.items()}, {"PD": 1, "RD": 1, "ID": 1})


if __name__ == "__main__":
    unittest.main()
