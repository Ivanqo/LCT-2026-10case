"""CHANGE_SHEET detector: RD change register entries looked up in the PD of the same discipline (see `change_sheet.py`)."""
from __future__ import annotations

from typing import Any

from .base import (
    CRITICALITY_MINOR,
    CRITICALITY_SUBSTANTIAL,
    METHOD_CHANGE_SHEET,
    EvidenceRef,
    HypothesisCandidate,
    make_dedup_key,
)
from .change_sheet import (
    DETECTOR,
    KIND_ADDED,
    KIND_CHANGED,
    KIND_MOVED,
    KIND_REMOVED,
    KIND_REPLACED,
    MAX_ID_WINDOW_WORDS,
    MAX_WINDOW_WORDS,
    best_window,
    entry_ids,
    entry_kind,
    entry_subject_stems,
    stem_weights,
)

MIN_ID_PARTS = 3
MAX_ID_PAGES = 6
MAX_PAGES_PER_DOC = 6
COVERAGE_MIN = 0.45


def _doc_stage(doc: Any) -> str | None:
    from .. import explication_compare as ec

    return ec._doc_stage(doc)


def _tags(gate: Any, doc: Any) -> frozenset[str]:
    fact = gate.facts.get(int(doc.id)) if gate is not None else None
    return frozenset(getattr(fact, "tags", None) or ())


def candidate_entries(scans: dict[int, dict[str, Any]], rd_docs: list[Any]) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, int]]:
    """Register entries that state a change of something existing and carry something to look for in the PD."""
    out: list[tuple[Any, dict[str, Any]]] = []
    stats = {"rd_documents": len(rd_docs), "documents_with_register": 0, "entries": 0, "entries_added_kind": 0, "entries_no_kind": 0, "entries_no_anchor": 0,
             "low_quality_pages": 0}
    for doc in rd_docs:
        scan = (scans.get(int(doc.id)) or {}).get("changes") or {}
        entries = scan.get("entries") or []
        stats["low_quality_pages"] += len(scan.get("low_quality") or [])
        if entries:
            stats["documents_with_register"] += 1
        for entry in entries:
            stats["entries"] += 1
            kind = entry_kind(entry["text"])
            if kind is None:
                stats["entries_no_kind"] += 1
                continue
            if kind == KIND_ADDED:
                stats["entries_added_kind"] += 1
                continue
            ids = [i for i in entry_ids(entry["text"]) if (i.count(".") + 1 >= MIN_ID_PARTS or "-" in i)]
            subject = entry_subject_stems(entry["text"])
            if not ids and len(subject) < 2:
                stats["entries_no_anchor"] += 1
                continue
            out.append((doc, {**entry, "kind": kind, "ids": ids, "subject": subject}))
    return out, stats


def _id_box(window: dict[str, Any], ids: list[str]) -> list[float] | None:
    """Box of the identifier word(s) inside the window, widened by 30 pt: the place on a drawing the identifier names."""
    from .text_utils import fold

    wanted = {fold(i) for i in ids}
    boxes = [w["bbox"] for w in window["words"] if fold(str(w.get("text") or "").strip(" ,;:()")) in wanted]
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes) - 30, 2), round(min(b[1] for b in boxes) - 30, 2), round(max(b[2] for b in boxes) + 30, 2), round(max(b[3] for b in boxes) + 30, 2)]


