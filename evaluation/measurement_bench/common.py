"""Shared constants/helpers for the measurement bench.

Nothing here imports the API service (so labelling/reading tools stay
independent of the mechanism under test); modules that need the pipeline
import it themselves after calling `pipeline_env()`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parent
CORPUS_DIR = BENCH_DIR / "corpus"
REPORT_DIR = REPO_ROOT / "evaluation" / "reports"
SILVER_PASS1 = REPO_ROOT / "evaluation" / "silver_new_objects_pass1"
MANIFESTS = SILVER_PASS1 / "manifests"

# Everything bulky or regenerable lives OUTSIDE the project data folders
# (task requirement: seed data / mutated PDFs / pipeline DBs are reproduced
# from a seed, never stored in the repo).
WORK_ROOT = Path(os.environ.get("CASE10_BENCH_WORK", r"E:\case10_measurement"))
TEXT_CACHE = Path(os.environ.get("SILVER_TEXT_CACHE", str(WORK_ROOT / "textcache")))
NEW_OBJECTS_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
PUBLIC_DOCS_ROOT = WORK_ROOT / "public_docs"          # PDFs of the 2 public objects extracted from the participant zip
DB_DIR = WORK_ROOT / "db"
OUT_DIR = WORK_ROOT / "out"
SEED_DIR = WORK_ROOT / "seeds"
MUTATED_PDF_DIR = WORK_ROOT / "mutated_pdfs"

PARTICIPANT_ZIP = REPO_ROOT / "case_data" / "01_ПАКЕТ_УЧАСТНИКАМ_3_ОБЪЕКТА.zip"
PARTICIPANT_DATA = next(
    (REPO_ROOT / "case_data" / "extracted" / "01_participant_package").glob("*/02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ/data"), None
)
PUBLIC_GOLD = REPO_ROOT / "learning_data" / "extracted" / "train_public_203" / "data" / "public_gold_checks.jsonl"
ANNOTATIONS_JSONL = (
    REPO_ROOT / "case_data" / "extracted" / "02_gold_methodology" / "domain_violation_reannotation_20260817" / "annotations.jsonl"
)
MATRIX_COVERAGE_JSON = REPO_ROOT / "evaluation" / "reports" / "matrix_coverage.json"

# --- object registry ---------------------------------------------------------
# split: "dev" = objects that participated in extractor development (public
# objects: every checkpoint 28-42; LOS3A: live-tagger bugs + forensic fixes
# 9.1); "holdout" = objects never used to design/tune an extractor.
# cleanliness (holdout only): CLEAN = never opened for anything except the
# organiser's drawing-level finding list; SEEN_IN_SILVER_REVIEW = its
# 19-group/TEP checks were read in the SILVER pass 1 (labels, not tuning).
OBJECTS: dict[str, dict[str, Any]] = {
    "LOS3A": {"object_id": "OBJ-NEW-LOS3A", "dirname": "Лосевская, 3А", "split": "dev", "cleanliness": "TUNED_ON", "kind": "new"},
    "ALT79B": {"object_id": "OBJ-NEW-ALT79B", "dirname": "Алтуфьевское, 79Б", "split": "holdout", "cleanliness": "SEEN_IN_SILVER_REVIEW", "kind": "new"},
    "POL17": {"object_id": "OBJ-NEW-POL17", "dirname": "Полярная, 17", "split": "holdout", "cleanliness": "SEEN_IN_SILVER_REVIEW", "kind": "new"},
    "DOO25": {"object_id": "OBJ-NEW-DOO25", "dirname": "Полярная ул. 25_ ДОО220к.9", "split": "holdout", "cleanliness": "SEEN_IN_SILVER_REVIEW", "kind": "new"},
    "OKT103": {"object_id": "OBJ-NEW-OKT103", "dirname": "Октябрьская 103", "split": "holdout", "cleanliness": "CLEAN", "kind": "new"},
    "IZM12": {"object_id": "OBJ-NEW-IZM12", "dirname": "Изумрудная, 12", "split": "holdout", "cleanliness": "CLEAN", "kind": "new"},
    "TYU": {"object_id": "OBJ-TYUMENSKAYA-5-GOLD-SEED", "dirname": None, "split": "dev", "cleanliness": "TUNED_ON", "kind": "public"},
    "NOV": {"object_id": "OBJ-NOVOSLOBODSKAYA", "dirname": None, "split": "dev", "cleanliness": "TUNED_ON", "kind": "public"},
}
# Речников (OBJ-RECHNIKOV-7-7) is the hidden TEST object: its documents ship in
# the participant zip, but it is deliberately NOT in the corpus (labelling it
# would mean reading the test documents to tune against them).
EXCLUDED_OBJECTS = {"OBJ-RECHNIKOV-7-7": "hidden test object; blind measurement runs only (prompt C/E2)"}

FAMILIES = ("ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT")
STAGES = ("PD", "RD", "ID")

CRITICAL_TEXT = "Критическое"


def utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# --- catalog -----------------------------------------------------------------
_CATALOG_CACHE: list[dict[str, Any]] | None = None


def catalog_rows() -> list[dict[str, Any]]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        assert PARTICIPANT_DATA is not None, "participant package data dir not found"
        _CATALOG_CACHE = read_jsonl(PARTICIPANT_DATA / "parameter_catalog_132.jsonl")
    return _CATALOG_CACHE


def catalog_by_code() -> dict[str, dict[str, Any]]:
    return {r["parameter_code"]: r for r in catalog_rows()}


def is_critical(row: dict[str, Any]) -> bool:
    return CRITICAL_TEXT in str(row.get("criticality") or "")


def family_of_code(code: str) -> str | None:
    """Family under which the generic tier would handle `code` -- the same
    classifiers the runtime uses (matrix_unit_classifier is dependency-free)."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "api_service"))
    from app.domain import matrix_unit_classifier as muc  # noqa: WPS433

    row = catalog_by_code()[code]
    unit = row.get("unit")
    excluded = frozenset({"PZ-009", "KR-055", "KR-058", "IOS4-078", "IOS4-079"})
    if code in excluded:
        return "RULE_PACK"
    if muc.is_generic_anchor_eligible(code=code, unit=unit, parameter_name=row["parameter_name"], excluded_codes=excluded):
        return "NUMERIC_TABLE"
    if muc.is_enum_class_eligible(code=code, unit=unit, parameter_name=row["parameter_name"], excluded_codes=excluded):
        return "ENUM"
    if muc.is_compound_eligible(code=code, unit=unit, parameter_name=row["parameter_name"], excluded_codes=excluded):
        return "COMPOUND"
    if muc.is_table_count_eligible(code=code, unit=unit, excluded_codes=excluded):
        return "TABLE_COUNT"
    return None


