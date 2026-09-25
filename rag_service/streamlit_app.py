from __future__ import annotations

import os
import time
from pathlib import Path

import streamlit as st

import math
import json

def _bbox_list(bbox_json: str) -> list[int]:
    try:
        return [int(x) for x in json.loads(bbox_json or "[]")]
    except Exception:
        return [0, 0, 0, 0]

def _intersect_area(a: list[int], b: list[int]) -> int:
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0
    return (x1 - x0) * (y1 - y0)

def _contains(outer: list[int], inner: list[int]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]

def _rect_gap(a: list[int], b: list[int]) -> float:
    dx = max(0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0, max(a[1] - b[3], b[1] - a[3]))
    return math.sqrt(dx * dx + dy * dy)

def _group_text_for_drawing(d_bbox: list[int], text_regions: list, page_w: int, page_h: int) -> list:
    diag = math.sqrt(float(page_w) ** 2 + float(page_h) ** 2) if page_w and page_h else 3000.0
    near_thr = max(18.0, diag * 0.012)  # same as backend default
    out = []
    for t in text_regions:
        tb = _bbox_list(t.bbox_json)
        txt = (t.ocr_text or "").strip()
        if not txt:
            continue
        if _contains(d_bbox, tb):
            out.append(("inside", 0.0, t))
            continue
        inter = _intersect_area(d_bbox, tb)
        if inter > 0:
            # overlap ratio over text area
            t_area = max(1, (tb[2]-tb[0])*(tb[3]-tb[1]))
            score = (1.0 - min(1.0, inter / t_area)) * 1000.0
            out.append(("intersect", score, t))
            continue
        gap = _rect_gap(d_bbox, tb)
        if gap <= near_thr:
            out.append(("near", gap, t))
    out.sort(key=lambda x: x[1])
    return out

from PIL import Image, ImageDraw  # type: ignore
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Document, Page, Region
from app.db.session import SessionLocal, init_db
from app.layout_ingestion import ingest_pdf_layout
from app.vectorstore import get_vectorstores

try:
    from app.admin import delete_document_all
except Exception:
    delete_document_all = None  # type: ignore


st.set_page_config(page_title="RAG Layout Tester", layout="wide")


def get_db() -> Session:
    return SessionLocal()


def draw_regions(page_img: Image.Image, regions: list[Region]) -> Image.Image:
    img = page_img.copy()
    draw = ImageDraw.Draw(img)
    for r in regions:
        bbox = r.bbox
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        draw.rectangle([x0, y0, x1, y1], width=3)
        draw.text((x0 + 4, y0 + 4), f"{r.region_type}#{r.id}")
    return img


