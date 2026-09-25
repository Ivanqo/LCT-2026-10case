"""Incremental dozagruzka / recompute: impact-scoped runs must not be a
hidden full rerun, must preserve inspector decisions whose evidence basis did
not change, must mark (not silently discard) decisions whose basis did
change, must keep previous protocol versions with an understandable diff,
must treat a duplicate upload/hash as a no-op, must never touch a finalized
protocol, and must correctly prefer a new approved revision over an obsolete
one. No real object_id/file_id/page/GOLD value from the competition dataset
is used anywhere here -- fixtures are synthetic or in-memory SimpleNamespaces.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    AttributeObservation,
    AuditLog,
    DocumentVersion,
    EntityObservation,
    EvidenceDecision,
    EvidenceGroup,
    Organization,
    Param,
    Project,
    Protocol,
    User,
)
from app.db.session import Base
from app.domain.document_versions import ensure_document_version_for_upload_job
from app.domain.evidence_groups import sweep_orphaned_evidence_groups, upsert_evidence_group
from app.domain.official_rule_packs import extract_official_rule_observations
from app.domain.synthetic_dataset import ensure_synthetic_case10_dataset
from app.domain.v3_pipeline import (
    MATRIX_VERSION_DEMO,
    current_document_versions,
    ensure_demo_matrix,
    finalize_protocol,
    get_or_create_open_process,
    latest_protocol,
    record_inspector_decision,
    run_process,
)


class EvidenceGroupUpsertTests(unittest.TestCase):
    """Unit-level coverage of the shared upsert engine all three evidence
    producers (official rule-pack, official fallback, demo path) go through."""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(id="proc-upsert", project_id=1, organization_id=1, object_id="OBJ-1")
        self.param = SimpleNamespace(id=1, code="DEMO-X")

    def tearDown(self):
        self.db.close()

    def _fields(self, actual_value="180"):
        return {
            "finding_status": "CANDIDATE",
            "expected_value": "200",
            "actual_value": actual_value,
            "delta": {"expected": "200", "actual": actual_value, "equal": False},
            "review_priority": "HIGH",
        }

    def _specs(self, value="180"):
        return [{"document_version_id": 10, "source_fragment_id": 20, "page": 1, "extracted_value": value, "role": "actual", "file_sha256": "h1"}]

    def test_identical_rerun_touches_nothing(self):
        group, should_write = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields(), fragment_specs=self._specs())
        self.assertIsNotNone(group)
        self.assertTrue(should_write)
        first_id, first_updated = group.id, group.updated_at

        group2, should_write2 = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields(), fragment_specs=self._specs())
        self.assertIsNone(group2)
        self.assertFalse(should_write2)
        row = self.db.query(EvidenceGroup).filter_by(process_id="proc-upsert", param_id=1, group_key="k1").one()
        self.assertEqual(row.id, first_id)
        self.assertEqual(row.updated_at, first_updated)
        self.assertEqual(self.db.query(EvidenceGroup).count(), 1)

    def test_changed_evidence_without_a_decision_refreshes_in_place(self):
        group, _ = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        first_id = group.id

        group2, should_write2 = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("170"), fragment_specs=self._specs("170"))
        self.assertIsNotNone(group2)
        self.assertTrue(should_write2)
        self.assertEqual(group2.id, first_id)
        self.assertEqual(group2.actual_value, "170")
        self.assertFalse(group2.needs_reverification)
        self.assertEqual(self.db.query(EvidenceGroup).count(), 1)

    def test_changed_evidence_with_a_decision_preserves_it_and_flags_for_review(self):
        group, _ = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        self.db.add(EvidenceDecision(evidence_group_id=group.id, decision="Confirm", user_id=7))
        group.finding_status = "CONFIRMED_VIOLATION"
        self.db.add(group)
        self.db.commit()

        group2, should_write2 = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("170"), fragment_specs=self._specs("170"))
        self.assertIsNotNone(group2)
        self.assertFalse(should_write2, "an already-decided group's fragments must not be silently rewritten")
        self.assertEqual(group2.id, group.id)
        self.assertEqual(group2.finding_status, "CONFIRMED_VIOLATION", "the inspector's verdict must survive a basis change")
        self.assertTrue(group2.needs_reverification)
        self.assertIn("pending_reverification", group2.delta)
        self.assertEqual(group2.delta["pending_reverification"]["actual_value"], "170")
        decisions = self.db.query(EvidenceDecision).filter_by(evidence_group_id=group.id).all()
        self.assertEqual(len(decisions), 1, "the original decision must not be duplicated or removed")

    def test_repeated_identical_mismatch_does_not_spam_audit_or_rewrite(self):
        group, _ = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        self.db.add(EvidenceDecision(evidence_group_id=group.id, decision="Reject", reason_code="OTHER", user_id=1))
        self.db.commit()

        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("170"), fragment_specs=self._specs("170"))
        first_flag_time = self.db.get(EvidenceGroup, group.id).basis_changed_at
        audit_count_after_first = self.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count()
        self.assertEqual(audit_count_after_first, 1)

        # A later rerun (e.g. a full recompute triggered by an unrelated
        # change) re-derives the exact same "new" evidence again -- nothing
        # about this mismatch is actually new information.
        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("170"), fragment_specs=self._specs("170"))
        row = self.db.get(EvidenceGroup, group.id)
        self.assertEqual(row.basis_changed_at, first_flag_time)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count(), 1)

    def test_sweep_deletes_undecided_orphans_and_preserves_decided_ones(self):
        undecided, _ = upsert_evidence_group(self.db, self.process, self.param, "k-a", fields=self._fields("180"), fragment_specs=self._specs("180"))
        decided, _ = upsert_evidence_group(self.db, self.process, self.param, "k-b", fields=self._fields("180"), fragment_specs=self._specs("180"))
        self.db.add(EvidenceDecision(evidence_group_id=decided.id, decision="Reject", reason_code="OTHER", user_id=1))
        self.db.commit()

        sweep_orphaned_evidence_groups(self.db, self.process, {1}, touched_keys={1: set()})

        remaining_ids = {row.id for row in self.db.query(EvidenceGroup).filter_by(process_id="proc-upsert").all()}
        self.assertNotIn(undecided.id, remaining_ids, "an undecided candidate with no history can be dropped")
        self.assertIn(decided.id, remaining_ids, "a decided finding must never be silently deleted")
        row = self.db.get(EvidenceGroup, decided.id)
        self.assertTrue(row.needs_reverification)
        self.assertTrue((row.delta or {}).get("evidence_no_longer_found"))


class ObsoleteRevisionFilterTests(unittest.TestCase):
    def test_a_document_superseded_within_the_same_set_is_dropped(self):
        old = SimpleNamespace(id=1, successor_id=2)
        new = SimpleNamespace(id=2, successor_id=None)
        unrelated = SimpleNamespace(id=3, successor_id=None)
        result = current_document_versions([old, new, unrelated])
        self.assertEqual({d.id for d in result}, {2, 3})

    def test_a_successor_pointing_outside_the_set_does_not_drop_the_document(self):
        doc = SimpleNamespace(id=1, successor_id=99)
        result = current_document_versions([doc])
        self.assertEqual([d.id for d in result], [1])

    def test_documents_with_no_chain_link_pass_through_unchanged(self):
        # This is the shape of every pre-seeded official-dataset document:
        # predecessor_id/successor_id are never populated for it.
        docs = [SimpleNamespace(id=i, successor_id=None) for i in range(1, 4)]
        result = current_document_versions(docs)
        self.assertEqual([d.id for d in result], [1, 2, 3])


class DuplicateUploadNoReworkTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _write(self, name: str, content: bytes) -> Path:
        path = Path(self.tmpdir.name) / name
        path.write_bytes(content)
        return path

    def test_reuploading_identical_bytes_via_a_fresh_ingest_is_a_no_op(self):
        job1 = SimpleNamespace(project_id=1, organization_id=1, document_id=501, filename="AR-plan-РД.pdf", source_type="rag")
        doc1, changed1 = ensure_document_version_for_upload_job(self.db, job1, self._write("v1.pdf", b"same-bytes"))
        self.assertTrue(changed1)

        # A different upload job (RAG assigns a fresh source_document_id on
        # every ingest), but byte-identical content.
        job2 = SimpleNamespace(project_id=1, organization_id=1, document_id=502, filename="AR-plan-РД.pdf", source_type="rag")
        doc2, changed2 = ensure_document_version_for_upload_job(self.db, job2, self._write("v2.pdf", b"same-bytes"))
        self.assertFalse(changed2, "a byte-identical re-upload must not be treated as new evidence")
        self.assertEqual(doc2.id, doc1.id)
        self.assertEqual(self.db.query(DocumentVersion).count(), 1)

    def test_reuploading_different_bytes_creates_a_linked_new_revision(self):
        job1 = SimpleNamespace(project_id=2, organization_id=1, document_id=601, filename="AR-plan-РД.pdf", source_type="rag")
        doc1, changed1 = ensure_document_version_for_upload_job(self.db, job1, self._write("a1.pdf", b"version-one"))
        self.assertTrue(changed1)

        job2 = SimpleNamespace(project_id=2, organization_id=1, document_id=602, filename="AR-plan-РД.pdf", source_type="rag")
        doc2, changed2 = ensure_document_version_for_upload_job(self.db, job2, self._write("a2.pdf", b"version-two-is-different"))
        self.assertTrue(changed2)
        self.assertNotEqual(doc2.id, doc1.id)
        self.assertEqual(doc1.successor_id, doc2.id)
        self.assertEqual(doc2.predecessor_id, doc1.id)


class ImpactScopedExtractionIsCheaperThanFullTests(unittest.TestCase):
    """Representative-incremental-run-is-faster-than-full, made deterministic:
    scoping extraction to one rule code must skip page rendering for the
    others entirely, not just filter the resulting EvidenceGroup rows."""

    @staticmethod
    def _document(doc_id: int, stage: str = "PD"):
        return SimpleNamespace(
            id=doc_id, dataset_file_id=f"F{doc_id}", filename=f"doc{doc_id}.pdf",
            file_hash="h", content_hash="h", discipline="KR", document_code=f"DOC{doc_id}",
            revision="1", approval_status="APPROVED", dataset_stage=stage, dataset_section=None,
            dataset_metadata={}, doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage],
        )

    @staticmethod
    def _fragment(doc_id: int, page: int, code: str):
        return SimpleNamespace(
            document_version_id=doc_id, page=page, source_system="learning_annotation",
            metadata_json={"annotation_type": "MATRIX_FIELD", "status": "AUTO_FIELD_CANDIDATE", "code": code},
            confidence=0.9, id=doc_id * 100 + page, text="",
        )

    def test_scoping_to_one_code_never_renders_pages_for_the_others(self):
        docs = [self._document(1), self._document(2)]
        fragments = [self._fragment(1, 1, "KR-055"), self._fragment(2, 1, "PZ-009")]

        calls: list[int] = []

        def fake_extract(document, pages):
            calls.append(document.id)
            return {p: {"page": p, "width": 100.0, "height": 100.0, "text": "", "words": []} for p in pages}

        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            extract_official_rule_observations(docs, fragments, only_codes={"KR-055"})
        scoped_touched = set(calls)

        calls.clear()
        with patch("app.domain.official_rule_packs.extract_original_pages", side_effect=fake_extract):
            extract_official_rule_observations(docs, fragments, only_codes=None)
        full_touched = set(calls)

        self.assertEqual(scoped_touched, {1})
        self.assertEqual(full_touched, {1, 2})
        self.assertLess(len(scoped_touched), len(full_touched))

    def test_empty_impact_set_scans_nothing(self):
        docs = [self._document(1)]
        fragments = [self._fragment(1, 1, "KR-055")]
        with patch("app.domain.official_rule_packs.extract_original_pages") as mock_extract:
            output, _ctx = extract_official_rule_observations(docs, fragments, only_codes=set())
        mock_extract.assert_not_called()
        self.assertEqual(output, {})


class _SyntheticDemoProcessTestCase(unittest.TestCase):
    """Shared fixture: an organization/project seeded with the synthetic
    CASE10 wall dataset (3 attributes -> 3 CANDIDATE findings on first run)
    on the demo matrix, driven directly through the domain layer so tests can
    control exactly which parameter is targeted by an incremental rerun."""

    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        org = Organization(name="Incremental Test Org", slug="incremental-test-org")
        self.db.add(org)
        self.db.flush()
        self.user = User(
            login="incremental-admin", email="admin@incremental.test", password_hash="x",
            api_key="incremental-key", is_admin=True, role="ADMIN", organization_id=org.id,
        )
        project = Project(name="Incremental Project", description="incremental recompute", organization_id=org.id)
        self.db.add_all([self.user, project])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(project)
        self.project = project

    def tearDown(self):
        self.db.close()

    def _run_full(self):
        ensure_synthetic_case10_dataset(self.db, project_id=self.project.id, organization_id=self.project.organization_id)
        ensure_demo_matrix(self.db, organization_id=self.project.organization_id, project_id=self.project.id)
        process = get_or_create_open_process(
            self.db, project_id=self.project.id, organization_id=self.project.organization_id, matrix_version=MATRIX_VERSION_DEMO,
        )
        self.db.commit()
        return run_process(self.db, process_id=str(process.id), user_id=self.user.id)

    def _groups_by_code(self, process) -> dict[str, EvidenceGroup]:
        rows = self.db.query(EvidenceGroup).filter_by(process_id=process.id).all()
        out: dict[str, EvidenceGroup] = {}
        for row in rows:
            param = self.db.get(Param, row.param_id)
            if param is not None:
                out[str(param.code)] = row
        return out

    def _mutate_wall_attribute(self, stage: str, attribute_name: str, new_value: str) -> None:
        doc = (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.project_id == self.project.id, DocumentVersion.doc_stage == stage)
            .one()
        )
        row = (
            self.db.query(AttributeObservation)
            .join(EntityObservation, AttributeObservation.entity_observation_id == EntityObservation.id)
            .filter(EntityObservation.document_version_id == doc.id, AttributeObservation.attribute_name == attribute_name)
            .one()
        )
        row.raw_value = new_value
        row.normalized_value = new_value
        row.normalized_numeric = float(new_value) if new_value.replace(".", "", 1).isdigit() else None
        self.db.add(row)
        self.db.commit()


class ImpactScopedRecomputeTests(_SyntheticDemoProcessTestCase):
    def test_scoped_rerun_only_touches_the_targeted_parameter(self):
        process = self._run_full()
        groups = self._groups_by_code(process)
        fire, material, thickness = groups["DEMO-KR-55"], groups["DEMO-KR-02"], groups["DEMO-AR-01"]

        record_inspector_decision(self.db, evidence_group_id=fire.id, decision="Confirm", reason_code=None, comment=None, user_id=self.user.id)
        record_inspector_decision(self.db, evidence_group_id=material.id, decision="Reject", reason_code="OTHER", comment="not a violation", user_id=self.user.id)

        fire_before = self.db.get(EvidenceGroup, fire.id)
        material_before = self.db.get(EvidenceGroup, material.id)
        fire_snapshot = (fire_before.id, fire_before.finding_status, fire_before.updated_at, fire_before.expected_value, fire_before.actual_value)
        material_snapshot = (material_before.id, material_before.finding_status, material_before.updated_at, material_before.expected_value, material_before.actual_value)
        fire_decision_count_before = self.db.query(EvidenceDecision).filter_by(evidence_group_id=fire.id).count()

        # New RD revision content changes the thickness figure only.
        self._mutate_wall_attribute("working", "thickness", "190")

        run_process(self.db, process_id=str(process.id), user_id=self.user.id, affected_param_codes=["DEMO-AR-01"])

        fire_after = self.db.get(EvidenceGroup, fire.id)
        material_after = self.db.get(EvidenceGroup, material.id)
        self.assertEqual(
            (fire_after.id, fire_after.finding_status, fire_after.updated_at, fire_after.expected_value, fire_after.actual_value),
            fire_snapshot,
            "an unrelated file/parameter must not change an unaffected finding",
        )
        self.assertEqual(
            (material_after.id, material_after.finding_status, material_after.updated_at, material_after.expected_value, material_after.actual_value),
            material_snapshot,
        )
        self.assertEqual(self.db.query(EvidenceDecision).filter_by(evidence_group_id=fire.id).count(), fire_decision_count_before)

        thickness_after = self.db.get(EvidenceGroup, thickness.id)
        self.assertEqual(thickness_after.id, thickness.id, "the affected finding is refreshed in place, not recreated")
        self.assertIn("190", str(thickness_after.expected_value))

    def test_a_changed_basis_on_an_already_decided_finding_is_marked_not_silently_overwritten(self):
        process = self._run_full()
        groups = self._groups_by_code(process)
        thickness = groups["DEMO-AR-01"]

        record_inspector_decision(self.db, evidence_group_id=thickness.id, decision="Confirm", reason_code=None, comment="looks like a real violation", user_id=self.user.id)
        decided = self.db.get(EvidenceGroup, thickness.id)
        self.assertEqual(decided.finding_status, "CONFIRMED_VIOLATION")

        self._mutate_wall_attribute("working", "thickness", "190")
        run_process(self.db, process_id=str(process.id), user_id=self.user.id, affected_param_codes=["DEMO-AR-01"])

        after = self.db.get(EvidenceGroup, thickness.id)
        self.assertEqual(after.id, thickness.id)
        self.assertEqual(after.finding_status, "CONFIRMED_VIOLATION", "an inspector decision must never be silently reset")
        self.assertTrue(after.needs_reverification)
        pending = (after.delta or {}).get("pending_reverification")
        self.assertIsNotNone(pending)
        self.assertIn("190", str(pending.get("expected_value")))
        self.assertEqual(self.db.query(EvidenceDecision).filter_by(evidence_group_id=thickness.id).count(), 1)

        # Deciding again folds the reviewed values in and clears the flag.
        record_inspector_decision(self.db, evidence_group_id=thickness.id, decision="Confirm", reason_code=None, comment="re-reviewed", user_id=self.user.id)
        reverified = self.db.get(EvidenceGroup, thickness.id)
        self.assertFalse(reverified.needs_reverification)
        self.assertIn("190", str(reverified.expected_value))
        self.assertEqual(self.db.query(EvidenceDecision).filter_by(evidence_group_id=thickness.id).count(), 2)

    def test_new_approved_revision_replaces_the_old_one_as_the_reference(self):
        process = self._run_full()
        groups = self._groups_by_code(process)
        thickness = groups["DEMO-AR-01"]
        self.assertIn("200", str(thickness.expected_value))

        old_rd_doc = (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.project_id == self.project.id, DocumentVersion.doc_stage == "working")
            .one()
        )
        wall_entity_id = (
            self.db.query(EntityObservation.canonical_entity_id)
            .filter(EntityObservation.document_version_id == old_rd_doc.id)
            .scalar()
        )
        new_rd_doc = DocumentVersion(
            project_id=self.project.id, organization_id=self.project.organization_id,
            source_type="synthetic", object_id=old_rd_doc.object_id, filename="SYNTHETIC_R_AR_rev2.pdf",
            doc_stage="working", document_stage="working", discipline="AR", document_code=old_rd_doc.document_code,
            version="v2", revision="2", approval_status="APPROVED",
            content_hash="case10-synthetic-v1:working:rev2", file_hash="case10-synthetic-v1:working:rev2",
            file_path="synthetic://case10-synthetic-v1/working/rev2", predecessor_id=old_rd_doc.id,
        )
        self.db.add(new_rd_doc)
        self.db.flush()
        old_rd_doc.successor_id = new_rd_doc.id
        self.db.add(old_rd_doc)

        from app.db.models import SourceFragment

        fragment = SourceFragment(
            document_version_id=new_rd_doc.id, page=1, bbox=[0.11, 0.32, 0.43, 0.37],
            text="Р: Wall A1 (A1), thickness = 195 mm", fragment_type="text", source_system="synthetic",
            external_id="case10-synthetic-v1:working:wall:rev2", extractor="synthetic_case10_fixture", confidence=0.99,
        )
        self.db.add(fragment)
        self.db.flush()
        observation = EntityObservation(
            canonical_entity_id=wall_entity_id, document_version_id=new_rd_doc.id, source_fragment_id=fragment.id,
            raw_name="Wall A1", raw_mark="A1", normalized_name="wall a1", stage="working",
            location={"building": "1", "floor": "3", "axes": "A-B / 4-7"}, confidence=1.0, extractor="synthetic_case10_fixture",
        )
        self.db.add(observation)
        self.db.flush()
        self.db.add(AttributeObservation(
            entity_observation_id=observation.id, source_fragment_id=fragment.id, attribute_name="thickness",
            raw_value="195", normalized_value="195", normalized_numeric=195.0, unit="mm", confidence=0.99,
            extractor="synthetic_case10_fixture",
        ))
        self.db.commit()

        run_process(self.db, process_id=str(process.id), user_id=self.user.id, affected_param_codes=["DEMO-AR-01"])

        after = self.db.get(EvidenceGroup, thickness.id)
        self.assertIn("195", str(after.expected_value), "the new approved revision must be used as the reference")
        self.assertNotIn("200", str(after.expected_value))


class ProtocolVersionHistoryTests(_SyntheticDemoProcessTestCase):
    def test_previous_protocol_version_is_preserved_with_an_understandable_diff(self):
        process = self._run_full()
        protocol_v1 = latest_protocol(self.db, project_id=self.project.id, organization_id=self.project.organization_id, process_id=process.id)
        v1_id, v1_version, v1_payload_snapshot = protocol_v1.id, protocol_v1.version, dict(protocol_v1.payload_json)

        groups = self._groups_by_code(process)
        record_inspector_decision(self.db, evidence_group_id=groups["DEMO-KR-55"].id, decision="Confirm", reason_code=None, comment=None, user_id=self.user.id)

        protocol_v2 = latest_protocol(self.db, project_id=self.project.id, organization_id=self.project.organization_id, process_id=process.id)
        self.assertGreater(protocol_v2.version, v1_version)
        self.assertNotEqual(protocol_v2.id, v1_id)

        stored_v1 = self.db.get(Protocol, v1_id)
        self.assertEqual(stored_v1.payload_json["findings"], v1_payload_snapshot["findings"], "an old protocol version must not mutate after a later run")

        diff = protocol_v2.payload_json["diff"]
        self.assertFalse(diff["baseline"])
        self.assertIn(int(groups["DEMO-KR-55"].id), diff["updated"])
        self.assertIn(int(groups["DEMO-AR-01"].id), diff["unchanged"])
        self.assertNotIn(int(groups["DEMO-AR-01"].id), diff["updated"])


class FinalizedProtocolIncrementBlockedTests(_SyntheticDemoProcessTestCase):
    def test_run_process_is_rejected_after_finalize_and_a_new_process_starts_a_fresh_protocol(self):
        process = self._run_full()
        for group in self.db.query(EvidenceGroup).filter_by(process_id=process.id).all():
            if group.finding_status == "CANDIDATE":
                record_inspector_decision(self.db, evidence_group_id=group.id, decision="Reject", reason_code="OTHER", comment="resolved", user_id=self.user.id)

        protocol = latest_protocol(self.db, project_id=self.project.id, organization_id=self.project.organization_id, process_id=process.id)
        finalize_protocol(self.db, protocol_id=protocol.id, user_id=self.user.id)

        with self.assertRaises(HTTPException) as ctx:
            run_process(self.db, process_id=str(process.id), user_id=self.user.id, affected_param_codes=["DEMO-AR-01"])
        self.assertEqual(ctx.exception.status_code, 409)

        new_process = get_or_create_open_process(
            self.db, project_id=self.project.id, organization_id=self.project.organization_id, matrix_version=MATRIX_VERSION_DEMO,
        )
        self.assertNotEqual(new_process.id, process.id, "dozagruzka must start a new protocol, never mutate the finalized one")


if __name__ == "__main__":
    unittest.main()
