"""Protocol side of the decision layer (Phase 10, prompt B, items 2(б) and 4): excluded files and competing editions
are written into the completeness section of the protocol (Annex 2 section 1) and survive the PDF render.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.domain.comparison_gate import GateContext
from app.domain.protocol_annex2 import annex2_payload, render_protocol_pdf


def _doc(doc_id, file_id, path, stage, section="OTHER", sha=None, pages=10):
    row = {"file_id": file_id, "stage": stage, "relative_path": path, "section": section, "sha256": sha or f"sha-{file_id}",
           "pdf_pages": pages, "extension": ".pdf", "size_bytes": 1000}
    return SimpleNamespace(
        id=doc_id, dataset_file_id=file_id, dataset_stage=stage, dataset_section=section, discipline=section, filename=path.rsplit("/", 1)[-1],
        doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage], file_hash=row["sha256"], content_hash=None, document_code=file_id,
        revision=None, approval_status="UNKNOWN", dataset_metadata={"document_manifest": row},
    )


class ProtocolDocumentAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.docs = [
            _doc(1, "P-1", "пд/133-0820-ОК-1-ПЗ2_(Корр.1) РнС.pdf", "PD"),
            _doc(2, "P-2", "пд/133-0820-ОК-1-ПЗ2_МГЭ_РнИ+.pdf", "PD"),
            _doc(3, "P-3", "пд/133-0820-ОК-1-ПЗ2_МГЭ_РнИ+ копия.pdf", "PD", sha="sha-P-2"),      # byte duplicate of P-2
            _doc(4, "R-1", "рд/133-0820-ОК-1-АР1.pdf", "RD", "AR"),
            _doc(5, "R-2", "рд/8. Раздел 8 ООС.pdf", "RD", pages=0),                              # unreadable
        ]
        self.analysis = GateContext(self.docs).protocol_section()

    def test_section_1_lists_excluded_files_and_conflicts(self):
        annex = annex2_payload([], self.docs, object_info={}, matrix_size=132, document_analysis=self.analysis)
        section = annex["section_1_document_upload"]
        excluded = {item["file_id"]: item["reason"] for item in section["integrity"]["excluded"]}
        self.assertEqual(excluded, {"P-3": "EXACT_DUPLICATE_WITHIN_STAGE", "R-2": "UNREADABLE_OR_EMPTY_SOURCE_FILE"})
        self.assertEqual([c["type"] for c in section["revision_conflicts"]], ["UNORDERED_REVISIONS"])
        rows = {row["stage"]: row for row in section["rows"]}
        self.assertEqual(rows["PD"]["excluded_files"], 1)
        self.assertEqual(rows["RD"]["excluded_files"], 1)
        self.assertEqual(rows["ID"]["excluded_files"], 0)

    def test_annex_without_analysis_keeps_the_old_shape(self):
        annex = annex2_payload([], self.docs, object_info={}, matrix_size=132)
        section = annex["section_1_document_upload"]
        self.assertNotIn("integrity", section)
        self.assertNotIn("excluded_files", section["rows"][0])

    def test_pdf_render_shows_both_blocks_and_does_not_fail(self):
        annex = annex2_payload([], self.docs, object_info={}, matrix_size=132, document_analysis=self.analysis)
        protocol = SimpleNamespace(id=1, version=1, status="DRAFT", object_id="OBJ-T", matrix_version="m", created_at="now", payload_json={"annex_2": annex})
        data = render_protocol_pdf(protocol)
        self.assertTrue(data.startswith(b"%PDF-"))
        import fitz

        with fitz.open(stream=data, filetype="pdf") as pdf:
            text = "\n".join(page.get_text() for page in pdf)
        self.assertIn("Исключённые из сравнения файлы", text)
        self.assertIn("Конфликты редакций", text)
        self.assertIn("P-3", text)



class SubmissionCitesCanonicalFilesTests(unittest.TestCase):
    """Integrity (10 points): a byte duplicate is one document; unreadable/service files are not documents."""

    def _protocol(self, findings, excluded):
        return {"payload": {"object_id": "OBJ-T", "findings": findings, "document_analysis": {"integrity": {"excluded": excluded}}}}

    @staticmethod
    def _group(*fragments):
        return {"finding_status": "CANDIDATE", "review_priority": "HIGH", "delta": {"parameter_code": "PZ-004", "location": "SITE", "matrix_scope": "MATRIX"},
                "parameter": {"code": "PZ-004"}, "fragments": [{"stage": "PD", "file_id": f, "page": p, "extracted_value": "1"} for f, p in fragments]}

    def test_duplicate_is_cited_once_under_its_canonical_id(self):
        from evaluation.exporter import protocol_to_submission

        excluded = [{"file_id": "F0101", "reason": "EXACT_DUPLICATE_WITHIN_STAGE", "duplicate_of": "F0100"}]
        submission = protocol_to_submission(self._protocol([self._group(("F0101", 5), ("F0100", 5), ("F0102", 7))], excluded))
        self.assertEqual([(e["file_id"], e["pdf_page_number"]) for e in submission["checks"][0]["evidence"]], [("F0100", 5), ("F0102", 7)])

    def test_files_that_are_not_documents_are_never_cited(self):
        from evaluation.exporter import protocol_to_submission

        excluded = [{"file_id": "F0418", "reason": "UNREADABLE_OR_EMPTY_SOURCE_FILE", "duplicate_of": None}]
        submission = protocol_to_submission(self._protocol([self._group(("F0418", 1), ("F0100", 5))], excluded))
        self.assertEqual([e["file_id"] for e in submission["checks"][0]["evidence"]], ["F0100"])

    def test_a_package_without_exclusions_exports_unchanged(self):
        from evaluation.exporter import protocol_to_submission

        group = self._group(("F0100", 5), ("F0102", 7))
        with_analysis = protocol_to_submission(self._protocol([group], []))
        without = protocol_to_submission({"payload": {"object_id": "OBJ-T", "findings": [group]}})
        self.assertEqual(with_analysis, without)


if __name__ == "__main__":
    unittest.main()
