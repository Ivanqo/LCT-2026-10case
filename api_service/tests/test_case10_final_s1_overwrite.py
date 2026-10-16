"""Final-round Step 1: "attribute values get overwritten on re-check" (expert feedback, 2026-10-16).

One test class per hypothesis of CASE10_FINAL_PLAN_AND_PROMPTS.md (H1..H5). Every test says in its docstring whether
the hypothesis was REPRODUCED on f0753c9 (the test failed there; the fix is in the same change) or NOT REPRODUCED (kept
as a guard). Fixtures are synthetic: real PDFs written to a temp dir with Latin parameter names, no object id / file id /
page / GOLD value of the competition data anywhere.

H1  evidence_groups.upsert_evidence_group + group keys      -> stale flag / stale pending values  (REPRODUCED x4)
H2  one value per attribute chosen in extraction            -> input-order dependence, silent collapse (REPRODUCED)
H3  incremental recompute, sweep, touched_keys              -> NOT REPRODUCED (guards), except the H1 flag bugs
H4  live tagger merge / cache                               -> NOT REPRODUCED (guards) + 1 latent last-wins fixed
H5  evaluation/metrics.py, evaluation/grader_sim.py         -> order dependence + duplicate TP (REPRODUCED)
"""
from __future__ import annotations

import copy
import json
import re
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import AuditLog, EvidenceDecision, EvidenceFragment, EvidenceGroup, Organization, Project, User
from app.db.session import Base
from app.domain import official_evidence
from app.domain.evidence_groups import sweep_orphaned_evidence_groups, upsert_evidence_group
from s1_overwrite_support import Harness


