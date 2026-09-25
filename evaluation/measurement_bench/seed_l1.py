"""Metric 2, level L1: seeded violations at the PAGE-SNAPSHOT level.

For every (object, parameter) where the corpus says PD and RD state the SAME value AND the real extractor found
both correctly on the baseline run (the "pair found" step -- whose own failure is Metric 1), the RD page snapshot
(the words the extractor sees) is edited at exactly the token the extractor read:

  numeric   DEC_ABOVE / INC_ABOVE   change beyond the parameter's tolerance   -> must be caught
            WITHIN_TOL              change inside the tolerance               -> must NOT be caught
            FORMAT_ONLY             same number, other formatting (, <-> . / thousands blank) -> must NOT be caught
  enum      DOWNGRADE               class lowered (I->II, C0->C1, A500C->A400, C345->C245, A->B, II->III)  -> must be caught
            FORMAT_ONLY             Cyrillic/Latin look-alike letters       -> must NOT be caught
  compound  COMPONENT_DEC           first component -20%                    -> must be caught
  count     ROW_DELETE              one table content row removed           -> must be caught

Tolerance: parsed from the catalog trigger text when it states a percentage ("> 1%", "> 5%"); otherwise the system's own
comparison tolerance (max(0.01, 0.1%)).  Two decision layers are reported:
  * "extractor+legacy compare": the four collectors + the original equality functions (values_equal / enum_values_equal /
    compare_components / counts_equal) -- independent of the decision-layer work (prompt B) that edits the same tree;
  * "protocol level": create_official_evidence_groups for the single code on the same baseline DB (whatever the tree
    does at run time, incl. any gate) -- only when that entry point works.
Detection (recall) requires: label VIOLATION_PRESENT, right parameter_code, location by convention, evidence citing the
right (file, page) pair for PD and RD.  Reproducible from SEED (case list + hash written to SEED_DIR).
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .common import OUT_DIR, SEED_DIR, catalog_by_code, norm_enum, norm_number, read_jsonl, utcnow_iso, CORPUS_DIR
from .location_convention import expected_location_type, location_ok
from .metric1 import rate, value_equal, wilson
from .pipeline_env import open_db, patch_bytes, require_frozen_corpus

SEED = 20260924
_PCT_RE = re.compile(r"(?:>|≥|более|свыше|превыш\w*|не менее|не более|выше)\s*(\d+(?:[.,]\d+)?)\s*%")
SYSTEM_TOLERANCE_REL = Decimal("0.001")

DOWNGRADE = {
    "I": "II", "II": "III", "III": "IV", "C0": "C1", "C1": "C2", "A500C": "A400", "A500": "A400", "A400": "A240",
    "C345": "C245", "C255": "C245", "C245": "C235", "A": "B", "B": "C", "A+": "A", "A++": "A+",
}


def tolerance_of(code: str) -> Decimal | None:
    trig = str(catalog_by_code()[code].get("trigger") or "")
    m = _PCT_RE.search(trig)
    return Decimal(m.group(1).replace(",", ".")) / 100 if m else None


def fmt_like(printed: str, value: Decimal) -> str:
    """Format `value` with the decimals/comma style of the printed token."""
    m = re.search(r"[.,](\d+)$", printed.strip())
    decimals = len(m.group(1)) if m else 0
    q = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = format(q, "f")
    return text.replace(".", ",") if "," in printed else text


class SnapshotMutator:
    """Wraps extract_original_pages: edits the snapshot of chosen (file_id, page) pairs, never the cached originals."""

    def __init__(self, orig):
        self.orig = orig
        self.rules: dict[tuple[str, int], Any] = {}

    def __call__(self, document, page_numbers):
        res = self.orig(document, page_numbers)
        fid = getattr(document, "dataset_file_id", None)
        for page in list(res):
            fn = self.rules.get((fid, int(page)))
            if fn is not None:
                res[page] = fn(copy.deepcopy(res[page]))
        return res


def _word_indices(snap: dict, bbox: list[float]) -> list[int]:
    x0, y0, x1, y1 = bbox
    return [i for i, w in enumerate(snap.get("words") or []) if w.get("bbox") and w["bbox"][0] >= x0 - 1 and w["bbox"][2] <= x1 + 1 and w["bbox"][1] >= y0 - 1 and w["bbox"][3] <= y1 + 1]


def replace_words(snap: dict, idx: list[int], new_texts: list[str]) -> dict:
    """Replace the words at `idx` by `new_texts` (any count): the union bbox of the replaced words is split evenly among the
    new words, which take the place of the first replaced word (reading order preserved)."""
    words = snap["words"]
    idx = sorted(idx)
    first, last = idx[0], idx[-1]
    boxes = [words[i]["bbox"] for i in idx]
    bb = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    n = max(1, len(new_texts))
    new_words = [{**words[first], "text": t, "bbox": [bb[0] + (bb[2] - bb[0]) * k / n, bb[1], bb[0] + (bb[2] - bb[0]) * (k + 1) / n, bb[3]]} for k, t in enumerate(new_texts)]
    kept_between = [w for j, w in enumerate(words[first:last + 1]) if (first + j) not in idx]
    snap["words"] = words[:first] + new_words + kept_between + words[last + 1:]
    snap["text"] = " ".join(w["text"] for w in snap["words"])
    return snap


def _obs_equal(o1: dict, o2: dict) -> bool:
    """Equality of two extractor observations as the mechanisms compare them (values_equal / canonical / components / count)."""
    fam = o1["family"]
    try:
        if fam == "NUMERIC_TABLE":
            a, b = Decimal(str(o1["norm"])), Decimal(str(o2["norm"]))
            return abs(a - b) <= max(Decimal("0.01"), abs(a) * Decimal("0.001"))
        if fam == "ENUM":
            return o1["canonical"] == o2["canonical"]
        if fam == "COMPOUND":
            return len(o1["norms"]) == len(o2["norms"]) and all(Decimal(x) == Decimal(y) for x, y in zip(o1["norms"], o2["norms"]))
        return int(o1["row_count"]) == int(o2["row_count"])
    except Exception:  # noqa: BLE001
        return False


def make_cases(results: list[dict], corpus: dict[str, dict], ext: dict[str, dict]) -> list[dict]:
    """Base pairs + mutation list (deterministic order).

    TRUTH_PAIR   the corpus states the same value on PD and RD and the extractor found BOTH correctly (the task's condition).
    SYSTEM_PAIR  supplementary: the extractor found an equal PD/RD pair that the corpus does not confirm (other/wrong token or no
                 corpus triple).  It tests the comparison sensitivity at the token the extractor read, not the parameter's truth --
                 reported separately and never used for the matrix statuses."""
    by = defaultdict(dict)
    for r in results:
        by[(r["obj"], r["param_code"])][r["stage"]] = r
    truth_keys: set[tuple[str, str]] = set()
    cases: list[dict] = []

    def add(obj, code, fam, obs_pd, obs_rd, split, kind, true_norm=None):
        window = [tuple(x) for x in (ext[obj].get("rendered_window", {}).get(code, {}).get("RD") or [])]
        base = {"obj": obj, "code": code, "family": fam, "pd_obs": obs_pd, "rd_obs": obs_rd, "true_norm": true_norm, "split": split, "base_kind": kind, "rd_window": window}
        muts: list[tuple[str, bool]] = []
        if fam == "NUMERIC_TABLE":
            muts = [("DEC_ABOVE", True), ("INC_ABOVE", True), ("WITHIN_TOL", False), ("FORMAT_ONLY", False)]
        elif fam == "ENUM":
            if norm_enum(obs_rd.get("canonical")) in DOWNGRADE:
                muts.append(("DOWNGRADE", True))
            muts.append(("FORMAT_ONLY", False))
        elif fam == "COMPOUND":
            muts = [("COMPONENT_DEC", True)]
        elif fam == "TABLE_COUNT":
            muts = [("ROW_DELETE", True)]
        muts.append(("IDENTITY", False))
        for m, must in muts:
            cases.append({**base, "mutation": m, "must_catch": must, "case_id": f"{obj}::{code}::{m}::{kind}"})

    for (obj, code), st in sorted(by.items()):
        pd, rd = st.get("PD"), st.get("RD")
        if not pd or not rd or pd["status"] != "CORRECT" or rd["status"] != "CORRECT":
            continue
        tp, tr = corpus[pd["triple_id"]], corpus[rd["triple_id"]]
        if tp["true_norm"] != tr["true_norm"]:
            continue
        truth_keys.add((obj, code))
        add(obj, code, tp["family"], ext[obj]["observations"][code]["PD"], ext[obj]["observations"][code]["RD"], tp["split"], "TRUTH_PAIR", tp["true_norm"])
    for obj, e in sorted(ext.items()):
        from .common import OBJECTS

        for code, stages in sorted(e["observations"].items()):
            if (obj, code) in truth_keys or "PD" not in stages or "RD" not in stages:
                continue
            if stages["PD"]["family"] != stages["RD"]["family"] or not _obs_equal(stages["PD"], stages["RD"]):
                continue
            add(obj, code, stages["PD"]["family"], stages["PD"], stages["RD"], OBJECTS[obj]["split"], "SYSTEM_PAIR")
    return cases


def mutate(snap: dict, case: dict) -> tuple[dict | None, str | None]:
    """Return (mutated snapshot, new printed text) or (None, reason) when the mutation cannot be expressed."""
    obs = case["rd_obs"]
    m, fam = case["mutation"], case["family"]
    if m == "IDENTITY":
        return snap, "identity"
    idx = _word_indices(snap, obs["bbox_pdf"])
    if not idx:
        return None, "token_not_found_in_snapshot"
    code = case["code"]
    if fam == "NUMERIC_TABLE":
        printed = obs["value"]
        v = Decimal(str(obs["norm"]))
        thr = tolerance_of(code)
        if m == "FORMAT_ONLY":
            if "," in printed:
                new = printed.replace(",", ".")
            elif "." in printed:
                new = printed.replace(".", ",")
            else:
                new = printed
            ip = re.match(r"^-?(\d+)", new.replace(" ", ""))
            if ip and len(ip.group(1)) >= 4:              # also insert a thousands blank: "88264,00" -> two words "88" "264,00"
                digits = re.sub(r"[^\d,.\-]", "", new)
                head, tail = re.match(r"^(-?\d+?)(\d{3}(?:[.,]\d+)?)$", digits).groups()
                return replace_words(snap, idx, [head, tail]), f"{head} {tail}"
            return replace_words(snap, idx, [new]), new
        if m == "WITHIN_TOL":
            factor = (Decimal(1) + thr * Decimal("0.3")) if thr else (Decimal(1) + SYSTEM_TOLERANCE_REL / 2)
        else:
            step = max(thr * 3, Decimal("0.05")) if thr else Decimal("0.10")
            factor = Decimal(1) - step if m == "DEC_ABOVE" else Decimal(1) + step
        new = fmt_like(printed, v * factor)
        if Decimal(norm_number(new)) == v:                # rounding swallowed the change: amplify the decimals
            new = fmt_like(printed + "0", v * factor)
        return replace_words(snap, idx, [new]), new
    if fam == "ENUM":
        canon = norm_enum(obs["canonical"])
        text = snap["words"][idx[0]]["text"]
        if m == "FORMAT_ONLY":
            swap = {"С": "C", "C": "С", "А": "A", "A": "А", "О": "O"}
            new = "".join(swap.get(ch, ch) for ch in text)
            return replace_words(snap, idx[:1], [new]), new
        target = DOWNGRADE[canon]
        core = re.sub(r"[^\w+]", "", text)
        new = text.replace(core, target) if core and core in text else target
        return replace_words(snap, idx[:1], [new]), new
    if fam == "COMPOUND":
        vals = [Decimal(x) for x in obs["norms"]]
        printed = obs["values"]
        # the observation's bbox spans the whole matched context: edit the word that carries the FIRST component, not the first word
        target = None
        for i in idx:
            core = re.sub(r"[^\d,.\-]", "", snap["words"][i]["text"]).strip(".,")
            if core and norm_number(core) == norm_number(printed[0]):
                target = i
                break
        if target is None:
            return None, "first_component_token_not_found"
        new_first = fmt_like(printed[0], vals[0] * Decimal("0.8"))
        if Decimal(norm_number(new_first)) == vals[0]:    # rounding swallowed the change (small integer component): step down by one
            new_first = fmt_like(printed[0], vals[0] - Decimal(1))
        if not (Decimal(0) <= Decimal(norm_number(new_first)) < vals[0]):
            return None, "no_effective_change"
        old_text = snap["words"][target]["text"]
        core = re.sub(r"[^\d,.\-]", "", old_text).strip(".,")
        return replace_words(snap, [target], [old_text.replace(core, new_first, 1)]), new_first
    if fam == "TABLE_COUNT":
        # remove all words of the LAST content row inside the table bbox (bbox rows are clustered by y)
        from app.domain.anchor_search import cluster_rows

        words = snap["words"]
        x0, y0, x1, y1 = obs["bbox_pdf"]
        inside = [w for w in words if w["bbox"][1] >= y0 - 1 and w["bbox"][3] <= y1 + 1]
        rows = cluster_rows(inside)
        if len(rows) < 3:
            return None, "table_too_small"
        drop_ids = {id(w) for w in rows[-1]}
        snap["words"] = [w for w in words if id(w) not in drop_ids]
        snap["text"] = " ".join(w["text"] for w in snap["words"])
        return snap, "row_deleted"
    return None, "unsupported"


def compare_legacy(fam: str, pd: Any, rd: Any) -> bool:
    """True = equal (NEGATIVE_VERIFIED) under the original mechanism comparison functions."""
    from app.domain.generic_compound_extraction import compare_components
    from app.domain.generic_enum_extraction import enum_values_equal
    from app.domain.generic_matrix_extraction import values_equal
    from app.domain.generic_table_row_count import counts_equal

    if fam == "NUMERIC_TABLE":
        return values_equal(pd.decimal_value, rd.decimal_value)
    if fam == "ENUM":
        return enum_values_equal(pd.canonical_value, rd.canonical_value)
    if fam == "COMPOUND":
        return all(c.equal for c in compare_components(pd, rd))
    return counts_equal(pd.row_count, rd.row_count)


def evaluate_pair(ctx: dict, case: dict) -> dict:
    """Collectors for ONE parameter on the (possibly edited) pages + legacy comparison of the PD/RD observations."""
    from app.domain.generic_compound_extraction import collect_compound_observations, new_compound_budget
    from app.domain.generic_enum_extraction import collect_enum_observations, new_enum_budget
    from app.domain.generic_matrix_extraction import collect_generic_observations, new_generic_budget
    from app.domain.generic_table_row_count import collect_table_count_observations, new_table_count_budget
    from app.domain.official_evidence import STAGE_CODES
    from app.domain.official_rule_packs import SUPPORTED_RULE_CODES

    code, fam = case["code"], case["family"]
    param = ctx["params_by_code"][code]
    by_code = {code: ctx["by_code"].get(code, [])}
    kw = dict(stage_codes=STAGE_CODES)
    if fam == "NUMERIC_TABLE":
        res = collect_generic_observations([param], by_code, ctx["by_id"], budget=new_generic_budget(), **kw)
    elif fam == "ENUM":
        res = collect_enum_observations([param], by_code, ctx["by_id"], budget=new_enum_budget(), excluded_codes=SUPPORTED_RULE_CODES, **kw)
    elif fam == "COMPOUND":
        res = collect_compound_observations([param], by_code, ctx["by_id"], budget=new_compound_budget(), excluded_codes=SUPPORTED_RULE_CODES, **kw)
    else:
        res = collect_table_count_observations([param], by_code, ctx["by_id"], budget=new_table_count_budget(), excluded_codes=SUPPORTED_RULE_CODES, **kw)
    stages = res.get(int(param.id)) or {}
    out: dict[str, Any] = {"case_id": case["case_id"]}
    if "PD" not in stages or "RD" not in stages:
        out.update(label="COMPARISON_IMPOSSIBLE", reason="pair_lost_after_mutation", stages=sorted(stages))
        return out
    pd, rd = stages["PD"], stages["RD"]
    equal = compare_legacy(fam, pd, rd)
    out["label"] = "NO_VIOLATION" if equal else "VIOLATION_PRESENT"
    out["pd"] = {"file_id": pd.document.dataset_file_id, "page": pd.page}
    out["rd"] = {"file_id": rd.document.dataset_file_id, "page": rd.page}
    out["rd_value"] = getattr(rd, "value", None) if fam in ("NUMERIC_TABLE", "ENUM") else (getattr(rd, "row_count", None) if fam == "TABLE_COUNT" else getattr(rd, "raw_values", None))
    out["location"] = pd.location
    out["evidence_ok"] = (out["pd"]["file_id"] == case["pd_obs"]["file_id"] and out["pd"]["page"] == case["pd_obs"]["page"]
                          and out["rd"]["file_id"] == case["rd_obs"]["file_id"] and out["rd"]["page"] == case["rd_obs"]["page"])
    ok_loc, lvl = location_ok(code, pd.location)
    out["location_ok"] = ok_loc
    out["location_level"] = lvl
    # the seeded edit sits on ONE page; a duplicate/sibling RD page restating the same (unedited) value can win the RD selection
    out["masked_by_other_rd_candidate"] = (out["label"] != "VIOLATION_PRESENT" and not out["evidence_ok"]
                                            and (out["rd"]["file_id"], out["rd"]["page"]) != (case["rd_obs"]["file_id"], case["rd_obs"]["page"]))
    return out


def evaluate_protocol_pair(ctx: dict, case: dict) -> dict:
    """Same edited pages, but through create_official_evidence_groups (the real decision layer of the code tree in use)
    for the single parameter; rolled back afterwards.  Returns the submission-style check for that parameter."""
    from app.db.models import EvidenceFragment, EvidenceGroup
    from app.domain.official_evidence import create_official_evidence_groups
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process
    from evaluation.exporter import evidence_group_to_submission_check

    db = ctx["db"]
    param = ctx["params_by_code"][case["code"]]
    out: dict[str, Any] = {"available": True}
    try:
        process = create_process(db, project_id=1, organization_id=1, object_id=ctx["object_id"], matrix_version=MATRIX_VERSION_OFFICIAL)
        create_official_evidence_groups(db, process, [param], ctx["docs"], user_id=None)
        groups = db.query(EvidenceGroup).filter(EvidenceGroup.process_id == process.id, EvidenceGroup.param_id == param.id).all()
        checks = []
        for g in groups:
            frs = db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == g.id).all()
            gd = {"id": g.id, "parameter": {"code": param.code, "criticality": getattr(param, "criticality", None)}, "delta": g.delta or {}, "entity_name": None,
                  "object_id": g.object_id, "finding_status": g.finding_status, "review_priority": g.review_priority,
                  "fragments": [{"stage": f.stage, "dataset_stage": None, "file_id": f.dataset_file_id, "page": f.page, "extracted_value": f.extracted_value} for f in frs]}
            checks.append(evidence_group_to_submission_check(gd))
        mine = [c for c in checks if c["parameter_code"] == case["code"]]
        if not mine:
            out.update(label="NO_CHECK", reason="no_group_for_parameter")
        else:
            c = mine[0]
            out["label"] = c["violation_label"]
            out["protocol_status"] = c["protocol_status"]
            out["location"] = c["location"]
            ev = {(e["stage"], e["file_id"], e["pdf_page_number"]) for e in c["evidence"]}
            want = {("PD", case["pd_obs"]["file_id"], case["pd_obs"]["page"]), ("RD", case["rd_obs"]["file_id"], case["rd_obs"]["page"])}
            out["evidence_ok"] = want <= ev
            ok_loc, lvl = location_ok(case["code"], c["location"])
            out["location_ok"], out["location_level"] = ok_loc, lvl
            out["reason"] = next((g.delta or {}).get("reason") for g in groups if (g.delta or {}).get("parameter_code", case["code"]) == case["code"]) if groups else None
    except Exception as exc:  # noqa: BLE001
        out = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.rollback()
    return out


def _same_value(fam: str, r: dict, obs: dict) -> bool:
    try:
        if fam == "NUMERIC_TABLE":
            return Decimal(str(r["norm"])) == Decimal(str(obs["norm"]))
        if fam == "ENUM":
            return norm_enum(r["canonical"]) == norm_enum(obs["canonical"])
        if fam == "COMPOUND":
            return [Decimal(x) for x in r["norms"]] == [Decimal(x) for x in obs["norms"]]
    except Exception:  # noqa: BLE001
        return False
    return False


def make_all_copies_rule(case: dict, applied: dict):
    """Edit EVERY restatement of the base value on an RD candidate page (a consistent RD-wide design change): the token is
    re-located on each page with the family's own extractor (no assumption about its bbox) and edited if it reads the same value."""
    from .metric1 import isolated_extract

    fam = case["family"]

    def rule(snap):
        r = isolated_extract(fam, case["code"], "RD", snap)
        if r is None or not _same_value(fam, r, case["rd_obs"]):
            return snap
        words = snap["words"]
        idx = list(range(r["word_start"], r["word_end"] + 1))
        boxes = [words[i]["bbox"] for i in idx if 0 <= i < len(words)]
        if not boxes:
            return snap
        bb = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        page_case = {**case, "rd_obs": {**case["rd_obs"], "bbox_pdf": bb, "value": r.get("value"), "norm": r.get("norm"), "canonical": r.get("canonical"),
                                        "values": r.get("values"), "norms": r.get("norms")}}
        new_snap, text = mutate(snap, page_case)
        if new_snap is None:
            return snap
        applied["n_pages"] = applied.get("n_pages", 0) + 1
        applied["text"] = text
        return new_snap

    return rule


def run_case_observation_level(ctx: dict, case: dict, mut: SnapshotMutator, variant: str = "SINGLE") -> dict:
    rd_fid, rd_page = case["rd_obs"]["file_id"], case["rd_obs"]["page"]
    mut.rules = {}
    snap_note: dict[str, Any] = {"applied": None, "error": None, "n_pages": 0}
    if variant == "SINGLE":
        def rule(snap):
            new_snap, new_text = mutate(snap, case)
            snap_note["applied"] = new_text if new_snap is not None else None
            snap_note["error"] = None if new_snap is not None else new_text
            snap_note["n_pages"] = 1 if new_snap is not None else 0
            return new_snap if new_snap is not None else snap

        mut.rules[(rd_fid, rd_page)] = rule
        edited_pages = {(rd_fid, rd_page)}
    else:
        applied: dict[str, Any] = {}
        rule_all = make_all_copies_rule(case, applied)
        pages = {(rd_fid, rd_page)} | set(case.get("rd_window") or [])
        for key in pages:
            mut.rules[key] = rule_all
        edited_pages = pages
    out: dict[str, Any] = {"case_id": case["case_id"], "variant": variant}
    result = evaluate_pair(ctx, case)
    if variant != "SINGLE":
        snap_note["applied"] = applied.get("text")
        snap_note["n_pages"] = applied.get("n_pages", 0)
        snap_note["error"] = None if applied.get("n_pages") else "no_page_edited"
    out.update(result)
    if variant != "SINGLE" and out.get("rd"):
        # evidence: PD as before; RD may cite ANY of the edited restatements
        out["evidence_ok"] = bool(out["pd"]["file_id"] == case["pd_obs"]["file_id"] and out["pd"]["page"] == case["pd_obs"]["page"] and (out["rd"]["file_id"], out["rd"]["page"]) in edited_pages)
        out["masked_by_other_rd_candidate"] = out["label"] != "VIOLATION_PRESENT" and (out["rd"]["file_id"], out["rd"]["page"]) not in edited_pages
    out["mutation_applied"], out["mutation_error"], out["pages_edited"] = snap_note["applied"], snap_note["error"], snap_note["n_pages"]
    if snap_note["applied"] is None:
        out["label"] = "NOT_APPLICABLE"
    elif ctx.get("protocol"):
        out["protocol"] = evaluate_protocol_pair(ctx, case)
        if variant != "SINGLE" and out["protocol"].get("available") and out["protocol"].get("label") is not None:
            pr = out["protocol"]
            ev_ok = out["evidence_ok"] if out.get("evidence_ok") is not None else None
            pr["evidence_ok"] = bool(pr.get("evidence_ok")) or (bool(ev_ok) and pr.get("label") == "VIOLATION_PRESENT")
    return out


def load_ctx(obj: str) -> dict:
    """Reopen the baseline DB written by run_extraction (tagger fragments + documents)."""
    from collections import defaultdict as dd

    from app.db.models import SourceFragment
    from app.domain.official_evidence import inference_annotation
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, current_document_versions, list_active_params, _project_documents, create_process

    patch_bytes(obj)
    db, engine = open_db(obj, fresh=False)
    from app.db.models import DocumentVersion

    docs = current_document_versions(db.query(DocumentVersion).all())
    params = list_active_params(db, organization_id=1, project_id=1, matrix_version=MATRIX_VERSION_OFFICIAL)
    by_id = {d.id: d for d in docs}
    by_code = dd(list)
    for f in db.query(SourceFragment).filter(SourceFragment.document_version_id.in_(by_id)).all():
        if inference_annotation(f):
            by_code[(f.metadata_json or {}).get("code")].append(f)
    from app.db.models import ConstructionObject  # noqa: F401  (import check)
    from .common import OBJECTS

    return {"db": db, "docs": docs, "by_id": by_id, "by_code": by_code, "params_by_code": {str(p.code): p for p in params},
            "object_id": OBJECTS[obj]["object_id"], "protocol": True}


def summarize(rows: list[dict], layer: str) -> dict:
    def block(sel: list[dict]) -> dict:
        must = [r for r in sel if r["must_catch"] and r.get("label") != "NOT_APPLICABLE"]
        mustnot = [r for r in sel if not r["must_catch"] and r.get("label") != "NOT_APPLICABLE"]
        det_label = [r for r in must if r["label"] == "VIOLATION_PRESENT"]
        det_ev = [r for r in det_label if r.get("evidence_ok")]
        det_full = [r for r in det_ev if r.get("location_ok")]
        fp = [r for r in mustnot if r["label"] == "VIOLATION_PRESENT"]
        return {
            "n_pairs": len({(r["obj"], r["code"]) for r in must}), "n_must_catch": len(must), "recall_label_only": rate(len(det_label), len(must)), "recall_label_and_evidence": rate(len(det_ev), len(must)),
            "recall_label_evidence_location": rate(len(det_full), len(must)),
            "n_must_not_catch": len(mustnot), "fp_rate": rate(len(fp), len(mustnot)),
            "abstained_or_lost": Counter(r["label"] for r in must if r["label"] not in ("VIOLATION_PRESENT",)),
            "misses_masked_by_other_rd_candidate": sum(1 for r in must if r["label"] != "VIOLATION_PRESENT" and r.get("masked_by_other_rd_candidate")),
        }
    out = {"layer": layer, "all": block(rows)}
    for m in sorted({r["mutation"] for r in rows}):
        out[f"mutation:{m}"] = block([r for r in rows if r["mutation"] == m])
    for f in ("NUMERIC_TABLE", "ENUM", "COMPOUND", "TABLE_COUNT"):
        sel = [r for r in rows if r["family"] == f]
        if sel:
            out[f"family:{f}"] = block(sel)
    for s in ("holdout", "dev"):
        sel = [r for r in rows if r["split"] == s]
        if sel:
            out[f"split:{s}"] = block(sel)
    for k in ("TRUTH_PAIR", "SYSTEM_PAIR"):
        sel = [r for r in rows if r.get("base_kind") == k]
        if sel:
            out[f"base:{k}"] = block(sel)
    for v in ("SINGLE", "ALL_COPIES"):
        sel = [r for r in rows if r.get("variant") == v]
        if sel:
            out[f"variant:{v}"] = block(sel)
            for k in ("TRUTH_PAIR", "SYSTEM_PAIR"):
                sub = [r for r in sel if r.get("base_kind") == k]
                if sub:
                    out[f"variant:{v}/base:{k}"] = block(sub)
    return out


def main() -> int:
    manifest = require_frozen_corpus()
    metric1 = json.loads((OUT_DIR / "metric1_results.json").read_text(encoding="utf-8"))["results"]
    corpus = {r["triple_id"]: r for r in read_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl")}
    ext = {}
    for o in {r["obj"] for r in metric1}:
        p = OUT_DIR / f"extract_{o}.json"
        if p.exists():
            ext[o] = json.loads(p.read_text(encoding="utf-8"))
    cases = make_cases([r for r in metric1 if r["obj"] in ext], corpus, ext)
    rng = random.Random(SEED)
    rng.shuffle(cases)                                   # order is seed-determined; results do not depend on it
    cases.sort(key=lambda c: c["case_id"])
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    seed_manifest = {"seed": SEED, "generated_at": utcnow_iso(), "corpus_sha256": manifest["corpus_sha256"], "n_cases": len(cases),
                     "cases": [{k: c[k] for k in ("case_id", "obj", "code", "family", "mutation", "must_catch", "base_kind")} for c in cases]}
    seed_manifest["cases_sha256"] = hashlib.sha256(json.dumps(seed_manifest["cases"], sort_keys=True).encode()).hexdigest()
    (SEED_DIR / "l1_seed_manifest.json").write_text(json.dumps(seed_manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    import app.domain.cross_stage_localization as csl

    mut = SnapshotMutator(csl.extract_original_pages)
    csl.extract_original_pages = mut
    rows: list[dict] = []
    ctxs: dict[str, dict] = {}
    for c in cases:
        if c["obj"] not in ctxs:
            ctxs[c["obj"]] = load_ctx(c["obj"])
        for variant in (("SINGLE",) if c["family"] == "TABLE_COUNT" or c["mutation"] == "IDENTITY" else ("SINGLE", "ALL_COPIES")):
            r = run_case_observation_level(ctxs[c["obj"]], c, mut, variant)
            r["case_id"] = f"{c['case_id']}::{variant}"
            rows.append({**{k: c[k] for k in ("obj", "code", "family", "mutation", "must_catch", "split", "base_kind")}, **r})
            print(r["case_id"], r.get("label"), r.get("mutation_applied"), r.get("pages_edited"), flush=True)
    summary = summarize(rows, "extractor+legacy_compare")
    proto_rows = [{**r, **{k: r["protocol"].get(k) for k in ("label", "evidence_ok", "location_ok")}} for r in rows if r.get("protocol", {}).get("available")]
    summary_protocol = summarize(proto_rows, "protocol_level (create_official_evidence_groups of the code tree in use)") if proto_rows else None
    unavailable = Counter(r["protocol"].get("error") for r in rows if r.get("protocol") and not r["protocol"].get("available"))
    (OUT_DIR / "l1_results.json").write_text(json.dumps({"rows": rows, "summary": summary, "summary_protocol": summary_protocol, "protocol_unavailable": dict(unavailable),
                                                          "seed_manifest_sha256": seed_manifest["cases_sha256"]}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary["all"], ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
