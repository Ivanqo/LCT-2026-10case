# Refactored: API + RAG + IFC (Heavy/Indexed)

Проект разделён на несколько независимых сервисов. Для CASE10 внешний контракт ТЗ теперь публикуется через минимальный **Node.js gateway** с OpenAPI, а Python API остается движком проверки, extraction и evidence.

## Сервисы

1) **api** (FastAPI, порт `8000`)  
   - Python engine для `/api/*`: проекты, загрузки, CASE10 checks, extraction, evidence, protocol.  
   - Хранит: проекты и историю чата (SQLite).  
   - Делегирует:
     - загрузку PDF/DOCX + ответы по документации → `rag`
     - импорт IFC (heavy/indexed) → `ifc`

2) **node_gateway** (Node.js, порт `8080`)
   - Внешний CASE10 API contract: `/health`, `/openapi.json`, ключевые `/api/case10/*`, `/api/entities*`, `/api/issues/*`.
   - Проксирует запросы в Python API без изменения алгоритмов.
   - Локально: `http://127.0.0.1:8080/openapi.json`.

3) **rag** (FastAPI, порт `8001`)  
   - Инжест PDF/DOCX, чанкинг, эмбеддинги, Chroma persistence  
   - Поиск контекста и вызов LLM (через `shared/llm.py` + `shared/llm_adapter.py`)

4) **ifc** (FastAPI, порт `8002`) + **ifc_worker** (Celery)  
   - Heavy/Indexed подход: `import → index → быстрые запросы из БД`
   - Хранилище: PostgreSQL (`postgres`) + очередь `redis`

## Как запускать (docker-compose)

```bash
docker compose up --build
```

### Примечания для Windows (локальная разработка) и Ubuntu (prod)

- Все сервисы собираются на базе **Python 3.11**, чтобы на Ubuntu не уходить в компиляцию C-зависимостей (например, `ifcopenshell`).
- Для `rag` PyTorch ставится **CPU-only** wheels (быстрее и меньше образ).
- В `docker-compose.yml` добавлен `extra_hosts: host.docker.internal:host-gateway`, чтобы **на Ubuntu** работало обращение к сервисам на хосте по имени `host.docker.internal`.
  - На Windows это обычно работает “из коробки”.
  - В проде на Ubuntu лучше явно задать `QWEN_PROXY_BASE_URL` на адрес прокси/шлюза, который доступен из контейнеров.

Переменные окружения для LLM (Qwen proxy) можно пробросить:
- `QWEN_PROXY_BASE_URL` (например `http://host.docker.internal:3264/api`)
- `QWEN_MODEL`
- `QWEN_TIMEOUT`

## Важное про LLM

Файл `shared/llm.py` **не изменён** (скопирован как есть).  
Для удобного использования из сервисов добавлен адаптер `shared/llm_adapter.py` (оборачивает `QwenProxyClient`).

## Эндпоинты фронта (API сервис, НЕ менялись по путям)

- `POST /api/projects`
- `GET /api/projects`
- `POST /api/upload`  (pdf|docx → rag, ifc → ifc)
- `POST /api/chat`    (делегирует вопрос в rag)
- `GET /api/history`
- `GET /api/documents` (объединяет документы rag + модели ifc)

## CASE10 Node gateway contract

- Gateway порт: `8080` (`GATEWAY_HOST_PORT`).
- Health: `GET /health`.
- OpenAPI: `GET /openapi.json`.
- CASE10 endpoints проксируются в Python API: matrix, official dataset import, processes, evidence groups, evidence page PNG, protocol JSON/PDF/finalize, submission, evaluation predictions.
- Python API (`8000`) остается внутренним engine для проверки, extraction, evidence и совместимости существующего frontend.

## IFC heavy/indexed минимальные эндпоинты (IFC сервис)

- `POST /models/import` → `{ model_id }` (очередь: парсинг/индексация в Celery)
- `GET /models/{model_id}/status`
- `GET /models?project_id=...`
- `POST /query/heavy` body: `{ model_id, query_spec }`
- `POST /models/{model_id}/compute` body: `{ metrics: [...] }`

`query_spec` сейчас минимальный (фильтр по `ifc_type`, группировка `storey_guid/ifc_type`, агрегации area/volume если есть в `metrics`).

## Логи и пути

- У каждого сервиса отдельный лог-файл (`/logs/<service>.log`) + вывод в консоль.
- Все пути задаются через env и нормализуются через `Path(...).resolve()`.

# API + RAG + IFC (Heavy/Indexed) — Полная документация проекта

## 📌 Общее описание

Система представляет собой **микросервисную платформу обработки инженерной документации и IFC-моделей** с поддержкой:

