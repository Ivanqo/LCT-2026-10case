"""Phase 12 / S1: room / apartment schedule parser and PD <-> RD comparison (synthetic word lists, no PDFs)."""
from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import explication_compare as ec  # noqa: E402
from app.domain import table_parser as tp  # noqa: E402

H = 10.0          # word height
PITCH = 18.0      # row pitch


def word(text: str, x: float, y: float, w: float | None = None) -> dict:
    return {"text": text, "bbox": [x, y, x + (w if w is not None else 6.0 * len(text)), y + H]}


def schedule(rows: list[tuple[str, str, str]], *, title: str = "Экспликация помещений 1-го этажа", total: str | None = None,
             x0: float = 1000.0, y0: float = 100.0, extra: list[dict] | None = None, name_lines: dict[str, list[str]] | None = None) -> dict:
    """A page with one schedule: title, header «Номер | Наименование | Площадь, м²», rows (key, name, area), total."""
    words = [word(t, x0 + 60 + 40 * i, y0) for i, t in enumerate(title.split())]
    header_y = y0 + 30
    words += [word("Номер", x0, header_y), word("Наименование", x0 + 60, header_y), word("Площадь,", x0 + 260, header_y), word("м²", x0 + 312, header_y)]
    y = header_y + 25
    for key, name, area in rows:
        lines = (name_lines or {}).get(key)
        words.append(word(key, x0 + 5, y))
        if lines:                                   # a wrapped name centred on the key line
            top = y - (len(lines) - 1) * H * 1.3 / 2
            for i, line in enumerate(lines):
                for j, token in enumerate(line.split()):
                    words.append(word(token, x0 + 60 + 55 * j, top + i * H * 1.3))
        else:
            for j, token in enumerate(name.split()):
                words.append(word(token, x0 + 60 + 55 * j, y))
        words += [word(area, x0 + 255, y), word("м²", x0 + 300, y)]
        y += PITCH * (1 + (len(lines) - 1) * 0.7 if lines else 1)
    if total is not None:
        words += [word("Итого", x0 + 60, y), word("по", x0 + 100, y), word("этажу", x0 + 120, y), word(total, x0 + 255, y)]
    words += extra or []
    return {"page": 1, "width": 3000.0, "height": 2000.0, "words": words}


ROWS = [("1", "Зона мойки", "165.05"), ("2", "Зона выдачи", "69.10"), ("3", "Сан.узел", "5.35"), ("4", "Помещение", "35.62"),
        ("5", "Тамбур", "17.32"), ("А", "Лестничная клетка", "24.51")]


def ref(table: tp.RoomTable, stage: str, file_id: str, *, doc: int | None = None, rank: tuple = (), volume: str | None = None) -> ec.TableRef:
    return ec.TableRef(table=table, stage=stage, file_id=file_id, doc_key=doc if doc is not None else file_id, revision_rank=rank, volume=volume)


def only(snapshot: dict) -> tp.RoomTable:
    tables = tp.find_room_tables(snapshot)
    assert len(tables) == 1, [t.title for t in tables]
    return tables[0]


