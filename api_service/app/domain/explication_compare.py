"""ПД ↔ РД comparison of room / apartment schedules (Phase 12, S1).

The most frequent kind of official violation (комплект предметной разметки 1.1: ALT79B, DOO25, POL16, SOSH25) is a
working-documentation schedule that silently departs from the approved project documentation: a room changes its
function, its area, disappears or appears; a floor total or an apartment's areas / composition change. PD is the
reference (expert session). Everything lives in the PDF text layer, so this is a deterministic geometry + text
mechanism (`table_parser`), no model involved.

Pipeline (one call per process run, `collect_explication_groups`):
  1. documents   PD and RD files of the АР section (document_facts tags; the gate's excluded / superseded files are
                 skipped), scanned page by page with a cheap text pre-filter, then parsed (`table_parser`). The result
                 per file is cached on disk (SHA-256 + parser code fingerprint), so a re-run costs a file read.
  2. pairing     an RD table pairs with a PD table of the same kind whose scope (floor / section / building / МОП,
                 БКТ, квартиры) is compatible and whose room keys overlap; the best PD candidate is the reference.
                 PD editions the gate cannot order (revision conflict) are all kept: a difference is reported only if
                 it holds against every such edition that has the same table ("robust" policy; `block` reports
                 nothing). Several RD editions of one table: the newest by revision marker is compared.
  3. rows        by room number (normalized: «Ст.1.1.1» ~ «1.1.1»), then by unique identical name; then the rest are
                 rooms present in one stage only. Names are equal after normalisation, when one contains the other
                 (a wrapped cell read partly, «Кабинет» / «Кабинет врача») or when they differ by a typo
                 (similarity >= NAME_SIMILARITY); areas differ when |Δ| exceeds both the catalog's relative trigger
                 (M-002: > 1 %) and one unit of the coarser printed precision (rounding noise).
  4. output      one evidence group per discrepancy (location = the room / apartment number as printed, finding_group
                 = the floor table pair), one per floor total (M-002 trigger > 1 %), one NEGATIVE_VERIFIED group per
                 table pair without discrepancies. Evidence: PD and RD row (file + page + row bbox) and both tables'
                 bbox (for IoU with zone-level gold), expected / actual, confidence.

Code mapping (an assumption, documented in evaluation/phase12/S1_REPORT.md):
  rooms       function / name change, area change, room only in one stage -> PZ-003 (M-003 «Полезная / Расчетная
              площадь», RD source «Сводная экспликация помещений»); floor total -> PZ-002 (M-002, trigger > 1 %)
  apartments  area columns -> PZ-003; number of apartments / apartment only in one stage -> PZ-010 (M-010);
              rooms count / type / name -> PZ-011 (M-011 «Квартирография»)
Flag: CASE10_EXPLICATION_COMPARE=1 enables it (default off; integration I decides).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Iterable

from . import table_parser as tpar
from .table_parser import KIND_APARTMENTS, KIND_ROOMS, RoomTable, Row

logger = logging.getLogger(__name__)

EXPLICATION_COMPARE_VERSION = "explication-compare-v1"
EXPLICATION_SOURCE = "explication_compare"
EXTRACTOR_NAME = "explication_table_parser"

AREA_RELATIVE_TOLERANCE = Decimal("0.01")         # catalog M-002 trigger «> 1%», reused for every area cell
TOTAL_RELATIVE_TOLERANCE = Decimal("0.01")        # M-002 trigger for a floor total
NAME_SIMILARITY = 0.8                             # below: a different function, above: the same name with a typo
MIN_COMMON_KEYS = 2
MIN_KEY_OVERLAP = 0.5
MIN_NAME_AGREEMENT_UNSCOPED = 0.5
PAIR_SCORE_BAND = 0.8
PARTIAL_MAX_ROWS = 3
PARTIAL_SHARE = 0.25
PARTIAL_SIZE_RATIO = 0.75
MIN_PAIR_SCORE = 1.7
TOTAL_MIN_COVERAGE = 0.8

CODE_ROOMS = "PZ-003"
CODE_TOTAL = "PZ-002"
CODE_APARTMENT_COUNT = "PZ-010"
CODE_APARTMENT_COMPOSITION = "PZ-011"
S1_CODES = frozenset({CODE_ROOMS, CODE_TOTAL, CODE_APARTMENT_COUNT, CODE_APARTMENT_COMPOSITION})

TYPE_NAME_CHANGED = "ROOM_FUNCTION_CHANGED"
TYPE_AREA_CHANGED = "AREA_CHANGED"
TYPE_ONLY_PD = "ONLY_IN_PD"
TYPE_ONLY_RD = "ONLY_IN_RD"
TYPE_COUNT_CHANGED = "COUNT_CHANGED"
TYPE_COMPOSITION_CHANGED = "COMPOSITION_CHANGED"
TYPE_TOTAL = "FLOOR_TOTAL"
TYPE_TABLE_EQUAL = "TABLE_EQUAL"

_PREFILTER_RE = re.compile(r"(экспликац|спецификац|ведомост)\w*\s+(?:\w+\s+){0,2}(помещ|квартир)|экспликаци", re.IGNORECASE)
_STAGES = ("PD", "RD")


def explication_compare_enabled() -> bool:
    return os.getenv("CASE10_EXPLICATION_COMPARE", "0").strip().lower() in {"1", "true", "yes", "on"}


def revision_policy() -> str:
    value = os.getenv("CASE10_EXPLICATION_REVISION_POLICY", "robust").strip().lower()
    return value if value in {"robust", "block"} else "robust"


# ---------------------------------------------------------------- comparison primitives (pure)


def normalize_name(text: object) -> str:
    raw = tpar.norm(text)
    raw = re.sub(r"(?<![\w.])\d+(?:\.\d+)+(?![\w.])", " ", raw)      # an apartment / room code read into the name cell
    raw = re.sub(r"[\s.,;:«»\"'()\-–—/№]+", "", raw)
    return raw


def names_equal(a: object, b: object) -> bool:
    left, right = normalize_name(a), normalize_name(b)
    if left == right:
        return True
    if not left or not right:
        return False
    short, long_ = sorted((left, right), key=len)
    if len(short) >= 4 and short in long_:
        return True                                  # a wrapped name read partly on one side, or a refinement
    return SequenceMatcher(None, left, right).ratio() >= NAME_SIMILARITY


def area_tolerance(expected: Decimal, decimals: int) -> Decimal:
    unit = Decimal(1).scaleb(-max(0, decimals))
    return max(abs(expected) * AREA_RELATIVE_TOLERANCE, unit)


def numbers_differ(pd_cell: dict[str, Any], rd_cell: dict[str, Any], *, area: bool) -> tuple[bool, Decimal]:
    """(differs, |Δ| of the closest pair). A cell may print two values (an edited value next to the struck one):
    it differs only if no printed PD value is within tolerance of any printed RD value."""
    best = None
    for left in pd_cell["all"]:
        for right in rd_cell["all"]:
            delta = abs(right - left)
            if best is None or delta < best[0]:
                best = (delta, left)
    delta, reference = best
    if not area:
        return delta != 0, delta
    decimals = min(int(pd_cell.get("decimals") or 0), int(rd_cell.get("decimals") or 0))
    return delta > area_tolerance(reference, decimals), delta


def fmt_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f") if value == value.to_integral() else format(value, "f")


@dataclass(slots=True)
class TableRef:
    """A parsed table and where it lives."""
    table: RoomTable
    stage: str
    file_id: str
    doc_key: Any                                 # DocumentVersion id (or any hashable id in tests)
    revision_rank: tuple = ()
    document: Any = None
    volume: str | None = None                    # section key of the volume («АР1»): editions of one volume compete

    @property
    def page(self) -> int:
        return int(self.table.page or 0)

    def ident(self) -> str:
        return f"{self.stage}:{self.file_id}:{self.page}:{','.join(str(round(v)) for v in self.table.bbox[:2])}"


@dataclass(slots=True)
class Discrepancy:
    type: str
    code: str
    location: str
    field: str | None
    expected: str | None
    actual: str | None
    pd_row: Row | None
    rd_row: Row | None
    details: dict[str, Any] = field(default_factory=dict)
    violation: bool = True
    confidence: float = 0.75


@dataclass(slots=True)
class PairResult:
    pd: TableRef
    rd: TableRef
    score: float
    alternatives: list[TableRef]
    discrepancies: list[Discrepancy]
    compared_rows: int
    suppressed: list[dict[str, Any]]
    notes: list[str]
    pd_copies: list[TableRef] = field(default_factory=list)
    rd_copies: list[TableRef] = field(default_factory=list)

    @property
    def finding_group(self) -> str:
        return f"{self.pd.file_id}:{self.pd.page}|{self.rd.file_id}:{self.rd.page}|{self.rd.table.scope.label() or self.rd.table.title}"


def scope_compatible(a: tpar.Scope, b: tpar.Scope) -> bool:
    for left, right in ((a.floor, b.floor), (a.section, b.section), (a.building, b.building)):
        if left and right and left != right:
            return False
    return set(a.qualifiers) == set(b.qualifiers)


def _keyed(table: RoomTable) -> tuple[dict[str, Row], set[str]]:
    rows: dict[str, Row] = {}
    duplicates: set[str] = set()
    for row in table.rows:
        key = tpar.normalize_key(row.key)
        if key in rows:
            duplicates.add(key)
            continue
        rows[key] = row
    for key in duplicates:
        rows.pop(key, None)
    return rows, duplicates


def _name_of(table: RoomTable, row: Row) -> str | None:
    values = tpar.row_values(table, row)
    return values.get(tpar.ROLE_NAME) or values.get(tpar.ROLE_TYPE)


def _areas_agree(pd_table: RoomTable, pd_row: Row, rd_table: RoomTable, rd_row: Row) -> bool:
    left, right = tpar.row_values(pd_table, pd_row), tpar.row_values(rd_table, rd_row)
    roles = [role for role in tpar.NUMERIC_ROLES if role in left and role in right]
    return bool(roles) and not any(numbers_differ(left[r], right[r], area=r in tpar.AREA_ROLES)[0] for r in roles)


def pair_score(pd: TableRef, rd: TableRef) -> float | None:
    if pd.table.kind != rd.table.kind or not scope_compatible(pd.table.scope, rd.table.scope):
        return None
    pd_rows, _ = _keyed(pd.table)
    rd_rows, _ = _keyed(rd.table)
    common = set(pd_rows) & set(rd_rows)
    if len(common) < MIN_COMMON_KEYS:
        return None
    overlap = len(common) / max(1, min(len(pd_rows), len(rd_rows)))
    if overlap < MIN_KEY_OVERLAP:
        return None
    agreement = sum(names_equal(_name_of(pd.table, pd_rows[k]), _name_of(rd.table, rd_rows[k])) for k in common) / len(common)
    scoped = bool(pd.table.scope.floor and rd.table.scope.floor)
    if not scoped and agreement < MIN_NAME_AGREEMENT_UNSCOPED:
        return None
    jaccard = len(common) / len(set(pd_rows) | set(rd_rows))
    values = sum(_areas_agree(pd.table, pd_rows[k], rd.table, rd_rows[k]) for k in common) / len(common)
    score = round(overlap + 0.5 * jaccard + 0.5 * agreement + 0.25 * values + (0.25 if scoped else 0.0), 6)
    return score if score >= MIN_PAIR_SCORE else None


def pair_tables(pd_refs: list[TableRef], rd_refs: list[TableRef], *, conflicting: dict[Any, set[Any]] | None = None) -> list[tuple[TableRef, TableRef, float, list[TableRef], list[TableRef]]]:
    """[(pd, rd, score, alternative PD editions, RD copies of the same table on other sheets)].

    PD reference of an RD table: among the PD tables that pair with it (score >= PAIR_SCORE_BAND x best), the one of
    the newest edition by revision marker (generation folder, «Изм.N», date), then the best score. PD editions the
    gate cannot order against it AND that carry the same revision rank are returned as alternatives (robust policy).
    RD: every volume is compared; of several editions of one volume (same section key, e.g. «АР1» and «АР1_Изм3»)
    only the newest pairs with a given PD table -- an outdated edition is never cited."""
    conflicting = conflicting or {}
    candidates = []
    for rd in rd_refs:
        scored = [(score, pd) for pd in pd_refs if (score := pair_score(pd, rd)) is not None]
        if not scored:
            continue
        top = max(score for score, _ in scored)
        band = [(score, pd) for score, pd in scored if score >= PAIR_SCORE_BAND * top]
        band.sort(key=lambda item: (tuple(-x for x in _rank_key(item[1].revision_rank)), -item[0], str(item[1].file_id), item[1].page))
        best_score, best = band[0]
        seen_docs: set[Any] = {best.doc_key}
        alternatives = []
        for score, pd in sorted(scored, key=lambda item: (-item[0], str(item[1].file_id), item[1].page)):
            if pd.doc_key in seen_docs or pd.doc_key not in conflicting.get(best.doc_key, set()):
                continue
            if _rank_key(pd.revision_rank) != _rank_key(best.revision_rank):
                continue                               # an older (orderable) edition is not a competing reference
            seen_docs.add(pd.doc_key)
            alternatives.append(pd)
        candidates.append((best, rd, best_score, alternatives))
    # per (PD table, RD volume): the newest edition document; its tables with identical content are one table
    # printed on several sheets (cited together), different content = different tables (compared separately)
    groups: dict[tuple[str, str], list[tuple[TableRef, TableRef, float, list[TableRef]]]] = {}
    for item in candidates:
        groups.setdefault((item[0].ident(), str(item[1].volume or item[1].file_id)), []).append(item)
    out = []
    for key in sorted(groups):
        items = groups[key]
        newest = max(_rank_key(item[1].revision_rank) for item in items)
        items = [item for item in items if _rank_key(item[1].revision_rank) == newest]
        doc = sorted(items, key=lambda c: (-c[2], c[1].ident()))[0][1].doc_key
        items = sorted((item for item in items if item[1].doc_key == doc), key=lambda c: (c[1].page, c[1].ident()))
        # different tables of one volume competing for one PD table: only the best-matching content pairs with it
        # (the others are other floors / sections whose own PD table was not found)
        top = max(item[2] for item in items)
        items = [item for item in items if item[2] >= top - 1e-9]
        by_content: dict[str, list[tuple[TableRef, TableRef, float, list[TableRef]]]] = {}
        for item in items:
            by_content.setdefault(content_signature(item[1].table), []).append(item)
        for signature in sorted(by_content, key=lambda sig: by_content[sig][0][1].ident()):
            first = by_content[signature][0]
            out.append((first[0], first[1], first[2], first[3], [item[1] for item in by_content[signature][1:]]))
    return out


def content_signature(table: RoomTable) -> str:
    rows = [(tpar.normalize_key(r.key), sorted((tpar.norm(v) for v in r.cells.values()))) for r in table.rows]
    totals = [t.values for t in table.totals]
    return hashlib.sha256(json.dumps([table.kind, rows, totals], ensure_ascii=False).encode("utf-8")).hexdigest()


def pd_copies_of(pd: TableRef, pd_refs: list[TableRef]) -> list[TableRef]:
    signature = content_signature(pd.table)
    return [ref for ref in pd_refs if ref.doc_key == pd.doc_key and ref is not pd and ref.page != pd.page and content_signature(ref.table) == signature]


def _rank_key(rank: tuple) -> tuple:
    return tuple(int(v) if isinstance(v, (int, float)) else 0 for v in rank)


def _code_for(kind: str, dtype: str, role: str | None) -> str:
    if kind == KIND_APARTMENTS:
        if dtype in (TYPE_ONLY_PD, TYPE_ONLY_RD) or role == tpar.ROLE_COUNT:
            return CODE_APARTMENT_COUNT
        if role in tpar.AREA_ROLES:
            return CODE_ROOMS
        return CODE_APARTMENT_COMPOSITION
    return CODE_TOTAL if dtype == TYPE_TOTAL else CODE_ROOMS


def compare_tables(pd: TableRef, rd: TableRef, alternatives: Iterable[TableRef] = ()) -> PairResult:
    alternatives = list(alternatives)
    kind = pd.table.kind
    pd_rows, pd_dup = _keyed(pd.table)
    rd_rows, rd_dup = _keyed(rd.table)
    alt_rows = [(_keyed(alt.table)[0], alt) for alt in alternatives]
    notes = []
    if pd_dup or rd_dup:
        notes.append(f"ambiguous keys skipped: PD {sorted(pd_dup)} RD {sorted(rd_dup)}")
    discrepancies: list[Discrepancy] = []
    suppressed: list[dict[str, Any]] = []
    pairs: list[tuple[str, Row, Row, str]] = [(key, pd_rows[key], rd_rows[key], "number") for key in pd_rows if key in rd_rows]
    only_pd = [key for key in pd_rows if key not in rd_rows and key not in rd_dup]
    only_rd = [key for key in rd_rows if key not in pd_rows and key not in pd_dup]
    # fallback key: a unique identical name on both sides among the unmatched rows (renumbering)
    by_name_pd: dict[str, list[str]] = {}
    for key in only_pd:
        by_name_pd.setdefault(normalize_name(_name_of(pd.table, pd_rows[key])), []).append(key)
    by_name_rd: dict[str, list[str]] = {}
    for key in only_rd:
        by_name_rd.setdefault(normalize_name(_name_of(rd.table, rd_rows[key])), []).append(key)
    for name, keys in by_name_pd.items():
        if name and len(keys) == 1 and len(by_name_rd.get(name, [])) == 1:
            pd_key, rd_key = keys[0], by_name_rd[name][0]
            pairs.append((pd_key, pd_rows[pd_key], rd_rows[rd_key], "name"))
            only_pd.remove(pd_key)
            only_rd.remove(rd_key)

    # second fallback: same name AND same printed area, unique on both sides (numbering differs, e.g. an apartment
    # prefix read on one side only)
    def signature(table: RoomTable, row: Row) -> tuple[str, str] | None:
        values = tpar.row_values(table, row)
        area = next((values[r]["raw"] for r in (tpar.ROLE_AREA, tpar.ROLE_TOTAL_AREA) if r in values), None)
        name = normalize_name(_name_of(table, row))
        return (name, area.replace(",", ".")) if name and area else None

    sig_pd: dict[tuple[str, str], list[str]] = {}
    for key in only_pd:
        if (sig := signature(pd.table, pd_rows[key])) is not None:
            sig_pd.setdefault(sig, []).append(key)
    sig_rd: dict[tuple[str, str], list[str]] = {}
    for key in only_rd:
        if (sig := signature(rd.table, rd_rows[key])) is not None:
            sig_rd.setdefault(sig, []).append(key)
    for sig, keys in sig_pd.items():
        if len(keys) == 1 and len(sig_rd.get(sig, [])) == 1:
            pd_key, rd_key = keys[0], sig_rd[sig][0]
            pairs.append((pd_key, pd_rows[pd_key], rd_rows[rd_key], "name_area"))
            only_pd.remove(pd_key)
            only_rd.remove(rd_key)

    def alt_agrees(key: str, role: str | None, rd_value: Any) -> str | None:
        """File id of an alternative (conflicting) PD edition that agrees with RD on this cell, if any."""
        for rows, alt in alt_rows:
            row = rows.get(key)
            if role is None:                              # presence check
                if (row is not None) == (rd_value is not None):
                    return alt.file_id
                continue
            if row is None:
                continue
            values = tpar.row_values(alt.table, row)
            if role in tpar.TEXT_ROLES:
                if names_equal(values.get(role), rd_value):
                    return alt.file_id
            elif role in values and not numbers_differ(values[role], rd_value, area=role in tpar.AREA_ROLES)[0]:
                return alt.file_id
        return None

    def add(d: Discrepancy, key: str, role: str | None, rd_value: Any) -> None:
        agreeing = alt_agrees(key, role, rd_value) if alternatives else None
        if agreeing:
            suppressed.append({"type": d.type, "location": d.location, "field": d.field, "agrees_with": agreeing})
            return
        discrepancies.append(d)

    compared = 0
    for key, pd_row, rd_row, via in pairs:
        compared += 1
        pv = tpar.row_values(pd.table, pd_row)
        rv = tpar.row_values(rd.table, rd_row)
        location = pd_row.key
        base_conf = 0.8 if via == "number" else 0.6
        details_base = {"matched_by": via, "rd_key": rd_row.key}
        for role in (tpar.ROLE_NAME, tpar.ROLE_TYPE):
            if role in pv and role in rv and not names_equal(pv[role], rv[role]):
                dtype = TYPE_NAME_CHANGED if kind == KIND_ROOMS else TYPE_COMPOSITION_CHANGED
                add(Discrepancy(type=dtype, code=_code_for(kind, dtype, role), location=location, field=role,
                                expected=pv[role], actual=rv[role], pd_row=pd_row, rd_row=rd_row,
                                details={**details_base, "similarity": round(SequenceMatcher(None, normalize_name(pv[role]), normalize_name(rv[role])).ratio(), 3)},
                                confidence=base_conf), key, role, rv[role])
        for role in sorted(tpar.NUMERIC_ROLES):
            if role not in pv or role not in rv:
                continue
            differs, delta = numbers_differ(pv[role], rv[role], area=role in tpar.AREA_ROLES)
            if not differs:
                continue
            is_area = role in tpar.AREA_ROLES
            dtype = TYPE_AREA_CHANGED if is_area else (TYPE_COUNT_CHANGED if role == tpar.ROLE_COUNT else TYPE_COMPOSITION_CHANGED)
            reference = pv[role]["value"]
            relative = (delta / abs(reference)) if reference else None
            add(Discrepancy(type=dtype, code=_code_for(kind, dtype, role), location=location, field=role,
                            expected=pv[role]["raw"], actual=rv[role]["raw"], pd_row=pd_row, rd_row=rd_row,
                            details={**details_base, "delta": fmt_decimal(delta), "relative": fmt_decimal(relative.quantize(Decimal("0.0001"))) if relative is not None else None,
                                     "tolerance": fmt_decimal(area_tolerance(reference, min(pv[role]["decimals"], rv[role]["decimals"]))) if is_area else "0"},
                            confidence=base_conf), key, role, rv[role])
    # an excerpt of a schedule (a fragment sheet repeating a few rooms) is not a schedule without the other rooms:
    # rooms present in one stage only are reported only while they are few
    if len(only_pd) > max(PARTIAL_MAX_ROWS, PARTIAL_SHARE * len(pd_rows)) or len(rd_rows) < PARTIAL_SIZE_RATIO * len(pd_rows):
        notes.append(f"RD table is partial: {len(only_pd)} PD rooms absent, not reported one by one")
        only_pd = []
    if len(only_rd) > max(PARTIAL_MAX_ROWS, PARTIAL_SHARE * len(rd_rows)) or len(pd_rows) < PARTIAL_SIZE_RATIO * len(rd_rows):
        notes.append(f"PD table is partial: {len(only_rd)} RD rooms absent, not reported one by one")
        only_rd = []
    for key in only_pd:
        row = pd_rows[key]
        add(Discrepancy(type=TYPE_ONLY_PD, code=_code_for(kind, TYPE_ONLY_PD, None), location=row.key, field=None,
                        expected=_row_summary(pd.table, row), actual=None, pd_row=row, rd_row=None,
                        details={"note": "нет в экспликации РД"}, confidence=0.7), key, None, None)
    for key in only_rd:
        row = rd_rows[key]
        add(Discrepancy(type=TYPE_ONLY_RD, code=_code_for(kind, TYPE_ONLY_RD, None), location=row.key, field=None,
                        expected=None, actual=_row_summary(rd.table, row), pd_row=None, rd_row=row,
                        details={"note": "нет в экспликации ПД"}, confidence=0.7), key, None, row)
    return PairResult(pd=pd, rd=rd, score=0.0, alternatives=alternatives, discrepancies=discrepancies,
                      compared_rows=compared, suppressed=suppressed, notes=notes)


def _row_summary(table: RoomTable, row: Row) -> str:
    values = tpar.row_values(table, row)
    parts = [row.key]
    for role in (tpar.ROLE_NAME, tpar.ROLE_TYPE):
        if role in values:
            parts.append(str(values[role]))
    for role in sorted(tpar.NUMERIC_ROLES):
        if role in values:
            parts.append(values[role]["raw"] + (" м²" if role in tpar.AREA_ROLES else ""))
    return " ".join(parts)


def compare_totals(pd: TableRef, rd: TableRef) -> Discrepancy | None:
    """Floor totals («Итог(о) по этажу», «Итого:») of the two tables, M-002 trigger > 1 %."""
    if not pd.table.totals or not rd.table.totals:
        return None
    pd_keys, rd_keys = set(_keyed(pd.table)[0]), set(_keyed(rd.table)[0])
    common = len(pd_keys & rd_keys)
    if not pd_keys or not rd_keys or common < TOTAL_MIN_COVERAGE * len(pd_keys) or common < TOTAL_MIN_COVERAGE * len(rd_keys):
        return None                                   # totals of different room sets (an excerpt) are not comparable
    left, left_all, left_dec = tpar.total_value(pd.table.totals[0])
    right, right_all, right_dec = tpar.total_value(rd.table.totals[0])
    if left is None or right is None or left <= 0:
        return None
    delta = min(abs(r - l) for l in left_all for r in right_all)
    relative = delta / left
    violation = relative > TOTAL_RELATIVE_TOLERANCE
    location = rd.table.scope.label() or pd.table.scope.label() or "итог по экспликации"
    return Discrepancy(type=TYPE_TOTAL, code=CODE_TOTAL, location=location, field="TOTAL",
                       expected=" ".join(pd.table.totals[0].values), actual=" ".join(rd.table.totals[0].values),
                       pd_row=None, rd_row=None, violation=violation, confidence=0.8,
                       details={"delta": fmt_decimal(delta), "relative": fmt_decimal(relative.quantize(Decimal("0.0001"))),
                                "trigger": "> 1 % (M-002)", "pd_label": pd.table.totals[0].label, "rd_label": rd.table.totals[0].label})


def merge_page_continuations(refs: list[TableRef]) -> list[TableRef]:
    """Parts of one schedule printed on the same sheet in separate blocks (same kind, title, scope and columns, no
    shared room numbers, only the last part carries the total) become one table."""
    out: list[TableRef] = []
    for ref in sorted(refs, key=lambda r: (str(r.file_id), r.page, r.table.bbox[0], r.table.bbox[1])):
        table = ref.table
        target = next((o for o in out if o.doc_key == ref.doc_key and o.page == ref.page and o.table.kind == table.kind
                       and tpar.norm(o.table.title) == tpar.norm(table.title) and o.table.scope.to_dict() == table.scope.to_dict()
                       and [c.role for c in o.table.columns[:len(table.columns)]] == [c.role for c in table.columns]
                       and not any(t.values for t in o.table.totals) and not (set(_keyed(o.table)[0]) & set(_keyed(table)[0]))), None)
        if target is None:
            out.append(TableRef(table=RoomTable.from_dict(table.to_dict()), stage=ref.stage, file_id=ref.file_id, doc_key=ref.doc_key,
                                revision_rank=ref.revision_rank, document=ref.document, volume=ref.volume))
            continue
        merged = target.table
        offset = len(merged.columns)
        for row in table.rows:
            merged.rows.append(Row(key=row.key, cells={k + offset: v for k, v in row.cells.items()}, bbox=row.bbox, key_bbox=row.key_bbox))
        merged.columns.extend(table.columns)
        merged.totals.extend(table.totals)
        merged.bbox = [min(merged.bbox[0], table.bbox[0]), min(merged.bbox[1], table.bbox[1]), max(merged.bbox[2], table.bbox[2]), max(merged.bbox[3], table.bbox[3])]
        merged.blocks += table.blocks
    return out


def compare_pairs(pd_refs: list[TableRef], rd_refs: list[TableRef], *, conflicting: dict[Any, set[Any]] | None = None,
                  policy: str = "robust") -> list[PairResult]:
    pd_refs, rd_refs = merge_page_continuations(pd_refs), merge_page_continuations(rd_refs)
    results = []
    for pd, rd, score, alternatives, rd_copies in pair_tables(pd_refs, rd_refs, conflicting=conflicting):
        result = compare_tables(pd, rd, alternatives if policy == "robust" else ())
        result.score = score
        result.rd_copies = rd_copies
        result.pd_copies = pd_copies_of(pd, pd_refs)
        if alternatives and policy == "block":
            result.notes.append("revision conflict: comparison blocked (CASE10_EXPLICATION_REVISION_POLICY=block)")
            result.suppressed.extend({"type": d.type, "location": d.location, "field": d.field, "agrees_with": "BLOCKED"} for d in result.discrepancies)
            result.discrepancies = []
        total = compare_totals(pd, rd)
        if total is not None:
            result.discrepancies.append(total)
        results.append(result)
    return results


# ---------------------------------------------------------------- document scanning (text layer + disk cache)


_FINGERPRINT_FILES = ("table_parser.py", "explication_compare.py", "anchor_search.py", "dataset_sources.py")


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    digest = hashlib.sha256()
    base = Path(__file__).resolve().parent
    for name in _FINGERPRINT_FILES:
        path = base / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"-")
    return digest.hexdigest()[:16]


def _cache_dir() -> Path | None:
    explicit = os.getenv("CASE10_EXPLICATION_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    tagger = os.getenv("CASE10_LIVE_TAGGER_CACHE_DIR", "").strip()
    return Path(tagger) / "explication" if tagger else None


def _load_cache(sha256: str) -> dict[str, Any] | None:
    directory = _cache_dir()
    if directory is None:
        return None
    path = directory / f"{sha256}_{code_fingerprint()}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("fingerprint") == code_fingerprint() else None


def _store_cache(sha256: str, record: dict[str, Any]) -> None:
    directory = _cache_dir()
    if directory is None:
        return
    path = directory / f"{sha256}_{code_fingerprint()}.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # a cache that cannot be written never fails a run
        logger.warning("explication cache write failed: %s", exc)


def scan_document_tables(document: Any) -> dict[str, Any]:
    """{"pages_total", "candidate_pages", "tables": [RoomTable dict, ...]} of one PDF; pure function of the bytes."""
    from . import dataset_sources

    ref = dataset_sources.document_original_ref(document)
    if ref is None:
        return {"pages_total": 0, "candidate_pages": [], "tables": [], "skipped": "no_pdf_original"}
    sha256 = ref[1]
    cached = _load_cache(sha256)
    if cached is not None:
        return cached["result"]
    import fitz

    try:
        data = dataset_sources.original_document_bytes(document)
    except Exception as exc:  # noqa: BLE001 -- an unreadable original is reported, never fatal
        return {"pages_total": 0, "candidate_pages": [], "tables": [], "skipped": type(exc).__name__}
    pages: list[int] = []
    with fitz.open(stream=data, filetype="pdf") as pdf:
        total = len(pdf)
        for index in range(total):
            text = re.sub(r"\s+", " ", pdf[index].get_text("text"))
            if _PREFILTER_RE.search(text):
                pages.append(index + 1)
    tables: list[dict[str, Any]] = []
    low_quality: list[dict[str, Any]] = []
    if pages:
        snapshots = dataset_sources.extract_original_pages(document, pages)
        for page in sorted(snapshots):
            if snapshots[page].get("cmap_corruption_suspected"):
                low_quality.append({"page": page, "reason": "broken_text_layer_no_ocr"})
                continue
            for table in tpar.find_room_tables(snapshots[page], page=page):
                if tpar.table_text_is_garbled(table):
                    low_quality.append({"page": page, "reason": "garbled_table_text", "title": table.title[:80]})
                    continue
                tables.append(table.to_dict())
    result = {"pages_total": total, "candidate_pages": pages, "tables": tables, "low_quality": low_quality}
    _store_cache(sha256, {"fingerprint": code_fingerprint(), "result": result})
    return result


# ---------------------------------------------------------------- pipeline integration


def _doc_stage(doc: Any) -> str | None:
    raw = str(getattr(doc, "dataset_stage", None) or "").upper()
    if raw in _STAGES:
        return raw
    return {"project": "PD", "working": "RD"}.get(str(getattr(doc, "doc_stage", None) or ""))


def _revision_rank(fact: Any) -> tuple:
    markers = dict((kind, value) for kind, value in (getattr(fact, "markers", None) or ()) if value is not None)
    generation = getattr(fact, "generation", None)
    return (int(generation) if generation and str(generation).isdigit() else 0, int(markers.get("izm") or 0), int(markers.get("date") or markers.get("year") or 0))


def candidate_documents(docs: Iterable[Any], gate: Any) -> list[Any]:
    out = []
    for doc in docs:
        stage = _doc_stage(doc)
        if stage is None or getattr(doc, "id", None) is None:
            continue
        if gate is not None and gate.drop_from_generic_candidates(doc.id):
            continue
        fact = gate.facts.get(int(doc.id)) if gate is not None else None
        tags = getattr(fact, "tags", None) or frozenset()
        if "АР" not in tags:
            continue
        out.append(doc)
    return sorted(out, key=lambda d: (str(getattr(d, "dataset_file_id", None) or ""), int(d.id)))


def _conflicts(gate: Any, docs: list[Any]) -> dict[Any, set[Any]]:
    if gate is None:
        return {}
    out: dict[Any, set[Any]] = {}
    key_to_doc = {fact.key: doc_id for doc_id, fact in gate.facts.items()}
    for doc in docs:
        fact = gate.facts.get(int(doc.id))
        if fact is None:
            continue
        for conflict in gate.revisions.conflicts_for(fact.key):
            out.setdefault(int(doc.id), set()).update(key_to_doc[k] for k in conflict.keys if k in key_to_doc and key_to_doc[k] != int(doc.id))
    return out


def build_table_refs(docs: list[Any], gate: Any) -> tuple[dict[str, list[TableRef]], dict[str, Any]]:
    refs: dict[str, list[TableRef]] = {stage: [] for stage in _STAGES}
    diagnostics: dict[str, Any] = {"documents": 0, "pages_total": 0, "candidate_pages": 0, "tables": 0, "skipped": {}}
    for doc in docs:
        result = scan_document_tables(doc)
        diagnostics["documents"] += 1
        diagnostics["pages_total"] += int(result.get("pages_total") or 0)
        diagnostics["candidate_pages"] += len(result.get("candidate_pages") or [])
        for item in result.get("low_quality") or []:
            diagnostics.setdefault("low_quality", []).append({"file_id": str(getattr(doc, "dataset_file_id", None) or doc.id), **item})
        if result.get("skipped"):
            diagnostics["skipped"][str(getattr(doc, "dataset_file_id", None) or doc.id)] = result["skipped"]
        fact = gate.facts.get(int(doc.id)) if gate is not None else None
        for data in result.get("tables") or []:
            table = RoomTable.from_dict(data)
            refs[_doc_stage(doc)].append(TableRef(table=table, stage=_doc_stage(doc), file_id=str(getattr(doc, "dataset_file_id", None) or f"doc:{doc.id}"),
                                                  doc_key=int(doc.id), revision_rank=_revision_rank(fact), document=doc,
                                                  volume=getattr(fact, "section_key", None)))
            diagnostics["tables"] += 1
    return refs, diagnostics


def collect_explication_groups(db: Any, process: Any, params: list[Any], docs: list[Any], *, gate: Any, touched_keys: dict[int, set[str]],
                               user_id: int | None = None) -> dict[str, Any]:
    """Writes the explication evidence groups of this run; returns diagnostics (also stored nowhere else)."""
    params_by_code = {str(p.code): p for p in params if str(p.code) in S1_CODES}
    if not params_by_code:
        return {"skipped": "no_s1_params_in_scope"}
    candidates = candidate_documents(docs, gate)
    refs, diagnostics = build_table_refs(candidates, gate)
    results = compare_pairs(refs["PD"], refs["RD"], conflicting=_conflicts(gate, candidates), policy=revision_policy())
    diagnostics.update({"pairs": len(results), "groups": 0, "violations": 0, "suppressed": 0})
    for result in results:
        diagnostics["suppressed"] += len(result.suppressed)
        written = 0
        for discrepancy in result.discrepancies:
            param = params_by_code.get(discrepancy.code)
            if param is None:
                continue
            key = _upsert_group(db, process, param, result, discrepancy, user_id=user_id)
            touched_keys.setdefault(int(param.id), set()).add(key)
            diagnostics["groups"] += 1
            diagnostics["violations"] += int(discrepancy.violation)
            written += int(discrepancy.type != TYPE_TOTAL)
        if written == 0 and not any(d.type != TYPE_TOTAL for d in result.discrepancies):
            param = params_by_code.get(CODE_ROOMS if result.pd.table.kind == KIND_ROOMS else CODE_APARTMENT_COMPOSITION)
            if param is not None:
                equal = Discrepancy(type=TYPE_TABLE_EQUAL, code=str(param.code), location=result.rd.table.scope.label() or result.rd.table.title or "экспликация",
                                    field=None, expected=f"{len(result.pd.table.rows)} строк", actual=f"{len(result.rd.table.rows)} строк",
                                    pd_row=None, rd_row=None, violation=False, confidence=0.7,
                                    details={"compared_rows": result.compared_rows, "suppressed": len(result.suppressed)})
                key = _upsert_group(db, process, param, result, equal, user_id=user_id)
                touched_keys.setdefault(int(param.id), set()).add(key)
                diagnostics["groups"] += 1
    return diagnostics


def _fragment(ref: TableRef, *, bbox_pdf: list[float], role: str, value: str | None, context: str, confidence: float) -> dict[str, Any]:
    doc = ref.document
    table = ref.table
    bbox = table.bbox_norm(bbox_pdf)
    return {
        "document_version_id": doc.id, "source_fragment_id": None,
        "dataset_file_id": getattr(doc, "dataset_file_id", None), "file_sha256": doc.file_hash or doc.content_hash,
        "stage": {"PD": "project", "RD": "working"}[ref.stage], "discipline": doc.discipline,
        "document_code": doc.document_code, "revision": doc.revision, "approval_status": doc.approval_status,
        "page": ref.page, "bbox": bbox, "bbox_pdf": [round(v, 2) for v in bbox_pdf],
        "page_width": table.width, "page_height": table.height,
        "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
        "extracted_value": value, "role": role, "context": context[:1000], "extractor": EXTRACTOR_NAME, "confidence": confidence,
    }


def _upsert_group(db: Any, process: Any, param: Any, result: PairResult, d: Discrepancy, *, user_id: int | None) -> str:
    from ..db.models import EvidenceFragment
    from .evidence_groups import upsert_evidence_group

    pd, rd = result.pd, result.rd
    specs = []
    for ref, row, role, value in ((pd, d.pd_row, "expected", d.expected), (rd, d.rd_row, "actual", d.actual)):
        if row is not None:
            specs.append(_fragment(ref, bbox_pdf=row.bbox, role=role, value=value, context=_row_summary(ref.table, row), confidence=d.confidence))
        elif d.type == TYPE_TOTAL and ref.table.totals:
            specs.append(_fragment(ref, bbox_pdf=ref.table.totals[0].bbox, role=role, value=value, context=ref.table.totals[0].label, confidence=d.confidence))
    for copies, row, role, value in ((result.pd_copies, d.pd_row, "expected", d.expected), (result.rd_copies, d.rd_row, "actual", d.actual)):
        # the same schedule printed on other sheets of the same file: the discrepancy is on each of them
        for copy in copies:
            twin = _keyed(copy.table)[0].get(tpar.normalize_key(row.key)) if row is not None else None
            if twin is not None:
                specs.append(_fragment(copy, bbox_pdf=twin.bbox, role=role, value=value, context=_row_summary(copy.table, twin), confidence=d.confidence))
            specs.append(_fragment(copy, bbox_pdf=copy.table.bbox, role="table", value=None,
                                   context=f"{copy.table.title or 'таблица'} ({len(copy.table.rows)} строк), копия", confidence=d.confidence))
    for ref, row in ((pd, d.pd_row), (rd, d.rd_row)):
        # the whole schedule of each stage: the zone an inspector (and IoU scoring) looks at; for a room present in one
        # stage only it is also the proof of absence on the other side (role "table" sorts after expected/actual, so
        # the stage value read by the exporter stays the row's)
        absent = row is None and d.type in (TYPE_ONLY_PD, TYPE_ONLY_RD)
        specs.append(_fragment(ref, bbox_pdf=ref.table.bbox, role="table", value="нет строки" if absent else None,
                               context=f"{ref.table.title or 'таблица'} ({len(ref.table.rows)} строк)", confidence=d.confidence))
    confidence = d.confidence - (0.1 if result.alternatives else 0.0) - (0.1 if not (pd.table.scope.floor and rd.table.scope.floor) else 0.0)
    status = "CANDIDATE" if d.violation else "NEGATIVE_VERIFIED"
    delta = {
        "source": EXPLICATION_SOURCE, "matrix_scope": "MATRIX", "parameter_code": str(param.code),
        "location": d.location, "location_type": "APARTMENT" if pd.table.kind == KIND_APARTMENTS and d.type != TYPE_TOTAL else ("FLOOR" if d.type in (TYPE_TOTAL, TYPE_TABLE_EQUAL) else "ROOM"),
        "discrepancy_type": d.type, "field": d.field, "finding_group": result.finding_group,
        "comparison_result": "TRIGGERED" if d.violation else "NON_TRIGGERING",
        "values": {"PD": d.expected, "RD": d.actual},
        "scope": {"PD": pd.table.scope.to_dict(), "RD": rd.table.scope.to_dict()},
        "tables": {"PD": {"title": pd.table.title, "rows": len(pd.table.rows), "page": pd.page, "file_id": pd.file_id},
                   "RD": {"title": rd.table.title, "rows": len(rd.table.rows), "page": rd.page, "file_id": rd.file_id}},
        "pair_score": result.score,
        "revision_alternatives": [alt.file_id for alt in result.alternatives],
        "extraction_method": "text_layer_table_geometry", "gold_validated": False,
        **({"details": d.details} if d.details else {}),
    }
    group_key = "explication:" + hashlib.sha256("|".join([result.finding_group, d.type, str(d.field), d.location]).encode("utf-8")).hexdigest()[:24]
    group, should_write = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version, "model_version": EXPLICATION_COMPARE_VERSION,
            "dataset_version": process.dataset_version, "comparison_scenario": "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE", "comparability_status": "COMPARABLE", "finding_status": status,
            "expected_value": d.expected, "actual_value": d.actual, "review_priority": param.review_priority,
            "confidence": round(max(0.3, confidence), 3), "delta": delta,
        },
        fragment_specs=specs, user_id=user_id,
    )
    if group is not None and should_write:
        for spec in specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key
