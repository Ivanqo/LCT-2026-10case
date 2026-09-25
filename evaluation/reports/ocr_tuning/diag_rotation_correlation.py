import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "api_service"))
from app.domain.dataset_sources import extract_original_pages, ocr_page_snapshot
from types import SimpleNamespace
import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from evaluation.ocr_fixtures import _read_jsonl, predicted_text_for_line, DEFAULT_GOLD_PATH
from app.domain.dataset_sources import original_document_bytes

def levenshtein(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + cost)
        prev = cur
    return prev[lb]

rows = _read_jsonl(DEFAULT_GOLD_PATH)
tess = [r for r in rows if r.get("source_provenance") == "TESSERACT_RUS_ENG_PREANNOTATION" and r.get("decision") != "CANNOT_VERIFY"]

cache = {}
page_rotation_cache = {}
results = []
for row in tess:
    relative, sha256, page_number = row["source_relative_path"], row["source_sha256"], row["pdf_page_number"]
    key = (relative, sha256, page_number)
    if key not in page_rotation_cache:
        document = SimpleNamespace(dataset_metadata={"document_manifest": {"relative_path": relative}}, file_hash=sha256, content_hash=sha256)
        try:
            data = original_document_bytes(document)
            with fitz.open(stream=data, filetype="pdf") as pdf:
                page = pdf[page_number - 1]
                page_rotation_cache[key] = int(page.rotation) % 360
        except Exception:
            page_rotation_cache[key] = None
    rotation = page_rotation_cache[key]
    predicted = predicted_text_for_line(cache, row)
    gold = row["gold_text"] or ""
    dist = levenshtein(gold, predicted)
    cer = dist / max(1, len(gold)) if gold else (0 if not predicted else 1)
    results.append({"line_id": row["line_id"], "rotation": rotation, "gold_len": len(gold), "cer": cer, "gold": gold, "predicted": predicted})

rotated = [r for r in results if r["rotation"] not in (0, None)]
not_rotated = [r for r in results if r["rotation"] in (0, None)]

def summarize(group, label):
    total_chars = sum(r["gold_len"] for r in group)
    total_errors = sum(r["cer"] * r["gold_len"] for r in group)
    acc = 1 - (total_errors / total_chars) if total_chars else None
    print(f"{label}: n={len(group)} chars={total_chars} char_accuracy={acc}")

summarize(rotated, "ROTATED")
summarize(not_rotated, "NOT_ROTATED")

print("\n--- worst 10 rotated lines ---")
for r in sorted(rotated, key=lambda r: -r["cer"])[:10]:
    print(json.dumps(r, ensure_ascii=False))

out_path = Path(__file__).resolve().parent / "diag_rotation_correlation_output.json"
out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