def find_pd_hits(entry: dict[str, Any], pd_docs: list[Any], indexes: dict[int, dict[str, Any]], total_pages: int, df_total: dict[str, int],
                 pages_cache: dict | None = None) -> list[dict[str, Any]]:
    """Best PD windows for one entry across the PD documents of the discipline."""
    from .. import dataset_sources

    weights = stem_weights(entry["subject"], df_total, total_pages)
    rare_limit = max(3, int(0.02 * total_pages))
    rare = [s for s in entry["subject"] if 0 < df_total.get(s, 0) <= rare_limit]
    hits: list[dict[str, Any]] = []
    id_pages_total = sum(len((indexes[int(d.id)].get("ids") or {}).get(i, [])) for d in pd_docs if int(d.id) in indexes for i in entry["ids"])
    use_ids = bool(entry["ids"]) and 0 < id_pages_total <= MAX_ID_PAGES
    for doc in pd_docs:
        index = indexes.get(int(doc.id))
        if not index:
            continue
        pages: dict[int, float] = {}
        if use_ids:
            for ident in entry["ids"]:
                for page in (index.get("ids") or {}).get(ident, []):
                    pages[page] = pages.get(page, 0.0) + 5.0
        if rare:
            for stem in entry["subject"]:
                for page in (index.get("inv") or {}).get(stem, []):
                    pages[page] = pages.get(page, 0.0) + weights.get(stem, 0.0)
        if not pages:
            continue
        top = sorted(pages, key=lambda p: (-pages[p], p))[:MAX_PAGES_PER_DOC]
        cache = pages_cache if pages_cache is not None else {}
        missing = [p for p in top if (int(doc.id), p) not in cache]
        if missing:
            try:
                for page, snap in dataset_sources.extract_original_pages(doc, missing).items():
                    cache[(int(doc.id), int(page))] = snap
            except Exception:  # noqa: BLE001 -- an unreadable PD file is simply not a source
                pass
        snapshots = {p: cache[(int(doc.id), p)] for p in top if (int(doc.id), p) in cache}
        for page, snapshot in snapshots.items():
            if snapshot.get("cmap_corruption_suspected"):
                continue
            window = best_window(snapshot.get("words") or [], weights, entry["ids"] if use_ids else [], max_words=MAX_ID_WINDOW_WORDS if use_ids else MAX_WINDOW_WORDS)
            if window is None:
                continue
            matched = window["stems"]
            if use_ids:
                accepted = window["id_hit"]                    # an entry that names an identifier is looked up by it, not by running words
            else:
                hit_rare = [s for s in matched if s in rare]
                accepted = len(matched) >= 2 and bool(hit_rare) and 2 * len(hit_rare) >= len(rare) and window["coverage"] >= COVERAGE_MIN
                if entry["kind"] == KIND_CHANGED and len(entry["subject"]) < 3:
                    accepted = False                           # "something was corrected" with one noun is too vague to look up
            if accepted:
                if window["id_hit"]:
                    window = {**window, "bbox": _id_box(window, entry["ids"]) or window["bbox"]}
                hits.append({**window, "doc": doc, "page": int(page), "width": float(snapshot.get("width") or 0), "height": float(snapshot.get("height") or 0),
                             "by": "identifier" if window["id_hit"] else "words"})
    hits.sort(key=lambda h: (-h["score"], str(getattr(h["doc"], "dataset_file_id", "")), h["page"]))
    return hits[:2]