# --------------------------------------------------------------------------------------------------------------
# H1 -- upsert_evidence_group / group keys
# --------------------------------------------------------------------------------------------------------------
class H1UpsertDecisionPathTests(unittest.TestCase):
    """The decision-preserving branch of `upsert_evidence_group` parks the fresh values under
    `delta.pending_reverification` and flags the row. Four ways that flag/pending pair went stale (REPRODUCED)."""

    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(id="proc-h1", project_id=1, organization_id=1, object_id="OBJ-1")
        self.param = SimpleNamespace(id=1, code="DEMO-X")

    def tearDown(self):
        self.db.close()

    @staticmethod
    def _fields(actual="180"):
        return {"finding_status": "CANDIDATE", "expected_value": "200", "actual_value": actual,
                "delta": {"expected": "200", "actual": actual, "equal": False}, "review_priority": "HIGH"}

    @staticmethod
    def _specs(value="180", page=1):
        return [{"document_version_id": 10, "source_fragment_id": 20, "page": page, "extracted_value": value,
                 "role": "actual", "file_sha256": "h1", "stage": "working", "dataset_file_id": "F-1"}]

    def _write(self, actual, page=1):
        group, write = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields(actual),
                                             fragment_specs=self._specs(actual, page))
        if group is not None and write:
            for spec in self._specs(actual, page):
                self.db.add(EvidenceFragment(evidence_group_id=group.id, **{**spec, "document_version_id": 10}))
            self.db.flush()
        return group

    def _decided_group(self):
        from app.db.models import DocumentVersion, SourceFragment

        self.db.add(DocumentVersion(id=10, project_id=1, organization_id=1, source_type="t", filename="f.pdf"))
        self.db.add(SourceFragment(id=20, document_version_id=10, page=1, text="x", fragment_type="t", source_system="t",
                                   external_id="e", extractor="t"))
        self.db.flush()
        group = self._write("180")
        self.db.add(EvidenceDecision(evidence_group_id=group.id, decision="Confirm", user_id=None))
        group.finding_status = "CONFIRMED_VIOLATION"
        self.db.commit()
        return group

    def test_evidence_returning_to_the_reviewed_basis_clears_the_stale_flag(self):
        """REPRODUCED: basis A (reviewed) -> B (flagged, pending=B) -> A again returned (None, False) and left the
        row flagged with pending values B that no longer describe anything."""
        group = self._decided_group()
        self._write("170")
        self.assertTrue(self.db.get(EvidenceGroup, group.id).needs_reverification)
        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        row = self.db.get(EvidenceGroup, group.id)
        self.assertFalse(row.needs_reverification, "evidence is back to what the inspector reviewed")
        self.assertNotIn("pending_reverification", row.delta or {})
        self.assertEqual(row.actual_value, "180")
        self.assertEqual(row.finding_status, "CONFIRMED_VIOLATION")

    def test_a_stale_pending_value_never_replaces_the_reviewed_value(self):
        """REPRODUCED: with the flag left behind, the inspector's next decision folded the stale pending value
        (170) over the value the evidence actually shows (180) -- a silent overwrite."""
        from app.domain.v3_pipeline import _apply_pending_reverification

        group = self._decided_group()
        self._write("170")
        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        row = self.db.get(EvidenceGroup, group.id)
        _apply_pending_reverification(self.db, row)
        self.assertEqual(row.actual_value, "180")

    def test_evidence_that_reappears_after_being_lost_clears_the_no_longer_found_flag(self):
        """REPRODUCED: sweep flags a decided group `evidence_no_longer_found`; when the same evidence is found again
        the basis hash matches and nothing cleared the flag."""
        group = self._decided_group()
        sweep_orphaned_evidence_groups(self.db, self.process, {1}, touched_keys={1: set()})
        self.assertTrue(self.db.get(EvidenceGroup, group.id).needs_reverification)
        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        row = self.db.get(EvidenceGroup, group.id)
        self.assertFalse(row.needs_reverification)
        self.assertNotIn("evidence_no_longer_found", row.delta or {})

    def test_a_reviewed_change_is_not_flagged_again_by_the_next_unchanged_run(self):
        """REPRODUCED: after the inspector re-decided on the flagged group, only the VALUES were folded in; the
        cited fragments stayed old, so the very next unchanged run computed a different basis hash and flagged the
        same, already-reviewed change a second time."""
        from app.domain.v3_pipeline import _apply_pending_reverification

        group = self._decided_group()
        self._write("170", page=2)                                     # new evidence cites another page
        row = self.db.get(EvidenceGroup, group.id)
        self.assertTrue(row.needs_reverification)
        _apply_pending_reverification(self.db, row)                    # what record_inspector_decision does
        self.db.commit()
        self.assertFalse(row.needs_reverification)
        self.assertEqual([f.page for f in row.fragments], [2], "the reviewed evidence is what the card shows")
        audits_before = self.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count()
        upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("170"), fragment_specs=self._specs("170", page=2))
        row = self.db.get(EvidenceGroup, group.id)
        self.assertFalse(row.needs_reverification, "nothing changed since the inspector reviewed it")
        self.assertEqual(self.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count(), audits_before)

    def test_database_ids_alone_do_not_change_the_basis(self):
        """NOT REPRODUCED (guard): the basis hash contains document/fragment DB ids; re-ingesting the same bytes
        does not mint new ids (`ensure_document_version_for_upload_job` is a no-op for identical bytes), so ids are a
        stable part of the basis. Identical specs -> identical hash -> no write."""
        group = self._write("180")
        again, write = upsert_evidence_group(self.db, self.process, self.param, "k1", fields=self._fields("180"), fragment_specs=self._specs("180"))
        self.assertEqual((again, write), (None, False))
        self.assertEqual(self.db.query(EvidenceGroup).count(), 1)
        self.assertIsNotNone(group)


