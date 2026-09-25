from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .db.models import Model, Element, Metric, Property, Quantity
from .db.session import init_db, get_db
from .logging_setup import setup_logging
from .schemas import ImportResponse, ModelStatus, ModelItem, HeavyQueryRequest, HeavyQueryResponse, DeleteModelResponse, IfcContextRequest, IfcContextResponse, IfcContextItem, IfcObservationExportItem
from .tasks import parse_and_index, compute_metrics

setup_logging("ifc")
app = FastAPI(title="IFC Service", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def org_scope(organization_id: int | None) -> str:
    return f"org_{organization_id or 0}"


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/models/import", response_model=ImportResponse)
async def import_model(project_id: int = Form(...), organization_id: int | None = Form(default=None), original_filename: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    file_hash = hashlib.sha256(data).hexdigest()
    proj_dir = settings.UPLOADS_DIR / org_scope(organization_id) / str(project_id)
    proj_dir.mkdir(parents=True, exist_ok=True)
    stored_path = proj_dir / f"{file_hash[:16]}_{original_filename}"
    stored_path.write_bytes(data)
    model = Model(organization_id=organization_id, project_id=int(project_id), original_filename=original_filename, stored_path=str(stored_path), file_hash=file_hash, status="queued")
    db.add(model)
    db.commit()
    db.refresh(model)
    parse_and_index.delay(model.model_id)
    return ImportResponse(model_id=model.model_id)


@app.get("/models", response_model=list[ModelItem])
def list_models(project_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    rows = db.execute(select(Model).where(Model.project_id == project_id, Model.organization_id == organization_id).order_by(Model.created_at.desc())).scalars().all()
    return [ModelItem(model_id=row.model_id, organization_id=row.organization_id, project_id=row.project_id, original_filename=row.original_filename, created_at=row.created_at, status=row.status, source_type="ifc") for row in rows]


def _cleanup_empty_parents(path_value: str | None, stop_at: Path) -> None:
    if not path_value:
        return
    try:
        path = Path(path_value).parent
        stop_at = stop_at.resolve()
        while path.exists() and path != stop_at and stop_at in path.resolve().parents:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
    except Exception:
        return


@app.delete("/models/{model_id}", response_model=DeleteModelResponse)
def delete_model(model_id: int, organization_id: int | None = None, db: Session = Depends(get_db)):
    model = db.execute(select(Model).where(Model.model_id == model_id, Model.organization_id == organization_id)).scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    deleted_files = 0
    if model.stored_path:
        path = Path(model.stored_path)
        if path.exists():
            path.unlink(missing_ok=True)
            deleted_files += 1

    stored_path = model.stored_path
    db.delete(model)
    db.commit()
    _cleanup_empty_parents(stored_path, settings.UPLOADS_DIR)
    return DeleteModelResponse(model_id=model_id, deleted_files=deleted_files)


@app.get("/models/{model_id}/status", response_model=ModelStatus)
def status(model_id: int, db: Session = Depends(get_db)):
    model = db.get(Model, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return ModelStatus(model_id=model.model_id, organization_id=model.organization_id, project_id=model.project_id, status=model.status, error=model.error, created_at=model.created_at, indexed_at=model.indexed_at)


@app.get("/models/{model_id}/observations", response_model=list[IfcObservationExportItem])
def export_observations(model_id: int, organization_id: int | None = None, limit: int = 500, db: Session = Depends(get_db)):
    model = db.get(Model, model_id)
    if not model or model.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Model not found")
    if model.status != "indexed":
        raise HTTPException(status_code=409, detail=f"Model status is '{model.status}', not ready")

    limit = max(1, min(int(limit or 500), 5000))
    elements = (
        db.query(Element)
        .options(joinedload(Element.properties), joinedload(Element.quantities), joinedload(Element.metrics))
        .filter(Element.model_id == int(model_id))
        .order_by(Element.element_id.asc())
        .limit(limit)
        .all()
    )
    out: list[IfcObservationExportItem] = []
    for element in elements:
        props: dict[str, Any] = {}
        for prop in element.properties or []:
            key = f"{prop.pset}.{prop.name}" if prop.pset else str(prop.name)
            props[key] = prop.value_num if prop.value_num is not None else prop.value_bool if prop.value_bool is not None else prop.value_text
        quantities: dict[str, Any] = {}
        for quantity in element.quantities or []:
            key = f"{quantity.qto}.{quantity.name}" if quantity.qto else str(quantity.name)
            quantities[key] = {"value": quantity.value_num, "unit": quantity.unit}
        metric = (element.metrics or [None])[0]
        metrics = {}
        if metric:
            metrics = {
                "area": metric.area,
                "volume": metric.volume,
                "bbox_min": metric.bbox_min,
                "bbox_max": metric.bbox_max,
                "source": metric.source,
            }
        summary_parts = [
            f"IFC {element.ifc_type}",
            f"GUID {element.guid}",
        ]
        if element.name:
            summary_parts.append(f"name {element.name}")
        if element.storey_guid:
            summary_parts.append(f"storey {element.storey_guid}")
        out.append(
            IfcObservationExportItem(
                external_id=f"element:{element.element_id}",
                guid=str(element.guid or ""),
                ifc_type=str(element.ifc_type or ""),
                name=element.name,
                storey_guid=element.storey_guid,
                container_guid=element.container_guid,
                attributes=props,
                quantities=quantities,
                metrics=metrics,
                text="; ".join(summary_parts),
            )
        )
    return out


@app.post("/models/{model_id}/compute")
def trigger_compute(model_id: int, body: dict, db: Session = Depends(get_db)):
    model = db.get(Model, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    metrics = body.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        raise HTTPException(status_code=400, detail="Body must contain metrics: [...]")
    compute_metrics.delay(model_id, metrics)
    return {"ok": True, "model_id": model_id, "queued_metrics": metrics}


@app.post("/query/heavy", response_model=HeavyQueryResponse)
def heavy_query(payload: HeavyQueryRequest, db: Session = Depends(get_db)):
    model = db.get(Model, payload.model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if model.status != "indexed":
        raise HTTPException(status_code=409, detail=f"Model status is '{model.status}', not ready")
    spec = payload.query_spec or {}
    ifc_types = spec.get("ifc_type")
    group_by = spec.get("group_by") or []
    want_metrics = spec.get("metrics") or []
    limit = int(spec.get("limit") or 200)
    q = db.query(Element).filter(Element.model_id == payload.model_id)
    if ifc_types:
        if isinstance(ifc_types, str):
            ifc_types = [ifc_types]
        q = q.filter(Element.ifc_type.in_(list(ifc_types)))
    rows: list[dict] = []
    if group_by:
        gb_cols = []
        if "storey_guid" in group_by:
            gb_cols.append(Element.storey_guid)
        if "ifc_type" in group_by:
            gb_cols.append(Element.ifc_type)
        mq = q.join(Metric, Metric.element_id == Element.element_id, isouter=True)
        selects = [c.label(c.key) for c in gb_cols]
        if "area" in want_metrics:
            selects.append(func.sum(Metric.area).label("sum_area"))
        if "volume" in want_metrics:
            selects.append(func.sum(Metric.volume).label("sum_volume"))
        selects.append(func.count(Element.element_id).label("count_elements"))
        for r in mq.with_entities(*selects).group_by(*gb_cols).limit(limit).all():
            rows.append({k: v for k, v in r._asdict().items()})
    else:
        for el in q.limit(limit).all():
            rows.append({"guid": el.guid, "ifc_type": el.ifc_type, "name": el.name, "storey_guid": el.storey_guid, "container_guid": el.container_guid})
    return HeavyQueryResponse(rows=rows)


_TOKEN_RX = re.compile(r"[A-Za-zА-Яа-яЁё0-9_\-/]+")
_IFC_TYPE_RX = re.compile(r"ifc[a-z0-9_]+", re.IGNORECASE)
_STOPWORDS = {
    "и", "в", "во", "на", "по", "с", "со", "к", "у", "из", "для", "как", "что", "это", "есть", "ли",
    "где", "какой", "какая", "какие", "какое", "покажи", "найди", "скажи", "проект", "модель",
    "the", "and", "for", "with", "from",
}
_RU_ENDINGS = (
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ими", "ыми", "ией", "ия", "ья", "ье", "ью",
    "ах", "ях", "ов", "ев", "ей", "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ам", "ям",
    "ом", "ем", "ым", "им", "ых", "их", "ою", "ею", "ы", "и", "а", "я", "о", "е", "у", "ю",
)
_IFC_TYPE_KEYWORDS = {
    "wall": "IfcWall",
    "walls": "IfcWall",
    "стен": "IfcWall",
    "door": "IfcDoor",
    "doors": "IfcDoor",
    "двер": "IfcDoor",
    "window": "IfcWindow",
    "windows": "IfcWindow",
    "окн": "IfcWindow",
    "slab": "IfcSlab",
    "плит": "IfcSlab",
    "перекрыти": "IfcSlab",
    "column": "IfcColumn",
    "columns": "IfcColumn",
    "колонн": "IfcColumn",
    "beam": "IfcBeam",
    "beams": "IfcBeam",
    "балк": "IfcBeam",
    "space": "IfcSpace",
    "spaces": "IfcSpace",
    "помещен": "IfcSpace",
    "roof": "IfcRoof",
    "roofs": "IfcRoof",
    "крыш": "IfcRoof",
    "stair": "IfcStair",
    "stairs": "IfcStair",
    "лестниц": "IfcStair",
}
_AREA_TERMS = {"area", "площад", "м2", "квадрат"}
_VOLUME_TERMS = {"volume", "объем", "обьем", "м3", "куб"}


def _normalize_search_text(text: object) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def _russian_stem(token: str) -> str:
    token = _normalize_search_text(token)
    if not re.search(r"[а-я]", token):
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def _token_variants(token: str) -> set[str]:
    token = _normalize_search_text(token)
    variants = {token}
    stem = _russian_stem(token)
    if len(stem) >= 4:
        variants.add(stem)
    return variants


def _question_tokens(text: str) -> list[str]:
    raw = [_normalize_search_text(t) for t in _TOKEN_RX.findall(_normalize_search_text(text))]
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        if len(token) < 3 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _is_ifc_type_token(token: str) -> bool:
    variants = _token_variants(token)
    return any(key in variants or any(v.startswith(key) for v in variants) for key in _IFC_TYPE_KEYWORDS)


def _is_metric_token(token: str) -> bool:
    variants = _token_variants(token)
    return bool(variants & _AREA_TERMS or variants & _VOLUME_TERMS)

def _guess_ifc_types(question: str) -> list[str]:
    explicit = [m.group(0) for m in _IFC_TYPE_RX.finditer(question or "")]
    if explicit:
        return sorted({x[0].upper() + x[1:] for x in explicit})
    guessed: set[str] = set()
    for token in _question_tokens(question):
        variants = _token_variants(token)
        for key, ifc_type in _IFC_TYPE_KEYWORDS.items():
            if key in variants or any(v.startswith(key) for v in variants):
                guessed.add(ifc_type)
    return sorted(guessed)

def _matches_metric(question: str, metric_name: str) -> bool:
    tokens = _question_tokens(question)
    variants = set().union(*(_token_variants(token) for token in tokens)) if tokens else set()
    if metric_name == "area":
        return bool(variants & _AREA_TERMS)
    if metric_name == "volume":
        return bool(variants & _VOLUME_TERMS)
    return False

@app.post("/query/context", response_model=IfcContextResponse)
def build_ifc_context(payload: IfcContextRequest, db: Session = Depends(get_db)):
    models = db.execute(select(Model).where(Model.project_id == payload.project_id, Model.organization_id == payload.organization_id, Model.status == "indexed").order_by(Model.indexed_at.desc().nullslast(), Model.created_at.desc()).limit(payload.max_models)).scalars().all()
    if not models:
        return IfcContextResponse(context_text="", items=[], used_model_ids=[])
    tokens = _question_tokens(payload.question)
    guessed_ifc_types = _guess_ifc_types(payload.question)
    want_area = _matches_metric(payload.question, "area")
    want_volume = _matches_metric(payload.question, "volume")
    free_text_tokens = [
        token for token in tokens
        if not _is_ifc_type_token(token) and not _is_metric_token(token)
    ]
    items: list[IfcContextItem] = []
    for model in models:
        q = db.query(Element).options(joinedload(Element.properties), joinedload(Element.quantities), joinedload(Element.metrics)).filter(Element.model_id == model.model_id)
        if guessed_ifc_types:
            q = q.filter(Element.ifc_type.in_(guessed_ifc_types))
        if free_text_tokens:
            clauses = []
            for token in free_text_tokens[:8]:
                for variant in _token_variants(token):
                    like = f"%{variant}%"
                    clauses.extend([Element.name.ilike(like), Element.ifc_type.ilike(like), Element.guid.ilike(like), Element.storey_guid.ilike(like), Property.name.ilike(like), Property.value_text.ilike(like), Quantity.name.ilike(like)])
            q = q.outerjoin(Property, Property.element_id == Element.element_id).outerjoin(Quantity, Quantity.element_id == Element.element_id).filter(or_(*clauses))
        rows = q.distinct(Element.element_id).limit(payload.limit).all()
        for el in rows:
            relevance = 0.0
            hay = " ".join([str(el.ifc_type or ""), str(el.name or ""), str(el.guid or ""), str(el.storey_guid or ""), " ".join(f"{p.pset or ''} {p.name or ''} {p.value_text or ''}" for p in (el.properties or [])[:20]), " ".join(f"{q.qto or ''} {q.name or ''} {q.value_num or ''}" for q in (el.quantities or [])[:20])]).lower()
            hay = _normalize_search_text(hay)
            for token in free_text_tokens:
                if any(variant in hay for variant in _token_variants(token)): relevance += 1.0
            if guessed_ifc_types and el.ifc_type in guessed_ifc_types: relevance += 2.0
            metric = el.metrics[0] if el.metrics else None
            props = ", ".join(f"{p.pset + '.' if p.pset else ''}{p.name}={p.value_text}" for p in (el.properties or [])[:4] if p.name and (p.value_text is not None or p.value_num is not None))
            qtos = ", ".join(f"{q.name}={q.value_num}{(' ' + q.unit) if q.unit else ''}" for q in (el.quantities or [])[:4] if q.name and q.value_num is not None)
            metric_parts = []
            if metric and want_area and metric.area is not None: metric_parts.append(f"area={metric.area}")
            if metric and want_volume and metric.volume is not None: metric_parts.append(f"volume={metric.volume}")
            summary_parts = [f"Модель {model.original_filename}", f"элемент {el.ifc_type}", f"GUID {el.guid}"]
            if el.name: summary_parts.append(f"наименование: {el.name}")
            if el.storey_guid: summary_parts.append(f"этаж/контейнер: {el.storey_guid}")
            if metric_parts: summary_parts.append("метрики: " + ", ".join(metric_parts))
            if qtos: summary_parts.append("количества: " + qtos)
            if props: summary_parts.append("свойства: " + props)
            items.append(IfcContextItem(model_id=model.model_id, file_name=model.original_filename, summary="; ".join(summary_parts), row_data={"guid": el.guid, "ifc_type": el.ifc_type, "name": el.name, "storey_guid": el.storey_guid, "has_metric": bool(metric)}, relevance=float(relevance)))
        if (want_area or want_volume) and guessed_ifc_types:
            agg_query = db.query(Element.ifc_type.label("ifc_type"), func.count(Element.element_id).label("count_elements"), func.sum(Metric.area).label("sum_area"), func.sum(Metric.volume).label("sum_volume")).join(Metric, Metric.element_id == Element.element_id, isouter=True).filter(Element.model_id == model.model_id, Element.ifc_type.in_(guessed_ifc_types)).group_by(Element.ifc_type)
            for row in agg_query.all():
                agg_parts = [f"Сводка по модели {model.original_filename}", f"тип {row.ifc_type}", f"количество={int(row.count_elements or 0)}"]
                if want_area and row.sum_area is not None: agg_parts.append(f"суммарная площадь={row.sum_area}")
                if want_volume and row.sum_volume is not None: agg_parts.append(f"суммарный объём={row.sum_volume}")
                items.append(IfcContextItem(model_id=model.model_id, file_name=model.original_filename, summary="; ".join(agg_parts), row_data={"ifc_type": row.ifc_type, "count_elements": int(row.count_elements or 0), "sum_area": row.sum_area, "sum_volume": row.sum_volume, "aggregate": True}, relevance=6.0))
    ranked = sorted(items, key=lambda x: (-x.relevance, x.file_name, x.summary))[: payload.limit]
    context_text = "\n".join(f"- {item.summary}" for item in ranked)
    used_model_ids = sorted({item.model_id for item in ranked})
    return IfcContextResponse(context_text=context_text, items=ranked, used_model_ids=used_model_ids)
