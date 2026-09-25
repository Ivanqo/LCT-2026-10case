"""Deterministic labelling plan (written BEFORE any value is read or any
extractor is run): which (object, parameter) pairs get a documented search
attempt, chosen by a seeded shuffle of each family's parameter list -- never by
what the pipeline found.  Every attempt is later logged as FOUND/ABSENT in the
corpus `probe_log`, so the denominator of 'what a reader can find' is public.
"""
from __future__ import annotations

import json
import random

from .common import CORPUS_DIR, OBJECTS, catalog_rows, family_of_code, utcnow_iso

SEED = 20260924
PER_OBJECT = {"ENUM": None, "TABLE_COUNT": None, "NUMERIC_TABLE": 12, "COMPOUND": 6}   # None = every param of the family


def build() -> dict:
    by_family: dict[str, list[str]] = {}
    for row in catalog_rows():
        fam = family_of_code(row["parameter_code"])
        if fam in PER_OBJECT:
            by_family.setdefault(fam, []).append(row["parameter_code"])
    plan: dict = {"seed": SEED, "created_at": utcnow_iso(), "per_object": PER_OBJECT, "family_sizes": {k: len(v) for k, v in by_family.items()}, "attempts": {}}
    for obj in OBJECTS:
        rng = random.Random(f"{SEED}:{obj}")
        entry: dict[str, list[str]] = {}
        for fam, codes in by_family.items():
            order = list(codes)
            rng.shuffle(order)
            n = PER_OBJECT[fam]
            entry[fam] = order if n is None else order[:n]
        plan["attempts"][obj] = entry
    return plan


if __name__ == "__main__":
    plan = build()
    out = CORPUS_DIR / "probe_plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out, plan["family_sizes"], {o: {f: len(v) for f, v in e.items()} for o, e in plan["attempts"].items()})
