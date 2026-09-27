# CASE10 «ИИ-инспектор ПД → РД → ИД» — инструкция для проверяющих

Сервис сверяет рабочую (РД) и исполнительную (ИД) документацию с проектной (ПД — эталон) по 132 параметрам
матрицы редакции 1.1 (коды M-001…M-132), показывает доказательство каждой находки (файл, SHA-256, страница,
прямоугольник на странице), ведёт решение инспектора и выпускает протокол и JSON по `submission_schema.json`
с полями схемы GOLD 1.1. Всё работает локально: модели лежат в образе, в сеть сервис не ходит.

## 1. Запуск одной командой

**Стенд (интерфейс инспектора + API):**

```bash
docker compose -f docker-compose.case10.yml up -d --build
```

| Что | Адрес |
|---|---|
| Интерфейс инспектора | http://127.0.0.1:3100 |
| API (Swagger) | http://127.0.0.1:8000/docs |
| Внешний контракт API (шлюз) | http://127.0.0.1:8080/openapi.json |
| Состояние | http://127.0.0.1:8000/health |

Учётная запись демо: **`admin` / `Case10-Demo-2026`** (роль администратора). Меняется в `.env`
(`CASE10_ADMIN_LOGIN`, `CASE10_ADMIN_PASSWORD`, шаблон — `deploy/case10/case10.env.example`) до первого старта.
API-ключ администратора (заголовок `X-API-Key`) при пустом `CASE10_ADMIN_API_KEY` генерируется и записывается в
`/data/initial_admin_credentials.txt` внутри тома `case10_data`.

**Пакет документов → JSON, без стенда** (сеть контейнеру не нужна вообще):

```bash
docker build -f api_service/Dockerfile -t case10-api .
docker run --rm --network none -v /path/to/package:/in:ro -v /path/to/out:/out case10-api \
  python -m app.cli run --docs /in --registry /in/registry.csv --out /out/result.json --timings /out/timings.json
```

`--docs` — папка или ZIP; `--registry` — реестр файлов по Перечню ИД 1.1 (см. §3). Код выхода: 0 — результат
записан и валиден по схеме, 2 — результат не прошёл схему, 1 — ошибка входных данных. Проверка готового файла:
`python -m app.cli validate /out/result.json`.

## 2. Что подаётся на вход

- **Пакет** — папка или ZIP с PDF (выгрузки САПР, сканы). Размер не ограничен: лимиты 50 МБ на файл и 200 МБ на
  пакет относятся только к загрузке через интерфейс (ТЗ 9.1, экспертная сессия §9). Большие комплекты подаются
  серверным путём — CLI выше или `POST /api/case10/batch-runs` (пакет лежит в `./packages` стенда).
- В офлайн-стенде PDF и реестр, загруженные через UI, сохраняются локально в `./packages/_uploads/` и проходят
  через тот же `batch_package` без RAG; для таких загрузок сохраняются UI-лимиты 50/200 МБ. Без реестра пакет
  остаётся в статусе `CLARIFICATION_REQUIRED`.
- **Реестр файлов** (Перечень ИД 1.1) — CSV (`;` или `,`), XLSX или JSON с полями `object_id`,
  `file_id` / `file_name` / `sha256`, `doc_stage` (PD/RD/ID), `discipline`, `document_code`, `revision`,
  `approval_status` (DRAFT/APPROVED/FOR_CONSTRUCTION/SUPERSEDED/CANCELLED), `approval_date`, `sheet_page_range`,
  `predecessor_id` / `successor_id`, `signature_status`. Файл сопоставляется со строкой по SHA-256, иначе по имени.
- **Без реестра** пакет принимается со статусом `CLARIFICATION_REQUIRED` (так требует Перечень 1.1): стадия
  берётся из имён папок пакета («Проектная документация», «РД», «ИД», …), идентификатор файла — `F-` + первые 16
  знаков SHA-256. Файлы вне реестра, несовпадение SHA-256, повтор `file_id` тоже дают `CLARIFICATION_REQUIRED`
  с перечнем причин в блоке `package` результата.

## 3. API

Авторизация: `POST /api/auth/login {"login","password"}` → `access_token` (заголовок
`Authorization: Bearer …`) или заголовок `X-API-Key`.