class TableParserTests(unittest.TestCase):
    def test_reads_rows_columns_total_and_scope(self):
        table = only(schedule(ROWS, total="316.95"))
        self.assertEqual([r.key for r in table.rows], ["1", "2", "3", "4", "5", "А"])
        self.assertEqual([c.role for c in table.columns], [tp.ROLE_NUMBER, tp.ROLE_NAME, tp.ROLE_AREA])
        values = tp.row_values(table, table.rows[1])
        self.assertEqual(values[tp.ROLE_NAME], "Зона выдачи")
        self.assertEqual(values[tp.ROLE_AREA]["value"], Decimal("69.10"))
        self.assertEqual(table.scope.floor, "1")
        self.assertEqual(tp.total_value(table.totals[0])[0], Decimal("316.95"))
        box = table.bbox_norm()
        self.assertTrue(all(0.0 <= v <= 1.0 for v in box) and box[0] < box[2] and box[1] < box[3])

    def test_decimal_comma_and_scope_words(self):
        table = only(schedule([("101", "Спальня", "56,7"), ("102", "Туалет", "29,7")], title="Экспликация помещений антресольного этажа секция 2"))
        self.assertEqual(tp.row_values(table, table.rows[0])[tp.ROLE_AREA]["value"], Decimal("56.7"))
        self.assertEqual((table.scope.floor, table.scope.section), ("антресоль", "2"))

    def test_wrapped_centred_names_stay_in_their_row(self):
        rows = [("1.108", "Серверная", "17,4"), ("1.109", "", "18,2"), ("1.110", "Коридор", "173,3")]
        table = only(schedule(rows, name_lines={"1.109": ["Зона ожидания", "начальной школы"]}))
        names = {r.key: tp.row_values(table, r).get(tp.ROLE_NAME) for r in table.rows}
        self.assertEqual(names["1.108"], "Серверная")
        self.assertEqual(names["1.109"], "Зона ожидания начальной школы")
        self.assertEqual(names["1.110"], "Коридор")

    def test_drawing_numbers_below_the_table_are_not_rows(self):
        extra = [word("1985", 1005, 100 + 30 + 25 + PITCH * 12), word("470", 1080, 100 + 30 + 25 + PITCH * 12)]
        table = only(schedule(ROWS, extra=extra))
        self.assertNotIn("1985", [r.key for r in table.rows])

    def test_finish_schedule_is_not_a_room_table(self):
        self.assertEqual(tp.find_room_tables(schedule(ROWS, title="Ведомость отделки помещений")), [])

    def test_overprinted_words_are_deduplicated(self):
        snap = schedule(ROWS)
        snap["words"] = snap["words"] + [dict(w, bbox=[v + 0.3 for v in w["bbox"]]) for w in snap["words"]]
        self.assertEqual(len(only(snap).rows), len(ROWS))

    def test_normalize_key(self):
        self.assertEqual(tp.normalize_key("Ст.1.1.1"), tp.normalize_key("1.1.1"))
        self.assertEqual(tp.normalize_key("012"), tp.normalize_key("12"))
        self.assertEqual(tp.normalize_key("1,109"), "1.109")

    def test_garbled_text_layer_is_flagged(self):
        table = only(schedule([("1", "ǖестниȂная клетка", "10.0"), ("2", "ǝамǬǾǻ", "5.0")]))
        self.assertTrue(tp.table_text_is_garbled(table))
        self.assertFalse(tp.table_text_is_garbled(only(schedule(ROWS))))


