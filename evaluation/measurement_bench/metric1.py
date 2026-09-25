"""Metric 1 -- extraction accuracy per family with Wilson intervals + error taxonomy.

Inputs: the frozen corpus and OUT_DIR/extract_<OBJ>.json written by run_extraction.py
(real live tagger + real collectors).  For every corpus triple the outcome is one of

  CORRECT          the extractor's value for (object, parameter, stage) equals an accepted truth value
  NOT_TAGGED       the tagger never produced a candidate fragment on any true page for this parameter
                   (sub: doc_not_scanned / doc_no_text_layer / page_beyond_cap / anchor_not_on_page)
  NO_VALUE         a candidate exists and the extractor was run on it, but no value came out
  WRONG_VALUE      a value came out and is wrong (sub: superseded_or_conflicting / other_page / unit_or_count)
  WRONG_ROW        wrong value read from the TRUE page (neighbouring row / column / sub-row)
  CITATION_NUMBER  the number read sits inside a norm/document citation (СП 54.13330.2016, п. 6.10, ...)
  ORDINAL          the number read is a row / section ordinal (first cell of a row, '2.10', '№ п/п')
  GATED            the value was blocked by a gate before/after the page was read (sub: stage_not_admitted,
                   page_cap = tagged but outside the 15-page render window, selection_or_semantic_filter =
                   the extractor reads the true page correctly in isolation but the pipeline did not output it)

The class is assigned by deterministic rules (documented in the report) and audited by hand on a sample.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from .common import CORPUS_DIR, OBJECTS, OUT_DIR, REPORT_DIR, catalog_by_code, norm_enum, read_jsonl, utcnow_iso
from .pipeline_env import patch_bytes, require_frozen_corpus  # noqa: F401  (imports also set sys.path for the app)

FAILURE_CLASSES = ("NOT_TAGGED", "NO_VALUE", "WRONG_VALUE", "WRONG_ROW", "CITATION_NUMBER", "ORDINAL", "GATED")


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": [round(lo, 4), round(hi, 4)]}


# ------------------------------------------------------------------ comparison
def _dec(x: Any) -> Decimal | None:
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return None


def value_equal(family: str, obs: dict, accepted: list) -> bool:
    if family == "NUMERIC_TABLE":
        d = _dec(obs.get("norm"))
        return d is not None and any(_dec(a) == d for a in accepted if not isinstance(a, list))
    if family == "ENUM":
        c = norm_enum(obs.get("canonical"))
        return any(c == norm_enum(a) for a in accepted if not isinstance(a, list))
    if family == "TABLE_COUNT":
        try:
            return any(int(obs["row_count"]) == int(_dec(a)) for a in accepted if not isinstance(a, list))
        except Exception:  # noqa: BLE001
            return False
    if family == "COMPOUND":
        got = [_dec(x) for x in obs.get("norms", [])]
        for a in accepted:
            if isinstance(a, list) and len(a) == len(got) and all(_dec(x) == g for x, g in zip(a, got)):
                return True
        return False
    return False


def printed_of(family: str, obs: dict) -> Any:
    if family == "NUMERIC_TABLE" or family == "ENUM":
        return obs.get("value")
    if family == "TABLE_COUNT":
        return obs.get("row_count")
    return obs.get("values")


# ------------------------------------------------------------------ isolated extraction (true page only)
_CITE_PREV = {"сп", "снип", "гост", "гост р", "ост", "ту", "фз", "№", "п", "пп", "прил", "приложение", "таблица", "табл", "рис", "рисунок", "ст", "статья", "раздел", "лист", "пункт", "п.п"}
_NORM_NUM_RE = re.compile(r"^\d{2,3}\.\d{4,5}(?:[-–]\d{2,4})?$")
_ORD_RE = re.compile(r"^\d{1,2}(?:[.,]\d{1,2}){0,2}\.?$")      # row / section / list numbers: 1-2 leading digits ("5", "2.10", "13.1")


def _norm_word(w: str) -> str:
    return re.sub(r"[.,;:()]+$", "", str(w).lower().replace("ё", "е")).strip()


def _snapshot(obj_rows: dict[str, dict], file_id: str, page: int) -> dict | None:
    from app.domain.dataset_sources import extract_original_pages

    row = obj_rows.get(file_id)
    if not row:
        return None
    doc = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": row["relative_path"], "pdf_pages": row.get("pdf_pages")}},
                          file_hash=row["sha256"], content_hash=row["sha256"], id=0)
    try:
        return extract_original_pages(doc, [page]).get(page)
    except Exception:  # noqa: BLE001
        return None


def isolated_extract(family: str, code: str, stage: str, snap: dict) -> dict | None:
    """What the family's extractor returns when run on exactly this page (no tagging, no ranking)."""
    from app.domain.anchor_search import derive_title_candidates
    from app.domain.enum_class_vocabularies import resolve_enum_families
    from app.domain.generic_compound_extraction import find_anchor_compound_values
    from app.domain.generic_enum_extraction import find_anchor_enum_value
    from app.domain.generic_matrix_extraction import find_anchor_numeric_value
    from app.domain.generic_table_row_count import find_table_row_count
    from app.domain.matrix_unit_classifier import compound_unit_components

    row = catalog_by_code()[code]
    name = row["parameter_name"]
    try:
        if family == "NUMERIC_TABLE":
            m = find_anchor_numeric_value(snap, name)
            return None if m is None else {"norm": str(m.decimal_value), "value": m.value, "word_start": m.word_start, "word_end": m.word_end}
        if family == "ENUM":
            fams = resolve_enum_families(parameter_name=name, pd_section=row.get("pd_section"))
            if not fams:
                return None
            m = find_anchor_enum_value(snap, name, fams[0])
            return None if m is None else {"canonical": m.canonical_value, "value": m.canonical_value, "word_start": m.word_index, "word_end": m.word_index}
        if family == "COMPOUND":
            comps = compound_unit_components(row.get("unit")) or []
            m = find_anchor_compound_values(snap, name, len(comps))
            return None if m is None else {"norms": [str(x) for x in m.normalized_values], "values": m.raw_values, "word_start": m.word_start, "word_end": m.word_end}
        hint = row.get({"PD": "source_pd", "RD": "source_rd", "ID": "source_id"}[stage]) or ""
        m = find_table_row_count(snap, derive_title_candidates(hint))
        return None if (m is None or not m.table_found) else {"row_count": m.row_count, "word_start": m.anchor_word_index, "word_end": m.anchor_word_index}
    except Exception:  # noqa: BLE001
        return None