* загрузки PDF / DOCX / IFC
* интеллектуального поиска по документации (RAG)
* аналитических запросов к BIM-моделям (IFC heavy indexed)
* LLM-ответов с использованием контекста
* истории чатов и проектов
* асинхронной индексации моделей

Архитектура построена по принципу **разделения ответственности**:

| Сервис | Роль                       |
| ------ | -------------------------- |
| API    | Точка входа для клиента    |
| RAG    | Работа с документами + LLM |
| IFC    | Индексация и аналитика BIM |
| Worker | Фоновая обработка IFC      |
| Shared | Общие компоненты           |

---

## 🧩 Архитектура системы

```
Frontend
   |
   v
API Service (FastAPI)
   |           |
   |           |
   v           v
RAG Service   IFC Service
   |           |
   v           v
Vector DB     PostgreSQL
LLM           Redis Queue
```

---

## 🧠 Основные принципы архитектуры

### 1. API = orchestration layer

API ничего не вычисляет тяжёлого — он только:

* принимает запросы
* сохраняет метаданные
* маршрутизирует в нужный сервис
* объединяет ответы

### 2. RAG = document intelligence

Отвечает за:

* ingestion документов
* OCR
* layout detection
* chunking
* embeddings
* vector search
* генерацию ответа LLM

### 3. IFC = structured analytics engine

Работает по модели:

```
IFC → parse → normalize → index → SQL analytics
```

Запросы выполняются **не по файлу**, а по индексированной БД.

### 4. Worker = heavy processing

Все тяжёлые операции выполняются асинхронно:

* разбор IFC
* вычисление геометрии
* построение индексов
* CASE10 inspection pipeline через RabbitMQ:
  `Node/API start -> case10.process.jobs -> case10_worker -> Python pipeline -> DB/status -> polling API`

CASE10 start endpoints быстро возвращают `process_id` и `job.job_id`; готовность читается через
`GET /api/case10/processes/{process_id}/status`. Worker хранит job-состояние в SQLite,
ограничивает retry и пишет failed jobs в error/DLQ очередь RabbitMQ.

---

## 📂 Структура проекта

```
api_service/
    app/
        api/                # REST endpoints
        clients/            # HTTP клиенты других сервисов
        db/                 # SQLite модели
        main.py             # entrypoint API
        worker_case10.py    # RabbitMQ consumer для CASE10 pipeline

rag_service/
    app/
        ingestion.py        # разбор документов
        vectorstore.py      # ChromaDB
        ocr_utils.py
        layout_ingestion.py
        titleblock_detector.py
        yolo_layout.py
        main.py

ifc_service/
    app/
        tasks.py            # Celery tasks
        celery_app.py
        db/
        main.py

shared/
    llm.py                  # Qwen proxy client
    llm_adapter.py          # унификация вызова LLM
```

---

## 🔁 Полный жизненный цикл пользователя

### 1. Создание проекта

```
POST /api/projects
```

API:

* создаёт запись в SQLite
* возвращает project_id

---

### 2. Загрузка файла

```
POST /api/upload
```

API определяет тип:

| Тип        | Куда отправляется |
| ---------- | ----------------- |
| PDF / DOCX | RAG               |
| IFC        | IFC service       |

---

## 📄 Обработка документов (RAG pipeline)

### ingestion pipeline

```
document
   ↓
layout detection (YOLO / CV)
   ↓
OCR
   ↓
semantic chunking
   ↓
embedding
   ↓
vector DB
```

### Что извлекается

* текст
* таблицы
* чертежи
* title blocks
* метаданные страниц

---

## 💬 Обработка вопроса пользователя

```
POST /api/chat
```

Поток:

```
API
  ↓
RAG search
  ↓
retrieve top chunks
  ↓
LLM prompt assembly
  ↓
LLM generation
  ↓
response
```

LLM получает:

* пользовательский вопрос
* релевантные chunks
* метаданные документов

---

## 🏗 Обработка IFC моделей (Heavy Indexed)

### импорт модели

```
POST /models/import
```

Происходит:

```
enqueue celery job
   ↓
worker parses IFC
   ↓
extract elements
   ↓
normalize geometry
   ↓
store in PostgreSQL
```

---

### состояние обработки

```
GET /models/{model_id}/status
```

---

### аналитический запрос

```
POST /query/heavy
```

Примеры операций:

* фильтр по типу элемента
* группировка по этажам
* расчёт площади
* расчёт объёма

Запрос выполняется SQL-агрегациями.

---

## 🗄 Хранилища данных

| Хранилище  | Назначение        |
| ---------- | ----------------- |
| SQLite     | проекты и история |
| Chroma     | embeddings        |
| PostgreSQL | IFC индексы       |
| Redis      | очередь задач     |

---

