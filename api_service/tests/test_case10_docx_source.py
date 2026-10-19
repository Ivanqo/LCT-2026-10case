"""DOCX uploads expose bounded document text to the same V3 regex extraction path."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import zipfile

from app.api.routes_upload import _ensure_docx_source_fragment
from app.db.models import DocumentVersion, SourceFragment
from app.domain.v3_extractors import RegexExtractor
from test_case10_s5_workbench import WorkbenchTestBase


class DocxSourceFragmentTests(WorkbenchTestBase):
    def test_docx_paragraph_text_is_available_to_generic_extractor(self):
        path = Path(self._tmp.name) / "drawing.docx"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Общая площадь 123 м2</w:t></w:r></w:p></w:body></w:document>'
        )
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", xml)

        doc = DocumentVersion(
            project_id=self.project.id,
            organization_id=self.org_id,
            source_type="rag",
            filename=path.name,
            file_path=str(path),
        )
        self.db.add(doc)
        self.db.flush()

        fragment = _ensure_docx_source_fragment(self.db, doc, path)
        self.db.commit()
        results = RegexExtractor().extract(
            param=SimpleNamespace(regex_pattern=r"площадь\s+(?P<value>\d+)"),
            document=doc,
            fragments=[fragment],
        )

        self.assertEqual(fragment.source_system, "docx")
        self.assertEqual(fragment.fragment_type, "text")
        self.assertIsNone(fragment.bbox)  # no page geometry is inferred from Word's text stream
        self.assertIn("Общая площадь 123 м2", fragment.text)
        self.assertEqual([item.value for item in results], ["123"])

        _ensure_docx_source_fragment(self.db, doc, path)
        self.assertEqual(
            self.db.query(SourceFragment)
            .filter(SourceFragment.document_version_id == doc.id, SourceFragment.source_system == "docx")
            .count(),
            1,
        )


class DocxFragmentsKeepOfficialChecksTests(WorkbenchTestBase):
    """The DOCX option is on by default because the official 132-parameter matrix carries no regex patterns: a DOCX text
    fragment is stored and shown, but none of the V3 extractors returns a value for it, so `checks` cannot change."""

    def test_official_matrix_extracts_nothing_from_docx_text(self):
        from app.domain.official_dataset import ensure_official_matrix
        from app.domain.v3_extractors import default_extractors
        from app.db.models import Param

        ensure_official_matrix(self.db, organization_id=self.org_id, project_id=self.project.id)
        params = self.db.query(Param).filter(Param.organization_id == self.org_id, Param.project_id == self.project.id).all()
        self.assertGreaterEqual(len(params), 132)
        self.assertEqual([p.code for p in params if str(p.regex_pattern or "").strip()], [])
        doc = DocumentVersion(project_id=self.project.id, organization_id=self.org_id, source_type="rag", filename="a.docx", file_path="a.docx")
        self.db.add(doc)
        self.db.flush()
        fragment = SourceFragment(document_version_id=int(doc.id), page=1, bbox=None, fragment_type="text", source_system="docx",
                                  external_id="docx:document", metadata_json={"source_type": "docx"}, extractor="docx_text_reader",
                                  confidence=0.8, text="Площадь застройки 123 м2. Ширина эвакуационных дверей 1200 мм. EI 60.")
        self.db.add(fragment)
        self.db.flush()
        found = [item for param in params for extractor in default_extractors()
                 for item in extractor.extract(param=param, document=doc, fragments=[fragment])]
        self.assertEqual(found, [])
