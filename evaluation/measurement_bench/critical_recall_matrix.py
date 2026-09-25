"""Critical-recall matrix over the 106 critical catalog codes (regenerable).

  python -m evaluation.measurement_bench.critical_recall_matrix

Writes evaluation/reports/critical_recall_matrix.json and .md from the bench outputs (OUT_DIR):
metric1_results.json, l1_results.json (+ l2_results.json), metric3_results.json.  Nothing here is hand-typed except the
prior-weight sources below.

Per code
  mechanism      catalog extractor (matrix_coverage.json): rule pack / numeric / enum / compound / table-count / none
  extraction     corpus triples for this code: k correct / n (Metric 1)
  seeded pairs   distinct (object, code) pairs where the corpus states the same value on PD and RD AND the extractor found
                 both correctly; each pair spawns several mutations.  Recall is reported per variant AND per pair (a pair
                 counts as caught only if every must-catch variant of it is caught -- the pair is the independent unit, so
                 the Wilson interval is computed on pairs, not on correlated variants)
  status         NO_MECHANISM   no extractor at all
                 UNTESTED       a mechanism exists but the bench has zero seeded pairs for this code
                 VERIFIED       pair-level Wilson lower bound >= 0.80 AND recall >= 0.95
                 WEAK           tested, not VERIFIED
  weight         PRIOR by type (not calibrated on hidden data): 1 + rows of the public pilot gold + mapped annotations.jsonl
                 findings + demonstration violations of "ПРИЛОЖЕНИЕ 2. ПРИМЕР ПРОТОКОЛА СРАВНЕНИЯ.docx"; normalised to 1
  priority       weight x (1 - e2e_lb) with e2e_lb = Wilson-lower(extraction accuracy) x Wilson-lower(seed recall);
                 unknown (no data) counts as 0 -> maximal priority; NO_MECHANISM has e2e = 0
Reading rule: end-to-end recall = P(pair found) x P(change seen | pair found). Seeding measures the second factor; the first is
Metric 1 (extraction accuracy) -- the two must be multiplied, never read separately.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from typing import Any

from .common import MATRIX_COVERAGE_JSON, OUT_DIR, REPORT_DIR, catalog_rows, is_critical, utcnow_iso
from .metric1 import rate, wilson

MECH_FAMILY = {
    "GENERIC_ANCHOR_TABLE": "NUMERIC_TABLE", "GENERIC_ANCHOR_ENUM": "ENUM", "GENERIC_ANCHOR_COMPOUND": "COMPOUND",
    "GENERIC_TABLE_ROW_COUNT": "TABLE_COUNT", "OFFICIAL_RULE_PACK": "RULE_PACK", "NONE": "NONE",
}
# --- prior weight sources --------------------------------------------------------------------------------------------
# (a) public pilot gold, critical VIOLATION_PRESENT rows (learning_data/.../public_gold_checks.jsonl) + the one hidden pilot
#     point named in the brief (SPZU-027).  Counted by code:
PILOT_CRITICAL_POINTS = {"IOS4-078": 5, "IOS4-079": 1, "SPZU-027": 1}
# (b) annotations.jsonl findings mapped to catalog codes by the nature of the finding (low confidence, documented):
ANNOTATION_MAP = {
    "ALT79B-V01": ["PZ-003"], "POL16-V01": ["PZ-003"], "DOO25-V01": ["PZ-003"], "SOSH25-V01": ["PZ-003"],   # areas/expositions: not critical -> weight 0 in this matrix
    "LOS3A-V01": ["AR-041"],                       # door moved (evacuation door position/width)
    "OKT103-V01": ["KR-054"],                      # geometric tolerances of an executive scheme (axes/dimensions)
    "IZM12-V01": ["KR-058"],                       # pile repair scheme (foundation)
    "UNDMS-V01": ["AR-044"],                       # roof pie composition (armoured sheet removed from the covering)
}
# (c) the 12 demonstration violations of ПРИЛОЖЕНИЕ 2 (codes as printed; 'AR-14' is a typo in the document -> AR-040):
ANNEX2_DEMO = ["KR-055", "AR-041", "AR-040", "SPZU-030", "IOS1-069", "PPM-103", "SPZU-025", "KR-067", "POS-082", "POD-093", "ODI-122", "ZU-129"]


def prior_weights(codes: list[str]) -> dict[str, dict[str, Any]]:
    src: dict[str, Counter] = {c: Counter() for c in codes}
    for c, n in PILOT_CRITICAL_POINTS.items():
        if c in src:
            src[c]["pilot"] += n
    for fid, cs in ANNOTATION_MAP.items():
        for c in cs:
            if c in src:
                src[c]["annotations"] += 1
    for c in ANNEX2_DEMO:
        if c in src:
            src[c]["annex2"] += 1
    raw = {c: 1 + sum(src[c].values()) for c in codes}
    total = sum(raw.values())
    return {c: {"weight": round(raw[c] / total, 5), "sources": dict(src[c])} for c in codes}


def _pair_stats(l1_rows: list[dict], layer: str) -> dict[str, dict[str, Any]]:
    """per code: pairs, variants, recall, fp for one decision layer ('obs' or 'protocol')."""
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    # one row per (object, code, mutation): the consistent RD-wide edit (ALL_COPIES) when it exists, else the single-page edit
    chosen: dict[tuple, dict] = {}
    for r in l1_rows:
        key = (r["obj"], r["code"], r["mutation"], r.get("base_kind"))
        if key not in chosen or (r.get("variant") == "ALL_COPIES" and chosen[key].get("variant") != "ALL_COPIES"):
            chosen[key] = r
    for r in chosen.values():
        if r.get("label") == "NOT_APPLICABLE" or r.get("base_kind", "TRUTH_PAIR") != "TRUTH_PAIR":
            continue                       # statuses rest on corpus-confirmed pairs only (SYSTEM_PAIR is supplementary)
        if layer == "protocol":
            p = r.get("protocol")
            if not p or not p.get("available"):
                continue
            row = {**r, "label": p["label"], "evidence_ok": p.get("evidence_ok"), "location_ok": p.get("location_ok")}
        else:
            row = r
        by_pair[(r["obj"], r["code"])].append(row)
    per_code: dict[str, dict] = defaultdict(lambda: {"pairs": 0, "pairs_caught": 0, "variants_must": 0, "variants_caught": 0, "variants_caught_full": 0, "must_not": 0, "fp": 0})
    for (obj, code), rows in by_pair.items():
        must = [x for x in rows if x["must_catch"]]
        mustnot = [x for x in rows if not x["must_catch"]]
        d = per_code[code]
        if must:
            d["pairs"] += 1
            caught = [x for x in must if x["label"] == "VIOLATION_PRESENT"]
            full = [x for x in caught if x.get("evidence_ok") and x.get("location_ok")]
            d["variants_must"] += len(must)
            d["variants_caught"] += len(caught)
            d["variants_caught_full"] += len(full)
            d["pairs_caught"] += int(len(caught) == len(must))
        d["must_not"] += len(mustnot)
        d["fp"] += sum(x["label"] == "VIOLATION_PRESENT" for x in mustnot)
    return per_code


def status_from_pairs(fam: str, pairs: int, caught: int) -> str:
    """VERIFIED / WEAK / UNTESTED / NO_MECHANISM by the brief's rule (pair-level Wilson lower bound >= 0.80 at recall >= 0.95)."""
    if fam == "NONE":
        return "NO_MECHANISM"
    if fam == "RULE_PACK" or pairs == 0:
        return "UNTESTED"
    lo, _hi = wilson(caught, pairs)
    return "VERIFIED" if (lo >= 0.80 and caught / pairs >= 0.95) else "WEAK"


