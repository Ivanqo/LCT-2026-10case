from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .celery_app import celery
from .db.session import SessionLocal
from .db.models import Model, Element, Property, Quantity, Relation, Metric

logger = logging.getLogger(__name__)


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return float(v)
        return float(v)
    except Exception:
        return None


def _as_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


@celery.task(name="ifc.parse_and_index")
def parse_and_index(model_id: int) -> dict:
    """Parse IFC file and populate index tables."""
    db: Session = SessionLocal()
    try:
        model = db.get(Model, model_id)
        if not model:
            return {"ok": False, "error": f"model {model_id} not found"}

        model.status = "importing"
        model.error = None
        db.commit()

        try:
            import ifcopenshell  # type: ignore
            import ifcopenshell.util.element  # type: ignore
        except Exception as e:
            model.status = "failed"
            model.error = f"ifcopenshell import failed: {e}"
            db.commit()
            return {"ok": False, "error": model.error}

        path = Path(model.stored_path)
        if not path.exists():
            model.status = "failed"
            model.error = "IFC file not found on disk"
            db.commit()
            return {"ok": False, "error": model.error}

        # Clear previous index in case of reimport
        db.execute(delete(Metric).where(Metric.element_id.in_(db.query(Element.element_id).filter(Element.model_id==model_id))))
        db.execute(delete(Property).where(Property.element_id.in_(db.query(Element.element_id).filter(Element.model_id==model_id))))
        db.execute(delete(Quantity).where(Quantity.element_id.in_(db.query(Element.element_id).filter(Element.model_id==model_id))))
        db.execute(delete(Relation).where(Relation.model_id == model_id))
        db.execute(delete(Element).where(Element.model_id == model_id))
        db.commit()

        ifc = ifcopenshell.open(str(path))
        model.schema = getattr(ifc, "schema", None)
        db.commit()

        # Storey map: storey guid -> name
        storey_by_id: Dict[int, str] = {}
        for st in ifc.by_type("IfcBuildingStorey"):
            try:
                storey_by_id[st.id()] = st.GlobalId
            except Exception:
                continue

        # Elements
        count = 0
        for el in ifc.by_type("IfcElement"):
            guid = getattr(el, "GlobalId", None)
            if not guid:
                continue
            ifc_type = el.is_a()
            name = getattr(el, "Name", None)

            storey_guid = None
            container_guid = None
            try:
                container = ifcopenshell.util.element.get_container(el)
                if container is not None:
                    container_guid = getattr(container, "GlobalId", None)
                    if container.is_a("IfcBuildingStorey"):
                        storey_guid = getattr(container, "GlobalId", None)
            except Exception:
                pass

            row = Element(
                model_id=model_id,
                guid=str(guid),
                ifc_type=str(ifc_type),
                name=_as_text(name),
                storey_guid=_as_text(storey_guid),
                container_guid=_as_text(container_guid),
            )
            db.add(row)
            db.flush()  # get element_id

            # Properties/QTO (best-effort)
            try:
                psets = ifcopenshell.util.element.get_psets(el) or {}
                for pset_name, props in psets.items():
                    for prop_name, prop_val in (props or {}).items():
                        # store as best effort (text + numeric)
                        db.add(Property(
                            element_id=row.element_id,
                            pset=_as_text(pset_name),
                            name=_as_text(prop_name) or "",
                            value_text=_as_text(prop_val),
                            value_num=_as_float(prop_val),
                            value_bool="true" if prop_val is True else ("false" if prop_val is False else None),
                        ))
            except Exception:
                pass

            try:
                qtos = ifcopenshell.util.element.get_qto(el) or {}
                for qto_name, qprops in qtos.items():
                    for qname, qval in (qprops or {}).items():
                        db.add(Quantity(
                            element_id=row.element_id,
                            qto=_as_text(qto_name),
                            name=_as_text(qname) or "",
                            value_num=_as_float(qval),
                            unit=None,
                        ))
            except Exception:
                pass

            # Cache metrics from quantities if obvious
            # Common names vary; we store area/volume if present
            area = None
            volume = None
            try:
                # scan quantities inserted in-memory not committed; reuse qtos
                for qto_name, qprops in (qtos or {}).items():  # type: ignore[name-defined]
                    for qname, qval in (qprops or {}).items():
                        qn = str(qname).lower()
                        if area is None and "area" in qn:
                            area = _as_float(qval)
                        if volume is None and "volume" in qn:
                            volume = _as_float(qval)
            except Exception:
                pass

            if area is not None or volume is not None:
                db.add(Metric(
                    element_id=row.element_id,
                    area=area,
                    volume=volume,
                    bbox_min=None,
                    bbox_max=None,
                    source="qto",
                ))

            count += 1
            if count % 500 == 0:
                db.commit()

        db.commit()
        model.status = "indexed"
        model.indexed_at = datetime.utcnow()
        db.commit()

        logger.info("Indexed IFC model_id=%s elements=%s", model_id, count)
        return {"ok": True, "elements": count}
    except Exception as e:
        logger.exception("parse_and_index failed: %s", e)
        try:
            model = db.get(Model, model_id)
            if model:
                model.status = "failed"
                model.error = str(e)
                db.commit()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


@celery.task(name="ifc.compute_metrics")
def compute_metrics(model_id: int, metrics: list[str]) -> dict:
    """Compute missing metrics (best-effort). Placeholder for geometry-based computations."""
    db: Session = SessionLocal()
    try:
        model = db.get(Model, model_id)
        if not model:
            return {"ok": False, "error": "model not found"}

        # NOTE: true geometry computations would require shape creation; here we only mark intent.
        updated = 0
        els = db.query(Element).filter(Element.model_id == model_id).all()
        for el in els:
            m = db.query(Metric).filter(Metric.element_id == el.element_id).first()
            if not m:
                m = Metric(element_id=el.element_id, source="computed")
                db.add(m)

            # if requested metric missing, try derive from quantities
            if "area" in metrics and m.area is None:
                q = db.query(Quantity).filter(Quantity.element_id == el.element_id, Quantity.name.ilike("%area%")).first()
                if q and q.value_num is not None:
                    m.area = q.value_num
                    updated += 1
            if "volume" in metrics and m.volume is None:
                q = db.query(Quantity).filter(Quantity.element_id == el.element_id, Quantity.name.ilike("%volume%")).first()
                if q and q.value_num is not None:
                    m.volume = q.value_num
                    updated += 1

        db.commit()
        return {"ok": True, "updated": updated}
    except Exception as e:
        logger.exception("compute_metrics failed: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()
