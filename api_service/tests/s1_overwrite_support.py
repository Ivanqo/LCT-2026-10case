"""Synthetic end-to-end harness for the S1 "value overwrite" investigation (test_case10_final_s1_overwrite.py).

Writes small real PDFs (Latin parameter names: PyMuPDF's built-in fonts carry no Cyrillic), imports them as
dataset documents and drives `create_official_evidence_groups` exactly like `run_process` does for the official
matrix -- live tagger included -- so the tests observe the real chain (tagger -> candidate pool -> extractor ->
upsert) instead of a mock of it. No object id, file id, page or GOLD value of the competition data is used."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, DocumentVersion, EvidenceDecision, EvidenceGroup, InspectionProcess, MatrixVersion, Param,
)
from app.domain.official_evidence import create_official_evidence_groups

STAGE_NAMES = {"PD": "project", "RD": "working", "ID": "as_built"}


def write_pdf(path: Path, pages: list[list[str]]) -> str:
    import fitz

    pdf = fitz.open()
    for lines in pages:
        page = pdf.new_page(width=595, height=842)
        y = 80
        for line in lines:
            page.insert_text((60, y), line, fontname="helv", fontsize=11)
            y += 18
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(path))
    pdf.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Harness:
    """One in-memory DB + one originals directory. `add_doc` order == DocumentVersion.id order, which is exactly
    the input-order lever the order-independence tests pull."""

    def __init__(self, params: list[tuple[str, str, str]] | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "originals"
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1", status="ACTIVE",
                               source_filename="t", source_hash="h")
        self.db.add(matrix)
        self.db.flush()
        self.params: list[Param] = []
        for code, name, unit in params or [("ZZ-001", "Total building area", "м²")]:
            row = Param(matrix_version_id=matrix.id, project_id=1, organization_id=1, code=code, matrix_code=code,
                        scoring_code=code, section="x", parameter_name=name, unit=unit, source_pd=True, source_rd=True,
                        source_id=False, trigger_logic="", review_priority="HIGH", data_type="number", is_active=True,
                        other_normative=json.dumps({"source_pd": "", "source_rd": "", "source_id": ""}))
            self.db.add(row)
            self.params.append(row)
        self.docs: list[DocumentVersion] = []
        self.process = InspectionProcess(id="proc-s1", project_id=1, organization_id=1, object_id="OBJ-S1", status="PARSING",
                                         upload_scenario="FULL", completeness_status={}, matrix_version="official-132-v1",
                                         dataset_version="v", model_version="m")
        self.db.add(self.process)
        self.db.flush()
        self._stack = ExitStack()
        self._stack.enter_context(patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root)}))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 1))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_ENABLED", False))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None))
        self._stack.enter_context(patch("app.domain.cross_stage_localization.semantic_text_similarity", return_value=None))

    def close(self) -> None:
        self._stack.close()
        self.db.close()
        self._tmp.cleanup()

    def add_doc(self, file_id: str, stage: str, pages: list[list[str]], *, discipline: str = "AR") -> DocumentVersion:
        relative = f"{stage}/{file_id}.pdf"
        sha = write_pdf(self.root / relative, pages)
        manifest = {"file_id": file_id, "stage": stage, "relative_path": relative, "section": discipline, "sha256": sha,
                    "pdf_pages": len(pages), "extension": ".pdf", "size_bytes": 1000}
        doc = DocumentVersion(project_id=1, organization_id=1, source_type="case10_dataset", dataset_file_id=file_id,
                              object_id="OBJ-S1", filename=f"{file_id}.pdf", doc_stage=STAGE_NAMES[stage],
                              document_stage=STAGE_NAMES[stage], dataset_stage=stage, dataset_section=discipline,
                              discipline=discipline, document_code=file_id, file_hash=sha, content_hash=sha,
                              approval_status="UNKNOWN", dataset_metadata={"document_manifest": manifest})
        self.db.add(doc)
        self.db.flush()
        self.docs.append(doc)
        return doc

    def run(self, codes: set[str] | None = None) -> None:
        params = [p for p in self.params if codes is None or p.code in codes]
        create_official_evidence_groups(self.db, self.process, params, list(self.docs))
        self.db.flush()

    def groups(self) -> dict[tuple[str, str], EvidenceGroup]:
        return {(g.param.code, g.group_key): g for g in self.db.query(EvidenceGroup).all()}

    def snapshot(self) -> list[tuple]:
        """Everything a consumer can observe about the persisted groups, id-free and order-stable."""
        files = {d.id: d.dataset_file_id for d in self.db.query(DocumentVersion).all()}
        out = []
        for g in self.db.query(EvidenceGroup).all():
            fragments = sorted(
                (files.get(f.document_version_id), f.page, f.role, f.extracted_value) for f in g.fragments
            )
            out.append((g.param.code, g.group_key, g.finding_status, g.expected_value, g.actual_value,
                        json.dumps(g.delta, sort_keys=True, default=str), g.needs_reverification, fragments))
        return sorted(out, key=lambda row: (row[0], row[1]))

    def decide(self, group: EvidenceGroup, decision: str = "Confirm") -> None:
        self.db.add(EvidenceDecision(evidence_group_id=group.id, decision=decision, reason_code=None, user_id=1))
        self.db.flush()
