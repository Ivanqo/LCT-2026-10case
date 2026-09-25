"""Metric 2, level L2: the same seeded violations as L1 but as REAL PDF edits.

A small seeded sample of the L1 cases is re-created by editing the RD PDF itself with PyMuPDF (redaction of the
original token + `insert_text` of the mutated one), saving the edited copy under MUTATED_PDF_DIR (outside the repo,
regenerable from the seed), and running the extractors on the edited bytes.  Purpose: check that L1 (snapshot-level
edits) is representative of what happens when the text really lives in a PDF (fonts, rotation, redaction leaving
residual glyphs, re-extraction word order).

Stop rule (task brief): if the edits prove fragile -- fewer than FRAGILITY_MIN of the attempted edits verify
(the new token is extractable at the old position AND the old token is gone) -- L2 is abandoned and the report says
"L2 not done", listing why.

  python -m evaluation.measurement_bench.seed_l2 [--n 24]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

from .common import MUTATED_PDF_DIR, OUT_DIR, SEED_DIR, utcnow_iso
from .pipeline_env import object_root, patch_bytes, require_frozen_corpus
from .seed_l1 import SEED, evaluate_pair, load_ctx, summarize

FRAGILITY_MIN = 0.70
FONT_FILE = Path(r"C:\Windows\Fonts\arial.ttf")


def _needs_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def edit_pdf(src: Path, page_no: int, rect_visible: list[float], new_text: str, out: Path) -> dict[str, Any]:
    import fitz

    doc = fitz.open(src)
    page = doc[page_no - 1]
    rot = page.rotation
    r_vis = fitz.Rect(*rect_visible)
    r_raw = r_vis * page.derotation_matrix
    r_raw.normalize()
    pad = fitz.Rect(-0.6, -0.6, 0.6, 0.6)
    page.add_redact_annot(fitz.Rect(r_raw.x0 + pad.x0, r_raw.y0 + pad.y0, r_raw.x1 + pad.x1, r_raw.y1 + pad.y1), fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    h = r_vis.height
    fontsize = max(4.0, min(14.0, h * 0.85))
    base_vis = fitz.Point(r_vis.x0, r_vis.y1 - h * 0.18)
    base_raw = base_vis * page.derotation_matrix
    kwargs: dict[str, Any] = {"fontsize": fontsize, "rotate": rot}
    if _needs_cyrillic(new_text) and FONT_FILE.exists():
        page.insert_font(fontname="benchArial", fontfile=str(FONT_FILE))
        kwargs["fontname"] = "benchArial"
    else:
        kwargs["fontname"] = "helv"
    page.insert_text(base_raw, new_text, **kwargs)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out, garbage=0, deflate=True)
    doc.close()
    # verification: re-extract, look at the visible frame around the old rectangle
    chk = fitz.open(out)
    p2 = chk[page_no - 1]
    mat = p2.rotation_matrix
    inside = []
    for x0, y0, x1, y1, w, *_ in p2.get_text("words"):
        a, b = fitz.Point(x0, y0) * mat, fitz.Point(x1, y1) * mat
        rx0, rx1 = sorted((a.x, b.x))
        ry0, ry1 = sorted((a.y, b.y))
        ov = fitz.Rect(rx0, ry0, rx1, ry1) & r_vis
        if not ov.is_empty and ov.get_area() > 0.25 * fitz.Rect(rx0, ry0, rx1, ry1).get_area():
            inside.append(w)
    chk.close()
    joined = " ".join(inside)
    return {"rotation": rot, "words_in_rect_after_edit": inside, "new_token_present": new_text.replace(" ", "") in joined.replace(" ", ""), "font": kwargs["fontname"]}


def run(n: int) -> int:
    manifest = require_frozen_corpus()
    l1 = json.loads((OUT_DIR / "l1_results.json").read_text(encoding="utf-8"))["rows"]
    pool = [r for r in l1 if r.get("variant") == "SINGLE" and r.get("mutation_applied") not in (None, "identity", "row_deleted") and r["family"] in ("NUMERIC_TABLE", "ENUM") and r.get("label") != "NOT_APPLICABLE"]
    pool.sort(key=lambda r: r["case_id"])
    rng = random.Random(SEED + 2)
    rng.shuffle(pool)
    sample = pool[:n]
    from app.domain import dataset_sources as ds

    from .reader import manifest_rows

    rows: list[dict] = []
    ctxs: dict[str, dict] = {}
    l1_full = json.loads((OUT_DIR / "l1_results.json").read_text(encoding="utf-8"))
    metric1 = json.loads((OUT_DIR / "metric1_results.json").read_text(encoding="utf-8"))["results"]
    ext_cache: dict[str, dict] = {}
    for r in sample:
        obj, code = r["obj"], r["code"]
        if obj not in ext_cache:
            ext_cache[obj] = json.loads((OUT_DIR / f"extract_{obj}.json").read_text(encoding="utf-8"))
        obs_rd = ext_cache[obj]["observations"][code]["RD"]
        obs_pd = ext_cache[obj]["observations"][code]["PD"]
        case = {"obj": obj, "code": code, "family": r["family"], "pd_obs": obs_pd, "rd_obs": obs_rd, "case_id": r["case_id"], "mutation": r["mutation"], "must_catch": r["must_catch"], "split": r["split"]}
        patch_bytes(obj)
        mrows = {m["file_id"]: m for m in manifest_rows(obj)}
        row = mrows[obs_rd["file_id"]]
        src = object_root(obj) / row["relative_path"].replace("/", "\\")
        out_pdf = MUTATED_PDF_DIR / f"{r['case_id'].replace('::', '__')}.pdf"
        rec: dict[str, Any] = {"case_id": r["case_id"], "obj": obj, "code": code, "family": r["family"], "mutation": r["mutation"], "must_catch": r["must_catch"], "split": r["split"], "new_text": r["mutation_applied"]}
        try:
            rec["edit"] = edit_pdf(src, obs_rd["page"], obs_rd["bbox_pdf"], r["mutation_applied"], out_pdf)
        except Exception as exc:  # noqa: BLE001
            rec["edit_error"] = f"{type(exc).__name__}: {exc}"
            rows.append(rec)
            print(r["case_id"], "EDIT_ERROR", rec["edit_error"], flush=True)
            continue
        rec["edit_verified"] = bool(rec["edit"]["new_token_present"])
        if obj not in ctxs:
            ctxs[obj] = load_ctx(obj)
        real_bytes = ds._original_document_bytes
        target = (row["relative_path"].replace("\\", "/"), row["sha256"])

        def wrapper(relative, expected_hash, _t=target, _orig=real_bytes, _pdf=out_pdf):
            if (relative, expected_hash) == _t:
                return _pdf.read_bytes()
            return _orig(relative, expected_hash)

        wrapper.cache_clear = lambda: None  # type: ignore[attr-defined]
        ds._original_document_bytes = wrapper
        ds._original_page_snapshots.cache_clear()
        try:
            res = evaluate_pair(ctxs[obj], case)
        finally:
            ds._original_document_bytes = real_bytes
            ds._original_page_snapshots.cache_clear()
        rec.update({k: v for k, v in res.items() if k != "case_id"})
        rec["l1_label"] = r["label"]
        rec["agrees_with_l1"] = rec.get("label") == r["label"]
        rows.append(rec)
        print(r["case_id"], "verified" if rec["edit_verified"] else "NOT_VERIFIED", rec.get("label"), "L1:", r["label"], flush=True)
        try:
            out_pdf.unlink()
        except OSError:
            pass
    attempted = len(rows)
    verified = sum(1 for x in rows if x.get("edit_verified"))
    fragile = attempted == 0 or verified / attempted < FRAGILITY_MIN
    verified_rows = [x for x in rows if x.get("edit_verified")]
    summary = {
        "attempted": attempted, "verified_edits": verified, "verified_rate": round(verified / attempted, 4) if attempted else None,
        "fragility_threshold": FRAGILITY_MIN, "L2_declared_fragile": fragile,
        "agreement_with_L1_on_verified": (sum(x["agrees_with_l1"] for x in verified_rows), len(verified_rows)),
        "layer_summary": None if fragile else summarize(verified_rows, "extractor+legacy_compare (L2 real PDF edits)"),
        "edit_errors": [x["edit_error"] for x in rows if x.get("edit_error")][:10],
        "unverified": [{"case_id": x["case_id"], "words_after": x.get("edit", {}).get("words_in_rect_after_edit"), "rotation": x.get("edit", {}).get("rotation")} for x in rows if not x.get("edit_verified")][:15],
    }
    (OUT_DIR / "l2_results.json").write_text(json.dumps({"generated_at": utcnow_iso(), "seed": SEED, "rows": rows, "summary": summary}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "layer_summary"}, ensure_ascii=False, default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    return run(ap.parse_args().n)


if __name__ == "__main__":
    sys.exit(main())
