"""Phase 12 / S5: inspector workbench backend -- versioned evidence edits, revision choice, bulk decisions,
completeness, verification time, page rendering. Endpoint-level (TestClient) plus the S3 hook contract."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.api.auth import get_current_user
from app.api.routes_case10 import router as case10_router
from app.db.models import (
    AuditLog,
    DocumentVersion,
    EvidenceFragment,
    EvidenceGroup,
    InspectorEdit,
    Organization,
    Project,
    SourceFragment,
    User,
)
from app.db.session import Base, get_db
from app.domain import inspector_workbench as workbench

FRAGMENT_COLUMNS = [c.name for c in EvidenceFragment.__table__.columns]


def _machine_rows(db) -> list[tuple]:
    return [tuple(getattr(row, name) for name in FRAGMENT_COLUMNS)
            for row in db.query(EvidenceFragment).order_by(EvidenceFragment.id).all()]


class WorkbenchTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_temp_uploads_dir = settings.TEMP_UPLOADS_DIR
        settings.TEMP_UPLOADS_DIR = Path(self._tmp.name)
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        org = Organization(name="S5 Org", slug="s5-org")
        other = Organization(name="Other Org", slug="other-org")
        self.db.add_all([org, other])
        self.db.flush()
        self.inspector = User(login="s5-inspector", email="i@s5.test", password_hash="x", api_key="i-key", is_admin=False,
                              role="INSPECTOR", organization_id=org.id)
        self.supervisor = User(login="s5-supervisor", email="s@s5.test", password_hash="x", api_key="s-key", is_admin=False,
                               role="SUPERVISOR", organization_id=org.id)
        self.outsider = User(login="s5-outsider", email="o@s5.test", password_hash="x", api_key="o-key", is_admin=False,
                             role="INSPECTOR", organization_id=other.id)
        self.project = Project(name="S5 Project", description="workbench", organization_id=org.id)
        self.db.add_all([self.inspector, self.supervisor, self.outsider, self.project])
        self.db.commit()
        self.org_id = org.id
        self.current_user = self.inspector
        app = FastAPI()
        app.include_router(case10_router, prefix="/api")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        settings.TEMP_UPLOADS_DIR = self._old_temp_uploads_dir
        self._tmp.cleanup()
        sys.modules.pop("app.domain.file_registry", None)

    # -- helpers --------------------------------------------------------------------------------------------------
    def seed(self):
        resp = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.process_id = resp.json()["process_id"]
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={self.process_id}").json()
        self.candidates = [g for g in groups if g["finding_status"] == "CANDIDATE" and g.get("fragments")]
        self.assertGreaterEqual(len(self.candidates), 2)
        return self.candidates[0]

    def make_pdf(self, pages: int = 2, rotate_second: bool = True) -> tuple[Path, str]:
        import fitz

        path = Path(self._tmp.name) / "sheet.pdf"
        doc = fitz.open()
        for index in range(pages):
            page = doc.new_page(width=842, height=595)
            page.insert_text((72, 72), f"Лист {index + 1}: помещение 1.109", fontsize=14)
            if rotate_second and index == 1:
                page.set_rotation(90)
        doc.save(str(path))
        doc.close()
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def attach_pdf(self, document_version_id: int) -> DocumentVersion:
        path, sha = self.make_pdf()
        doc = self.db.get(DocumentVersion, int(document_version_id))
        doc.file_path, doc.file_hash, doc.content_hash, doc.dataset_metadata = str(path), sha, sha, None
        self.db.commit()
        return doc

    def post(self, url, body=None):
        return self.client.post(url, json=body or {})


class EvidenceVersioningTests(WorkbenchTestBase):
    """Expert session §13: add / remove / refine evidence -> a new version with user, time, reason, reference to the
    source entity and the previous value; the machine output is never overwritten."""

    def test_refine_remove_restore_versions_and_machine_output_untouched(self):
        group = self.seed()
        before_rows = _machine_rows(self.db)
        source_before = self.db.query(SourceFragment).count()
        fragment = group["fragments"][0]
        key = f"machine:{fragment['id']}"

        refined = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/refine",
                            {"extracted_value": "240 мм", "note": "значение с соседней строки", "reason": "OCR спутал строку"})
        self.assertEqual(refined.status_code, 200, refined.text)
        edit = refined.json()["edit"]
        self.assertEqual((edit["action"], edit["version"], edit["entity_key"]), ("REFINE", 1, key))
        self.assertEqual(edit["user_id"], self.inspector.id)
        self.assertEqual(edit["reason"], "OCR спутал строку")
        self.assertEqual(edit["source_fragment_id"], fragment["id"])
        self.assertEqual(edit["previous_value"]["extracted_value"], fragment["extracted_value"])
        self.assertEqual(edit["new_value"]["extracted_value"], "240 мм")
        self.assertIsNotNone(edit["created_at"])
        effective = {item["key"]: item for item in refined.json()["evidence_group"]["effective_fragments"]}
        self.assertEqual(effective[key]["current"]["extracted_value"], "240 мм")
        self.assertEqual(effective[key]["machine"]["extracted_value"], fragment["extracted_value"])  # machine view kept
        self.assertEqual(effective[key]["version"], 1)

        removed = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/remove", {"reason": "не то помещение"})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual((removed.json()["edit"]["version"], removed.json()["edit"]["previous_edit_id"]), (2, edit["id"]))
        self.assertEqual({i["key"]: i["status"] for i in removed.json()["evidence_group"]["effective_fragments"]}[key], "REMOVED")
        twice = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/remove", {"reason": "ещё раз"})
        self.assertEqual(twice.status_code, 409, twice.text)
        refine_removed = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/refine", {"note": "x", "reason": "нельзя"})
        self.assertEqual(refine_removed.status_code, 409, refine_removed.text)

        restored = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/restore", {"reason": "проверено повторно"})
        self.assertEqual(restored.status_code, 200, restored.text)
        item = {i["key"]: i for i in restored.json()["evidence_group"]["effective_fragments"]}[key]
        self.assertEqual((item["status"], item["version"], item["current"]["extracted_value"]), ("ACTIVE", 3, "240 мм"))

        history = self.client.get(f"/api/case10/evidence-groups/{group['id']}/edits").json()
        self.assertEqual([(h["action"], h["version"]) for h in history], [("REFINE", 1), ("REMOVE", 2), ("RESTORE", 3)])
        self.db.expire_all()
        self.assertEqual(_machine_rows(self.db), before_rows)  # EvidenceFragment rows byte-for-byte unchanged
        self.assertEqual(self.db.query(SourceFragment).count(), source_before)
        actions = [row.action for row in self.db.query(AuditLog).filter(AuditLog.process_id == self.process_id).all()]
        for action in ("EVIDENCE_FRAGMENT_REFINED", "EVIDENCE_FRAGMENT_REMOVED", "EVIDENCE_FRAGMENT_RESTORED"):
            self.assertIn(action, actions)

    def test_add_fragment_with_bbox_on_real_pdf_computes_bbox_pdf_in_visible_frame(self):
        group = self.seed()
        doc_id = group["fragments"][0]["document_version_id"]
        self.attach_pdf(doc_id)
        added = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments", {
            "document_version_id": doc_id, "page": 2, "bbox_norm": [0.1, 0.2, 0.3, 0.4], "extracted_value": "1.109",
            "role": "actual", "reason": "помещение добавлено в РД",
        })
        self.assertEqual(added.status_code, 200, added.text)
        edit = added.json()["edit"]
        self.assertEqual((edit["action"], edit["version"]), ("ADD", 1))
        self.assertEqual(edit["entity_key"], f"manual:{edit['id']}")
        self.assertIsNone(edit["previous_value"])
        state = edit["new_value"]
        # page 2 is rotated by 90 degrees: the visible frame is 595 x 842, which is what the panels render
        self.assertEqual(state["bbox_pdf"], [59.5, 168.4, 178.5, 336.8])
        self.assertEqual(state["file_sha256"], self.db.get(DocumentVersion, doc_id).file_hash)
        item = {i["key"]: i for i in added.json()["evidence_group"]["effective_fragments"]}[edit["entity_key"]]
        self.assertEqual((item["origin"], item["view_bbox_quality"]), ("INSPECTOR", "EXACT"))
        self.assertEqual(item["view_bbox"], [0.1, 0.2, 0.3, 0.4])

        # refine the added fragment: new version on the same key, previous value = the ADD state
        refined = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{edit['entity_key']}/refine",
                            {"bbox_norm": [0.12, 0.2, 0.3, 0.42], "reason": "рамка по строке таблицы"})
        self.assertEqual(refined.status_code, 200, refined.text)
        self.assertEqual(refined.json()["edit"]["version"], 2)
        self.assertEqual(refined.json()["edit"]["previous_value"]["bbox_norm"], [0.1, 0.2, 0.3, 0.4])

    def test_add_fragment_validation(self):
        group = self.seed()
        doc_id = group["fragments"][0]["document_version_id"]
        self.attach_pdf(doc_id)
        url = f"/api/case10/evidence-groups/{group['id']}/fragments"
        base = {"document_version_id": doc_id, "page": 1, "reason": "основание"}
        self.assertEqual(self.post(url, {**base, "reason": "ok"}).status_code, 422)          # reason too short
        self.assertEqual(self.post(url, {**base, "page": 3}).status_code, 422)                # page outside the PDF
        self.assertEqual(self.post(url, {**base, "bbox_norm": [0.5, 0.2, 0.4, 0.3]}).status_code, 422)  # x1 > x2
        self.assertEqual(self.post(url, {**base, "bbox_norm": [0, 0, 1.2, 1]}).status_code, 422)        # outside [0;1]
        self.assertEqual(self.post(url, {**base, "role": "verdict"}).status_code, 422)
        self.assertEqual(self.post(url, {**base, "document_version_id": 99999}).status_code, 404)
        refine = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/machine:{group['fragments'][0]['id']}/refine",
                           {"reason": "ничего не меняю"})
        self.assertEqual(refine.status_code, 422)  # nothing changed -> no empty version
        missing = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/machine:999999/remove", {"reason": "нет такого"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.db.query(InspectorEdit).count(), 0)

    def test_edits_blocked_after_finalize_and_foreign_org_cannot_see_group(self):
        group = self.seed()
        for candidate in self.candidates:
            self.assertEqual(self.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions",
                                       {"decision": "Confirm", "comment": "ok"}).status_code, 200)
        remaining = [g for g in self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={self.process_id}").json()
                     if g["finding_status"] == "CANDIDATE"]
        for candidate in remaining:
            self.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions", {"decision": "Confirm", "comment": "ok"})
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={self.process_id}").json()
        self.assertEqual(self.post(f"/api/case10/protocols/{protocol['id']}/finalize").status_code, 200)
        key = f"machine:{group['fragments'][0]['id']}"
        blocked = self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/remove", {"reason": "слишком поздно"})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.current_user = self.outsider
        self.assertEqual(self.client.get(f"/api/case10/evidence-groups/{group['id']}/workbench").status_code, 404)
        self.assertEqual(self.client.get(f"/api/case10/processes/{self.process_id}/workbench").status_code, 404)

    def test_orphaned_edit_history_survives_machine_recompute(self):
        group = self.seed()
        fragment_id = group["fragments"][0]["id"]
        key = f"machine:{fragment_id}"
        self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/refine", {"note": "уточнено", "reason": "проверка"})
        self.db.query(EvidenceFragment).filter(EvidenceFragment.id == fragment_id).delete()  # a recompute replaced it
        self.db.commit()
        effective = workbench.effective_fragments(self.db, self.db.get(EvidenceGroup, group["id"]), with_view=False)
        orphan = next(item for item in effective if item["key"] == key)
        self.assertEqual(orphan["status"], "ORPHANED")
        self.assertEqual(orphan["current"]["note"], "уточнено")
        self.assertEqual(self.post(f"/api/case10/evidence-groups/{group['id']}/fragments/{key}/remove", {"reason": "нельзя"}).status_code, 409)


class WorkbenchPayloadTests(WorkbenchTestBase):
    def test_queue_and_card_expose_expert_session_fields(self):
        group = self.seed()
        summary = self.client.get(f"/api/case10/processes/{self.process_id}/workbench").json()
        self.assertEqual(summary["process_id"], self.process_id)
        row = next(item for item in summary["items"] if item["id"] == group["id"])
        for key in ("type_key", "finding_status", "inspector_status", "stages", "low_confidence", "low_quality", "min_confidence", "edits"):
            self.assertIn(key, row)
        detail = self.client.get(f"/api/case10/evidence-groups/{group['id']}/workbench").json()
        card = detail["card"]
        # §14: ID, object_id, parameter code, rule, stage, document, cipher, revision, page, bbox, extracted and
        # reference value, description (delta), confidence, the inspector's decision
        for key in ("finding_id", "evidence_group_id", "object_id", "parameter_code", "rule", "expected_value", "actual_value",
                    "expected_stage", "actual_stage", "source_expected", "source_actual", "delta", "confidence", "inspector_status"):
            self.assertIn(key, card)
        self.assertTrue(card["finding_id"].startswith("F-"))
        for key in ("file_id", "code", "revision", "approval", "page", "bbox_polygon"):
            self.assertIn(key, card["source_expected"])
        self.assertEqual(set(detail["panels"]), {"PD", "RD", "ID"})
        self.assertTrue(set(detail["visible_stages"]) <= {"PD", "RD", "ID"})
        self.assertTrue(all(item["origin"] == "MACHINE" for item in detail["effective_fragments"]))

    def test_low_quality_flag_comes_only_from_explicit_pipeline_flags(self):
        flags = workbench.quality_flags({"confidence": 0.9, "comparability_status": "COMPARABLE", "delta": {}},
                                        [{"confidence": 0.95, "file_id": "F1", "page": 3}], 0.75)
        self.assertEqual((flags["low_confidence"], flags["low_quality"]), (False, False))
        flags = workbench.quality_flags({"confidence": 0.9, "delta": {}}, [{"confidence": 0.5, "quality_status": "LOW_QUALITY",
                                                                             "file_id": "F1", "page": 3}], 0.75)
        self.assertEqual((flags["low_confidence"], flags["low_quality"]), (True, True))
        self.assertEqual(flags["low_quality_reasons"], ["F1 стр. 3"])
        flags = workbench.quality_flags({"confidence": None, "comparability_status": "LOW_QUALITY", "delta": {}}, [], 0.75)
        self.assertEqual((flags["low_confidence"], flags["low_quality"]), (False, True))

    def test_page_geometry_png_and_source_pdf_of_a_rotated_page(self):
        group = self.seed()
        doc_id = group["fragments"][0]["document_version_id"]
        self.attach_pdf(doc_id)
        geometry = self.client.get(f"/api/case10/document-versions/{doc_id}/pages/2/geometry").json()
        self.assertEqual((geometry["width"], geometry["height"], geometry["rotation"], geometry["page_count"]), (595.0, 842.0, 90, 2))
        png = self.client.get(f"/api/case10/document-versions/{doc_id}/pages/2.png?max_side=800")
        self.assertEqual((png.status_code, png.headers["content-type"]), (200, "image/png"))
        from PIL import Image
        import io

        width, height = Image.open(io.BytesIO(png.content)).size
        self.assertLess(width, height)  # rendered in the visible (rotated) frame
        pdf = self.client.get(f"/api/case10/document-versions/{doc_id}/pages/1.pdf")
        self.assertEqual(pdf.status_code, 200)
        import fitz

        with fitz.open(stream=pdf.content, filetype="pdf") as single:
            self.assertEqual(len(single), 1)
            self.assertIn("1.109", single[0].get_text())
        self.assertEqual(self.client.get(f"/api/case10/document-versions/{doc_id}/pages/9.png").status_code, 422)
        tampered = self.db.get(DocumentVersion, doc_id)
        tampered.file_hash = tampered.content_hash = "0" * 64
        self.db.commit()
        workbench._page_geometry_cached.cache_clear()
        workbench._render_page_cached.cache_clear()
        self.assertEqual(self.client.get(f"/api/case10/document-versions/{doc_id}/pages/1.png").status_code, 422)  # hash mismatch


class BulkDecisionTests(WorkbenchTestBase):
    def _twin_candidate(self, group_dict) -> int:
        """A second CANDIDATE of the same parameter (another location) -- the §17 'однотипные' case."""
        source = self.db.get(EvidenceGroup, group_dict["id"])
        twin = EvidenceGroup(process_id=source.process_id, project_id=source.project_id, organization_id=source.organization_id,
                             object_id=source.object_id, param_id=source.param_id, canonical_entity_id=None,
                             finding_status="CANDIDATE", comparability_status="COMPARABLE", review_priority=source.review_priority,
                             delta={"location": "2.01"}, group_key="twin")
        self.db.add(twin)
        self.db.commit()
        return int(twin.id)

    def test_bulk_rules(self):
        group = self.seed()
        twin = self._twin_candidate(group)
        other_type = next(g for g in self.candidates if g["parameter"]["code"] != group["parameter"]["code"])
        url = "/api/case10/evidence-groups/bulk-decisions"
        mixed = self.post(url, {"evidence_group_ids": [group["id"], other_type["id"]], "decision": "Confirm", "comment": "одно и то же"})
        self.assertEqual(mixed.status_code, 422, mixed.text)
        no_comment = self.post(url, {"evidence_group_ids": [group["id"], twin], "decision": "Confirm", "comment": "ok"})
        self.assertEqual(no_comment.status_code, 422)  # shared comment of >= 3 chars
        unconfirmed = self.post(url, {"evidence_group_ids": [group["id"], twin], "decision": "Reject", "comment": "не нарушение"})
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
        self.db.expire_all()
        self.assertEqual({self.db.get(EvidenceGroup, gid).finding_status for gid in (group["id"], twin)}, {"CANDIDATE"})

        done = self.post(url, {"evidence_group_ids": [group["id"], twin], "decision": "Reject", "reason_code": "NOT_A_VIOLATION",
                               "comment": "в РД решение сохранено", "confirm_bulk_reject": True})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["count"], 2)
        self.db.expire_all()
        self.assertEqual({self.db.get(EvidenceGroup, gid).finding_status for gid in (group["id"], twin)}, {"NEGATIVE_VERIFIED"})
        bulk_audit = self.db.query(AuditLog).filter(AuditLog.action == "BULK_DECISION").one()
        self.assertEqual(sorted(bulk_audit.details["evidence_group_ids"]), sorted([group["id"], twin]))
        self.assertTrue(bulk_audit.details["confirmed_bulk_reject"])

    def test_bulk_is_all_or_nothing_on_a_technical_status(self):
        group = self.seed()
        twin = self._twin_candidate(group)
        row = self.db.get(EvidenceGroup, twin)
        row.finding_status = "MISSING_EVIDENCE"
        self.db.commit()
        resp = self.post("/api/case10/evidence-groups/bulk-decisions",
                         {"evidence_group_ids": [group["id"], twin], "decision": "Confirm", "comment": "подтверждаю"})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.db.expire_all()
        self.assertEqual(self.db.get(EvidenceGroup, group["id"]).finding_status, "CANDIDATE")  # nothing applied


class RevisionChoiceTests(WorkbenchTestBase):
    def _chain(self):
        self.seed()
        old = DocumentVersion(project_id=self.project.id, organization_id=self.org_id, object_id=None, filename="АР изм.1.pdf",
                              doc_stage="working", document_stage="working", document_code="OBJ-RD-AR", revision="1",
                              approval_status="APPROVED")
        new = DocumentVersion(project_id=self.project.id, organization_id=self.org_id, object_id=None, filename="АР изм.2.pdf",
                              doc_stage="working", document_stage="working", document_code="OBJ-RD-AR", revision="2",
                              approval_status="APPROVED")
        self.db.add_all([old, new])
        self.db.flush()
        old.successor_id, new.predecessor_id = new.id, old.id
        from app.db.models import InspectionProcess

        process = self.db.get(InspectionProcess, self.process_id)
        process.object_id = None  # the chain is registered at project level in this fixture
        self.db.commit()
        return old, new

    def test_choice_is_versioned_with_justification_and_history(self):
        old, new = self._chain()
        scopes = self.client.get(f"/api/case10/processes/{self.process_id}/revisions").json()
        self.assertEqual(scopes["source"], "file_registry")
        chain = next(s for s in scopes["scopes"] if s["type"] == "PREDECESSOR_CHAIN")
        self.assertEqual((chain["status"], chain["system_choice_id"], chain["effective_choice_id"]), ("RESOLVED", new.id, new.id))
        url = f"/api/case10/processes/{self.process_id}/revision-choices"
        self.assertEqual(self.post(url, {"scope_key": chain["scope_key"], "document_version_id": old.id, "justification": "ok"}).status_code, 422)
        self.assertEqual(self.post(url, {"scope_key": "nope", "document_version_id": old.id, "justification": "по письму"}).status_code, 404)
        foreign = self.post(url, {"scope_key": chain["scope_key"], "document_version_id": 99999, "justification": "по письму"})
        self.assertEqual(foreign.status_code, 422)

        first = self.post(url, {"scope_key": chain["scope_key"], "document_version_id": old.id, "justification": "изм.2 отозвано письмом"})
        self.assertEqual(first.status_code, 200, first.text)
        choice = first.json()["choice"]
        self.assertEqual((choice["action"], choice["version"], choice["source_document_version_id"]), ("CHOOSE", 1, old.id))
        self.assertEqual(choice["previous_value"]["system_choice_id"], new.id)  # previous value = the system's choice
        self.assertEqual(first.json()["effect"]["status"], "RECOMPUTE_QUEUED")
        self.assertTrue(first.json()["effect"]["applied"])
        scope = next(s for s in first.json()["revisions"]["scopes"] if s["scope_key"] == chain["scope_key"])
        self.assertEqual((scope["status"], scope["effective_choice_id"]), ("RESOLVED_BY_INSPECTOR", old.id))

        second = self.post(url, {"scope_key": chain["scope_key"], "document_version_id": new.id, "justification": "письмо аннулировано"})
        self.assertEqual(second.json()["choice"]["version"], 2)
        self.assertEqual(second.json()["choice"]["previous_edit_id"], choice["id"])
        self.assertEqual(second.json()["choice"]["previous_value"]["chosen"]["document_version_id"], old.id)
        scope = next(s for s in second.json()["revisions"]["scopes"] if s["scope_key"] == chain["scope_key"])
        self.assertEqual(len(scope["history"]), 2)
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "REVISION_CHOSEN_BY_INSPECTOR").count(), 2)

    def test_s3_file_registry_hooks_take_over_when_present(self):
        self._chain()
        calls = {}
        fake = types.ModuleType("app.domain.file_registry")
        fake.revision_scopes = lambda db, process: [{
            "scope_key": "RD|REGISTRY|AR", "stage": "RD", "scope": "АР", "type": "REGISTRY", "system_status": "CLARIFICATION_REQUIRED",
            "system_choice_id": None, "basis": "реестр: две редакции без статуса",
            "candidates": [{"document_version_id": 1, "file_id": "F1"}, {"document_version_id": 2, "file_id": "F2"}],
        }]

        def apply(db, process, choice):
            calls["choice"] = choice
            return {"applied": True, "status": "RECOMPUTE_QUEUED"}

        fake.apply_inspector_revision_choice = apply
        fake.completeness_report = lambda db, process: {"source": "file_registry", "status": "COMPLETE", "rows": []}
        sys.modules["app.domain.file_registry"] = fake
        scopes = self.client.get(f"/api/case10/processes/{self.process_id}/revisions").json()
        self.assertEqual((scopes["source"], scopes["open_conflicts"]), ("file_registry", 1))
        chosen = self.post(f"/api/case10/processes/{self.process_id}/revision-choices",
                           {"scope_key": "RD|REGISTRY|AR", "document_version_id": 2, "justification": "по реестру заказчика"})
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual(chosen.json()["effect"], {"applied": True, "status": "RECOMPUTE_QUEUED"})
        self.assertEqual(calls["choice"]["chosen"]["document_version_id"], 2)
        self.assertEqual(calls["choice"]["justification"], "по реестру заказчика")
        self.assertEqual(self.client.get(f"/api/case10/processes/{self.process_id}/completeness").json()["source"], "file_registry")


class CompletenessAndTimingTests(WorkbenchTestBase):
    def test_completeness_stub_reports_expected_uploaded_and_basis(self):
        self.seed()
        data = self.client.get(f"/api/case10/processes/{self.process_id}/completeness").json()
        self.assertIn(data["status"], {"COMPLETE", "INCOMPLETE", "CLARIFICATION_REQUIRED"})
        self.assertFalse(data["registry_present"])
        self.assertTrue(data["basis"])
        self.assertEqual([s["stage"] for s in data["stages"]], ["PD", "RD", "ID"])
        for row in data["rows"]:
            self.assertIn(row["status"], {"UPLOADED", "MISSING_EVIDENCE", "UNCERTAIN", "NOT_APPLICABLE"})
            self.assertTrue(row["expected_basis"])

    def test_section_token_canonicalization(self):
        self.assertEqual(workbench._section_token("Раздел 5. ИОС4"), "ИОС4")
        self.assertEqual(workbench._section_token("Раздел 2. СПЗУ"), "ПЗУ")
        self.assertEqual(workbench._section_token("Раздел 9. ППМ"), "ПБ")

    def test_verification_time_from_open_to_finalize_lands_in_audit_log(self):
        self.seed()
        opened = self.post(f"/api/case10/processes/{self.process_id}/verification/open").json()
        self.assertTrue(opened["running"])
        self.post(f"/api/case10/processes/{self.process_id}/verification/open")  # idempotent within a cycle
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "VERIFICATION_OPENED").count(), 1)
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={self.process_id}").json()
        for index, group in enumerate(g for g in groups if g["finding_status"] == "CANDIDATE"):
            resp = self.post(f"/api/case10/evidence-groups/{group['id']}/decisions",
                             {"decision": "Confirm", "comment": "ok", "ui_metrics": {"actions": 3 + index % 2, "elapsed_ms": 4000, "via": "keyboard"}})
            self.assertEqual(resp.status_code, 200, resp.text)
        timing = self.client.get(f"/api/case10/processes/{self.process_id}/verification/timing").json()
        self.assertGreaterEqual(timing["decisions"], 2)
        self.assertEqual(timing["avg_seconds_per_decision"], 4.0)
        self.assertGreaterEqual(timing["avg_actions_per_decision"], 3.0)
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={self.process_id}").json()
        self.assertEqual(self.post(f"/api/case10/protocols/{protocol['id']}/finalize").status_code, 200)
        measured = self.db.query(AuditLog).filter(AuditLog.action == "VERIFICATION_TIME_MEASURED").one()
        self.assertEqual(measured.details["decisions"], timing["decisions"])
        self.assertGreaterEqual(measured.details["duration_seconds"], 0)
        final = self.client.get(f"/api/case10/processes/{self.process_id}/verification/timing").json()
        self.assertFalse(final["running"])
        self.assertEqual(len(final["measurements"]), 1)

        # unfinalize (supervisor) starts a new verification cycle: opening again starts a new clock
        self.current_user = self.supervisor
        protocol = self.client.get(f"/api/case10/protocols/current?project_id={self.project.id}&process_id={self.process_id}").json()
        self.assertEqual(self.post(f"/api/case10/protocols/{protocol['id']}/unfinalize", {"reason": "перепроверка"}).status_code, 200)
        reopened = self.post(f"/api/case10/processes/{self.process_id}/verification/open").json()
        self.assertTrue(reopened["running"])
        self.assertEqual(reopened["decisions"], 0)
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "VERIFICATION_OPENED").count(), 2)


if __name__ == "__main__":
    unittest.main()
