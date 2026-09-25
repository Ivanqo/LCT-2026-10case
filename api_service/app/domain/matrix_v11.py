"""Matrix 1.1 code table: catalog ID <-> M-001..M-132 <-> internal (legacy) code, plus review priority.

Internal codes (PZ-001, IOS4-078, ...) stay the keys of the whole pipeline; matrix 1.1 codes are an export
convention. The table is committed (`matrix_v11_codes.json`, rebuilt and drift-checked against the organizer's
xlsx and `parameter_catalog_132.jsonl` by `evaluation/phase12/build_matrix_v11_table.py`) so the mapping works
without `case_data/`.

Export style: `CASE10_PARAMETER_CODE_STYLE=matrix11` (default) puts the M-code into `parameter_code`;
`legacy` keeps the internal code. Codes outside the matrix (FREE-*, demo codes) pass through unchanged.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any

TABLE_PATH = Path(__file__).with_name("matrix_v11_codes.json")
CODE_STYLE_ENV = "CASE10_PARAMETER_CODE_STYLE"
CODE_STYLE_MATRIX11 = "matrix11"
CODE_STYLE_LEGACY = "legacy"
CODE_STYLES = (CODE_STYLE_MATRIX11, CODE_STYLE_LEGACY)


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    payload = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    rows = tuple(payload["rows"])
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_code[row["matrix_code"].upper()] = row
        by_code[row["legacy_code"].upper()] = row
    return {"meta": {k: v for k, v in payload.items() if k != "rows"}, "rows": rows, "by_code": by_code}


def matrix_rows() -> tuple[dict[str, Any], ...]:
    return _table()["rows"]


def table_metadata() -> dict[str, Any]:
    return dict(_table()["meta"])


def lookup(code: object) -> dict[str, Any] | None:
    """The table row for a legacy code, an M-code, or None for codes outside the matrix."""
    key = str(code or "").strip().upper()
    return _table()["by_code"].get(key) if key else None


def matrix_code_for(code: object) -> str | None:
    row = lookup(code)
    return row["matrix_code"] if row else None


def legacy_code_for(code: object) -> str | None:
    row = lookup(code)
    return row["legacy_code"] if row else None


def parameter_id_for(code: object) -> int | None:
    row = lookup(code)
    return int(row["parameter_id"]) if row else None


def review_priority_for(code: object) -> str | None:
    row = lookup(code)
    return row["review_priority"] if row else None


def canonical_code(code: object) -> str:
    """One comparable key for a code in either convention: the M-code when the code is in the matrix, else the
    code itself (upper-cased, stripped). Used by graders to match legacy-coded gold with M-coded predictions."""
    row = lookup(code)
    return row["matrix_code"] if row else str(code or "").strip().upper()


def export_code_style() -> str:
    value = os.getenv(CODE_STYLE_ENV, CODE_STYLE_MATRIX11).strip().lower() or CODE_STYLE_MATRIX11
    if value not in CODE_STYLES:
        raise ValueError(f"{CODE_STYLE_ENV}={value!r}; expected one of {', '.join(CODE_STYLES)}")
    return value


def export_parameter_code(code: object, style: str | None = None) -> str:
    """`parameter_code` for the submission in the requested style; codes outside the matrix are unchanged."""
    raw = str(code or "").strip()
    style = style or export_code_style()
    row = lookup(raw)
    if row is None:
        return raw
    return row["matrix_code"] if style == CODE_STYLE_MATRIX11 else row["legacy_code"]