def build() -> dict:
    cov = json.loads(MATRIX_COVERAGE_JSON.read_text(encoding="utf-8"))
    mech = {p["code"]: p for p in cov["parameters"]}
    crit = [r for r in catalog_rows() if is_critical(r)]
    codes = [r["parameter_code"] for r in crit]
    weights = prior_weights(codes)
    m1 = json.loads((OUT_DIR / "metric1_results.json").read_text(encoding="utf-8"))["results"] if (OUT_DIR / "metric1_results.json").exists() else []
    l1 = json.loads((OUT_DIR / "l1_results.json").read_text(encoding="utf-8")) if (OUT_DIR / "l1_results.json").exists() else None
    l2 = json.loads((OUT_DIR / "l2_results.json").read_text(encoding="utf-8")) if (OUT_DIR / "l2_results.json").exists() else None
    obs_stats = _pair_stats(l1["rows"], "obs") if l1 else {}
    proto_stats = _pair_stats(l1["rows"], "protocol") if l1 else {}
    ext_by_code: dict[str, list[dict]] = defaultdict(list)
    for r in m1:
        ext_by_code[r["param_code"]].append(r)
    fam_hold = defaultdict(lambda: [0, 0])
    for r in m1:
        if r["split"] == "holdout" and r["status"] != "NOT_RUN":
            fam_hold[r["family"]][1] += 1
            fam_hold[r["family"]][0] += int(r["status"] == "CORRECT")
    fam_ext_lb = {f: wilson(k, n)[0] for f, (k, n) in fam_hold.items()}

    rows = []
    for r in crit:
        code = r["parameter_code"]
        mrow = mech[code]
        fam = MECH_FAMILY[mrow["extractor"]]
        e = ext_by_code.get(code, [])
        e_k, e_n = sum(x["status"] == "CORRECT" for x in e), len(e)
        use_proto = code in proto_stats and proto_stats[code]["pairs"] > 0
        st = (proto_stats if use_proto else obs_stats).get(code)
        layer = "protocol" if use_proto else "extractor+legacy"
        # rule packs are not seeded by this stand (their pilot numbers are cited, not re-measured) -> UNTESTED here
        status = status_from_pairs(fam, st["pairs"] if st else 0, st["pairs_caught"] if st else 0)
        # extraction lower bound: code-level if data else family-level
        ext_lb = wilson(e_k, e_n)[0] if e_n >= 3 else fam_ext_lb.get(fam, 0.0)
        rec_lb = wilson(st["pairs_caught"], st["pairs"])[0] if st and st["pairs"] else 0.0
        e2e_lb = 0.0 if fam in ("NONE",) else ext_lb * rec_lb
        w = weights[code]["weight"]
        row = {
            "code": code, "name": r["parameter_name"], "unit": r.get("unit"), "section": r.get("pd_section"), "mechanism": mrow["extractor"], "family": fam,
            "extraction": rate(e_k, e_n) if e_n else None,
            "seeded": None if not st else {
                "layer": layer, "pairs": st["pairs"], "pairs_caught": st["pairs_caught"], "pair_recall": rate(st["pairs_caught"], st["pairs"]),
                "variants_must_catch": st["variants_must"], "variants_caught": st["variants_caught"], "variant_recall": rate(st["variants_caught"], st["variants_must"]),
                "variants_caught_with_evidence_and_location": st["variants_caught_full"],
                "fp_on_must_not_catch": rate(st["fp"], st["must_not"]) if st["must_not"] else None,
            },
            "seeded_other_layer": None,
            "status": status, "weight": w, "weight_sources": weights[code]["sources"],
            "e2e_recall_lower_bound": round(e2e_lb, 4), "priority": round(w * (1 - e2e_lb), 6),
            "basis": {"extraction": "code" if e_n >= 3 else "family_holdout", "recall": "pairs" if st else "none"},
        }
        other = (obs_stats if use_proto else proto_stats).get(code)
        if other and other["pairs"]:
            row["seeded_other_layer"] = {"layer": "extractor+legacy" if use_proto else "protocol", "pairs": other["pairs"], "pair_recall": rate(other["pairs_caught"], other["pairs"]),
                                         "fp_on_must_not_catch": rate(other["fp"], other["must_not"]) if other["must_not"] else None}
        rows.append(row)
    rule_pack_evidence: dict[str, dict[str, int]] = {}
    runs_path = OUT_DIR / "metric3_runs_full.json"
    if runs_path.exists():
        runs = json.loads(runs_path.read_text(encoding="utf-8"))
        rp_codes = {x["code"] for x in rows if x["mechanism"] == "OFFICIAL_RULE_PACK"}
        for code in sorted(rp_codes):
            c: Counter = Counter()
            for obj, run in runs.items():
                for g in run.get("groups", []):
                    if g["parameter_code"] == code:
                        c[f"{g['finding_status']}/{(g.get('delta') or {}).get('reason')}"] += 1
            rule_pack_evidence[code] = dict(c)
    by_family: dict[str, dict[str, Any]] = {}
    for fam in sorted({x["family"] for x in rows}):
        rr = [x for x in rows if x["family"] == fam]
        pairs = sum((x["seeded"] or {}).get("pairs", 0) for x in rr)
        caught = sum((x["seeded"] or {}).get("pairs_caught", 0) for x in rr)
        h = fam_hold.get(fam)
        by_family[fam] = {"codes": len(rr), "weight_mass": round(sum(x["weight"] for x in rr), 4),
                          "holdout_extraction": rate(h[0], h[1]) if h and h[1] else None,
                          "seeded_pairs": pairs, "seeded_pairs_caught": caught, "seeded_pair_recall": rate(caught, pairs) if pairs else None,
                          "status": dict(Counter(x["status"] for x in rr))}
    summary = {
        "by_family": by_family,
        "n_codes": len(rows), "by_status": dict(Counter(x["status"] for x in rows)), "by_mechanism": dict(Counter(x["mechanism"] for x in rows)),
        "verified": [x["code"] for x in rows if x["status"] == "VERIFIED"],
        "weight_mass_by_status": {s: round(sum(x["weight"] for x in rows if x["status"] == s), 4) for s in ("VERIFIED", "WEAK", "UNTESTED", "NO_MECHANISM")},
        "L2": None if not l2 else {"attempted": l2["summary"]["attempted"], "verified_edits": l2["summary"]["verified_edits"], "declared_fragile": l2["summary"]["L2_declared_fragile"]},
    }
    # order: priority, then the number of independent prior sources (pilot / annotations / annex2), then code (deterministic)
    ordered = sorted(rows, key=lambda x: (-x["priority"], -len(x["weight_sources"]), x["code"]))
    top10 = ordered[:10]
    cutoff = top10[-1]["priority"]
    tied = [x["code"] for x in ordered if x["priority"] == cutoff]
    return {"generated_at": utcnow_iso(), "summary": summary, "top10_by_weight_x_worst_recall": [
        {k: x[k] for k in ("code", "name", "status", "mechanism", "weight", "e2e_recall_lower_bound", "priority")} for x in top10],
        "top10_tie_at_cutoff": {"priority": cutoff, "n_codes_tied": len(tied), "codes": tied,
                                "note": "no tested pair exists for these codes, so recall is unknown (counted as 0) and the order between them is decided only by prior weight, then by number of prior sources, then by code"},
        "rule_pack_evidence_on_new_objects": rule_pack_evidence,
        "rows": rows}