class H1GroupKeyTests(unittest.TestCase):
    """NOT REPRODUCED (guards): mechanisms cannot overwrite each other's group for one parameter."""

    def test_mechanism_group_keys_are_pairwise_distinct_and_never_loc_prefixed(self):
        source = open(official_evidence.__file__, encoding="utf-8").read()
        literals = re.findall(r'group_key\s*=\s*"([^"]+)"', source) + re.findall(r'group_key="([^"]+)"', source)
        generic = sorted({key for key in literals if key.startswith("generic:")})
        self.assertEqual(generic, ["generic:anchor", "generic:compound", "generic:enum", "generic:table_count"])
        self.assertFalse(any(key.startswith("loc:") for key in literals), "rule-pack keys are built from the location only")
        self.assertEqual(len({"annotation", "low_quality", *generic}), 2 + len(generic))
        # the rule-pack key space ("loc:<location>") cannot reach a mechanism key whatever the location says
        self.assertTrue(all(f"loc:{key}" != key for key in generic))

    def test_one_parameter_gets_one_comparison_group_per_run_whichever_mechanism_answers(self):
        """The loop `continue`s after the first mechanism that answers, so a parameter's comparison group is written
        by exactly one mechanism per run; a quality-warning group is the only legitimate second row."""
        h = Harness()
        try:
            h.add_doc("PD-1", "PD", [["Total building area 1000,5 m2"]])
            h.add_doc("RD-1", "RD", [["Total building area 1010,0 m2"]])
            h.run()
            keys = [key for (code, key) in h.groups() if code == "ZZ-001"]
            self.assertEqual(keys, ["generic:anchor"])
        finally:
            h.close()

    def test_a_mechanism_switch_keeps_the_decided_group_and_flags_it(self):
        """NOT REPRODUCED: run 1 compares through the generic tier; the inspector decides; run 2 loses the RD value
        (that document is gone from the pool) so the annotation fallback answers. The decided group is not deleted
        or overwritten -- it is flagged `evidence_no_longer_found`, the fallback gets its own key."""
        h = Harness()
        try:
            h.add_doc("PD-1", "PD", [["Total building area 1000,5 m2"]])
            rd = h.add_doc("RD-1", "RD", [["Total building area 1200,0 m2"]])
            h.run()
            group = h.groups()[("ZZ-001", "generic:anchor")]
            self.assertEqual(group.finding_status, "CANDIDATE")
            h.decide(group)
            group.finding_status = "CONFIRMED_VIOLATION"
            h.db.flush()
            h.docs.remove(rd)
            h.run()
            groups = h.groups()
            decided = groups[("ZZ-001", "generic:anchor")]
            self.assertEqual(decided.finding_status, "CONFIRMED_VIOLATION")
            self.assertEqual(decided.actual_value, "1200,0")
            self.assertTrue(decided.needs_reverification)
            self.assertTrue(decided.delta.get("evidence_no_longer_found"))
            self.assertIn(("ZZ-001", "annotation"), groups)
        finally:
            h.close()


# --------------------------------------------------------------------------------------------------------------
# H2 -- one value per attribute
# --------------------------------------------------------------------------------------------------------------
PD_PAGE = [["Total building area 1000,5 m2"]]


def _two_rd(order: str, values=("1100,0", "1200,0")):
    h = Harness()
    h.add_doc("PD-1", "PD", PD_PAGE)
    docs = [("RD-A", [[f"Total building area {values[0]} m2"]]), ("RD-B", [[f"Total building area {values[1]} m2"]])]
    if order == "reversed":
        docs.reverse()
    for file_id, pages in docs:
        h.add_doc(file_id, "RD", pages)
    return h


