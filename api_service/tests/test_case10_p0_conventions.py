"""Phase 12 / P0: matrix 1.1 codes, GOLD 1.1 export fields, live mode (organizer annotations off), anchor vocabulary."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "api_service"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.domain import anchor_vocab, matrix_v11  # noqa: E402
from app.domain.official_dataset import (  # noqa: E402
    ORGANIZER_ANNOTATIONS_ENV,
    _generated_matrix_v11,
    _live_index_row,
    _validate_matrix_v11,
    organizer_annotations_disabled,
    stage_from_package_path,
)
from evaluation.exporter import evidence_group_to_submission_check, protocol_to_submission, validate_submission_schema  # noqa: E402

SCHEMA = REPO / "evaluation" / "phase12" / "submission_schema.json"
EXAMPLE = REPO / "evaluation" / "phase12" / "example_submission_gold11.json"
ORGANIZER_MATRIX = REPO / "case_data" / "official_1_1_20260925" / "Матрица_параметров_редакция1.1.xlsx"

GOLD11_CHECK_KEYS = (
    "object_id", "parameter_id", "parameter_code_legacy", "matrix_code", "rule_version",
    "evidence_group_id", "finding_id", "expected_value", "actual_value",
    "source_expected_file_id", "source_expected_sha256", "source_expected_stage", "source_expected_code",
    "source_expected_revision", "source_expected_approval", "source_expected_page", "source_expected_bbox_polygon",
    "source_actual_file_id", "source_actual_sha256", "source_actual_stage", "source_actual_code",
    "source_actual_revision", "source_actual_approval", "source_actual_page", "source_actual_bbox_polygon",
    "approved_change_ref", "completeness_status", "finding_status", "review_priority", "confidence",
    "dataset_version", "matrix_version", "model_version", "input_manifest_hash",
)
GOLD11_EVIDENCE_KEYS = ("file_id", "sha256", "stage", "document_code", "revision", "approval_status", "page", "bbox_norm")
CONTEXT = {"object_id": "OBJ-T", "dataset_version": "ds-1", "matrix_version": "official-132-v1.1", "model_version": "m-1", "input_manifest_hash": "abc"}


def _fragment(stage, file_id, page, value, role, bbox=(0.1, 0.2, 0.3, 0.4), **extra):
    return {"stage": stage, "file_id": file_id, "page": page, "extracted_value": value, "role": role,
            "file_sha256": f"sha-{file_id}", "document_code": f"CODE-{file_id}", "revision": "2", "approval_status": "APPROVED",
            "bbox_normalized": list(bbox) if bbox is not None else None, "coordinate_space": "SOURCE_PAGE", **extra}


def _group(code="IOS4-078", status="CANDIDATE", fragments=None, **extra):
    return {
        "id": 7, "object_id": "OBJ-T", "finding_status": status, "review_priority": "HIGH", "confidence": 0.8,
        "model_version": "official-rule-packs-v2", "matrix_version": "official-132-v1.1", "dataset_version": "ds-1",
        "parameter": {"code": code, "criticality": "Критическое (приостановка работ)"},
        "delta": {"parameter_code": code, "location": "140", "source": "official_rule_pack", "matrix_scope": "MATRIX"},
        "fragments": fragments if fragments is not None else [
            _fragment("project", "F1", 88, "V2.7", "expected"),
            _fragment("working", "F2", 18, "V2.8", "actual", bbox=(0.9, 0.1, 0.8, 0.3)),
        ],
        **extra,
    }


class MatrixV11TableTest(unittest.TestCase):
    def test_table_has_132_ordered_codes_and_the_106_26_priority_split(self):
        rows = matrix_v11.matrix_rows()
        self.assertEqual([row["matrix_code"] for row in rows], [f"M-{i:03d}" for i in range(1, 133)])
        self.assertEqual([row["parameter_id"] for row in rows], list(range(1, 133)))
        self.assertEqual(len({row["legacy_code"] for row in rows}), 132)
        self.assertEqual(sum(row["review_priority"] == "HIGH" for row in rows), 106)
        self.assertEqual(sum(row["review_priority"] == "MEDIUM" for row in rows), 26)

    def test_lookup_works_from_either_convention_and_passes_non_matrix_codes_through(self):
        self.assertEqual(matrix_v11.matrix_code_for("IOS4-078"), "M-078")
        self.assertEqual(matrix_v11.legacy_code_for("m-078"), "IOS4-078")
        self.assertEqual(matrix_v11.parameter_id_for("M-132"), 132)
        self.assertEqual(matrix_v11.canonical_code("PZ-001"), matrix_v11.canonical_code("M-001"))
        self.assertEqual(matrix_v11.canonical_code("FREE-HEATING-001"), "FREE-HEATING-001")
        self.assertIsNone(matrix_v11.lookup("FREE-HEATING-001"))

    def test_export_style_flag(self):
        with mock.patch.dict(os.environ, {matrix_v11.CODE_STYLE_ENV: ""}):
            self.assertEqual(matrix_v11.export_parameter_code("KR-055"), "M-055")
        with mock.patch.dict(os.environ, {matrix_v11.CODE_STYLE_ENV: "legacy"}):
            self.assertEqual(matrix_v11.export_parameter_code("M-055"), "KR-055")
        with mock.patch.dict(os.environ, {matrix_v11.CODE_STYLE_ENV: "m-codes"}):
            with self.assertRaises(ValueError):
                matrix_v11.export_code_style()
        self.assertEqual(matrix_v11.export_parameter_code("FREE-HEATING-001", "matrix11"), "FREE-HEATING-001")

    def test_param_import_fallback_takes_codes_and_priorities_from_the_table(self):
        catalog = [{"parameter_id": row["parameter_id"], "parameter_code": row["legacy_code"], "parameter_name": row["parameter_name"]}
                   for row in matrix_v11.matrix_rows()]            # no criticality at all: the heuristic cannot help
        generated = _generated_matrix_v11(catalog)
        _validate_matrix_v11(generated)                          # 132 codes, 106/26
        self.assertEqual(generated[78]["matrix_code"], "M-078")

    @unittest.skipUnless(ORGANIZER_MATRIX.is_file(), "organizer matrix 1.1 not on disk")
    def test_committed_table_matches_the_organizer_matrix_and_catalog(self):
        from evaluation.phase12.build_matrix_v11_table import build_table, render

        self.assertEqual(render(build_table()), matrix_v11.TABLE_PATH.read_text(encoding="utf-8"))


class Gold11ExportTest(unittest.TestCase):
    def test_check_carries_matrix11_code_legacy_code_and_every_gold11_field(self):
        with mock.patch.dict(os.environ, {matrix_v11.CODE_STYLE_ENV: "matrix11"}):
            check = evidence_group_to_submission_check(_group(), context=CONTEXT)
        self.assertEqual((check["parameter_code"], check["parameter_id"], check["parameter_code_legacy"], check["matrix_code"]),
                         ("M-078", 78, "IOS4-078", "M-078"))
        for key in GOLD11_CHECK_KEYS:
            self.assertIn(key, check)
        for evidence in check["evidence"]:
            for key in GOLD11_EVIDENCE_KEYS:
                self.assertIn(key, evidence)
        self.assertEqual((check["expected_value"], check["actual_value"]), ("V2.7", "V2.8"))
        self.assertEqual((check["source_expected_file_id"], check["source_expected_page"], check["source_expected_stage"]), ("F1", 88, "PD"))
        self.assertEqual((check["source_actual_file_id"], check["source_actual_page"], check["source_actual_sha256"]), ("F2", 18, "sha-F2"))
        self.assertEqual(check["source_actual_bbox_polygon"], [0.8, 0.1, 0.9, 0.3])      # corners re-ordered
        self.assertEqual((check["approved_change_ref"], check["completeness_status"], check["finding_status"]), ("NONE", "COMPLETE", "CANDIDATE"))
        self.assertEqual((check["review_priority"], check["confidence"], check["input_manifest_hash"]), ("HIGH", 0.8, "abc"))
        self.assertEqual(check["rule_version"], "official_rule_pack/official-rule-packs-v2")

    def test_legacy_style_keeps_the_internal_code(self):
        check = evidence_group_to_submission_check(_group(), context=CONTEXT, code_style="legacy")
        self.assertEqual((check["parameter_code"], check["parameter_code_legacy"], check["matrix_code"]), ("IOS4-078", "IOS4-078", "M-078"))

    def test_free_search_code_passes_through(self):
        check = evidence_group_to_submission_check(_group(code="FREE-HEATING-001"), context=CONTEXT, code_style="matrix11")
        self.assertEqual((check["parameter_code"], check["parameter_id"], check["matrix_code"], check["parameter_code_legacy"]),
                         ("FREE-HEATING-001", None, None, "FREE-HEATING-001"))

    def test_abstentions_map_to_completeness_status_and_null_finding_status(self):
        expected = {"MISSING_EVIDENCE": "MISSING_EVIDENCE", "NOT_APPLICABLE": "NOT_APPLICABLE", "NOT_COMPARABLE": "NOT_COMPARABLE",
                    "LOW_QUALITY": "NOT_COMPARABLE", "CLARIFICATION_REQUIRED": "CLARIFICATION_REQUIRED"}
        for status, completeness in expected.items():
            check = evidence_group_to_submission_check(_group(status=status, fragments=[]), context=CONTEXT)
            self.assertEqual((check["completeness_status"], check["finding_status"], check["finding_status_internal"]), (completeness, None, status))
        for status in ("NEGATIVE_VERIFIED", "CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION"):
            check = evidence_group_to_submission_check(_group(status=status), context=CONTEXT)
            self.assertEqual((check["completeness_status"], check["finding_status"]), ("COMPLETE", status))

    def test_bbox_is_normalized_clamped_or_dropped(self):
        pixel = _fragment("project", "F1", 1, "x", "expected", bbox=(100, 50, 300, 150), page_width=1000, page_height=500)
        no_size = _fragment("working", "F2", 1, "y", "actual", bbox=(100, 50, 300, 150))
        outside = _fragment("as_built", "F3", 1, "z", "context", bbox=(-0.1, 0.2, 1.0, 0.4))   # >1 would read as points
        check = evidence_group_to_submission_check(_group(fragments=[pixel, no_size, outside]), context=CONTEXT)
        boxes = {item["file_id"]: item["bbox_norm"] for item in check["evidence"]}
        self.assertEqual(boxes["F1"], [0.1, 0.1, 0.3, 0.3])
        self.assertIsNone(boxes["F2"])
        self.assertEqual(boxes["F3"], [0.0, 0.2, 1.0, 0.4])

    def test_ids_are_content_hashes_stable_across_styles_and_runs(self):
        a = evidence_group_to_submission_check(_group(), context=CONTEXT, code_style="matrix11")
        b = evidence_group_to_submission_check(_group(), context=CONTEXT, code_style="legacy")
        self.assertEqual((a["finding_id"], a["evidence_group_id"]), (b["finding_id"], b["evidence_group_id"]))
        moved = _group(fragments=[_fragment("project", "F1", 89, "V2.7", "expected"), _fragment("working", "F2", 18, "V2.8", "actual")])
        c = evidence_group_to_submission_check(moved, context=CONTEXT)
        self.assertEqual(a["finding_id"], c["finding_id"])                 # same object + code + location
        self.assertNotEqual(a["evidence_group_id"], c["evidence_group_id"])  # other cited page
        protocol = {"payload": {**CONTEXT, "findings": [_group()]}}
        self.assertEqual(json.dumps(protocol_to_submission(protocol), ensure_ascii=False, sort_keys=True),
                         json.dumps(protocol_to_submission(protocol), ensure_ascii=False, sort_keys=True))

    def test_submission_stays_valid_against_the_organizer_schema(self):
        protocol = {"payload": {**CONTEXT, "findings": [_group(), _group(status="MISSING_EVIDENCE", fragments=[]), _group(code="FREE-HEATING-001")]}}
        submission = protocol_to_submission(protocol, include_suspicions=True)
        self.assertEqual(validate_submission_schema(submission, SCHEMA), [])
        self.assertEqual(submission["export_conventions"]["input_manifest_hash"], "abc")
        broken = json.loads(json.dumps(submission))
        broken["checks"][0]["evidence"][0]["stage"] = "RD_ID_MIXED"
        self.assertTrue(validate_submission_schema(broken, SCHEMA))

    @unittest.skipUnless(EXAMPLE.is_file(), "example not generated yet")
    def test_published_example_is_schema_valid_and_carries_gold11_fields(self):
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(validate_submission_schema(example, SCHEMA), [])
        self.assertTrue(example["checks"])
        for check in example["checks"]:
            for key in GOLD11_CHECK_KEYS:
                self.assertIn(key, check)
            self.assertTrue(check["parameter_code"].startswith("M-") or check["matrix_code"] is None)


class LiveModeTest(unittest.TestCase):
    def test_flag(self):
        for value, expected in (("1", True), ("true", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {ORGANIZER_ANNOTATIONS_ENV: value}):
                self.assertIs(organizer_annotations_disabled(), expected)

    def test_stage_comes_from_the_outermost_stage_folder(self):
        cases = {
            "Объект/Объект/Стадия П/Том 1.pdf": "PD",
            "Объект/Стадия РД/АР.pdf": "RD",
            "Объект/Исполнительная документация/АОСР №1.pdf": "ID",
            "ПД/5 Сведения/ИОС4.pdf": "PD",
            "РД/3. КР/КЖ.pdf": "RD",
            "ИД/папка 1.pdf": "ID",
            "Проектная документация/РД-ОВ1.pdf": "PD",            # the file name is not a stage folder
            "Рабочая и исполнительная документация/План.pdf": "RD_ID_MIXED",
            "ПД/Рабочая документация/x.pdf": "PD",                 # outermost stage folder wins
            "Перечень нарушений.txt": "UNKNOWN",
            "Разное/x.pdf": "UNKNOWN",
        }
        for path, stage in cases.items():
            self.assertEqual(stage_from_package_path(path), stage, path)
        self.assertEqual(stage_from_package_path("РД\\2. АР\\АР2.pdf"), "RD")

    def test_live_row_drops_organizer_labels_and_keeps_file_properties(self):
        row = {"file_id": "F1", "object_id": "O", "relative_path": "O/Стадия РД/x.pdf", "sha256": "h", "pdf_pages": 3,
               "stage": "RD_ID_MIXED", "section": "OV", "matrix_codes": ["IOS4-078"], "distribution_status": "EXCLUDE"}
        live = _live_index_row(row)
        self.assertEqual({k: live[k] for k in ("file_id", "relative_path", "sha256", "pdf_pages", "stage", "stage_source")},
                         {"file_id": "F1", "relative_path": "O/Стадия РД/x.pdf", "sha256": "h", "pdf_pages": 3,
                          "stage": "RD", "stage_source": "PACKAGE_PATH"})
        for key in ("section", "matrix_codes", "distribution_status"):
            self.assertNotIn(key, live)
        self.assertNotIn("stage", _live_index_row({"file_id": "F1", "stage": "PD", "matrix_codes": []}, with_stage=False))

    def test_import_in_live_mode_has_no_organizer_annotations_sections_or_codes(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.models import Base, DocumentVersion, SourceFragment
        from app.domain.official_dataset import find_dataset_paths, import_official_dataset

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_toy_dataset(root)
            results = {}
            for flag in ("0", "1"):
                find_dataset_paths.cache_clear()
                engine = create_engine("sqlite+pysqlite:///:memory:")
                Base.metadata.create_all(engine)
                db = sessionmaker(bind=engine)()
                with mock.patch.dict(os.environ, {ORGANIZER_ANNOTATIONS_ENV: flag}):
                    summary = import_official_dataset(db, project_id=1, organization_id=1, object_ids=["OBJ-TOY"], include_gold=False, dataset_root=root)
                docs = {doc.dataset_file_id: doc for doc in db.query(DocumentVersion).all()}
                fragments = db.query(SourceFragment).all()
                results[flag] = (summary, docs, fragments)
                db.close()
            find_dataset_paths.cache_clear()
        summary, docs, fragments = results["0"]
        self.assertEqual(summary["organizer_annotations"], "ENABLED")
        self.assertEqual(docs["T2"].dataset_section, "OV")
        self.assertEqual(sum(f.source_system == "learning_annotation" for f in fragments), 1)
        summary, docs, fragments = results["1"]
        self.assertEqual(summary["organizer_annotations"], "DISABLED_LIVE_MODE")
        self.assertEqual({fid: (doc.dataset_stage, doc.dataset_section) for fid, doc in docs.items()},
                         {"T1": ("PD", None), "T2": ("RD_ID_MIXED", None)})
        self.assertFalse([f for f in fragments if f.source_system == "learning_annotation"])
        for doc in docs.values():
            for row in (doc.dataset_metadata or {}).values():
                if isinstance(row, dict):
                    self.assertNotIn("matrix_codes", row)
                    self.assertNotIn("section", row)
        for fragment in fragments:
            self.assertNotIn("matrix_codes", fragment.metadata_json or {})


def _write_toy_dataset(root: Path) -> None:
    participant = root / "case_data" / "extracted" / "01_participant_package" / "pkg" / "02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ" / "data"
    split = root / "learning_data" / "extracted" / "train_public_203" / "data"
    participant.mkdir(parents=True)
    split.mkdir(parents=True)
    catalog = [{"parameter_id": row["parameter_id"], "parameter_code": row["legacy_code"], "pd_section": row["pd_section"],
                "parameter_name": row["parameter_name"], "unit": row["unit"], "source_pd": "ПД", "source_rd": "РД", "source_id": "",
                "trigger": "t", "criticality": "Критическое (приостановка работ)" if row["review_priority"] == "HIGH" else "Существенное (предписание)"}
               for row in matrix_v11.matrix_rows()]
    files = [
        {"file_id": "T1", "object_id": "OBJ-TOY", "split": "TRAIN_PUBLIC", "relative_path": "Toy/ПД/5 ОВ/ОВ.pdf", "sha256": "a" * 64,
         "stage": "PD", "section": "OV", "pdf_pages": 2, "matrix_codes": ["IOS4-078"]},
        {"file_id": "T2", "object_id": "OBJ-TOY", "split": "TRAIN_PUBLIC", "relative_path": "Toy/Рабочая и исполнительная документация/РД-ОВ1.pdf",
         "sha256": "b" * 64, "stage": "RD_ID_MIXED", "section": "OV", "pdf_pages": 2, "matrix_codes": ["IOS4-078"]},
    ]
    files_index = [{**{k: v for k, v in row.items() if k not in ("relative_path", "sha256")}, "source_relative_path": row["relative_path"],
                    "source_sha256": row["sha256"]} for row in files]
    pages = [{"file_id": "T2", "object_id": "OBJ-TOY", "split": "TRAIN_PUBLIC", "stage": "RD_ID_MIXED", "section": "OV", "source_page_number": 1,
              "page_width": 100.0, "page_height": 100.0, "matrix_codes": ["IOS4-078"], "annotation_count": 1}]
    annotations = [{"annotation_id": "A1", "file_id": "T2", "object_id": "OBJ-TOY", "split": "TRAIN_PUBLIC", "page_number": 1,
                    "annotation_type": "MATRIX_FIELD", "code": "IOS4-078", "text": "V2.8", "bbox_normalized": [0.1, 0.1, 0.2, 0.2]}]
    for path, rows in ((participant / "parameter_catalog_132.jsonl", catalog), (participant / "document_manifest.jsonl", files),
                       (split / "files_index.jsonl", files_index), (split / "page_index.jsonl", pages), (split / "annotations.jsonl", annotations)):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class AnchorVocabTest(unittest.TestCase):
    def setUp(self):
        anchor_vocab.clear_cache()
        self.addCleanup(anchor_vocab.clear_cache)

    def test_every_matrix_code_has_a_valid_vocabulary_file(self):
        for row in matrix_v11.matrix_rows():
            path = anchor_vocab.vocab_path(row["legacy_code"])
            self.assertTrue(path.is_file(), path.name)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(anchor_vocab.validate_entry(raw, expected_code=row["legacy_code"]), [], path.name)
            self.assertEqual(raw["matrix_code"], row["matrix_code"])

    def test_empty_vocabulary_leaves_only_the_catalog_name(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(anchor_vocab, "VOCAB_DIR", Path(tmp)):
            (Path(tmp) / "PZ-001.json").write_text(json.dumps({"code": "PZ-001", "phrases": [], "units": []}), encoding="utf-8")
            self.assertEqual(anchor_vocab.anchor_phrases("PZ-001", " Площадь  застройки "), ("Площадь  застройки",))
            self.assertEqual(anchor_vocab.anchor_phrases("NO-SUCH-CODE", "Имя"), ("Имя",))

    def test_phrases_are_added_after_the_name_filtered_by_stage_and_deduplicated(self):
        entry = {"code": "SPZU-027", "phrases": [
            {"text": "Площадь озеленения", "provenance": [{"object": "LOS3A", "file_id": "F", "page": 1}]},
            {"text": "площадь  ОЗЕЛЕНЕНИЯ и газонов", "stages": ["PD"], "provenance": [{"object": "LOS3A", "file_id": "F", "page": 2}]},
            {"text": "Площадь озеленения и газонов", "provenance": [{"object": "LOS3A", "file_id": "F", "page": 3}]},
        ], "units": [{"text": "м2", "provenance": [{"object": "LOS3A", "file_id": "F", "page": 1}]}]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(anchor_vocab, "VOCAB_DIR", Path(tmp)):
            (Path(tmp) / "SPZU-027.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(anchor_vocab.anchor_phrases("SPZU-027", "Площадь озеленения", "PD"),
                             ("Площадь озеленения", "площадь ОЗЕЛЕНЕНИЯ и газонов"))
            self.assertEqual(anchor_vocab.anchor_phrases("SPZU-027", "Площадь озеленения", "RD"),
                             ("Площадь озеленения", "Площадь озеленения и газонов"))
            self.assertEqual(anchor_vocab.unit_spellings("SPZU-027"), ("м2",))

    def test_a_phrase_without_provenance_is_rejected(self):
        bad = {"code": "PZ-002", "phrases": [{"text": "Общая площадь"}]}
        self.assertTrue(anchor_vocab.validate_entry(bad, expected_code="PZ-002"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(anchor_vocab, "VOCAB_DIR", Path(tmp)):
            (Path(tmp) / "PZ-002.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                anchor_vocab.load_vocab("PZ-002")

    def test_tagger_anchor_list_is_unchanged_by_empty_vocabulary_and_extended_by_phrases(self):
        from app.domain import live_candidate_tagger as tagger

        params = [SimpleNamespace(code="PZ-002", parameter_name="Общая площадь здания", source_pd=True, source_rd=True, source_id=False, other_normative=None),
                  SimpleNamespace(code="PZ-007", parameter_name="Эт", source_pd=True, source_rd=False, source_id=False, other_normative=None)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(anchor_vocab, "VOCAB_DIR", Path(tmp)):
            plain = {stage: [(a.code, a.anchor_phrase) for a in anchors] for stage, anchors in tagger._build_param_anchors(params).items()}
            self.assertEqual(plain, {"PD": [("PZ-002", "Общая площадь здания")], "RD": [("PZ-002", "Общая площадь здания")], "ID": []})
            (Path(tmp) / "PZ-002.json").write_text(json.dumps({"code": "PZ-002", "phrases": [
                {"text": "Площадь здания", "stages": ["RD"], "provenance": [{"object": "LOS3A", "file_id": "F", "page": 11}]}]},
                ensure_ascii=False), encoding="utf-8")
            (Path(tmp) / "PZ-007.json").write_text(json.dumps({"code": "PZ-007", "phrases": [
                {"text": "Количество этажей", "provenance": [{"object": "LOS3A", "file_id": "F", "page": 11}]}]},
                ensure_ascii=False), encoding="utf-8")
            anchor_vocab.clear_cache()
            extended = {stage: [(a.code, a.anchor_phrase) for a in anchors] for stage, anchors in tagger._build_param_anchors(params).items()}
        self.assertEqual(extended["PD"], [("PZ-002", "Общая площадь здания"), ("PZ-007", "Количество этажей")])
        self.assertEqual(extended["RD"], [("PZ-002", "Общая площадь здания"), ("PZ-002", "Площадь здания")])


if __name__ == "__main__":
    unittest.main()