def render_md(d: dict) -> str:
    s = d["summary"]
    L = ["# Матрица критического recall (106 критических кодов)", "", f"Сгенерировано {d['generated_at']} командой `python -m evaluation.measurement_bench.critical_recall_matrix`.",
         "", "Правило чтения: итоговый recall = P(нашли пару) × P(увидели изменение | нашли пару). Посев измеряет второй множитель; первый — метрика 1 (точность извлечения). "
         "Статус VERIFIED: нижняя граница Уилсона по парам ≥ 0,80 при recall ≥ 0,95.", "",
         f"**Статусы:** {s['by_status']}. **VERIFIED:** {len(s['verified'])} из {s['n_codes']}. Масса априорного веса по статусам: {s['weight_mass_by_status']}.", "",
         "## Топ-10 по (вес × худший recall)", "", "| # | код | параметр | статус | механизм | вес | e2e recall (нижняя гр.) | приоритет |", "|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(d["top10_by_weight_x_worst_recall"], 1):
        L.append(f"| {i} | {x['code']} | {x['name']} | {x['status']} | {x['mechanism']} | {x['weight']:.4f} | {x['e2e_recall_lower_bound']:.3f} | {x['priority']:.5f} |")
    L += ["", "## По семействам механизмов", "", "Извлечение — holdout метрики 1 по семейству (все коды семейства, не только критические); посев — пары, подтверждённые корпусом (TRUTH_PAIR), критические коды.", "",
          "| семейство | критических кодов | масса веса | извлечение (holdout) | посев: пар / поймано | статусы |", "|---|---|---|---|---|---|"]
    for fam, b in s["by_family"].items():
        e = b["holdout_extraction"]
        ext = f"{e['k']}/{e['n']} = {100 * e['rate']:.0f}% [{100 * e['wilson95'][0]:.0f}; {100 * e['wilson95'][1]:.0f}]" if e else "—"
        L.append(f"| {fam} | {b['codes']} | {b['weight_mass']:.3f} | {ext} | {b['seeded_pairs']} / {b['seeded_pairs_caught']} | {b['status']} |")
    tie = d.get("top10_tie_at_cutoff")
    if tie and tie["n_codes_tied"] > 1:
        L += ["", f"Замечание о ничьих: на границе десятки (приоритет {tie['priority']:.5f}) равны {tie['n_codes_tied']} кодов: {', '.join(tie['codes'])}. "
                  "Для этих кодов нет ни одной проверенной пары, recall неизвестен (считается нулём), порядок между ними задан только априорным весом, затем числом независимых источников веса, затем кодом."]
    rpe = d.get("rule_pack_evidence_on_new_objects")
    if rpe:
        L += ["", "### Пилотные rule-pack коды: почему UNTESTED", "",
              "Пять кодов с механизмом OFFICIAL_RULE_PACK срабатывают только на объектах с разметкой организатора (публичные Новослободская/Тюменская и закрытый тест). "
              "На шести новых объектах стенда живого тегировщика для них нет: итог проверки этих кодов в прогоне метрики 3 (статус/причина → число объектов):", "",
              "| код | итоги на шести объектах |", "|---|---|"]
        for code, c in rpe.items():
            L.append(f"| {code} | " + "; ".join(f"{k} × {v}" for k, v in c.items()) + " |")
        L += ["", "Посев на этих кодах требует отдельного стенда на размеченных публичных объектах; в этой итерации он не выполнен (статус UNTESTED, не WEAK)."]
    L += ["", "## Все 106 кодов", "", "| код | параметр | механизм | извл. k/n | посев: пар | пар пойм. | recall пар [Уилсон] | recall вариантов | FP на «не должно ловиться» | статус | вес |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in sorted(d["rows"], key=lambda r: (-r["weight"], r["code"])):
        e = x["extraction"]
        sd = x["seeded"]
        ext = f"{e['k']}/{e['n']}" if e else "—"
        if sd:
            pr = sd["pair_recall"]
            fp = sd["fp_on_must_not_catch"]
            L.append(f"| {x['code']} | {x['name']} | {x['mechanism'].replace('GENERIC_', '').replace('OFFICIAL_', '')} | {ext} | {sd['pairs']} | {sd['pairs_caught']} | {pr['rate']:.2f} [{pr['wilson95'][0]:.2f}; {pr['wilson95'][1]:.2f}] | {sd['variants_caught']}/{sd['variants_must_catch']} | "
                     f"{(str(fp['k']) + '/' + str(fp['n'])) if fp else '—'} | {x['status']} | {x['weight']:.4f} |")
        else:
            L.append(f"| {x['code']} | {x['name']} | {x['mechanism'].replace('GENERIC_', '').replace('OFFICIAL_', '')} | {ext} | 0 | — | — | — | — | {x['status']} | {x['weight']:.4f} |")
    L += ["", "Вес — априорный по типам (не калиброван на скрытых данных): 1 + строки публичного пилота + сопоставленные находки annotations.jsonl + демонстрационные нарушения Приложения 2.", ""]
    return "\n".join(L)


def main() -> int:
    d = build()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "critical_recall_matrix.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORT_DIR / "critical_recall_matrix.md").write_text(render_md(d), encoding="utf-8")
    print(json.dumps(d["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