def token_kind(snap: dict | None, obs: dict) -> str | None:
    """CITATION_NUMBER / ORDINAL / None from the geometry+words around the extracted number."""
    if snap is None or not obs.get("bbox_pdf"):
        return None
    from app.domain.anchor_search import cluster_rows

    words = snap.get("words") or []
    x0, y0, x1, y1 = obs["bbox_pdf"]
    hit = [i for i, w in enumerate(words) if w.get("bbox") and w["bbox"][0] >= x0 - 1 and w["bbox"][2] <= x1 + 1 and w["bbox"][1] >= y0 - 1 and w["bbox"][3] <= y1 + 1]
    if not hit:
        return None
    i = hit[0]
    text = str(words[i].get("text") or "").strip()
    prev = [_norm_word(w.get("text")) for w in words[max(0, i - 3):i]]
    nxt = [_norm_word(w.get("text")) for w in words[i + 1:i + 3]]
    if _NORM_NUM_RE.match(text) or any(p in _CITE_PREV for p in prev[-2:]) or (nxt and re.match(r"^\d{4}$", nxt[0]) and text.endswith("-")):
        return "CITATION_NUMBER"
    for row in cluster_rows(words):
        if any(w is words[i] for w in row):
            if row[0] is words[i] and _ORD_RE.match(text) and len(row) >= 2:
                return "ORDINAL"
            break
    if prev and prev[-1] in {"№", "п/п", "поз"} and _ORD_RE.match(text):
        return "ORDINAL"
    return None