def detect(documents: list[Any], gate: Any, *, object_id: str | None) -> tuple[list[HypothesisCandidate], dict[str, Any]]:
    from . import doc_scan

    def usable(doc: Any) -> bool:
        return not (gate is not None and gate.drop_from_generic_candidates(doc.id))

    rd_docs = [d for d in documents if _doc_stage(d) == "RD" and usable(d)]
    pd_all = [d for d in documents if _doc_stage(d) == "PD" and usable(d)]
    scans = doc_scan.scan_documents(rd_docs, ["changes"])
    entries, stats = candidate_entries(scans, rd_docs)
    stats["candidate_entries"] = len(entries)
    stats["hypotheses"] = 0
    if not entries:
        return [], stats
    wanted_tags: set[str] = set()
    for doc, _entry in entries:
        wanted_tags |= set(_tags(gate, doc)) or {"АР"}
    pd_docs = [d for d in pd_all if _tags(gate, d) & wanted_tags]
    indexes_raw = doc_scan.scan_documents(pd_docs, ["index"])
    indexes = {doc_id: scan["index"] for doc_id, scan in indexes_raw.items() if scan.get("index") and not scan["index"].get("skipped")}
    stats["pd_documents_indexed"] = len(indexes)
    if not indexes:
        stats["abstained"] = "no_pd_index"
        return [], stats
    total_pages = sum(int(i.get("pages_total") or 0) for i in indexes.values()) or 1
    df_total: dict[str, int] = {}
    for index in indexes.values():
        for stem, count in (index.get("df") or {}).items():
            df_total[stem] = df_total.get(stem, 0) + int(count)
    stats["pd_pages"] = total_pages
    cache: dict[str, list[dict[str, Any]]] = {}
    pages_cache: dict = {}
    out: list[HypothesisCandidate] = []
    for rd_doc, entry in entries:
        discipline = set(_tags(gate, rd_doc)) or {"АР"}
        key = " ".join(sorted(entry["subject"])) + "|" + ",".join(entry["ids"]) + "|" + ",".join(sorted(discipline))
        if key not in cache:
            cache[key] = find_pd_hits(entry, [d for d in pd_docs if _tags(gate, d) & discipline], indexes, total_pages, df_total, pages_cache)
        hits = cache[key]
        if not hits:
            stats["entries_without_pd_match"] = stats.get("entries_without_pd_match", 0) + 1
            continue
        primary = hits[0]
        confidence = (0.55 + 0.25 * min(1.0, primary["coverage"])) if primary["by"] == "identifier" else (0.4 + 0.4 * min(1.0, primary["coverage"]))
        if entry["kind"] == KIND_REMOVED:
            confidence += 0.05
        confidence = round(min(0.85, confidence), 3)
        verb = {KIND_REMOVED: "исключён/удалён", KIND_MOVED: "перемещён", KIND_REPLACED: "заменён", KIND_CHANGED: "изменён"}[entry["kind"]]
        where = f"{getattr(rd_doc, 'dataset_file_id', None) or rd_doc.id}, стр. {entry['page']}"
        pd_where = "; ".join(f"{getattr(h['doc'], 'dataset_file_id', None) or h['doc'].id}, стр. {h['page']}" for h in hits)
        description = (
            f"В РД ({where}) в листе изменений (Изм. {entry['izm'] or '—'}, лист {entry['sheet']}) записано: «{entry['text']}» ({verb}). "
            f"В ПД ({pd_where}) найден соответствующий элемент: «{primary['text'][:200]}». Изменение в ПД не отражено: расхождение возможно, "
            "параметр Матрицы не определён; вывод за инспектором (проверить, согласовано ли изменение)."
        )
        evidence = [EvidenceRef(document=rd_doc, page=int(entry["page"]), stage="RD", role="actual", bbox_pdf=entry["bbox"], page_width=entry["width"],
                                page_height=entry["height"], value=entry["text"], context=f"Лист изменений: Изм. {entry['izm']}, лист {entry['sheet']}",
                                confidence=confidence)]
        for index, hit in enumerate(hits):
            evidence.append(EvidenceRef(document=hit["doc"], page=hit["page"], stage="PD", role="expected" if index == 0 else "context", bbox_pdf=hit["bbox"],
                                        page_width=hit["width"], page_height=hit["height"], value=hit["text"][:200], context=hit["text"][:300], confidence=confidence))
        out.append(HypothesisCandidate(
            method=METHOD_CHANGE_SHEET, detector=DETECTOR,
            dedup_key=make_dedup_key(object_id, METHOD_CHANGE_SHEET, entry["text"], primary["text"]),
            description=description, confidence=confidence, evidence=evidence,
            criticality=CRITICALITY_MINOR if entry["kind"] == KIND_CHANGED else CRITICALITY_SUBSTANTIAL,
            location=(entry["ids"][0] if entry["ids"] else " ".join(entry["subject"][:3])), expected=primary["text"][:200], actual=entry["text"], normative_base=None,
            details={"rule_name": "Изменение, записанное в РД, не отражено в ПД", "change_kind": entry["kind"], "izm": entry["izm"], "sheet": entry["sheet"],
                     "matched_by": primary["by"], "matched_stems": primary["stems"], "coverage": round(primary["coverage"], 3), "evidence_required": ["PD", "RD"]},
            editions=tuple(sorted({int(rd_doc.id), *(int(h["doc"].id) for h in hits)})),
        ))
    stats["hypotheses"] = len(out)
    return out, stats