| Метод и путь | Назначение |
|---|---|
| `POST /api/projects` | создать проект (надзорное дело) |
| `POST /api/case10/batch-runs` | серверный пакет + реестр → документы → прогон в очереди (`project_id`, `package`, `registry`, `object_id`) |
| `GET /api/case10/batch-runs/{process_id}` | состояние прогона и отчёт о пакете (реестр, стадии, проблемы) |
| `GET /api/case10/batch-runs/{process_id}/result` | итоговый JSON (тот же формат, что у CLI) |
| `POST /api/upload` | загрузка файла через интерфейс; в offline delivery PDF/реестр локально проходят через `batch_package` (лимиты 50/200 МБ) |
| `POST /api/case10/projects/{id}/processes`, `POST /api/case10/processes/{id}/run`, `GET …/status` | процесс проверки |
| `GET /api/case10/evidence-groups?process_id=…`, `GET /api/case10/evidence-groups/{id}` | находки и доказательства |
| `GET /api/case10/evidence-fragments/{id}/page.png` | страница-первоисточник с выделением |
| `POST /api/case10/evidence-groups/{id}/decisions` | решение инспектора |
| `GET /api/case10/protocols/current`, `…/{id}/submission`, `…/{id}/pdf`, `POST …/{id}/finalize` | протокол, JSON, PDF, финализация |
| `GET /health`, `GET /metrics` | состояние (OCR, GPU), метрики Prometheus |

Пример серверного пути:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"login":"admin","password":"Case10-Demo-2026"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8000/api/projects -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Объект 1"}'
curl -s -X POST localhost:8000/api/case10/batch-runs -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"project_id":1,"package":"obj-1","registry":"obj-1.registry.csv"}'
curl -s localhost:8000/api/case10/batch-runs/<process_id>/result -H "Authorization: Bearer $TOKEN" > result.json
```

## 4. Выходной JSON

Корневые ключи включают `object_id`, `checks`, `quality_issues`, `completeness`, `export_conventions` и `package`. Ключи `quality_issues` и `completeness` присутствуют всегда. Синтетический пример формы ответа находится в `evaluation/phase12/example_submission_synthetic.json`; он не содержит результатов по реальным объектам и не является оценкой качества детекции.

## 5. Модели, веса и лицензии

| Компонент | Где | Размер | Лицензия |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` (эмбеддинги, подтверждение якорей) | в образе | см. `LICENSES_MODELS.md` | Apache-2.0 |
| Tesseract 5.5 + `rus`/`eng` (OCR сканов) | в образе | см. `LICENSES_MODELS.md` | Apache-2.0 |
| Qwen3-4B (необязательный верификатор кандидатов) | **не в образе**, выключен | — | Apache-2.0 |

Облачных API в критическом пути нет. Полный перечень библиотек, размеров и квантования — `LICENSES_MODELS.md`.

## 6. CPU и GPU

По умолчанию всё работает на CPU. GPU (CUDA 12.1, в т. ч. H100) подхватывается автоматически, если контейнер
запущен с `--gpus all` (стенд: `-f docker-compose.case10.gpu.yml`); сервис занимает не больше
`CASE10_GPU_MEMORY_FRACTION` (0.2) памяти GPU. Результат на CPU и GPU одинаков: запас оценок семантического фильтра
до порога в ~6000 раз больше расхождения CPU/CUDA (`evaluation/LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md` §3.1).
Число процессов тэггера — по числу доступных контейнеру ядер (`--cpus`), максимум 24.

## 7. Ожидаемое время

Замер в Docker на реальных объектах, `--cpus 3 --memory 6g --gpus all` (ноутбук, 3 ядра — нижняя граница):

| Объект | Пакет | Первый прогон | Повтор (кэш тэггера) |
|---|---|---|---|
| LOS3A | 151 PDF, 11 тыс. стр., 2,6 ГБ | 7 мин | 2,3 мин |
| DOO25 | 1 308 PDF, 17 тыс. стр., 6,0 ГБ | 14,5 мин | 9,6 мин |

Из этого сравнение 132 параметров и выпуск протокола — ≈ 2 с, вместе с разбором страниц — до 54 с (ТЗ §11: ≤ 2 мин);
основное время первого прогона — разовое сканирование документов, результат кэшируется по SHA-256 файла. Оценка на
сервере с 24 ядрами и локальным диском: первый прогон ≈ 1,5–3,5 мин на объект. Пик памяти — 2,3 ГБ RAM и 0,5 ГБ
VRAM. Подробно — `evaluation/phase12/S6_REPORT.md` §5.

## 8. Воспроизводимость

`result.json` не содержит времени, путей и идентификаторов базы: два прогона одного образа на одном пакете дают
один SHA-256 (`sha256sum result.json`), при любом числе процессов и с тёплым или холодным кэшем. Все лимиты
детерминированные (документы/страницы, не секунды), и их срабатывание видно в протоколе.