def main() -> None:
    st.title("📄 RAG Layout Tester (Sheets/Regions)")
    st.caption(
        "Загрузка PDF → разбиение на объекты (page/title_block/drawing/table/text) → OCR → индексация. "
        "Проверяй по страницам разметку, bbox, кропы и текст/summary."
    )

    init_db()

    # Important: vectorstore init can be heavy (model download). Make it optional.
    with st.sidebar:
        st.header("Настройки")
        project_id = st.number_input("project_id", min_value=1, value=1, step=1)
        dpi = st.slider("Render DPI", min_value=90, max_value=350, value=int(settings.RENDER_DPI), step=10)
        # Стандартизируем под строительные PDF: OCR всегда включен, языки rus+eng.
        # Оставляем только галку "большие регионы" (чертеж/страница), т.к. это реально влияет на скорость.
        enable_large_ocr = st.checkbox(
            "OCR на больших регионах (чертеж/страница) — медленно",
            value=bool(getattr(settings, "ENABLE_OCR_LARGE_REGIONS", False)),
        )

        # Всегда используем YOLO и всегда ищем таблицы.
        yolo_conf = st.slider(
            "YOLO conf",
            min_value=0.05,
            max_value=0.80,
            value=float(getattr(settings, "YOLO_CONF", 0.25)),
            step=0.05,
        )
        enable_tables = True
        max_pages = st.number_input("Max pages (0 = all)", min_value=0, value=int(getattr(settings, "MAX_PAGES", 0)), step=1)
        # Индексация в Chroma всегда включена для режима разработки.
        index_in_chroma = True
        st.markdown("---")
        st.write("Data dir:", str(settings.DATA_DIR))
        st.write("DB:", str(settings.SQLITE_PATH))

    # Apply session overrides
    settings.RENDER_DPI = int(dpi)
    settings.OCR_LANG = "rus+eng"
    settings.ENABLE_OCR = True
    settings.LAYOUT_DETECTOR = "yolo"
    settings.ENABLE_TABLE_DETECTION = True
    settings.YOLO_CONF = float(yolo_conf)
    if hasattr(settings, "ENABLE_OCR_LARGE_REGIONS"):
        settings.ENABLE_OCR_LARGE_REGIONS = bool(enable_large_ocr)  # type: ignore

    os.environ["RAG_ENABLE_TABLE_DETECT"] = "1"
    os.environ["RAG_MAX_PAGES"] = str(int(max_pages))

    uploaded = st.file_uploader("Загрузи PDF для обработки (layout ingest)", type=["pdf"])

    col_a, col_b = st.columns([1, 2], vertical_alignment="top")

    with col_a:
        if uploaded is not None:
            original_filename = uploaded.name
            data = uploaded.getvalue()  # IMPORTANT: safe with Streamlit reruns
            st.write(f"Файл: **{original_filename}** ({len(data)/1024:.1f} KB)")

            if st.button("🚀 Запустить обработку (layout ingest)", type="primary"):
                status = st.status("Обработка…", expanded=True)
                status.write("Подготовка...")

                proj_dir = settings.UPLOADS_DIR / str(int(project_id))
                proj_dir.mkdir(parents=True, exist_ok=True)
                safe_name = original_filename.replace("\\", "_").replace("/", "_")
                stored_path = proj_dir / f"streamlit_{int(time.time())}_{safe_name}"
                stored_path.write_bytes(data)
                status.write(f"Сохранён файл: {stored_path}")
                status.write(f"DPI: {settings.RENDER_DPI} OCR: {settings.ENABLE_OCR} Large OCR: {getattr(settings,'ENABLE_OCR_LARGE_REGIONS',False)} Lang: {settings.OCR_LANG}")
                status.write(f"Table detect: {enable_tables} | Max pages: {max_pages or 'all'}")

                # Progress widgets
                page_bar = st.progress(0)
                region_bar = st.progress(0)
                page_line = st.empty()
                region_line = st.empty()

                def progress_fn(event: str, payload: dict) -> None:
                    # Called from the ingestion loop (same thread)
                    if event == "page_start":
                        p = int(payload.get("page_number", 0))
                        t = int(payload.get("total_pages", 1))
                        page_line.write(f"**Page {p}/{t}**: render/layout")
                        page_bar.progress(min(1.0, max(0.0, (p - 1) / max(1, t))), text=f"Страницы: {p-1}/{t}")
                        region_bar.progress(0, text="Регионы: 0")
                    elif event == "region":
                        p = int(payload.get("page_number", 0))
                        i = int(payload.get("region_index", 0))
                        n = int(payload.get("regions_total", 1))
                        rt = payload.get("region_type", "")
                        region_line.write(f"Page {p}: region {i}/{n} ({rt})")
                        region_bar.progress(min(1.0, max(0.0, i / max(1, n))), text=f"Регионы: {i}/{n} ({rt})")
                    elif event == "page_done":
                        p = int(payload.get("page_number", 0))
                        t = int(payload.get("total_pages", 1))
                        page_bar.progress(min(1.0, max(0.0, p / max(1, t))), text=f"Страницы: {p}/{t}")

                db = get_db()
                try:
                    vs = get_vectorstores() if index_in_chroma else None
                    if vs is None:
                        # Create a lightweight dummy object with no-op upserts
                        class _NoOpVS:  # noqa: N801
                            def upsert_text(self, *args, **kwargs):
                                return None

                            def upsert_asset(self, *args, **kwargs):
                                return None

                        vs = _NoOpVS()  # type: ignore

                    doc_id, pages_created, regions_created, text_chunks_created, asset_records_created = ingest_pdf_layout(
                        db=db,
                        vectorstores=vs,  # type: ignore
                        project_id=int(project_id),
                        stored_path=stored_path,
                        original_filename=original_filename,
                        render_dpi=int(dpi),
                        enable_ocr=True,
                        enable_large_ocr=bool(enable_large_ocr),
                        ocr_lang="rus+eng",
                        enable_tables=True,
                        max_pages=int(max_pages),
                        progress_fn=progress_fn,
                    )
                    status.update(label="Готово ✅", state="complete", expanded=False)
                    st.success(
                        f"Готово: doc_id={doc_id} pages={pages_created} regions={regions_created} "
                        f"text_chunks={text_chunks_created} assets={asset_records_created}"
                    )
                    st.session_state["last_doc_id"] = doc_id
                except Exception as e:
                    status.update(label="Ошибка ❌", state="error", expanded=True)
                    st.exception(e)
                finally:
                    db.close()

    with col_b:
        db = get_db()
        try:
            st.subheader("Документы проекта")
            docs = (
                db.query(Document)
                .filter(Document.project_id == int(project_id))
                .order_by(Document.uploaded_at.desc())
                .all()
            )
            if not docs:
                st.info("Пока нет документов. Загрузи PDF слева.")
                return

            doc_labels = {f"{d.id}: {d.filename} ({d.uploaded_at})": d.id for d in docs}
            default_doc_id = st.session_state.get("last_doc_id", docs[0].id)
            selected_label = st.selectbox(
                "Выбери document_id",
                options=list(doc_labels.keys()),
                index=list(doc_labels.values()).index(default_doc_id) if default_doc_id in doc_labels.values() else 0,
            )
            doc_id = int(doc_labels[selected_label])

            # Delete
            del_col1, del_col2 = st.columns([1, 3])
            with del_col1:
                if st.button("🗑️ Удалить документ", type="secondary"):
                    if delete_document_all is None:
                        st.error("В проекте нет модуля удаления (app/admin.py).")
                    else:
                        try:
                            delete_document_all(db=db, project_id=int(project_id), document_id=doc_id)
                            st.success("Удалено")
                            st.session_state.pop("last_doc_id", None)
                            st.rerun()
                        except Exception as e:
                            st.exception(e)

            pages = db.query(Page).filter(Page.document_id == int(doc_id)).order_by(Page.page_number.asc()).all()
            if not pages:
                st.warning("У документа нет страниц (возможно, он загружен через старый ingest без layout).")
                return

            page_numbers = [p.page_number for p in pages]
            page_no = st.slider(
                "Страница",
                min_value=min(page_numbers),
                max_value=max(page_numbers),
                value=min(page_numbers),
                step=1,
            )
            page = next(p for p in pages if p.page_number == page_no)

            regions = db.query(Region).filter(Region.page_id == page.id).order_by(Region.id.asc()).all()
            if not page.image_path:
                st.error("Не найден page_image.")
                return

            page_img = Image.open(page.image_path).convert("RGB")
            overlay = draw_regions(page_img, regions)

            st.image(overlay, caption=f"Page {page_no} (overlay regions)", width="stretch")

            st.markdown("### Объекты на странице")
            for r in regions:
                with st.expander(f"{r.region_type}  (region_id={r.id})  bbox={r.bbox}", expanded=False):
                    cols = st.columns([1, 2])
                    with cols[0]:
                        if r.image_path and Path(r.image_path).exists():
                            st.image(Image.open(r.image_path).convert("RGB"), caption="Crop", width="stretch")
                        else:
                            st.write("Crop image: отсутствует")
                    with cols[1]:
                        st.write("**Summary:**", r.summary or "")
                        st.write("**OCR/Text:**")
                        full_txt = (r.ocr_text or "").strip() if r.ocr_text else ""
                        st.text_area("text", value=full_txt, height=220, label_visibility="collapsed", key=f"ocr_{r.id}")
                        if r.region_type == "drawing":
                            try:
                                data = json.loads(r.data_json) if r.data_json else {}
                            except Exception:
                                data = {}
                            linked = data.get("linked_text_regions") or []
                            if linked:
                                st.write("**Linked text blocks (nearby):**")
                                for it in linked:
                                    st.caption(f"region_id={it.get('region_id')} bbox={it.get('bbox')}")
                                    st.write(it.get("text") or "")

        finally:
            db.close()


if __name__ == "__main__":
    main()
