"""Metamorphic robustness harness for CASE10's offline batch path.

The harness builds isolated variants from one real source PDF (or creates a
small synthetic edge case), invokes ``python -m app.cli run`` with a hard
timeout, and checks the submission schema, all 132 matrix codes, process-tree
RSS and deterministic-output invariants. Generated packages and results stay
under ``--work-root``; source files are only read.

Example (run from the repository root):
    python evaluation/phase12/rob_harness.py --source NOV=E:/case10_measurement/public_docs/NOV \
        --case pdf-rotate-90 --case file-broken --case zip-slip --workers 4

Each run is sequential. A timeout is capped at 25 minutes; Windows process
trees are terminated together so a stuck worker does not remain behind.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from typing import Callable


REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "api_service"
SCHEMA = API_DIR / "app" / "reference_data" / "case_data" / "submission_schema.json"
MAX_TIMEOUT_SECONDS = 25 * 60
EXPECTED_MATRIX_CODES = frozenset(f"M-{number:03d}" for number in range(1, 133))


@dataclass
class Case:
    name: str
    docs: Path
    registry: Path | None
    expected_documented_error: bool = False
    expected_excluded: list[str] | None = None
    expected_absent: list[str] | None = None


@dataclass
class Run:
    name: str
    exit_code: int | None
    seconds: float
    peak_rss_bytes: int | None
    result_sha256: str | None
    checks: int | None
    quality_issues: int | None
    result: dict | None
    outcome: str
    detail: str = ""
    cpu_affinity: list[int] | None = None
    cpu_limit_applied: bool = False
    matrix_codes: int | None = None


class CaseSkipped(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_pdf(source: Path) -> Path:
    """Pick the smallest readable PDF page set deterministically (no names are hard-coded)."""
    import fitz

    candidates = []
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        try:
            with fitz.open(path) as pdf:
                if len(pdf):
                    candidates.append((len(pdf), path.stat().st_size,
                                       unicodedata.normalize("NFC", path.relative_to(source).as_posix()).casefold(), path))
        except Exception:
            continue
    if not candidates:
        raise FileNotFoundError(f"no readable PDF under {source}")
    return min(candidates)[-1]


def _first_page(data: bytes) -> bytes:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as source:
        if not len(source):
            raise ValueError("sample PDF has no pages")
        out = fitz.open()
        out.insert_pdf(source, from_page=0, to_page=0)
        return out.tobytes(garbage=3, deflate=True, no_new_id=True)


def _blank_pdf(width: float = 612, height: float = 792, text: str | None = None) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    if text:
        page.insert_text((36, 40), text[:2000], fontsize=7)
    doc.set_metadata({"creationDate": "D:20000101000000Z", "modDate": "D:20000101000000Z"})
    return doc.tobytes(garbage=3, deflate=True, no_new_id=True)


def _pdf_variant(name: str, original: bytes) -> bytes:
    import fitz

    if name in {"pdf-empty", "pdf-truncated"}:
        return b"" if name == "pdf-empty" else original[:max(8, len(original) // 2)]
    if name == "pdf-a0":
        return _blank_pdf(2384, 3370, "A0 large page")
    if name == "pdf-tiny-page":
        return _blank_pdf(18, 24, "tiny")
    if name == "pdf-blank-page":
        return _blank_pdf()
    if name == "pdf-long-table":
        doc = fitz.open()
        for block in range(8):
            page = doc.new_page(width=612, height=792)
            for row in range(250):
                y = 26 + row * 2.8
                page.insert_text((24, y), f"{block * 250 + row:05d}  ITEM-{row:04d}  123.456  MATERIAL TEXT", fontsize=3)
        return doc.tobytes(garbage=3, deflate=True)
    if name == "pdf-dense-vector":
        from PIL import Image
        from io import BytesIO

        doc = fitz.open()
        page = doc.new_page(width=1200, height=1200)
        shape = page.new_shape()
        for row in range(55):
            for col in range(55):
                x, y = 5 + col * 21, 5 + row * 21
                shape.draw_rect(fitz.Rect(x, y, x + 15, y + 15))
        shape.finish(color=(0, 0, 0), width=0.2)
        shape.commit()
        image = Image.new("RGB", (12, 12), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        for row in range(32):
            for col in range(32):
                x, y = 8 + col * 36, 8 + row * 36
                page.insert_image(fitz.Rect(x, y, x + 8, y + 8), stream=buffer.getvalue())
        return doc.tobytes(garbage=3, deflate=True)
    if name == "pdf-encrypted":
        with fitz.open(stream=_first_page(original), filetype="pdf") as source:
            return source.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_AES_256,
                                  owner_pw="case10-owner", user_pw="case10-user")
    if name == "pdf-no-tounicode":
        source = fitz.open(stream=_first_page(original), filetype="pdf")
        removed = 0
        for font in source[0].get_fonts(full=True):
            font_xref = int(font[0])
            if source.xref_get_key(font_xref, "ToUnicode")[0] != "null":
                source.xref_set_key(font_xref, "ToUnicode", "null")
                removed += 1
        if not removed:
            windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
            font_candidates = [
                windows / "arial.ttf", windows / "segoeui.ttf", windows / "times.ttf",
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
            ]
            font_path = next((path for path in font_candidates if path.is_file()), None)
            if font_path is None:
                raise CaseSkipped("sample has no ToUnicode map and no local embeddable Windows font is available")
            source.close()
            source = fitz.open()
            page = source.new_page()
            page.insert_font(fontname="Case10Embedded", fontfile=str(font_path))
            page.insert_text((36, 40), "ToUnicode map mutation sample", fontname="Case10Embedded", fontsize=12)
            for font in page.get_fonts(full=True):
                font_xref = int(font[0])
                if source.xref_get_key(font_xref, "ToUnicode")[0] != "null":
                    source.xref_set_key(font_xref, "ToUnicode", "null")
                    removed += 1
        if not removed:
            raise CaseSkipped("could not create a font with a ToUnicode map")
        return source.tobytes(garbage=3, deflate=True, no_new_id=True)

    rotate = {"pdf-rotate-90": 90, "pdf-rotate-180": 180, "pdf-rotate-270": 270}
    if name in rotate:
        with fitz.open(stream=_first_page(original), filetype="pdf") as source:
            page = source[0]
            page.set_rotation(rotate[name])
            return source.tobytes(garbage=3, deflate=True)
    if name == "pdf-content-rotated":
        with fitz.open(stream=_first_page(original), filetype="pdf") as source:
            page = source[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(1, 1).prerotate(90), alpha=False)
            out = fitz.open()
            rotated = out.new_page(width=pix.width, height=pix.height)
            rotated.insert_image(rotated.rect, stream=pix.tobytes("png"))
            return out.tobytes(garbage=3, deflate=True)
    if name == "pdf-rasterized":
        with fitz.open(stream=_first_page(original), filetype="pdf") as source:
            page = source[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
            out = fitz.open()
            raster = out.new_page(width=page.rect.width, height=page.rect.height)
            raster.insert_image(raster.rect, stream=pix.tobytes("png"))
            return out.tobytes(garbage=3, deflate=True)
    raise KeyError(name)


def _registry_rows(docs: Path) -> list[dict]:
    rows = []
    for path in sorted((p for p in docs.rglob("*") if p.is_file()),
                       key=lambda p: (unicodedata.normalize("NFC", p.relative_to(docs).as_posix()).casefold(),
                                      p.relative_to(docs).as_posix())):
        rel = path.relative_to(docs).as_posix()
        sha = _sha256(path)
        first = PurePosixPath(rel).parts[0].upper() if PurePosixPath(rel).parts else "PD"
        stage = {"RD": "RD", "РД": "RD", "WORK_STAGE": "RD",
                 "ID": "ID", "ИД": "ID", "AS_BUILT_STAGE": "ID"}.get(first, "PD")
        rows.append({"file_id": f"F-{sha[:16]}-{len(rows):04d}", "file_name": rel, "sha256": sha,
                     "doc_stage": stage, "approval_status": "APPROVED"})
    return rows


def _write_json_registry(docs: Path, path: Path, *, duplicate_first: bool = False, missing_file: bool = False,
                         extra_column: bool = False, missing_columns: bool = False,
                         missing_approval: bool = False, approval_value: str | None = None) -> Path:
    rows = _registry_rows(docs)
    if missing_approval:
        for row in rows:
            row.pop("approval_status", None)
    elif approval_value is not None:
        for row in rows:
            row["approval_status"] = approval_value
    if extra_column:
        for row in rows:
            row["unexpected_extension"] = "preserved"
    if missing_columns:
        rows = [{"file_name": row["file_name"], "unexpected_extension": "preserved"} for row in rows]
    if duplicate_first and rows:
        rows.append(dict(rows[0]))
    if missing_file:
        rows.append({"file_id": "F-MISSING", "file_name": "missing.pdf", "doc_stage": "RD"})
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_csv_registry(docs: Path, path: Path, encoding: str, *, extra: bool = False, missing_columns: bool = False,
                        duplicate_first: bool = False) -> Path:
    rows = _registry_rows(docs)
    if duplicate_first and rows:
        rows.append(dict(rows[0]))
    headers = ["file_name", "sha256", "doc_stage", "approval_status"]
    if extra:
        headers.append("unrecognized_column")
    if missing_columns:
        headers = ["file_name"]
    text = ";".join(headers) + "\r\n"
    for row in rows:
        text += ";".join(str(row.get(header, "")) for header in headers) + "\r\n"
    path.write_bytes((b"\xef\xbb\xbf" if encoding == "utf-8-sig" else b"") + text.encode("cp1251" if encoding == "cp1251" else "utf-8"))
    return path


def _write_xlsx_registry(docs: Path, path: Path, *, missing_columns: bool = False) -> Path:
    """Write a tiny standards-shaped XLSX using only stdlib ZIP/XML-compatible strings."""
    rows = _registry_rows(docs)
    headers = ["file_name"] if missing_columns else ["file_name", "sha256", "doc_stage", "approval_status", "extra_column"]
    values = [headers] + [[row.get(key, "extra") for key in headers] for row in rows]

    def col(n: int) -> str:
        result = ""
        while n:
            n, rem = divmod(n - 1, 26)
            result = chr(65 + rem) + result
        return result

    from xml.sax.saxutils import escape

    x_rows = []
    for r_idx, record in enumerate(values, 1):
        cells = []
        for c_idx, value in enumerate(record, 1):
            address = f"{col(c_idx)}{r_idx}"
            cells.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        x_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{rel_ns}"><sheets><sheet name="Registry" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(x_rows)}</sheetData></worksheet>')
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
    return path


def _corrupt_stored_zip_member(archive_path: Path, member_name: str) -> None:
    """Flip one payload byte while preserving the ZIP directory and recorded CRC."""
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo(member_name)
    raw = bytearray(archive_path.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    data_offset = info.header_offset + 30 + name_length + extra_length
    raw[data_offset] ^= 0xFF
    archive_path.write_bytes(raw)


def build_case(name: str, root: Path, sample_pdf: bytes) -> Case:
    """Build one test package; every case is isolated and uses the same package basename."""
    docs = root / "package"
    docs.mkdir(parents=True, exist_ok=True)
    registry: Path | None = None
    source_bytes = _first_page(sample_pdf)
    target = docs / "PD" / "sample.pdf"

    if name.startswith("pdf-"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_pdf_variant(name, source_bytes))
        if name not in {"pdf-empty", "pdf-truncated", "pdf-encrypted"}:
            registry = _write_json_registry(docs, root / "registry.json")
        else:
            registry = _write_json_registry(docs, root / "registry.json")
        return Case(name, docs, registry)

    if name in {"file-empty", "file-broken"}:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"" if name == "file-empty" else b"%PDF-1.7\ncorrupt")
        (docs / "RD" / "survivor.pdf").parent.mkdir(parents=True, exist_ok=True)
        (docs / "RD" / "survivor.pdf").write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-duplicate-content":
        for rel in ("PD/first.pdf", "RD/second-copy.pdf"):
            dest = docs / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source_bytes)
    elif name == "file-duplicate-basename":
        for rel, data in (("PD/same.pdf", source_bytes), ("RD/same.pdf", _blank_pdf(text="second"))):
            dest = docs / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-unicode-nfd":
        name_nfd = unicodedata.normalize("NFD", "café-plan.pdf")
        dest = docs / "РД" / name_nfd
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-unicode-spaces":
        dest = docs / "Рабочая документация" / "План A.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-name-over-200-bytes":
        dest = docs / ("a" * 197 + ".pdf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-dir-name-154-cyrillic":
        directory = docs / ("а" * 154)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sample.pdf").write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "file-unsupported-extension":
        (docs / "PD").mkdir(parents=True, exist_ok=True)
        (docs / "PD" / "survivor.pdf").write_bytes(source_bytes)
        (docs / "RD" / "notes.unsupported").parent.mkdir(parents=True, exist_ok=True)
        (docs / "RD" / "notes.unsupported").write_bytes(b"unsupported payload")
        registry = _write_json_registry(docs, root / "registry.json")
        return Case(name, docs, registry, expected_excluded=["notes.unsupported"])
    elif name == "package-renamed-stages":
        for rel in ("design_stage/one.pdf", "work_stage/two.pdf", "as_built_stage/three.pdf"):
            dest = docs / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name in {"package-order-forward", "package-order-reverse"}:
        entries = [("PD/a.pdf", source_bytes), ("RD/b.pdf", _blank_pdf(text="working")),
                   ("ID/c.pdf", _blank_pdf(text="as-built"))]
        if name == "package-order-reverse":
            entries.reverse()
        for rel, data in entries:
            dest = docs / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "package-2000-files":
        (docs / "PD" / "anchor.pdf").parent.mkdir(parents=True, exist_ok=True)
        (docs / "PD" / "anchor.pdf").write_bytes(source_bytes)
        junk = docs / "misc"
        junk.mkdir(parents=True, exist_ok=True)
        for index in range(1999):
            (junk / f"note-{index:04d}.bin").write_bytes(b"x")
        registry = _write_json_registry(docs, root / "registry.json")
    elif name == "registry-missing":
        (docs / "PD").mkdir(parents=True, exist_ok=True)
        (docs / "PD" / "sample.pdf").write_bytes(source_bytes)
    elif name.startswith("registry-"):
        (docs / "PD").mkdir(parents=True, exist_ok=True)
        (docs / "PD" / "sample.pdf").write_bytes(source_bytes)
        registry = root / "registry"  # suffix selected below
        if name == "registry-csv-extra":
            registry = _write_csv_registry(docs, root / "registry.csv", "utf-8", extra=True)
        elif name == "registry-csv-missing-columns":
            registry = _write_csv_registry(docs, root / "registry.csv", "utf-8", missing_columns=True)
        elif name == "registry-csv-cp1251":
            registry = _write_csv_registry(docs, root / "registry.csv", "cp1251")
        elif name == "registry-csv-utf8-bom":
            registry = _write_csv_registry(docs, root / "registry.csv", "utf-8-sig")
        elif name == "registry-csv-duplicate":
            registry = _write_csv_registry(docs, root / "registry.csv", "utf-8", duplicate_first=True)
        elif name == "registry-xlsx-extra":
            registry = _write_xlsx_registry(docs, root / "registry.xlsx")
        elif name == "registry-xlsx-missing-columns":
            registry = _write_xlsx_registry(docs, root / "registry.xlsx", missing_columns=True)
        elif name == "registry-json-extra":
            registry = _write_json_registry(docs, root / "registry.json", extra_column=True)
        elif name == "registry-json-missing-columns":
            registry = _write_json_registry(docs, root / "registry.json", missing_columns=True)
        elif name == "registry-json-missing-approval":
            registry = _write_json_registry(docs, root / "registry.json", missing_approval=True)
        elif name == "registry-json-unknown-approval":
            registry = _write_json_registry(docs, root / "registry.json", approval_value="неизвестно")
        elif name == "registry-json-duplicate":
            registry = _write_json_registry(docs, root / "registry.json", duplicate_first=True)
        elif name == "registry-json-missing-file":
            registry = _write_json_registry(docs, root / "registry.json", missing_file=True)
        elif name == "registry-json-invalid":
            registry = root / "registry.json"
            registry.write_text("{not json", encoding="utf-8")
        elif name == "registry-xlsx-invalid":
            registry = root / "registry.xlsx"
            registry.write_bytes(b"not an XLSX archive")
        else:
            raise KeyError(name)
        return Case(name, docs, registry, expected_documented_error=name.endswith("-invalid"))
    elif name in {"zip-nested", "zip-slip", "zip-corrupt-member"}:
        (docs / "PD").mkdir(parents=True, exist_ok=True)
        (docs / "PD" / "safe.pdf").write_bytes(source_bytes)
        registry = _write_json_registry(docs, root / "registry.json")
        archive = root / "outer.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as outer:
            for path in sorted(docs.rglob("*")):
                if path.is_file():
                    outer.write(path, Path("package") / path.relative_to(docs))
            if name == "zip-nested":
                inner = root / "inner.zip"
                with zipfile.ZipFile(inner, "w") as nested:
                    nested.writestr("nested/hidden.pdf", source_bytes)
                outer.write(inner, "package/PD/nested.zip")
            elif name == "zip-slip":
                outer.writestr("package/../../escape.pdf", b"must not escape")
                outer.writestr("package/../../../escape_dir/", b"")
                outer.writestr("C:/drive.pdf", b"must not escape")
            else:
                outer.writestr("package/PD/bad.pdf", b"bad member payload")
        if name == "zip-corrupt-member":
            _corrupt_stored_zip_member(archive, "package/PD/bad.pdf")
        return Case(name, archive, registry,
                    expected_excluded=(
                        ["escape.pdf", "escape_dir", "drive.pdf"] if name == "zip-slip"
                        else ["bad.pdf"] if name == "zip-corrupt-member"
                        else ["nested.zip"] if name == "zip-nested" else []
                    ),
                    expected_absent=(
                        ["escape.pdf", "escape_dir", "drive.pdf"] if name == "zip-slip"
                        else ["bad.pdf"] if name == "zip-corrupt-member" else []
                    ))
    else:
        raise KeyError(name)

    return Case(name, docs, registry)


def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    capture_output=True, timeout=10, check=False)
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        import psutil

        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:
        pass


def _rss_tree(pid: int) -> int:
    try:
        import psutil

        parent = psutil.Process(pid)
        processes = [parent]
        try:
            processes.extend(parent.children(recursive=True))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        total = 0
        for process in processes:
            try:
                if process.is_running():
                    total += process.memory_info().rss
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return total
    except Exception:
        return 0


def _matrix_code_coverage(checks: object) -> tuple[set[str], set[str], set[str]]:
    """Return seen, missing and unexpected official codes (rows may repeat by location)."""
    seen = {
        str(row.get("matrix_code")).strip().upper()
        for row in checks if isinstance(row, dict)
        and re.fullmatch(r"M-\d{3}", str(row.get("matrix_code") or "").strip().upper())
    } if isinstance(checks, list) else set()
    return seen, EXPECTED_MATRIX_CODES - seen, seen - EXPECTED_MATRIX_CODES


def _apply_cpu_affinity(pid: int, cpu_ids: list[int]) -> bool:
    """Constrain the CLI and any already-started descendants to the same CPUs."""
    try:
        import psutil

        parent = psutil.Process(pid)
        for process in [parent] + parent.children(recursive=True):
            try:
                if process.cpu_affinity() != cpu_ids:
                    process.cpu_affinity(cpu_ids)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return parent.cpu_affinity() == cpu_ids
    except Exception:
        return False


def run_cli(case: Case, out_root: Path, *, timeout: int, workers: int, memory_limit_bytes: int | None = None,
            code_root: Path | None = None, cpu_limit: int = 4, robust_enabled: bool = False) -> Run:
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds")
    code_root = (code_root or REPO).resolve()
    api_dir = code_root / "api_service"
    schema_path = code_root / "api_service" / "app" / "reference_data" / "case_data" / "submission_schema.json"
    out_root.mkdir(parents=True, exist_ok=True)
    out = out_root / f"{case.name}.result.json"
    timings = out_root / f"{case.name}.timings.json"
    work = out_root / f"{case.name}.work"
    command = [sys.executable, "-m", "app.cli", "run", "--docs", str(case.docs), "--out", str(out),
               "--work-dir", str(work), "--timings", str(timings), "--workers", str(workers)]
    if case.registry:
        command += ["--registry", str(case.registry)]
    env = os.environ.copy()
    env.update({"PYTHONPATH": f"{api_dir};{code_root}", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false", "OMP_THREAD_LIMIT": "1",
                "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "CASE10_LIVE_TAGGER_WORKERS": str(workers), "API_DATA_DIR": str(work / "data"),
                "CASE10_ROBUST_INPUTS": "1" if robust_enabled else "0",
                "CASE10_APPROVAL_UNKNOWN_POLICY": "block"})
    if "CASE10_LIVE_TAGGER_CACHE_DIR" not in env and Path(r"E:\case10_measurement\tagger_cache").is_dir():
        env["CASE10_LIVE_TAGGER_CACHE_DIR"] = r"E:\case10_measurement\tagger_cache"
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=code_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
    try:
        import psutil

        available_cpus = psutil.Process().cpu_affinity()
        cpu_ids = available_cpus[:cpu_limit]
    except Exception:
        cpu_ids = []
    cpu_limit_applied = bool(cpu_ids and _apply_cpu_affinity(process.pid, cpu_ids))

    def record_run(*values) -> Run:
        result = values[7] if len(values) > 7 and isinstance(values[7], dict) else None
        matrix_codes = len(_matrix_code_coverage(result.get("checks"))[0]) if result else None
        return Run(*values, cpu_affinity=cpu_ids or None, cpu_limit_applied=cpu_limit_applied,
                   matrix_codes=matrix_codes)

    peak = 0
    exceeded = threading.Event()

    def watch_memory() -> None:
        nonlocal peak, cpu_limit_applied
        while process.poll() is None:
            if cpu_ids:
                applied = _apply_cpu_affinity(process.pid, cpu_ids)
                # The process may exit between poll() and psutil's lookup. That is not a
                # failed limit if an earlier sample already constrained it.
                if applied:
                    cpu_limit_applied = True
                elif process.poll() is None:
                    cpu_limit_applied = False
            rss = _rss_tree(process.pid)
            peak = max(peak, rss)
            if memory_limit_bytes is not None and rss > memory_limit_bytes:
                exceeded.set()
                _kill_process_tree(process.pid)
                return
            time.sleep(0.5)

    watcher = threading.Thread(target=watch_memory, name="rob-harness-rss", daemon=True)
    watcher.start()
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process.pid)
        output, _ = process.communicate(timeout=15)
        elapsed = time.monotonic() - started
        return record_run(case.name, None, round(elapsed, 2), peak or None, None, None, None, None, "TIMEOUT",
                          output[-1200:])
    finally:
        watcher.join(timeout=2)
    elapsed = time.monotonic() - started
    if peak == 0 and timings.is_file():
        try:
            timing_data = json.loads(timings.read_text(encoding="utf-8"))
            peak = int(float(timing_data.get("memory", {}).get("peak_rss_process_tree_mb", 0)) * 1024**2)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    code = int(process.returncode or 0)
    if exceeded.is_set():
        return record_run(case.name, code, round(elapsed, 2), peak or None, None, None, None, None, "MEMORY_LIMIT",
                          f"process tree exceeded {memory_limit_bytes} bytes\n{output[-1000:]}")
    if code != 0:
        if "Traceback (most recent call last)" in output:
            outcome = "TRACEBACK"
        elif case.expected_documented_error and code == 1 and "error:" in output.lower():
            outcome = "DOCUMENTED_ERROR"
        else:
            outcome = "UNDOCUMENTED_ERROR"
        return record_run(case.name, code, round(elapsed, 2), peak or None, None, None, None, None, outcome,
                          output[-1200:])
    if not out.is_file():
        return record_run(case.name, code, round(elapsed, 2), peak or None, None, None, None, None,
                          "MISSING_RESULT", output[-800:])
    raw = out.read_bytes()
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), None,
                          None, None, "INVALID_JSON", str(exc))
    try:
        import jsonschema

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(result, schema)
    except Exception as exc:
        return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(),
                          len(result.get("checks") or []), len(result.get("quality_issues") or []), result,
                          "INVALID_SCHEMA", f"{type(exc).__name__}: {exc}")
    checks = result.get("checks")
    if not isinstance(checks, list):
        return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(),
                          None, len(result.get("quality_issues") or []), result, "CHECK_COUNT",
                          "checks is not a list")
    if robust_enabled and len(checks) != 132:
        return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(),
                          len(checks), len(result.get("quality_issues") or []), result, "CHECK_COUNT",
                          f"robust mode expected exactly 132 checks, got {len(checks)}")
    seen_codes, missing_codes, unexpected_codes = _matrix_code_coverage(checks)
    if missing_codes or unexpected_codes:
        return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(),
                          len(checks), len(result.get("quality_issues") or []), result, "MATRIX_CODE_COVERAGE",
                          f"unique official codes={len(seen_codes)}, missing={sorted(missing_codes)}, "
                          f"unexpected={sorted(unexpected_codes)}")
    unreadable_cases = {"pdf-empty", "pdf-encrypted", "file-empty", "file-broken"}
    if case.name in unreadable_cases:
        unreadable = (result.get("package") or {}).get("pdf_unreadable") or []
        if not unreadable:
            return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), 132,
                              len(result.get("quality_issues") or []), result, "UNREADABLE_NOT_RECORDED",
                              "package.pdf_unreadable is empty")
        statuses = (result.get("package") or {}).get("file_statuses") or []
        if not any(item.get("status") == "PROCESSING_ERROR" and item.get("reason") == "PDF_UNREADABLE"
                   for item in statuses if isinstance(item, dict)):
            return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), 132,
                              len(result.get("quality_issues") or []), result, "PROCESSING_STATUS_MISSING",
                              "unreadable PDF has no PROCESSING_ERROR file status")
    if case.name == "file-duplicate-content":
        package = result.get("package") or {}
        if package.get("files_imported") != 1 or package.get("files_total") != 2:
            return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), 132,
                              len(result.get("quality_issues") or []), result, "DUPLICATE_INVARIANT",
                              f"expected 2 listed / 1 imported, got {package.get('files_total')} / {package.get('files_imported')}")
    if case.expected_excluded:
        package_exclusions = (result.get("package") or {}).get("excluded_files") or []
        reported_names = {PurePosixPath(str(item.get("file") or "").replace("\\", "/")).name
                          for item in package_exclusions if isinstance(item, dict)}
        missing = sorted(set(case.expected_excluded) - reported_names)
        if missing:
            return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), 132,
                              len(result.get("quality_issues") or []), result, "EXCLUSION_NOT_REPORTED",
                              f"excluded members lack a reason in package report: {missing}")
        unpacked = work / "unpacked"
        absent_names = set(case.expected_absent or [])
        present = [path.name for path in unpacked.rglob("*") if path.is_file() and path.name in absent_names] if unpacked.exists() else []
        if present:
            return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), 132,
                              len(result.get("quality_issues") or []), result, "ZIP_EXCLUSION_FAILURE",
                              f"unsafe members appeared after extraction: {present}")
    return record_run(case.name, code, round(elapsed, 2), peak or None, hashlib.sha256(raw).hexdigest(), len(checks),
                      len(result.get("quality_issues") or []), result, "PASS", output[-500:])


def _same_result(first: Run, second: Run) -> bool:
    return first.result_sha256 is not None and first.result_sha256 == second.result_sha256


def _build_control(root: Path, sample: bytes) -> Case:
    docs = root / "package"
    target = docs / "PD" / "sample.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_first_page(sample))
    registry = _write_json_registry(docs, root / "registry.json")
    return Case("control", docs, registry)


def _case_names() -> list[str]:
    return [
        "pdf-rasterized", "pdf-rotate-90", "pdf-rotate-180", "pdf-rotate-270", "pdf-content-rotated",
        "pdf-no-tounicode", "pdf-a0", "pdf-tiny-page", "pdf-blank-page", "pdf-empty", "pdf-truncated",
        "pdf-encrypted", "pdf-dense-vector", "pdf-long-table", "file-empty", "file-broken",
        "file-duplicate-content", "file-duplicate-basename", "file-unicode-nfd", "file-unicode-spaces", "file-name-over-200-bytes",
        "file-dir-name-154-cyrillic",
        "file-unsupported-extension", "package-renamed-stages", "package-2000-files", "registry-missing",
        "package-order-forward", "package-order-reverse",
        "registry-csv-extra", "registry-csv-missing-columns", "registry-csv-cp1251", "registry-csv-utf8-bom",
        "registry-csv-duplicate", "registry-xlsx-extra", "registry-xlsx-missing-columns", "registry-json-extra",
        "registry-json-missing-columns", "registry-json-missing-approval", "registry-json-unknown-approval",
        "registry-json-duplicate", "registry-json-missing-file", "registry-json-invalid",
        "registry-xlsx-invalid", "zip-nested", "zip-slip", "zip-corrupt-member",
    ]


def _parse_named_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in name):
        raise argparse.ArgumentTypeError("expected a simple NAME and a PATH as NAME=PATH")
    return name, Path(path).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", action="append", type=_parse_named_path, required=True,
                        help="source package, NAME=PATH; pass NOV, ALT79B, and TYU")
    parser.add_argument("--registry", action="append", type=_parse_named_path, default=[],
                        help="registry path for a source, NAME=PATH")
    parser.add_argument("--code-root", type=Path, default=REPO,
                        help="repository checkout used by the CLI (default: this worktree)")
    parser.add_argument("--full-package", action="store_true",
                        help="run each full source package twice for a byte-identity baseline; no variants")
    parser.add_argument("--single-run", action="store_true",
                        help="with --full-package, measure one run only (for large-package resource profiling)")
    parser.add_argument("--expect-result", action="append", type=_parse_named_path, default=[],
                        help="compare full-package result bytes with NAME=PATH baseline output")
    parser.add_argument("--case", action="append", choices=_case_names(), dest="cases",
                        help="variant to run; repeat; default: all except the 2000-file stress and optional ToUnicode mutation")
    parser.add_argument("--work-root", type=Path, default=Path(tempfile.gettempdir()) / "case10-rob-harness")
    parser.add_argument("--timeout-seconds", type=int, default=1500)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cpu-limit", type=int, default=4,
                        help="limit the CLI process tree to this many CPUs using psutil affinity")
    parser.add_argument("--memory-limit-gb", type=float, default=8.0)
    parser.add_argument("--max-cases", type=int, default=6, help="maximum sequential variants per source package")
    parser.add_argument("--plan-only", action="store_true", help="list planned variants and sources without running the CLI")
    args = parser.parse_args(argv)
    if args.timeout_seconds > MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout-seconds cannot exceed {MAX_TIMEOUT_SECONDS}")
    if args.single_run and not args.full_package:
        parser.error("--single-run requires --full-package")
    if args.workers < 1 or args.cpu_limit < 1 or args.max_cases < 1 or args.memory_limit_gb <= 0:
        parser.error("workers, cpu-limit, max-cases, and memory-limit-gb must be positive")
    work_root = args.work_root.resolve()
    if work_root == REPO or work_root in REPO.parents or REPO in work_root.parents:
        parser.error("--work-root must be outside the repository")
    for source_name, source_path in args.source:
        if work_root == source_path or work_root in source_path.parents or source_path in work_root.parents:
            parser.error("--work-root must not overlap any source package")
    code_root = args.code_root.resolve()
    if not (code_root / "api_service" / "app" / "cli.py").is_file():
        parser.error(f"--code-root is not a CASE10 repository: {code_root}")
    if work_root == code_root or work_root in code_root.parents or code_root in work_root.parents:
        parser.error("--work-root must not overlap --code-root")
    args.work_root = work_root
    args.code_root = code_root
    cases = args.cases or [name for name in _case_names() if name not in {"pdf-no-tounicode", "package-2000-files"}]
    if not args.full_package:
        cases = cases[:args.max_cases]
    registry_map = dict(args.registry)
    expected_map = dict(args.expect_result)
    args.work_root.mkdir(parents=True, exist_ok=True)
    output: dict = {"sources": [], "cases": [], "limits": {"timeout_seconds": args.timeout_seconds,
                                                            "workers": args.workers, "cpu_limit": args.cpu_limit,
                                                            "memory_limit_gb": args.memory_limit_gb}}
    for source_name, source_path in args.source:
        if not source_path.is_dir():
            parser.error(f"source directory does not exist: {source_path}")
        sample_path = None
        source_record = {"name": source_name, "path": str(source_path)}
        if not args.full_package:
            sample_path = _source_pdf(source_path)
            source_record.update({"sample_relative_path": sample_path.relative_to(source_path).as_posix(),
                                  "sample_bytes": sample_path.stat().st_size, "sha256": _sha256(sample_path)})
        output["sources"].append(source_record)
        if args.full_package:
            if args.plan_only:
                output["cases"].append({"source": source_name, "case": "full-package", "path": str(source_path)})
                continue
            full_root = args.work_root / source_name / "full"
            if full_root.exists():
                shutil.rmtree(full_root)
            full_root.mkdir(parents=True)
            full_case = Case(f"full-{source_name}", source_path, registry_map.get(source_name))
            first = run_cli(full_case, full_root / "first", timeout=args.timeout_seconds, workers=args.workers,
                            memory_limit_bytes=int(args.memory_limit_gb * 1024**3), code_root=args.code_root,
                            cpu_limit=args.cpu_limit)
            second = None
            if not args.single_run:
                second = run_cli(full_case, full_root / "second", timeout=args.timeout_seconds, workers=args.workers,
                                 memory_limit_bytes=int(args.memory_limit_gb * 1024**3), code_root=args.code_root,
                                 cpu_limit=args.cpu_limit)
            expected_path = expected_map.get(source_name)
            expected_same = None
            if expected_path is not None and (full_root / "first" / f"{full_case.name}.result.json").is_file():
                expected_same = ((full_root / "first" / f"{full_case.name}.result.json").read_bytes()
                                 == expected_path.read_bytes())
            full_record = {"source": source_name, "first": asdict(first),
                           "second": asdict(second) if second is not None else None,
                           "byte_identical": _same_result(first, second) if second is not None else None,
                           "determinism_checked": second is not None,
                           "byte_identical_to_expected": expected_same}
            full_record["first"]["result"] = None
            if full_record["second"] is not None:
                full_record["second"]["result"] = None
            output.setdefault("full_packages", {})[source_name] = full_record
            print(json.dumps({"source": source_name, "case": "full-package", "first": first.outcome,
                              "second": second.outcome if second is not None else None,
                              "byte_identical": full_record["byte_identical"],
                              "expected_same": expected_same, "seconds": first.seconds,
                              "peak_rss_bytes": first.peak_rss_bytes}, ensure_ascii=False))
            continue
        if args.plan_only:
            output["cases"].extend({"source": source_name, "case": name} for name in cases)
            continue
        assert sample_path is not None
        sample = sample_path.read_bytes()
        source_root = args.work_root / source_name
        source_root.mkdir(parents=True, exist_ok=True)
        baseline_root = source_root / "baseline"
        if baseline_root.exists():
            shutil.rmtree(baseline_root)
        baseline_root.mkdir(parents=True)
        baseline_case = Case(f"baseline-{source_name}", source_path, registry_map.get(source_name))
        baseline = run_cli(baseline_case, baseline_root / "out", timeout=args.timeout_seconds,
                           workers=args.workers, memory_limit_bytes=int(args.memory_limit_gb * 1024**3),
                           code_root=args.code_root, cpu_limit=args.cpu_limit)
        baseline_record = asdict(baseline)
        baseline_record["result"] = None
        output.setdefault("baselines", {})[source_name] = baseline_record
        output.setdefault("baseline_seconds", {})[source_name] = (
            baseline.seconds if baseline.outcome == "PASS" else None
        )
        print(json.dumps({"source": source_name, "case": "source-baseline", "outcome": baseline.outcome,
                          "seconds": baseline.seconds, "peak_rss_bytes": baseline.peak_rss_bytes,
                          "matrix_codes": baseline.matrix_codes}, ensure_ascii=False))
        control_dir = source_root / "control"
        if control_dir.exists():
            shutil.rmtree(control_dir)
        control_dir.mkdir(parents=True)
        control = _build_control(control_dir, sample)
        control_first = run_cli(control, control_dir / "out-first", timeout=args.timeout_seconds,
                                workers=args.workers, memory_limit_bytes=int(args.memory_limit_gb * 1024**3),
                                code_root=args.code_root, cpu_limit=args.cpu_limit, robust_enabled=True)
        control_second = run_cli(control, control_dir / "out-second", timeout=args.timeout_seconds,
                                 workers=args.workers, memory_limit_bytes=int(args.memory_limit_gb * 1024**3),
                                 code_root=args.code_root, cpu_limit=args.cpu_limit, robust_enabled=True)
        deterministic = _same_result(control_first, control_second)
        output.setdefault("control", {})[source_name] = {
            "first": asdict(control_first), "second": asdict(control_second),
            "byte_identical": deterministic,
        }
        output["control"][source_name]["first"]["result"] = None
        output["control"][source_name]["second"]["result"] = None
        print(json.dumps({"source": source_name, "case": "control", "first": control_first.outcome,
                          "second": control_second.outcome, "byte_identical": deterministic,
                          "seconds": control_first.seconds}, ensure_ascii=False))
        for case_name in cases:
            case_root = source_root / case_name
            if case_root.exists():
                shutil.rmtree(case_root)
            case_root.mkdir(parents=True)
            try:
                case = build_case(case_name, case_root, sample)
            except CaseSkipped as exc:
                output["cases"].append({"source": source_name, "case": case_name, "outcome": "SKIPPED",
                                        "detail": str(exc)})
                continue
            result = run_cli(case, case_root / "out", timeout=args.timeout_seconds, workers=args.workers,
                             memory_limit_bytes=int(args.memory_limit_gb * 1024**3), code_root=args.code_root,
                             cpu_limit=args.cpu_limit, robust_enabled=True)
            record = {"source": source_name, **asdict(result)}
            record["result"] = None  # the full submission is in result.json, not the run summary
            baseline_seconds = output.get("baseline_seconds", {}).get(source_name)
            if baseline_seconds:
                record["within_3x_baseline"] = result.seconds <= 3 * baseline_seconds
            else:
                record["within_3x_baseline"] = None
            if case.expected_documented_error and result.outcome != "DOCUMENTED_ERROR":
                record["expected_error_check"] = "FAIL"
            elif case.expected_documented_error:
                record["expected_error_check"] = "PASS"
            output["cases"].append(record)
            print(json.dumps(record, ensure_ascii=False, default=str))
    report = args.work_root / "rob_harness_summary.json"
    failures = [] if args.plan_only else [
        item for item in output["cases"]
        if item.get("outcome") not in {"PASS", "DOCUMENTED_ERROR", "SKIPPED"}
        or item.get("expected_error_check") == "FAIL" or item.get("within_3x_baseline") is False
    ]
    if not args.plan_only:
        failures.extend({"source": item.get("source"), "case": item.get("name"), "outcome": "CPU_LIMIT_NOT_APPLIED"}
                        for item in output["cases"] if item.get("cpu_limit_applied") is False)
        failures.extend({"source": name, "outcome": "SOURCE_BASELINE_FAILURE"}
                        for name, data in output.get("baselines", {}).items()
                        if data.get("outcome") != "PASS" or not data.get("cpu_limit_applied"))
        for source_name, _ in args.source:
            source_cases = {item.get("name"): item for item in output["cases"] if item.get("source") == source_name}
            forward, reverse = source_cases.get("package-order-forward"), source_cases.get("package-order-reverse")
            if forward is not None and reverse is not None:
                same = bool(forward.get("result_sha256") and
                            forward.get("result_sha256") == reverse.get("result_sha256"))
                forward["order_independent"] = reverse["order_independent"] = same
                output.setdefault("order_permutation", {})[source_name] = {
                    "byte_identical": same,
                    "forward_sha256": forward.get("result_sha256"),
                    "reverse_sha256": reverse.get("result_sha256"),
                }
                if not same:
                    failures.append({"source": source_name, "outcome": "ORDER_DEPENDENT_RESULT"})
    if not args.plan_only:
        failures.extend({"source": name, "outcome": "NONDETERMINISTIC_CONTROL"}
                        for name, data in output.get("control", {}).items()
                        if not data.get("byte_identical") or data["first"].get("outcome") != "PASS"
                        or data["second"].get("outcome") != "PASS"
                        or not data["first"].get("cpu_limit_applied")
                        or not data["second"].get("cpu_limit_applied"))
        failures.extend({"source": name, "outcome": "FULL_PACKAGE_FAILURE"}
                        for name, data in output.get("full_packages", {}).items()
                        if data["first"].get("outcome") != "PASS"
                        or not data["first"].get("cpu_limit_applied")
                        or (data.get("determinism_checked") and
                            (not data.get("byte_identical") or data["second"].get("outcome") != "PASS"
                             or not data["second"].get("cpu_limit_applied")))
                        or data.get("byte_identical_to_expected") is False)
    output["failures"] = failures
    report.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"summary: {report}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
