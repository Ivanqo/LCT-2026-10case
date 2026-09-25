"""Reusable structural candidate-coverage diagnostic.

Answers, per object and per one of the 132 matrix parameters: does this
object's document set contain at least one document, at the stage(s) the
parameter's catalog entry expects (PD/RD/ID), whose discipline plausibly
matches the catalog's own `source_pd`/`source_rd`/`source_id` hint text
(e.g. "Раздел АР: ...", "Раздел ПП (ГП): ..."?

This is deliberately NOT the same question as "did the extraction pipeline
find a SourceFragment candidate" -- CASE10's generic-tier and rule-pack
mechanisms only ever see candidates tagged by the organizer-supplied
`learning_annotation` (AUTO_FIELD_CANDIDATE) rows shipped with the 3 official
hackathon objects (see official_dataset.py/official_evidence.py); there is
no code in this repo that tags a raw, newly-supplied PDF on its own. For any
object without that pre-supplied tagging, the pipeline-level candidate count
is unconditionally zero regardless of document composition (see
NEW_OBJECTS_ANALYSE.md for the direct in-process confirmation on 6 new
objects). This module answers the weaker, upstream structural question
checkpoint 40 first asked by hand for Tyumenskaya/Novoslobodskaya: is the
right *kind* of document even present in the object's document set at all,
independent of whether anything in this codebase could currently locate it.

Usable both on our own SILVER manifest rows (New_data objects, `discipline`
field, Cyrillic codes) and on the official document_manifest.jsonl (`section`
field, transliterated codes) via DISCIPLINE_ALIASES.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

# Canonical discipline vocabulary (ПП РФ №87 section abbreviations) plus the
# transliterated codes the official document_manifest.jsonl / dataset_sources
# pipeline uses for `section`. Every alias maps to one canonical Cyrillic code.
DISCIPLINE_ALIASES: dict[str, str] = {
    "АР": "АР", "AR": "АР",
    "КЖ": "КР", "КМ": "КР", "КР": "КР", "KR": "КР",
    "ГП": "ГП", "ПЗУ": "ГП", "GP": "ГП",
    "ОВ": "ОВ", "OV": "ОВ",
    "ВК": "ВК", "VK": "ВК",
    "ЭОМ": "ЭОМ", "ЭО": "ЭОМ", "EOM": "ЭОМ",
    "СС": "СС", "SS": "СС",
    "ПОС": "ПОС", "POS": "ПОС",
    "ПБ": "ПБ", "PB": "ПБ",
    "ООС": "ООС",
    "ОДИ": "ОДИ",
    "ТХ": "ТХ",
    "АИ": "АИ",
    "НИС": "НИС",
    "ПЗ": "ПЗ",
    "БТИ": "БТИ",
    "ЗОС": "ЗОС",
    "ОТДЕЛКА": "АР",
}

_TOKEN_RE = re.compile(r"[A-ZА-ЯЁ]{2,5}")

STAGE_SOURCE_FIELD = {"PD": "source_pd", "RD": "source_rd", "ID": "source_id"}


def parse_discipline_hints(source_text: str | None) -> set[str]:
    """Extract plausible discipline codes from a catalog `source_*` hint
    string, e.g. 'Раздел ПП (ГП): Лист "Общие данные", Таблица ТЭП' -> {ГП}."""
    if not source_text:
        return set()
    hints: set[str] = set()
    for token in _TOKEN_RE.findall(source_text.upper()):
        canonical = DISCIPLINE_ALIASES.get(token)
        if canonical:
            hints.add(canonical)
    return hints


def load_parameter_catalog(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["_hints"] = {
                stage: parse_discipline_hints(row.get(field))
                for stage, field in STAGE_SOURCE_FIELD.items()
            }
            rows.append(row)
    return rows


def discipline_inventory_from_registry_csv(path: str | Path) -> dict[str, Counter]:
    """Build {stage: Counter[discipline]} from one of our SILVER registry
    CSVs (multi_object_annotation_20260817/*/..._РЕЕСТР_*_ФАЙЛОВ.csv)."""
    inventory: dict[str, Counter] = defaultdict(Counter)
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stage = row["stage"]
            disc_raw = (row.get("discipline") or "OTHER").strip().upper()
            canonical = DISCIPLINE_ALIASES.get(disc_raw, "OTHER" if disc_raw in ("", "OTHER") else disc_raw)
            inventory[stage][canonical] += 1
    return inventory


def discipline_inventory_from_manifest_jsonl(path: str | Path, object_id: str) -> dict[str, Counter]:
    """Build {stage: Counter[discipline]} from an official-schema
    document_manifest.jsonl (`section` field), filtered to one object_id."""
    inventory: dict[str, Counter] = defaultdict(Counter)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("object_id") != object_id:
                continue
            stage = row.get("stage")
            if stage not in ("PD", "RD", "ID"):
                continue
            disc_raw = (row.get("section") or "OTHER").strip().upper()
            canonical = DISCIPLINE_ALIASES.get(disc_raw, "OTHER" if disc_raw in ("", "OTHER") else disc_raw)
            inventory[stage][canonical] += 1
    return inventory


def coverage_report(
    object_code: str,
    inventory: dict[str, Counter],
    catalog_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """For every (parameter, stage) the catalog expects a source for, check
    whether `inventory[stage]` contains at least one document whose
    discipline is in the parsed hint set for that stage (or the hint set is
    empty/unparseable, in which case ANY document at that stage counts --
    an unparsed hint must not be scored as "impossible", only "unknown";
    generic `OTHER`-only inventories still count for hint-less parameters
    but not for parameters with a specific parsed hint that OTHER can't
    satisfy)."""
    per_param = []
    stage_totals = {stage: {"expected": 0, "has_any_doc": 0, "has_matching_discipline": 0} for stage in ("PD", "RD", "ID")}
    for row in catalog_rows:
        code = row.get("parameter_code")
        entry = {"parameter_code": code, "parameter_name": row.get("parameter_name")}
        for stage, field in STAGE_SOURCE_FIELD.items():
            source_text = row.get(field)
            if not source_text:
                continue
            stage_totals[stage]["expected"] += 1
            hints = row["_hints"][stage]
            stage_docs = inventory.get(stage, Counter())
            has_any_doc = sum(stage_docs.values()) > 0
            if hints:
                matching = sum(stage_docs.get(h, 0) for h in hints)
            else:
                matching = sum(stage_docs.values())
            entry[f"{stage}_hint_disciplines"] = sorted(hints)
            entry[f"{stage}_available_disciplines"] = dict(stage_docs)
            entry[f"{stage}_has_any_doc"] = has_any_doc
            entry[f"{stage}_has_matching_discipline"] = matching > 0
            if has_any_doc:
                stage_totals[stage]["has_any_doc"] += 1
            if matching > 0:
                stage_totals[stage]["has_matching_discipline"] += 1
        per_param.append(entry)
    return {
        "object_code": object_code,
        "stage_totals": stage_totals,
        "per_param": per_param,
    }


def summarize(report: dict[str, Any]) -> str:
    lines = [f"=== {report['object_code']} ==="]
    for stage, totals in report["stage_totals"].items():
        lines.append(
            f"  {stage}: expected={totals['expected']:3d}  "
            f"any_doc_at_stage={totals['has_any_doc']:3d}  "
            f"discipline_matches_hint={totals['has_matching_discipline']:3d}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True, help="path to parameter_catalog_132.jsonl")
    ap.add_argument("--registry-csv", help="SILVER registry CSV (multi_object_annotation_20260817 style)")
    ap.add_argument("--manifest-jsonl", help="official-schema document_manifest.jsonl")
    ap.add_argument("--object-id", help="object_id to filter (required with --manifest-jsonl)")
    ap.add_argument("--object-code", default="OBJECT")
    ap.add_argument("--json-out", help="optional path to dump the full per-param report as JSON")
    args = ap.parse_args()

    catalog = load_parameter_catalog(args.catalog)
    if args.registry_csv:
        inv = discipline_inventory_from_registry_csv(args.registry_csv)
    elif args.manifest_jsonl:
        inv = discipline_inventory_from_manifest_jsonl(args.manifest_jsonl, args.object_id)
    else:
        raise SystemExit("need --registry-csv or --manifest-jsonl/--object-id")

    report = coverage_report(args.object_code, inv, catalog)
    print(summarize(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
