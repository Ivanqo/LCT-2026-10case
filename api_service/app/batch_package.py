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
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from typing import Any, Iterable

from .robustness import default_max_unpacked_bytes, robust_inputs_enabled

PACKAGE_SPLIT = "BATCH_PACKAGE"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"

REGISTRY_FIELDS = (
    "object_id", "file_id", "file_name", "sha256", "doc_stage", "discipline", "document_code", "revision",
    "approval_status", "approval_date", "sheet_page_range", "predecessor_id", "successor_id", "signature_status",
)
APPROVAL_STATUSES = ("DRAFT", "APPROVED", "FOR_CONSTRUCTION", "SIGNED", "SUPERSEDED", "CANCELLED")

# Header spellings seen in practice -> the Perechen 1.1 field. Keys are normalized by `_header_key`.
_HEADER_ALIASES = {
    "sha_256": "sha256", "sha": "sha256", "hash": "sha256", "checksum": "sha256", "контрольная_сумма": "sha256",
    "sha256_hash": "sha256", "хеш": "sha256", "хэш": "sha256",
    "filename": "file_name", "file": "file_name", "document_name": "file_name", "name": "file_name",
    "path": "file_name", "relative_path": "file_name", "file_path": "file_name", "filepath": "file_name",
    "имя": "file_name", "имя_файла": "file_name", "наименование": "file_name",
    "название_файла": "file_name", "наименование_файла": "file_name", "файл": "file_name",
    "путь_к_файлу": "file_name", "относительный_путь": "file_name",
    "file_hash": "sha256", "хэш_файла": "sha256", "хеш_файла": "sha256",
    "stage": "doc_stage", "document_stage": "doc_stage", "development_stage": "doc_stage",
    "stage_code": "doc_stage", "стадия": "doc_stage", "стадия_разработки": "doc_stage",
    "стадия_документа": "doc_stage", "стадия_документации": "doc_stage", "этап": "doc_stage",
    "этап_документации": "doc_stage",
    "section": "discipline", "section_code": "discipline", "discipline_code": "discipline",
    "discipline_name": "discipline", "project_section": "discipline", "section_mark": "discipline",
    "раздел": "discipline", "раздел_проекта": "discipline", "код_раздела": "discipline",
    "код_раздела_проекта": "discipline", "обозначение_раздела_проекта": "discipline",
    "обозначение_раздела": "discipline", "марка": "discipline",
    "code": "document_code", "document_number": "document_code", "document_code": "document_code",
    "шифр": "document_code", "шифр_документа": "document_code", "код_документа": "document_code",
    "rev": "revision", "revision_number": "revision", "edition": "revision", "редакция": "revision",
    "редакция_документа": "revision", "номер_редакции": "revision", "изм": "revision", "номер_изменения": "revision",
    "status": "approval_status", "approval": "approval_status", "approval_state": "approval_status",
    "approval_status_value": "approval_status", "document_status": "approval_status",
    "статус": "approval_status", "статус_документа": "approval_status",
    "статус_утверждения": "approval_status", "статус_согласования": "approval_status",
    "date": "approval_date", "approval_timestamp": "approval_date", "дата": "approval_date",
    "дата_утверждения": "approval_date", "дата_согласования": "approval_date",
    "pages": "sheet_page_range", "page_range": "sheet_page_range", "sheet_pages": "sheet_page_range",
    "листы": "sheet_page_range", "диапазон_листов": "sheet_page_range",
    "predecessor": "predecessor_id", "предшественник": "predecessor_id",
    "successor": "successor_id", "последователь": "successor_id", "следующий_документ": "successor_id",
    "signature": "signature_status", "подпись": "signature_status", "статус_подписи": "signature_status",
    "object": "object_id", "object_code": "object_id", "объект": "object_id", "идентификатор_объекта": "object_id",
    "id_объекта": "object_id", "file_id": "file_id", "идентификатор_файла": "file_id",
}
# These aliases are still parsed for compatibility, but are surfaced so callers can detect
# registry schema drift in organizer files and fix the source rather than silently relying on it.
_REGISTRY_HEADER_REVIEW_KEYS = frozenset({
    "статус_документа", "стадия_разработки", "код_раздела_проекта",
})
_STAGE_WORDS = {
    "PD": "PD", "P": "PD", "П": "PD", "ПД": "PD", "PROJECT": "PD", "ПРОЕКТ": "PD", "ПРОЕКТНАЯ": "PD",
    "RD": "RD", "W": "RD", "Р": "RD", "РД": "RD", "WORKING": "RD", "РАБОЧАЯ": "RD",
    "ID": "ID", "A": "ID", "ИД": "ID", "AS_BUILT": "ID", "ASBUILT": "ID",
    "EXECUTIVE": "ID", "ИСПОЛНИТЕЛЬНАЯ": "ID",
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
    pdf_pages: int | None = None   # page count of a readable PDF (None: not a PDF, or unreadable)
    excluded_reason: str | None = None
    processing_issue: str | None = None

    @property
    def is_pdf(self) -> bool:
        return self.relative_path.lower().endswith(".pdf")


@dataclass
class PackagePlan:
    object_id: str
    rows: list[dict[str, Any]]                      # manifest rows, import order
    report: dict[str, Any]                          # deterministic, goes into result.json
    links: list[tuple[str, str, str]] = field(default_factory=list)   # (file_id, "predecessor"/"successor", file_id)


class RegistryRows(list[dict[str, Any]]):
    """List-compatible registry data plus header names that are not in the supported schema."""

    def __init__(self, rows=(), *, unrecognized_columns=()):
        super().__init__(rows)
        self.unrecognized_columns = tuple(sorted(set(unrecognized_columns)))


# --------------------------------------------------------------------------------------------- package on disk


def resolve_package(
    docs: str | Path, *, unpack_root: Path, max_unpacked_bytes: int | None = None,
) -> tuple[Path, str]:
    """(package root directory, package name) for a folder or a ZIP. A ZIP is unpacked once below `unpack_root`
    into a directory named by its SHA-256, so a re-run reuses it and two different ZIPs never mix."""
    path = Path(docs)
    if path.is_dir():
        return path.resolve(), path.resolve().name
    if path.is_file() and zipfile.is_zipfile(path):
        if max_unpacked_bytes is None:
            max_unpacked_bytes = default_max_unpacked_bytes() if robust_inputs_enabled() else 0
        return unpack_zip(path, unpack_root, max_unpacked_bytes=max_unpacked_bytes), path.stem
    raise PackageError(f"--docs must be a folder or a ZIP archive: {path}")


def unpack_zip(zip_path: Path, unpack_root: Path, *, max_unpacked_bytes: int | None = None) -> Path:
    if not robust_inputs_enabled():
        return _unpack_zip_legacy(zip_path, unpack_root, max_unpacked_bytes=max_unpacked_bytes or 0)
    if max_unpacked_bytes is None:
        max_unpacked_bytes = default_max_unpacked_bytes()
    digest = sha256_file(zip_path)
    unpack_root = unpack_root.resolve()
    target = (unpack_root / f"zip-{digest[:16]}").resolve()
    if not target.is_relative_to(unpack_root):
        raise PackageError("ZIP extraction path is outside the unpack directory")
    marker = target / ".unpacked"
    exclusions_path = unpack_root / f".{target.name}.exclusions.json"
    if (marker.is_file() and exclusions_path.is_file()
            and marker.read_text(encoding="utf-8").strip() == digest):
        return target
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    total = 0
    excluded: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    # Windows archivers write Cyrillic names in cp866 without the UTF-8 flag; names that carry the flag are
    # decoded as UTF-8 regardless of `metadata_encoding`.
    try:
        archive_context = zipfile.ZipFile(zip_path, metadata_encoding="cp866")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PackageError(f"cannot read ZIP package {zip_path} ({type(exc).__name__})") from exc
    try:
        with archive_context as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if _is_symlink(info):
                    excluded.append({"file": info.filename, "reason": "SYMLINK_MEMBER"})
                    continue
                relative = _safe_member_path(info.filename)
                if relative is None:
                    excluded.append({"file": info.filename, "reason": _member_exclusion_reason(info.filename)})
                    continue
                if info.is_dir():
                    continue
                path_key = _path_sort_key(relative)
                if path_key in seen_paths:
                    excluded.append({"file": info.filename, "reason": "DUPLICATE_MEMBER_PATH"})
                    continue
                seen_paths.add(path_key)
                declared_size = int(info.file_size)
                if max_unpacked_bytes > 0 and total + declared_size > max_unpacked_bytes:
                    raise PackageError(
                        f"ZIP unpacks to more than {max_unpacked_bytes} bytes (CASE10_BATCH_MAX_UNPACKED_BYTES)")
                destination = (target / relative).resolve()
                if not destination.is_relative_to(target):
                    excluded.append({"file": info.filename, "reason": "PATH_ESCAPES_EXTRACTION_ROOT"})
                    continue
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as out:
                        while chunk := source.read(_HASH_CHUNK):
                            total += len(chunk)
                            if max_unpacked_bytes > 0 and total > max_unpacked_bytes:
                                raise PackageError(
                                    f"ZIP unpacks to more than {max_unpacked_bytes} bytes "
                                    "(CASE10_BATCH_MAX_UNPACKED_BYTES)")
                            out.write(chunk)
                except PackageError:
                    raise
                except (FileExistsError, IsADirectoryError, NotADirectoryError):
                    if destination.is_file():
                        destination.unlink(missing_ok=True)
                    excluded.append({"file": info.filename, "reason": "PATH_CONFLICT"})
                except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error):
                    destination.unlink(missing_ok=True)
                    excluded.append({"file": info.filename, "reason": "CORRUPT_ZIP_MEMBER"})
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise PackageError(
                            f"not enough disk space while extracting ZIP member {info.filename!r}") from exc
                    destination.unlink(missing_ok=True)
                    excluded.append({"file": info.filename, "reason": "ZIP_MEMBER_IO_ERROR"})
    except PackageError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError) as exc:
        raise PackageError(f"cannot read ZIP package {zip_path} ({type(exc).__name__})") from exc
    exclusions_path.parent.mkdir(parents=True, exist_ok=True)
    exclusions_path.write_text(json.dumps(excluded, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    marker.write_text(digest, encoding="utf-8")
    return target


def _unpack_zip_legacy(zip_path: Path, unpack_root: Path, *, max_unpacked_bytes: int = 0) -> Path:
    """Pre-gate extraction semantics, retained for byte-identical validation runs."""
    digest = sha256_file(zip_path)
    target = (unpack_root / f"zip-{digest[:16]}").resolve()
    marker = target / ".unpacked"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        return target
    target.mkdir(parents=True, exist_ok=True)
    total = 0
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
    if _member_exclusion_reason(name) != "":
        return None
    return "/".join(parts)


def _member_exclusion_reason(name: str) -> str:
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        return "EMPTY_MEMBER_PATH"
    if normalized.startswith("/") or name.startswith("\\"):
        return "ABSOLUTE_MEMBER_PATH"
    if ".." in parts:
        return "PARENT_TRAVERSAL"
    if ":" in parts[0]:
        return "DRIVE_QUALIFIED_PATH"
    if any(part.lower() in _IGNORED_DIRS for part in parts):
        return "IGNORED_METADATA_DIRECTORY"
    return ""


def package_exclusions(root: str | Path) -> list[dict[str, str]]:
    """Excluded ZIP members, if this package root was produced by :func:`unpack_zip`."""
    if not robust_inputs_enabled():
        return []
    root = Path(root).resolve()
    if not (root / ".unpacked").is_file():
        return []
    path = root.parent / f".{root.name}.exclusions.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [{"file": path.name, "reason": "EXCLUSION_REPORT_UNREADABLE"}]
    if not isinstance(payload, list):
        return [{"file": path.name, "reason": "EXCLUSION_REPORT_INVALID"}]
    return [item for item in payload if isinstance(item, dict) and item.get("file") and item.get("reason")]


def scan_package(root: Path, *, scan_errors: list[dict[str, str]] | None = None) -> list[PackageFile]:
    """Every regular file in sorted path order; hash supported PDFs and mark other extensions for exclusion."""
    root = root.resolve()
    found: list[str] = []
    collected_errors = scan_errors if scan_errors is not None else []

    def onerror(exc: OSError) -> None:
        if not robust_inputs_enabled():
            return
        failed = getattr(exc, "filename", None) or str(root)
        try:
            relative = Path(failed).relative_to(root).as_posix()
        except (ValueError, OSError):
            relative = str(failed)
        collected_errors.append({"file": relative, "reason": "DIRECTORY_READ_ERROR"})

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror if robust_inputs_enabled() else None):
        dirnames[:] = sorted(name for name in dirnames if name.lower() not in _IGNORED_DIRS and not name.startswith("."))
        for name in filenames:
            if name.lower() in _IGNORED_NAMES or name.startswith("."):
                continue
            full = Path(dirpath) / name
            try:
                if full.is_symlink() or not full.is_file():
                    continue
            except OSError:
                if not robust_inputs_enabled():
                    raise
                collected_errors.append({"file": full.relative_to(root).as_posix(), "reason": "FILE_METADATA_ERROR"})
                continue
            found.append(full.relative_to(root).as_posix())
    if robust_inputs_enabled():
        found.sort(key=lambda relative: (_path_sort_key(relative), relative))
    else:
        found.sort(key=_path_sort_key)
    if not robust_inputs_enabled():
        return [PackageFile(rel, (root / rel).stat().st_size, _package_file_sha256(root / rel), pdf_page_count(root / rel))
                for rel in found]
    result = []
    for relative in found:
        path = root / relative
        is_pdf = Path(relative).suffix.lower() == ".pdf"
        size = 0
        try:
            size = path.stat().st_size
            sha256 = _package_file_sha256(path) if is_pdf else ""
        except OSError:
            result.append(PackageFile(relative, size, "", None, "FILE_READ_ERROR"))
            continue
        pdf_pages, processing_issue = _package_pdf_info(path) if is_pdf else (None, None)
        result.append(PackageFile(
            relative, size, sha256,
            pdf_pages,
            None if is_pdf else "UNSUPPORTED_EXTENSION",
            processing_issue,
        ))
    return result


def _pdf_scan_info(path: Path) -> tuple[int | None, str | None]:
    """Page count of a PDF (PyMuPDF reads the xref only), recorded in the manifest row as `pdf_pages`. The live
    tagger and the rule-pack fallback size their page ranges and the deterministic page budget from it
    (`dataset_sources.document_page_count_hint`); without it every document counts as 60 pages -- longer ones would
    be cut at page 60 and a package of more than 500 PDFs would exhaust the 30 000-page budget early."""
    if not str(path).lower().endswith(".pdf"):
        return None, None
    try:
        import fitz

        with fitz.open(path) as document:
            if robust_inputs_enabled() and document.needs_pass:
                return None, "PDF_PASSWORD_REQUIRED"
            page_count = int(document.page_count)
            if robust_inputs_enabled() and bool(getattr(document, "is_repaired", False)):
                return page_count, "PDF_REPAIRED_DURING_OPEN"
            return page_count, None
    except Exception:  # noqa: BLE001 -- unreadable/encrypted PDF: the pipeline reports it, the scan must not stop
        return None, "PDF_UNREADABLE"


def pdf_page_count(path: Path) -> int | None:
    return _pdf_scan_info(path)[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _package_file_sha256(path: Path) -> str:
    """`sha256_file` for the package scan, served from the stat-keyed digest cache when one is configured."""
    from .domain.file_hash_cache import cached_sha256

    return cached_sha256(path, sha256_file)


def _package_pdf_info(path: Path) -> tuple[int | None, str | None]:
    """`_pdf_scan_info` for the package scan, served from the stat-keyed file-fact cache when one is configured (the
    answer also depends on the PyMuPDF build and on the robust-input switch, so both are in the cache kind)."""
    import fitz

    from .domain.file_hash_cache import cached_fact

    kind = f"pdf_info/{getattr(fitz, 'VersionBind', '')}/{int(robust_inputs_enabled())}"
    value = cached_fact(path, kind, lambda p: list(_pdf_scan_info(p)),
                        valid=lambda v: isinstance(v, list) and len(v) == 2)
    return (value[0], value[1])


def _path_sort_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


# ---------------------------------------------------------------------------------------------------- registry


def read_registry(path: str | Path) -> list[dict[str, Any]]:
    """Registry rows with Perechen 1.1 field names (unknown columns are kept under their normalized name)."""
    path = Path(path)
    if not path.is_file():
        raise PackageError(f"registry not found: {path}")
    try:
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xlsm"):
            table = _xlsx_first_sheet(path)
        elif suffix in (".json", ".jsonl"):
            raw_rows = _json_rows(path)
            unrecognized = {
                str(key).strip() for row in raw_rows for key in row
                if _header_key(key) not in REGISTRY_FIELDS or _raw_header_key(key) in _REGISTRY_HEADER_REVIEW_KEYS
            }
            return RegistryRows((_normalize_row(row) for row in raw_rows), unrecognized_columns=unrecognized)
        else:
            table = _csv_table(path)
    except PackageError:
        raise
    except Exception as exc:  # noqa: BLE001 -- malformed user-supplied registries are input errors
        if not robust_inputs_enabled():
            raise
        detail = str(exc).strip()[:200]
        suffix = f": {detail}" if detail else ""
        raise PackageError(f"cannot read registry {path} ({type(exc).__name__}){suffix}") from exc
    if not table:
        return RegistryRows()
    header = [_header_key(cell) for cell in table[0]]
    unrecognized = {
        str(cell).strip() for cell, key in zip(table[0], header)
        if key and (key not in REGISTRY_FIELDS or _raw_header_key(cell) in _REGISTRY_HEADER_REVIEW_KEYS)
    }
    rows = []
    for raw in table[1:]:
        if not any(str(cell or "").strip() for cell in raw):
            continue
        rows.append(_normalize_row({header[i]: raw[i] if i < len(raw) else None for i in range(len(header)) if header[i]}))
    return RegistryRows(rows, unrecognized_columns=unrecognized)


def _header_key(value: Any) -> str:
    key = _raw_header_key(value)
    return _HEADER_ALIASES.get(key, key)


def _raw_header_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я_]+", "_", text, flags=re.IGNORECASE).strip("_")


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
        out["approval_status"] = normalize_approval_status(out["approval_status"])
    if "sha256" in out:
        out["sha256"] = str(out["sha256"]).strip().lower()
    if "file_name" in out:
        out["file_name"] = str(out["file_name"]).replace("\\", "/")
    for key in ("file_id", "object_id", "predecessor_id", "successor_id", "document_code", "revision", "discipline"):
        if key in out:
            out[key] = _text(out[key])
    if out.get("discipline"):
        from .domain.document_facts import canonical_manifest_section

        out["discipline"] = canonical_manifest_section(out["discipline"]) or out["discipline"]
    if "approval_date" in out:
        out["approval_date"] = _iso_date(out["approval_date"])
    return out


def normalize_stage(value: Any) -> str | None:
    words = re.findall(r"[0-9a-zа-яё_]+", str(value or "").upper(), flags=re.IGNORECASE)
    stages = {_STAGE_WORDS[word.upper()] for word in words if word.upper() in _STAGE_WORDS}
    # Labels such as "Стадия П/Р" describe a combined/ambiguous stage. Do not
    # silently select whichever token happened to come first.
    return next(iter(stages)) if len(stages) == 1 else None


def normalize_approval_status(value: Any) -> str:
    """Map common Russian, English and Perechen 1.1 approval labels to the internal status vocabulary."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold().replace("ё", "е")
    key = re.sub(r"[^0-9a-zа-я]+", "_", text, flags=re.IGNORECASE).strip("_").upper()
    aliases = {
        "APPROVED": "APPROVED", "УТВЕРЖДЕН": "APPROVED", "УТВЕРЖДЕНО": "APPROVED",
        "УТВЕРЖДЕНА": "APPROVED", "СОГЛАСОВАН": "APPROVED", "СОГЛАСОВАНО": "APPROVED",
        "SIGNED": "SIGNED", "ПОДПИСАН": "SIGNED", "ПОДПИСАНО": "SIGNED",
        "FOR_CONSTRUCTION": "FOR_CONSTRUCTION", "ДЕЙСТВУЕТ": "FOR_CONSTRUCTION", "ДЕЙСТВУЮЩИЙ": "FOR_CONSTRUCTION",
        "В_ПРОИЗВОДСТВО_РАБОТ": "FOR_CONSTRUCTION",
        "SUPERSEDED": "SUPERSEDED", "ЗАМЕНЕН": "SUPERSEDED", "ЗАМЕНЕНА": "SUPERSEDED",
        "УТРАТИЛ_СИЛУ": "SUPERSEDED",
        "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED", "ОТМЕНЕН": "CANCELLED",
        "ОТМЕНЕНО": "CANCELLED", "АННУЛИРОВАН": "CANCELLED", "АННУЛИРОВАНО": "CANCELLED",
        "DRAFT": "DRAFT", "ЧЕРНОВИК": "DRAFT", "НЕ_УТВЕРЖДЕНО": "DRAFT",
        "НЕИЗВЕСТНО": "UNKNOWN", "НЕИЗВЕСТЕН": "UNKNOWN",
        "UNKNOWN": "UNKNOWN",
    }
    return aliases.get(key, "UNKNOWN")


def _safe_registry_path(value: object) -> bool:
    """Reject paths that are absolute, traverse upward, or contain a Windows drive or NUL."""
    raw = str(value or "").replace("\\", "/")
    path = PurePosixPath(raw)
    return bool(raw) and "\x00" not in raw and not path.is_absolute() and ".." not in path.parts \
        and not re.match(r"^[A-Za-z]:", raw)


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
    scan_errors: list[dict[str, str]] | None = None,
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

    has_registry_headers = hasattr(registry, "unrecognized_columns")
    unrecognized_columns = set(getattr(registry, "unrecognized_columns", ()))
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
    if not has_registry_headers:
        unrecognized_columns.update(
            {str(key) for row in registry for key in row if key not in REGISTRY_FIELDS},
        )
    unrecognized_columns = sorted(unrecognized_columns, key=lambda value: (_path_sort_key(value), value))
    registry_manifest_summary = [
        {
            "doc_stage": str(row.get("doc_stage") or "").upper(),
            "file_id": str(row.get("file_id") or "").strip() or None,
            "file_name": str(row.get("file_name") or "").strip() or None,
            "sha256": str(row.get("sha256") or "").strip().lower() or None,
        }
        for row in registry
        if str(row.get("doc_stage") or "").upper() in {"PD", "RD", "ID"}
    ]

    by_sha: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    by_base: dict[str, list[int]] = {}
    invalid_path_indices: set[int] = set()
    for index, row in enumerate(registry):
        if row.get("sha256"):
            by_sha.setdefault(row["sha256"], []).append(index)
        if row.get("file_name"):
            if robust_inputs_enabled() and not _safe_registry_path(row["file_name"]):
                invalid_path_indices.add(index)
                continue
            name = _path_sort_key(row["file_name"])
            by_name.setdefault(name, []).append(index)
            by_base.setdefault(PurePosixPath(name).name, []).append(index)

    used: set[int] = set()
    rows: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}
    excluded_files: list[dict[str, str]] = []
    for item in files:
        reg_index = _match(item, by_sha, by_name, by_base, used)
        reg = registry[reg_index] if reg_index is not None else None
        if reg_index is not None:
            used.add(reg_index)
        if item.excluded_reason and robust_inputs_enabled():
            excluded_files.append({"file": item.relative_path, "reason": item.excluded_reason})
            continue
        file_issues: list[str] = []
        if registry and reg is None:
            file_issues.append("NOT_IN_REGISTRY")
        if robust_inputs_enabled() and reg is not None and not reg.get("sha256"):
            file_issues.append("REGISTRY_MISSING_SHA256")
        if robust_inputs_enabled() and reg is not None and reg.get("doc_stage") not in ("PD", "RD", "ID"):
            file_issues.append("REGISTRY_MISSING_STAGE")
        if robust_inputs_enabled() and reg_index in invalid_path_indices:
            file_issues.append("INVALID_REGISTRY_PATH")
        if reg is not None and reg.get("sha256") and reg["sha256"] != item.sha256:
            file_issues.append("SHA256_MISMATCH")
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
            "pdf_pages": item.pdf_pages,
            "relative_path": f"{originals_prefix.strip('/')}/{item.relative_path}".lstrip("/"),
            "package_path": item.relative_path,
            "registry": registry_view or None,
            # The summary is one fact about the whole registry. Written into every row it made the manifest (and each
            # document's JSON metadata) O(files^2): 450 KB per row, 584 MB for a 1 300-document package, decoded on every
            # query of the documents. `registry_stage_counts` takes the union over the documents, so the first row
            # carries it for all of them.
            "registry_manifest_summary": (registry_manifest_summary or None) if not rows else None,
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
        {"file_id": row.get("file_id"), "file_name": row.get("file_name"), "doc_stage": row.get("doc_stage"),
         **({"reason": "INVALID_REGISTRY_PATH"} if index in invalid_path_indices else {})}
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

    incomplete_fields = {"REGISTRY_MISSING_SHA256", "REGISTRY_MISSING_STAGE"}
    if registry_sha256 is None:
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_MISSING"
    elif not registry and robust_inputs_enabled():
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_EMPTY"
    elif robust_inputs_enabled() and any(set(entry["issues"]) & incomplete_fields for entry in issues):
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_INCOMPLETE"
    elif missing or any(set(entry["issues"]) & {"NOT_IN_REGISTRY", "SHA256_MISMATCH", "DUPLICATE_FILE_ID", "INVALID_REGISTRY_PATH"} for entry in issues):
        status, reason = STATUS_CLARIFICATION_REQUIRED, "REGISTRY_DOES_NOT_MATCH_PACKAGE"
    else:
        status, reason = STATUS_ACCEPTED, None
    stages: dict[str, int] = {}
    for row in rows:
        stages[row["stage"]] = stages.get(row["stage"], 0) + 1
    if robust_inputs_enabled():
        unregistered_files = sorted(
        (row["package_path"] for row in rows
         if not registry or "NOT_IN_REGISTRY" in (row.get("registry_issues") or [])),
        key=lambda value: (_path_sort_key(value), value),
        )
        approval_unverified = sorted(
        (row["package_path"] for row in rows
         if not (row.get("registry") or {}).get("approval_status")
         or (row.get("registry") or {}).get("approval_status") == "UNKNOWN"),
        key=lambda value: (_path_sort_key(value), value),
        )
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
        "pdf_pages_total": sum(item.pdf_pages or 0 for item in files),
        "pdf_unreadable": sorted(item.relative_path for item in files if item.is_pdf and item.pdf_pages is None),
        "documents_by_stage": dict(sorted(stages.items())),
        "files": [
            {"file_id": row["file_id"], "relative_path": row["package_path"], "sha256": row["sha256"],
             "stage": row["stage"], "stage_source": row["stage_source"], "pdf_pages": row["pdf_pages"]}
            for row in rows
        ],
        "issues": sorted(issues, key=lambda entry: (entry["file"], entry["issues"])),
        "registry_rows_without_file": missing,
    }
    if excluded_files and robust_inputs_enabled():
        report["files_excluded"] = len(excluded_files)
        report["excluded_files"] = sorted(excluded_files, key=lambda entry: (entry["file"], entry["reason"]))
    if robust_inputs_enabled():
        invalid_registry_paths = [
            {"file_id": registry[index].get("file_id"), "file_name": registry[index].get("file_name")}
            for index in sorted(invalid_path_indices)
        ]
        report["unregistered_files"] = unregistered_files
        report["approval_unverified_files"] = approval_unverified
        report["invalid_registry_paths"] = invalid_registry_paths
        warnings: list[dict[str, Any]] = []
        if missing:
            warnings.append({"code": "REGISTRY_ROW_WITHOUT_FILE", "count": len(missing), "rows": missing})
        if unregistered_files:
            warnings.append({"code": "FILE_WITHOUT_REGISTRY_ROW", "count": len(unregistered_files),
                             "files": unregistered_files})
        if approval_unverified:
            warnings.append({"code": "APPROVAL_UNVERIFIED", "count": len(approval_unverified),
                             "files": approval_unverified})
        if invalid_registry_paths:
            warnings.append({"code": "INVALID_REGISTRY_PATH", "count": len(invalid_registry_paths),
                             "rows": invalid_registry_paths})
        report["registry_warnings"] = warnings
        file_statuses = []
        for item in files:
            if not item.is_pdf or not item.processing_issue:
                continue
            status = "LOW_QUALITY" if item.processing_issue == "PDF_REPAIRED_DURING_OPEN" else "PROCESSING_ERROR"
            file_statuses.append({"file": item.relative_path, "status": status, "reason": item.processing_issue})
        if file_statuses:
            report["file_statuses"] = sorted(file_statuses, key=lambda item: item["file"])
        if scan_errors:
            report["scan_errors"] = sorted(scan_errors, key=lambda item: (item.get("file", ""), item.get("reason", "")))
    if unrecognized_columns:
        report["unrecognized_columns"] = unrecognized_columns
        warnings = report.setdefault("registry_warnings", [])
        warnings.append({"code": "UNRECOGNIZED_REGISTRY_COLUMNS", "count": len(unrecognized_columns),
                         "columns": unrecognized_columns})
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