class CompareTests(unittest.TestCase):
    def results(self, pd_rows, rd_rows, **kw):
        pd = ref(only(schedule(pd_rows, total=kw.get("pd_total"))), "PD", "P-1")
        rd = ref(only(schedule(rd_rows, total=kw.get("rd_total"))), "RD", "R-1")
        return ec.compare_pairs([pd], [rd])

    def kinds(self, results):
        return sorted((d.type, d.location) for r in results for d in r.discrepancies if d.violation)

    def test_identical_tables_have_no_violation(self):
        self.assertEqual(self.kinds(self.results(ROWS, ROWS, pd_total="316.95", rd_total="316.95")), [])

    def test_function_change_area_change_added_removed(self):
        rd = [("1", "Зона мойки", "165.88"),            # +0.5 %: within the 1 % trigger
              ("2", "Зона выдачи", "72.10"),            # +4.3 %
              ("3", "Санузел", "5.35"),                 # same name after normalisation
              ("4", "Раздевалка женская", "35.62"),     # function change
              ("А", "Лестничная клетка", "24.51"),
              ("6", "Комната отдыха", "17.30")]         # 5 removed, 6 added
        self.assertEqual(self.kinds(self.results(ROWS, rd)), [
            (ec.TYPE_AREA_CHANGED, "2"), (ec.TYPE_ONLY_PD, "5"), (ec.TYPE_ONLY_RD, "6"), (ec.TYPE_NAME_CHANGED, "4")])

    def test_typo_is_not_a_function_change(self):
        rd = [(k, "Зона мойкки" if k == "1" else n, a) for k, n, a in ROWS]
        self.assertEqual(self.kinds(self.results(ROWS, rd)), [])

    def test_rounding_unit_is_tolerated(self):
        pd = [("1", "Кладовая", "1,8"), ("2", "Коридор", "29,7")]
        rd = [("1", "Кладовая", "1,9"), ("2", "Коридор", "29,8")]
        self.assertEqual(self.kinds(self.results(pd, rd)), [])

    def test_floor_total_uses_the_catalog_trigger(self):
        results = self.results(ROWS, ROWS, pd_total="316.95", rd_total="318.00")
        total = [d for r in results for d in r.discrepancies if d.type == ec.TYPE_TOTAL][0]
        self.assertFalse(total.violation)                                       # 0.33 % <= 1 %
        results = self.results(ROWS, ROWS, pd_total="316.95", rd_total="330.00")
        self.assertTrue([d for r in results for d in r.discrepancies if d.type == ec.TYPE_TOTAL][0].violation)

    def test_codes_follow_the_mapping(self):
        rd = [(k, "Раздевалка" if k == "4" else n, a) for k, n, a in ROWS]
        codes = {d.code for r in self.results(ROWS, rd, pd_total="1", rd_total="1") for d in r.discrepancies}
        self.assertEqual(codes, {ec.CODE_ROOMS, ec.CODE_TOTAL})

    def test_floors_do_not_cross_pair(self):
        pd1 = ref(only(schedule(ROWS, title="Экспликация помещений 1-го этажа")), "PD", "P")
        rd2 = ref(only(schedule([(k, n + " 2", a) for k, n, a in ROWS], title="Экспликация помещений 2-го этажа")), "RD", "R")
        self.assertEqual(ec.compare_pairs([pd1], [rd2]), [])

    def test_excerpt_does_not_report_missing_rooms(self):
        rd = ROWS[:2]
        self.assertEqual([d for r in self.results(ROWS, rd) for d in r.discrepancies if d.type == ec.TYPE_ONLY_PD], [])

    def test_newest_pd_edition_is_the_reference(self):
        old = ref(only(schedule([(k, n, "10.00" if k == "2" else a) for k, n, a in ROWS])), "PD", "P-old", doc=1, rank=(2022, 0, 0))
        new = ref(only(schedule(ROWS)), "PD", "P-new", doc=2, rank=(2025, 2, 0))
        rd = ref(only(schedule([(k, n, "10.00" if k == "2" else a) for k, n, a in ROWS])), "RD", "R", doc=3)
        results = ec.compare_pairs([old, new], [rd], conflicting={1: {2}, 2: {1}})
        self.assertEqual(results[0].pd.file_id, "P-new")
        self.assertEqual(self.kinds(results), [(ec.TYPE_AREA_CHANGED, "2")])

    def test_unordered_editions_are_checked_robustly(self):
        a = ref(only(schedule([(k, n, "10.00" if k == "2" else a) for k, n, a in ROWS])), "PD", "P-a", doc=1)
        b = ref(only(schedule(ROWS)), "PD", "P-b", doc=2)
        rd = ref(only(schedule([(k, n, "10.00" if k == "2" else a) for k, n, a in ROWS])), "RD", "R", doc=3)
        results = ec.compare_pairs([a, b], [rd], conflicting={1: {2}, 2: {1}})
        self.assertEqual(self.kinds(results), [])                                 # one unordered edition agrees with RD

    def test_older_rd_edition_of_the_same_volume_is_not_compared(self):
        pd = ref(only(schedule(ROWS)), "PD", "P", doc=1)
        rd_old = ref(only(schedule([(k, n, "99.00" if k == "3" else a) for k, n, a in ROWS])), "RD", "R-old", doc=2, rank=(0, 0, 0), volume="АР1")
        rd_new = ref(only(schedule(ROWS)), "RD", "R-new", doc=3, rank=(0, 3, 0), volume="АР1")
        results = ec.compare_pairs([pd], [rd_old, rd_new])
        self.assertEqual([r.rd.file_id for r in results], ["R-new"])

    def test_same_table_on_two_sheets_is_one_comparison(self):
        pd = ref(only(schedule(ROWS)), "PD", "P", doc=1)
        rd_rows = [(k, n, "72.10" if k == "2" else a) for k, n, a in ROWS]
        sheet4, sheet11 = only(schedule(rd_rows)), only(schedule(rd_rows))
        sheet4.page, sheet11.page = 4, 11
        results = ec.compare_pairs([pd], [ref(sheet4, "RD", "R", doc=2), ref(sheet11, "RD", "R", doc=2)])
        self.assertEqual(len(results), 1)
        self.assertEqual([c.page for c in results[0].rd_copies], [11])

    def test_flag_default_off(self):
        old = os.environ.pop("CASE10_EXPLICATION_COMPARE", None)
        try:
            self.assertFalse(ec.explication_compare_enabled())
            os.environ["CASE10_EXPLICATION_COMPARE"] = "1"
            self.assertTrue(ec.explication_compare_enabled())
        finally:
            os.environ.pop("CASE10_EXPLICATION_COMPARE", None)
            if old is not None:
                os.environ["CASE10_EXPLICATION_COMPARE"] = old


