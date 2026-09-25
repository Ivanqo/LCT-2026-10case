"""Comparability gate (Phase 10, prompt B, items 2 and 4): every reason, the group/export wiring, and a replay of
the six checks the SILVER review (pass 1) found the corpus cannot support -- on the repo's own frozen
mechanism snapshots + manifests, so no Docker, no PDF, no E: drive is needed.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.domain import comparison_gate as cg
from app.domain import value_plausibility as vp
from app.domain.official_rule_packs import SUPPORTED_RULE_CODES
from evaluation.exporter import evidence_group_to_submission_check

D = Decimal
REPO = Path(__file__).resolve().parents[2]


def _doc(doc_id: int, file_id: str, path: str, stage: str, section: str = "OTHER", sha: str | None = None, pages: int = 10):
    row = {"file_id": file_id, "stage": stage, "relative_path": path, "section": section, "sha256": sha or f"sha-{file_id}",
           "pdf_pages": pages, "extension": ".pdf", "size_bytes": 1000}
    return SimpleNamespace(
        id=doc_id, dataset_file_id=file_id, dataset_stage=stage, dataset_section=section, discipline=section, filename=path.rsplit("/", 1)[-1],
        doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage], file_hash=row["sha256"], content_hash=None, document_code=file_id,
        revision=None, approval_status="UNKNOWN", dataset_metadata={"document_manifest": row},
    )


_PARAM_IDS = iter(range(1, 10_000))


def _param(code="PZ-004", unit="м³", pd="Раздел ПЗ: Таблица ТЭП", rd="Раздел АР/КР: Лист Общие данные", pid=None):
    pid = pid if pid is not None else next(_PARAM_IDS)   # the gate caches hints per param id: ids must be unique
    return SimpleNamespace(
        id=pid, code=code, unit=unit, trigger_logic="", review_priority="HIGH", source_pd=bool(pd), source_rd=bool(rd), source_id=True,
        other_normative=json.dumps({"source_pd": pd, "source_rd": rd, "source_id": ""}),
    )


def _evidence(verdict=vp.ADJACENT, **extra):
    return {"verdict": verdict, "expected_family": "VOLUME_M3", **extra}


def _obs(doc, value="100", *, unit_evidence=None, page=1):
    facts = {"unit_evidence": unit_evidence or _evidence()}
    return SimpleNamespace(
        document=doc, page=page, decimal_value=D(value), normalized_value=value, value=value, confidence=0.55, gate_facts=facts,
        bbox_normalized=[0.1, 0.1, 0.2, 0.2], bbox_pdf=[1.0, 1.0, 2.0, 2.0], page_width=100.0, page_height=100.0,
        extractor="generic_anchor_numeric", context="ctx", source_fragment=None,
    )


def _ctx(docs, rule_pack_codes=()):
    return cg.GateContext(docs, rule_pack_codes=rule_pack_codes)


def _clean_docs():
    return [
        _doc(1, "P-1", "пд/01-01-00-02-ПЗ.pdf", "PD"),
        _doc(2, "P-2", "пд/01-03-00-01-АР.pdf", "PD", "AR"),
        _doc(3, "R-1", "рд/19-0322-ОК-1_Н-АР2.pdf", "RD", "AR"),
        _doc(4, "R-2", "рд/19-0322-ОК-1_Н-КЖ1.pdf", "RD", "KR"),
    ]


class GateReasonTests(unittest.TestCase):
    def test_a_clean_pair_may_be_compared(self):
        docs = _clean_docs()
        ctx = _ctx(docs)
        self.assertIsNone(ctx.evaluate(_param(), "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2])}))

    def test_rule_pack_codes_are_never_gated(self):
        docs = _clean_docs()
        ctx = _ctx(docs, SUPPORTED_RULE_CODES)
        param = _param(code="IOS4-079", unit="м³/ч / Па / кВт")
        # even a hopeless pair (same file) passes through: that tier is unchanged by construction
        self.assertIsNone(ctx.evaluate(param, "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[0])}))

    def test_trigger_not_evaluable(self):
        docs = _clean_docs()
        decision = _ctx(docs).evaluate(_param(code="POS-081", unit="м"), "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2])})
        self.assertEqual((decision.status, decision.reason), (cg.STATUS_NOT_COMPARABLE, cg.REASON_TRIGGER_NOT_EVALUABLE))

    def test_multi_instance_parameter(self):
        docs = _clean_docs()
        param = _param(code="AR-041", unit="м", pd="Ведомость заполнения проемов (АР)", rd="Спецификация дверей (АР)")
        decision = _ctx(docs).evaluate(param, "numeric", {"PD": _obs(docs[1]), "RD": _obs(docs[2])})
        self.assertEqual(decision.reason, cg.REASON_MULTI_INSTANCE)

    def test_multi_instance_is_not_applied_to_enum_codes(self):
        docs = _clean_docs()
        param = _param(code="AR-041", unit="м", pd="(АР)", rd="(АР)")
        self.assertIsNone(_ctx(docs).evaluate(param, "enum", {"PD": _obs(docs[1]), "RD": _obs(docs[2])}))

    def test_missing_discipline_document_when_the_stage_lacks_the_named_section(self):
        # RD has АР and КЖ only; the parameter's RD source is ЭОМ
        docs = _clean_docs()
        param = _param(code="PZ-014", unit="кВт", pd="Раздел ПЗ: Текст (ТУ)", rd="Раздел ЭОМ: Лист Общие данные")
        decision = _ctx(docs).evaluate(param, "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2])})
        self.assertEqual((decision.status, decision.reason, decision.stages), (cg.STATUS_MISSING_EVIDENCE, cg.REASON_MISSING_DISCIPLINE, ["RD"]))

    def test_an_unrecognisable_file_never_proves_a_section_absent(self):
        docs = _clean_docs() + [_doc(5, "R-3", "рд/чертёж 17.pdf", "RD")]
        param = _param(code="PZ-014", unit="кВт", pd="Раздел ПЗ: Текст (ТУ)", rd="Раздел ЭОМ: Лист Общие данные")
        ctx = _ctx(docs)
        self.assertEqual(ctx.stage_completeness(param, "RD")[0], "UNKNOWN")
        decision = ctx.evaluate(param, "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2])})
        self.assertNotEqual(getattr(decision, "reason", None), cg.REASON_MISSING_DISCIPLINE)

    def test_completeness_verdicts(self):
        ctx = _ctx(_clean_docs())
        self.assertEqual(ctx.stage_completeness(_param(), "PD")[0], "PRESENT")
        self.assertEqual(ctx.stage_completeness(_param(pd="Технический план БТИ"), "PD")[0], "UNCONSTRAINED")
        self.assertEqual(ctx.stage_completeness(_param(rd="Раздел ЭОМ: схемы"), "RD")[0], "ABSENT")
        self.assertEqual(ctx.stage_completeness(_param(), "ID")[0], "UNCONSTRAINED")

    def test_evidence_from_a_section_the_catalog_does_not_name(self):
        docs = _clean_docs() + [_doc(5, "P-9", "пд/01-08-00-02-06-ООС.2.pdf", "PD", "ООС")]
        param = _param(code="KR-067", unit="м³ / т", pd="Сводные таблицы расхода (КР)", rd="Ведомости расхода стали (КЖ/КМ)")
        # PD has no КР volume at all here -> MISSING_DOCUMENT rather than a mismatch
        decision = _ctx(docs).evaluate(param, "numeric", {"PD": _obs(docs[4]), "RD": _obs(docs[3])})
        self.assertEqual(decision.reason, cg.REASON_MISSING_DISCIPLINE)
        # with a КР volume present, citing the ООС page is a section mismatch
        docs.append(_doc(6, "P-10", "пд/01-04-00-01-19-КР.1.pdf", "PD"))
        decision = _ctx(docs).evaluate(param, "numeric", {"PD": _obs(docs[4]), "RD": _obs(docs[3])})
        self.assertEqual((decision.status, decision.reason), (cg.STATUS_NOT_COMPARABLE, cg.REASON_DISCIPLINE_MISMATCH))
        self.assertEqual(decision.details["cited_sections"], ["ООС"])

    def test_enum_tier_is_discipline_gated_on_the_rd_side_only(self):
        # PD: a fire-resistance degree legitimately repeats in ОДИ/ПБ/АР volumes -> not gated
        docs = _clean_docs() + [_doc(5, "P-5", "пд/133-0820-ОК-1-ОДИ Корр. 5.pdf", "PD", "ОДИ"),
                                _doc(6, "R-6", "рд/19-0322-ОК-1_Н-МЗ. Молниезащита_изм.3.pdf", "RD")]
        param = _param(code="PZ-022", unit="Степень", pd="Раздел ПЗ: Противопожарные характеристики", rd="Раздел АР/КР")
        self.assertIsNone(_ctx(docs).evaluate(param, "enum", {"PD": _obs(docs[4]), "RD": _obs(docs[2])}))
        # RD: the class read off a lightning-protection set is not the design value named by the catalog (АР/КР)
        decision = _ctx(docs).evaluate(param, "enum", {"PD": _obs(docs[4]), "RD": _obs(docs[5])})
        self.assertEqual((decision.reason, decision.details["cited_sections"]), (cg.REASON_DISCIPLINE_MISMATCH, ["ЭОМ"]))

    def test_revision_conflict_on_the_cited_file(self):
        docs = [
            _doc(1, "P-1", "пд/133-0820-ОК-1-ПЗ2_(Корр.1) РнС.pdf", "PD"),
            _doc(2, "P-2", "пд/133-0820-ОК-1-ПЗ2_МГЭ_РнИ+.pdf", "PD"),
            _doc(3, "P-3", "пд/133-0820-ОК-1-КР1.pdf", "PD"),
            _doc(4, "R-1", "рд/133-0820-ОК-1-АР1.pdf", "RD", "AR"),
        ]
        param = _param(code="PZ-005")
        ctx = _ctx(docs)
        decision = ctx.evaluate(param, "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[3])})
        self.assertEqual((decision.status, decision.reason), (cg.STATUS_NOT_COMPARABLE, cg.REASON_REVISION_CONFLICT))
        self.assertEqual(decision.details["stage"], "PD")
        self.assertEqual(decision.details["conflicts"][0]["type"], "UNORDERED_REVISIONS")
        # the same parameter read from an unconflicted volume is not blocked by this gate
        self.assertNotEqual(getattr(ctx.evaluate(_param(code="PZ-005", pd="Раздел КР"), "numeric", {"PD": _obs(docs[2]), "RD": _obs(docs[3])}), "reason", None),
                            cg.REASON_REVISION_CONFLICT)

    def test_self_comparison_by_file_and_by_bytes(self):
        docs = _clean_docs()
        ctx = _ctx(docs)
        decision = ctx.evaluate(_param(), "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[0])})
        self.assertEqual(decision.reason, cg.REASON_SELF_COMPARISON)
        twin = _doc(9, "R-9", "рд/19-0322-ОК-1_Н-АР9.pdf", "RD", "AR", sha="sha-P-1")   # same bytes as the PD file
        decision = _ctx(docs + [twin]).evaluate(_param(), "numeric", {"PD": _obs(docs[0]), "RD": _obs(twin)})
        self.assertEqual((decision.reason, decision.details["same"]), (cg.REASON_SELF_COMPARISON, "sha"))

    def test_unit_evidence_gates(self):
        docs = _clean_docs()
        ctx = _ctx(docs)
        mismatch = ctx.evaluate(_param(), "numeric", {
            "PD": _obs(docs[0], unit_evidence=_evidence(vp.MISMATCH, adjacent_family="LENGTH_M")), "RD": _obs(docs[2])})
        self.assertEqual(mismatch.reason, cg.REASON_UNIT_MISMATCH)
        absent = ctx.evaluate(_param(), "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2], unit_evidence=_evidence(vp.ABSENT))})
        self.assertEqual((absent.reason, absent.details["stage"]), (cg.REASON_UNIT_ABSENT, "RD"))
        in_row = ctx.evaluate(_param(), "numeric", {"PD": _obs(docs[0], unit_evidence=_evidence(vp.IN_ROW)), "RD": _obs(docs[2])})
        self.assertIsNone(in_row)

    def test_compound_unit_evidence_is_checked_per_component(self):
        docs = _clean_docs() + [_doc(5, "P-5", "пд/01-04-00-01-19-КР.1.pdf", "PD")]
        obs_pd = _obs(docs[4])
        obs_pd.gate_facts = {"unit_evidence": [{"label": "м³", "verdict": vp.IN_ROW}, {"label": "т", "verdict": vp.ABSENT}]}
        decision = _ctx(docs).evaluate(_param(code="KR-067", unit="м³ / т", pd="(КР)", rd="(КЖ/КМ)"), "compound", {"PD": obs_pd, "RD": _obs(docs[3])})
        self.assertEqual(decision.reason, cg.REASON_UNIT_ABSENT)

    def test_magnitude_mismatch(self):
        docs = _clean_docs()
        decision = _ctx(docs).evaluate(_param(), "numeric", {"PD": _obs(docs[0], "40.95"), "RD": _obs(docs[2], "88942.6")})
        self.assertEqual(decision.reason, cg.REASON_MAGNITUDE)
        self.assertIsNone(_ctx(docs).evaluate(_param(), "numeric", {"PD": _obs(docs[0], "16867.9"), "RD": _obs(docs[2], "25036.27")}))

    def test_table_count_tier_uses_discipline_and_conflict_gates_but_not_units(self):
        docs = _clean_docs()
        param = _param(code="PZ-010", unit="шт.", pd="Раздел ПЗ: Таблица ТЭП", rd="Раздел АР: Сводная экспликация квартир")
        counts = {"PD": _obs(docs[0]), "RD": _obs(docs[2])}
        for obs in counts.values():
            obs.gate_facts = {"unit_evidence": _evidence(vp.ABSENT)}     # irrelevant for a row count
        self.assertIsNone(_ctx(docs).evaluate(param, "table_count", counts))

    def test_decision_delta_shape(self):
        docs = _clean_docs()
        decision = _ctx(docs).evaluate(_param(code="POS-081", unit="м"), "numeric", {"PD": _obs(docs[0]), "RD": _obs(docs[2])})
        delta = decision.to_delta()
        self.assertEqual(delta["reason"], "trigger_not_evaluable")
        self.assertEqual(delta["gate"]["reason"], "trigger_not_evaluable")


class CandidateFilterAndProtocolTests(unittest.TestCase):
    def test_duplicates_and_superseded_editions_do_not_feed_the_generic_tiers(self):
        docs = [
            _doc(1, "F0100", "пд/01-0722-14-П-ПЗУ.pdf", "PD", "GP", sha="S"),
            _doc(2, "F0101", "пд/01-0722-14-П-ПЗУ дубль.pdf", "PD", "GP", sha="S"),
            _doc(3, "F0102", "пд/01-07.22-14-П-ПЗУ-кор2.pdf", "PD", "GP"),
            _doc(4, "F0103", "пд/01-07.22-14-П-ПЗУ-кор3.pdf", "PD", "GP"),
        ]
        ctx = _ctx(docs)
        dropped = {d.id for d in docs if ctx.drop_from_generic_candidates(d.id)}
        self.assertIn(2, dropped)          # byte duplicate of F0100
        self.assertNotIn(4, dropped)       # the newest correction stays
        self.assertIn(1, dropped)          # the canonical copy F0100 is the ORIGINAL edition, superseded by кор3
        self.assertIn(3, dropped)          # кор2 is superseded by кор3
        self.assertEqual(dropped, {1, 2, 3})

    def test_protocol_section_lists_exclusions_conflicts_and_inventory(self):
        docs = _clean_docs() + [_doc(5, "R-5", "рд/Thumbs.db", "RD"), _doc(6, "P-6", "пд/пусто.pdf", "PD", pages=0)]
        section = _ctx(docs).protocol_section()
        self.assertEqual(section["integrity"]["excluded_by_reason"], {"TEMPORARY_OR_SERVICE_FILE": 1, "UNREADABLE_OR_EMPTY_SOURCE_FILE": 1})
        self.assertEqual(section["stage_inventory"]["PD"]["files"], 2)
        self.assertIn("revision_analysis", section)

    def test_a_process_with_no_official_facts_still_builds(self):
        # plain upload: no dataset metadata at all
        doc = SimpleNamespace(id=1, dataset_file_id=None, dataset_stage=None, dataset_section=None, discipline=None, filename="Раздел АР.pdf",
                              doc_stage="project", file_hash="h", content_hash=None, document_code="c", revision=None,
                              approval_status="UNKNOWN", dataset_metadata=None)
        section = _ctx([doc]).protocol_section()
        self.assertEqual(section["stage_inventory"]["PD"]["files"], 1)


class GroupAndExportWiringTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.process = SimpleNamespace(id="p", project_id=1, organization_id=1, object_id="OBJ-T", matrix_version="official-132-v1",
                                       dataset_version="v", upload_scenario="FULL")

    def tearDown(self):
        self.db.close()

    def _group(self, param, docs, observations, tier="numeric"):
        from app.db.models import EvidenceGroup
        from app.domain import official_evidence as oe

        fn = {"numeric": oe._upsert_generic_group, "enum": oe._upsert_enum_group, "compound": oe._upsert_compound_group,
              "table_count": oe._upsert_table_count_group}[tier]
        key = fn(self.db, self.process, param, observations, gate=_ctx(docs))
        group = self.db.query(EvidenceGroup).one()
        return key, group

    @staticmethod
    def _as_dict(group):
        fragments = [{"stage": f.stage, "file_id": f.dataset_file_id, "page": f.page, "extracted_value": f.extracted_value} for f in group.fragments]
        return {"finding_status": group.finding_status, "delta": group.delta, "parameter": {"code": group.param.code if group.param else None},
                "review_priority": group.review_priority, "fragments": fragments}

    def test_a_gated_pair_becomes_comparison_impossible_and_exports_no_values(self):
        docs = _clean_docs()
        param = _param(code="POS-081", unit="м")
        param_row = self._persist_param(param)
        key, group = self._group(param_row, docs, {"PD": _obs(docs[0], "10"), "RD": _obs(docs[2], "50")})
        self.assertEqual(key, "generic:anchor")
        self.assertEqual((group.finding_status, group.comparability_status), ("NOT_COMPARABLE", "NOT_COMPARABLE"))
        self.assertEqual(group.delta["reason"], "trigger_not_evaluable")
        self.assertEqual(group.delta["values_seen"], {"PD": "10", "RD": "50"})
        check = evidence_group_to_submission_check(self._as_dict(group) | {"delta": {**group.delta, "parameter_code": "POS-081"}})
        self.assertEqual((check["violation_label"], check["protocol_status"]), ("COMPARISON_IMPOSSIBLE", "COMPARISON_IMPOSSIBLE"))
        self.assertIsNone(check["pd_value"])
        self.assertIsNone(check["rd_value"])
        self.assertEqual(len(check["evidence"]), 2)           # the cited pages stay visible to the inspector

    def test_a_missing_discipline_document_exports_missing_document(self):
        docs = _clean_docs()
        param = self._persist_param(_param(code="PZ-014", unit="кВт", pd="Раздел ПЗ: Текст (ТУ)", rd="Раздел ЭОМ: Лист Общие данные"))
        _key, group = self._group(param, docs, {"PD": _obs(docs[0], "10"), "RD": _obs(docs[2], "12")})
        self.assertEqual(group.finding_status, "MISSING_EVIDENCE")
        check = evidence_group_to_submission_check(self._as_dict(group) | {"delta": {**group.delta, "parameter_code": "PZ-014"}})
        self.assertEqual((check["violation_label"], check["protocol_status"]), ("MISSING_DOCUMENT", "RD_MISSING"))

    def _persist_param(self, ns):
        from app.db.models import MatrixVersion, Param

        matrix = self.db.query(MatrixVersion).first()
        if matrix is None:
            matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1", status="ACTIVE", source_filename="t", source_hash="h")
            self.db.add(matrix)
            self.db.flush()
        row = Param(matrix_version_id=matrix.id, project_id=1, organization_id=1, code=ns.code, matrix_code=ns.code, scoring_code=ns.code,
                    section="x", parameter_name="p", unit=ns.unit, source_pd=ns.source_pd, source_rd=ns.source_rd, source_id=True,
                    trigger_logic="", review_priority="HIGH", other_normative=ns.other_normative, data_type="number", is_active=True)
        self.db.add(row)
        self.db.flush()
        return row


# --------------------------------------------------------------------------------------------------------------
# Replay of the six unsupported checks on frozen SILVER data (evaluation/silver_new_objects_pass1)
# --------------------------------------------------------------------------------------------------------------
_SILVER = REPO / "evaluation" / "silver_new_objects_pass1"
_CATALOG = next(iter((REPO / "case_data").rglob("parameter_catalog_132.jsonl")), None) if (REPO / "case_data").is_dir() else None

# the six checks the SILVER review labelled NEEDS_REVIEW (corpus cannot support a verdict): object, code
_UNSUPPORTED = [("ALT79B", "KR-067"), ("LOS3A", "AR-041"), ("LOS3A", "KR-067"), ("POL17", "AR-041"), ("POL17", "PZ-005"), ("POL17", "PZ-006")]
# committed groups the review confirmed carry genuine values on both stages, the cited PD volume is not one of several
# competing editions, and the RD value comes from the section the catalog names (КМ is part of "АР/КР")
_GENUINE = [("LOS3A", "PZ-004"), ("POL17", "PZ-022")]
# genuine value, but the RD page is a lightning-protection (МЗ) / drainage (НК2) set that merely repeats the class in
# its general notes -- the value review marked both VALID_NON_AUTHORITATIVE; PZ-022 was a false CANDIDATE (I vs II)
_NON_AUTHORITATIVE_RD = [("LOS3A", "PZ-022"), ("LOS3A", "PZ-023")]
_GENUINE_BUT_CONFLICTED = [("POL17", "PZ-023"), ("DOO25", "PZ-023")]


def _load_silver(obj: str):
    manifest = _SILVER / "manifests" / f"manifest_{obj}.jsonl"
    snapshot = _SILVER / "mechanism_snapshots" / f"snapshot_{obj}.json"
    if not (manifest.exists() and snapshot.exists()):
        return None
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    docs = []
    by_file = {}
    for i, row in enumerate(rows, start=1):
        row = dict(row)
        rel = row["relative_path"].replace("\\", "/")
        doc = SimpleNamespace(
            id=i, dataset_file_id=row["file_id"], dataset_stage=row["stage"], dataset_section=row["section"], discipline=row["section"],
            filename=rel.rsplit("/", 1)[-1], doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[row["stage"]],
            file_hash=row["sha256"], content_hash=None, document_code=row["file_id"], revision=None, approval_status="UNKNOWN",
            dataset_metadata={"document_manifest": row},
        )
        docs.append(doc)
        by_file[row["file_id"]] = doc
    return docs, by_file, json.loads(snapshot.read_text(encoding="utf-8"))["groups"]


def _replay_words(context: str):
    return [{"text": token, "bbox": [i * 10.0, 0.0, i * 10.0 + 9.0, 10.0]} for i, token in enumerate((context or "").split())]


def _locate(words, raw: str):
    """(first, last) word span of the cited number inside its context window (thousand groups merge two words)."""
    from app.domain.anchor_search import to_decimal

    target = to_decimal(raw)
    for i, word in enumerate(words):
        if to_decimal(word["text"].strip(".,;:()")) == target and target is not None:
            return i, i
        if i + 1 < len(words):
            merged = to_decimal((word["text"] + words[i + 1]["text"]).strip(".,;:()"))
            if merged is not None and merged == target:
                return i, i + 1
    return 0, 0


def _replay_observation(group_fragments, stage_internal, by_file, param, tier, raw_values):
    fragment = next(f for f in group_fragments if f["stage"] == stage_internal)
    doc = by_file[fragment["dataset_file_id"]]
    words = _replay_words(fragment["context"])
    row_text = fragment["context"]
    obs = SimpleNamespace(document=doc, page=fragment["page"], confidence=fragment["confidence"], context=fragment["context"], gate_facts={})
    if tier == "numeric":
        first, last = _locate(words, raw_values)
        from app.domain.anchor_search import to_decimal
        obs.decimal_value = to_decimal(raw_values) or D(0)
        obs.gate_facts = {"unit_evidence": vp.unit_evidence(words, first, last, param.unit, row_text=row_text)}
    elif tier == "compound":
        parts = [p.strip() for p in raw_values.split("/")]
        labels = [p.strip() for p in param.unit.split(" / ")]
        evidences = []
        for label, part in zip(labels, parts):
            first, last = _locate(words, part)
            evidences.append({"label": label, **vp.component_unit_evidence(words, first, last, label, row_text=row_text)})
        obs.gate_facts = {"unit_evidence": evidences}
    return obs


@unittest.skipIf(_CATALOG is None or not _SILVER.exists(), "SILVER snapshots / catalog not available")
class SilverReplayTests(unittest.TestCase):
    """The acceptance criterion of prompt B on the frozen mechanism output: the six checks the SILVER review
    found unsupported are no longer verdicts, the groups it found genuine still are."""

    @classmethod
    def setUpClass(cls):
        rows = [json.loads(line) for line in _CATALOG.read_text(encoding="utf-8").splitlines() if line.strip()]
        cls.params = {}
        for i, row in enumerate(rows, start=1):
            cls.params[row["parameter_code"]] = SimpleNamespace(
                id=i, code=row["parameter_code"], unit=row["unit"], trigger_logic=f"trigger={row['trigger']}",
                other_normative=json.dumps({"source_pd": row["source_pd"], "source_rd": row["source_rd"], "source_id": row["source_id"]}),
            )
        cls.objects = {}

    def _decision(self, obj: str, code: str):
        if obj not in self.objects:
            self.objects[obj] = _load_silver(obj)
        loaded = self.objects[obj]
        if loaded is None:
            self.skipTest(f"{obj} snapshot not available")
        docs, by_file, groups = loaded
        group = next(g for g in groups if g["parameter_code"] == code and g["comparability_status"] == "COMPARABLE")
        tier = {"generic-anchor-table-v1": "numeric", "generic-anchor-enum-v1": "enum", "generic-anchor-compound-v1": "compound"}[group["model_version"]]
        param = self.params[code]
        observations = {}
        for stage, internal in (("PD", "project"), ("RD", "working")):
            fragment = next(f for f in group["fragments"] if f["stage"] == internal)
            if tier == "enum":
                obs = SimpleNamespace(document=by_file[fragment["dataset_file_id"]], page=fragment["page"], confidence=0.5, context=fragment["context"], gate_facts=None)
            else:
                obs = _replay_observation(group["fragments"], internal, by_file, param, tier, fragment["extracted_value"])
            observations[stage] = obs
        return _ctx(docs, SUPPORTED_RULE_CODES).evaluate(param, tier, observations)

    def test_the_six_unsupported_checks_are_no_longer_committed(self):
        for obj, code in _UNSUPPORTED:
            with self.subTest(check=f"{obj}::{code}"):
                decision = self._decision(obj, code)
                self.assertIsNotNone(decision, "a verdict would still be committed")
                self.assertIn(decision.status, {cg.STATUS_NOT_COMPARABLE, cg.STATUS_MISSING_EVIDENCE})

    def test_each_of_the_six_has_the_expected_named_reason(self):
        expected = {
            ("ALT79B", "KR-067"): {cg.REASON_REVISION_CONFLICT},                # PD КР mixes two project series
            ("LOS3A", "AR-041"): {cg.REASON_MULTI_INSTANCE},                    # doors: one number per document is arbitrary
            ("LOS3A", "KR-067"): {cg.REASON_DISCIPLINE_MISMATCH},               # PD value from ООС, RD value from АР
            ("POL17", "AR-041"): {cg.REASON_MULTI_INSTANCE},
            ("POL17", "PZ-005"): {cg.REASON_REVISION_CONFLICT},                 # ПЗ2 has two unorderable editions
            ("POL17", "PZ-006"): {cg.REASON_REVISION_CONFLICT},
        }
        for (obj, code), reasons in expected.items():
            with self.subTest(check=f"{obj}::{code}"):
                self.assertIn(self._decision(obj, code).reason, reasons)

    def test_the_groups_the_review_found_genuine_are_still_committed(self):
        for obj, code in _GENUINE:
            with self.subTest(check=f"{obj}::{code}"):
                self.assertIsNone(self._decision(obj, code))

    def test_class_copied_into_an_unrelated_rd_set_is_not_compared(self):
        for obj, code in _NON_AUTHORITATIVE_RD:
            with self.subTest(check=f"{obj}::{code}"):
                decision = self._decision(obj, code)
                self.assertEqual((decision.reason, decision.details["stage"]), (cg.REASON_DISCIPLINE_MISMATCH, "RD"))

    def test_genuine_values_in_a_conflicted_edition_set_abstain_by_design(self):
        for obj, code in _GENUINE_BUT_CONFLICTED:
            with self.subTest(check=f"{obj}::{code}"):
                self.assertEqual(self._decision(obj, code).reason, cg.REASON_REVISION_CONFLICT)


if __name__ == "__main__":
    unittest.main()


class FallbackCompletenessIntegrationTests(unittest.TestCase):
    """`create_official_evidence_groups` end to end (no fragments, no PDFs): the annotation-context fallback now tells
    "the stage has no document of the section the catalog names" (MISSING_DOCUMENT) apart from "documents exist, no
    value found" (COMPARISON_IMPOSSIBLE) -- and leaves the rule-pack codes' fallback exactly as it was."""

    def setUp(self):
        from unittest.mock import patch

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self._patch = patch("app.domain.official_evidence.tag_live_candidates", lambda *a, **k: None)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.db.close)

    def _run(self, params_spec, docs_spec, source_id=False):
        from app.db.models import DocumentVersion, InspectionProcess, MatrixVersion, Param
        from app.domain.official_evidence import create_official_evidence_groups

        matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1", status="ACTIVE", source_filename="t", source_hash="h")
        self.db.add(matrix)
        self.db.flush()
        params = []
        for code, unit, pd, rd in params_spec:
            row = Param(matrix_version_id=matrix.id, project_id=1, organization_id=1, code=code, matrix_code=code, scoring_code=code, section="x",
                        parameter_name="Наименование параметра", unit=unit, source_pd=True, source_rd=True, source_id=source_id, trigger_logic="",
                        review_priority="HIGH", data_type="number", is_active=True,
                        other_normative=json.dumps({"source_pd": pd, "source_rd": rd, "source_id": ""}))
            self.db.add(row)
            params.append(row)
        docs = []
        for i, (file_id, path, stage, section) in enumerate(docs_spec, start=1):
            manifest = {"file_id": file_id, "stage": stage, "relative_path": path, "section": section, "sha256": f"sha-{file_id}",
                        "pdf_pages": 10, "extension": ".pdf", "size_bytes": 1000}
            doc = DocumentVersion(project_id=1, organization_id=1, source_type="case10_dataset", dataset_file_id=file_id, object_id="OBJ-T",
                                  filename=path.rsplit("/", 1)[-1], doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage],
                                  document_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage], dataset_stage=stage,
                                  dataset_section=section, discipline=section, document_code=file_id, file_hash=f"sha-{file_id}",
                                  content_hash=f"sha-{file_id}", approval_status="UNKNOWN", dataset_metadata={"document_manifest": manifest})
            self.db.add(doc)
            docs.append(doc)
        self.db.flush()
        process = InspectionProcess(id="proc-1", project_id=1, organization_id=1, object_id="OBJ-T", status="PARSING", upload_scenario="FULL",
                                    completeness_status={}, matrix_version="official-132-v1", dataset_version="v", model_version="m")
        self.db.add(process)
        self.db.flush()
        create_official_evidence_groups(self.db, process, params, docs)
        from app.db.models import EvidenceGroup

        return {g.param.code: g for g in self.db.query(EvidenceGroup).all()}

    def test_absent_section_is_missing_document_and_present_section_is_not(self):
        groups = self._run(
            [("PZ-014", "кВт", "Раздел ПЗ: Текст (ТУ)", "Раздел ЭОМ: Лист Общие данные"),      # RD has no ЭОМ
             ("PZ-002", "м²", "Раздел ПЗ: Таблица ТЭП", "Раздел АР: Лист Общие данные"),        # RD has АР
             ("KR-058", "мм", "Опалубочный чертеж фундамента (КР)", "Опалубочные чертежи (КЖ)")],  # rule-pack code: fallback untouched
            [("P-1", "пд/01-01-00-02-ПЗ.pdf", "PD", "OTHER"), ("P-2", "пд/01-04-00-01-КР.pdf", "PD", "OTHER"),
             ("R-1", "рд/19-0322-АР2.pdf", "RD", "AR"), ("R-2", "рд/19-0322-КЖ1.pdf", "RD", "KR")],
        )
        missing = groups["PZ-014"]
        self.assertEqual((missing.finding_status, missing.delta["reason"], missing.delta["stages"]), ("MISSING_EVIDENCE", "missing_discipline_document", ["RD"]))
        self.assertEqual(groups["PZ-002"].delta["reason"], "no_relevant_evidence")
        self.assertEqual(groups["KR-058"].delta["reason"], "no_relevant_evidence")

    def test_rule_pack_code_keeps_its_fallback_even_when_the_section_is_absent(self):
        groups = self._run(
            [("KR-058", "мм", "Опалубочный чертеж фундамента (КР)", "Опалубочные чертежи плиты (КЖ)")],
            [("P-1", "пд/01-01-00-02-ПЗ.pdf", "PD", "OTHER"), ("R-1", "рд/19-0322-АР2.pdf", "RD", "AR")],   # no КР anywhere
        )
        self.assertEqual(groups["KR-058"].delta["reason"], "no_relevant_evidence")

    def test_a_missing_id_stage_does_not_hide_the_missing_rd_section(self):
        # ALT79B-like package: PD+RD only. RD lacks ЭОМ. Both absences are reported; RD (the severer) leads the status.
        groups = self._run(
            [("PZ-014", "кВт", "Раздел ПЗ: Текст (ТУ)", "Раздел ЭОМ: Лист Общие данные")],
            [("P-1", "пд/01-01-00-02-ПЗ.pdf", "PD", "OTHER"), ("R-1", "рд/19-0322-АР2.pdf", "RD", "AR")],
            source_id=True,
        )
        group = groups["PZ-014"]
        self.assertEqual((group.delta["reason"], group.delta["stages"]), ("missing_discipline_document", ["RD", "ID"]))
        check = evidence_group_to_submission_check({"finding_status": group.finding_status, "delta": group.delta, "fragments": [], "parameter": {"code": "PZ-014"}})
        self.assertEqual((check["violation_label"], check["protocol_status"]), ("MISSING_DOCUMENT", "RD_MISSING"))

    def test_missing_stage_still_wins_over_missing_section(self):
        groups = self._run(
            [("PZ-014", "кВт", "Раздел ПЗ: Текст (ТУ)", "Раздел ЭОМ: Лист Общие данные")],
            [("P-1", "пд/01-01-00-02-ПЗ.pdf", "PD", "OTHER")],          # no RD at all
        )
        self.assertEqual(groups["PZ-014"].delta["reason"], "missing_stage")