class H2OrderIndependenceTests(unittest.TestCase):
    def test_the_rd_value_does_not_depend_on_the_order_documents_were_imported(self):
        """REPRODUCED: two RD documents state different values at equal confidence; the winner was whichever got
        the lower DocumentVersion.id, i.e. the package's file order."""
        snapshots = []
        for order in ("natural", "reversed"):
            h = _two_rd(order)
            try:
                h.run()
                snapshots.append(h.snapshot())
            finally:
                h.close()
        self.assertEqual(snapshots[0], snapshots[1])

    def test_the_pd_value_does_not_depend_on_the_order_documents_were_imported(self):
        """REPRODUCED (same ranking function, PD side)."""
        snapshots = []
        for order in ("natural", "reversed"):
            h = Harness()
            try:
                docs = [("PD-A", [["Total building area 1000,5 m2"]]), ("PD-B", [["Total building area 1300,0 m2"]])]
                if order == "reversed":
                    docs.reverse()
                for file_id, pages in docs:
                    h.add_doc(file_id, "PD", pages)
                h.add_doc("RD-1", "RD", [["Total building area 1000,5 m2"]])
                h.run()
                snapshots.append(h.snapshot())
            finally:
                h.close()
        self.assertEqual(snapshots[0], snapshots[1])

    def test_the_cited_document_of_a_non_comparable_parameter_does_not_depend_on_import_order(self):
        """REPRODUCED: the annotation-context fallback cites the first fragment per stage by (confidence, document id)."""
        cited = []
        for order in ("natural", "reversed"):
            h = Harness([("ZZ-009", "Total building area", "—")])      # no unit -> no comparison tier, fallback only
            try:
                docs = [("PD-A", [["Total building area"]]), ("PD-B", [["Total building area"]])]
                if order == "reversed":
                    docs.reverse()
                for file_id, pages in docs:
                    h.add_doc(file_id, "PD", pages)
                h.add_doc("RD-1", "RD", [["Total building area"]])
                h.run()
                group = h.groups()[("ZZ-009", "annotation")]
                cited.append(sorted((f.dataset_file_id, f.page) for f in group.fragments))
            finally:
                h.close()
        self.assertEqual(cited[0], cited[1])

    def test_rule_group_selection_does_not_depend_on_document_ids(self):
        """REPRODUCED: `_upsert_rule_groups` picked the best observation per stage by (confidence, document.id)."""
        picked = []
        for ids in ((1, 2), (2, 1)):
            h = Harness()
            try:
                params = h.params[0]
                docs = [SimpleNamespace(id=ids[0], dataset_file_id="RD-A", file_hash="sha-a", content_hash="sha-a", discipline="AR",
                                        document_code="A", revision=None, approval_status=None),
                        SimpleNamespace(id=ids[1], dataset_file_id="RD-B", file_hash="sha-b", content_hash="sha-b", discipline="AR",
                                        document_code="B", revision=None, approval_status=None)]
                pd_doc = SimpleNamespace(id=3, dataset_file_id="PD-1", file_hash="sha-p", content_hash="sha-p", discipline="AR",
                                         document_code="P", revision=None, approval_status=None)

                def obs(stage, doc, value, normalized):
                    return SimpleNamespace(location="Room 1", stage=stage, confidence=0.9, document=doc, page=1, value=value,
                                           normalized_value=normalized, bbox_normalized=[0.1, 0.1, 0.2, 0.2], bbox_pdf=[1, 1, 2, 2],
                                           page_width=100.0, page_height=100.0, source_fragment=None, extractor="rule", context="c")

                observations = [obs("PD", pd_doc, "10", "10"), obs("RD", docs[0], "11", "11"), obs("RD", docs[1], "12", "12")]
                with patch("app.domain.official_evidence.rule_is_violation", return_value=False):
                    official_evidence._upsert_rule_groups(h.db, h.process, params, observations, documents=None, budget=None)
                group = h.db.query(EvidenceGroup).filter_by(group_key="loc:Room 1").one()
                picked.append(group.actual_value)
            finally:
                h.close()
        self.assertEqual(picked[0], picked[1])


class H2CollapseTests(unittest.TestCase):
    def test_distinct_competing_values_in_one_stage_are_kept_as_alternatives(self):
        """REPRODUCED: two RD documents (plus a PD value) state different numbers for one attribute; one was
        silently dropped, so a reviewer could never learn it existed."""
        h = _two_rd("natural")
        try:
            h.run()
            group = h.groups()[("ZZ-001", "generic:anchor")]
            alternatives = (group.delta or {}).get("alternatives") or {}
            seen = {(row["file_id"], row["value"]) for row in alternatives.get("RD", [])}
            chosen = (group.fragments and next(f for f in group.fragments if f.role == "actual").dataset_file_id,
                      group.actual_value)
            both = {("RD-A", "1100,0"), ("RD-B", "1200,0")}
            self.assertEqual(seen | {chosen}, both, "the chosen value and the alternatives together are every value found")
            self.assertEqual(len(seen), 1, "the chosen value is not repeated among its own alternatives")
        finally:
            h.close()

    def test_equal_values_on_several_documents_are_not_reported_as_alternatives(self):
        """Guard: the same value repeated on several sheets is corroboration, not ambiguity."""
        h = _two_rd("natural", values=("1100,0", "1100,0"))
        try:
            h.run()
            group = h.groups()[("ZZ-001", "generic:anchor")]
            self.assertNotIn("alternatives", group.delta or {})
        finally:
            h.close()

    def test_a_single_candidate_keeps_the_delta_byte_identical(self):
        """Guard: no competing value -> no new key, so every existing export/basis hash is unchanged."""
        h = Harness()
        try:
            h.add_doc("PD-1", "PD", PD_PAGE)
            h.add_doc("RD-1", "RD", [["Total building area 1200,0 m2"]])
            h.run()
            delta = h.groups()[("ZZ-001", "generic:anchor")].delta
            self.assertNotIn("alternatives", delta)
        finally:
            h.close()


