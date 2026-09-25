# /landing — микросервис-лендинг для B2B пилотов (строительная документация + AI/RAG)

Одностраничный «продающий» лендинг под пилоты 1–2 месяца.

- Backend: **FastAPI** (Python)
- Front: **чистые HTML/CSS/JS** (без сборки)
- Лиды: лог в `./data/leads.jsonl` + опционально отправка в **Telegram**
- Антиспам: **honeypot** + простой **rate limit** in-memory

## Структура

```
/landing
  app/                # FastAPI
  templates/          # Jinja2 шаблоны
  public/             # CSS/JS/скриншоты-заглушки
  data/               # leads.jsonl (volume в Docker)
  Dockerfile
  docker-compose.landing.yml
```

## Запуск локально

### Вариант A: через Python (рекомендуется)

```bash
cd landing
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 3001
```

Открыть: `http://localhost:3001`

### Вариант B: через npm (если вам удобнее одна команда)

> В проекте **нет** фронтовой сборки и зависимостей. `npm` используется только как «таск-раннер».

```bash
cd landing
npm install
npm run dev
```

## Запуск в Docker

```bash
cd landing
docker compose -f docker-compose.landing.yml up --build
```

Открыть: `http://localhost:3001`

## Куда пишутся лиды

- `landing/data/leads.jsonl` — каждая заявка отдельной строкой JSON
- При запуске в Docker папка `./data` монтируется в контейнер (`/app/data`)

## Telegram интеграция

Для отправки лидов в Telegram задайте переменные окружения:

- `TELEGRAM_BOT_TOKEN` — токен бота
- `TELEGRAM_CHAT_ID` — id пользователя/чата, куда отправлять

Пример (Linux/macOS):

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

Если переменные не заданы — лиды просто логируются в файл.

## Как заменить плейсхолдер видео

В Hero-блоке поддерживаются 2 варианта:

1) **YouTube** — задайте `LANDING_YOUTUBE` (только ID ролика):

```bash
export LANDING_YOUTUBE="dQw4w9WgXcQ"
```

2) **Локальный mp4** — положите файл, например, в `public/video/demo.mp4` и задайте:

```bash
export LANDING_MP4="/public/video/demo.mp4"
```

## Как заменить скриншоты

Заглушки лежат в:

- `public/screens/screen1.png ... screen5.png`

Замените файлы на реальные скриншоты **с теми же именами** — верстку менять не нужно.

## Антиспам

- Honeypot поле `website` скрыто в форме.
- Rate limit (по IP) задаётся env:
  - `LANDING_RATE_LIMIT_MAX` (по умолчанию 5)
  - `LANDING_RATE_LIMIT_WINDOW_SEC` (по умолчанию 900 секунд)

## Разработка / правки текста

Тексты лежат в `templates/index.html`. Дизайн — в `public/styles.css`.
