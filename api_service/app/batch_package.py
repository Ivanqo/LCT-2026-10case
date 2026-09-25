"""Server-side batch path (Phase 12, S6): a document package -- a folder or a ZIP on the server -- plus the file
registry the Perechen ID 1.1 requires, turned into the manifest rows the CASE10 pipeline imports.

No UI limits apply here: the 50/200 MB caps of `routes_upload` are for interactive uploads only (expert session
§9, Q14); real packages are gigabytes and arrive by a server path. Nothing is special-cased by object, file name or
page: the package layout is read as data (stage from the registry, else from the package's own folder names), and
the output depends only on the bytes of the files and the registry, never on where the package sits on disk, the
order a file system lists it, or the time -- so the same package gives the same `result.json` (byte-identical) on
any machine running the same image.

Registry (Perechen ID 1.1, "ОБЯЗАТЕЛЬНЫЙ МАШИНОЧИТАЕМЫЙ РЕЕСТР ФАЙЛОВ"): CSV / XLSX / JSON with `object_id`,
`file_id` / `file_name` / `SHA-256`, `doc_stage`, `discipline`, `document_code`, `revision`, `approval_status`,
`approval_date`, `sheet_page_range`, `predecessor_id` / `successor_id`, `signature_status`. A package without a
registry is accepted with status CLARIFICATION_REQUIRED (the document says so); so is one whose registry does not
describe every file or whose SHA-256 does not match the file. What the pipeline does with those statuses (source
selection, blocking conclusions) belongs to the decision layer; this module records them on every document
(`dataset_metadata["document_manifest"]["registry"]`) and in the package report.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Iterable

PACKAGE_SPLIT = "BATCH_PACKAGE"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"

REGISTRY_FIELDS = (
    "object_id", "file_id", "file_name", "sha256", "doc_stage", "discipline", "document_code", "revision",
    "approval_status", "approval_date", "sheet_page_range", "predecessor_id", "successor_id", "signature_status",
)
APPROVAL_STATUSES = ("DRAFT", "APPROVED", "FOR_CONSTRUCTION", "SUPERSEDED", "CANCELLED")

# Header spellings seen in practice -> the Perechen 1.1 field. Keys are normalized by `_header_key`.
_HEADER_ALIASES = {
    "sha_256": "sha256", "sha": "sha256", "hash": "sha256", "checksum": "sha256", "контрольная_сумма": "sha256",
    "filename": "file_name", "name": "file_name", "path": "file_name", "relative_path": "file_name",
    "file_path": "file_name", "имя_файла": "file_name", "файл": "file_name",
    "stage": "doc_stage", "стадия": "doc_stage",
    "section": "discipline", "раздел": "discipline", "марка": "discipline",
    "code": "document_code", "шифр": "document_code",
    "rev": "revision", "редакция": "revision", "изм": "revision",
    "status": "approval_status", "статус": "approval_status",
    "date": "approval_date", "дата_утверждения": "approval_date",
    "pages": "sheet_page_range", "page_range": "sheet_page_range",
    "predecessor": "predecessor_id", "successor": "successor_id",
    "signature": "signature_status", "подпись": "signature_status",
    "object": "object_id", "объект": "object_id",
}
_STAGE_WORDS = {
    "PD": "PD", "П": "PD", "ПД": "PD", "PROJECT": "PD", "ПРОЕКТНАЯ": "PD",
    "RD": "RD", "Р": "RD", "РД": "RD", "WORKING": "RD", "РАБОЧАЯ": "RD",
    "ID": "ID", "ИД": "ID", "AS_BUILT": "ID", "ИСПОЛНИТЕЛЬНАЯ": "ID",
}
_IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_IGNORED_DIRS = {"__macosx", ".git", ".svn"}
_HASH_CHUNK = 4 * 1024 * 1024


class PackageError(ValueError):
    """The package or its registry cannot be read (a user error, not a crash)."""


@dataclass(frozen=True)
class PackageFile:
    relative_path: str   # POSIX, relative to the package root
    size: int
    sha256: str

    @property
    def is_pdf(self) -> bool:
        return self.relative_path.lower().endswith(".pdf")


@dataclass
class PackagePlan:
    object_id: str
    rows: list[dict[str, Any]]                      # manifest rows, import order
    report: dict[str, Any]                          # deterministic, goes into result.json
    links: list[tuple[str, str, str]] = field(default_factory=list)   # (file_id, "predecessor"/"successor", file_id)


# --------------------------------------------------------------------------------------------- package on disk


def resolve_package(docs: str | Path, *, unpack_root: Path, max_unpacked_bytes: int = 0) -> tuple[Path, str]:
    """(package root directory, package name) for a folder or a ZIP. A ZIP is unpacked once below `unpack_root`
    into a directory named by its SHA-256, so a re-run reuses it and two different ZIPs never mix."""
    path = Path(docs)
    if path.is_dir():
        return path.resolve(), path.resolve().name
    if path.is_file() and zipfile.is_zipfile(path):
        return unpack_zip(path, unpack_root, max_unpacked_bytes=max_unpacked_bytes), path.stem
    raise PackageError(f"--docs must be a folder or a ZIP archive: {path}")


def unpack_zip(zip_path: Path, unpack_root: Path, *, max_unpacked_bytes: int = 0) -> Path:
    digest = sha256_file(zip_path)
    target = (unpack_root / f"zip-{digest[:16]}").resolve()
    marker = target / ".unpacked"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        return target
    target.mkdir(parents=True, exist_ok=True)
    total = 0
    # Windows archivers write Cyrillic names in cp866 without the UTF-8 flag; names that carry the flag are
    # decoded as UTF-8 regardless of `metadata_encoding`.
    with zipfile.ZipFile(zip_path, metadata_encoding="cp866") as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir() or _is_symlink(info):
                continue
            relative = _safe_member_path(info.filename)
            if relative is None:
                continue
            total += int(info.file_size)
            if max_unpacked_bytes and total > max_unpacked_bytes:
                raise PackageError(
                    f"ZIP unpacks to more than {max_unpacked_bytes} bytes (CASE10_BATCH_MAX_UNPACKED_BYTES)")
            destination = (target / relative).resolve()
            if not destination.is_relative_to(target):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as out:
                while chunk := source.read(_HASH_CHUNK):
                    out.write(chunk)
    marker.write_text(digest, encoding="utf-8")
    return target


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _safe_member_path(name: str) -> str | None:
    """A ZIP member name as a safe relative POSIX path, or None (absolute, drive-qualified or `..` members are
    dropped -- zip-slip)."""
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0] or name.startswith(("/", "\\")):
        return None
    if any(part.lower() in _IGNORED_DIRS for part in parts):
        return None
    return "/".join(parts)


def scan_package(root: Path) -> list[PackageFile]:
    """Every regular file under `root`, sorted by relative path (never file-system order), with its SHA-256."""
    root = root.resolve()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name.lower() not in _IGNORED_DIRS and not name.startswith("."))
        for name in filenames:
            if name.lower() in _IGNORED_NAMES or name.startswith("."):
                continue
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            found.append(full.relative_to(root).as_posix())
    found.sort(key=_path_sort_key)
    return [PackageFile(rel, (root / rel).stat().st_size, sha256_file(root / rel)) for rel in found]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _path_sort_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


# ---------------------------------------------------------------------------------------------------- registry


def read_registry(path: str | Path) -> list[dict[str, Any]]:
    """Registry rows with Perechen 1.1 field names (unknown columns are kept under their normalized name)."""
    path = Path(path)
    if not path.is_file():
        raise PackageError(f"registry not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        table = _xlsx_first_sheet(path)
    elif suffix in (".json", ".jsonl"):
        return [_normalize_row(row) for row in _json_rows(path)]
    else:
        table = _csv_table(path)
    if not table:
        return []
    header = [_header_key(cell) for cell in table[0]]
    rows = []
    for raw in table[1:]:
        if not any(str(cell or "").strip() for cell in raw):
            continue
        rows.append(_normalize_row({header[i]: raw[i] if i < len(raw) else None for i in range(len(header)) if header[i]}))
    return rows


def _header_key(value: Any) -> str:
    key = re.sub(r"[\s\-/.]+", "_", str(value or "").strip().lower()).strip("_")
    return _HEADER_ALIASES.get(key, key)


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        key = _header_key(key)
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            continue
        out[key] = value
    if "doc_stage" in out:
        out["doc_stage"] = normalize_stage(out["doc_stage"]) or str(out["doc_stage"]).upper()
    if "approval_status" in out:
        out["approval_status"] = str(out["approval_status"]).strip().upper().replace(" ", "_")
    if "sha256" in out:
        out["sha256"] = str(out["sha256"]).strip().lower()
    if "file_name" in out:
        out["file_name"] = str(out["file_name"]).replace("\\", "/").strip("/")
    for key in ("file_id", "object_id", "predecessor_id", "successor_id", "document_code", "revision", "discipline"):
        if key in out:
            out[key] = _text(out[key])
    if "approval_date" in out:
        out["approval_date"] = _iso_date(out["approval_date"])
    return out


def normalize_stage(value: Any) -> str | None:
    words = re.findall(r"[0-9a-zа-яё_]+", str(value or "").upper(), flags=re.IGNORECASE)
    for word in words:
        stage = _STAGE_WORDS.get(word.upper())
        if stage:
            return stage
    return None


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _iso_date(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # XLSX serial date (1900 system)
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    text = str(value).strip()
    for candidate, pattern in ((text[:10], "%Y-%m-%d"), (text[:10], "%d.%m.%Y"), (text[:8], "%d.%m.%y"),
                               (text[:10], "%d/%m/%Y")):
        try:
            return datetime.strptime(candidate, pattern).date().isoformat()
        except ValueError:
            continue
    return text or None


def _csv_table(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise PackageError(f"registry is neither UTF-8 nor cp1251: {path}")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _json_rows(path: Path) -> Iterable[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, dict):
        for key in ("files", "documents", "registry", "items", "rows"):
            if isinstance(payload.get(key), list):
                inherited = {k: v for k, v in payload.items() if not isinstance(v, (list, dict))}
                return [{**inherited, **row} for row in payload[key] if isinstance(row, dict)]
        raise PackageError(f"JSON registry has no list of files: {path}")
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise PackageError(f"JSON registry must be a list or an object with 'files': {path}")


_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_XLSX_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _xlsx_first_sheet(path: Path) -> list[list[Any]]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", _XLSX_NS):
                shared.append("".join(node.text or "" for node in item.iter(f"{{{_XLSX_NS['m']}}}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        first = workbook.find("m:sheets/m:sheet", _XLSX_NS)
        sheet_path = "xl/worksheets/sheet1.xml"
        if first is not None and "xl/_rels/workbook.xml.rels" in names:
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                if rel.get("Id") == first.get(_XLSX_REL):
                    target = rel.get("Target", "").lstrip("/")
                    sheet_path = target if target.startswith("xl/") else f"xl/{target}"
        sheet = ET.fromstring(archive.read(sheet_path))
    table: list[list[Any]] = []
    for row in sheet.iter(f"{{{_XLSX_NS['m']}}}row"):
        values: dict[int, Any] = {}
        for cell in row.findall("m:c", _XLSX_NS):
            column = _xlsx_column(cell.get("r", "A1"))
            kind = cell.get("t")
            if kind == "inlineStr":
                values[column] = "".join(node.text or "" for node in cell.iter(f"{{{_XLSX_NS['m']}}}t"))
                continue
            node = cell.find("m:v", _XLSX_NS)
            if node is None or node.text is None:
                continue
            if kind == "s":
                values[column] = shared[int(node.text)]
            elif kind in ("str", "b", "e"):
                values[column] = node.text
            else:
                number = float(node.text)
                values[column] = int(number) if number.is_integer() else number
        if values:
            table.append([values.get(index) for index in range(max(values) + 1)])
    return table


def _xlsx_column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    index = 0
    for char in letters.group(0) if letters else "A":
        index = index * 26 + (ord(char) - 64)
    return index - 1


# ----------------------------------------------------------------------------------------------- manifest plan


def build_plan(
    files: list[PackageFile],
    registry: list[dict[str, Any]] | None,
    *,
    package_name: str,
    object_id: str | None = None,
    registry_sha256: str | None = None,
    originals_prefix: str = "",
) -> PackagePlan:
    """Match registry rows to files and produce the manifest rows the pipeline imports.

    Matching: by SHA-256 when the registry gives one, else by `file_name` (exact relative path, then a unique
    base name). Stage: the registry's `doc_stage`, else the package's own folder names (`stage_from_package_path`,
    the same rule the live mode uses for official objects). `file_id`: the registry's, else `F-` + the first 16
    hex digits of the file's SHA-256 (content-addressed, so independent of names and listing order).

    `originals_prefix`: where the package root sits below CASE10_ORIGINALS_ROOT (the HTTP path keeps every package
    under one root; the CLI points the root at the package itself, prefix ""). Stage is always read from the
    package-relative path, so the name of the folder a package was dropped into never decides a stage."""
    from .domain.official_dataset import stage_from_package_path

    registry = list(registry or [])
    issues: list[dict[str, Any]] = []
    object_ids = sorted({str(row["object_id"]) for row in registry if row.get("object_id")})
    if object_id is None:
        if len(object_ids) > 1:
            raise PackageError(f"registry names several objects {object_ids}; choose one with --object-id")
        object_id = object_ids[0] if object_ids else _object_id_from_name(package_name)
    if registry and object_ids and object_id not in object_ids:
        raise PackageError(f"--object-id {object_id!r} is not in the registry (it names {object_ids})")
    registry = [row for row in registry if not row.get("object_id") or row.get("object_id") == object_id]

    by_sha: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    by_base: dict[str, list[int]] = {}
    for index, row in enumerate(registry):
        if row.get("sha256"):
            by_sha.setdefault(row["sha256"], []).append(index)
        if row.get("file_name"):
            name = _path_sort_key(row["file_name"])
            by_name.setdefault(name, []).append(index)
            by_base.setdefault(PurePosixPath(name).name, []).append(index)

    used: set[int] = set()
    rows: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}
    for item in files:
        reg_index = _match(item, by_sha, by_name, by_base, used)
        reg = registry[reg_index] if reg_index is not None else None
        if reg_index is not None:
            used.add(reg_index)
        file_issues: list[str] = []
        if registry and reg is None:
            file_issues.append("NOT_IN_REGISTRY")
        if reg is not None and reg.get("sha256") and reg["sha256"] != item.sha256:
            file_issues.append("SHA256_MISMATCH")
        if reg is not None and reg.get("approval_status") and reg["approval_status"] not in APPROVAL_STATUSES:
            file_issues.append("UNKNOWN_APPROVAL_STATUS")
        stage = (reg or {}).get("doc_stage") if (reg or {}).get("doc_stage") in ("PD", "RD", "ID") else None
        stage_source = "REGISTRY" if stage else "PACKAGE_PATH"
        if stage is None:
            stage = stage_from_package_path(item.relative_path)
        file_id = str((reg or {}).get("file_id") or f"F-{item.sha256[:16]}")
        if file_id in seen_ids:
            file_issues.append("DUPLICATE_FILE_ID" if reg and reg.get("file_id") else "DUPLICATE_CONTENT")
            issues.append({"file": item.relative_path, "issues": file_issues, "same_file_id_as": seen_ids[file_id]})
            continue   # one DocumentVersion per (object, file_id): identical content is read once
        seen_ids[file_id] = item.relative_path
        registry_view = {key: reg[key] for key in REGISTRY_FIELDS if reg and key in reg}
        row = {
            "file_id": file_id,
            "object_id": object_id,
            "split": PACKAGE_SPLIT,
            "stage": stage,
            "stage_source": stage_source,
            "sha256": item.sha256,
            "size_bytes": item.size,
            "relative_path": f"{originals_prefix.strip('/')}/{item.relative_path}".lstrip("/"),
            "package_path": item.relative_path,
            "registry": registry_view or None,
            "registry_issues": file_issues or None,
        }
        if reg and reg.get("discipline"):
            row["section"] = reg["discipline"]
        if reg and reg.get("approval_status"):
            row["approval_status"] = reg["approval_status"]
        rows.append(row)
        if file_issues:
            issues.append({"file": item.relative_path, "issues": file_issues})

    missing = [
        {"file_id": row.get("file_id"), "file_name": row.get("file_name"), "doc_stage": row.get("doc_stage")}
        for index, row in enumerate(registry) if index not in used
    ]
    links = []
    known_ids = {row["file_id"] for row in rows}
    for row in rows:
        reg = row.get("registry") or {}
        for kind in ("predecessor", "successor"):
            other = reg.get(f"{kind}_id")
            if other and other in known_ids and other != row["file_id"]:
                links.append((row["file_id"], kind, other))
            elif other and other not in known_ids:
                issues.append({"file": row["package_path"], "issues": [f"{kind.upper()}_NOT_IN_PACKAGE"], "ref": other})

    if registry_sha256 is None:
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_MISSING"
    elif any(set(entry["issues"]) & {"NOT_IN_REGISTRY", "SHA256_MISMATCH", "DUPLICATE_FILE_ID"} for entry in issues):
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_DOES_NOT_MATCH_PACKAGE"
    else:
        status, reason = STATUS_ACCEPTED, None
    stages: dict[str, int] = {}
    for row in rows:
        stages[row["stage"]] = stages.get(row["stage"], 0) + 1
    report = {
        "package_name": package_name,
        "object_id": object_id,
        "registry_status": status,
        "registry_status_reason": reason,
        "registry_sha256": registry_sha256,
        "files_total": len(files),
        "files_imported": len(rows),
        "files_pdf": sum(1 for item in files if item.is_pdf),
        "bytes_total": sum(item.size for item in files),
        "documents_by_stage": dict(sorted(stages.items())),
        "files": [
            {"file_id": row["file_id"], "relative_path": row["package_path"], "sha256": row["sha256"],
             "stage": row["stage"], "stage_source": row["stage_source"]}
            for row in rows
        ],
        "issues": sorted(issues, key=lambda entry: (entry["file"], entry["issues"])),
        "registry_rows_without_file": missing,
    }
    return PackagePlan(object_id=object_id, rows=rows, report=report, links=sorted(links))


def _match(item: PackageFile, by_sha, by_name, by_base, used: set[int]) -> int | None:
    """Registry row for a file: its SHA-256 first (the first unused row when the registry lists identical content
    twice), else a unique match on the relative path, then on the base name. A name match with a different SHA-256
    is still a match -- `build_plan` then flags SHA256_MISMATCH instead of silently treating it as a new file."""
    free = [index for index in by_sha.get(item.sha256, ()) if index not in used]
    if free:
        return free[0]
    key = _path_sort_key(item.relative_path)
    for candidates in (by_name.get(key), by_base.get(PurePosixPath(key).name)):
        free = [index for index in candidates or () if index not in used]
        if len(free) == 1:
            return free[0]
    return None


def _object_id_from_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", unicodedata.normalize("NFC", name)).strip("-._")
    return (cleaned or "OBJECT")[:64]


# ---------------------------------------------------------------------------------------------------- import


def import_plan(db, plan: PackagePlan, *, project_id: int, organization_id: int, manifest_path: Path) -> int:
    """Write the manifest as JSONL and import it through the same path the official dataset uses, then apply the
    registry fields that path does not carry (document code, revision, approval date, revision chain). Returns the
    number of documents imported."""
    from .db.models import DocumentVersion
    from .domain.official_dataset import _import_document_manifest

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as stream:
        for row in plan.rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    _import_document_manifest(db, manifest_path, project_id=project_id, organization_id=organization_id,
                              object_ids={plan.object_id})
    docs = {
        str(doc.dataset_file_id): doc
        for doc in db.query(DocumentVersion).filter(
            DocumentVersion.project_id == int(project_id),
            DocumentVersion.organization_id == int(organization_id),
            DocumentVersion.object_id == plan.object_id,
            DocumentVersion.dataset_split == PACKAGE_SPLIT,
        )
    }
    for row in plan.rows:
        doc = docs.get(row["file_id"])
        reg = row.get("registry") or {}
        if doc is None or not reg:
            continue
        if reg.get("document_code"):
            doc.document_code = str(reg["document_code"])[:255]
        if reg.get("revision"):
            doc.revision = str(reg["revision"])[:64]
        if reg.get("approval_date"):
            try:
                doc.approval_date = datetime.fromisoformat(str(reg["approval_date"]))
            except ValueError:
                pass
        db.add(doc)
    for file_id, kind, other in plan.links:
        doc, other_doc = docs.get(file_id), docs.get(other)
        if doc is None or other_doc is None:
            continue
        if kind == "successor":
            doc.successor_id, other_doc.predecessor_id = int(other_doc.id), int(doc.id)
        else:
            doc.predecessor_id, other_doc.successor_id = int(other_doc.id), int(doc.id)
        db.add(doc)
        db.add(other_doc)
    db.flush()
    return len(docs)
