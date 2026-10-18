"""Final / step Z: TZ module 7 (DOCX/XML export of the protocol, the object dashboard) and module 10 (weekly ML report),
the traceability matrix and the claims checker. Endpoint-level (TestClient) on the synthetic dataset plus pure functions."""
from __future__ import annotations

from io import BytesIO
import json
import unittest
from xml.etree import ElementTree as ET
import zipfile

from app.domain import inspector_dashboard as dash
from app.domain import protocol_export
from test_case10_s5_workbench import WorkbenchTestBase

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class SeededBase(WorkbenchTestBase):
    def seed(self):
        response = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(response.status_code, 200, response.text)
        self.process_id = response.json()["process_id"]
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={self.process_id}")
        self.assertEqual(protocol.status_code, 200, protocol.text)
        self.protocol = protocol.json()
        return self.protocol


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p"))


class ProtocolExportTests(SeededBase):
    def test_docx_is_a_valid_package_with_the_protocol_content(self):
        protocol = self.seed()
        response = self.client.get(f"/api/case10/protocols/{protocol['id']}/docx")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], protocol_export.DOCX_MEDIA_TYPE)
        self.assertIn(f"protocol_{protocol['id']}.docx", response.headers["content-disposition"])
        with zipfile.ZipFile(BytesIO(response.content)) as package:
            self.assertIsNone(package.testzip())
            names = set(package.namelist())
            self.assertTrue({"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml"} <= names)
            for name in names:  # every part is well-formed XML
                ET.fromstring(package.read(name))
        text = docx_text(response.content)
        annex = protocol["payload"]["annex_2"]
        self.assertIn(f"№ {protocol['id']}", text)
        for number in ("1", "2", "3", "4", "5", "6", "7"):
            self.assertIn(f"РАЗДЕЛ {number}.", text)
        shown = [row for key in ("section_3_unverified_parameters", "section_4_critical_violations", "section_5_substantial_violations",
                                 "section_6_ai_suspicions", "pending_candidates") for row in annex[key]]
        self.assertTrue(shown)
        for row in shown:
            self.assertIn(str(row["parameter_code"]), text)
        for row in annex["pending_candidates"]:
            for source in row.get("evidence") or []:
                if source.get("file_sha256"):
                    self.assertIn(source["file_sha256"], text)

    def test_xml_parses_and_equals_the_json_protocol(self):
        protocol = self.seed()
        response = self.client.get(f"/api/case10/protocols/{protocol['id']}/xml")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/xml")
        root = ET.fromstring(response.content)
        self.assertEqual(root.tag, "protocol")
        self.assertEqual(root.get("schema_version"), protocol_export.XML_SCHEMA_VERSION)
        self.assertEqual(root.get("id"), str(protocol["id"]))
        annex = protocol["payload"]["annex_2"]
        for xml_name, json_key in (("section_3", "section_3_unverified_parameters"), ("section_4", "section_4_critical_violations"),
                                   ("section_5", "section_5_substantial_violations"), ("section_6_hypotheses", "section_6_ai_suspicions"),
                                   ("pending_candidates", "pending_candidates")):
            node = root.find(xml_name)
            self.assertIsNotNone(node, xml_name)
            self.assertEqual(int(node.get("count")), len(annex[json_key]), xml_name)
            self.assertEqual(len(list(node)), len(annex[json_key]), xml_name)
            codes = [child.findtext("parameter_code") for child in node]
            self.assertEqual(codes, [str(row["parameter_code"]) for row in annex[json_key]], xml_name)
        stats = {item.findtext("name"): int(item.findtext("count")) for item in root.find("section_2")}
        self.assertEqual(stats, {row["name"].strip(): row["count"] for row in annex["section_2_statistics"]})
        finding = next(iter(root.find("pending_candidates")))
        self.assertTrue(finding.findtext("evidence_group_id"))
        sources = finding.find("sources")
        self.assertIsNotNone(sources)
        first = next(iter(sources))
        self.assertTrue(first.findtext("file"))
        self.assertIsNotNone(first.find("page"))
        self.assertIsNotNone(first.find("bbox"))
        self.assertIsNotNone(first.find("sha256"))

    def test_same_protocol_gives_the_same_bytes_and_other_org_gets_404(self):
        protocol = self.seed()
        first = self.client.get(f"/api/case10/protocols/{protocol['id']}/docx").content
        second = self.client.get(f"/api/case10/protocols/{protocol['id']}/docx").content
        self.assertEqual(first, second)
        self.assertEqual(self.client.get(f"/api/case10/protocols/{protocol['id']}/xml").content,
                         self.client.get(f"/api/case10/protocols/{protocol['id']}/xml").content)
        self.current_user = self.outsider
        for kind in ("docx", "xml", "pdf"):
            self.assertEqual(self.client.get(f"/api/case10/protocols/{protocol['id']}/{kind}").status_code, 404, kind)

    def test_docx_opens_in_a_real_word_reader_when_one_is_installed(self):
        try:
            import docx  # python-docx: not a dependency of the service, only a reader for this check
        except ImportError:
            self.skipTest("python-docx is not installed")
        protocol = self.seed()
        document = docx.Document(BytesIO(self.client.get(f"/api/case10/protocols/{protocol['id']}/docx").content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn(f"№ {protocol['id']}", text)
        self.assertGreater(len(document.tables), 3)
        self.assertTrue(any("РАЗДЕЛ 6." in paragraph.text for paragraph in document.paragraphs))

    def test_inspector_decision_reaches_both_formats(self):
        self.seed()
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={self.process_id}").json()
        candidate = next(row for row in groups if row["finding_status"] == "CANDIDATE")
        decided = self.client.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions",
                                   json={"decision": "Confirm", "comment": "проверено по чертежу ОВ-3"})
        self.assertEqual(decided.status_code, 200, decided.text)
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={self.process_id}").json()
        self.assertIn("проверено по чертежу ОВ-3", docx_text(self.client.get(f"/api/case10/protocols/{protocol['id']}/docx").content))
        self.assertIn("проверено по чертежу ОВ-3", self.client.get(f"/api/case10/protocols/{protocol['id']}/xml").content.decode("utf-8"))


class ProtocolExportPureTests(unittest.TestCase):
    PROTOCOL = {"id": 7, "process_id": "p", "version": 2, "status": "DRAFT", "matrix_version": "m", "dataset_version": "d",
                "model_version": "r", "input_manifest_hash": "abc", "object_id": "OBJ",
                "payload": {"annex_2": {
                    "schema_version": "case10-annex2-v1", "header": {"object_name": "Дом <1> & Ко"},
                    "section_1_document_upload": {"rows": [{"stage": "PD", "status": "PD_UPLOADED", "loaded_files": 1, "expected_files": None}]},
                    "section_2_statistics": [{"name": "Всего", "count": 3, "percent_of_matrix": None}],
                    "section_3_unverified_parameters": [], "section_4_critical_violations": [], "section_5_substantial_violations": [],
                    "section_6_ai_suspicions": [{"parameter_code": "H-1", "parameter_name": "Гипотеза\x00 с управляющим символом", "finding_status": "SUSPICION",
                                                  "evidence_tier": "HYPOTHESIS", "delta": {"discovery_method": "UNMATCHED_DIFF", "matrix_scope": "FREE_SEARCH"},
                                                  "evidence": [{"file_id": "F1", "file_sha256": "ff", "page": 3, "bbox_normalized": [0.1, 0.2, 0.3, 0.4]}]}],
                    "section_7_resolution": {"status": "INSPECTOR_REQUIRED", "items": []}, "pending_candidates": []}}}

    def test_empty_sections_special_characters_and_hypotheses_render_in_both_formats(self):
        docx = protocol_export.render_docx(self.PROTOCOL)
        text = docx_text(docx)
        self.assertIn("Дом <1> & Ко", text)
        self.assertIn("Нет записей.", text)
        self.assertIn("РАЗДЕЛ 6.", text)
        self.assertIn("H-1", text)
        self.assertNotIn("\x00", text)
        root = ET.fromstring(protocol_export.render_xml(self.PROTOCOL))
        self.assertEqual(root.find("section_3").get("count"), "0")
        hypothesis = next(iter(root.find("section_6_hypotheses")))
        self.assertEqual(hypothesis.findtext("method"), "UNMATCHED_DIFF")
        self.assertEqual(hypothesis.findtext("tier"), "HYPOTHESIS")
        self.assertEqual(root.find("header").findtext("object_name"), "Дом <1> & Ко")
        self.assertEqual(root.find("header").find("address").get("nil"), "true")

    def test_protocol_without_payload_still_renders(self):
        for render in (protocol_export.render_docx, protocol_export.render_xml):
            self.assertTrue(render({"id": 1, "version": 1, "status": "DRAFT"}))

    def test_model_is_json_serialisable(self):
        json.dumps(protocol_export.build_export_model(self.PROTOCOL), ensure_ascii=False)



class DashboardRuleTests(unittest.TestCase):
    def color(self, status="COMPLETED", **counts):
        counts.setdefault("total", 5)
        return dash.classify(status, counts)

    def test_gray_for_unfinished_checks_and_for_no_data(self):
        for status in ("PENDING", "QUEUED", "PROCESSING", "PARSING", "FAILED", None):
            self.assertEqual(self.color(status, confirmed=3), (dash.GRAY, "process_not_finished"), status)
        self.assertEqual(self.color(total=0), (dash.GRAY, "no_data"))

    def test_red_for_confirmed_violation_or_unreviewed_high_priority_candidate(self):
        self.assertEqual(self.color(confirmed=1, candidates=4), (dash.RED, "confirmed_violation"))
        self.assertEqual(self.color(candidates=1, high_priority_candidates=1), (dash.RED, "high_priority_candidate"))

    def test_yellow_for_unreviewed_candidates_hypotheses_and_clarification(self):
        self.assertEqual(self.color(candidates=2), (dash.YELLOW, "unreviewed_candidates"))
        self.assertEqual(self.color(hypotheses=1), (dash.YELLOW, "unreviewed_hypotheses"))
        self.assertEqual(self.color(clarification=1), (dash.YELLOW, "clarification_requested"))

    def test_green_when_everything_is_reviewed_and_nothing_confirmed(self):
        for status in ("READY", "VERIFYING", "COMPLETED", "FINALIZED"):
            self.assertEqual(self.color(status), (dash.GREEN, "all_reviewed"), status)

    def test_every_colour_and_reason_has_a_text_for_the_ui(self):
        for color in dash.COLORS:
            self.assertIn(color, dash.RULE_TEXT)
        for reason in ("process_not_finished", "no_data", "confirmed_violation", "high_priority_candidate", "unreviewed_candidates",
                       "unreviewed_hypotheses", "clarification_requested", "all_reviewed"):
            self.assertIn(reason, dash.REASON_TEXT)

    def test_counts_split_candidates_by_tier_priority_and_hypothesis_review(self):
        rows = [("CANDIDATE", "HIGH", {}, None), ("CANDIDATE", "LOW", {"evidence_tier": "SUGGESTION", "tier_reasons": ["trigger_not_evaluable"]}, None),
                ("CONFIRMED_VIOLATION", "HIGH", {}, None), ("CLARIFICATION_REQUIRED", "LOW", {}, None),
                ("SUSPICION", "LOW", {"matrix_scope": "FREE_SEARCH"}, "PENDING"), ("SUSPICION", "LOW", {"matrix_scope": "FREE_SEARCH"}, "REJECTED")]
        counts = dash._counts(rows)
        self.assertEqual((counts["confirmed"], counts["high_priority_candidates"], counts["violation_candidates"], counts["suggestions"],
                          counts["clarification"], counts["hypotheses"]), (1, 1, 1, 1, 1, 1))


class DashboardEndpointTests(SeededBase):
    def rows(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.client.get(f"/api/case10/dashboard?{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_seeded_check_is_listed_with_colour_and_it_follows_the_inspector(self):
        self.seed()
        data = self.rows()
        self.assertEqual(data["total"], 1)
        row = data["items"][0]
        self.assertEqual(row["process_id"], self.process_id)
        self.assertIn(row["color"], (dash.RED, dash.YELLOW))  # synthetic data has unreviewed candidates
        self.assertEqual(data["summary"][row["color"]], 1)
        self.assertEqual(set(data["rules"]), set(dash.COLORS))
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={self.process_id}").json()
        for group in groups:
            if group["finding_status"] == "CANDIDATE":
                answer = self.client.post(f"/api/case10/evidence-groups/{group['id']}/decisions",
                                          json={"decision": "Reject", "reason_code": "OTHER", "comment": "проверено"})
                self.assertEqual(answer.status_code, 200, answer.text)
        row = self.rows()["items"][0]
        self.assertEqual((row["color"], row["reason"]), (dash.GREEN, "all_reviewed"))
        first = next(g for g in groups if g["finding_status"] == "CANDIDATE")
        self.client.post(f"/api/case10/evidence-groups/{first['id']}/decisions", json={"decision": "Confirm", "comment": "нарушение"})
        row = self.rows()["items"][0]
        self.assertEqual((row["color"], row["reason"]), (dash.RED, "confirmed_violation"))
        self.assertEqual(row["counts"]["confirmed"], 1)

    def test_filters_by_status_section_and_dates(self):
        self.seed()
        colour = self.rows()["items"][0]["color"]
        self.assertEqual(self.rows(status=colour)["total"], 1)
        other = "GREEN" if colour != "GREEN" else "RED"
        self.assertEqual(self.rows(status=other)["total"], 0)
        self.assertEqual(self.client.get("/api/case10/dashboard?status=PURPLE").status_code, 422)
        sections = self.rows()["filters"]["sections"]
        self.assertTrue(sections)
        self.assertEqual(self.rows(section=sections[0])["total"], 1)
        self.assertEqual(self.rows(section="нет-такого-раздела")["total"], 0)
        self.assertEqual(self.rows(date_from="2000-01-01", date_to="2999-01-01")["total"], 1)
        self.assertEqual(self.rows(date_to="2000-01-01")["total"], 0)
        self.assertEqual(self.rows(date_from="2999-01-01")["total"], 0)
        self.assertEqual(self.client.get("/api/case10/dashboard?date_from=вчера").status_code, 422)

    def test_other_organisation_sees_nothing_and_unfinished_check_is_gray(self):
        self.seed()
        self.current_user = self.outsider
        self.assertEqual(self.rows()["total"], 0)
        self.current_user = self.inspector
        from app.db.models import InspectionProcess
        process = self.db.get(InspectionProcess, self.process_id)
        process.status = "PROCESSING"
        self.db.commit()
        row = self.rows()["items"][0]
        self.assertEqual((row["color"], row["reason"]), (dash.GRAY, "process_not_finished"))


if __name__ == "__main__":
    unittest.main()
