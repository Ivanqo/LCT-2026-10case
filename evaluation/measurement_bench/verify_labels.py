"""Re-check every cited (file, page) of the corpus against the ORIGINAL-PDF text
cache: the page must exist, contain the printed value token, and (for the
primary location) contain most words of the recorded line.  A typo in a label
would otherwise silently become "ground truth".

  python -m evaluation.measurement_bench.verify_labels
"""
from __future__ import annotations

import re
import sys

from .common import CORPUS_DIR, read_jsonl
from .reader import load_pages

_LAT = str.maketrans({"С": "C", "О": "O", "В": "B", "А": "A", "Е": "E", "Н": "H", "К": "K", "М": "M", "Р": "P", "Т": "T", "Х": "X",
                      "с": "c", "о": "o", "в": "b", "а": "a", "е": "e", "н": "h", "к": "k", "м": "m", "р": "p", "т": "t", "х": "x"})


def _flat(s: str) -> str:
    return re.sub(r"[\s ]+", "", s).replace(",", ".").lower().translate(_LAT).replace("ё", "е")


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[а-яa-z0-9]+", s.lower().replace("ё", "е")) if len(w) >= 3]


_cache: dict[str, list[str]] = {}


def page_text(file_id: str, page: int) -> str | None:
    if file_id not in _cache:
        try:
            _cache[file_id] = load_pages(file_id)
        except FileNotFoundError:
            return None
    pages = _cache[file_id]
    return pages[page - 1] if 1 <= page <= len(pages) else None


def check_value(text: str, printed) -> bool:
    if isinstance(printed, list):
        return all(check_value(text, p) for p in printed)
    flat = _flat(text)
    p = _flat(str(printed))
    if p in flat:
        return True
    if "." in p:  # "37076.0" vs page "37076,0"/"37 076" etc.
        base = p.rstrip("0").rstrip(".")
        return base in flat
    return False


def main() -> int:
    rows = read_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl")
    bad: list[str] = []
    n_loc = 0
    for r in rows:
        for loc in r["locations"]:
            n_loc += 1
            t = page_text(loc["file_id"], loc["page"])
            tag = f"{r['triple_id']} @{loc['file_id']} p{loc['page']} ({loc['role']})"
            if t is None:
                bad.append(f"NO_PAGE {tag}")
                continue
            if not check_value(t, loc["value_printed"]):
                bad.append(f"VALUE_NOT_ON_PAGE {tag}: {loc['value_printed']!r}")
        t = page_text(r["file_id"], r["page"])
        if t is not None:
            ws = _words(r["line_text"])
            if ws:
                low = t.lower().replace("ё", "е")
                hit = sum(1 for w in ws if w in low)
                if hit / len(ws) < 0.6:
                    bad.append(f"LINE_MISMATCH {r['triple_id']}: {hit}/{len(ws)} words of {r['line_text'][:60]!r}")
        for s in r["superseded_or_conflicting"]:
            n_loc += 1
            t2 = page_text(s["file_id"], s["page"])
            if t2 is None:
                bad.append(f"NO_PAGE(sup) {r['triple_id']} {s['file_id']} p{s['page']}")
            elif not check_value(t2, s["value"]):
                bad.append(f"SUP_VALUE_NOT_ON_PAGE {r['triple_id']} {s['file_id']} p{s['page']}: {s['value']!r}")
    print(f"checked {len(rows)} triples / {n_loc} locations; problems: {len(bad)}")
    out = CORPUS_DIR / "verify_report.txt"
    out.write_text("\n".join(bad), encoding="utf-8")
    for b in bad[:80]:
        print(b.encode("utf-8", "replace").decode("utf-8", "replace"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