class H2WholePackagePermutationTests(unittest.TestCase):
    def test_a_multi_parameter_package_gives_the_same_groups_for_every_import_order(self):
        """Whole-chain guard (tagger -> pool -> gate -> extractors -> upsert): three parameters, two PD and three RD
        documents (one value contested, one corroborated, one single), imported in six shuffled orders."""
        import random

        params = [("ZZ-001", "Total building area", "м²"), ("ZZ-002", "Construction volume", "м³"), ("ZZ-003", "Plot area size", "м²")]
        docs = [
            ("PD-1", "PD", [["Total building area 1000,5 m2", "Construction volume 5000,0 m3"]]),
            ("PD-2", "PD", [["Plot area size 300,0 m2"]]),
            ("RD-1", "RD", [["Total building area 1000,5 m2", "Construction volume 5100,0 m3"]]),
            ("RD-2", "RD", [["Total building area 1010,0 m2", "Plot area size 300,0 m2"]]),
            ("RD-3", "RD", [["Construction volume 5100,0 m3", "Total building area 1000,5 m2"]]),
        ]
        snapshots = []
        for seed in range(6):
            order = list(docs)
            random.Random(seed).shuffle(order)
            h = Harness(params)
            try:
                for file_id, stage, pages in order:
                    h.add_doc(file_id, stage, pages)
                h.run()
                snapshots.append(json.dumps(h.snapshot(), sort_keys=True, default=str))
            finally:
                h.close()
        self.assertEqual(len(set(snapshots)), 1)
        self.assertIn("generic:anchor", snapshots[0])


