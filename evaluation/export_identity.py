"""Step F1: one (parameter code, location) = one object of comparison in the exported submission.

The grader (and any consumer indexing by object + parameter + location, see `metrics._align_predictions`) keeps ONE of
several rows that share the key, so the result used to depend on which row came first or won a tie. A check's
location named a room / floor / the object, not *what was compared*: the same room number of one floor compared with
two RD volumes, a page-quality warning next to a clarification request, and so on all shared it.

`plan_identity` takes the exported groups with their checks (location as before) and decides, deterministically and
independently of the input order:

  * the *object of comparison* of a row: (PD table, RD table, field) for a table pair, (source, reason) for a service
    row (location = the object id itself), the group itself otherwise;
  * rows of ONE object (a true duplicate, or a contradiction): the strongest claim stays, the rest are folded into it
    (`related_group_ids`, `superseded`); the evidence of a fold that says the same thing is merged in;
  * rows of DIFFERENT objects: the strongest keeps the plain location (so a match with the organizer's markup, which
    names the place, is unchanged); the others get a readable qualifier made only of what varies in the set
    (floor, PD table, RD table, field -- or the reason for a service row).

A package without collisions is not touched (byte-identical export). Pure functions, no I/O.
"""
from __future__ import annotations

import json
from typing import Any

# label strength: a claimed violation beats a missing-document statement, a verified negative, an impossible comparison
_LABEL_RANK = {"VIOLATION_PRESENT": 0, "MISSING_DOCUMENT": 1, "NO_VIOLATION": 2, "COMPARISON_IMPOSSIBLE": 3}
_WEAK_SOURCES = {"page_quality": 1}            # a quality warning never outranks the statement it accompanies
_SERVICE_REASON_RU = {
    "LOW_QUALITY": "предупреждение качества",
    "CLARIFICATION_REQUIRED": "запрос уточнения",
    "MISSING_STAGE": "отсутствует документ",
    "MISSING_DISCIPLINE_DOCUMENT": "отсутствует документ раздела",
    "VALUES_NOT_EXTRACTED": "значение не извлечено",
}
_FIELD_RU = {"AREA": "площадь", "NAME": "наименование", "TOTAL": "итог по экспликации", "NUMBER": "номер"}


def normalize_location(value: Any) -> str:
    """Same normalization the scorer applies to a location."""
    return " ".join(str(value or "").strip().lower().split())


def _delta(group: dict[str, Any]) -> dict[str, Any]:
    return group.get("delta") if isinstance(group.get("delta"), dict) else {}


def group_sort_key(group: dict[str, Any]) -> str:
    """Stable per group, independent of the position in the list."""
    key = group.get("group_key")
    if key:
        return str(key)
    return json.dumps({k: group.get(k) for k in ("id", "entity_name", "finding_status")}, sort_keys=True, default=str, ensure_ascii=False)


def _reason(group: dict[str, Any]) -> str:
    return str(_delta(group).get("reason") or "").upper()


def _is_service(group: dict[str, Any], check: dict[str, Any]) -> bool:
    delta = _delta(group)
    object_id = str(check.get("object_id") or group.get("object_id") or "")
    return not delta.get("tables") and (not check["location"] or check["location"] == object_id)


def _table_ref(delta: dict[str, Any], stage: str) -> tuple[str, str, str]:
    """(file, page, title): two tables of one page (floor 1 and the mezzanine) are different tables."""
    table = (delta.get("tables") or {}).get(stage) or {}
    return str(table.get("file_id") or ""), str(table.get("page") or ""), str(table.get("title") or "")


def _floor(delta: dict[str, Any]) -> str:
    scope = delta.get("scope") if isinstance(delta.get("scope"), dict) else {}
    for stage in ("RD", "PD"):
        item = scope.get(stage) or {}
        parts = [str(item[key]) for key in ("building", "section", "floor") if item.get(key)]
        if parts:
            label = ", ".join(parts)
            return f"этаж {label}" if label.isdigit() else label
    return ""


