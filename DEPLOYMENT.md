# Развёртывание стенда CASE10

Стенд — это офлайн-сервис проверки ПД/РД/ИД и его интерфейс инспектора. Внешние сервисы не нужны: модели лежат в
образе, во время работы нет обращений в интернет (подробно — `README_GRADER.md`, лицензии — `LICENSES_MODELS.md`).

| Сервис | Что делает | Порт (по умолчанию только 127.0.0.1) |
|---|---|---|
| `api` | FastAPI: загрузка, процессы, доказательства, решения, протокол, JSON; серверный пакетный путь | 8000 (`/docs`) |
| `case10_worker` | выполняет прогоны из очереди (тот же образ) | — |
| `rabbitmq` | очередь заданий между `api` и воркером | не публикуется |
| `node_gateway` | внешний контракт API (`/openapi.json`), прокси к `api` | 8080 |
| `react_frontend` | рабочее место инспектора | 3100 |
| `nginx` (+ `certbot`) | только для публичного стенда: один домен, `/` → интерфейс, `/api/` → шлюз | 80 / 443 |

Файлы: `docker-compose.case10.yml` (стенд), `docker-compose.case10.gpu.yml` (GPU), `deploy/case10/` (nginx, TLS,
bootstrap, шаблон `.env`). Старый `docker-compose.yml` — среда разработки (RAG, IFC, Postgres, чат), в поставку не
входит.

## 1. Локально или на сервере, одна команда

Требования: Docker 24+ с Compose v2; 4+ ядра, 8+ ГБ RAM (16 ГБ для объектов на тысячи страниц), 20 ГБ диска под
образ. GPU не обязателен.

```bash
docker compose -f docker-compose.case10.yml up -d --build
```

Первая сборка скачивает зависимости (≈ 3–4 ГБ, в основном PyTorch с CUDA-библиотеками) и модель эмбеддингов;
дальше стенду интернет не нужен. Проверка:

```bash
docker compose -f docker-compose.case10.yml ps
curl -s http://127.0.0.1:8000/health
```

Интерфейс: http://127.0.0.1:3100, вход `admin` / `Case10-Demo-2026` (меняется переменными `CASE10_ADMIN_LOGIN`,
`CASE10_ADMIN_PASSWORD` в `.env`, см. `deploy/case10/case10.env.example`). Пароль задаётся при **первом** старте,
когда создаётся база; чтобы сменить его, остановите стенд и удалите том `case10_data` (`down -v`).

С GPU (нужен NVIDIA Container Toolkit):

```bash
docker compose -f docker-compose.case10.yml -f docker-compose.case10.gpu.yml up -d --build
```

## 2. Публичный стенд на Ubuntu

```bash
git clone <repo> case10 && cd case10
APP_DOMAIN=case10.example.ru LETSENCRYPT_EMAIL=ops@example.ru bash deploy/case10/bootstrap.sh
```

Скрипт ставит Docker (если его нет), создаёт `.env` из `deploy/case10/case10.env.example` со случайными паролем
администратора и API-ключом, собирает и поднимает стенд за nginx, получает сертификат Let's Encrypt и
переключает nginx на HTTPS. Без `APP_DOMAIN` стенд работает по HTTP на порту 80. `GPU=1` добавляет GPU-оверлей.
Учётные данные печатаются в конце и лежат в `.env`. В файрволе достаточно открыть 22, 80, 443.

## 3. Большие пакеты: серверный путь

Интерфейс принимает файлы до 50 МБ и пакеты до 200 МБ (ТЗ 9.1). Реальные комплекты — гигабайты, они загружаются
на сервер и обрабатываются без этих лимитов:

В офлайн-поставке PDF и реестр, загруженные через UI, сохраняются в `./packages/_uploads/<project>/<process>/`
и регистрируются локальным `batch_package`; запросы к RAG для PDF не выполняются. UI-путь сохраняет лимиты 50/200 МБ.
Для крупных комплектов используйте серверный `batch-runs` ниже.

1. Положить папку или ZIP пакета (и реестр файлов по Перечню ИД 1.1) в `./packages` на хосте
   (`CASE10_PACKAGES_DIR`), например `packages/obj-17/…` и `packages/obj-17.registry.csv`.
2. `POST /api/case10/batch-runs` с `{"project_id": 1, "package": "obj-17", "registry": "obj-17.registry.csv"}`
   (роль администратора или руководителя). Пакет импортируется, процесс ставится в очередь воркеру и виден в
   интерфейсе; результат — `GET /api/case10/batch-runs/{process_id}/result`.

Или без стенда, одной командой в контейнере: `python -m app.cli run` (см. `README_GRADER.md`, раздел 2).

## 4. Эксплуатация

```bash
docker compose -f docker-compose.case10.yml logs -f api case10_worker
docker compose -f docker-compose.case10.yml restart api case10_worker
docker compose -f docker-compose.case10.yml down          # данные (том case10_data) сохраняются
docker compose -f docker-compose.case10.yml down -v       # полностью очистить базу и кэш
```

Данные: SQLite-база, кэш тэггера и распакованные ZIP — в томе `case10_data` (`/data`), журналы — `case10_logs`.
Обновление кода: `git pull && docker compose -f docker-compose.case10.yml up -d --build`. Кэш тэггера при смене
версий tesseract/PyMuPDF/torch в образе нужно очистить (`docker volume rm case10_case10_data` или
`CASE10_LIVE_TAGGER_CACHE_DIR`), см. `evaluation/LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md` §3.

## 5. Комплект сдачи

`python deploy/make_delivery.py bundle` собирает из закоммиченных файлов только код, Dockerfile, compose, эту
инструкцию, `README_GRADER.md` и `LICENSES_MODELS.md` (без внутренних отчётов, данных организатора и служебных
файлов) и проверяет результат; `python deploy/make_delivery.py check-image case10-api:latest` проверяет содержимое
образа.

---

Прежняя схема `docaibuild.ru` (лендинг, RAG-приложение: `docker-compose.prod.yml`, `deploy/bootstrap-ubuntu.sh`,
`deploy/nginx/templates/`) к CASE10 не относится и в поставку не входит.