class PipelineGroupsTests(unittest.TestCase):
    """collect_explication_groups on a real (in-memory) process: groups, fragments, export, idempotent re-run."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.models import Base, DocumentVersion
        from app.domain.official_dataset import MATRIX_VERSION_OFFICIAL
        from app.domain.v3_pipeline import create_process, list_active_params
        from unittest.mock import patch

        self.worker_env = patch.dict(os.environ, {"CASE10_EXPLICATION_WORKERS": "1"})
        self.worker_env.start()
        self.addCleanup(self.worker_env.stop)
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = create_process(self.db, project_id=1, organization_id=1, object_id="OBJ-S1", matrix_version=MATRIX_VERSION_OFFICIAL)
        self.params = list_active_params(self.db, organization_id=1, project_id=1, matrix_version=MATRIX_VERSION_OFFICIAL)
        self.docs = []
        for index, (stage, name) in enumerate((("PD", "Проектная документация/3. П-2025-01-АР.pdf"), ("RD", "Рабочая документация/РД-2025-01-АР2.pdf")), start=1):
            doc = DocumentVersion(project_id=1, organization_id=1, source_type="case10_dataset", filename=name.split("/")[-1],
                                  dataset_stage=stage, dataset_file_id=f"OBJ-{index:06d}", file_hash=f"sha{index}",
                                  dataset_metadata={"document_manifest": {"relative_path": name, "pdf_pages": 30}})
            self.db.add(doc)
            self.db.flush()
            self.docs.append(doc)
        rd_rows = [(k, "Раздевалка женская" if k == "4" else n, a) for k, n, a in ROWS]
        self.scans = {"sha1": self.scan(ROWS, 19), "sha2": self.scan(rd_rows, 4)}

    @staticmethod
    def scan(rows, page):
        table = only(schedule(rows, total="316.95"))
        table.page = page
        return {"pages_total": 30, "candidate_pages": [page], "tables": [table.to_dict()]}

    def run_once(self):
        from unittest.mock import patch

        with patch.object(ec, "scan_document_tables", side_effect=lambda doc: self.scans[doc.file_hash]):
            return ec.collect_explication_groups(self.db, self.process, self.params, self.docs)

    def groups(self):
        from app.db.models import EvidenceGroup
        from app.domain.v3_pipeline import evidence_group_to_dict

        rows = self.db.query(EvidenceGroup).filter(EvidenceGroup.group_key.like("explication:%")).all()
        return [evidence_group_to_dict(self.db, g) for g in rows]

    def test_groups_fragments_and_export(self):
        from evaluation.exporter import evidence_group_to_submission_check

        diagnostics = self.run_once()
        self.assertEqual((diagnostics["pairs"], diagnostics["violations"]), (1, 1))
        groups = self.groups()
        violation = [g for g in groups if g["finding_status"] == "CANDIDATE"]
        self.assertEqual(len(violation), 1)
        check = evidence_group_to_submission_check(violation[0], code_style="matrix11")
        self.assertEqual((check["parameter_code"], check["location"], check["violation_label"]), ("M-003", "4", "VIOLATION_PRESENT"))
        self.assertEqual((check["pd_value"], check["rd_value"]), ("Помещение", "Раздевалка женская"))
        cited = {(e["stage"], e["file_id"], e["page"], e["role"]) for e in check["evidence"]}
        self.assertEqual(cited, {("PD", "OBJ-000001", 19, "expected"), ("PD", "OBJ-000001", 19, "table"),
                                 ("RD", "OBJ-000002", 4, "actual"), ("RD", "OBJ-000002", 4, "table")})
        self.assertTrue(all(e["bbox_norm"] and all(0 <= v <= 1 for v in e["bbox_norm"]) for e in check["evidence"]))
        total = [g for g in groups if g["delta"]["discrepancy_type"] == ec.TYPE_TOTAL]
        self.assertEqual([g["finding_status"] for g in total], ["NEGATIVE_VERIFIED"])

    def test_rerun_is_idempotent_and_keeps_decisions(self):
        from app.domain.v3_pipeline import record_inspector_decision

        self.run_once()
        first = {g["group_key"]: g["id"] for g in self.groups()}
        candidate = next(g for g in self.groups() if g["finding_status"] == "CANDIDATE")
        record_inspector_decision(self.db, evidence_group_id=candidate["id"], decision="Confirm", reason_code=None, comment="ok", user_id=None)
        self.run_once()
        second = {g["group_key"]: g for g in self.groups()}
        self.assertEqual(set(first), set(second))
        self.assertEqual(second[candidate["group_key"]]["id"], candidate["id"])
        self.assertEqual(second[candidate["group_key"]]["inspector_status"], "CONFIRMED")

    def test_official_evidence_calls_explication_before_logical_analysis(self):
        from unittest.mock import patch

        from app.domain import official_evidence as oe
        from app.domain.comparison_gate import GateContext

        events = []
        captured = {}

        def collect(db, process, params, docs, **kwargs):
            events.append("explication")
            captured.update(docs=docs, **kwargs)
            return {"pairs": 1}

        def logical(db, process, params, touched_keys, **kwargs):
            events.append("logical")
            captured["logical_touched_keys"] = touched_keys

        empty_extraction = ({}, {"documents": [], "scanned_pages": set()})
        with (
            patch.object(oe, "explication_compare_enabled", return_value=True),
            patch.object(oe, "collect_explication_groups", side_effect=collect),
            patch.object(oe, "extract_official_rule_observations", return_value=empty_extraction),
            patch.object(oe, "collect_generic_observations", return_value={}),
            patch.object(oe, "collect_enum_observations", return_value={}),
            patch.object(oe, "collect_compound_observations", return_value={}),
            patch.object(oe, "collect_table_count_observations", return_value={}),
            patch.object(oe, "run_logical_analysis_module", side_effect=logical),
        ):
            oe.create_official_evidence_groups(self.db, self.process, [], self.docs)

        self.assertEqual(events, ["explication", "logical"])
        self.assertEqual(captured["docs"], self.docs)
        self.assertIsInstance(captured["gate"], GateContext)
        self.assertIs(captured["touched_keys"], captured["logical_touched_keys"])

    def test_group_ids_survive_the_official_evidence_sweep(self):
        from unittest.mock import patch
        from app.domain.evidence_groups import sweep_orphaned_evidence_groups

        param_ids = {int(param.id) for param in self.params}
        first_touched = {}
        with patch.object(ec, "scan_document_tables", side_effect=lambda doc: self.scans[doc.file_hash]):
            ec.collect_explication_groups(self.db, self.process, self.params, self.docs, touched_keys=first_touched)
        first = {g["group_key"]: g["id"] for g in self.groups()}
        sweep_orphaned_evidence_groups(self.db, self.process, param_ids, first_touched)

        second_touched = {}
        with patch.object(ec, "scan_document_tables", side_effect=lambda doc: self.scans[doc.file_hash]):
            ec.collect_explication_groups(self.db, self.process, self.params, self.docs, touched_keys=second_touched)
        sweep_orphaned_evidence_groups(self.db, self.process, param_ids, second_touched)
        second = {g["group_key"]: g["id"] for g in self.groups()}

        self.assertEqual(first, second)

    def test_a_vanished_discrepancy_is_swept(self):
        self.run_once()
        self.scans["sha2"] = self.scan(ROWS, 4)
        self.run_once()
        self.assertEqual([g for g in self.groups() if g["finding_status"] == "CANDIDATE"], [])


if __name__ == "__main__":
    unittest.main()
