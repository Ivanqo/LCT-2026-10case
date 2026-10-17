"""Ventilation rule pack on top of room identity: same number on two floors, duplicate keys, ambiguity."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.domain import official_rule_packs as packs
from app.domain.room_drawing_compare import detect_sheet_context, floors_relation

CODE = "IOS4-078"


def _doc(doc_id: int, stage: str):
    return SimpleNamespace(id=doc_id, dataset_stage=stage, doc_stage=None, dataset_file_id=f"F{doc_id}",
                           dataset_metadata={}, discipline="OV", document_code=None, revision=None,
                           approval_status=None, file_hash=f"h{doc_id}", content_hash=f"h{doc_id}", filename=f"{doc_id}.pdf")


def _sheet(page: int, title: str, rooms: dict[str, tuple[float, float, list[str]]]):
    """rooms: number -> (x, y, system labels printed right above the number)."""
    words = []
    for number, (x, y, labels) in rooms.items():
        words.append({"text": number, "bbox": [x, y, x + 18.0, y + 12.0]})
        for index, label in enumerate(labels):
            words.append({"text": label, "bbox": [x, y - 14.0 * (index + 1), x + 22.0, y - 14.0 * (index + 1) + 11.0]})
    return {"page": page, "width": 1000.0, "height": 500.0, "text": f"{title} воздуховод " + " ".join(w["text"] for w in words),
            "words": words}


def _run(pd_sheets, rd_sheets):
    documents, snapshots, candidates = {}, {}, {}
    for stage, sheets, base in (("PD", pd_sheets, 10), ("RD", rd_sheets, 20)):
        for offset, sheet in enumerate(sheets):
            document = _doc(base + offset, stage)
            documents[document.id] = document
            snapshots[(document.id, sheet["page"])] = sheet
            if stage == "PD":
                candidates.setdefault((document.id, CODE), {})[sheet["page"]] = []
    report = packs.new_room_identity_report()
    with mock.patch.object(packs, "_ocr_room_system_words", return_value=([], "")):
        observations = packs._extract_ventilation_observations(CODE, documents, candidates, snapshots, report)
    return observations, report


class RulePackIdentityTests(unittest.TestCase):
    def test_same_room_number_on_two_floors_gives_two_independent_pairs(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])}),
              _sheet(2, "План 2 этажа", {"101": (200, 150, ["В2.1"])})]
        rd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])}),
              _sheet(2, "План 2 этажа", {"101": (200, 150, ["В2.2"])})]
        observations, report = _run(pd, rd)
        pairs = {}
        for obs in observations:
            pairs.setdefault(obs.location, {})[obs.stage] = obs.normalized_value
        self.assertEqual(pairs, {
            "101 (этаж 1)": {"PD": "V1.1", "RD": "V1.1"},
            "101 (этаж 2)": {"PD": "V2.1", "RD": "V2.2"},
        })
        self.assertEqual(report["issues"], [])

    def test_unique_number_keeps_its_printed_location(self):
        observations, _ = _run([_sheet(1, "План 1 этажа", {"140": (200, 150, ["В2.7"])})],
                               [_sheet(1, "План 1 этажа", {"140": (200, 150, ["В2.7"])})])
        self.assertEqual({o.location for o in observations}, {"140"})

    def test_conflicting_project_records_stop_the_comparison(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])}),
              _sheet(2, "План 1 этажа", {"101": (200, 150, ["В1.2"])})]
        rd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])})]
        observations, report = _run(pd, rd)
        self.assertEqual(observations, [])
        self.assertEqual([(i["reason"], i["status"]) for i in report["issues"]],
                         [("DUPLICATE_ROOM_KEY", "CLARIFICATION_REQUIRED")])
        self.assertEqual(len(report["issues"][0]["sources"]), 2)

    def test_observations_are_independent_of_sheet_order(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])}),
              _sheet(2, "План 2 этажа", {"101": (200, 150, ["В2.1"])})]
        rd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.1"])}),
              _sheet(2, "План 2 этажа", {"101": (200, 150, ["В2.2"])})]
        first, _ = _run(pd, rd)
        second, _ = _run(list(reversed(pd)), list(reversed(rd)))
        view = lambda rows: sorted((o.location, o.stage, o.normalized_value) for o in rows)
        self.assertEqual(view(first), view(second))

    def test_added_system_is_reported_without_a_violation_claim(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В2.7"])})]
        rd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В2.7", "В2.8"])})]
        observations, report = _run(pd, rd)
        self.assertEqual(len(report["added_systems"]), 1)
        added = report["added_systems"][0]
        self.assertEqual((added["added_systems"], added["violation_claimed"], added["type"]),
                         (["V2.8"], False, "added"))
        self.assertFalse(packs.rule_is_violation(CODE, "V2.7", "V2.7,V2.8"))

    def test_ambiguous_expected_label_is_not_reported_as_disappeared(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В1.2"])})]
        rd_words = [
            {"text": "101", "bbox": [200.0, 150.0, 218.0, 162.0]},
            {"text": "102", "bbox": [200.0, 230.0, 218.0, 242.0]},
            {"text": "В1.2", "bbox": [200.0, 190.0, 222.0, 201.0]},          # midway between 101 and 102
        ]
        rd = [{"page": 1, "width": 1000.0, "height": 500.0, "text": "План 1 этажа воздуховод", "words": rd_words}]
        # the legacy clip-text fallback would read the nearby label straight from the page text; switch it off
        # to look at the linkage rule itself
        with mock.patch.object(packs, "_extract_exhaust_systems", return_value=""),                 mock.patch.object(packs, "_text_in_bbox", return_value="text"):
            observations, report = _run(pd, rd)
        self.assertEqual([o.stage for o in observations], ["PD"])
        self.assertEqual([i["reason"] for i in report["issues"]], ["AMBIGUOUS_SYSTEM_LINK"])
        self.assertEqual(report["issues"][0]["unresolved_systems"], ["V1.2"])

    def test_ambiguous_label_that_cannot_change_the_verdict_does_not_block_it(self):
        pd = [_sheet(1, "План 1 этажа", {"101": (200, 150, ["В2.7", "В2.8", "В2.9"])})]
        rd_words = [
            {"text": "101", "bbox": [200.0, 150.0, 218.0, 162.0]},
            {"text": "102", "bbox": [200.0, 230.0, 218.0, 242.0]},
            {"text": "В2.9", "bbox": [200.0, 190.0, 222.0, 201.0]},          # midway between 101 and 102
        ]
        rd = [{"page": 1, "width": 1000.0, "height": 500.0, "text": "План 1 этажа воздуховод", "words": rd_words}]
        with mock.patch.object(packs, "_extract_exhaust_systems", return_value=""),                 mock.patch.object(packs, "_text_in_bbox", return_value="text"):
            observations, report = _run(pd, rd)
        self.assertEqual([o.stage for o in observations], ["PD", "RD"])
        self.assertEqual(observations[1].normalized_value, packs._MISSING_EXHAUST)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["ambiguity_not_decisive"][0]["unresolved_systems"], ["V2.9"])

    def test_sheet_overlap_chooses_between_project_sheets_without_floor(self):
        shared = {f"{n}05": (60.0 + 70 * n, 300, []) for n in range(1, 7)}
        pd_near = _sheet(1, "Схема", {"012": (200, 150, ["В3.1"]), **shared})
        pd_far = _sheet(2, "Схема", {"012": (200, 150, ["В3.2"]), "301": (500, 300, []), "302": (600, 300, [])})
        rd = _sheet(1, "Схема", {"012": (200, 150, ["В3.1"]), **shared})
        observations, report = _run([pd_near, pd_far], [rd])
        by_stage = {(o.location, o.stage): o.normalized_value for o in observations}
        self.assertEqual(by_stage[("012", "PD")], "V3.1")
        self.assertEqual(by_stage[("012", "RD")], "V3.1")
        self.assertEqual([r["resolution"] for r in report["room_resolutions"]], ["sheet_overlap", "lost_to_overlap"])
        self.assertEqual(report["issues"], [])

    def test_without_floor_or_overlap_evidence_nobody_is_chosen(self):
        pd = [_sheet(1, "Схема", {"012": (200, 150, ["В3.1"])}), _sheet(2, "Схема", {"012": (200, 150, ["В3.2"])})]
        rd = [_sheet(1, "Схема", {"012": (200, 150, ["В3.1"])})]
        observations, report = _run(pd, rd)
        self.assertEqual({o.stage for o in observations}, {"PD"})
        self.assertEqual([i["reason"] for i in report["issues"]], ["AMBIGUOUS_ROOM_MATCH", "AMBIGUOUS_ROOM_MATCH"])


class SheetFloorTextTests(unittest.TestCase):
    def test_building_height_and_legend_text_do_not_define_a_floor(self):
        for text in ("Здание 25 этажей, 9-этажная секция", "Кабинет иностранного языка 2 этаж",
                     "Схема приточных установок 1 этаж"):
            self.assertIsNone(detect_sheet_context({"text": text, "words": []})["floor"], text)

    def test_explication_and_plan_titles_define_it(self):
        self.assertEqual(detect_sheet_context({"text": "Экспликация помещений 3 этажа", "words": []})["floor"], "3")
        self.assertEqual(detect_sheet_context({"text": "Экспликация помещений подвала на отм.", "words": []})["floor"], "подвал")
        self.assertEqual(detect_sheet_context({"text": "План вентиляции 1-го этажа", "words": []})["floor"], "1")

    def test_floor_relation(self):
        self.assertEqual(floors_relation("1", "1"), "equal")
        self.assertEqual(floors_relation("1", "2"), "different")
        self.assertEqual(floors_relation("1", "подвал"), "different")
        self.assertEqual(floors_relation("-1", "подвал"), "unknown")
        self.assertEqual(floors_relation("", "2"), "unknown")
        self.assertEqual(floors_relation("отм:+3.300", "2"), "unknown")


class ClarificationGroupTests(unittest.TestCase):
    """The protocol side: an identity issue becomes a CLARIFICATION_REQUIRED group, never a verdict."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.db.session import Base

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def test_issue_becomes_clarification_group_with_sources(self):
        from app.db.models import DocumentVersion, EvidenceFragment, EvidenceGroup
        from app.domain import official_evidence

        for doc_id in (10, 11):
            self.db.add(DocumentVersion(id=doc_id, project_id=1, organization_id=1, source_type="t", filename=f"{doc_id}.pdf"))
        self.db.flush()
        process = SimpleNamespace(id="p1", project_id=1, organization_id=1, object_id="O", matrix_version="m",
                                  dataset_version="d", upload_scenario="s")
        param = SimpleNamespace(id=7, code=CODE, review_priority="HIGH")
        sources = [{"stage": "PD", "document_id": 10, "page": 3, "bbox_pdf": [10.0, 20.0, 110.0, 70.0],
                    "page_width": 1000.0, "page_height": 500.0, "systems": ["V1.1"]},
                   {"stage": "PD", "document_id": 11, "page": 4, "bbox_pdf": [10.0, 20.0, 110.0, 70.0],
                    "page_width": 1000.0, "page_height": 500.0, "systems": ["V1.2"]}]
        issues = [{"code": CODE, "reason": "DUPLICATE_ROOM_KEY", "status": "CLARIFICATION_REQUIRED", "location": "101",
                   "stage": "PD", "sources": sources, "text": "duplicate"},
                  {"code": "OTHER", "reason": "DUPLICATE_ROOM_KEY", "location": "9", "sources": [], "text": "x"}]
        documents = {10: self.db.get(DocumentVersion, 10), 11: self.db.get(DocumentVersion, 11)}
        keys = official_evidence._upsert_room_identity_groups(self.db, process, param, issues, documents=documents)
        again = official_evidence._upsert_room_identity_groups(self.db, process, param, issues, documents=documents)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys, again)                       # stable key: a re-run updates, not duplicates
        groups = self.db.query(EvidenceGroup).all()
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].finding_status, groups[0].comparability_status), ("CLARIFICATION_REQUIRED", "NOT_COMPARABLE"))
        self.assertEqual(groups[0].delta["identity_reason"], "DUPLICATE_ROOM_KEY")
        self.assertEqual(groups[0].delta["wall_check"], "not_evaluated")
        self.assertIsNone(groups[0].expected_value)
        fragments = self.db.query(EvidenceFragment).all()
        self.assertEqual(sorted(f.page for f in fragments), [3, 4])
        self.assertAlmostEqual(fragments[0].bbox[2], 0.11)


if __name__ == "__main__":
    unittest.main()