def identity_of(group: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """Components of the object of comparison (what a qualifier can be made of)."""
    delta = _delta(group)
    if delta.get("tables"):
        field = str(delta.get("field") or delta.get("discrepancy_type") or "")
        return {"kind": "pair", "floor": _floor(delta), "pd": _table_ref(delta, "PD"), "rd": _table_ref(delta, "RD"),
                "field": field, "type": str(delta.get("discrepancy_type") or "")}
    if _is_service(group, check):
        return {"kind": "service", "source": str(delta.get("source") or ""), "reason": _reason(group) or str(delta.get("source") or "").upper()}
    return {"kind": "group", "key": group_sort_key(group), "source": str(delta.get("source") or ""),
            "field": str(delta.get("field") or delta.get("discrepancy_type") or "")}


def object_key(identity: dict[str, Any]) -> tuple:
    if identity["kind"] == "pair":
        return ("pair", identity["pd"], identity["rd"], identity["field"], identity["type"])
    if identity["kind"] == "service":
        return ("service", identity["source"], identity["reason"])
    return ("group", identity["key"])


def strength(group: dict[str, Any], check: dict[str, Any]) -> tuple:
    """Smaller = stronger. Deterministic: the last element is the group key."""
    source = str(_delta(group).get("source") or "")
    confidence = check.get("confidence")
    try:
        confidence = -float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    return (_LABEL_RANK.get(str(check.get("violation_label")), 9), _WEAK_SOURCES.get(source, 0), confidence, group_sort_key(group))


def _same_statement(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(key) == b.get(key) for key in ("violation_label", "protocol_status", "pd_value", "rd_value", "id_value"))


def _qualifier(identity: dict[str, Any], varying: set[str], *, only_pair: bool) -> str:
    if identity["kind"] == "service":
        return _SERVICE_REASON_RU.get(identity["reason"], identity["reason"].lower().replace("_", " ") or identity["source"])
    parts: list[str] = []
    if identity["kind"] == "pair":
        if identity["floor"] and ("floor" in varying or only_pair):
            parts.append(identity["floor"])
        for stage_key, label in (("pd", "ПД"), ("rd", "РД")):
            file_id, page, _ = identity[stage_key]
            if file_id and (stage_key in varying or only_pair):
                parts.append(f"{label} {file_id}" + (f" стр. {page}" if page else ""))
        field = identity["field"]
        if field and "field" in varying:
            parts.append(_FIELD_RU.get(field.upper(), field.lower().replace("_", " ")))
    else:
        label = identity["field"] or identity["source"]
        if label:
            parts.append(_FIELD_RU.get(label.upper(), label.lower().replace("_", " ")))
    return "; ".join(parts)


def _varying(identities: list[dict[str, Any]]) -> set[str]:
    """What tells the objects of one place apart. The PD table is named only when the floor does not already say it
    (one PD table per floor is the usual case, and 'этаж 2; ПД стр. 20' would say the same thing twice)."""
    out: set[str] = set()
    pairs = [i for i in identities if i["kind"] == "pair"]
    for key in ("floor", "pd", "rd", "field"):
        if len({i[key] for i in pairs}) > 1:
            out.add(key)
    if "pd" in out and "floor" in out and len({(i["floor"], i["pd"]) for i in pairs}) == len({i["floor"] for i in pairs}):
        out.discard("pd")
    return out


def plan_identity(entries: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """`entries` = [(group, check)] in export order. Returns one plan per entry:
    {"location": str, "fold_into": int | None, "merge_evidence": bool, "qualifier": str | None}.
    `fold_into` is the index of the surviving entry; folded entries are not exported as checks of their own."""
    plans = [{"location": check["location"], "fold_into": None, "merge_evidence": False, "qualifier": None} for _, check in entries]
    buckets: dict[tuple[str, str], list[int]] = {}
    for index, (_, check) in enumerate(entries):
        buckets.setdefault((str(check["parameter_code"]), normalize_location(check["location"])), []).append(index)
    taken = {key for key in buckets}                 # every key in use; qualified locations must not collide with them
    for (code, _), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        identities = {i: identity_of(*entries[i]) for i in members}
        ranked = sorted(members, key=lambda i: strength(*entries[i]))
        by_object: dict[tuple, list[int]] = {}
        for i in ranked:
            by_object.setdefault(object_key(identities[i]), []).append(i)
        survivors: list[int] = []
        for rows in by_object.values():
            head = rows[0]
            survivors.append(head)
            for other in rows[1:]:
                plans[other]["fold_into"] = head
                plans[other]["merge_evidence"] = _same_statement(entries[head][1], entries[other][1])
        survivors.sort(key=lambda i: strength(*entries[i]))
        if len(survivors) < 2:
            continue
        varying = _varying([identities[i] for i in survivors])
        pair_count = sum(identities[i]["kind"] == "pair" for i in survivors)
        used: set[str] = {normalize_location(entries[survivors[0]][1]["location"])}
        for position, i in enumerate(survivors[1:], start=1):
            base = entries[i][1]["location"]
            qualifier = _qualifier(identities[i], varying, only_pair=pair_count == 1) or f"вариант {position + 1}"
            candidate = f"{base} ({qualifier})"
            ordinal = position + 1
            while (code, normalize_location(candidate)) in taken or normalize_location(candidate) in used:
                ordinal += 1
                candidate = f"{base} ({qualifier}, вариант {ordinal})"
            taken.add((code, normalize_location(candidate)))
            used.add(normalize_location(candidate))
            plans[i]["location"] = candidate
            plans[i]["qualifier"] = qualifier
    return plans
