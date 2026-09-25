"""`location` conventions of the organiser's gold + the normalisation used for matching.

Evidence base = ONLY `public_gold_checks.jsonl` (15 rows, 2 public objects; the closed
`all_gold_checks.jsonl` is never read here).  What the 15 rows show:

  location_type ROOM                 location = the room number as a string, zero-padding kept ("012", "140", "267")
  location_type CONSTRUCTION_ELEMENT location = a descriptive element / level name in Russian
                                     ("Фундаментная плита", "Стена в грунте", "Вертикальные конструкции подземной части",
                                      "Отметка 0.000")
  location_type SITE                 NOT present in the 15 public rows.  It is stated by the task brief / backlog and by the
                                     closed-gold example cited in code comments (SPZU-027 -> "SITE"); it is used here as the
                                     convention for object-level (scalar) parameters and flagged `evidence: brief_only`.

Consequences measured by the bench (see report): the four generic mechanisms emit the constant "SITE" for every
group, so a check for an element- or room-level parameter can never match the organiser's (parameter_code, location)
key even when the value and the pages are right.

Normalisation (`normalize`): NFC, casefold, ё->е, NBSP/space runs collapsed, surrounding quotes/dots/commas stripped.
Match levels used by the bench:
  STRICT  normalize(a) == normalize(b)
  LOOSE   STRICT, or both are pure digits and equal after stripping leading zeros ("012" ~ "12"), or one normalised
          element name contains the other ("фундаментная плита" ~ "плита")
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .common import PUBLIC_GOLD, read_jsonl

SITE = "SITE"
# Parameters whose gold rows are per element / per room in the PUBLIC gold.
ELEMENT_LEVEL_CODES = {"KR-055", "KR-058", "PZ-009"}
ROOM_LEVEL_CODES = {"IOS4-078", "IOS4-079", "FREE-HEATING-001"}


def normalize(text: Any) -> str:
    s = unicodedata.normalize("NFC", str(text if text is not None else ""))
    s = s.casefold().replace("ё", "е").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" .,;:«»\"'")


def match_level(expected: Any, produced: Any) -> str:
    a, b = normalize(expected), normalize(produced)
    if a == b:
        return "STRICT"
    if a.isdigit() and b.isdigit() and a.lstrip("0") == b.lstrip("0"):
        return "LOOSE"
    if a and b and (a in b or b in a) and not a.isdigit():
        return "LOOSE"
    return "NONE"


def observed_conventions() -> dict[str, Any]:
    rows = read_jsonl(PUBLIC_GOLD)
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["location_type"], []).append({"parameter_code": r["parameter_code"], "location": r["location"], "label": r["violation_label"]})
    return {
        "source": "learning_data/extracted/train_public_203/data/public_gold_checks.jsonl",
        "n_rows": len(rows),
        "location_types_seen": {t: {"n": len(v), "examples": v[:6]} for t, v in by_type.items()},
        "location_types_not_seen_in_public_gold": ["SITE"],
        "room_number_format": "string, zero padding preserved ('012', '140')",
        "element_format": "descriptive Russian name / level ('Фундаментная плита', 'Отметка 0.000')",
        "site_convention_evidence": "brief_only (task statement + code comment about closed gold SPZU-027); NOT verifiable from public gold",
    }


def expected_location_type(code: str) -> str:
    if code in ROOM_LEVEL_CODES:
        return "ROOM"
    if code in ELEMENT_LEVEL_CODES:
        return "CONSTRUCTION_ELEMENT"
    return "SITE"


def location_ok(code: str, produced: Any, expected: Any | None = None) -> tuple[bool, str]:
    """(strict_ok, level).  For SITE-convention parameters the expected string is 'SITE'; for element/room parameters the
    concrete organiser string must be supplied (`expected`) -- without it the check cannot succeed for a mechanism that
    emits the constant 'SITE'."""
    kind = expected_location_type(code)
    if kind == "SITE":
        lvl = match_level(SITE, produced)
        return lvl == "STRICT", lvl
    if expected is None:
        return False, "NEEDS_ELEMENT_OR_ROOM_STRING"
    lvl = match_level(expected, produced)
    return lvl == "STRICT", lvl


def main() -> int:
    import json

    from .common import REPORT_DIR

    out = REPORT_DIR / "measurement_bench"
    out.mkdir(parents=True, exist_ok=True)
    data = {
        "observed": observed_conventions(),
        "normalisation": "NFC, casefold, ё->е, blank runs collapsed, surrounding quotes/dots/commas stripped; match levels STRICT/LOOSE (leading-zero-insensitive room numbers, element-name containment)",
        "expected_location_type_rule": {"ROOM": sorted(ROOM_LEVEL_CODES), "CONSTRUCTION_ELEMENT": sorted(ELEMENT_LEVEL_CODES), "SITE": "every other code (brief-only evidence)"},
        "generic_mechanisms_emit": SITE,
    }
    (out / "location_conventions.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out / "location_conventions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