# --- normalisation of values / locations -------------------------------------
_NUM_RE = re.compile(r"-?\d+(?:[  ]\d{3})*(?:[.,]\d+)?")


def norm_number(text: Any) -> str | None:
    """Canonical decimal string for a printed number ('17 140,2' -> '17140.2',
    '88264,00' -> '88264'); None when `text` holds no number."""
    from decimal import Decimal, InvalidOperation

    s = str(text if text is not None else "").strip()
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        d = Decimal(raw)
    except InvalidOperation:
        return None
    out = format(d.normalize(), "f")
    return out


_CYR_TO_LAT = str.maketrans({"С": "C", "О": "O", "В": "B", "А": "A", "Е": "E", "Н": "H", "К": "K", "М": "M", "Р": "P", "Т": "T", "Х": "X"})


def norm_enum(text: Any) -> str:
    """Canonical enum token: strip punctuation/space, Cyrillic look-alikes ->
    Latin, 'СО' (letter O) -> 'C0' (digit zero), upper-case."""
    s = str(text or "").strip().strip(".,;:()«»\"' ").upper().replace(" ", "")
    s = s.translate(_CYR_TO_LAT)
    s = re.sub(r"^(C)O$", r"\g<1>0", s)
    return s


def norm_location(text: Any) -> str:
    s = str(text or "").strip().lower().replace("ё", "е")
    s = re.sub(r"[\s]+", " ", s)
    return s.strip(" .,;:«»\"'")
