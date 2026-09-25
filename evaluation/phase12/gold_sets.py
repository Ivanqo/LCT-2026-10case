"""Phase 12 label sets for grader_sim (see CASE10_AGENT_PROMPTS_BACKLOG.md, «Общий контекст Фазы 12»).

(A) official labels: public_train_checks.jsonl (10 positive Tyumen rows), TRAIN_PUBLIC negatives of all_gold_checks.jsonl
    (5 Novoslobodskaya rows), the 4 Rechnikov checks (archive; use allowed since the expert session), and the 9 cases of
    the organizer's domain re-annotation (pages + rectangles, no parameter code -> page-pair matching).
(B) the same labels, scored on live-mode predictions (organizer annotations off).
(C) SILVER: the manual 9.3 verdicts (evaluation/silver_labels/new_objects_review_pass1.jsonl) + stand A corpus pairs
    whose stage values are verified EQUAL (-> NEGATIVE); unequal corpus pairs are not labels (no verified violation).
(D) stand A L1 seeds: one "world" per seeded case, must-catch -> POSITIVE, must-not-catch -> NEGATIVE, gold evidence =
    the seeded (PD, RD) page pair.

All sets are SILVER or smaller than the organizer's own statement of adequacy ("пилот ... не подходит для количественной
приёмки"): every number is reported with its sample size and Wilson interval.

ASSUMED_DOMAIN_CODES is OUR assumption (the organizer's annotation has no code): which matrix parameters could carry each
annotated discrepancy. It is used only for the separate "page pair + code" variant, never for the page-pair match.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from evaluation.grader_sim import MATCH_PAGE_PAIR, NEGATIVE, POSITIVE, gold_item
from evaluation.measurement_bench.location_convention import SITE, expected_location_type

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE_DATA = REPO_ROOT / "case_data" / "extracted"
PARTICIPANT_DATA = CASE_DATA / "01_participant_package" / "ХАКАТОН_УЧАСТНИКАМ_ГОТОВО_К_ПЕРЕДАЧЕ" / "02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ" / "data"
PUBLIC_TRAIN_CHECKS = PARTICIPANT_DATA / "public_train_checks.jsonl"
DOCUMENT_MANIFEST = PARTICIPANT_DATA / "document_manifest.jsonl"
ALL_GOLD_CHECKS = CASE_DATA / "02_gold_methodology" / "hackathon_gold_20260811" / "ОРГАНИЗАТОР_ЗАКРЫТЫЙ" / "data" / "all_gold_checks.jsonl"
DOMAIN_ANNOTATIONS = CASE_DATA / "02_gold_methodology" / "domain_violation_reannotation_20260817" / "annotations.jsonl"
SILVER_93 = REPO_ROOT / "evaluation" / "silver_labels" / "new_objects_review_pass1.jsonl"
CORPUS = REPO_ROOT / "evaluation" / "measurement_bench" / "corpus" / "value_corpus_v1.jsonl"
SILVER_MANIFESTS = REPO_ROOT / "evaluation" / "silver_new_objects_pass1" / "manifests"

# finding_id -> (assumed matrix codes, discrepancy type). Our reading of the annotated case, not organizer data.
ASSUMED_DOMAIN_CODES: dict[str, tuple[tuple[str, ...], str]] = {
    "ALT79B-V01": (("PZ-002", "PZ-003"), "EXPLICATION_ROOM_FUNCTIONS_AREAS"),     # назначения помещений и итоги площадей этажей
    "UNDMS-V01": (("AR-044", "KR-059"), "LAYER_COMPOSITION_CHANGE_SHEET"),        # бронелист исключён из состава покрытия
    "IZM12-V01": (("KR-058", "KR-055"), "ID_REPAIR_SCHEME"),                      # ИД: схема ремонта свай (в каталоге нет кода свай)
    "LOS3A-V01": (("ODI-119",), "LAYOUT_CHANGE_SHEET"),                           # перенос двери санузла, записан в ведомости изменений
    "OKT103-V01": (("KR-054", "KR-060", "KR-061"), "ID_TOLERANCE_EXCEEDED"),      # ИД: отклонения сверх допусков
    "POL16-V01": (("PZ-002", "PZ-003", "PZ-011"), "EXPLICATION_APARTMENT_AREAS"), # площади одинаковых квартир
    "DOO25-V01": (("PZ-002", "PZ-003", "PZ-013"), "EXPLICATION_ROOM_FUNCTIONS_AREAS"),  # пищеблок, площади помещений
    "SOSH25-V01": (("PZ-002", "PZ-003"), "EXPLICATION_ROOM_ADDED"),               # добавлено помещение 1.109, итог этажа
    "POL17-N01": ((), "NEGATIVE_PAIR"),
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"stage": e["stage"], "file_id": e["file_id"], "page": e["pdf_page_number"]} for e in rows if e.get("file_id") and e.get("pdf_page_number")]


def new_object_id(prefix: str) -> str:
    return f"OBJ-NEW-{prefix}"


def available_new_objects() -> set[str]:
    return {path.stem.removeprefix("manifest_") for path in SILVER_MANIFESTS.glob("manifest_*.jsonl")}


def set_a() -> list[dict[str, Any]]:
    golds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _read_jsonl(PUBLIC_TRAIN_CHECKS):
        seen.add(row["check_id"])
        golds.append(_official_row(row, tier="TRAIN_PUBLIC"))
    for row in _read_jsonl(ALL_GOLD_CHECKS):
        if row["check_id"] in seen:
            continue
        if row["split"] == "TRAIN_PUBLIC" and row["violation_label"] == "NO_VIOLATION":
            golds.append(_official_row(row, tier="TRAIN_PUBLIC"))
        elif row["split"] == "TEST_HIDDEN":
            golds.append(_official_row(row, tier="ARCHIVE_RECHNIKOV"))
    golds.extend(domain_cases())
    return golds


def _official_row(row: dict[str, Any], *, tier: str) -> dict[str, Any]:
    label = POSITIVE if row["violation_label"] == "VIOLATION_PRESENT" else NEGATIVE
    return gold_item(
        gold_id=row["check_id"], set_name="A", tier=tier, object_id=row["object_id"], label=label,
        code=row["parameter_code"], location=str(row["location"]), evidence=_evidence(row.get("evidence") or []),
        violation_type=row.get("comparison_result"), critical=label == POSITIVE and "критическ" in str(row.get("criticality") or "").lower(),
        score_eligible=bool(row.get("score_eligible")), source_status=row.get("gold_status"),
    )


def domain_cases() -> list[dict[str, Any]]:
    by_finding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_jsonl(DOMAIN_ANNOTATIONS):
        by_finding[row["finding_id"]].append(row)
    available = available_new_objects()
    golds = []
    for finding_id in sorted(by_finding):
        rows = by_finding[finding_id]
        prefix = rows[0]["source_file_id"].split("-", 1)[0]
        status = str(rows[0]["status"]).lower()
        codes, kind = ASSUMED_DOMAIN_CODES.get(finding_id, ((), "UNMAPPED"))
        label = NEGATIVE if status == "negative" else POSITIVE
        golds.append(gold_item(
            gold_id=finding_id, set_name="A", tier=f"DOMAIN_1_1_{status.upper()}", object_id=new_object_id(prefix), label=label,
            match_mode=MATCH_PAGE_PAIR, assumed_codes=codes, violation_type=kind,
            evidence=[{"stage": r["stage"], "file_id": r["source_file_id"], "page": r["page"], "rects": r.get("rects_normalized_xyxy_top_origin") or []} for r in rows],
            critical=False if label == NEGATIVE else None, score_eligible=status in {"confirmed", "negative"},
            source_status=status, available=prefix in available,
        ))
    return golds


def set_c() -> tuple[list[dict[str, Any]], dict[str, int]]:
    golds: list[dict[str, Any]] = []
    stats = defaultdict(int)
    keys: set[tuple[str, str]] = set()
    for row in _read_jsonl(SILVER_93):
        code = row["parameter_code"]
        if row["verdict"] not in {"VIOLATION_PRESENT", "NO_VIOLATION"}:
            stats["silver_93_skipped_" + row["verdict"].lower()] += 1
            continue
        if expected_location_type(code) != SITE:
            stats["silver_93_skipped_non_site_location"] += 1
            continue
        evidence = []
        for stage, field in (("PD", "pd_reference"), ("RD", "rd_reference"), ("ID", "id_reference")):
            for ref in row.get(field) or []:
                if isinstance(ref, dict) and ref.get("file_id") and ref.get("page"):
                    evidence.append({"stage": stage, "file_id": ref["file_id"], "page": int(ref["page"])})
        keys.add((row["object_id"], code))
        golds.append(gold_item(gold_id=row["check_id"], set_name="C", tier="SILVER_9_3", object_id=row["object_id"],
                               label=POSITIVE if row["verdict"] == "VIOLATION_PRESENT" else NEGATIVE, code=code, location=SITE,
                               evidence=evidence, violation_type=f"SILVER_{row['verdict']}", score_eligible=False, source_status="SILVER_SINGLE_ANNOTATOR"))
    triples: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in _read_jsonl(CORPUS):
        triples[(row["object_id"], row["param_code"])][row["stage"]] = row
    for (object_id, code), stages in sorted(triples.items()):
        if len(stages) < 2:
            continue
        if (object_id, code) in keys:
            stats["corpus_pair_covered_by_9_3"] += 1
            continue
        if expected_location_type(code) != SITE:
            stats["corpus_pair_skipped_non_site_location"] += 1
            continue
        accepted = [_accepted(stages[stage]) for stage in sorted(stages)]
        if not set.intersection(*accepted):
            stats["corpus_pair_unequal_not_a_label"] += 1
            continue
        stats["corpus_pair_equal_negative"] += 1
        evidence = [{"stage": stage, "file_id": loc["file_id"], "page": int(loc["page"])}
                    for stage, triple in sorted(stages.items()) for loc in triple.get("locations") or [] if loc.get("file_id") and loc.get("page")]
        golds.append(gold_item(gold_id=f"{object_id}::{code}::CORPUS_EQUAL", set_name="C", tier="SILVER_CORPUS_EQUAL", object_id=object_id,
                               label=NEGATIVE, code=code, location=SITE, evidence=evidence, violation_type="SILVER_EQUAL_PAIR",
                               score_eligible=False, source_status="SILVER_SINGLE_ANNOTATOR"))
    return golds, dict(stats)


def _accepted(triple: dict[str, Any]) -> set[str]:
    values = triple.get("accepted_norm") or [triple.get("true_norm")]
    if values and isinstance(values[0], list):        # compound: one accepted tuple of components per alternative
        return {json.dumps([str(v) for v in value]) for value in values}
    return {json.dumps([str(v) for v in values])} if triple.get("family") == "COMPOUND" else {str(v) for v in values}


def set_d(l1_results: Path, object_ids: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """(golds, predictions-as-checks-with-world, stats) from a seed_l1 run that stored `seed_pair` and the protocol check."""
    from evaluation.grader_sim import normalize_prediction

    payload = json.loads(Path(l1_results).read_text(encoding="utf-8"))
    golds, preds = [], []
    stats = defaultdict(int)
    for row in payload["rows"]:
        protocol = row.get("protocol") or {}
        if not protocol.get("available") or not row.get("seed_pair") or row.get("mutation_applied") is None:
            stats["skipped_unavailable_or_not_applied"] += 1
            continue
        world = row["case_id"]
        object_id = object_ids[row["obj"]]
        golds.append(gold_item(gold_id=world, set_name="D", tier=f"SEED_L1_{row['split'].upper()}", object_id=object_id, world=world,
                               label=POSITIVE if row["must_catch"] else NEGATIVE, code=row["code"], location=SITE,
                               evidence=[{"stage": s.upper(), **row["seed_pair"][s]} for s in ("pd", "rd")],
                               violation_type=f"{row['family']}:{row['mutation']}", score_eligible=False, source_status="SEED"))
        if protocol.get("check"):
            preds.append(normalize_prediction(protocol["check"], object_id=object_id, world=world))
        stats["cases"] += 1
    return golds, preds, dict(stats)


def registry() -> dict[str, dict[str, dict[str, Any]]]:
    """File registry for document linkage: the organizer's document manifest (official objects) and our SILVER
    manifests (new objects). Neither carries a document code or revision, so those stay unverifiable."""
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in _read_jsonl(DOCUMENT_MANIFEST):
        out[row["object_id"]][row["file_id"]] = {"stage": row.get("stage"), "excluded": str(row.get("distribution_status") or "").upper() == "EXCLUDE"}
    for path in SILVER_MANIFESTS.glob("manifest_*.jsonl"):
        for row in _read_jsonl(path):
            out[row["object_id"]][row["file_id"]] = {"stage": row.get("stage"), "excluded": False}
    return dict(out)