# ------------------------------------------------------------------ classification
def classify(t: dict, ext: dict, docs: dict[str, dict], frags_by_code: dict[str, list], obj_rows: dict[str, dict], snap_cache: dict) -> dict:
    fam, code, stage = t["family"], t["param_code"], t["stage"]
    true_locs = [(l["file_id"], l["page"]) for l in t["locations"]]
    true_set = set(true_locs)
    obs = (ext["observations"].get(code) or {}).get(stage)
    out: dict[str, Any] = {"triple_id": t["triple_id"], "true_norm": t["true_norm"], "accepted_norm": t["accepted_norm"]}

    def snap_of(loc):
        if loc not in snap_cache:
            snap_cache[loc] = _snapshot(obj_rows, *loc)
        return snap_cache[loc]

    # isolated extraction on each true page (diagnostic; also decides GATED vs NO_VALUE)
    iso = {}
    for loc in true_locs:
        s = snap_of(loc)
        if s is not None:
            r = isolated_extract(fam, code, stage, s)
            if r is not None:
                iso[loc] = r
    iso_correct = [loc for loc, r in iso.items() if value_equal(fam, r, t["accepted_norm"])]
    out["isolated_on_true_page"] = {f"{k[0]}:{k[1]}": {kk: vv for kk, vv in v.items() if kk in ("norm", "value", "canonical", "norms", "values", "row_count")} for k, v in iso.items()}
    out["isolated_correct"] = bool(iso_correct)

    truth_doc = docs.get(t["file_id"], {})
    if truth_doc.get("stage") not in ("PD", "RD", "ID"):
        out.update(status="GATED", sub="stage_not_admitted", note=f"true document stage={truth_doc.get('stage')}")
        return out

    if obs is not None:
        out["pipeline"] = {"file_id": obs["file_id"], "page": obs["page"], "value": printed_of(fam, obs), "extractor": obs["extractor"], "context": obs.get("context")}
        page_ok = (obs["file_id"], obs["page"]) in true_set
        out["page_ok"] = page_ok
        if value_equal(fam, obs, t["accepted_norm"]):
            out.update(status="CORRECT", sub="page_verified" if page_ok else "value_only_page_unverified")
            return out
        # wrong value: classify
        sup_vals = [s["value_norm"] for s in t["superseded_or_conflicting"]]
        kind = None
        if fam in ("NUMERIC_TABLE", "COMPOUND"):
            snap = snap_of((obs["file_id"], obs["page"])) if (obs["file_id"], obs["page"]) in true_set else _snapshot(obj_rows, obs["file_id"], obs["page"])
            kind = token_kind(snap, obs)
        if kind:
            out.update(status=kind, sub="same_page" if page_ok else "other_page")
        elif fam == "NUMERIC_TABLE" and any(_dec(obs.get("norm")) == _dec(sv) for sv in sup_vals if sv is not None and not isinstance(sv, list)):
            out.update(status="WRONG_VALUE", sub="superseded_or_conflicting_place")
        elif fam == "ENUM" and any(norm_enum(obs.get("canonical")) == norm_enum(sv) for sv in sup_vals if sv is not None and not isinstance(sv, list)):
            out.update(status="WRONG_VALUE", sub="superseded_or_conflicting_place")
        elif page_ok and fam in ("NUMERIC_TABLE", "COMPOUND"):
            out.update(status="WRONG_ROW", sub="same_page_other_row_or_column")
        elif fam == "TABLE_COUNT":
            out.update(status="WRONG_VALUE", sub="row_count_is_not_the_stated_quantity")
        else:
            out.update(status="WRONG_VALUE", sub="same_page_other_class" if page_ok else "other_page")
        if out.get("isolated_correct"):
            out["selection_error"] = True            # the extractor reads the true page correctly; the pipeline picked another candidate
        return out

    # no observation
    frs = frags_by_code.get(code, [])
    tagged_true = [f for f in frs if (f["file_id"], f["page"]) in true_set]
    if not tagged_true:
        marker = truth_doc.get("marker") or {}
        status = marker.get("status")
        if not marker or status not in ("scanned", "partial"):
            sub = "doc_not_scanned"
        elif status == "partial" and any(l[0] == t["file_id"] and l[1] > int(marker.get("pages") or 0) for l in true_locs if isinstance(marker.get("pages"), int)):
            sub = "page_beyond_cap"
        elif marker.get("no_text_layer") or status == "no_text_layer":
            sub = "doc_no_text_layer"
        else:
            sub = "anchor_not_on_page" if not iso else "anchor_matches_extractor_only"
        out.update(status="NOT_TAGGED", sub=sub, doc_marker=marker)
        return out
    window = {(a, b) for a, b in (ext["rendered_window"].get(code, {}).get(stage) or [])}
    if not any((f["file_id"], f["page"]) in window for f in tagged_true):
        out.update(status="GATED", sub="page_cap_outside_render_window")
        return out
    if out["isolated_correct"]:
        out.update(status="GATED", sub="selection_or_semantic_filter")
    elif iso:
        out.update(status="NO_VALUE", sub="dropped_wrong_candidate")
    else:
        out.update(status="NO_VALUE", sub="tagged_but_no_value_parsed")
    return out