# --------------------------------------------------------------------------------------------------------------
# H3 -- incremental recompute / sweep
# --------------------------------------------------------------------------------------------------------------
class H3RecomputeTests(unittest.TestCase):
    def test_a_second_identical_run_changes_nothing(self):
        """NOT REPRODUCED (guard): double run of one package -> same rows, same hashes, no audit, no new rows."""
        h = _two_rd("natural")
        try:
            h.run()
            first = h.snapshot()
            hashes = {k: g.evidence_basis_hash for k, g in h.groups().items()}
            ids = {k: g.id for k, g in h.groups().items()}
            audits = h.db.query(AuditLog).count()
            h.run()
            self.assertEqual(h.snapshot(), first)
            self.assertEqual({k: g.evidence_basis_hash for k, g in h.groups().items()}, hashes)
            self.assertEqual({k: g.id for k, g in h.groups().items()}, ids)
            self.assertEqual(h.db.query(AuditLog).count(), audits)
        finally:
            h.close()

    def test_a_param_scoped_run_leaves_other_parameters_untouched(self):
        """NOT REPRODUCED (guard) on the official path (the existing test covers the demo path)."""
        h = Harness([("ZZ-001", "Total building area", "м²"), ("ZZ-002", "Construction volume", "м³")])
        try:
            h.add_doc("PD-1", "PD", [["Total building area 1000,5 m2", "Construction volume 5000,0 m3"]])
            h.add_doc("RD-1", "RD", [["Total building area 1000,5 m2", "Construction volume 5200,0 m3"]])
            h.run()
            before = {k: (g.id, g.expected_value, g.actual_value, g.evidence_basis_hash) for k, g in h.groups().items()}
            h.run(codes={"ZZ-001"})
            after = {k: (g.id, g.expected_value, g.actual_value, g.evidence_basis_hash) for k, g in h.groups().items()}
            self.assertEqual(before, after)
        finally:
            h.close()

    def test_a_document_added_later_flags_a_decided_group_and_keeps_the_decision(self):
        """NOT REPRODUCED (guard): the pool changes (a new RD sheet is uploaded) after the inspector decided."""
        h = Harness()
        try:
            h.add_doc("PD-1", "PD", PD_PAGE)
            h.add_doc("RD-1", "RD", [["Total building area 1200,0 m2"]])
            h.run()
            group = h.groups()[("ZZ-001", "generic:anchor")]
            h.decide(group)
            group.finding_status = "CONFIRMED_VIOLATION"
            h.db.flush()
            h.add_doc("RD-0", "RD", [["Total building area 1300,0 m2"]])     # sorts first by content, new evidence
            h.run()
            row = h.groups()[("ZZ-001", "generic:anchor")]
            self.assertEqual(row.finding_status, "CONFIRMED_VIOLATION")
            self.assertEqual(h.db.query(EvidenceDecision).filter_by(evidence_group_id=row.id).count(), 1)
            if row.needs_reverification:                                      # the pool change displaced the value
                self.assertIn("pending_reverification", row.delta)
                self.assertEqual(row.actual_value, "1200,0", "the reviewed value itself is never overwritten")
        finally:
            h.close()

    def test_the_whole_review_cycle_on_the_official_path_settles_after_one_re_review(self):
        """REPRODUCED before the H1 fix (the unit tests above are its parts): decide -> a new document displaces the
        value -> flagged, reviewed value kept -> the inspector re-decides -> an unchanged rerun must stay quiet."""
        from app.db.models import Organization, Project, User
        from app.domain.v3_pipeline import record_inspector_decision

        h = Harness()
        try:
            h.db.add(Organization(id=1, name="o", slug="o"))
            h.db.flush()
            h.db.add_all([Project(id=1, name="p", description="d", organization_id=1),
                          User(id=1, login="u", email="u@x.test", password_hash="x", api_key="k", is_admin=True, role="ADMIN", organization_id=1)])
            h.db.flush()
            h.add_doc("PD-1", "PD", PD_PAGE)
            h.add_doc("RD-1", "RD", [["Total building area 1200,0 m2"]])
            h.run()
            gid = h.groups()[("ZZ-001", "generic:anchor")].id
            record_inspector_decision(h.db, evidence_group_id=gid, decision="Confirm", reason_code=None, comment=None, user_id=1)
            h.add_doc("RD-0", "RD", [["Total building area 1300,0 m2"]])
            h.run()
            flagged = h.db.get(EvidenceGroup, gid)
            self.assertTrue(flagged.needs_reverification)
            self.assertEqual(flagged.actual_value, "1200,0", "the reviewed value is not overwritten by the re-check")
            self.assertEqual(flagged.delta["pending_reverification"]["actual_value"], "1300,0")
            record_inspector_decision(h.db, evidence_group_id=gid, decision="Confirm", reason_code=None, comment="re-reviewed", user_id=1)
            audits = h.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count()
            h.run()
            settled = h.db.get(EvidenceGroup, gid)
            self.assertFalse(settled.needs_reverification)
            self.assertEqual(settled.actual_value, "1300,0")
            self.assertEqual([f.dataset_file_id for f in settled.fragments if f.role == "actual"], ["RD-0"])
            self.assertEqual(h.db.query(AuditLog).filter_by(action="EVIDENCE_BASIS_CHANGED").count(), audits)
            self.assertEqual(h.db.query(EvidenceDecision).filter_by(evidence_group_id=gid).count(), 2)
        finally:
            h.close()

    def test_an_undecided_group_follows_the_new_evidence_in_place(self):
        """NOT REPRODUCED (guard): without a decision the row is refreshed in place (same id), not duplicated."""
        h = Harness()
        try:
            h.add_doc("PD-1", "PD", PD_PAGE)
            h.add_doc("RD-1", "RD", [["Total building area 1200,0 m2"]])
            h.run()
            gid = h.groups()[("ZZ-001", "generic:anchor")].id
            h.docs[1].file_hash = "changed"                                   # same pool, nothing else changes
            h.docs[1].file_hash = h.docs[1].content_hash
            h.run()
            self.assertEqual(h.db.query(EvidenceGroup).count(), 1)
            self.assertEqual(h.groups()[("ZZ-001", "generic:anchor")].id, gid)
        finally:
            h.close()