## 🤖 LLM слой

LLM не встроен в сервисы напрямую.

Используется адаптер:

```
shared/llm_adapter.py
```

Он:

* оборачивает Qwen proxy
* стандартизирует API
* управляет timeout

---

## 🔗 Межсервисное взаимодействие

API использует HTTP clients:

```
rag_client.py
ifc_client.py
```

Каждый вызов:

* синхронный (REST)
* с таймаутами
* с логированием

---

## 📊 Логирование

Каждый сервис:

```
/logs/<service>.log
```

Пишет:

* входящие запросы
* ошибки
* время выполнения
* состояние pipeline

---

## ⚙️ Переменные окружения

### LLM

```
QWEN_PROXY_BASE_URL
QWEN_MODEL
QWEN_TIMEOUT
```

### инфраструктура

```
POSTGRES_URL
REDIS_URL
VECTORSTORE_PATH
UPLOAD_PATH
```

---

## 🚀 Запуск системы

```
docker compose up --build
```

Запускаются:

* api
* rag
* ifc
* worker
* postgres
* redis

---

## 🧪 Потоки выполнения (sequence)

### Chat flow

```
user
  ↓
API
  ↓
RAG retrieve
  ↓
LLM
  ↓
API
  ↓
user
```

---

### IFC import flow

```
upload
  ↓
API
  ↓
IFC service
  ↓
Celery queue
  ↓
Worker parse
  ↓
PostgreSQL index
```

---

## 🧱 Слои ответственности

| Слой           | Что делает        |
| -------------- | ----------------- |
| Transport      | FastAPI endpoints |
| Orchestration  | API service       |
| AI             | RAG               |
| Data analytics | IFC               |
| Async compute  | Celery            |
| Storage        | DB                |

---

## 📌 Почему heavy indexed IFC

Преимущества:

* быстрые запросы
* масштабируемость
* сложная аналитика
* повторное использование индекса

---

## 🧩 Расширяемость

Можно легко добавить:

* новые типы документов
* новые метрики IFC
* другой LLM
* streaming ответы
* semantic caching

---

## 🛠 Важные точки входа кода

| Файл                           | Назначение        |
| ------------------------------ | ----------------- |
| api_service/app/main.py        | запуск API        |
| rag_service/app/main.py        | запуск RAG        |
| ifc_service/app/main.py        | запуск IFC        |
| ifc_service/app/tasks.py       | heavy parsing     |
| rag_service/app/ingestion.py   | document pipeline |
| rag_service/app/vectorstore.py | embeddings        |

---

## 💡 Как думать о системе

Это:

```
knowledge platform
   +
BIM analytics engine
   +
LLM reasoning layer
```



## Аккаунтинг + организации + админ панель

### Как устроено
- Доступ к API теперь по заголовку `X-API-Key`.
- Каждый пользователь привязан к `organization_id`.
- Проекты принадлежат организации, данные из другой организации недоступны.

### Bootstrap админа
При первом старте (если таблица users пустая) сервис создаёт:
- организацию `Default` (или `DEFAULT_ADMIN_ORG`)
- админ пользователя `admin@local` (или `DEFAULT_ADMIN_EMAIL`)
- API key:
  - берётся из `DEFAULT_ADMIN_API_KEY`, если задан
  - иначе генерируется и выводится в логи контейнера `api`

Пример (docker-compose):
```bash
DEFAULT_ADMIN_EMAIL=admin@company.com DEFAULT_ADMIN_API_KEY=CHANGE_ME docker compose up --build
```

### Админ панель (frontend)
Откройте `frontend/index.html`, вставьте API key админа в блок **Доступ** → появится вкладка **Админ**.
Там можно:
- создать организацию
- создать пользователя (email + org) и получить его `api_key`


## Что добавлено для совместной работы IFC + документации

Теперь `/api/chat` работает как **единая точка инженерного запроса**:

1. API запрашивает релевантные фрагменты из документации через `rag`.
2. API запрашивает релевантные сущности, свойства, количества и агрегаты из IFC через `ifc`.
3. Оба контекста объединяются в один prompt.
4. Итоговый инженерный ответ и заключение формируются через LLM.

### Новый IFC endpoint

- `POST /query/context`
  - принимает: `{ project_id, organization_id, question, max_models, limit }`
  - возвращает нормализованный текстовый контекст по элементам IFC, который пригоден для LLM/RAG orchestration

### Что это даёт

- можно одним вопросом получать ответ одновременно по PDF/DOCX и IFC;
- IFC больше не изолирован от чата;
- модель получает уже нормализованный BIM-контекст: тип элемента, GUID, этаж, свойства, количества, area/volume и агрегированные сводки;
- это упрощает связку с документацией и построение выводов/проверок.
