"""Extract the PD/RD PDFs of the two PUBLIC objects (Тюменская, Новослободская)
from the participant zip into WORK_ROOT/public_docs (outside the repo), write a
per-object manifest and build the reader text cache.  The hidden test object
(Речников) is never touched.

  python -m evaluation.measurement_bench.extract_public [--stages PD RD]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from .common import OBJECTS, PARTICIPANT_DATA, PARTICIPANT_ZIP, PUBLIC_DOCS_ROOT, TEXT_CACHE, read_jsonl, write_jsonl

ZIP_PREFIX = "ХАКАТОН_УЧАСТНИКАМ_ГОТОВО_К_ПЕРЕДАЧЕ/01_ДОКУМЕНТАЦИЯ/"
CODE_BY_OBJECT_ID = {v["object_id"]: k for k, v in OBJECTS.items() if v["kind"] == "public"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", nargs="*", default=["PD", "RD", "RD_ID_MIXED"])
    a = ap.parse_args()
    rows = [r for r in read_jsonl(PARTICIPANT_DATA / "document_manifest.jsonl") if r["object_id"] in CODE_BY_OBJECT_ID]
    z = zipfile.ZipFile(PARTICIPANT_ZIP, metadata_encoding="cp866")
    names = {n: n for n in z.namelist()}
    per_obj: dict[str, list[dict]] = {}
    for r in rows:
        stage = r["stage"]
        if stage not in a.stages or not str(r["relative_path"]).lower().endswith(".pdf"):
            continue
        obj = CODE_BY_OBJECT_ID[r["object_id"]]
        member = ZIP_PREFIX + r["relative_path"]
        if member not in names:
            print("MISSING in zip:", member)
            continue
        dest = PUBLIC_DOCS_ROOT / obj / r["relative_path"].replace("/", "\\")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(member)
            if hashlib.sha256(data).hexdigest() != r["sha256"]:
                print("SHA MISMATCH", r["file_id"])
                continue
            dest.write_bytes(data)
        per_obj.setdefault(obj, []).append({
            "file_id": r["file_id"], "object_id": r["object_id"], "split": r["split"],
            "stage": "RD" if stage == "RD_ID_MIXED" else stage, "section": r.get("section"), "sha256": r["sha256"],
            "pdf_pages": r["pdf_pages"], "relative_path": r["relative_path"], "orig_stage": stage,
        })
    for obj, lst in per_obj.items():
        write_jsonl(PUBLIC_DOCS_ROOT / f"manifest_{obj}.jsonl", lst)
        print(obj, len(lst), "files")


if __name__ == "__main__":
    main()
