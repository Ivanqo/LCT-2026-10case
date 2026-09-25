from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from email_validator import EmailNotValidError, validate_email
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "public"
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LEADS_PATH = DATA_DIR / "leads.jsonl"

# --- Simple in-memory rate limiter (per IP) ---
RATE_LIMIT_MAX = int(os.getenv("LANDING_RATE_LIMIT_MAX", "5"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("LANDING_RATE_LIMIT_WINDOW_SEC", str(15 * 60)))
_rate_state: Dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only if you terminate TLS/proxy yourself.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_ok(ip: str) -> bool:
    now = time.time()
    bucket = _rate_state.get(ip)
    if not bucket:
        _rate_state[ip] = [now]
        return True

    # purge old
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    bucket[:] = [t for t in bucket if t >= cutoff]

    if len(bucket) >= RATE_LIMIT_MAX:
        return False

    bucket.append(now)
    return True


def _is_phone(s: str) -> bool:
    # permissive: digits, spaces, +, (), -
    return bool(re.fullmatch(r"[\d\s\+\-\(\)]{7,25}", s))


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone(timezone.utc).isoformat(timespec="seconds")


async def _send_telegram(lead: Dict[str, Any]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    text = (
        "🧱 Новый лид (лендинг)\n"
        f"Время (UTC): {lead.get('ts')}\n"
        f"Имя: {lead.get('name')}\n"
        f"Компания: {lead.get('company')}\n"
        f"Роль: {lead.get('role')}\n"
        f"Email: {lead.get('email')}\n"
        f"Телефон: {lead.get('phone') or '-'}\n"
        f"Комментарий: {lead.get('comment') or '-'}\n"
        f"Источник/CTA: {lead.get('cta') or '-'}\n"
        f"IP: {lead.get('ip')}"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, data=payload)
        r.raise_for_status()


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _validate_email(email: str) -> str:
    try:
        v = validate_email(email, check_deliverability=False)
        return v.normalized
    except EmailNotValidError as e:
        raise ValueError(str(e))


app = FastAPI(title="Landing", version="1.0.0")

app.mount("/public", StaticFiles(directory=str(STATIC_DIR)), name="public")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = {
        "request": request,
        "brand": os.getenv("LANDING_BRAND", "DocuRAG"),
        "youtube": os.getenv("LANDING_YOUTUBE", ""),
        "mp4": os.getenv("LANDING_MP4", ""),
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    ctx = {"request": request, "brand": os.getenv("LANDING_BRAND", "DocuRAG")}
    return templates.TemplateResponse(request, "privacy.html", ctx)


@app.get("/success", response_class=HTMLResponse)
async def success(request: Request):
    ctx = {"request": request, "brand": os.getenv("LANDING_BRAND", "DocuRAG")}
    return templates.TemplateResponse(request, "success.html", ctx)


@app.post("/api/lead")
async def submit_lead(
    request: Request,
    name: str = Form(..., max_length=80),
    company: str = Form(..., max_length=120),
    role: str = Form(..., max_length=80),
    email: str = Form(..., max_length=120),
    phone: Optional[str] = Form(None, max_length=40),
    comment: Optional[str] = Form(None, max_length=1000),
    consent: Optional[str] = Form(None),
    cta: Optional[str] = Form(None, max_length=40),
    website: Optional[str] = Form(None, max_length=80),  # honeypot
):
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")

    # Honeypot: silently accept
    if website and website.strip():
        return JSONResponse({"ok": True})

    if not _rate_limit_ok(ip):
        # Return 200 but don't process further to avoid giving spammers a signal.
        return JSONResponse({"ok": True})

    # Validate consent
    if consent not in {"on", "true", "1", "yes"}:
        raise HTTPException(status_code=400, detail="Необходимо согласие на обработку данных")

    name = name.strip()
    company = company.strip()
    role = role.strip()
    email = email.strip()
    phone = (phone or "").strip() or None
    comment = (comment or "").strip() or None
    cta = (cta or "").strip() or None

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Укажите имя")
    if len(company) < 2:
        raise HTTPException(status_code=400, detail="Укажите компанию")
    if len(role) < 2:
        raise HTTPException(status_code=400, detail="Укажите роль")

    try:
        email_norm = _validate_email(email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректный email: {e}")

    if phone and not _is_phone(phone):
        raise HTTPException(status_code=400, detail="Некорректный телефон")

    lead = {
        "ts": _iso_now(),
        "ip": ip,
        "ua": ua,
        "name": name,
        "company": company,
        "role": role,
        "email": email_norm,
        "phone": phone,
        "comment": comment,
        "cta": cta,
    }

    _append_jsonl(LEADS_PATH, lead)

    try:
        await _send_telegram(lead)
    except Exception:
        # Don't break the form if telegram fails.
        pass

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/success", status_code=303)


@app.get("/health")
async def health():
    return {"ok": True}
