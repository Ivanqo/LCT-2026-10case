"""Inventory of what the image ships: every Python distribution (version, declared license, installed size), the
baked model caches and the OCR language data. Run inside the image:

    docker run --rm --network none case10-api:latest python /app/deploy_tools/inventory.py   (or pipe the file in)
    docker run --rm --network none -i case10-api:latest python - < deploy/tools/inventory.py > inventory.json
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
from pathlib import Path
import platform
import subprocess


def _size(paths) -> int:
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def _tree_size(root: Path) -> int:
    # symlinks skipped: the HF cache links snapshot files to blobs, counting both would double the size
    return sum(_size([Path(d) / f for f in files if not (Path(d) / f).is_symlink()]) for d, _, files in os.walk(root)) if root.exists() else 0


def distributions() -> list[dict]:
    rows = []
    for dist in md.distributions():
        meta = dist.metadata
        classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
        declared = (meta.get("License-Expression") or meta.get("License") or "").strip()
        if len(declared) > 80 or "\n" in declared:   # full license text pasted into the field
            declared = declared.splitlines()[0][:80] + " …"
        files = [dist.locate_file(f) for f in (dist.files or [])]
        rows.append({"name": meta["Name"], "version": dist.version, "license": declared,
                     "license_classifiers": classifiers, "installed_mb": round(_size(files) / 2**20, 1)})
    return sorted(rows, key=lambda row: -row["installed_mb"])


def main() -> None:
    hf = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    try:
        tess = subprocess.run(["tesseract", "--version"], capture_output=True, text=True)
        tess_version = (tess.stdout or tess.stderr).splitlines()[:1]
    except OSError:
        tess_version = None
    tessdata = next((p for p in Path("/usr/share/tesseract-ocr").glob("*/tessdata")), None)
    out = {
        "python": platform.python_version(),
        "distributions": distributions(),
        "hf_cache_mb": round(_tree_size(hf) / 2**20, 1),
        "hf_models": sorted(p.name for p in (hf / "hub").glob("models--*")) if (hf / "hub").exists() else [],
        "tesseract": tess_version,
        "tessdata": {p.name: round(p.stat().st_size / 2**20, 2) for p in sorted(tessdata.glob("*.traineddata"))} if tessdata else {},
        "site_packages_mb": round(_tree_size(Path(md.distribution("pip").locate_file(""))) / 2**20, 1),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