# --------------------------------------------------------------------------------------------------------------
# H4 -- live tagger merge and cache
# --------------------------------------------------------------------------------------------------------------
class H4TaggerTests(unittest.TestCase):
    def test_group_values_are_the_same_with_a_cold_and_a_warm_tagger_cache(self):
        """NOT REPRODUCED (guard): the SHA-256 cache returns the scan the pages would have produced."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_cache_dir:
            cache_dir = Path(raw_cache_dir)
            snapshots = []
            for _ in range(2):
                h = _two_rd("natural")
                try:
                    with patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_ENABLED", True), \
                         patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_DIR", cache_dir):
                        h.run()
                    snapshots.append(h.snapshot())
                finally:
                    h.close()
            self.assertEqual(snapshots[0], snapshots[1])

    def test_a_forced_rescan_updates_fragments_in_place_and_changes_nothing(self):
        """NOT REPRODUCED (guard): the merge keys fragments by (document, page, code); re-scanning the same bytes
        (a "re-check") neither adds rows nor changes ids or content."""
        from app.db.models import SourceFragment
        from app.domain import live_candidate_tagger as tagger

        h = Harness()
        try:
            h.add_doc("PD-1", "PD", [["Total building area 1000,5 m2"], ["Total building area 1001,5 m2"]])
            tagger.tag_live_candidates(h.db, list(h.docs), h.params)
            before = [(r.id, r.external_id, r.page, r.text, r.confidence) for r in h.db.query(SourceFragment).order_by(SourceFragment.id)]
            tagger.tag_live_candidates(h.db, list(h.docs), h.params, force=True)
            after = [(r.id, r.external_id, r.page, r.text, r.confidence) for r in h.db.query(SourceFragment).order_by(SourceFragment.id)]
            self.assertEqual(len(before), 2)
            self.assertEqual(before, after)
        finally:
            h.close()

    def test_two_phrases_for_one_code_on_one_page_do_not_overwrite_each_other_last_wins(self):
        """LATENT, reproduced with a patched vocabulary (the shipped vocabulary files are empty): the merge keys a
        fragment by (document, page, code); a later phrase of the same code silently replaced the catalog-name hit
        that comes first. First hit (catalog name first) must win and stay."""
        from app.domain import live_candidate_tagger as tagger
        from app.domain.anchor_vocab import anchor_phrases as real_phrases

        def phrases(code, name, stage):
            return [*real_phrases(code, name, stage), "Building area alt"]

        h = Harness()
        try:
            h.add_doc("PD-1", "PD", [["Total building area 1000,5 m2", "Building area alt 7777,0 m2"]])
            with patch.object(tagger, "anchor_phrases", phrases):
                tagger.tag_live_candidates(h.db, list(h.docs), h.params)
            from app.db.models import SourceFragment

            rows = h.db.query(SourceFragment).filter_by(source_system="live_tagger").all()
            self.assertEqual(len(rows), 1)
            self.assertIn("1000,5", rows[0].text)
        finally:
            h.close()


# --------------------------------------------------------------------------------------------------------------
# H5 -- evaluation
# --------------------------------------------------------------------------------------------------------------
class H5MetricsTests(unittest.TestCase):
    def test_alignment_does_not_depend_on_prediction_order(self):
        """REPRODUCED: two predictions on one gold key (object, code, location) -- the first took the gold row, the
        second became an unmatched FP -- so reordering the same predictions changed recall and FP."""
        from evaluation import metrics

        gold = {"findings": [{"id": "G1", "object_id": "O", "parameter_code": "M-001", "location": "SITE", "status": "CONFIRMED_VIOLATION",
                              "is_violation": True}]}
        negative = {"id": "P1", "object_id": "O", "parameter_code": "M-001", "location": "SITE", "status": "NEGATIVE_VERIFIED"}
        positive = {"id": "P2", "object_id": "O", "parameter_code": "M-001", "location": "SITE", "status": "CANDIDATE"}
        results = []
        for order in ((negative, positive), (positive, negative)):
            aligned = metrics._align_predictions(gold, {"findings": [dict(row) for row in order]})
            result = metrics._evaluate_findings(gold["findings"], aligned["findings"])["overall"]
            results.append({key: result.get(key) for key in ("true_positive", "false_positive", "false_negative", "true_negative")}
                           if "true_positive" in result else result)
        self.assertEqual(results[0], results[1])

    def test_duplicate_prediction_ids_are_not_collapsed(self):
        """NOT REPRODUCED (guard): two unmatched predicted violations that share an id both stay false positives."""
        from evaluation import metrics

        gold = {"findings": [{"id": "G1", "object_id": "O", "parameter_code": "M-001", "location": "A", "status": "NEGATIVE_VERIFIED"}]}
        predicted = {"findings": [
            {"id": "X", "object_id": "O", "parameter_code": "M-002", "location": "B", "status": "CANDIDATE"},
            {"id": "X", "object_id": "O", "parameter_code": "M-003", "location": "C", "status": "CANDIDATE"},
        ]}
        aligned = metrics._align_predictions(gold, predicted)
        counts = metrics._evaluate_findings(gold["findings"], aligned["findings"])["overall"]["counts"]
        self.assertEqual(counts["fp"], 2)

    def test_a_duplicated_true_positive_counts_once(self):
        """REPRODUCED in grader_sim: every positive prediction that matched a gold row was a TP, so exporting the
        same finding twice (two groups, one key) doubled TP and inflated precision."""
        from evaluation import grader_sim as gs

        gold = gs.gold_item(gold_id="G1", object_id="O", code="M-001", location="SITE", label=gs.POSITIVE, world="W")
        check = {"parameter_code": "M-001", "location": "SITE", "violation_label": "VIOLATION_PRESENT", "protocol_status": "CRITICAL",
                 "evidence": []}
        preds = [gs.normalize_prediction(copy.deepcopy(check), object_id="O", world="W"),
                 gs.normalize_prediction(copy.deepcopy(check), object_id="O", world="W")]
        report = gs.score_variant([gold], preds, gs.KEY_ONLY)
        self.assertEqual(report["outcomes"].get("tp"), 1)
        self.assertEqual(report["precision_closed"]["k"], 1)


class H1H5ExportIdentifierTests(unittest.TestCase):
    """REPRODUCED on the submission: two groups of one parameter at one location (a page-quality warning beside the
    file-registry row, seen in every package with a low-quality page) shared `finding_id`; two table discrepancies of
    one room number shared `finding_id` too. A consumer indexing by it kept one row and lost the other."""

    @staticmethod
    def _group(group_key, status, reason, code="PZ-002", location="OBJ-1", fragments=()):
        return {"id": 1, "object_id": "OBJ-1", "group_key": group_key, "finding_status": status,
                "parameter": {"code": code}, "fragments": list(fragments),
                "delta": {"reason": reason, "location": location, "source": "t"}}

    def test_two_groups_of_one_parameter_and_location_get_distinct_identifiers(self):
        from evaluation.exporter import protocol_to_submission

        protocol = {"payload": {"object_id": "OBJ-1", "findings": [
            self._group("low_quality", "NOT_COMPARABLE", "values_not_extracted"),
            self._group("annotation", "MISSING_EVIDENCE", "missing_stage"),
        ]}}
        checks = protocol_to_submission(protocol)["checks"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(len({c["finding_id"] for c in checks}), 2)
        self.assertEqual(len({c["evidence_group_id"] for c in checks}), 2)
        # the group whose key sorts first keeps the plain id: the common identifier does not move
        plain = {c["finding_id"] for c in protocol_to_submission({"payload": {"object_id": "OBJ-1", "findings": [
            self._group("annotation", "MISSING_EVIDENCE", "missing_stage")]}})["checks"]}
        self.assertTrue(plain <= {c["finding_id"] for c in checks})

    def test_identifiers_do_not_depend_on_the_order_of_the_groups(self):
        from evaluation.exporter import protocol_to_submission

        groups = [self._group("low_quality", "NOT_COMPARABLE", "values_not_extracted"),
                  self._group("annotation", "MISSING_EVIDENCE", "missing_stage")]
        ids = []
        for order in (groups, list(reversed(groups))):
            checks = protocol_to_submission({"payload": {"object_id": "OBJ-1", "findings": order}})["checks"]
            ids.append({c["finding_status_internal"]: c["finding_id"] for c in checks})
        self.assertEqual(ids[0], ids[1])

    def test_a_package_without_collisions_exports_unchanged(self):
        """Guard: distinct locations / codes keep the plain content-hash identifiers."""
        from evaluation.exporter import protocol_to_submission

        checks = protocol_to_submission({"payload": {"object_id": "OBJ-1", "findings": [
            self._group("annotation", "MISSING_EVIDENCE", "missing_stage", location="A"),
            self._group("annotation", "MISSING_EVIDENCE", "missing_stage", location="B"),
        ]}})["checks"]
        self.assertTrue(all(len(c["finding_id"]) == 18 for c in checks))      # "F-" + 16 hex digits, no suffix


if __name__ == "__main__":
    unittest.main()
