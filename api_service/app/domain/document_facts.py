"""Deterministic, metadata-only facts about the documents of ONE object: what section a file belongs to,
which project series / revision it carries, whether it is a byte duplicate or an unreadable/service file,
and which files compete as "the" edition of the same section.

Phase 10, prompt B, items 2(б) and 4. Nothing here opens a PDF or reads a label: input is the manifest
row (file id, relative path, stage, section, sha256, size, pages, extension) and the file NAME, so the
result is reproducible byte for byte between two runs and cheap enough to compute on every protocol build.

Design rules (each learnt from a real corpus, see evaluation/decision_layer_b/REPORT.md):
  * Revisions that are TOTALLY ORDERED (all files of one section carry the same numeric marker kind:
    "кор2" < "кор3", "Изм.1" < "Изм.3") resolve to "the newest wins"; the others are `superseded`.
    Речников has original + кор2 + кор3 for almost every PD section -- that is normal practice, not a
    conflict.
  * Revisions that are NOT orderable ("Корр.1" vs "МГЭ" vs "v5", "Изм.2" vs "кор3", "ЖС-РД-270121" vs
    "2025-04-266" series) are a CONFLICT: which file is the approved baseline cannot be told from the
    package, so no verdict may be drawn from any of them (COMPARISON_IMPOSSIBLE, never a violation).
  * "unknown" never proves absence: a file whose section cannot be recognised is `unknown`, and an
    object with unknown files can never be declared to lack a section.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
import re
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------------------------
# Section vocabulary (ПП РФ №87 section abbreviations + working-drawing marks)
# --------------------------------------------------------------------------------------------
_LATIN_TO_CYR = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")

# token -> canonical primary tag
_TOKEN_TAG: dict[str, str] = {
    "ПЗУ": "ПЗУ", "СПЗУ": "ПЗУ", "СПОЗУ": "ПЗУ", "ГП": "ГП", "ПП": "ГП",
    "ПЗ": "ПЗ", "ОПЗ": "ПЗ",
    "АР": "АР",
    "КР": "КР", "КЖ": "КР", "КМ": "КР", "КМД": "КР", "КК": "КР",
    "ПОС": "ПОС", "ППР": "ПОС", "ПОД": "ПОД", "ООС": "ООС",
    "ПБ": "ПБ", "ППМ": "ПБ", "МОПБ": "ПБ",
    "ОДИ": "ОДИ", "ЭЭ": "ЭЭ", "ЗУ": "ЭЭ",
    "ТБЭ": "ТБЭ", "ТБЭО": "ТБЭ", "ТОБЭ": "ТБЭ", "БЭО": "ТБЭ",
    "ТХ": "ТХ", "ЭОМ": "ЭОМ", "ЭС": "ЭОМ", "ЭМ": "ЭОМ", "ЭГ": "ЭОМ", "ЭО": "ЭОМ", "ЭН": "ЭОМ", "МЗ": "ЭОМ",
    "ВК": "ВК", "НВК": "ВК", "НК": "ВК", "НВ": "ВК", "ОВ": "ОВ", "ОВК": "ОВ", "ТС": "ОВ", "НТС": "ОВ", "ИТП": "ОВ",
    "АИ": "АР",
    "СС": "СС", "АПС": "СС", "СОУЭ": "СС", "СКД": "СС",
    "СМ": "СМ",
}
# tag -> the extra synonyms it also satisfies (an ИОС4 volume IS the ОВ discipline, etc.)
_EXPAND: dict[str, tuple[str, ...]] = {
    "ПЗУ": ("ГП",), "ГП": ("ПЗУ",),
    "ИОС1": ("ЭОМ",), "ЭОМ": ("ИОС1",),
    "ИОС2": ("ВК",), "ИОС3": ("ВК",), "ВК": (),
    "ИОС4": ("ОВ",), "ОВ": ("ИОС4",),
    "ИОС5": ("СС",), "СС": ("ИОС5",),
    "ИОС7": ("ТХ",), "ТХ": ("ИОС7",),
}
_MANIFEST_SECTION_ALIASES = {
    "AR": "АР", "KR": "КР", "KZH": "КР", "GP": "ГП", "OV": "ОВ", "VK": "ВК", "EOM": "ЭОМ", "SS": "СС",
    "POS": "ПОС", "PB": "ПБ", "PZ": "ПЗ", "OOS": "ООС", "ODI": "ОДИ", "TX": "ТХ",
}
_SECTION_WORDS = "|".join(sorted(_TOKEN_TAG, key=len, reverse=True))
_SECTION_RE = re.compile(
    rf"(?<![А-Я])(?:(ИОС)[\s.]*(\d)|({_SECTION_WORDS}))(?![А-Я])(?:[\s.]*(\d+(?:\.\d+)*))?(\.[А-Я]{{1,3}}(?![А-Я]))?"
)
_BUILDING_RE = re.compile(r"(?<![А-Я0-9])К\s?(\d{1,2})(?![А-Я0-9])")
_HINT_RE = re.compile(rf"(?<![А-Яа-яA-Za-z])(?:(ИОС)\s*(\d)|({_SECTION_WORDS}))(?![А-Яа-яA-Za-z])")

# names that are recognisably NOT a project section (so an object containing them is not "unknown")
_OTHER_KNOWN_RE = re.compile(
    r"ИГИ|ИГДИ|ИЭИ|ИГМИ|ИЗЫСК|АГР|ГОЧС|ЗАКЛЮЧ|КРИПТО|ПРОГРАММ|СПРАВК|РАСПОРЯЖ|БУКЛЕТ|ПРИКАЗ|"
    r"АКТ|АОСР|ЖУРНАЛ|ПАСПОРТ|СЕРТИФИКАТ|ДЕКЛАРАЦ|ПРОТОКОЛ|СВИДЕТЕЛ|ДОГОВОР|ПИСЬМО|РЕЕСТР|ОТЧЕТ|ФОТО|ЗАМЕЧАН",
)
# NB: deliberately narrow. Names such as "Ведомость", "Схема", "Смета", "КЕО" can belong to a project
# section, so they stay UNKNOWN (and an UNKNOWN file blocks any "this section is absent" claim).

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[._](\d{1,2})[._](20\d{2}|\d{2})(?!\d)")
_KORR_RE = re.compile(r"(?<![а-я])кор{1,2}(?![а-я])\.?\s*(\d+)?")
_IZM_RE = re.compile(r"(?<![а-я])изм(?![а-я])\.?\s*(\d+)?")
_RED_RE = re.compile(r"(?<![а-я])ред(?![а-я])\.?\s*(\d+)?")
_VER_RE = re.compile(r"(?<![a-zа-я0-9])v\s*(\d+)(?!\d)")
_COPY_RE = re.compile(r"\((\d{1,2})\)")
_YEAR_RE = re.compile(r"(?<!\d)(20[12]\d)(?!\d)")
_VARIANT_TAGS = ("РНС", "РНИ", "МГЭ", "АГР", "ГИП", "ЭЦП")
_GENERATION_RE = re.compile(r"(?:^|[\\/])(?:ПД|РД|ИД|ПРОЕКТ\w*)[ _-]*(20\d\d)(?:[\\/]|$)", re.IGNORECASE)

_SERVICE_NAME_RE = re.compile(
    r"^(thumbs\.db|desktop\.ini|\.ds_store)$|^~\$|\.(dwl2?|bak|tmp|log|db|lnk|crdownload|part|swp)$", re.IGNORECASE,
)


def _cyr_upper(text: str) -> str:
    return text.upper().replace("Ё", "Е").translate(_LATIN_TO_CYR)


def canonical_manifest_section(section: object) -> str | None:
    raw = str(section or "").strip().upper()
    if not raw or raw == "OTHER":
        return None
    raw = raw.replace("Ё", "Е")
    return _MANIFEST_SECTION_ALIASES.get(raw) or _TOKEN_TAG.get(raw) or None


def expand_tags(primary: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for tag in primary:
        out.add(tag)
        out.update(_EXPAND.get(tag, ()))
    return frozenset(out)


def hint_tags(source_text: object) -> frozenset[str]:
    """Section tags a catalog `source_pd/rd/id` hint names, as an ANY-OF set. Empty == unconstrained
    (nothing recognisable: e.g. "Технический план БТИ; Акт выноса осей", or lowercase-only prose).
    Only whole upper-case words count, so "фасадов" never yields "ОВ" (the sub-word bug of the older
    `candidate_coverage.parse_discipline_hints`)."""
    text = str(source_text or "").replace("Ё", "Е")
    tags: set[str] = set()
    for match in _HINT_RE.finditer(text):
        if match.group(1):
            tags.add(f"ИОС{match.group(2)}")
        else:
            token = match.group(3)
            if token.upper() == token:  # whole-upper-case words only ("АР", not a stray "ар")
                tags.add(_TOKEN_TAG[token])
    return expand_tags(tags)


# --------------------------------------------------------------------------------------------
# Per-document facts
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DocFacts:
    key: str                       # dataset file id when present, else "doc:<id>"
    doc_id: int | None
    file_id: str | None
    filename: str
    relative_path: str
    stage: str                     # "PD" | "RD" | "ID" | "RD_ID_MIXED" | "UNKNOWN"
    sha256: str | None
    extension: str
    size_bytes: int | None
    pages: int | None
    distribution_status: str | None
    exclusion_reason: str | None
    duplicate_group: str | None
    # ---- derived from the name/path
    tags: frozenset[str]           # expanded section tags, empty == unknown/other
    primary_tags: frozenset[str]
    section_key: str | None        # e.g. "АР", "ПЗ2", "ИОС4.2"
    doc_class: str                 # SECTION | OTHER_KNOWN | UNKNOWN
    family: str | None             # cipher family, alnum-only ("010722" style), None when not code-like
    building: str | None           # "К1"/"К2" corpus number: a different building, never a different revision
    generation: str | None         # path-derived generation folder ("2022"), None if absent
    markers: tuple[tuple[str, int | None], ...]   # ((kind, value), ...) in name order
    variant_tags: frozenset[str]


def _family_of(head: str) -> str | None:
    """Cipher family from the text BEFORE the section token: strips leading numbering ("5.4."), the word
    "Раздел N", stage letters (П/Р/И as lone segments) and trailing separators, then keeps alnum only."""
    text = _cyr_upper(head)
    text = re.sub(r"^\s*\d{1,2}(?:\.\d{1,2})*\.(?=\s|[А-ЯA-Z])\s*", "", text)   # "10.2. ", "5.1.П-" -- never "23.009-"
    text = re.sub(r"РАЗДЕЛ\s*\d*(?:\.\d+)*", " ", text)
    text = re.sub(r"(?<![А-Я0-9])[ПРИ][\s.-]+\d(?![\d.])", " ", text)   # stage letter + one-digit part ("Р-1"): a part, not a series
    text = re.sub(r"(?<![А-Я0-9])[ПРИ](?![А-Я0-9])", " ", text)          # lone stage letters
    text = re.sub(r"(?<![А-Я0-9])(?:РД|ПД|ИД)(?![А-Я0-9])", " ", text)
    text = re.sub(r"^\s*(?:V\d+)[\s_-]*", "", text)                       # "V2_" edition prefix
    core = re.sub(r"[^А-Я0-9]+", "", text)
    digits = sum(ch.isdigit() for ch in core)
    return core if len(core) >= 5 and digits >= 3 else None   # "23.009" (5) is a real, short project cipher


def _markers_of(name: str) -> tuple[tuple[tuple[str, int | None], ...], frozenset[str]]:
    """Revision markers of the part of the file name AFTER the section token: the head is the cipher
    ("01_07.22-14" must never be read as a date, "ЖС-РЛ-0624-2024" not as a year)."""
    lowered = name.lower().replace("ё", "е").replace("_", " ")
    lowered_keep_us = name.lower().replace("ё", "е")
    markers: list[tuple[str, int | None]] = []
    for match in _DATE_RE.finditer(lowered_keep_us):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        year = year + 2000 if year < 100 else year
        if 1 <= month <= 12 and 1 <= day <= 31:
            markers.append(("date", year * 10000 + month * 100 + day))
    for kind, regex in (("korr", _KORR_RE), ("izm", _IZM_RE), ("red", _RED_RE), ("ver", _VER_RE)):
        for match in regex.finditer(lowered):
            markers.append((kind, int(match.group(1)) if match.group(1) else None))
    for match in _COPY_RE.finditer(lowered):
        markers.append(("copy", int(match.group(1))))
    if not any(kind == "date" for kind, _ in markers):
        for match in _YEAR_RE.finditer(lowered):
            markers.append(("year", int(match.group(1))))
    upper = _cyr_upper(name)
    variants = frozenset(tag for tag in _VARIANT_TAGS if tag in upper)
    return tuple(markers), variants


def parse_document_name(filename: str, relative_path: str = "") -> dict[str, Any]:
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename or "")
    upper = _cyr_upper(stem.replace("_", " "))
    section_match = None
    primary: set[str] = set()
    section_key = None
    head = upper
    tail = ""
    for match in _SECTION_RE.finditer(upper):
        if section_match is None:
            section_match = match
            if match.group(1):
                section_key = f"ИОС{match.group(2)}" + (f".{match.group(4)}" if match.group(4) else "")
            else:
                section_key = match.group(3) + (match.group(4) or "")
            section_key += match.group(5) or ""
            if re.fullmatch(r"\s\d{1,2}", upper[match.end():] or ""):
                section_key += "_" + upper[match.end():].strip()   # a single sheet of the section, not a revision
            head = upper[: match.start()]
            tail = stem[match.start():]
        primary.add(f"ИОС{match.group(2)}" if match.group(1) else _TOKEN_TAG[match.group(3)])
    building = None
    building_match = _BUILDING_RE.search(head)
    if building_match:
        building = building_match.group(1)
        head = head[: building_match.start()] + " " + head[building_match.end():]
    doc_class = "SECTION" if primary else ("OTHER_KNOWN" if _OTHER_KNOWN_RE.search(upper) else "UNKNOWN")
    markers, variants = _markers_of(tail) if tail else ((), frozenset())
    generation_match = _GENERATION_RE.search(relative_path or "")
    return {
        "primary_tags": frozenset(primary),
        "tags": expand_tags(primary),
        "section_key": section_key,
        "doc_class": doc_class,
        "family": _family_of(head) if section_match else None,
        "building": building,
        "generation": generation_match.group(1) if generation_match else None,
        "markers": markers,
        "variant_tags": variants,
    }


def _stage_code(value: object) -> str:
    raw = str(value or "").strip().upper()
    return raw if raw in {"PD", "RD", "ID", "RD_ID_MIXED"} else "UNKNOWN"


def facts_from_manifest_row(row: Mapping[str, Any], *, doc_id: int | None = None) -> DocFacts:
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "")
    filename = relative.replace("\\", "/").rsplit("/", 1)[-1] if relative else str(row.get("filename") or row.get("file_id") or "")
    parsed = parse_document_name(filename, relative)
    section = canonical_manifest_section(row.get("section"))
    tags = set(parsed["tags"])
    primary = set(parsed["primary_tags"])
    if section:
        primary.add(section)
        tags |= expand_tags([section])
    doc_class = "SECTION" if primary else parsed["doc_class"]
    ext = str(row.get("extension") or "")
    if not ext and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    return DocFacts(
        key=str(row.get("file_id") or (f"doc:{doc_id}" if doc_id is not None else filename)),
        doc_id=doc_id,
        file_id=str(row.get("file_id")) if row.get("file_id") else None,
        filename=filename,
        relative_path=relative,
        stage=_stage_code(row.get("stage")),
        sha256=str(row.get("sha256") or row.get("source_sha256") or "") or None,
        extension=ext.lower(),
        size_bytes=_opt_int(row.get("size_bytes")),
        pages=_pages_of(row, ext.lower()),
        distribution_status=str(row.get("distribution_status") or "") or None,
        exclusion_reason=str(row.get("exclusion_reason") or "") or None,
        duplicate_group=str(row.get("duplicate_group") or "") or None,
        tags=frozenset(tags),
        primary_tags=frozenset(primary),
        section_key=parsed["section_key"],
        doc_class=doc_class,
        family=parsed["family"],
        building=parsed["building"],
        generation=parsed["generation"],
        markers=parsed["markers"],
        variant_tags=parsed["variant_tags"],
    )


def facts_from_document(doc: Any) -> DocFacts:
    """Build facts from a `DocumentVersion` (official-dataset rows carry the manifest row in
    `dataset_metadata`; a plain upload has only its file name)."""
    meta = getattr(doc, "dataset_metadata", None) or {}
    row = dict(meta.get("document_manifest") or meta.get("files_index") or {})
    row.setdefault("file_id", getattr(doc, "dataset_file_id", None))
    row.setdefault("stage", getattr(doc, "dataset_stage", None) or {"project": "PD", "working": "RD", "as_built": "ID"}.get(str(getattr(doc, "doc_stage", "")), None))
    if not (row.get("relative_path") or row.get("source_relative_path")):
        row["relative_path"] = str(getattr(doc, "filename", "") or "")
    row.setdefault("section", getattr(doc, "dataset_section", None) or getattr(doc, "discipline", None))
    row.setdefault("sha256", getattr(doc, "file_hash", None) or getattr(doc, "content_hash", None))
    if str(getattr(doc, "approval_status", "") or "").upper() == "EXCLUDED":
        row["distribution_status"] = "EXCLUDE"
    facts = facts_from_manifest_row(row, doc_id=int(doc.id) if getattr(doc, "id", None) is not None else None)
    return facts


def _pages_of(row: Mapping[str, Any], extension: str) -> int | None:
    """A manifest that REPORTS a page count for PDFs (`pdf_pages` key present) reports None/0 for a file it could
    not open (`Cannot find Root object in pdf`); that is "unreadable", not "unknown". A row with no `pdf_pages`
    key at all (a plain upload) says nothing and stays None."""
    value = row.get("pdf_pages")
    if value not in (None, ""):
        return _opt_int(value)
    return 0 if ("pdf_pages" in row and extension == ".pdf") else None


def _opt_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _natural_key(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text or "")]


# --------------------------------------------------------------------------------------------
# Integrity (10 points: duplicates / unreadable / service files)
# --------------------------------------------------------------------------------------------
REASON_EXACT_DUPLICATE = "EXACT_DUPLICATE_WITHIN_STAGE"
REASON_UNREADABLE = "UNREADABLE_OR_EMPTY_SOURCE_FILE"
REASON_SERVICE = "TEMPORARY_OR_SERVICE_FILE"
REASON_ORGANIZER_EXCLUDE = "EXCLUDED_BY_MANIFEST"


@dataclass(slots=True)
class IntegrityReport:
    excluded: list[dict[str, Any]] = field(default_factory=list)
    cross_stage_duplicates: list[dict[str, Any]] = field(default_factory=list)
    unparsed_formats: list[dict[str, Any]] = field(default_factory=list)

    @property
    def excluded_keys(self) -> set[str]:
        return {item["key"] for item in self.excluded}

    def to_dict(self) -> dict[str, Any]:
        by_reason: dict[str, int] = defaultdict(int)
        for item in self.excluded:
            by_reason[item["reason"]] += 1
        return {
            "excluded_count": len(self.excluded),
            "excluded_by_reason": dict(sorted(by_reason.items())),
            "excluded": self.excluded,
            "cross_stage_duplicate_groups": self.cross_stage_duplicates,
            "non_pdf_files": len(self.unparsed_formats),
        }


def analyze_integrity(facts: Iterable[DocFacts]) -> IntegrityReport:
    """Duplicates by SHA-256 WITHIN a stage collapse to one canonical document (the lowest natural file id);
    empty/unreadable PDFs, editor/service files and manifest-flagged exclusions are dropped. Identical files
    in DIFFERENT stages are kept (each is a legitimate member of its stage) but reported: a PD-vs-RD pair
    resting on byte-identical files is not an independent comparison (see `comparison_gate`)."""
    report = IntegrityReport()
    items = sorted(facts, key=lambda f: (_natural_key(f.file_id or ""), f.doc_id or 0))
    dropped: set[str] = set()
    for fact in items:
        reason = None
        if fact.distribution_status and fact.distribution_status.upper() == "EXCLUDE":
            reason = REASON_ORGANIZER_EXCLUDE
        elif _SERVICE_NAME_RE.search(fact.filename or ""):
            reason = REASON_SERVICE
        elif fact.extension == ".pdf" and (fact.pages == 0 or fact.size_bytes == 0):
            reason = REASON_UNREADABLE
        elif fact.size_bytes == 0:
            reason = REASON_UNREADABLE
        if reason:
            dropped.add(fact.key)
            report.excluded.append({"key": fact.key, "file_id": fact.file_id, "filename": fact.filename, "stage": fact.stage,
                                    "reason": reason, "duplicate_of": None})
        elif fact.extension and fact.extension != ".pdf":
            report.unparsed_formats.append({"key": fact.key, "extension": fact.extension})
    by_stage_sha: dict[tuple[str, str], list[DocFacts]] = defaultdict(list)
    by_sha: dict[str, list[DocFacts]] = defaultdict(list)
    for fact in items:
        if fact.key in dropped or not fact.sha256:
            continue
        by_stage_sha[(fact.stage, fact.sha256)].append(fact)
        by_sha[fact.sha256].append(fact)
    for (_stage, _sha), group in by_stage_sha.items():
        canonical = group[0]
        for other in group[1:]:
            report.excluded.append({"key": other.key, "file_id": other.file_id, "filename": other.filename, "stage": other.stage,
                                    "reason": REASON_EXACT_DUPLICATE, "duplicate_of": canonical.file_id or canonical.key})
    for sha, group in by_sha.items():
        stages = {f.stage for f in group}
        if len(stages) > 1:
            report.cross_stage_duplicates.append({"sha256": sha, "stages": sorted(stages), "file_ids": [f.file_id or f.key for f in group]})
    report.excluded.sort(key=lambda item: (_natural_key(item.get("file_id") or ""), item["reason"]))
    return report


# --------------------------------------------------------------------------------------------
# Revision analysis
# --------------------------------------------------------------------------------------------
CONFLICT_MIXED_SERIES = "MIXED_PROJECT_BASELINES"
CONFLICT_UNORDERED = "UNORDERED_REVISIONS"
_ORDER_KINDS = ("korr", "izm", "red", "ver", "date", "year")
_MIN_DOCS_FOR_SERIES = 3


@dataclass(slots=True)
class RevisionConflict:
    conflict_id: str
    type: str
    stage: str
    scope: str                       # primary tag (MIXED_SERIES) or section key (UNORDERED)
    detail: dict[str, Any]
    keys: tuple[str, ...]
    file_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"conflict_id": self.conflict_id, "type": self.type, "stage": self.stage, "scope": self.scope,
                "detail": self.detail, "file_ids": list(self.file_ids)}


@dataclass(slots=True)
class RevisionReport:
    conflicts: list[RevisionConflict] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)   # superseded key -> key of the newest revision
    clusters: int = 0

    def conflicts_for(self, key: str) -> list[RevisionConflict]:
        return [c for c in self.conflicts if key in c.keys]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflicts": [c.to_dict() for c in self.conflicts],
            "superseded_revisions": [{"file": k, "superseded_by": v} for k, v in sorted(self.superseded.items())],
            "clusters_examined": self.clusters,
        }


def _marker_value(fact: DocFacts, kind: str) -> tuple[bool, int | None]:
    values = [value for k, value in fact.markers if k == kind]
    if not values:
        return False, None
    numeric = [v for v in values if v is not None]
    return True, (max(numeric) if numeric else None)


def _order_cluster(cluster: list[DocFacts]) -> tuple[bool, dict[str, DocFacts]]:
    """(ordered?, {superseded key: newest}) for files that are variants of the SAME section."""
    variants = {f.variant_tags for f in cluster}
    if len(variants) > 1 and any(v for v in variants):
        return False, {}
    kinds_present = [k for k in _ORDER_KINDS if any(_marker_value(f, k)[0] for f in cluster)]
    for kind in kinds_present:
        others = [k for k in kinds_present if k != kind]
        # every file either carries this numeric kind or carries NO ordering marker at all (an original)
        ok = True
        ranked: list[tuple[int, DocFacts]] = []
        for fact in cluster:
            has, value = _marker_value(fact, kind)
            if has:
                if value is None:
                    ok = False
                    break
                ranked.append((value, fact))
            elif any(_marker_value(fact, other)[0] for other in others):
                ok = False
                break
            else:
                ranked.append((-1, fact))
        if not ok:
            continue
        values = [v for v, _ in ranked]
        if len(set(values)) != len(values):
            continue  # ties: two files claim the same revision
        newest = max(ranked, key=lambda item: item[0])[1]
        return True, {f.key: newest for _, f in ranked if f.key != newest.key}
    return False, {}


def analyze_revisions(facts: Iterable[DocFacts], *, exclude_keys: Iterable[str] = ()) -> RevisionReport:
    skip = set(exclude_keys)
    docs = [f for f in facts
            if f.key not in skip and f.stage in {"PD", "RD", "ID"} and f.doc_class == "SECTION"
            and f.extension in ("", ".pdf")]  # archives/CAD siblings of a PDF are the same drawing, not another edition
    report = RevisionReport()
    by_stage: dict[str, list[DocFacts]] = defaultdict(list)
    for fact in docs:
        by_stage[fact.stage].append(fact)

    counter = 0

    def cid() -> str:
        nonlocal counter
        counter += 1
        return f"REV-{counter:03d}"

    for stage, stage_docs in sorted(by_stage.items()):
        # ---- (1) mixed project series within the stage
        series_of: dict[str, str] = {}
        family_counts: dict[str, int] = defaultdict(int)
        for fact in stage_docs:
            if fact.family:
                family_counts[fact.family] += 1
        for fact in stage_docs:
            code = fact.family if fact.family and family_counts[fact.family] >= _MIN_DOCS_FOR_SERIES else ""
            if fact.generation or code:
                series_of[fact.key] = f"{fact.generation or ''}|{code}"
        tag_series: dict[str, dict[str, list[DocFacts]]] = defaultdict(lambda: defaultdict(list))
        for fact in stage_docs:
            series = series_of.get(fact.key)
            if series is None:
                continue
            for tag in fact.primary_tags:
                tag_series[tag][series].append(fact)
        for tag, per_series in sorted(tag_series.items()):
            if len(per_series) < 2:
                continue
            members = [f for group in per_series.values() for f in group]
            report.conflicts.append(RevisionConflict(
                conflict_id=cid(), type=CONFLICT_MIXED_SERIES, stage=stage, scope=tag,
                detail={"series": {series or "(без шифра)": len(group) for series, group in sorted(per_series.items())},
                        "reason": "в одной стадии присутствуют разные серии шифров/поколения проекта для одного раздела"},
                keys=tuple(f.key for f in members), file_ids=tuple(f.file_id or f.key for f in members),
            ))
        # ---- (2) variants of the same section inside one series
        clusters: dict[tuple[str, str, str], list[DocFacts]] = defaultdict(list)
        for fact in stage_docs:
            if fact.family and fact.section_key:
                clusters[(fact.family, fact.building or "", fact.section_key)].append(fact)
        for (family, building, section_key), cluster in sorted(clusters.items()):
            report.clusters += 1
            if len(cluster) < 2:
                continue
            ordered, superseded = _order_cluster(cluster)
            if ordered:
                for key, newest in superseded.items():
                    report.superseded[key] = newest.file_id or newest.key
                continue
            report.conflicts.append(RevisionConflict(
                conflict_id=cid(), type=CONFLICT_UNORDERED, stage=stage, scope=(f"К{building} " if building else "") + section_key,
                detail={"markers": {f.file_id or f.key: [f"{k}:{v}" if v is not None else k for k, v in f.markers] + sorted(f.variant_tags)
                                    for f in cluster},
                        "reason": "редакции одного раздела нельзя упорядочить (разные виды/отсутствие номера редакции)"},
                keys=tuple(f.key for f in cluster), file_ids=tuple(f.file_id or f.key for f in cluster),
            ))
    return report


# --------------------------------------------------------------------------------------------
# Stage/section inventory (completeness)
# --------------------------------------------------------------------------------------------
def stage_inventory(facts: Iterable[DocFacts], *, exclude_keys: Iterable[str] = ()) -> dict[str, list[DocFacts]]:
    skip = set(exclude_keys)
    out: dict[str, list[DocFacts]] = {"PD": [], "RD": [], "ID": []}
    for fact in facts:
        if fact.key in skip:
            continue
        if fact.stage in out:
            out[fact.stage].append(fact)
        elif fact.stage == "RD_ID_MIXED":
            out["RD"].append(fact)
            out["ID"].append(fact)
    return out