def run() -> dict:
    manifest = require_frozen_corpus()
    corpus = read_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl")
    results: list[dict] = []
    by_obj: dict[str, list[dict]] = defaultdict(list)
    for t in corpus:
        by_obj[t["obj"]].append(t)
    from .reader import manifest_rows

    for obj, triples in by_obj.items():
        path = OUT_DIR / f"extract_{obj}.json"
        if not path.exists():
            for t in triples:
                results.append({**t, "status": "NOT_RUN", "sub": "extraction_output_missing"})
            continue
        ext = json.loads(path.read_text(encoding="utf-8"))
        assert ext["corpus_sha256"] == manifest["corpus_sha256"], f"{obj}: extraction was run against another corpus"
        patch_bytes(obj)
        docs = {d["file_id"]: d for d in ext["documents"]}
        frags_by_code: dict[str, list] = defaultdict(list)
        for f in ext["fragments"]:
            frags_by_code[f["code"]].append(f)
        obj_rows = {r["file_id"]: r for r in manifest_rows(obj)}
        snap_cache: dict = {}
        for t in triples:
            r = classify(t, ext, docs, frags_by_code, obj_rows, snap_cache)
            results.append({**{k: t[k] for k in ("triple_id", "obj", "split", "cleanliness", "param_code", "family", "stage", "critical", "provenance", "truth_kind", "confidence", "selected_by_system_output")}, **r})
    return {"results": results, "manifest": manifest}


def summarize(results: list[dict]) -> dict:
    def block(rows: list[dict]) -> dict:
        n = len(rows)
        correct = sum(r["status"] == "CORRECT" for r in rows)
        strict = sum(r["status"] == "CORRECT" and r.get("sub") == "page_verified" for r in rows)
        tax = Counter(r["status"] for r in rows if r["status"] != "CORRECT")
        subs = Counter(f"{r['status']}/{r.get('sub')}" for r in rows if r["status"] != "CORRECT")
        return {"n": n, "value_correct": rate(correct, n), "value_and_page_correct": rate(strict, n), "taxonomy": dict(tax), "taxonomy_sub": dict(subs),
                "isolated_reads_true_page_correctly": rate(sum(bool(r.get("isolated_correct")) for r in rows), n)}

    out: dict[str, Any] = {}
    for split in ("holdout", "dev", "all"):
        rows = [r for r in results if split == "all" or r["split"] == split]
        out[split] = {"all_families": block(rows), **{fam: block([r for r in rows if r["family"] == fam]) for fam in ("ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT")}}
    clean = [r for r in results if r["split"] == "holdout" and r["cleanliness"] == "CLEAN"]
    out["holdout_clean_only"] = {"all_families": block(clean), **{fam: block([r for r in clean if r["family"] == fam]) for fam in ("ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT")}}
    indep = [r for r in results if r["split"] == "holdout" and not r["selected_by_system_output"]]
    out["holdout_system_independent_selection"] = {"all_families": block(indep)}
    out["by_object"] = {o: block([r for r in results if r["obj"] == o]) for o in sorted({r["obj"] for r in results})}
    out["by_stage_holdout"] = {s: block([r for r in results if r["split"] == "holdout" and r["stage"] == s]) for s in ("PD", "RD", "ID")}
    out["by_confidence_holdout"] = {c: block([r for r in results if r["split"] == "holdout" and r["confidence"] == c]) for c in ("HIGH", "MEDIUM")}
    out["by_truth_kind_holdout"] = {k: block([r for r in results if r["split"] == "holdout" and r["truth_kind"] == k]) for k in ("SINGLE", "SET_ANY")}
    out["selection_errors_holdout"] = sum(bool(r.get("selection_error")) for r in results if r["split"] == "holdout")
    return out


def main() -> int:
    data = run()
    summary = summarize(data["results"])
    payload = {"metric": "M1_extraction_accuracy_by_family", "generated_at": utcnow_iso(), "corpus_sha256": data["manifest"]["corpus_sha256"],
               "corpus_frozen_at": data["manifest"]["frozen_at"], "summary": summary}
    (OUT_DIR / "metric1_results.json").write_text(json.dumps({"results": data["results"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "metric1_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    h = summary["holdout"]
    for fam in ("all_families", "ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT"):
        b = h[fam]
        print(fam, b["n"], "value_correct", b["value_correct"]["rate"], b["value_correct"]["wilson95"], b["taxonomy"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
