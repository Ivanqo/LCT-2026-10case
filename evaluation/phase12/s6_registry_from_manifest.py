"""S6 timing runs: a Perechen ID 1.1 file registry (CSV) for one SILVER new object, built from its manifest.

The new objects (LOS3A, DOO25, ...) came without an organizer registry. For the §11 timing runs of
`python -m app.cli run` the registry carries what the E1 harness imported for the same files -- file_id,
relative path, SHA-256, stage and section from `evaluation/silver_new_objects_pass1/manifests/` -- so the CLI
result is comparable with the in-process harness. It has no approval status/date or revision chain (the manifests
have none): this is an operator-style registry for measuring time, not an organizer registry.

    python evaluation/phase12/s6_registry_from_manifest.py LOS3A out/registry_LOS3A.csv
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
import sys

MANIFESTS = Path(__file__).resolve().parents[1] / "silver_new_objects_pass1" / "manifests"
COLUMNS = ("object_id", "file_id", "file_name", "sha256", "doc_stage", "discipline")


def main(code: str, out: str) -> None:
    rows = [json.loads(line) for line in (MANIFESTS / f"manifest_{code}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with Path(out).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([row["object_id"], row["file_id"], str(row["relative_path"]).replace("\\", "/"), row["sha256"],
                             row.get("stage") or "", row.get("section") or ""])
    print(f"{len(rows)} rows -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
