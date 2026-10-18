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
