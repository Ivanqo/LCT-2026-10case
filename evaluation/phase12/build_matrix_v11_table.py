"""Build (or check) the committed matrix 1.1 code table `api_service/app/domain/matrix_v11_codes.json`.

The table maps the official catalog ID -> matrix 1.1 code (M-001..M-132) -> our internal/legacy code
(PZ-001, IOS4-078, ...) -> review priority (HIGH/MEDIUM) -> section/name/unit. It is generated from the two
organizer files and cross-checked row by row: the content of the 132 parameters (section, name, unit, PD/RD/ID
sources, trigger) must be identical in `Матрица_параметров_редакция1.1.xlsx` and `parameter_catalog_132.jsonl`,
and HIGH must coincide with the catalog's "Критическое" criticality. The table is committed because `case_data/`
is not (and is absent in the delivery image); the runtime never reads the xlsx for code mapping.

Usage (repo root as CWD):
    python evaluation/phase12/build_matrix_v11_table.py            # rebuild the JSON
    python evaluation/phase12/build_matrix_v11_table.py --check    # verify the committed JSON, exit 1 on drift
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from app.domain.matrix_v11 import TABLE_PATH  # noqa: E402
from app.domain.official_dataset import _load_matrix_v11, _read_jsonl, _review_priority_v11  # noqa: E402

MATRIX_XLSX = REPO_ROOT / "case_data" / "official_1_1_20260925" / "Матрица_параметров_редакция1.1.xlsx"
CATALOG = (REPO_ROOT / "case_data" / "extracted" / "01_participant_package" / "ХАКАТОН_УЧАСТНИКАМ_ГОТОВО_К_ПЕРЕДАЧЕ"
           / "02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ" / "data" / "parameter_catalog_132.jsonl")
CONTENT_FIELDS = ("pd_section", "parameter_name", "unit", "source_pd", "source_rd", "source_id", "trigger")


def _norm(value: object) -> str:
    return " ".join(str(value or "").split())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_table(matrix_path: Path = MATRIX_XLSX, catalog_path: Path = CATALOG) -> dict:
    matrix = _load_matrix_v11(matrix_path)
    catalog = {int(row["parameter_id"]): row for row in _read_jsonl(catalog_path)}
    if sorted(matrix) != sorted(catalog) or len(matrix) != 132:
        raise ValueError(f"ID sets differ: matrix {len(matrix)} vs catalog {len(catalog)}")
    rows = []
    for parameter_id in sorted(matrix):
        m, c = matrix[parameter_id], catalog[parameter_id]
        drift = [field for field in CONTENT_FIELDS if _norm(m.get(field)) != _norm(c.get(field))]
        if drift:
            raise ValueError(f"ID {parameter_id}: matrix 1.1 and catalog differ in {drift}")
        priority = _review_priority_v11(m.get("review_priority"))
        if (priority == "HIGH") != ("критическ" in str(c.get("criticality") or "").lower()):
            raise ValueError(f"ID {parameter_id}: priority {priority} does not match criticality {c.get('criticality')!r}")
        rows.append({
            "parameter_id": parameter_id,
            "matrix_code": str(m["matrix_code"]).strip(),
            "legacy_code": str(c["parameter_code"]).strip(),
            "review_priority": priority,
            "pd_section": _norm(m.get("pd_section")),
            "parameter_name": _norm(m.get("parameter_name")),
            "unit": _norm(m.get("unit")) or None,
        })
    return {
        "matrix_version": "1.1",
        "generated_by": "evaluation/phase12/build_matrix_v11_table.py",
        "sources": {
            "matrix_xlsx": {"name": matrix_path.name, "sha256": _sha256(matrix_path)},
            "catalog_jsonl": {"name": catalog_path.name, "sha256": _sha256(catalog_path)},
        },
        "rows": rows,
    }


def render(table: dict) -> str:
    return json.dumps(table, ensure_ascii=False, indent=1) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify the committed table instead of writing it")
    args = parser.parse_args()
    text = render(build_table())
    if args.check:
        current = TABLE_PATH.read_text(encoding="utf-8") if TABLE_PATH.exists() else ""
        if current != text:
            print(f"DRIFT: {TABLE_PATH} differs from the organizer files; rebuild it", file=sys.stderr)
            return 1
        print("OK: matrix 1.1 table matches the organizer files")
        return 0
    TABLE_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TABLE_PATH} ({len(json.loads(text)['rows'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
