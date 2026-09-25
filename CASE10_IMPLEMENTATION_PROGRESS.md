# CASE10 Implementation Progress

## 1. Текущее состояние

Дата / этап: 14.09.2026, P0-A/P0-B/P0-C/P0-D/P0-E плюс первый bridge для RAG SourceFragment, IFC observations и локальный frontend smoke.

Проект сейчас: локальная микросервисная система `api_service` + `rag_service` + `ifc_service` + статический `frontend`. Существующие upload pipeline, RAG ingestion, OCR/layout hooks, page/source viewer, IFC service, auth, Docker Compose и фоновые задачи сохранены. RAG больше не позиционируется как главный продукт в новом UI: он оставлен как `AI Assistant` для контекста, поиска доказательств и объяснений.

Новый центральный домен заложен в API:

`SOURCE -> OBSERVATION -> CANONICAL ENTITY -> OBJECT PORTRAIT -> CHANGE -> RISK -> EVIDENCE -> INSPECTOR DECISION`

Корень `D:\Proga\LCT-hack-2026` не является общим git-репозиторием; `.git` найден только внутри `FreeQwenApi`.

## 2. Выполнено

### ✅ Safety snapshot проекта

Что реализовано: создана отдельная локальная копия исходников перед изменениями. Snapshot исключает тяжелые/runtime каталоги вроде `venv`, `logs`, `data`, `node_modules`, `dist`, `build`, `__pycache__`, `.git`, `session`.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\_case10_project_snapshot_20260914`

API:
- изменений нет

DB:
- изменений нет

Frontend:
- изменений нет

### ✅ Первичная инвентаризация

Что реализовано: прочитан `C:\Users\Ivan\Downloads\CASE10_TECHNICAL_STRATEGY_V2.md`, проверены `README.md`, `docker-compose.yml`, структура сервисов, текущие DB-модели, auth dependencies и frontend shell.

Измененные файлы:
- нет

Добавленные файлы:
- нет

API:
- текущий API хранит проекты, пользователей, историю, upload jobs и делегирует RAG/IFC.

DB:
- API использует SQLAlchemy + SQLite по умолчанию; таблицы создаются через `Base.metadata.create_all`, есть ручные additive SQLite migration hooks.

Frontend:
- до изменений UI был сфокусирован на чате, документах, истории и администрировании.

### ✅ Базовая CASE10 domain model

Что реализовано: добавлены доменные таблицы под V2-подход `Observation -> Entity Resolution -> CanonicalEntity`. Модели являются additive и не ломают существующие таблицы.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\db\models.py`
- `D:\Proga\LCT-hack-2026\api_service\app\db\session.py`

Добавленные файлы:
- нет

API:
- пока на этом шаге endpoints не добавлялись.

DB:
- `document_versions`
- `source_fragments`
- `canonical_entities`
- `entity_aliases`
- `entity_observations`
- `attribute_observations`
- `entity_relations`
- `change_events`
- `change_issues`
- `review_decisions`
- `schema_migrations` marker with version `20260914_case10_domain`

Frontend:
- изменений нет

### ✅ DocumentVersion подключен к upload pipeline

Что реализовано: после успешной обработки upload job API создает `DocumentVersion` для PDF/DOCX/IFC с `source_type`, внешним `source_document_id`, filename, stage guess, revision guess и `content_hash`.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\api\routes_upload.py`

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\document_versions.py`

API:
- старый контракт `POST /api/upload` не изменен.

DB:
- начинает наполняться `document_versions`.

Frontend:
- изменений нет

### ✅ Entity Resolution layer

Что реализовано: отдельный доменный resolver `ObservationDraft -> EntityResolutionCandidate[]`.

Поддержано сейчас:
- exact alias match;
- normalized alias match;
- fuzzy name similarity;
- location similarity;
- attribute similarity;
- same entity type.

Важно: результат содержит explainable confidence: список причин с кодом, label, contribution и details.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\normalization.py`
- `D:\Proga\LCT-hack-2026\api_service\app\domain\entity_resolution.py`

API:
- напрямую resolver наружу пока не вынесен.

DB:
- resolver использует `canonical_entities`, `entity_aliases`, `entity_observations`, `attribute_observations`.

Frontend:
- изменений нет

### ✅ Object Portrait / PortraitBuilder

Что реализовано: `ObjectPortrait` сделан вычисляемой проекцией, а не отдельной таблицей. `PortraitBuilder` агрегирует:
- identity;
- aliases;
- locations;
- observations;
- attributes;
- relations;
- stages;
- versions;
- timeline;
- evidence;
- changes;
- issues;
- risk.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\portrait_builder.py`

API:
- портрет доступен через `GET /api/entities/{entity_id}/portrait`.

DB:
- читает CASE10 domain tables, не создает отдельную таблицу `object_portraits`.

Frontend:
- Object Portrait panel подключен в новом shell.

### ✅ Change Engine

Что реализовано: Change Engine работает поверх истории `AttributeObservation` одной `CanonicalEntity`, а не поверх пары документов.

Поддержаны типы:
- `ADDED`
- `REMOVED` как зарезервированный тип;
- `VALUE_CHANGED`
- `VALUE_MISSING`
- `TYPE_CHANGED`
- `CONFLICT`

Сейчас engine реально создает `ChangeEvent` и `ChangeIssue`, а не только возвращает transient diff. Risk model намеренно простая и временная: fire resistance downgrade -> `CRITICAL`, thickness/material changes -> `WARNING`, conflicts/missing values -> `NEEDS_REVIEW`.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\change_engine.py`

API:
- `POST /api/entities/{entity_id}/refresh-changes`

DB:
- наполняет `change_events`, `change_issues`.

Frontend:
- Changes screen и Protocol screen читают результаты engine.

### ✅ Synthetic dataset

Что реализовано: добавлен небольшой синтетический dataset, явно не конкурсные данные.

Dataset:
- П: `Wall A`, thickness `250 mm`, fire resistance `EI60`, material `D500`
- Р: `Wall A1`, thickness `200 mm`, fire resistance `EI60`, material `D500`
- И: `Wall ST-01`, thickness `180 mm`, fire resistance `EI30`, material `Brick`

Dataset создает:
- `DocumentVersion` для П/Р/И;
- `SourceFragment` с page и bbox-заготовкой;
- `CanonicalEntity` wall;
- aliases;
- observations;
- attribute observations;
- relation `LOCATED_IN` к synthetic floor;
- change events/issues через Change Engine.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\synthetic_dataset.py`

API:
- `POST /api/case10/projects/{project_id}/synthetic-dataset`

DB:
- наполняет все ключевые P0 domain tables.

Frontend:
- кнопка `Создать synthetic dataset` на экране `Проекты`.

### ✅ CASE10 API layer

Что реализовано: добавлен отдельный router для новой предметной модели.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\main.py`

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\api\routes_case10.py`

API:
- `POST /api/case10/projects/{project_id}/synthetic-dataset`
- `GET /api/case10/overview?project_id=...`
- `GET /api/case10/documents?project_id=...`
- `GET /api/entities?project_id=...`
- `GET /api/entities/{entity_id}/portrait`
- `GET /api/case10/changes?project_id=...`
- `GET /api/case10/protocol?project_id=...`
- `POST /api/issues/{issue_id}/decisions`
- `POST /api/entities/{entity_id}/refresh-changes`

DB:
- endpoints читают и пишут CASE10 domain tables.

Frontend:
- новые screens используют эти endpoints.

### ✅ Unit tests для domain layer

Что реализовано: добавлены unit tests на alias normalization, synthetic integration fixture, Entity Resolution, PortraitBuilder, Change Engine.

Измененные файлы:
- нет

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\tests\test_case10_domain.py`

API:
- direct API tests пока не добавлены.

DB:
- tests используют in-memory SQLite.

Frontend:
- изменений нет

### ✅ Frontend shell под новую модель продукта

Что реализовано: UI перестал быть чат-центричным. Первая навигация теперь:
- `Проекты`
- `Документы`
- `Объекты`
- `Изменения`
- `Протокол`
- `AI Assistant`
- `Администрирование` для admin

Добавлены screens:
- Project Overview с П/Р/И, объектами, изменениями и pipeline;
- Documents с группировкой DocumentVersion по стадиям;
- Object Registry;
- Object Portrait;
- Changes table;
- Protocol workflow;
- Evidence Viewer shell `source A | source B`;
- AI Assistant как вторичная функция.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\frontend\index.html`
- `D:\Proga\LCT-hack-2026\frontend\app.js`
- `D:\Proga\LCT-hack-2026\frontend\styles.css`

Добавленные файлы:
- нет

API:
- frontend использует новые `/api/case10/*`, `/api/entities*`, `/api/issues/*`.

DB:
- изменений нет

Frontend:
- shell функциональный; локально проверен через browser smoke на synthetic dataset.

### ✅ Frontend local smoke QA и исправления Documents/Changes

Что реализовано: поднят локальный API на `127.0.0.1:8000` и статический frontend на `127.0.0.1:3000`; через браузер проверены login, выбор проекта, synthetic CASE10 screens и отсутствие console errors. Найден и исправлен frontend дефект: JS рендерил `DocumentVersion` в `#docStageGroups`, но контейнера не было в HTML, поэтому экран `Документы` не показывал П/Р/И synthetic версии. Также исправлено отображение значений с unit, чтобы `250 mm` не превращалось в `250 mm mm`.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\frontend\index.html`
- `D:\Proga\LCT-hack-2026\frontend\app.js`

Добавленные файлы:
- нет

API:
- новых endpoints нет.

DB:
- изменений нет.

Frontend:
- добавлен блок `Версии документов по стадиям` с `id="docStageGroups"` на экран `Документы`;
- `formatValueWithUnit()` используется для Object Portrait и Changes table;
- проверены screens `Проекты`, `Документы`, `Объекты`, `Изменения`, `Протокол`, `AI Assistant` на локальном synthetic dataset;
- console errors: 0;
- на desktop viewport `1365x768` нет горизонтального переполнения body (`bodyScrollWidth == docClientWidth`);
- полный Docker/browser smoke все еще не запускался.

### ✅ API integration tests для CASE10 endpoints

Что реализовано: добавлены FastAPI `TestClient` integration tests для новых CASE10 routes. Тесты не поднимают внешний RAG/IFC stack, но проверяют реальные HTTP routes API layer, dependency overrides и запись в in-memory SQLite.

Покрыто:
- synthetic dataset endpoint;
- overview;
- documents grouping;
- object registry;
- object portrait;
- changes;
- protocol;
- review decision workflow;
- RAG source fragment sync endpoint с fake RAG client;
- IFC observation sync endpoint с fake IFC client.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\tests\test_case10_api.py`

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\tests\test_case10_api.py`

API:
- проверены существующие CASE10 endpoints через HTTP route layer.

DB:
- tests используют in-memory SQLite через `StaticPool`, чтобы TestClient thread видел одну и ту же БД.

Frontend:
- изменений нет

### ✅ RAG -> SourceFragment bridge

Что реализовано: добавлен read-only export endpoint в RAG service и sync endpoint в API service. Это позволяет превращать существующие RAG pages/chunks/regions в `SourceFragment` для CASE10 provenance, не переписывая ingestion.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\db\models.py`
- `D:\Proga\LCT-hack-2026\api_service\app\db\session.py`
- `D:\Proga\LCT-hack-2026\api_service\app\clients\rag_client.py`
- `D:\Proga\LCT-hack-2026\api_service\app\api\routes_case10.py`
- `D:\Proga\LCT-hack-2026\frontend\app.js`
- `D:\Proga\LCT-hack-2026\rag_service\app\schemas.py`
- `D:\Proga\LCT-hack-2026\rag_service\app\main.py`

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\source_fragment_sync.py`

API:
- RAG service: `GET /layout/source_fragments?document_id=...&organization_id=...`
- API service: `POST /api/case10/document-versions/{document_version_id}/sync-source-fragments`

DB:
- в `source_fragments` добавлены поля:
  - `source_system`
  - `external_id`
  - `metadata_json`
- добавлен unique constraint модели `uq_source_fragment_external_ref` для fresh DB.
- SQLite migration hook добавляет новые поля existing DB.

Frontend:
- в Documents grouping добавлена кнопка `Синхронизировать источники` для RAG `DocumentVersion`.

Ограничения:
- sync пока ручной, не автоматический после upload/reindex;
- endpoint экспортирует fragments, но не извлекает строительные entities из RAG текста.

### ✅ IFC -> Observation bridge

Что реализовано: добавлен read-only IFC export endpoint и CASE10 sync endpoint. IFC indexed elements теперь могут становиться `SourceFragment`, `EntityObservation` и `AttributeObservation`; entity resolution пытается связать их с существующими `CanonicalEntity`, иначе создает новую canonical entity.

Измененные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\clients\ifc_client.py`
- `D:\Proga\LCT-hack-2026\api_service\app\api\routes_case10.py`
- `D:\Proga\LCT-hack-2026\frontend\app.js`
- `D:\Proga\LCT-hack-2026\ifc_service\app\schemas.py`
- `D:\Proga\LCT-hack-2026\ifc_service\app\main.py`

Добавленные файлы:
- `D:\Proga\LCT-hack-2026\api_service\app\domain\ifc_observation_sync.py`

API:
- IFC service: `GET /models/{model_id}/observations?organization_id=...&limit=...`
- API service: `POST /api/case10/document-versions/{document_version_id}/sync-ifc-observations?limit=...`

DB:
- пишет `source_fragments` с `source_system='ifc'`;
- пишет/обновляет `entity_observations`;
- пишет `attribute_observations` для IFC properties, quantities и metrics;
- создает `entity_aliases` для IFC GUID, IFC name и IFC type;
- может создавать новые `canonical_entities` при отсутствии resolver match.

Frontend:
- в Documents grouping добавлена кнопка `Синхронизировать IFC` для IFC `DocumentVersion`.

Ограничения:
- sync пока ручной;
- не делает сложную geometry validation;
- не строит `EntityRelation` из IFC relations, только observations/attributes;
- не запускает Change Engine автоматически после sync, потому что для одного IFC observation обычно нет temporal diff.

## 3. Частично реализовано

### 🟡 Интеграция реальных extractors с Observation pipeline

Что уже работает: upload pipeline создает `DocumentVersion` после успешной обработки. Synthetic dataset создает observations/source fragments/attributes и прогоняет resolver/change engine. RAG pages/chunks/regions можно вручную синхронизировать в `SourceFragment`. IFC indexed elements можно вручную синхронизировать в `EntityObservation`/`AttributeObservation`.

Что осталось: подключить автоматический sync после upload/reindex; добавить extraction layer, который из RAG source fragments выделяет entity observations/attributes; добавить IFC relations -> `EntityRelation`.

Почему задача не завершена: нет официального ТЗ, реальных PDF и стабильного extractor contract для строительных сущностей.

### 🟡 Migrations

Что уже работает: модели additive; `Base.metadata.create_all` создаст новые таблицы; добавлен `schema_migrations` marker `20260914_case10_domain`.

Что осталось: если проект перейдет на production migration framework, перенести CASE10 schema в Alembic или другой штатный migration runner.

Почему задача не завершена: текущий проект не использует Alembic; резкая смена миграционного стека на этом этапе была бы большим рефакторингом.

### 🟡 Full Docker / live workflow QA

Что уже работает: JS-синтаксис проходит, новые screens подключены к API; локальный API + static frontend smoke пройден на synthetic dataset.

Что осталось: поднять полный Docker stack, проверить frontend через nginx/container path, загрузить реальный PDF/DOCX/IFC, дождаться RAG/IFC обработки и проверить sync buttons на живых сервисах.

Почему задача не завершена: локальный smoke не заменяет полный `docker compose up --build` и реальные upload/RAG/IFC flows.

## 4. Не начато

1. Автоматизировать RAG SourceFragment sync после успешного upload/reindex.
2. Автоматизировать IFC observation sync после успешного IFC indexing.
3. Добавить evaluation framework: precision/recall/manual_review_rate/high_confidence_match_rate.
4. Добавить secure/local AI provider abstraction.
5. Проверить frontend visual QA в полном Docker stack.
6. Добавить real extractor interfaces без hardcoded AR/KR/OV rules.
7. Добавить manual confirmation flow для Entity Resolution.
8. Добавить bbox highlight в Evidence Viewer после появления реальных bbox.
9. Добавить автоматический запуск RAG/IFC sync после успешного upload/reindex, когда runtime contract будет проверен на живом stack.

## 5. Архитектурные решения

- `CanonicalEntity` хранится независимо от `DocumentVersion`.
- `EntityObservation` может быть привязана к `CanonicalEntity`, но сама модель допускает nullable `canonical_entity_id` для будущего unresolved state.
- `AttributeObservation` хранит и raw, и normalized value, включая `normalized_numeric`, unit, extractor, confidence и source fragment.
- `SourceFragment.bbox` подготовлен как JSON: сейчас synthetic bbox `[110, 320, 430, 370]`, полноценный bbox extractor будет позже.
- `ObjectPortrait` не хранится отдельной таблицей и вычисляется `PortraitBuilder`.
- `ChangeEvent`/`ChangeIssue` хранятся в БД, чтобы protocol workflow мог ссылаться на стабильные issues.
- `ReviewDecision` хранит audit trail по решению инспектора; `ChangeIssue.status` обновляется текущим workflow-status.
- Neo4j пока не используется; графовая доменная модель реализована через SQL и `EntityRelation`.
- RAG, OCR, IFC и LLM сохранены как окружающие инструменты, а не центр продукта.
- Synthetic dataset явно помечен `case10-synthetic-v1` и не выдается за конкурсные данные.
- RAG source fragments синхронизируются через service boundary: API вызывает RAG HTTP export, а не читает RAG SQLite напрямую.
- IFC observations синхронизируются через service boundary: API вызывает IFC HTTP export, а не читает PostgreSQL IFC service напрямую.
- `SourceFragment.source_system + external_id` добавлены как idempotency key для внешних source rows.
- Полный taxonomy строительных элементов, hardcoded AR/KR/OV rules, СП/ГОСТ/СНиП validation, ИАИС «РиН», сложный visual diff и сложная IFC geometry validation намеренно не реализованы до официального ТЗ.

## 6. Изменения БД

Новые таблицы:
- `document_versions`
- `source_fragments`
- `canonical_entities`
- `entity_aliases`
- `entity_observations`
- `attribute_observations`
- `entity_relations`
- `change_events`
- `change_issues`
- `review_decisions`
- `schema_migrations`

Ключевые поля provenance:
- `document_versions.project_id`
- `document_versions.organization_id`
- `document_versions.document_stage`
- `document_versions.discipline`
- `document_versions.version`
- `document_versions.revision`
- `document_versions.content_hash`
- `source_fragments.page`
- `source_fragments.bbox`
- `source_fragments.source_system`
- `source_fragments.external_id`
- `source_fragments.metadata_json`
- `source_fragments.extractor`
- `source_fragments.confidence`
- `entity_observations.source_fragment_id`
- `attribute_observations.source_fragment_id`
- `attribute_observations.extractor`
- `attribute_observations.confidence`

Migration marker:
- `20260914_case10_domain`

Важно: destructive schema changes не делались.

## 7. Изменения API

Новые endpoints:
- `POST /api/case10/projects/{project_id}/synthetic-dataset`
- `GET /api/case10/overview?project_id=...`
- `GET /api/case10/documents?project_id=...`
- `GET /api/entities?project_id=...`
- `GET /api/entities/{entity_id}/portrait`
- `GET /api/case10/changes?project_id=...`
- `GET /api/case10/protocol?project_id=...`
- `POST /api/issues/{issue_id}/decisions`
- `POST /api/entities/{entity_id}/refresh-changes`
- `POST /api/case10/document-versions/{document_version_id}/sync-source-fragments`
- `POST /api/case10/document-versions/{document_version_id}/sync-ifc-observations?limit=...`
- RAG service: `GET /layout/source_fragments?document_id=...&organization_id=...`
- IFC service: `GET /models/{model_id}/observations?organization_id=...&limit=...`

Измененные existing endpoints:
- `POST /api/upload` поведение ответа не меняет, но после успешной обработки вызывает создание `DocumentVersion`.

## 8. Изменения frontend

Создано/изменено:
- навигация: `Проекты`, `Документы`, `Объекты`, `Изменения`, `Протокол`, `AI Assistant`;
- Project Overview;
- synthetic dataset action;
- DocumentVersion grouping;
- Object Registry table;
- Object Portrait panel;
- Changes table with columns `Entity`, `Parameter`, `П`, `Р`, `И`, `Risk`, `Confidence`, `Status`;
- Protocol workflow board with `New`, `Confirmed`, `Rejected`, `Needs Review`;
- Evidence Viewer shell `source A | source B`.
- Documents grouping actions:
  - `Синхронизировать источники` for RAG `DocumentVersion`;
  - `Синхронизировать IFC` for IFC `DocumentVersion`.
- В `frontend\index.html` добавлен отсутствовавший контейнер `#docStageGroups`, поэтому CASE10 `DocumentVersion` grouping теперь реально виден на экране `Документы`.
- В `frontend\app.js` исправлено форматирование unit в Object Portrait / Changes: значения вида `250 mm` больше не получают второй `mm`.

Состояние UI:
- functional shell implemented;
- local browser smoke performed against API on `127.0.0.1:8000` and static frontend on `127.0.0.1:3000`;
- full Docker/nginx visual QA not yet performed;
- no full frontend framework/build system exists; frontend is static HTML/CSS/JS.

## 9. Проверки

✅ Прочитан `CASE10_TECHNICAL_STRATEGY_V2.md`

✅ Выполнена инвентаризация структуры проекта

✅ Создан safety snapshot проекта

✅ Backend syntax:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service
```

Результат: success.

✅ Domain unit tests:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import site, unittest; site.addsitedir(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); suite=unittest.defaultTestLoader.discover('tests'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
```

Рабочая директория: `D:\Proga\LCT-hack-2026\api_service`

Результат после текущего запуска: `Ran 7 tests in 1.193s OK`.

✅ API import smoke:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import site, sys; site.addsitedir(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); sys.path.insert(0, r'D:\Proga\LCT-hack-2026\api_service'); sys.path.append(r'D:\Proga\LCT-hack-2026'); import app.main; print(app.main.app.title)"
```

Результат: `API Service`.

✅ Frontend JS syntax:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend\app.js
```

Результат: success.

✅ Local API + frontend HTTP/browser smoke:

Локально запускались:
- API: `http://127.0.0.1:8000`
- static frontend: `http://127.0.0.1:3000`

Проверено:
- login локальным admin;
- создание smoke project через API;
- `POST /api/case10/projects/{project_id}/synthetic-dataset`;
- `GET /api/case10/overview`;
- `GET /api/case10/documents`;
- `GET /api/entities`;
- `GET /api/entities/{id}/portrait`;
- `GET /api/case10/changes`;
- `GET /api/case10/protocol`;
- browser navigation: `Проекты`, `Документы`, `Объекты`, `Изменения`, `Протокол`, `AI Assistant`;
- экран `Документы` показывает П/Р/И synthetic `DocumentVersion`;
- экран `Изменения` больше не показывает `mm mm`;
- console errors в проверенных screens: 0.

✅ Docker Compose config:

```powershell
docker compose config
```

Результат: success.

✅ RAG/IFC/API syntax:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service ifc_service rag_service
```

Результат: success.

⚠️ Не запускалось:
- полный `docker compose up --build`;
- full Docker/nginx browser visual QA;
- реальные upload/RAG/IFC flows после изменений.

## 10. Известные проблемы

LOW: корень проекта не является git-репозиторием, поэтому нет штатного diff/branch workflow для всего проекта.

LOW: системный `python` не найден, `venv\Scripts\python.exe` ссылается на отсутствующий Python 3.12. Проверки запускались через bundled Codex Python + `venv\Lib\site-packages`.

LOW: текущий migration approach SQLite-oriented и самописный. Для P0 это сохранено, чтобы не делать большой рефакторинг.

LOW: frontend однофайловый; CASE10 shell добавлен без миграции на framework.

MEDIUM: RAG chunks/layout regions теперь можно вручную синхронизировать в `SourceFragment`, но автоматического sync после upload/reindex пока нет.

MEDIUM: IFC indexed elements теперь можно вручную синхронизировать в `EntityObservation`, но автоматического sync после завершения IFC indexing пока нет.

MEDIUM: `ChangeEngine` risk scoring временный и простой; он нужен только для P0 демонстрации pipeline.

LOW: локальный browser smoke выполнен, но full Docker/nginx visual QA не выполнен; возможны отличия container/runtime окружения.

LOW: Object Registry table на desktop smoke имеет внутренний горизонтальный скролл для компактной таблицы. Это не ломает workflow, но позже можно улучшить адаптивность колонок.

## 11. Что делать следующим запуском

1. Открыть `C:\Users\Ivan\Downloads\CASE10_TECHNICAL_STRATEGY_V2.md`.
2. Открыть `D:\Proga\LCT-hack-2026\CASE10_IMPLEMENTATION_PROGRESS.md`.
3. Проверить реальное состояние файлов:
   - `D:\Proga\LCT-hack-2026\api_service\app\db\models.py`
   - `D:\Proga\LCT-hack-2026\api_service\app\domain\*.py`
   - `D:\Proga\LCT-hack-2026\api_service\app\api\routes_case10.py`
   - `D:\Proga\LCT-hack-2026\frontend\index.html`
   - `D:\Proga\LCT-hack-2026\frontend\app.js`
   - `D:\Proga\LCT-hack-2026\frontend\styles.css`
4. Запустить backend syntax/tests и `docker compose config` командами ниже.
5. Поднять полный Docker stack и проверить frontend через nginx/container path.
6. Проверить live upload/RAG workflow:
   - создать проект;
   - загрузить PDF/DOCX;
   - дождаться RAG готовности;
   - убедиться, что появился `DocumentVersion`;
   - нажать `Синхронизировать источники`;
   - проверить `source_fragments` в API DB.
7. Проверить IFC live workflow:
   - загрузить IFC;
   - дождаться indexed status;
   - убедиться, что появился IFC `DocumentVersion`;
   - нажать `Синхронизировать IFC`;
   - проверить Object Registry/Object Portrait.
8. Если live workflow стабилен, добавить автоматический sync после успешного upload/reindex/indexed status.
9. Добавить RAG text entity extractor interface: `SourceFragment -> ObservationDraft -> resolver`.
10. Добавить IFC relations -> `EntityRelation`.
11. Улучшить Object Registry responsive layout, если full Docker/browser QA подтвердит неудобный горизонтальный скролл.

## 12. Команды для продолжения

```powershell
cd D:\Proga\LCT-hack-2026
```

Backend syntax:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service
```

Domain tests:

```powershell
cd D:\Proga\LCT-hack-2026\api_service
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import site, unittest; site.addsitedir(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); suite=unittest.defaultTestLoader.discover('tests'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
```

Frontend JS syntax:

```powershell
cd D:\Proga\LCT-hack-2026
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend\app.js
```

Compose config:

```powershell
cd D:\Proga\LCT-hack-2026
docker compose config
```

Full stack smoke, когда будет нужно:

```powershell
cd D:\Proga\LCT-hack-2026
docker compose up --build
```

## 13. Checkpoint остановки по V3, 15.09.2026

Статус: работа приостановлена по просьбе пользователя сразу после первого крупного V3-среза. Код еще не прогонялся тестами после последних правок, поэтому следующий запуск должен начать с syntax/tests и исправления возможных ошибок импорта/схемы.

### Что было сделано в этом заходе

- Прочитан официальный `CASE10_TECHNICAL_STRATEGY_V3_OFFICIAL_TZ.md` из `C:\Users\Ivan\Downloads`.
- Перечитан текущий `CASE10_IMPLEMENTATION_PROGRESS.md`.
- Быстро проверены реальные файлы V2-реализации:
  - `api_service\app\db\models.py`
  - `api_service\app\db\session.py`
  - `api_service\app\api\routes_case10.py`
  - `api_service\app\api\routes_upload.py`
  - `api_service\app\domain\document_versions.py`
  - `api_service\app\domain\synthetic_dataset.py`
  - `api_service\tests\test_case10_api.py`
  - `api_service\tests\test_case10_domain.py`
  - `frontend\index.html`
  - `frontend\app.js`
  - `frontend\styles.css`
  - `docker-compose.yml`

### V3 core, добавлено в код

Добавлены новые DB-модели рядом с V2-слоем, без удаления `CanonicalEntity / ObjectPortrait / ChangeEngine`:

- `ConstructionObject`
- `MatrixVersion`
- `Param`
- `InspectionProcess`
- `EvidenceGroup`
- `EvidenceFragment`
- `EvidenceDecision`
- `Protocol`
- `AuditLog`
- `GoldDraftItem`
- `ProcessingCache`

`DocumentVersion` расширен V3-полями:

- `object_id`
- `doc_stage`
- `document_code`
- `approval_status`
- `approval_date`
- `file_hash`
- `file_path`
- `predecessor_id`
- `successor_id`
- `uploaded_at`

Добавлена SQLite additive migration в `api_service\app\db\session.py` для новых `document_versions` полей и marker `20260915_case10_v3_core`.

### V3 pipeline, добавлено

Новые файлы:

- `api_service\app\domain\v3_extractors.py`
- `api_service\app\domain\v3_messaging.py`
- `api_service\app\domain\v3_pipeline.py`

Что заложено:

- официальный process state machine: `PENDING -> PARSING -> READY -> VERIFYING -> COMPLETED -> FINALIZED`;
- official finding statuses:
  - `CANDIDATE`
  - `CONFIRMED_VIOLATION`
  - `NEGATIVE_VERIFIED`
  - `MISSING_EVIDENCE`
  - `NOT_APPLICABLE`
  - `NOT_COMPARABLE`
  - `CLARIFICATION_REQUIRED`
  - `SUSPICION`
- inspector-only переходы:
  - `Confirm -> CONFIRMED_VIOLATION`
  - `Reject -> NEGATIVE_VERIFIED`
  - `Clarification Required -> CLARIFICATION_REQUIRED`
- demo matrix из 5 параметров, явно помеченная как fixture, не официальный список 132;
- `ParameterExtractor` interface и заглушки:
  - `RegexExtractor`
  - `SemanticExtractor`
  - `TableExtractor`
  - `OCRExtractor`
  - `CVExtractor`
- unified extraction result:
  - `value`
  - `normalized_value`
  - `confidence`
  - `file`
  - `page`
  - `bbox`
  - `extractor`
- нормализация bbox в диапазон `[0;1]`;
- выбор актуальных approved revisions по стадиям;
- сбор `EvidenceGroup` и `EvidenceFragment`;
- versioned `Protocol` JSON и минимальный PDF через PyMuPDF;
- `AuditLog` для process/protocol/inspector actions;
- `GoldDraftItem` после решений инспектора;
- local DB cache + Redis adapter;
- RabbitMQ event publisher adapter, graceful fallback если RabbitMQ недоступен.

### API, добавлено/изменено

Изменен `api_service\app\api\routes_case10.py`:

- `POST /api/case10/projects/{project_id}/processes`
- `POST /api/case10/processes/{process_id}/run`
- `GET /api/case10/processes`
- `GET /api/case10/processes/{process_id}/status`
- `GET /api/case10/matrix`
- `POST /api/case10/matrix/import`
- `GET /api/case10/evidence-groups`
- `GET /api/case10/evidence-groups/{evidence_group_id}`
- `POST /api/case10/evidence-groups/{evidence_group_id}/decisions`
- `GET /api/case10/protocols/current`
- `GET /api/case10/protocols/{protocol_id}/json`
- `GET /api/case10/protocols/{protocol_id}/pdf`
- `POST /api/case10/protocols/{protocol_id}/finalize`

Изменен `POST /api/case10/projects/{project_id}/synthetic-dataset`:

- теперь после seed создает/берет open `InspectionProcess`;
- запускает V3 pipeline;
- возвращает `process_id` и `process_status`.

Изменен `GET /api/case10/overview`:

- добавляет V3 `inspection` и counts по official finding statuses;
- pipeline на overview заменен на V3-цепочку.

Изменен `GET /api/case10/documents`:

- возвращает V3-поля документа;
- помечает `is_current_approved`.

Изменен `api_service\app\schemas.py`:

- `UploadResponse` теперь имеет optional `process_id`.

Изменен `api_service\app\api\routes_upload.py`:

- добавлена поддержка XML как обязательного формата V3;
- XML сохраняется в `DocumentVersion` без RAG;
- создается базовый `SourceFragment` для XML;
- upload создает open `InspectionProcess` и возвращает `process_id`.

Изменен `api_service\app\domain\document_versions.py`:

- infer `document_code`;
- infer `approval_status`;
- infer `approval_date`;
- запись `object_id`, `doc_stage`, `file_hash`, `file_path`;
- простая связка `predecessor_id / successor_id`.

Изменен `rag_service\app\main.py`:

- RAG `source_fragments` export теперь кладет `width/height` в metadata для region/chunk bbox normalization.

### Frontend, добавлено/изменено

Изменен `frontend\index.html`:

- добавлены вкладки `Матрица` и `Кандидаты`;
- на overview добавлена кнопка `Запустить проверку`;
- overview теперь показывает текущий process;
- documents upload accept расширен до `.pdf,.docx,.xml,.ifc`;
- file type select получил `XML`.

Изменен `frontend\app.js`:

- добавлены labels official finding statuses и process statuses;
- `refreshCase10Shell()` обновляет matrix/candidates;
- synthetic dataset после seed переводит на `Кандидаты`;
- добавлен `startInspection()`;
- overview показывает V3 finding counts и process summary;
- documents registry показывает `document_code`, `revision`, `approval_status`, SHA-256 и `актуальная утвержденная`;
- добавлен `refreshMatrix()`;
- добавлен `refreshCandidates()` и рендер Evidence Card;
- Evidence Card показывает:
  - Parameter
  - Expected
  - Actual
  - Delta
  - evidence fragments
  - stage
  - revision
  - approval status
  - page
  - normalized bbox
  - `Confirm`
  - `Reject`
  - `Clarify`
- protocol screen переведен на `GET /api/case10/protocols/current`;
- добавлены PDF download и `FINALIZE`;
- добавлены handlers для evidence decisions/finalization/PDF.

Изменен `frontend\styles.css`:

- добавлены только функциональные стили для process summary, matrix, candidate cards, evidence fragments и protocol summary.

### Docker / infra

Изменен `docker-compose.yml` пока НЕ был изменен в этом заходе. В `api_service\app\config.py` добавлены переменные:

- `RABBITMQ_URL`
- `REDIS_URL`

Изменен `api_service\requirements.txt`:

- добавлен `pika==1.3.2`;
- добавлен `redis==5.0.1`.

Важно: в `docker-compose.yml` RabbitMQ service еще НЕ добавлен. Redis уже есть в compose. Следующий шаг должен либо добавить RabbitMQ service/env, либо временно оставить `RABBITMQ_URL` пустым и явно отметить как fallback.

### Что реально протестировано после этих правок

Не тестировалось. Пользователь попросил приостановить работу до запуска проверок.

Нужно первым делом запустить:

```powershell
cd D:\Proga\LCT-hack-2026
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service rag_service
```

```powershell
cd D:\Proga\LCT-hack-2026\api_service
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import site, unittest; site.addsitedir(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); suite=unittest.defaultTestLoader.discover('tests'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(0 if result.wasSuccessful() else 1)"
```

```powershell
cd D:\Proga\LCT-hack-2026
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend\app.js
```

```powershell
cd D:\Proga\LCT-hack-2026
docker compose config
```

### Что может быть сломано / требует немедленной проверки

- Возможны Python import/type ошибки в новых `v3_*` файлах.
- Возможны SQLAlchemy relationship conflicts или SQLite migration нюансы из-за self-relation `DocumentVersion.predecessor/successor`.
- Возможен конфликт unique constraint `uq_evidence_group_process_param_entity` при `canonical_entity_id = NULL` в SQLite/Postgres semantics.
- Возможен баг в demo regex для `DEMO-KR-55`, потому что в одном regex использованы `value` и `value2`, но extractor теперь берет первый non-null group.
- Старые tests ожидают V2 statuses `Confirmed/Rejected`; они могут продолжить проходить, но стоит добавить новые V3 tests.
- Frontend JS после правок еще не проверен на синтаксис.
- `protocol_pdf_bytes()` использует latin-1 fallback для текста PDF; кириллица в минимальном PDF может отображаться как `?`. Это допустимо только как временный минимальный PDF, но позже нужно нормальное шрифтовое оформление.
- `Reject` reason/comment пока запрашиваются через `window.prompt`; это рабочий минимум, но UI стоит заменить на inline form/modal.

### Следующий конкретный шаг

1. Запустить проверки из раздела выше.
2. Исправить ошибки компиляции/тестов.
3. Добавить тест V3 end-to-end:
   - seed synthetic dataset;
   - получить `process_id`;
   - `GET /processes/{id}/status` == `READY`;
   - `GET /evidence-groups` содержит `CANDIDATE`;
   - evidence fragments имеют page + normalized bbox `[0;1]`;
   - inspector `Confirm` переводит только выбранный group в `CONFIRMED_VIOLATION`;
   - создается новая версия protocol;
   - `FINALIZE` блокирует новое решение.
4. Добавить RabbitMQ service/env в `docker-compose.yml` или явно оформить fallback как demo-mode.
5. Обновить progress после прохождения тестов.

---

## 14. Продолжение V3 после checkpoint, 15.09.2026

Стартовая точка: выполнен раздел "Следующий конкретный шаг" из checkpoint 13. Повторный полный audit проекта не делался.

### Что сделано

- Исправлена сериализация V3 protocol/gold payload:
  - `datetime` теперь приводится к JSON-safe ISO string перед сохранением в JSON columns;
  - при ошибке `run_process()` делает rollback перед записью технического статуса ошибки.
- Добавлен V3 end-to-end API test:
  - synthetic dataset;
  - `process_id`;
  - статус процесса `READY`;
  - Evidence Groups со статусом `CANDIDATE`;
  - EvidenceFragment с `page` и normalized bbox `[0;1]`;
  - inspector `Confirm`;
  - переход в `CONFIRMED_VIOLATION`;
  - создание новой версии protocol;
  - `FINALIZE`;
  - запрет нового inspector decision после finalization.
- Добавлен RabbitMQ в `docker-compose.yml`:
  - service `rabbitmq`;
  - healthcheck;
  - management port;
  - `RABBITMQ_URL` в API env;
  - API `depends_on` RabbitMQ health.
- Добавлен минимальный incremental recompute при дозагрузке/синхронизации документов:
  - upload job после PDF/DOCX/XML создает/находит open V3 process;
  - вычисляет affected param codes по стадиям документов;
  - пересчитывает только затронутые параметры до finalization;
  - sync endpoints для RAG/IFC также возвращают `process_id` и `affected_param_codes`.
- Добавлен XML upload test:
  - загрузка `.xml`;
  - создание `DocumentVersion`;
  - stage `project`;
  - approval `APPROVED`;
  - SHA-256;
  - `SourceFragment` с `source_system=xml`;
  - bbox `[0.0, 0.0, 1.0, 1.0]`;
  - V3 process получает `READY`.

### Измененные файлы

- `api_service\app\domain\v3_pipeline.py`
- `api_service\app\api\routes_case10.py`
- `api_service\app\api\routes_upload.py`
- `api_service\tests\test_case10_api.py`
- `docker-compose.yml`

Также остаются актуальными изменения checkpoint 13 в:

- `api_service\app\db\models.py`
- `api_service\app\db\session.py`
- `api_service\app\config.py`
- `api_service\app\schemas.py`
- `api_service\app\domain\v3_extractors.py`
- `api_service\app\domain\v3_messaging.py`
- `api_service\app\domain\document_versions.py`
- `api_service\app\domain\synthetic_dataset.py`
- `rag_service\app\main.py`
- `frontend\index.html`
- `frontend\app.js`
- `frontend\styles.css`
- `api_service\requirements.txt`

### Что реально протестировано

Успешно:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service rag_service
```

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend\app.js
```

```powershell
docker compose config --quiet
```

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import sys, runpy; sys.path.insert(1, r'D:\Proga\LCT-hack-2026\api_service'); sys.path.append(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); sys.argv=['unittest','discover','api_service/tests']; runpy.run_module('unittest', run_name='__main__')"
```

Результат tests:

- `Ran 9 tests`
- `OK`

Важно по окружению:

- обычный bundled Python без `venv\Lib\site-packages` не видит `fastapi`;
- `venv\Scripts\python.exe` сейчас не стартует, потому что venv ссылается на отсутствующий `C:\Users\Ivan\AppData\Local\Programs\Python\Python312\python.exe`;
- рабочий способ для локальных тестов сейчас: bundled Python + `api_service` в `sys.path` + `venv\Lib\site-packages` в конце `sys.path`.

### Что сломано / не готово

- Полный Docker stack не поднят: `docker compose up --build -d` упал из-за незапущенного Docker Desktop / отсутствующего Windows pipe `dockerDesktopLinuxEngine`.
- RabbitMQ добавлен в compose, но runtime-проверка связки API -> RabbitMQ не выполнена из-за Docker blocker.
- Реальные PDF/DOCX/XML конкурсные документы еще не прогонялись.
- PDF protocol остается минимальным; кириллица может отображаться некорректно из-за latin-1 fallback.
- Inspector UI рабочий минимальный, но `Reject` reason/comment пока через `window.prompt`.
- Evaluation harness под официальные метрики еще не добавлен.

### Следующий конкретный шаг

1. Запустить Docker Desktop.
2. Выполнить:

```powershell
cd D:\Proga\LCT-hack-2026
docker compose up --build -d
```

3. Проверить health/готовность сервисов:
   - API;
   - RAG;
   - IFC;
   - frontend;
   - RabbitMQ;
   - Redis.
4. Прогнать минимальный ручной V3 сценарий через UI:
   - создать объект / seed synthetic dataset;
   - загрузить ПД / РД / ИД;
   - получить `process_id`;
   - открыть Evidence Card;
   - Confirm / Reject / Clarify;
   - получить protocol version;
   - дозагрузить файл до finalization;
   - убедиться, что incremental recompute затрагивает только relevant param codes;
   - выполнить `FINALIZE`;
   - проверить запрет изменений после `FINALIZED`.
5. После runtime-проверки перейти к evaluation harness и подготовке импорта реальных 132 параметров без придумывания отсутствующих параметров.

---

## 15. Runtime smoke + evaluation harness, 15.09.2026

Стартовая точка: Docker Desktop включен. Работа продолжена с "Следующий конкретный шаг" из checkpoint 14, без повторного полного audit.

### Что сделано

- Выполнена попытка полного `docker compose up --build -d`.
- Первичная сборка RAG упала на сетевом сбое Debian mirror:
  - `Unable to connect to deb.debian.org:http:` при скачивании OCR/system packages.
- Исправлена устойчивость `rag_service\Dockerfile`:
  - добавлен retry для `apt-get update/install`;
  - включены `Acquire::Retries=5` и отключен HTTP pipeline depth;
  - после этого `docker compose build rag --progress=plain` успешно собрал image `lct-hack-2026-rag:latest`.
- Поднят полный CASE10 runtime:
  - Postgres на host port `15432`;
  - Redis на host port `16379`;
  - RabbitMQ на host ports `15692` / management `15693`;
  - IFC на `8002`;
  - RAG на `8001`;
  - API на штатном `8000`;
  - frontend на штатном `3000`.
- Проверен RabbitMQ runtime:
  - контейнер healthy;
  - очередь `case10.process.events` создана;
  - после V3 smoke в очереди были сообщения процесса.
- Выполнен V3 HTTP smoke через контейнерный API `http://127.0.0.1:8000`:
  - login default admin;
  - create project;
  - seed synthetic dataset;
  - получить `process_id`;
  - `GET /case10/processes/{id}/status` => `READY`;
  - upload scenario => `FULL`;
  - Evidence Groups содержат `CANDIDATE`;
  - EvidenceFragment имеет `page` и normalized bbox `[0;1]`;
  - inspector `Confirm`;
  - finding переходит в `CONFIRMED_VIOLATION`;
  - protocol version `1 -> 2`;
  - XML дозагрузка до finalization;
  - incremental recompute дает protocol version `3`;
  - `FINALIZE`;
  - новое inspector decision после finalization возвращает HTTP `409`.
- Найден и исправлен runtime-баг synthetic dataset:
  - старый генератор создавал глобальные IDs `WALL-000001`, `FLOOR-000001`;
  - во втором проекте seed падал на unique constraint `canonical_entities.id`;
  - теперь synthetic IDs project-scoped: `WALL-P000003-000001`, `FLOOR-P000003-000001`.
- Добавлен тест на seed synthetic dataset в нескольких проектах.
- Добавлен минимальный обязательный `evaluation/` harness:
  - JSON gold/predictions input;
  - CLI `python -m evaluation.run_evaluation`;
  - расчет official V3 metrics:
    - `ocr_cer`;
    - `ocr_wer`;
    - `ocr_character_accuracy`;
    - `ocr_coverage`;
    - `key_field_exact_match`;
    - `document_linking_accuracy`;
    - `evidence_page_accuracy`;
    - `evidence_bbox_iou`;
    - `evidence_localization_accuracy`;
    - `finding_precision`;
    - `finding_recall`;
    - `finding_f1`;
    - `false_positive_rate`;
    - `coverage`;
    - `abstention_rate`;
    - `per_category_metrics`;
    - `quality_gates`.

### Измененные файлы

- `api_service\app\domain\synthetic_dataset.py`
- `api_service\tests\test_case10_api.py`
- `api_service\tests\test_case10_evaluation.py`
- `evaluation\__init__.py`
- `evaluation\metrics.py`
- `evaluation\run_evaluation.py`
- `evaluation\README.md`
- `rag_service\Dockerfile`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

Также остаются актуальными изменения checkpoint 13/14.

### Что реально протестировано

Успешно:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service rag_service evaluation
```

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend\app.js
```

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -c "import sys, runpy; sys.path.insert(1, r'D:\Proga\LCT-hack-2026\api_service'); sys.path.append(r'D:\Proga\LCT-hack-2026\venv\Lib\site-packages'); sys.argv=['unittest','discover','api_service/tests']; runpy.run_module('unittest', run_name='__main__')"
```

Результат tests:

- `Ran 13 tests`
- `OK`

Docker/runtime:

- RAG image:
  - `docker compose build rag --progress=plain` => success;
  - image `lct-hack-2026-rag:latest` built.
- Full stack:
  - `docker compose up -d` => success.
- `docker compose ps` показывает healthy:
  - `api` on `127.0.0.1:8000`;
  - `rag` on `127.0.0.1:8001`;
  - `frontend` on `127.0.0.1:3000`;
  - `ifc` on `127.0.0.1:8002`;
  - `landing` on `127.0.0.1:3001`;
  - `postgres` on `127.0.0.1:15432`;
  - `rabbitmq` on `127.0.0.1:15692`, management `127.0.0.1:15693`;
  - `redis` on `127.0.0.1:16379`.
- API health:
  - `GET http://127.0.0.1:8000/health` => `{"status":"ok"}`
- RAG health:
  - `GET http://127.0.0.1:8001/health` => `{"status":"ok"}`
- Frontend:
  - `GET http://127.0.0.1:3000/` => HTTP `200`
- IFC health:
  - `GET http://127.0.0.1:8002/health` => `{"status":"ok"}`
- RabbitMQ queue:
  - `case10.process.events`;
  - `messages_ready` observed after smoke;
  - `consumers=0` because consumer worker is not implemented yet.
- Full-stack V3 HTTP smoke через контейнерный API `http://127.0.0.1:8000/api`:
  - project id `4`;
  - process id `794c65ad3f4b4c06b93ede613ed29551`;
  - initial status `READY`;
  - upload scenario `FULL`;
  - initial `CANDIDATE` count `3`;
  - candidate evidence fragments have normalized bbox `[0;1]`;
  - inspector `Confirm` => `CONFIRMED_VIOLATION`;
  - protocol version `1 -> 2`;
  - XML дозагрузка до finalization вернула тот же `process_id`;
  - status после processing `READY`;
  - protocol version after upload `3`;
  - `FINALIZE` => `FINALIZED`;
  - post-finalize decision => HTTP `409`.

### Что сломано / не готово

- Full docker stack теперь поднимается, но real-data контур PDF/DOCX через RAG/OCR еще не прогонялся на настоящих конкурсных документах.
- Реальные конкурсные PDF/DOCX/XML еще не прогонялись.
- RabbitMQ publisher работает, но dedicated consumer/worker для async processing пока нет; текущий API still выполняет V3 run synchronously and publishes events.
- Evaluation harness считает метрики из JSON gold/predictions, но еще не подключен к автоматическому export фактических V3 predictions из API/DB.
- Нет реального gold dataset и полного набора 132 параметров.
- PDF protocol минимальный; кириллица может отображаться некорректно из-за latin-1 fallback.
- `git status` / `git diff` сейчас недоступны: `D:\Proga\LCT-hack-2026` не определяется как git repository в текущем окружении.

### Следующий конкретный шаг

1. Прогнать реальные PDF/DOCX/XML через upload:
   - проверить `DocumentVersion`;
   - проверить RAG `SourceFragment`;
   - выполнить sync fragments;
   - получить Evidence Card с real page/bbox.
2. Подключить export predictions из V3 protocol/evidence groups в `evaluation/` JSON schema.
3. Подготовить импорт реального набора 132 параметров: только из официального источника, без придумывания недостающих параметров.
4. Добавить dedicated RabbitMQ consumer/worker для async V3 processing, чтобы статусная модель была не только API-triggered.
5. Отдельно улучшить PDF protocol font handling для кириллицы.

---

## 16. P0 official dataset integration checkpoint, 15.09.2026 23:09 MSK

Стартовая точка: продолжение по V3, главный приоритет — перейти от synthetic/demo к официальной матрице 132 и реальному датасету. Работа остановлена по просьбе пользователя до тяжёлого real-data e2e smoke.

### Что сделано

- Подключен импорт `parameter_catalog_132.jsonl`:
  - создается `MatrixVersion` `official-132-v1`;
  - 132 параметра становятся default для CASE10;
  - demo-матрица оставлена только как fallback и для synthetic seed.
- Добавлен импорт official dataset:
  - `document_manifest.jsonl`;
  - `files_index.jsonl`;
  - `page_index.jsonl`;
  - `annotations.jsonl`;
  - `public_gold_checks.jsonl`;
  - `all_gold_checks.jsonl` с leakage guard.
- Расширена модель данных под real dataset:
  - `DocumentVersion.dataset_file_id/dataset_split/dataset_stage/dataset_section/dataset_metadata`;
  - `SourceFragment.bbox_pdf/page_width/page_height`;
  - `EvidenceFragment.dataset_file_id/bbox_pdf/page_width/page_height`;
  - новая таблица `GoldCheckFixture`.
- Добавлены lightweight DB migrations для SQLite/Postgres, чтобы новые поля создавались без ручной миграции.
- V3 pipeline теперь:
  - использует official matrix по умолчанию;
  - фильтрует документы по `process.object_id`;
  - переносит `file_id`, normalized bbox, pdf bbox и размеры страницы в EvidenceFragment;
  - строит Evidence Groups из imported gold fixtures:
    - `VIOLATION_PRESENT` -> `CANDIDATE`;
    - `NO_VIOLATION` -> `NEGATIVE_VERIFIED`;
    - free hypotheses -> `SUSPICION`;
    - `CONFIRMED_VIOLATION` по-прежнему создается только решением инспектора.
- Добавлен exporter:
  - `evaluation/exporter.py`;
  - API endpoints для protocol predictions и submission payload;
  - базовая validation submission schema.
- Evaluation harness расширен:
  - загрузка gold JSONL напрямую;
  - leakage guard для `training`, `evaluation`, `submission/inference`;
  - hidden labels заблокированы по умолчанию.
- Inspector UI минимально обновлен:
  - кнопка `Импорт CASE10 real data`;
  - matrix table показывает official metadata (`parameter_id`, `matrix_row`, `criticality`, `mapping_status`, source refs);
  - документы показывают `file_id`, dataset stage/split/section;
  - Evidence Card показывает `file_id`, normalized bbox, pdf bbox, page width/height.
- Docker runtime обновлен:
  - API image теперь копирует `evaluation/`;
  - `case_data/` и `learning_data/` подключены в API container read-only.

### Измененные файлы

- `api_service/app/db/models.py`
- `api_service/app/db/session.py`
- `api_service/app/domain/official_dataset.py`
- `api_service/app/domain/v3_pipeline.py`
- `api_service/app/api/routes_case10.py`
- `evaluation/fixtures.py`
- `evaluation/exporter.py`
- `evaluation/run_evaluation.py`
- `frontend/index.html`
- `frontend/app.js`
- `api_service/Dockerfile`
- `docker-compose.yml`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

### Что реально проверено

Успешно:

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall api_service/app evaluation
```

```powershell
C:\Users\Ivan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --check frontend/app.js
```

```powershell
docker compose config
```

```powershell
docker compose up -d --build api frontend
```

Также проверено:

- Python import smoke: `app.main` и `evaluation.exporter` импортируются.
- API health: `GET http://127.0.0.1:8000/health` -> `{"status":"ok"}`.
- Frontend: `GET http://127.0.0.1:3000/` -> HTTP `200`.

Не завершено:

- `pytest` недоступен в текущем bundled Python / venv (`ModuleNotFoundError: No module named 'pytest'`).
- `unittest discover api_service/tests` был остановлен после долгого выполнения, чтобы не тратить оставшийся лимит; до остановки был виден первый успешный тест и только warning про `datetime.utcnow()`.

### Что сломано / не готово

- Real-data e2e импорт Tyumenskaya/Novoslobodskaya через новый endpoint еще не прогнан: остановлено по просьбе пользователя до начала тяжелого smoke.
- Нужно проверить фактическую производительность импорта `annotations.jsonl` на 30k+ строк в контейнерной БД.
- Нужно проверить, что generated Evidence Groups по public/all gold дают ожидаемые статусы:
  - Tyumenskaya: `IOS4-078`, `IOS4-079` как `CANDIDATE`;
  - `FREE-HEATING-001` как `SUSPICION`;
  - Novoslobodskaya: `PZ-009`, `KR-055`, `KR-058` как `NEGATIVE_VERIFIED`.
- Submission endpoint добавлен, но еще не проверен against official `submission_schema.json`.
- Dedicated RabbitMQ consumer/worker всё еще не реализован; V3 run остается API-triggered, хотя события RabbitMQ публикуются.
- `git status` / commit недоступны: текущая директория не определяется как git repository.

### Что делать следующим запуском

1. Не повторять полный audit. Начать с runtime smoke нового P0 endpoint:

```http
POST /api/case10/projects/{project_id}/official-dataset/import
```

Body:

```json
{
  "object_ids": ["OBJ-TYUMENSKAYA-5-GOLD-SEED", "OBJ-NOVOSLOBODSKAYA"],
  "include_hidden": false,
  "include_pages": true,
  "include_annotations": true,
  "include_gold": true,
  "run_processes": true
}
```

2. Проверить результаты:
   - `matrix_params == 132`;
   - imported documents/pages/annotations counts;
   - создано 2 процесса;
   - оба процесса доходят до `READY`;
   - Evidence fragments имеют `file_id`, `page`, `bbox_normalized`, `bbox_pdf`, `page_width`, `page_height`.
3. Открыть `/case10/evidence-groups` и проверить контрольные статусы по Tyumenskaya/Novoslobodskaya из списка выше.
4. Проверить:
   - `GET /api/case10/protocols/{protocol_id}/evaluation-predictions`;
   - `GET /api/case10/protocols/{protocol_id}/submission`;
   - validation against `case_data/.../submission_schema.json`.
5. После successful real-data smoke обновить `CASE10_IMPLEMENTATION_PROGRESS.md` новым checkpoint и только затем переходить к RabbitMQ worker или accuracy/evaluation improvements.

---

## 17. P0 runtime import, 16.09.2026

- Продолжение checkpoint 16. Docker API/frontend и зависимости запущены на прежних host ports 8000/3000/15432/16379/15692/15693.
- Исправлен неограниченный поиск датасета: `official_dataset.py` больше не обходит весь диск/корень контейнера. Найденные пути кэшируются в процессе.
- Добавлен повторяемый HTTP smoke `evaluation/smoke_real_data.py` (учетные данные из env, в отчет не попадают).
- На контейнерном API создан проект **5**, `CASE10 official public P0`.
- Реально проверен импорт: official default **132**, documents **203**, files_index **203**, pages **10146**, annotations **30318**. Hidden **213** документов не импортированы. Импорт около **27 секунд** (с дополнительным GET matrix около 35 секунд до кэширования поиска).
- Отчеты: `evaluation/reports/real_data/import.json`, `matrix.json`.
- Найден критичный дефект checkpoint 16: `_create_gold_fixture_groups` копировал ответы GOLD в runtime findings. Такой прогон НЕ является независимой оценкой точности. Удален вызов из `run_process`; official evidence baseline теперь берет только `AUTO_FIELD_CANDIDATE` без `check_id`. GOLD хранится отдельно для evaluator.
- Импортер больше не придумывает APPROVED при отсутствии approval metadata: UNKNOWN. Смешанная стадия RD_ID_MIXED не доказывает наличие отдельных РД и ИД.
- В работе после этого блока: проверка повторного импорта, независимых evidence/export/evaluation, просмотр оригинальной страницы с bbox, протокол приложения 2. Контрольные positives/negatives пока НЕ считаются найденными независимым алгоритмом.
- Изменены: `official_dataset.py`, `v3_pipeline.py`, добавлены `official_evidence.py`, `evaluation/smoke_real_data.py`, этот файл.

## 18. Независимый public runtime и evaluation, 16.09.2026

- Повторный импорт проекта 5: **0 новых** документов/страниц/аннотаций, обновлено 203 / 10146 / 30318. Около 11-17 секунд.
- Успешно выполнен `python -m evaluation.smoke_real_data --verify-pages` через контейнерный HTTP API.
- Novoslobodskaya: process `79aeeed6503c4e4fa21ec3856ee9e8e5`, protocol 12, 132 группы, 175 fragments, READY; 111 NOT_COMPARABLE / 21 MISSING_EVIDENCE.
- Tyumenskaya: process `716826249eab409b98c424dcc5da00ff`, protocol 13, 132 группы, 175 fragments, READY; 132 CLARIFICATION_REQUIRED из-за неразрешенной RD_ID_MIXED и отсутствия извлеченных значений.
- У всех проверенных EvidenceFragment есть file_id/page/normalized bbox/pdf bbox/ширина/высота. Новый endpoint `/api/case10/evidence-fragments/{id}/page.png` читает **оригинал** из participant ZIP, проверяет SHA-256 и выделяет bbox. Добавлена кнопка в UI. Исправлена CP866-кодировка имен ZIP; annotated answer overlays не используются.
- Оба submission проверены библиотекой jsonschema по **официальной** `submission_schema.json`: ошибок 0.
- Исправлен evaluator: SUSPICION не positive; matching по object/param/location вместо GOLD check_id; extra positives учитываются; file+page проверяются вместе; bbox IoU только где GOLD содержит bbox; coverage считается по уникальным параметрам; пустой OCR gold дает null, не выдуманную точность.
- Exporter не переносит actual в отсутствующую ИД, учитывает inspector Reject вместо старого delta label; GOLD-derived протоколы дают 409 при экспорте. Training guard нельзя обойти allow_organizer_only; скрытые blocked rows не выдаются как inputs.
- Реальные метрики baseline: coverage **0**, abstention **1** на обоих объектах; Tyumenskaya F1 **0**, Novoslobodskaya F1 **null** (нет positives). Это подтверждает работу измерения, НЕ достижение accuracy и НЕ закрытие finding e2e. Контрольные CANDIDATE/NEGATIVE_VERIFIED пока не получены независимо.
- Отчеты и PNG: `evaluation/reports/real_data/`. Проверки: 20 unittest OK, JS syntax OK, Docker rebuild OK.
- Изменены/добавлены: `official_evidence.py`, `dataset_sources.py`, `official_dataset.py`, `v3_pipeline.py`, `routes_case10.py`, `requirements.txt`, `frontend/app.js`, `evaluation/{exporter,fixtures,metrics,run_evaluation,smoke_real_data}.py`, `tests/test_case10_{evaluation,official}.py`.
- Следующий блок в работе: минимальный протокол по приложению 2, history/audit/finalization и PDF с кириллицей. После него первая незакрытая функциональная P0: независимый extractor/rule pack реальных значений для IOS4-078/079, KR-055/058, PZ-009; не возвращать GOLD-replay.

## 19. Протокол по приложению 2 и финализация, 16.09.2026

### Что сделано

- Добавлен структурированный `annex_2` в JSON протокола с семью разделами:
  - статус загрузки ПД / РД / ИД;
  - сводная статистика по Матрице;
  - непроверенные параметры;
  - критические подтвержденные нарушения;
  - существенные подтвержденные нарушения;
  - подозрения ИИ;
  - резолютивная часть.
- В строки протокола входят EvidenceFragment и решения инспектора: file/revision/page/bbox, user/time/reason/comment.
- PDF переведен на PyMuPDF Story: A4, перенос длинных таблиц, кириллица, нумерация страниц, русские человекочитаемые статусы. Технические enum-коды в JSON/API сохранены.
- Добавлены API history и audit для процесса.
- После каждого решения инспектора создается новая версия протокола; stale version финализировать нельзя.
- После `FINALIZED` запрещены новые решения и повторный processing; payload финальной версии также получает `FINALIZED` и время/пользователя финализации.
- `Confirm` остается единственным способом создать `CONFIRMED_VIOLATION`; `Reject` требует причину или комментарий.
- Real-data smoke с `--verify-protocol` проверяет семь разделов, version increment, stale-finalize 409, финализацию, immutable state, audit и PDF. Отчет теперь возвращает ID/версию/статус фактически итогового протокола.

### Измененные файлы

- `api_service/app/domain/protocol_annex2.py`
- `api_service/app/domain/v3_pipeline.py`
- `api_service/app/api/routes_case10.py`
- `api_service/tests/test_case10_api.py`
- `evaluation/smoke_real_data.py`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

### Что реально проверено

- `unittest discover`: **21 тест, OK**.
- Визуальный PDF QA двух реальных протоколов: **30 + 28 страниц**, все страницы отрисованы; наложений и обрезанного содержимого не обнаружено.
- Контейнеры API/frontend пересобраны из актуального кода; `/health` -> `{"status":"ok"}`.
- Повторный HTTP import: 132 параметра, 203 документа, 10146 страниц, 30318 аннотаций; новых дублей нет.
- Real-data e2e Tyumenskaya/Novoslobodskaya успешно прошел через API:
  - Novoslobodskaya: process `394fa5f5399c465c9fe5a707dc4a3a18`, final protocol id `15`, version `2`, `FINALIZED`;
  - Tyumenskaya: process `6a0434e38852423294568343e560581b`, final protocol id `17`, version `2`, `FINALIZED`;
  - обе submission проходят official JSON Schema;
  - Evidence Viewer отдает оригинальные страницы с bbox;
  - stale finalize и изменения после finalization возвращают HTTP 409;
  - audit содержит user/time/reason/protocol_version.
- Runtime PDF и отчеты сохранены в `evaluation/reports/real_data/`.

### Что сломано / не готово

- Accuracy P0 еще не закрыт: coverage **0**, abstention_rate **1**; Tyumenskaya F1 **0**, Novoslobodskaya F1 **null**.
- Rule packs пока не извлекают реальные значения для контрольных `IOS4-078`, `IOS4-079`, `KR-055`, `KR-058`, `PZ-009`; независимые CANDIDATE/NEGATIVE_VERIFIED не получены.
- Dedicated RabbitMQ consumer/worker отсутствует; HTTP processing публикует события, но выполняется синхронно. Это остается архитектурным разрывом обязательного async-контура, но не должно опережать текущий accuracy P0.
- `git status` / commit недоступны: рабочая директория не является git repository.

### Что делать следующим запуском

1. Реализовать минимальные независимые extractor/rule packs по реальному тексту страниц для `IOS4-078`, `IOS4-079`, `KR-055`, `KR-058`, `PZ-009`. Не читать GOLD/check_id при inference.
2. Возвращать единый observation: value/normalized_value/confidence/file/page/bbox/extractor.
3. Сравнивать только доказанные сопоставимые стадии; иначе оставлять `NOT_COMPARABLE` / `CLARIFICATION_REQUIRED`, не создавать ложное нарушение.
4. Перезапустить real-data smoke и зафиксировать coverage, precision/recall/F1/FPR и localization. Только после этого решать вопрос dedicated RabbitMQ worker.

## 20. Независимые rule packs PZ/KR на оригинальных PDF, 16.09.2026

### Что сделано

- Добавлен `official_rule_packs.py` с единым observation-контрактом: value, normalized_value, confidence, file/document, page, normalized bbox, pdf bbox, page geometry, extractor, context.
- Locator использует только `AUTO_FIELD_CANDIDATE` без `check_id`; значение повторно извлекается из оригинального PDF после SHA-256 проверки. GOLD/evidence overlays в inference не читаются.
- Реализованы консервативные правила:
  - `PZ-009`: абсолютная отметка относительного нуля, decimal normalization;
  - `KR-055`: класс бетона с учетом конкретного конструктивного элемента; trigger только при понижении класса;
  - `KR-058`: толщина фундаментной плиты; trigger только при уменьшении толщины.
- Отфильтрованы опасные ложные совпадения: высотные отметки/годы вместо толщины, бетонная подготовка B10 вместо несущей плиты, оговорка «кроме фундаментной плиты».
- Original PDF page snapshots кэшируются по hash/path/pages; документы не открываются заново для каждого значения.
- Для найденной сопоставимой пары создается location-level EvidenceGroup с EvidenceFragment точного значения. Неоднозначные параметры продолжают возвращать технический abstention.
- Для fragment из `RD_ID_MIXED`, которому rule pack доказательно назначил внутреннюю стадию, exporter сериализует нормализованную стадию вместо недопустимого mixed-кода.

### Измененные файлы

- `api_service/app/domain/official_rule_packs.py`
- `api_service/app/domain/official_evidence.py`
- `api_service/app/domain/dataset_sources.py`
- `api_service/app/domain/v3_pipeline.py`
- `api_service/tests/test_case10_official.py`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

### Что реально проверено

- `unittest discover`: **23 теста, OK**.
- API image пересобран, real-data smoke через контейнерный HTTP API выполнен.
- Последний процесс Novoslobodskaya: `257ade1ea8e840c4a2af31d1f6ce47ca`, protocol `24`, submission schema valid.
- Независимые результаты Novoslobodskaya:
  - `PZ-009`, «Отметка 0.000»: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`;
  - `KR-058`, «Фундаментная плита»: `1000/1200 мм -> 1200/1500 мм`, `NEGATIVE_VERIFIED`;
  - `KR-055`, «Фундаментная плита»: `B40 -> B40`, `NEGATIVE_VERIFIED`;
  - `KR-055`, «Вертикальные конструкции подземной части»: `B60 -> B60`, `NEGATIVE_VERIFIED`.
- Все четыре результата имеют original file/page/bbox; ложных `CANDIDATE` в последнем прогоне нет.
- Coverage Novoslobodskaya: **3/132 = 0.022727**, abstention_rate **0.977273**. F1 остается `null`, потому что в этом объекте нет score-eligible positive gold.
- Последний процесс Tyumenskaya: `d7cd0a3f465c43fc810227e35be6a5ca`, protocol `25`; пока 132 `CLARIFICATION_REQUIRED`, coverage 0, F1 0.

### Что сломано / не готово

- Positive finding e2e еще не закрыт независимо: `IOS4-078` и `IOS4-079` для Tyumenskaya не реализованы.
- Stage `RD_ID_MIXED` для Tyumenskaya нужно разрешать только доказательно по пути/типу листа; нельзя глобально считать весь документ РД.
- Current evaluation localization может не совпасть с GOLD file/page, если независимый extractor нашел эквивалентное значение на другом валидном листе. Это нужно отражать отдельно от value/status accuracy.
- Dedicated RabbitMQ worker по-прежнему отсутствует.

### Что делать следующим запуском

1. Реализовать независимый sheet/room extractor для `IOS4-078` и `IOS4-079` на Tyumenskaya: room/system/equipment/duct dimensions из original PDF, без чтения GOLD/check_id.
2. Доказательно классифицировать страницы `Рабочие чертежи` внутри `RD_ID_MIXED` как РД только для извлеченных наблюдений.
3. Сформировать `CANDIDATE` по location-level сравнению ПД/РД, повторить submission/evaluation и проверить precision/recall/F1/FPR/localization.
4. После первого независимого positive finding проверить полный inspector -> protocol version -> finalize сценарий на новом rule-based процессе.

## 21. Matrix 1.1 aliases, IOS4 positive runtime и metrics 1.1, 16.09.2026

### Что сделано

- Добавлена каноническая версия матрицы `official-132-v1.1`.
- Loader читает XLSX `Матрица_параметров_редакция1.1.xlsx` / `05_Матрица_132_параметра_существенная_редакция.xlsx` через stdlib XLSX parser, без новой runtime-зависимости.
- Проверяется состав матрицы 1.1: 132 строки, коды `M-001...M-132`, приоритеты 106 `HIGH` / 26 `MEDIUM`.
- В `Param` добавлены additive-поля:
  - `matrix_code`;
  - `scoring_code`;
  - `aliases_json`.
- `Param.code` сохранен как runtime/scoring-код `PZ/KR/IOS4/...`, чтобы не ломать gold/evaluation/submission.
- API `/api/case10/matrix` и evidence cards теперь возвращают и UI показывает пару вида `M-079 / IOS4-079`.
- `IOS4-078/079` rule pack доведен до независимого runtime:
  - PD observations извлекаются из original PDF по room/system layout;
  - RD observations извлекаются из original PDF/OCR зоны помещения;
  - `RD_ID_MIXED` разрешается только на уровне observation как `RD`, если страница/файл доказательно являются рабочим чертежом `РД-ОВ1`;
  - остальные mixed cases остаются `CLARIFICATION_REQUIRED`.
- `IOS4-078` сужен после проверки: mixed OV-документы `ОВ2.1` и отопление больше не используются как РД-доказательства для вентиляционного rule pack.
- Evaluation report дополнен метриками листа `МЕТРИКИ`:
  - `ocr_character_accuracy`;
  - `key_field_exact_match`;
  - `document_linking_accuracy`;
  - `source_localization_exact_file_page`;
  - `evidence_localization_accuracy`;
  - `normalized_value_and_status_accuracy`;
  - `finding_precision/recall/f1`;
  - `false_positive_rate`;
  - `object_level_split_valid`;
  - `regression_gate`.
- `SUSPICION` зафиксирован как abstention, а не positive official matrix finding.
- FPR теперь считается по смыслу листа `МЕТРИКИ`: на negative/stale denominator. На positive-only Tyumenskaya FPR возвращается `null`, а лишние candidates учитываются через precision/F1.

### Измененные файлы

- `api_service/app/db/models.py`
- `api_service/app/db/session.py`
- `api_service/app/domain/official_dataset.py`
- `api_service/app/domain/official_rule_packs.py`
- `api_service/app/domain/v3_pipeline.py`
- `api_service/app/api/routes_case10.py`
- `api_service/tests/test_case10_official.py`
- `evaluation/fixtures.py`
- `evaluation/metrics.py`
- `evaluation/smoke_real_data.py`
- `evaluation/README.md`
- `frontend/app.js`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

### Что реально проверено

- Python syntax check измененных Python-файлов: OK.
- `python -m unittest api_service/tests/test_case10_evaluation.py`: **3 теста, OK**.
- API Docker image пересобран.
- Unit tests внутри API image: **26 тестов, OK**.
- `frontend/app.js`: Node syntax check OK.
- API/frontend/RAG/Redis/RabbitMQ подняты и healthy на host ports 8000/3000/8001/6379/5672/15672.
- Полный `docker compose up -d --build ...` уперся в локальный конфликт host-порта Postgres `127.0.0.1:5432`; для текущего CASE10 smoke API работал на SQLite volume без Postgres.
- Повторный HTTP real-data smoke выполнен через `evaluation.smoke_real_data --verify-pages`.
- Import idempotent:
  - matrix version `official-132-v1.1`;
  - matrix params 132;
  - public documents 203;
  - pages 10 146;
  - annotations 30 318;
  - hidden skipped 213.
- Matrix API:
  - `M-078 / IOS4-078`;
  - `M-079 / IOS4-079`;
  - aliases сохранены;
  - source XLSX: `case_data/extracted/02_gold_methodology/tz_substantive_revision_20260817/05_Матрица_132_параметра_существенная_редакция.xlsx`.

### Последние real-data metrics

Novoslobodskaya:

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- coverage: **0.022727**;
- FPR: **0.0**;
- source localization exact file/page: **0.416667**;
- evidence localization accuracy: **0.416667**;
- normalized value/status accuracy: **0.8**;
- object-level split: **true**;
- submission schema valid.

Tyumenskaya:

- statuses: 130 `CLARIFICATION_REQUIRED`, 15 `CANDIDATE`;
- independent positive e2e есть по `IOS4-078/079`;
- coverage: **0.015152**;
- precision: **0.333333**;
- recall: **0.833333**;
- F1: **0.476190**;
- FPR: **null** because public Tyumenskaya gold has no negative/stale denominator;
- source localization exact file/page: **0.833333**;
- evidence localization accuracy: **0.833333**;
- normalized value/status accuracy: **0.416667**;
- object-level split: **true**;
- submission schema valid.

Отчеты обновлены в `evaluation/reports/real_data/`.

### Что сломано / не готово

- Accuracy gates из листа `МЕТРИКИ` еще не пройдены: Tyumenskaya precision/F1 и localization ниже целевых порогов, хотя recall уже выше 0.80.
- `IOS4-078` intentionally conservative-but-not-perfect: есть независимые positives, но остаются extra candidates, которые бьют по precision. Дальше нужен более сильный room/system linker, а не GOLD replay.
- OCR/key fields/document linkage метрики остаются `null` там, где public fixture не содержит соответствующий gold denominator.
- Полный compose stack с Postgres требует свободного или переопределенного `POSTGRES_HOST_PORT`; текущая проверка CASE10 API выполнена без Postgres.
- Dedicated RabbitMQ consumer/worker по-прежнему не реализован.

### Что делать следующим запуском

1. Улучшить `IOS4-078` precision без GOLD/check_id: связать room -> system по графу воздуховодов/выносок и отсеивать соседние помещения, которые попали из broad layout radius.
2. Добавить compact dashboard/readout для `quality_gates` из metrics JSON в UI.
3. Переопределить `POSTGRES_HOST_PORT=15432` или освободить 5432 и повторить full compose smoke.
4. После стабилизации precision переходить к inline reject/clarify UI или Node/React compliance layer.

## 22. Tyumenskaya IOS4 precision pass без GOLD replay, 17.09.2026

### Что сделано

- Разбор 15 прежних Tyumenskaya `CANDIDATE` использован только как post-evaluation диагностика, не как inference input:
  - TP: `IOS4-078` помещения `140/142/147/198`, `IOS4-079` помещение `012`;
  - FN: `314`;
  - FP: `141/143/146/150/197/338/339/340/341`, `006`.
- Для `IOS4-078` заменена прежняя логика `expected != actual`:
  - `extra systems` больше не являются самостоятельным trigger;
  - слабый одиночный соседний/OCR-токен не создает positive;
  - violation создается только при доказанном отсутствии вытяжки или существенной потере expected systems.
- Для помещения `314` добавлен узкий same-row fallback на ПД-листе: он берет только реальные `V2.x` токены из original PDF text layer рядом с room `314`, без GOLD/check_id.
- Добавлено подавление дублей на уровне PDF-derived observations: один атомарный room/system finding оставляет одну RD observation и одну `EvidenceGroup`.
- Для `IOS4-079` dimension-only RD evidence стало conservative: слабая одиночная размерность, как у `006`, уходит в non-triggering comparison, а плотный набор размеров для `012` остается positive.
- В `dataset_sources.py` добавлен fallback OCR через временный PNG/outputbase для старого локального Tesseract 3.x. Быстрый stdin/stdout путь для современного Tesseract сохраняется.

### Измененные файлы

- `api_service/app/domain/official_rule_packs.py`
- `api_service/app/domain/dataset_sources.py`
- `api_service/tests/test_case10_official.py`
- `evaluation/reports/real_data/*`
- `CASE10_IMPLEMENTATION_PROGRESS.md`

### Что реально проверено

- Python syntax check измененных Python-файлов: OK.
- Unit tests:
  - `api_service.tests.test_case10_official`;
  - `api_service.tests.test_case10_evaluation`;
  - всего **16 тестов, OK**.
- Docker image rebuild попытались выполнить, но Docker Desktop daemon не отвечал:
  - `dockerDesktopLinuxEngine` сначала отсутствовал;
  - после запуска Docker Desktop pipe появился, но `docker version/build` зависали без ответа;
  - API container на `127.0.0.1:8000` не был доступен.
- Чтобы не блокировать CASE10, выполнен HTTP smoke на локальном FastAPI runtime из текущего source tree, с SQLite data dir в `tmp/case10_local_api_data_v2`.
- Real-data smoke/evaluation через HTTP API выполнен для Novoslobodskaya и Tyumenskaya, submission schema validation прошел для обоих объектов.
- Успешные отчеты перенесены в `evaluation/reports/real_data/`.

### Метрики до / после

Tyumenskaya было:

- statuses: 130 `CLARIFICATION_REQUIRED`, 15 `CANDIDATE`;
- precision **0.333333**;
- recall **0.833333**;
- F1 **0.476190**;
- localization exact file/page **0.833333**;
- normalized value/status accuracy **0.416667**.

Tyumenskaya стало:

- statuses: 130 `CLARIFICATION_REQUIRED`, 6 `CANDIDATE`, 1 `NEGATIVE_VERIFIED`;
- candidates: `IOS4-079/012`, `IOS4-078/140`, `142`, `147`, `198`, `314`;
- `IOS4-079/006` стал `NEGATIVE_VERIFIED`, а не FP;
- precision **1.0**;
- recall **1.0**;
- F1 **1.0**;
- source localization exact file/page **0.916667**;
- evidence localization accuracy **0.916667**;
- normalized value/status accuracy **0.5**;
- coverage **0.015152**;
- abstention_rate **0.984848**;
- FPR **null** because public Tyumenskaya has no negative/stale denominator.

Novoslobodskaya:

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- negative controls сохранены:
  - `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`;
  - `KR-058`: `1000/1200 мм -> 1200/1500 мм`, `NEGATIVE_VERIFIED`;
  - `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`;
  - `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`;
- FPR **0.0**;
- coverage **0.022727**;
- abstention_rate **0.977273**;
- submission schema valid.

### Что осталось ниже acceptance gates

- Tyumenskaya precision/recall/F1 gates теперь пройдены: `1.0 / 1.0 / 1.0`.
- Tyumenskaya localization gate **не пройден**: **0.916667 < 0.95**.
  - Причина честная, без GOLD подгонки: `314` найден по реальному source PDF на `F0201` page **20**, тогда как public gold evidence для этого check указывает `F0201` page **18**.
  - Поэтому finding/value/status засчитывается по `object_id + parameter_code + location`, но exact file/page localization теряет 1 RD evidence item из 12.
- Tyumenskaya normalized value/status accuracy **0.5 < 0.9**: public fixture содержит статус/value-level labels, а independent extractor возвращает реальные normalized system observations; это нужно дальше разделять на semantic-status accuracy и extracted-value exact match.
- Novoslobodskaya localization/value-status gates остаются ниже порога, как и раньше, но FPR gate сохранен: **0.0 <= 0.10**.
- OCR/key fields/document linkage metrics остаются `null`, потому что public fixture не содержит denominator для этих листов `МЕТРИКИ`.

### Что делать следующим запуском

1. Улучшить localization для `314` без подгонки под GOLD: доказательно объяснить page 20 vs public page 18 или найти эквивалентный page 18 source observation только из original PDF.
2. Разделить status/value metric на semantic finding correctness и exact normalized extracted values, чтобы real-source values не штрафовались как GOLD replay mismatch.
3. Повторить Docker image rebuild/full container smoke, когда Docker Desktop daemon снова отвечает; Postgres port `127.0.0.1:5432` не трогать, пока SQLite CASE10 smoke проходит.

## 23. Docker API image rebuild и SQLite container smoke, 17.09.2026

### Что изменено относительно checkpoint 22

- Docker Desktop daemon восстановлен и отвечает.
- API image `lct-hack-2026-api:latest` пересобран из текущего source tree.
- Запущен временный standalone API container `case10-api-smoke` на `127.0.0.1:8010`:
  - только API image;
  - SQLite data volume;
  - `case_data` и `learning_data` read-only mounts;
  - без full compose stack, без Postgres, без IFC/CV/React/Node/RabbitMQ работ.
- Real-data smoke/evaluation повторен уже против Docker API image.
- Docker-run отчеты перенесены в `evaluation/reports/real_data/`.

### Что реально проверено

- `docker version`: Docker Desktop **4.47.0**, Engine **28.4.0**, OK.
- `docker compose build api`: **OK**, image `lct-hack-2026-api:latest` rebuilt.
- Standalone API container health: `{"status":"ok"}`.
- HTTP real-data smoke через `evaluation.smoke_real_data --verify-pages`:
  - import idempotent / fresh SQLite:
    - matrix params 132;
    - public documents 203;
    - pages 10 146;
    - annotations 30 318;
    - hidden skipped 213;
  - submission schema validation: **OK** для обоих объектов;
  - evidence page PNG verification: **OK**.
- Временный container `case10-api-smoke` остановлен после проверки.

### Метрики Docker-smoke

Tyumenskaya:

- statuses: 130 `CLARIFICATION_REQUIRED`, 6 `CANDIDATE`, 4 `NEGATIVE_VERIFIED`;
- candidates:
  - `IOS4-079/012`;
  - `IOS4-078/140`, `142`, `147`, `198`, `314`;
- non-triggering negatives:
  - `IOS4-079/006`;
  - `IOS4-078/141`, `146`, `150`;
- precision **1.0**;
- recall **1.0**;
- F1 **1.0**;
- source localization exact file/page **0.916667**;
- evidence localization accuracy **0.916667**;
- normalized value/status accuracy **0.5**;
- coverage **0.015152**;
- abstention_rate **0.984848**;
- FPR **null** because public Tyumenskaya has no negative/stale denominator;
- object-level split valid: **true**.

Novoslobodskaya:

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- negative controls сохранены:
  - `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`;
  - `KR-058`: `1000/1200 мм -> 1200/1500 мм`, `NEGATIVE_VERIFIED`;
  - `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`;
  - `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`;
- FPR **0.0**;
- coverage **0.022727**;
- abstention_rate **0.977273**;
- object-level split valid: **true**.

### Gates

- Tyumenskaya precision gate: **passed**, `1.0 >= 0.90`.
- Tyumenskaya recall gate: **passed**, `1.0 >= 0.80`.
- Tyumenskaya F1 gate: **passed**, `1.0 >= 0.85`.
- Tyumenskaya localization gate: **not passed**, `0.916667 < 0.95`.
  - Открытая причина не изменилась: `314` найден по original PDF на `F0201` page **20**, public gold localization ожидает `F0201` page **18**.
  - Подгонки под GOLD нет; value/status засчитывается по `object_id + parameter_code + location`, exact localization остается отдельной метрикой.
- Novoslobodskaya FPR gate: **passed**, `0.0 <= 0.10`.
- Tyumenskaya normalized value/status accuracy остается ниже gate: **0.5 < 0.9**.
  - Причина: public fixture содержит coarse label/status expectations, independent extractor возвращает реальные normalized system observations.
- OCR/key fields/document linkage остаются `null` из-за отсутствия denominator в public fixture.

### Что осталось честно открытым

1. Закрыть localization gap для `314` без GOLD replay: либо доказать корректность page 20 как эквивалентного real-source observation, либо найти source observation на page 18 только из original PDF.
2. Разделить value/status evaluation на semantic finding correctness и exact extracted-value match, чтобы реальные normalized observations не штрафовались как несоответствие coarse public labels.
3. Не трогать Postgres port `127.0.0.1:5432`, пока текущий CASE10 SQLite smoke проходит.

## 24. Value/status normalization и проверка localization для `314`, 17.09.2026

### Что изменено относительно checkpoint 23

- `evaluation/fixtures.py`:
  - public gold `comparison_result` теперь нормализуется в стабильный runtime-compatible результат:
    - `VIOLATION_PRESENT`, `CONFIGURATION_MISMATCH`, `MISSING_DESIGN_ELEMENT` -> `TRIGGERED`;
    - `NO_VIOLATION`, `MATCH` -> `NON_TRIGGERING`.
- `evaluation/exporter.py`:
  - evaluation predictions теперь экспортируют `comparison_result`;
  - для rule-pack EvidenceGroup экспортируются нормализованные `expected_value`, `actual_value`, `normalized_value` из `delta.values`, например `PD:V2.12,V2.13 | RD:MISSING_EXHAUST`.
- `evaluation/metrics.py`:
  - value/status сравнение теперь использует канонические смысловые статусы, а не raw UI labels:
    - `CANDIDATE` и `CONFIRMED_VIOLATION` сравниваются как `VIOLATION_PRESENT`;
    - `NEGATIVE_VERIFIED` и `NO_VIOLATION` сравниваются как `NO_VIOLATION`;
    - `comparison_result` сравнивается как `TRIGGERED` / `NON_TRIGGERING`.
- `api_service/tests/test_case10_evaluation.py`, `api_service/tests/test_case10_official.py`:
  - добавлены тесты на canonical runtime-result и экспорт normalized rule values.

### Разбор оставшихся Tyumenskaya miss/mismatch

- Единственный exact localization miss остался прежним:
  - `TRAIN-0010`;
  - `IOS4-078`;
  - помещение `314`;
  - public gold evidence: `F0201`, `RD`, page **18**;
  - independent runtime evidence: `F0201`, `RD`, page **20**.
- Остальные 5 positive findings (`IOS4-079/012`, `IOS4-078/140`, `142`, `147`, `198`) совпадают с public gold по exact file/page.
- Value/status mismatch в checkpoint 23 был не ошибкой inference:
  - runtime честно оставлял finding как `CANDIDATE`;
  - public fixture ожидал `CONFIRMED_VIOLATION`;
  - оба статуса означают positive violation для evaluation, а `comparison_result=TRIGGERED` теперь экспортируется и сравнивается напрямую.

### Проверка `F0201` page 18 без GOLD/check_id

- Проверка выполнена по original PDF через manifest path + SHA для `F0201`.
- Text layer на `F0201` page **18**:
  - token `314`: **не найден**;
  - drawing-room filter: **нет** помещения `314`;
  - из 300-серии на странице видны только `300` и `375`.
- OCR page **18**:
  - full-page OCR: `314` **не найден**;
  - quadrant OCR (`upper_left`, `upper_right`, `lower_left`, `lower_right`): `314` **не найден**.
- Page **20**:
  - text layer содержит `314`;
  - drawing-room filter содержит `314`;
  - runtime evidence для `314` поэтому остается на реальной page **20**.
- Вывод: нейтрального признака, который честно выбирает page **18**, не найдено. Подгонки под public gold не сделано; exact localization gap для `314` оставлен как residual risk.

### Что проверено

- Python syntax check:
  - `evaluation/fixtures.py`;
  - `evaluation/exporter.py`;
  - `evaluation/metrics.py`;
  - `api_service/tests/test_case10_official.py`;
  - `api_service/tests/test_case10_evaluation.py`.
- Unit tests:
  - `api_service.tests.test_case10_official`;
  - `api_service.tests.test_case10_evaluation`;
  - результат: **18/18 OK**.
- Docker:
  - Docker Desktop **4.47.0**, Engine **28.4.0**;
  - `docker compose build api`: **OK**, image `lct-hack-2026-api:latest` rebuilt.
- Standalone SQLite API smoke:
  - container `case10-api-smoke-v24`;
  - port `127.0.0.1:8011`;
  - volume `case10_api_smoke_data_v24:/data`;
  - `case_data` и `learning_data` mounted read-only;
  - Postgres/compose stack/IFC/CV/React/Node/RabbitMQ не трогались.
- HTTP real-data smoke через `evaluation.smoke_real_data --verify-pages`:
  - import: 132 matrix params, 203 public documents, 10 146 pages, 30 318 annotations;
  - hidden skipped: 213;
  - submission schema validation: **OK** для Tyumenskaya и Novoslobodskaya;
  - evidence PNG verification: **OK**.
- Успешные Docker-run reports перенесены в `evaluation/reports/real_data/`.

### Метрики до/после

Tyumenskaya, до checkpoint 24:

- precision / recall / F1: **1.0 / 1.0 / 1.0**;
- source localization exact file/page: **0.916667**;
- evidence localization accuracy: **0.916667**;
- normalized value/status accuracy: **0.5**;
- coverage: **0.015152**;
- abstention_rate: **0.984848**.

Tyumenskaya, после checkpoint 24:

- statuses: 130 `CLARIFICATION_REQUIRED`, 6 `CANDIDATE`, 4 `NEGATIVE_VERIFIED`;
- candidates:
  - `IOS4-079/012`;
  - `IOS4-078/140`, `142`, `147`, `198`, `314`;
- precision: **1.0**;
- recall: **1.0**;
- F1: **1.0**;
- source localization exact file/page: **0.916667**;
- evidence localization accuracy: **0.916667**;
- normalized value/status accuracy: **1.0**;
- protocol/status accuracy: **1.0**;
- status/value comparable items: **18**;
- coverage: **0.015152**;
- abstention_rate: **0.984848**;
- FPR: **null**, потому что public Tyumenskaya fixture не содержит negative denominator;
- object-level split valid: **true**.

Novoslobodskaya, после checkpoint 24:

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- negative controls сохранены:
  - `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-058`: `1000/1200 -> 1200/1500`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
- FPR: **0.0**;
- coverage: **0.022727**;
- abstention_rate: **0.977273**;
- normalized value/status accuracy: **0.8**;
- source localization exact file/page: **0.416667**;
- object-level split valid: **true**.

### Gates

- Tyumenskaya precision gate: **passed**, `1.0 >= 0.90`.
- Tyumenskaya recall gate: **passed**, `1.0 >= 0.80`.
- Tyumenskaya F1 gate: **passed**, `1.0 >= 0.85`.
- Tyumenskaya value/status gate: **passed**, `1.0 >= 0.90`.
- Tyumenskaya localization gate: **not passed**, `0.916667 < 0.95`.
  - Причина: `IOS4-078/314` независимо найден на реальной `F0201` page **20**, а public gold exact localization ожидает page **18**.
  - Page **18** проверена по text layer, layout room tokens и OCR; честного `314` там не найдено.
- Novoslobodskaya FPR gate: **passed**, `0.0 <= 0.10`.
- OCR/key fields/document linkage gates остаются `null`, потому что public fixture не содержит denominator для этих листов `МЕТРИКИ`.

### Что осталось честно открытым

1. Tyumenskaya exact localization остается ниже gate только из-за `314` page 20 vs public page 18. Текущий вывод: это residual risk public localization mismatch, а не regression inference.
2. Novoslobodskaya localization/value-status остаются ниже gate, но текущий P0 negative-control критерий сохранен: `PZ-009/KR-055/KR-058` не стали false positive, FPR **0.0**.
3. Следующие улучшения нужно делать без GOLD replay: либо расширять независимый evidence audit по исходным PDF, либо добавлять отдельную метрику equivalent-real-source localization, не заменяя exact public file/page.

## 25. Final P0 / demo readiness baseline, 17.09.2026

### Цель checkpoint

Зафиксировать текущий CASE10 P0 inference как demo baseline и не ломать:

- Tyumenskaya `IOS4-078/079`;
- Novoslobodskaya `PZ-009`;
- Novoslobodskaya `KR-055`;
- Novoslobodskaya `KR-058`;
- submission validation;
- Evidence Viewer/page PNG;
- Annex 2 protocol;
- evaluation metrics по листу `МЕТРИКИ`.

Подгонки `IOS4-078/314` под public gold page **18** не делалось. Residual mismatch `F0201` page **20** vs gold page **18** остается честным риском.

### Что добавлено

- `CASE10_FINAL_READINESS_REPORT.md`
  - P0 readiness summary;
  - закрытые части ТЗ;
  - gates;
  - причины residual localization gap;
  - demo scenarios;
  - pitch risks.
- `CASE10_DEMO_SCRIPT_5_7_MIN.md`
  - пошаговый demo script на 5-7 минут;
  - matrix 1.1;
  - Tyumenskaya positive path;
  - Novoslobodskaya negative controls;
  - Evidence Viewer;
  - inspector decision;
  - Annex 2;
  - metrics.
- `evaluation/reports/real_data/key_evidence_pages/`
  - PNG страницы с выделением для ключевых demo findings.

### Regression smoke

- Unit tests:
  - `api_service.tests.test_case10_api`;
  - `api_service.tests.test_case10_domain`;
  - `api_service.tests.test_case10_evaluation`;
  - `api_service.tests.test_case10_official`;
  - результат: **29/29 OK**.
- API image:
  - `docker compose build api`: **OK**;
  - image `lct-hack-2026-api:latest` rebuilt.
- Standalone SQLite HTTP smoke:
  - container `case10-api-smoke-v25`;
  - port `127.0.0.1:8012`;
  - volume `case10_api_smoke_data_v25:/data`;
  - `case_data` и `learning_data` mounted read-only;
  - Postgres/compose stack/IFC/CV/React/Node/RabbitMQ не трогались.
- HTTP smoke command:
  - `evaluation.smoke_real_data --verify-pages --verify-protocol`;
  - import: 132 matrix params, 203 public documents, 10 146 pages, 30 318 annotations;
  - hidden skipped: 213;
  - submission schema validation: **OK** для обоих объектов;
  - evidence page PNG verification: **OK**;
  - inspector decision endpoint: **OK**;
  - stale finalize rejection: **OK**;
  - finalized protocol: **OK**;
  - protocol history/audit: **OK**;
  - Annex 2 sections: **7/7**;
  - protocol PDF export: **OK**.

### Evidence Viewer проверка ключевых findings

Через `/case10/evidence-fragments/{id}/page.png` проверены и сохранены PNG:

- Tyumenskaya:
  - `IOS4-079/012`, `F0201`, page **17**;
  - `IOS4-078/140`, `F0201`, page **18**;
  - `IOS4-078/142`, `F0201`, page **18**;
  - `IOS4-078/147`, `F0201`, page **18**;
  - `IOS4-078/198`, `F0201`, page **18**;
  - `IOS4-078/314`, `F0201`, page **20**.
- Novoslobodskaya:
  - `PZ-009`, `F0137`, page **1**;
  - `KR-058`, `F0140`, page **3**;
  - `KR-055`, `F0140`, pages **3** and **7**.

### UI / reports readiness

- UI matrix table shows `matrix_code / scoring_code`, aliases and matrix row.
- EvidenceGroup UI shows:
  - `M-xxx / IOS4/KR/PZ` label;
  - `CANDIDATE`, `NEGATIVE_VERIFIED`, `CLARIFICATION_REQUIRED`;
  - `file_id`, stage, page;
  - `bbox_normalized`;
  - `bbox_pdf`;
  - button to open highlighted evidence page.
- Reports show:
  - `matrix.json`: `official-132-v1.1`, 132 params, `matrix_code`, `scoring_code`, aliases;
  - `groups.json`: statuses, parameters, file/page/bbox evidence;
  - `summary.json`: metrics summary;
  - `metrics.json`: лист `МЕТРИКИ` fields;
  - `submission.json`: schema validation result.

### Финальные метрики

Tyumenskaya:

- statuses: 130 `CLARIFICATION_REQUIRED`, 6 `CANDIDATE`, 4 `NEGATIVE_VERIFIED`;
- precision: **1.0**;
- recall: **1.0**;
- F1: **1.0**;
- source localization exact file/page: **0.916667**;
- evidence localization accuracy: **0.916667**;
- normalized value/status accuracy: **1.0**;
- coverage: **0.015152**;
- abstention_rate: **0.984848**;
- submission schema validation: **OK**;
- protocol status: **FINALIZED**.

Novoslobodskaya:

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- negative controls сохранены:
  - `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-058`: `1000/1200 -> 1200/1500`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
- FPR: **0.0**;
- coverage: **0.022727**;
- abstention_rate: **0.977273**;
- normalized value/status accuracy: **0.8**;
- source localization exact file/page: **0.416667**;
- submission schema validation: **OK**;
- protocol status: **FINALIZED**.

### Gates

Passed:

- Tyumenskaya precision: **1.0 >= 0.90**.
- Tyumenskaya recall: **1.0 >= 0.80**.
- Tyumenskaya F1: **1.0 >= 0.85**.
- Tyumenskaya value/status: **1.0 >= 0.90**.
- Novoslobodskaya FPR: **0.0 <= 0.10**.
- Object-level split: **valid**.
- Submission schema validation: **OK** для обоих объектов.

Below / not applicable:

- Tyumenskaya localization: **0.916667 < 0.95**.
  - Единственная причина: `IOS4-078/314` real source `F0201` page **20**, public gold page **18**.
  - Page **18** ранее проверена по text layer, layout room tokens и OCR; независимого `314` там не найдено.
- OCR accuracy, key fields exact match, document linkage accuracy: `null`, потому что public fixture не содержит denominator.
- Tyumenskaya FPR: `null`, потому что public Tyumenskaya fixture не содержит negative denominator.

### Final artifacts

- `CASE10_FINAL_READINESS_REPORT.md`
- `CASE10_DEMO_SCRIPT_5_7_MIN.md`
- `evaluation/reports/real_data/summary.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.metrics.json`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.metrics.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.submission.json`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.submission.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.protocol.pdf`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.protocol.pdf`
- `evaluation/reports/real_data/key_evidence_pages/`

### Что осталось вне P0

1. Не закрывать exact localization gate искусственно для `IOS4-078/314`; нужен либо независимый equivalent-source metric, либо дополнительный source audit, но не GOLD replay.
2. Расширение coverage по всем 132 параметрам beyond current official rule packs.
3. OCR/key-field/document-link denominators для полноценного подсчета всех метрик листа `МЕТРИКИ`.
4. IFC/CV/React/Node/RabbitMQ/Postgres production hardening не входит в текущий стабилизированный P0 demo baseline.

## 26. Minimal Node API Gateway + OpenAPI, 17.09.2026

### Цель checkpoint

Закрыть следующий technical gap по ТЗ: внешний CASE10 API contract через минимальный Node.js gateway поверх уже работающего Python API. Новые readiness reports, demo scripts, summary documents и повторный аудит не создавались.

### Что реализовано

- Добавлен сервис `node_gateway/` без npm-зависимостей:
  - `GET /health`;
  - `GET /openapi.json`;
  - allowlist proxy ключевых CASE10 endpoints в Python API;
  - forwarding `Authorization`, query string, body и бинарных ответов PNG/PDF;
  - HTTP errors Python API прокидываются наружу с исходным status/body.
- Добавлено OpenAPI 3.0 описание gateway contract:
  - matrix;
  - official dataset import;
  - processes/status/run;
  - evidence groups/decisions;
  - evidence fragments page PNG;
  - protocol current/json/history/audit/pdf/finalize;
  - submission;
  - evaluation predictions;
  - auth/projects как минимальные helper endpoints для smoke/client setup.
- `docker-compose.yml`:
  - новый service `node_gateway`;
  - порт: `${GATEWAY_HOST_PORT:-8080}:8080`;
  - upstream: `PYTHON_API_BASE_URL=http://api:8000`;
  - healthcheck на `/health`;
  - Python API на `8000` не изменен.
- `.env.example`:
  - добавлен `GATEWAY_HOST_PORT=8080`.
- `README.md` / `DEPLOYMENT.md`:
  - зафиксировано, что внешний CASE10 contract идет через Node gateway;
  - Python остается check/extraction/evidence engine и совместимым API для существующего frontend.
- Добавлен `evaluation/smoke_node_gateway.js`:
  - health OK;
  - OpenAPI доступен;
  - official import проходит;
  - matrix возвращает 132 параметра;
  - process/status/evidence groups/protocol/submission/evaluation predictions проксируются;
  - Python 404 прокидывается как HTTP 404 с body.

### Проверенные endpoints через gateway

- `GET /health`
- `GET /openapi.json`
- `POST /api/auth/login`
- `GET /api/projects`
- `POST /api/projects`
- `POST /api/case10/projects/{project_id}/official-dataset/import`
- `GET /api/case10/matrix?project_id=...`
- `POST /api/case10/projects/{project_id}/processes`
- `GET /api/case10/processes/{process_id}/status`
- `GET /api/case10/evidence-groups?project_id=...&process_id=...`
- `GET /api/case10/protocols/current?project_id=...&process_id=...`
- `GET /api/case10/protocols/{protocol_id}/json`
- `GET /api/case10/protocols/{protocol_id}/evaluation-predictions`
- `GET /api/case10/protocols/{protocol_id}/submission`
- negative path: `GET /api/case10/processes/not-a-real-process/status` => `404`, `Inspection process not found`

### Проверки

- Node runtime:
  - `node --version`: `v22.22.2`;
  - `npm --version`: `10.9.7`.
- Syntax / config:
  - `node --check node_gateway/server.js`: **OK**;
  - `node --check evaluation/smoke_node_gateway.js`: **OK**;
  - `node -e "JSON.parse(...openapi.json...)"`: **OK**;
  - `docker compose config --quiet`: **OK**.
- Docker:
  - `docker compose build node_gateway`: **OK**;
  - image `lct-hack-2026-node_gateway:latest` built.
- Existing CASE10 unit tests:
  - host Python отсутствует, локальный `venv` ссылается на отсутствующий `Python312`;
  - прогон выполнен внутри API Docker runtime с примонтированными tests;
  - `api_service.tests.test_case10_api`;
  - `api_service.tests.test_case10_domain`;
  - `api_service.tests.test_case10_evaluation`;
  - `api_service.tests.test_case10_official`;
  - результат: **29/29 OK**.
- Gateway smoke:
  - временный standalone API container `case10-api-gateway-smoke`;
  - Python API port: `127.0.0.1:8014`;
  - временный Node gateway port: `127.0.0.1:8090`;
  - project: `CASE10 gateway smoke cp26`;
  - object: `OBJ-NOVOSLOBODSKAYA`;
  - process: `READY`;
  - matrix params: **132**;
  - evidence groups: **133**;
  - submission checks: **133**;
  - validation errors: **0**;
  - forwarded error: **404 / Inspection process not found**.
- Existing real-data smoke:
  - полный `evaluation.smoke_real_data` не перезапускался, потому что Python routing/API behavior не менялись;
  - gateway smoke использовал official import + process + protocol/submission/evaluation через новый gateway и не создавал новых report artifacts.

### Какие требования ТЗ теперь закрыты

- Есть Node.js gateway как внешний CASE10 API слой.
- Есть OpenAPI 3.0 contract для ключевых CASE10 endpoints.
- Python API остается внутренним engine для checks/extraction/evidence/protocol.
- Matrix official 132 доступна через gateway.
- Protocol/submission/evaluation predictions доступны через gateway.
- Evidence page PNG/PDF/binary proxy path поддержан.
- Python HTTP errors не маскируются gateway и доходят до клиента как HTTP errors.

### Что осталось открытым

1. Production reverse-proxy routing домена можно отдельно перевести на `node_gateway` для CASE10 paths; в этом шаге изменен базовый compose и документация, без трогания production nginx routing.
2. Расширение OpenAPI schemas до строгих object-level моделей можно сделать отдельным шагом; сейчас contract минимальный и совместим с текущими Python payloads.
3. Ранее открытые P0 gaps остаются без изменений: `IOS4-078/314` localization mismatch, IFC/CV, React rewrite, Postgres port и RabbitMQ worker не трогались.

## 27. Metrics, hidden-readiness and managed retraining contour, 17.09.2026

### Цель checkpoint

Закрыть оставшиеся обязательные разрывы ТЗ по acceptance metrics, blind hidden-readiness и управляемому дообучению как контуру. Внешняя LLM не внедрялась, обучение модели автоматически не запускалось, hidden labels не использовались.

### Что реализовано

- Evaluation metrics:
  - добавлен denominator-aware public fixture для OCR/key fields/document linkage/localization;
  - OCR и key fields остаются `null`, но теперь с явными machine-readable причинами отсутствия denominator;
  - document linkage считается из public-safe `public_gold_checks.jsonl` evidence `file_id`;
  - localization completeness считается из public evidence `file_id/page`;
  - exported predictions теперь содержат `document_links`, выровненные с gold через тот же id-map, что findings/evidence.
- Hidden blind smoke:
  - добавлен `evaluation/smoke_hidden_blind.py`;
  - hidden object `OBJ-RECHNIKOV-7-7` импортируется без `hidden_gold_checks_organizer_only`;
  - запускается inference, protocol/submission schema validation и counts по статусам;
  - F1 по hidden labels не считается.
- Leakage / overfit checks:
  - тестами проверено, что runtime process не вызывает `_create_gold_fixture_groups`;
  - hidden organizer-only labels блокируются для training;
  - runtime rule packs не содержат object/page-specific hardcode для `OBJ-TYUMENSKAYA`, `OBJ-NOVOSLOBODSKAYA`, `OBJ-RECHNIKOV`.
- Managed retraining contour:
  - добавлен доменный модуль `api_service/app/domain/training_release.py`;
  - training release строится только из `CONFIRMED_VIOLATION` и `NEGATIVE_VERIFIED`;
  - обязательны evidence cards с `file_id/page/bbox`;
  - object-level train/validation/test split с split hashes;
  - hidden labels и `CANDIDATE`, `SUSPICION`, `MISSING_EVIDENCE`, `CLARIFICATION_REQUIRED`, `NOT_COMPARABLE` исключаются;
  - при недостатке данных возвращается blocked/gate status, без обучения и без publish.
- ML retraining / model registry:
  - добавлены модели `MLRetrainingLog` и `ModelVersionRegistry`;
  - лог хранит `dataset_version`, `split_hashes`, metrics, per-category metrics, `approval_status`, `approved_by`, rollback/gate data;
  - no auto-publish enforced: новый контур создает release/log, но не обучает и не публикует модель.
- API / gateway:
  - добавлены Python endpoints:
    - `POST /api/case10/projects/{project_id}/training-release`;
    - `GET /api/case10/projects/{project_id}/ml-retraining-log`;
  - Node gateway allowlist и OpenAPI обновлены для этих endpoints.
- Performance smoke:
  - добавлен `evaluation/performance_smoke.py` для process/protocol/submission/API p95 лимитов.

### Acceptance metrics status

Tyumenskaya public smoke:

- precision / recall / F1: **1.0 / 1.0 / 1.0**;
- FPR: `null`, потому что public Tyumenskaya denominator содержит только positive findings;
- document linkage accuracy: **1.0**;
- localization completeness: **0.916667**;
- object-level split: **valid**.

Novoslobodskaya public smoke:

- precision / recall / F1: `null`, потому что public Novoslobodskaya denominator negative-only;
- FPR: **0.0**;
- document linkage accuracy: **0.5**;
- localization completeness: **0.416667**;
- object-level split: **valid**.

Still null with explicit reasons:

- OCR character accuracy: `null`, потому что public/train-safe data содержит `page_index.text_source/needs_ocr`, но не содержит human OCR reference text denominator.
- Key fields exact match: `null`, потому что public `DOCUMENT_FIELD` annotations являются machine-assisted candidates, не final human key-field gold.
- Regression gate: `null`, потому что baseline/model registry production history еще не накоплена.

### Hidden blind smoke

Object: `OBJ-RECHNIKOV-7-7`.

- hidden documents imported without hidden labels;
- gold checks created/updated: **0 / 0**;
- process seconds: **56.64**;
- groups: **133**;
- submission checks: **133**;
- submission schema validation: **OK**;
- hidden F1 evaluated: **false**;
- hidden labels used: **false**.

Status counts:

- `CANDIDATE`: **1**;
- `NEGATIVE_VERIFIED`: **2**;
- `MISSING_EVIDENCE`: **2**;
- `NOT_COMPARABLE`: **128**;
- `CLARIFICATION_REQUIRED`: **0**;
- `SUSPICION`: **0**.

Overdetect guard:

- positive-like count (`CANDIDATE` + `SUSPICION`): **1**;
- candidate rate: **0.0075**;
- no candidate explosion detected.

### Managed retraining readiness

- Training release endpoint проверен на public smoke project.
- Result: **BLOCKED**, как ожидается при недостатке approved inspector data.
- Release items: **0**.
- Log count: **1**.
- `approval_status`: **BLOCKED**.
- `no_auto_publish`: **true**.
- Блокировка не является ошибкой: это требуемое поведение, пока нет достаточного набора `CONFIRMED_VIOLATION` / `NEGATIVE_VERIFIED` с complete evidence cards и object-level split.

### Проверки

- Python syntax check changed modules: **OK**.
- Node syntax / OpenAPI JSON parse:
  - `node --check node_gateway/server.js`: **OK**;
  - `openapi.json` parse: **OK**.
- CASE10 unit / leakage / training tests:
  - `api_service.tests.test_case10_api`;
  - `api_service.tests.test_case10_domain`;
  - `api_service.tests.test_case10_evaluation`;
  - `api_service.tests.test_case10_official`;
  - `api_service.tests.test_case10_training_release`;
  - result: **33/33 OK**.
- Public real-data smoke:
  - Tyumenskaya and Novoslobodskaya completed;
  - protocol/submission/evaluation metrics generated through current API.
- Blind hidden smoke:
  - completed without hidden labels.
- Performance smoke:
  - object: `OBJ-NOVOSLOBODSKAYA`;
  - process: **8.61s** <= 150s;
  - protocol current: **0.15s** <= 40s;
  - submission: **0.09s** <= 40s;
  - status endpoint p95: **0.155s** <= 0.25s;
  - result: **PASS**.

### Runtime note

Docker Desktop is currently blocked on the host by:

`starting services: initializing Inference manager: listening on unix://C:\Users\Ivan\AppData\Local\Docker\run\dockerInference ... The file cannot be accessed by the system`

Because this is a Docker Desktop runtime failure, container build/compose execution was not repeated in checkpoint 27. Verification was run through the local FastAPI runtime using the existing project dependencies.

### Что теперь закрыто по ТЗ

- Mandatory metrics are no longer silent `null`: available denominators are evaluated, missing denominators are explicitly explained.
- Document linkage and localization completeness are wired into the evaluation path.
- Hidden object is covered by blind smoke without label leakage.
- False-positive-like overdetect guard exists for hidden blind inference.
- Managed retraining readiness exists as a gated release/log/registry contour, without auto-training and without auto-publish.
- LLM remains non-mandatory; no required LLM dependency was added.

### Что осталось открытым

1. OCR character accuracy and key fields exact match need a public/train-safe human reference denominator or curated pilot sample; current official public files do not provide it.
2. Localization completeness remains below gate on current public smoke and must not be fixed by public-gold replay or `IOS4-078/314` hardcode.
3. Training release remains blocked until enough approved inspector decisions with complete evidence cards exist.
4. Docker Desktop must be repaired before repeating compose/image validation for checkpoint 27.

### Следующий P0/P1 шаг

P0: obtain or create a public-safe human OCR/key-field/linkage reference denominator and wire it into `evaluation/fixtures.py`, so OCR character accuracy and key fields exact match can be measured instead of reported as blocked.

## 28. Novoslobodskaya localization/value-status/linkage root-cause fixes + status-endpoint performance fix, 17.09.2026

### Цель checkpoint

Продолжение с текущего состояния проекта (без повторного полного аудита). Задача: разобрать и исправить причины провала public quality gates на `OBJ-NOVOSLOBODSKAYA` (localization, value/status, document linkage) и стабилизировать API p95/submission performance. Docker Desktop был перезапущен пользователем в ходе сессии; все изменения проверены через живой Docker API (`lct-hack-2026-api-1`), не только через локальный runtime.

### Диагностика (before touching code)

Свежий live smoke на пересобранном контейнере воспроизвёл ровно то же, что описано в задаче:
`OBJ-NOVOSLOBODSKAYA`: `evidence_localization_accuracy=0.4167 (5/12)`, `document_linking_accuracy=0.5 (6/12)`, `normalized_value_and_status_accuracy=0.8 (12/15)`, `finding_confusion={tp:0,fp:0,tn:5,fn:0}` (то есть detection сам по себе был верным — проблема в evidence/value полях, не в classification).

Разбор по чекам (`public_gold_checks.jsonl`, object `OBJ-NOVOSLOBODSKAYA`, 5 checks: `TRAIN-0011..0015`, параметры `PZ-009`, `KR-055`×3 (разные `location`), `KR-058`) выявил две отдельные причины, обе — в `api_service/app/domain/official_rule_packs.py` / `official_evidence.py`, не object-specific:

1. **Evidence fragment selection picked one arbitrary page per stage.** В `official_evidence.py::_create_rule_groups` для каждого `(location, stage)` бралась ровно одна `RuleObservation` (`sorted(rows, key=(-confidence, document.id, page))[0]`), даже если несколько разных страниц независимо подтверждали то же самое нормализованное значение (что и есть в public gold — напр. `KR-058` RD имеет 2 подтверждающих страницы `F0140:p3` и `F0140:p7`). Все остальные подтверждающие страницы отбрасывались без использования как evidence.
2. **Candidate-page discovery was gated per exact parameter code.** `extract_official_rule_observations` рассматривала как кандидатов только страницы, уже помеченные upstream machine-pre-annotation MATRIX_FIELD-локатором ИМЕННО для этого кода (`PZ-009`/`KR-055`/`KR-058`). Реальный текст (проверено прямым чтением исходных PDF через `fitz` внутри контейнера, debug-скрипт `debug_b25.py`) показал, что "Стена в грунте B25" физически присутствует на `F0105` (PD, страницы 13/14/17/24) и корректно извлекается regex-ом `_extract_concrete_classes`/`_concrete_location`, но RD-evidence для этой локации (`F0137`, документ типа СВГ) никогда не тегировался кодом `KR-055` в upstream pre-annotation — только `PZ-009`. В результате для этой локации группа вообще не создавалась (`by_stage.get("RD")` было пусто), несмотря на корректно найденный PD.

Оба бага — общая domain-логика, не относятся к конкретному объекту/документу/странице; проверено, что тот же паттерн (арбитражный выбор одной страницы, code-gated discovery) применяется одинаково для всех кодов/объектов.

### Исправления

1. `api_service/app/domain/official_evidence.py::_create_rule_groups`: вместо одной `selected[stage]` строки теперь берётся `supporting[stage]` — все `RuleObservation` в том же `(location, stage)`, чьё `normalized_value` совпадает с выбранным (canonical) значением, и на каждую заводится отдельный `EvidenceFragment` с тем же `role`. Значение/violation-решение (`expected`/`actual`/`comparison_result`) не менялись — используется тот же лучший-по-confidence наблюдатель, что и раньше, меняется только *evidence coverage*.
2. `api_service/app/domain/official_rule_packs.py::extract_official_rule_observations`: добавлено объединение кандидатных страниц по документу для value-кодов (`PZ-009`, `KR-055`, `KR-058`, исключая вентиляционные `IOS4-*`, у которых уже есть свой более широкий page-sweep) — страница, уже помеченная MATRIX_FIELD-локатором для ЛЮБОГО из этих трёх кодов в данном документе, становится кандидатом для ВСЕХ трёх экстракторов в этом документе. Мотивация: одна страница общих примечаний часто повторяет несколько параметров сразу (это и подтвердилось: `F0137:p1` содержит одновременно abs-отметку для `PZ-009` и "Стена в грунте B25" для `KR-055`). Per-document/per-code page cap (`_prioritized_pages(..., limit=80)`) не менялся — это не «просканировать всё», а «расширить пул уже find преаннотированных кандидатных страниц», поэтому асимптотика стоимости та же.
3. Regression test не добавлялся отдельным файлом — существующие `test_case10_official.py` (`test_official_rule_pack_extracts_values_with_source_bbox`, `test_metrics_match_independent_ids_count_extra_positives_and_file_page`, `test_evaluation_export_includes_rule_comparison_result_and_normalized_values`) уже покрывают selection/export-контракт и прошли без изменений; полноценный regression на multi-fragment/union-discovery закрыт live smoke на реальном `OBJ-NOVOSLOBODSKAYA` (детерминированный, использует реальные PDF, не gold-replay).

### Performance: `/case10/processes/{id}/status` payload deferral

Отдельно от quality gates — профилирование (не угадывание) показало, что flaky p95 (`0.20–0.298s` при лимите `0.25s`) стабильно объясняется одним и тем же: `process_to_dict` на каждый status-poll вызывал `latest_protocol(...)` → SQLAlchemy eager-загружала `Protocol.payload_json` (`Column(JSON)`, полный findings/annex_2 payload, >1MB на объект с 132+ параметрами) и заново JSON-сериализовала её в HTTP-ответ, хотя `/status` семантически должен отдавать только process/protocol identity + counts.

Исправление:
- `latest_protocol(..., include_payload: bool = False)` — при `False` применяет `query.options(defer(Protocol.payload_json))`, so SQLite не читает и не декодирует эту колонку.
- `protocol_to_dict(..., include_payload: bool = True)` — при `False` не обращается к (defer-нутому) `payload_json` вовсе (иначе SQLAlchemy triggered бы ленивый SELECT).
- `process_to_dict(..., include_protocol_payload: bool = True)` — прокидывает флаг.
- `GET /case10/processes/{process_id}/status` теперь вызывает `process_to_dict(db, process, include_protocol_payload=False)`.
- Все остальные вызовы (`/case10/protocols/*`, `/case10/processes` list, create/run process, official-dataset import с `run_processes=True`, project overview) не изменены — используют default `include_payload=True`, полный payload там по-прежнему доступен.

### Проверки (все — на живом Docker `lct-hack-2026-api-1`, пересобран `docker compose up -d --build api`)

- Unit/domain/evaluation/official/training-release suite: `python -m unittest discover -s tests` внутри контейнера (tests примонтированы через `docker cp`) — **33/33 OK**, без изменений в самих тестах.
- `evaluation.smoke_real_data` (full, с `--verify-pages --verify-protocol`, оба объекта, финализация протокола, PDF-рендер, audit) — **PASS**, `evaluation/reports/real_data_cp28_final/`.
- `evaluation.smoke_hidden_blind` (`OBJ-RECHNIKOV-7-7`) — **PASS**: `gold_checks created/updated = 0`, `status_counts` идентичны checkpoint 27 (`CANDIDATE:1, NEGATIVE_VERIFIED:2, MISSING_EVIDENCE:2, NOT_COMPARABLE:128`), `candidate_rate=0.0075` (лимит `0.15`), `hidden_labels_used=false` — никакого overdetection или leakage regression.
- `evaluation.performance_smoke` — прогнан **9 раз подряд** (включая один прогон сразу после `docker compose restart api`, холодный старт + новый project) на обоих объектах (`OBJ-NOVOSLOBODSKAYA`, `OBJ-TYUMENSKAYA-5-GOLD-SEED`): `status_endpoint_p95_seconds` стабильно **0.030–0.047s** (было `0.13–0.298s`, нестабильно проходило/падало у лимита `0.25s`); `submission_seconds` стабильно **0.08–0.22s** (лимит `40s`); `process_seconds` **1.6–18s** (лимит `150s`, максимум — холодный старт первого запроса после restart, не повторяется на последующих прогонах). Ранее заявленные ~65s на submission на этом коде и данных не воспроизведены ни разу за 9 прогонов, включая холодный старт — вероятно относились к более раннему/другому состоянию или сильно "загрязнённому" повторными прогонами project.

### Acceptance metrics после фикса (live, `evaluation/reports/real_data_cp28_final/`)

`OBJ-NOVOSLOBODSKAYA`:
- `evidence_localization_accuracy` / `source_localization_exact_file_page`: **0.4167 → 0.75** (5/12 → 9/12). Gate `>=0.95`: **ещё FAIL**.
- `document_linking_accuracy`: **0.5 → 0.75** (6/12 → 9/12). Gate `>=0.95`: **ещё FAIL**.
- `normalized_value_and_status_accuracy`: **0.8 → 1.0** (12/15 → 15/15). Gate `>=0.90`: **теперь PASS**.
- `false_positive_rate`: **0.0**. Gate `<=0.10`: **PASS**.
- `finding_precision/recall/f1`: `null` — public Novoslobodskaya denominator для этого object содержит только negative-checks (`tn=5, tp=fp=fn=0`), numerator/denominator для precision/recall структурно не определены при отсутствии positive gold на этом объекте; это не изменилось этим checkpoint-ом и не является багом извлечения.
- `object_level_split_valid`: **true**.

`OBJ-TYUMENSKAYA-5-GOLD-SEED` (regression check, без изменений в логике для этого объекта, кроме побочного эффекта union-discovery): `evidence_localization_accuracy=0.9167` (без изменений, тот же единственный известный `IOS4-078/314` источник расхождения из checkpoint 24/25 — НЕ трогался и не закрывался тут), `document_linking_accuracy=1.0`, `normalized_value_and_status_accuracy=1.0`, `finding_precision/recall/f1=1.0/1.0/1.0`. **Регрессии нет.**

### Оставшиеся P0 после этого checkpoint (localization/linkage gate ещё не закрыт)

Полный разбор оставшихся 3 из 12 evidence-несовпадений на `OBJ-NOVOSLOBODSKAYA` (после fix):

1. `PZ-009` RD: gold-evidence `F0140:p3`; после union-discovery `F0140` стал кандидатом для `PZ-009`, но `_extract_absolute_zero` не находит на его кандидатных страницах текстовое совпадение "абсолютная отметка ... 159.95" — значение там, по всей видимости, присутствует не как текстовое предложение (gold пометил это evidence как `PAGE_LEVEL_VISUALLY_VERIFIED`, не `OCR_VERIFIED`), то есть чисто текстовый regex-extractor этого не закрывает без визуального/графического извлечения.
2. `KR-055 "Стена в грунте"` ID stage: gold ID-evidence — `F0004:p5`; `F0004` не помечен MATRIX_FIELD ни одним из трёх value-кодов (его теги — `ODI-122/SPZU-029/SPZU-039`), поэтому union-discovery его не подхватывает; это тот же паттерн missing-tag gap на один уровень глубже.
3. `KR-058` вторая RD-страница: gold RD-evidence содержит 2 страницы (`F0140:p3` и `F0140:p7`); `_extract_foundation_thickness` находит совпадение только на `p3` — anchor-regex не сработал на `p7` (другая формулировка/расположение текста на странице), чисто recall-gap конкретного regex, не selection-bug.

Все три — узкие, объяснённые extraction-recall gaps (не арбитражный selection-баг и не object-specific hardcode), закрытие которых потребовало бы либо визуального/графического извлечения текста, либо расширения discovery на документы без ЛЮБОГО из трёх value-кодов вообще (что уже не «объединение уже найденных кандидатов», а полноценный broad scan — риск для performance P0, если делать не глядя). Не закрывалось hardcode-ом номера страницы/документа/локации — запрещено ТЗ этой сессии.

### Что осталось P0/P1

1. `evidence_localization_accuracy` и `document_linking_accuracy` для `OBJ-NOVOSLOBODSKAYA` (**0.75**) и `evidence_localization_accuracy` для `OBJ-TYUMENSKAYA` (**0.9167**, известный `IOS4-078/314` case) остаются ниже gate `0.95`. Причины для каждого конкретного оставшегося incident задокументированы выше/в checkpoint 24-25 — не является unknown unknown.
2. `finding_precision/recall/f1` для `OBJ-NOVOSLOBODSKAYA` остаются `null` — на public fixture этого объекта нет positive-gold denominator; не измеримо без нового labeled-примера, не выдумывается.
3. `OCR character accuracy`, `key fields exact match` — по-прежнему `null` с явной причиной (нет public/train-safe human-reference denominator), как в checkpoint 27.
4. RabbitMQ: подтверждено повторной проверкой (`grep` по всему репозиторию) — `api_service/app/domain/v3_messaging.py::publish_process_event` публикует в очередь `case10.process.events`, но ни в `api_service`, ни где-либо ещё в репозитории НЕТ consumer/worker-а для этой очереди и нет отдельного `docker-compose` service под него (`ifc_worker` — это Celery-consumer для IFC/Postgres, к CASE10 не относится). Остаётся задокументированным gap; production-like consumer не реализовывался в этом checkpoint, так как quality/performance P0 (пункт 1 выше) ещё не полностью закрыты.
5. React: подтверждено — `frontend/` статический (`index.html`/`app.js`/`styles.css`, без `package.json`/build-стека). Решение о минимальном React-shell не принималось в этом checkpoint (не в scope, пока не закрыт п.1); анализ trade-off уже задокументирован в `CASE10_TECHNICAL_STRATEGY_V3_OFFICIAL_TZ.md` (раздел про React-shell как P0 для 5/5 соответствия ТЗ).
6. Managed retraining/model registry — не трогался в этом checkpoint; тест `test_case10_training_release` по-прежнему в зелёном наборе (33/33), поведение (`BLOCKED` при недостатке approved данных, `no_auto_publish=true`) не менялось.

### Следующий P0 шаг

Продолжить с пункта 1 выше: либо визуальное/табличное извлечение для чисто-графических evidence (`PZ-009` case), либо решить, оправдано ли расширение discovery до документов без tagged value-кода вообще (с explicit performance budget/cap), и только затем переходить к RabbitMQ worker / React-shell.

## 29. Localization/linkage root-cause fixes (bbox token bug, KR-058 generalization, bounded confirm-only fallback, OCR rus+eng), 17.09.2026

### Цель checkpoint

Продолжение с checkpoint 28 baseline (Novoslobodskaya localization/linkage 0.75, Tyumenskaya 0.9167). Задача: закрыть оставшиеся P0 gaps общими механизмами, без hardcode object_id/file_id/page/value, без GOLD leakage, без performance-регрессии. Сначала воспроизведён и проверен по актуальному коду baseline (не доверял checkpoint 28 без проверки) — подтверждён точно совпадающим свежим прогоном на пересобранном Docker API.

### Точная диагностика по каждому mismatch (до любых изменений кода)

Прямым чтением исходных PDF внутри контейнера (`fitz`, без GOLD/check_id) для каждого из 3 оставшихся Novoslobodskaya evidence-несовпадений:

1. **`PZ-009`, RD, gold `F0140:p3`.** Текст-слой страницы 3 РЕАЛЬНО содержит `"...абсолютной отметке 159.95."` (проверено прямым поиском по тексту снапшота). Regex `_extract_absolute_zero` НАХОДИЛ это совпадение, но `_bbox_for_tokens` требовал byte-exact совпадение токена, а PDF-экстракция приклеивает точку конца предложения к последнему слову (`"159.95."`, не `"159.95"`) → bbox lookup молча проваливался → вся observation отбрасывалась (`if not bbox: return []`). **Не visual/OCR проблема** — чистый extraction bug в bbox-сопоставлении токенов, общий для любого значения в конце предложения на любом документе/объекте.
2. **`KR-055`, "Стена в грунте" (B25), RD, gold `F0137:p1`.** `F0137` (документ типа СВГ/стройгенплан) никогда не был кандидатом для кода `KR-055`, потому что upstream MATRIX_FIELD-разметка тегировала его страницы только кодом `PZ-009`. Значение B25 для этой локации РЕАЛЬНО присутствует и корректно извлекается на PD-стороне (`F0105`, страницы 13/14/17/24 — проверено вручную). RD-сторона отсутствовала строго из-за code-gated discovery, не из-за отсутствия текста.
3. **`KR-058`, RD, вторая страница gold `F0140:p7`.** Anchor-regex требовал `толщин\w*` рядом с `фундамент\w*...плит\w*` в фиксированном порядке/окне. Реальная формулировка на этой странице — `"Железобетонная фундаментная плита h=1200 мм"` — не содержит слова "толщина" вообще; использует стандартное чертёжное обозначение `h=`.
4. **`KR-055`, "Стена в грунте" (B25), ID, gold `F0004:p5`.** Прямая проверка (текст-слой + rus+eng OCR) страницы 5 документа `F0004` показала: это страница РЕЕСТРА приложений акта (список протоколов испытаний/сертификатов), а не страница со значением класса бетона как текстом. `F0004` также не тегирован ни одним из 3 value-кодов upstream-разметкой. Значение B25, по всей видимости, находится на другой, вероятно отсканированной странице того же 34-страничного акта в виде штампа/таблицы протокола испытаний — не извлекается ни text-layer, ни generic OCR регексом без специализированного table/stamp-парсинга.
5. **Tyumenskaya `IOS4-078/314`, localization.** Переподтверждено НЕЗАВИСИМО от checkpoint 24 (там использовался только English-only OCR): полностраничный rus+eng OCR (после починки zoom, см. ниже) страницы 18 документа `F0201` также НЕ находит токен `314`; страница 20 подтверждена как единственная реальная страница с этим токеном (text-layer). Причина — не OCR-язык; общий OCR-механизм не меняет вывод. Room-314-specific исключение не добавлялось (запрещено этой сессией).

### Реализованные общие исправления

1. **`api_service/app/domain/official_rule_packs.py::_bbox_for_tokens`** — сравнение токена теперь допускает висящую конечную пунктуацию (`.rstrip(".,;:")`) на стороне слова перед сравнением. Общий фикс для ЛЮБОГО значения в конце предложения на ЛЮБОМ документе/объекте/коде, использующем `_extract_absolute_zero`.
2. **`_extract_foundation_thickness`** переписан на два прохода: сначала находит "упоминание плиты" (`фундамент\w*.{0,60}?плит\w*` ИЛИ обратный порядок), затем ищет thickness-anchor (`толщин\w*` ИЛИ `h\s*=`) в широком (±220 симв.) локальном окне вокруг него — устойчиво к перестановке слов, словам между анкорами и построчным разрывам (не переживают склейку `_indexed_words`). `h=NNNN мм` добавлен как общепринятое чертёжное обозначение толщины элемента (не специфично для одного документа).
3. **Bounded confirm-only document-level fallback** (`api_service/app/domain/official_rule_packs.py::find_confirming_fallback_observation`, `new_fallback_budget`, `_matching_observation`; вызывается из `official_evidence.py::_create_rule_groups`):
   - Триггерится ТОЛЬКО когда локация уже НЕ является нарушением (PD/RD уже согласны) и один из enabled-стейджей (типично ID) не нашёлся быстрым путём.
   - Документы-кандидаты выбираются СТРУКТУРНО (`effective_dataset_stage(doc) == missing_stage`), НЕ по upstream value-code тегам — по возрастанию `document.id` (порядок реестра датасета, не GOLD-производный).
   - **Confirm-only по конструкции**: найденная observation принимается ТОЛЬКО если её `location` и `normalized_value` совпадают с уже установленным (другими стейджами) значением. Несовпадающее значение отбрасывается — механизм НЕ МОЖЕТ ни создать новое нарушение, ни изменить уже принятое решение.
   - Единый **shared mutable budget** (`documents`/`pages`/`ocr_pages`) на ВЕСЬ process-run, а не на каждый (code, stage) gap отдельно — иначе N找 gaps дают N× стоимость (это и произошло в первой, отклонённой версии этого фикса, см. "Отклонённые подходы" ниже).
   - OCR — крайняя мера внутри того же бюджета, только для страниц с подозрительно малым числом text-layer слов (вероятно сканы).
   - Diagnostics (documents_scanned/pages_scanned/ocr_pages_scanned/found/elapsed_seconds) пишутся в audit log процесса (`action=RULE_FALLBACK_DISCOVERY`) через уже существующий `add_audit`, без новых таблиц.
4. **rus+eng OCR как общий, конфигурируемый механизм** (`api_service/app/domain/dataset_sources.py`):
   - `tesseract-ocr-rus` добавлен в `api_service/Dockerfile`.
   - `ocr_original_clip(..., lang: str | None = None)` — параметр языка, по умолчанию `settings.OCR_LANG` (`rus+eng`); кеш (`lru_cache`) теперь включает `lang` в ключ.
   - Новая `ocr_page_snapshot(document, page, *, lang=None)` — полностраничный OCR через TSV (`tesseract ... tsv`), возвращает snapshot той же формы, что text-layer (`page/width/height/text/words`), с **честными word-level bbox из TSV координат**, переведёнными в PDF point-space (та же система координат, что у text-layer bbox) — НЕ синтетический/угаданный bbox.
   - **Адаptive OCR zoom** (`ocr_zoom_for_page_size`): фикс реальной находки — A0/A1-страницы (например, `F0201:p18`, 2384×3370pt) при фиксированном zoom=2.5 давали ~50-мегапиксельное изображение → tesseract timeout (замерено: 46.3s, timeout). Теперь zoom адаптивно ограничивается так, чтобы длинная сторона рендера не превышала ~3500px (A4 получает максимальный zoom=3.0; A0 — ~1.04).
   - **Регрессия найдена и исправлена**: смена дефолтного языка `ocr_original_clip` с `"eng"` на `settings.OCR_LANG` сломала СУЩЕСТВУЮЩИЙ, уже откалиброванный вентиляционный OCR-путь (`_extract_ventilation_observations`, используется для размерных crop'ов типа `"200x100"`) — rus+eng режим дал на Tyumenskaya НОВЫЙ ложный `CANDIDATE` (`IOS4-079/006`, garbled value), `finding_f1` упал 1.0→0.923. Исправлено явным `lang="eng"` в этом ОДНОМ вызове (цифровые crop'ы без прозы не выигрывают от кириллицы, только рискуют визуальной путаницей букв/цифр); полностраничный текстовый OCR остаётся на `rus+eng` по умолчанию.

### Отклонённые подходы (важно для будущих сессий)

- **Первая версия document-level fallback** (без confirm-only ограничения, с per-gap budget по 300 страниц) была реализована, прогнана и ОТКЛОНЕНА: она (a) создала новый ложноположительный `CANDIDATE` на Novoslobodskaya (`KR-055` "Стена в грунте", нашла B15 на нерелевантном ID-документе и МЕНЯЛА решение, `finding_f1` упал до 0.0, `normalized_value_and_status_accuracy` 1.0→0.8), и (b) утроила/учетверила стоимость (Novoslobodskaya 43s, Tyumenskaya 111s), потому что бюджет на каждый (code,stage) gap умножался на число gaps. Заменена на confirm-only + shared budget дизайн выше. Это прямое, эмпирически найденное подтверждение того, почему "свободное открытие новых значений" через fallback небезопасно, а "подтверждение уже принятого решения" — безопасно по конструкции.

### Metrics до/после (все — на живом Docker `lct-hack-2026-api-1`, пересобран из этого checkpoint)

`OBJ-NOVOSLOBODSKAYA`:
| metric | cp28 | cp29 | gate |
|---|---|---|---|
| `evidence_localization_accuracy` | 0.75 (9/12) | **0.9167 (11/12)** | `>=0.95`: ещё FAIL |
| `document_linking_accuracy` | 0.75 | **0.9167** | `>=0.95`: ещё FAIL |
| `normalized_value_and_status_accuracy` | 1.0 | 1.0 | `>=0.90`: PASS (сохранён) |
| `false_positive_rate` | 0.0 | 0.0 | `<=0.10`: PASS |
| `finding_precision/recall/f1` | null | null | public denominator для этого объекта — только negative checks (не изменилось) |
| `coverage` | 0.0227 | 0.0227 | не изменилось — фиксы касались evidence quality, не решений |
| `abstention_rate` | 0.9773 | 0.9773 | не изменилось |

`OBJ-TYUMENSKAYA-5-GOLD-SEED`: без изменений и без регрессии — `evidence_localization_accuracy=0.9167` (тот же документированный `IOS4-078/314` case, переподтверждён под rus+eng OCR, не закрыт намеренно), `document_linking_accuracy=1.0`, `precision/recall/f1=1.0/1.0/1.0`, `normalized_value_and_status_accuracy=1.0`, `coverage=0.0152`, `abstention_rate=0.9848`. (Промежуточная версия с непроверенным OCR-lang-дефолтом на секунду дала `f1=0.923` — эта регрессия найдена и исправлена в этом же checkpoint, финальный прогон подтверждает возврат к 1.0.)

Оставшийся Novoslobodskaya localization/linkage gap (11/12) — единственная причина: `KR-055 "Стена в грунте"` ID-evidence (`F0004:p5`, честно проверено — реестр протоколов, не значение как текст; см. root cause #4 выше). Confirmed-safe bounded fallback это искал (audit log: 5 документов, 80 страниц, 5 OCR-страниц, 10.3s, `found=false`) и корректно НЕ нашёл — не форсировалось никаким hardcode.

### Hidden blind (`OBJ-RECHNIKOV-7-7`) до/после

Идентично checkpoint 27/28: `status_counts` не изменились (`CANDIDATE:1, NEGATIVE_VERIFIED:2, MISSING_EVIDENCE:2, NOT_COMPARABLE:128`), `candidate_rate=0.0075` (лимит 0.15), `gold_checks created/updated=0`, `hidden_labels_used=false`. Confirm-only fallback не создал никаких новых candidate на hidden-объекте.

### Performance

- API `/status` p95: **0.015–0.037s** через 5 прогонов подряд после restart (цель сессии `<=0.20s`, hard-лимит `<=0.25s`) — без изменений относительно cp28, подтверждена стабильность.
- `process_seconds` (Novoslobodskaya): **cold ≈ 27–36s** (первый запрос после restart контейнера, включает confirm-only fallback scan), **warm ≈ 2–2.4s** (lru_cache на страницы/документы) — лимит `150s`, запас большой.
- `process_seconds` (Tyumenskaya): **≈ 20–25s**, без регрессии.
- `submission_seconds`: **0.08–0.15s** (лимит `40s`).
- `protocol_seconds`: **0.19–0.30s** (лимит `40s`).
- Fallback-бюджет по умолчанию (`config.py`): `RULE_FALLBACK_MAX_DOCUMENTS=8`, `RULE_FALLBACK_MAX_PAGES_TOTAL=80`, `RULE_FALLBACK_MAX_OCR_PAGES=5`, все — env-конфигурируемые, единый shared pool на весь process run (не на каждый gap).

### Изменённые файлы

- `api_service/app/domain/official_rule_packs.py` — bbox trailing-punctuation fix, `_extract_foundation_thickness` generalization, `find_confirming_fallback_observation`/`new_fallback_budget`/`_matching_observation`/`_document_page_count_hint`, explicit `lang="eng"` pin на вентиляционном OCR crop.
- `api_service/app/domain/official_evidence.py` — `_create_rule_groups` теперь принимает `documents/exclude_pages/budget/fallback_diagnostics` и вызывает confirm-only fallback только для non-violation групп с missing enabled stage; `create_official_evidence_groups` возвращает diagnostics list.
- `api_service/app/domain/v3_pipeline.py` — `run_process` пишет `RULE_FALLBACK_DISCOVERY` audit-запись при непустых diagnostics.
- `api_service/app/domain/dataset_sources.py` — `lang`-параметр и cache-key для `ocr_original_clip`; новые `ocr_page_snapshot`/`_ocr_page_words`/`ocr_zoom_for_page_size`/`_run_tesseract_tsv`.
- `api_service/app/config.py` — `RULE_FALLBACK_MAX_DOCUMENTS`, `RULE_FALLBACK_MAX_PAGES_TOTAL`, `RULE_FALLBACK_MAX_OCR_PAGES`.
- `api_service/Dockerfile` — `tesseract-ocr-rus`.
- `api_service/tests/test_case10_fallback.py` (новый файл, 14 тестов, все — synthetic fixtures, без реальных object_id/file_id/page/GOLD-значений):
  - `KR058GeneralizationTests` (3): reordered/gapped phrasing, `h=` anchor, unrelated-thickness-not-picked-up.
  - `PZ009BboxTokenTests` (1): значение в конце предложения всё равно резолвит bbox.
  - `OcrZoomTests` (3): A4 получает max zoom, A0 капается, degenerate size не паникует/не инвертирует.
  - `MultiFragmentEvidenceTests`/`MultiFragmentEvidenceCountTests` (2): несколько страниц с одним значением — все становятся fragments; страница с ДРУГИМ значением на той же локации — не подтягивается как support.
  - `ConfirmOnlyFallbackTests` (5): находит confirming значение на нетегированном документе; НЕСОВПАДАЮЩЕЕ значение отбрасывается (не создаёт violation); document cap соблюдается; page budget общий и истощается между документами; отсутствие документов нужной стадии — ноль вызовов extraction.

### Проверки

- `python -m unittest discover -s tests` внутри `lct-hack-2026-api-1` (tests примонтированы через `docker cp`, как в checkpoint 27/28) — **47/47 OK** (33 существующих + 14 новых, 0 изменённых старых тестов).
- `evaluation.smoke_real_data --verify-pages --verify-protocol` (оба объекта, финализация, PDF, audit) — **PASS**, `evaluation/reports/real_data_cp29_final/`.
- `evaluation.smoke_hidden_blind` — **PASS**, идентичные counts.
- `evaluation.performance_smoke` — **5 прогонов на обоих объектах** после `docker compose restart api` — **PASS** каждый раз, p95 стабилен.
- Промежуточные отклонённые/итерационные прогоны сохранены как `evaluation/reports/real_data_cp29_{baseline,fix1_bbox,fix1b,fix2,fix3,fix3b,fix3c,cold}/` для трассируемости решений (какая версия что дала).

### Что осталось P0/P1

1. **Novoslobodskaya `evidence_localization_accuracy`/`document_linking_accuracy` = 0.9167 < 0.95 gate.** Единственная оставшаяся причина честно диагностирована (root cause #4 выше: `KR-055 "Стена в грунте"` ID-evidence — реестр протоколов испытаний, вероятно отсканированная страница дальше в 34-страничном акте с классом бетона в штампе/таблице). Bounded confirm-only fallback это искал и корректно не нашёл — закрытие требует специализированного table/stamp-parsing поверх OCR (существенно больший объём работы, вне экономного бюджета этой сессии) либо признания как honest residual gap.
2. **Tyumenskaya `evidence_localization_accuracy` = 0.9167 < 0.95 gate**, тот же документированный `IOS4-078/314` case из checkpoint 24, переподтверждён под rus+eng OCR в этой сессии — не OCR-язык, не общий механизм; room-314-specific исключение НЕ добавлялось (запрещено).
3. `finding_precision/recall/f1` для Novoslobodskaya — `null`, public denominator для объекта не содержит positive checks; не измеримо без нового labeled примера.
4. `OCR character accuracy`, `key fields exact match` — по-прежнему `null` с явной причиной (нет human-reference denominator), не изменилось.
5. **Coverage остаётся низким** (`0.0227` Novoslobodskaya, `0.0152` Tyumenskaya) несмотря на `precision/recall/f1=1.0` на Tyumenskaya — это ожидаемо: только positive/negative controls, найденные rule-packs, доходят до определённого статуса; остальные 130/132 параметров абстинируются (`NOT_COMPARABLE`/`CLARIFICATION_REQUIRED`/`MISSING_EVIDENCE`) за пределами текущих 5 официальных rule-packs (`PZ-009`, `KR-055`, `KR-058`, `IOS4-078`, `IOS4-079`). P/R/F1=1.0 не следует трактовать как полное покрытие системы.
6. RabbitMQ (publisher есть, CASE10 consumer/worker отсутствует) и React (frontend статический) — НЕ трогались в этом checkpoint, так как quality gates п.1-2 ещё не полностью закрыты (явное условие этой сессии для перехода к RabbitMQ worker).
7. Managed retraining/model registry — не трогался; `test_case10_training_release` в зелёном наборе без изменений.

### Следующий шаг

P0 (если продолжать quality track): специализированное извлечение для сканированных протоколов испытаний/штампов (table-aware OCR или структурный парсинг лабораторных сертификатов) — заметно больший объём работы, чем текущие исправления; оценить готовность инвестировать до перехода к RabbitMQ.
P0 (если считать текущий 0.9167/0.9167 результат достаточным для перехода): реализовать RabbitMQ job worker (пункт 10 задания этой сессии) — POST запуска должен быстро возвращать управление, job — в очередь, worker — идемпотентно вызывать существующий `run_process` и обновлять статусы через уже существующий `add_audit`/`_set_process_status`.

## 30. Novoslobodskaya закрыт честным extraction-фиксом (12/12), Tyumenskaya IOS4-078/314 доказан как public GOLD localization conflict, 17.09.2026

### Цель checkpoint

Продолжение с checkpoint 29 baseline (Novoslobodskaya localization/linkage 0.9167 = 11/12, Tyumenskaya 0.9167, unrelated к 0.9167 из checkpoint 29). Два последних localization gap разбирались РАЗНЫМИ методами: (A) Novoslobodskaya — честная попытка закрыть реальный extraction miss; (B) Tyumenskaya — сначала независимый forensic audit (physical page vs logical sheet, PDF `/PageLabels`, gold-blind текстовый поиск), чтобы установить, runtime-баг это или public GOLD conflict, ДО каких-либо изменений в inference. Полного повторного аудита проекта не делалось; RabbitMQ/React не трогались; метрики не подгонялись под известный GOLD — ни одно значение page/file/object не хардкодилось.

### Phase 1 — forensic audit по каждому mismatch (до изменений кода)

**Novoslobodskaya, `KR-055` "Стена в грунте", ID-стадия, gold `F0004:p5`.** Прямым чтением исходного PDF (`fitz`, без GOLD/check_id для discovery — gold читался только ПОСЛЕ, для сравнения):
- `F0004` — акт освидетельствования скрытых работ (АОСР №1А-БСС), стадия ID, 34 страницы.
- Страница 5 — «Реестр приложений №1 к акту» (табличный реестр документов о качестве). В тексте страницы буквально 8 раз встречается фраза `«...партии БСТ В25 П4F(I)200W8»` — значение класса бетона B25 РЕАЛЬНО присутствует как обычный текст (не штамп/скан/растр, вопреки первоначальной гипотезе checkpoint 29 — она была неверна, что и подтвердил прямой пословный дамп страницы).
- Почему не извлекалось: `_extract_concrete_classes` ищет локацию («Стена в грунте», «Фундаментная плита» и т.д.) в 35 словах ПЕРЕД найденным `B25`/`В25`. На странице 5 (табличный реестр) рядом нет ни одного из anchor-слов («грунт», «стен», «фундамент», «плит», …) — только названия документов о качестве и организации-поставщики. Локация для этого объекта декларируется НЕ рядом со значением, а на страницах 1–2 ТОГО ЖЕ документа: «7. Разрешается производство последующих работ: Устройство стены в грунте» — это стандартное поле №7 типовой формы «Акт освидетельствования скрытых работ» (АОСР), не специфичное для одного файла/объекта.
- Вывод: реальный причинный механизм — общий для класса документов «акт + приложенный реестр материалов», а не для одной страницы/файла. Решаемо генерализуемым фиксом, без OCR/визуального fallback.

**Tyumenskaya, `IOS4-078`/помещение `314`, RD-стадия, gold `F0201:p18` (sheet 4) vs runtime `F0201:p20`.**
- Проверено: `F0201` (676 страниц) не содержит `/PageLabels` (`pdf.get_page_labels() == []`, `catalog.PageLabels` отсутствует), т.е. общего PDF-механизма подписи страниц нет.
- Однако по ДВУМ независимым точкам данных (`IOS4-079/012`: sheet 3 → pdf p.17; `IOS4-078` sheets для комнат 140/142/147/198/314: sheet 4 → pdf p.18) обнаружен систематический сдвиг **pdf_page = document_sheet_number + 14** (14 нечертёжных страниц фронт-маттера/обложки перед «Листом 1»). Проверка на прямом чтении страницы 18: это `«Экспликация помещений 1 этажа»`, где буквально перечислены помещения **140, 142, 147, 198** — то есть offset ПОДТВЕРЖДЁН и РАБОТАЕТ корректно для этих четырёх помещений (общий page-numbering фикс НЕ требуется — гипотеза "physical page != logical sheet" оказалась верной, но не объясняет расхождение по 314).
- Помещение **314 на странице 18 ОТСУТСТВУЕТ** (gold-blind текстовый поиск токена `314` по полному тексту страницы — 0 совпадений). Помещение 314 — это этаж 3 («Мастерская прототипирования», 37.4 м²), которое находится на **странице 20** («Экспликация помещений 3 этажа» / план ОВ того же этажа) — независимо подтверждено И текстовым слоем (`... 313 | прототипирования | 37.4 | 314 | 315 ...`), И тем, что именно эту страницу runtime уже самостоятельно (gold-blind, через `official_rule:ios4-078-ocr`) выбирает как источник для помещения 314.
- Причина: gold check `TRAIN-0010` (помещение 314) входит в `finding_group_id = G-TR-004` вместе с `TRAIN-0008` (147) и `TRAIN-0009` (198) и переиспользует ТОТ ЖЕ evidence-bundle (`F0201:p18`), что корректно для 147/198, но не для 314 (другой этаж, другой физический лист). Это copy-paste конфликт в public gold-разметке одного shared evidence bundle на несколько разных находок одной finding_group, а не системная ошибка нумерации страниц и не runtime-баг.
- Per инструкции: раз общий mapping НЕ найден (offset существует, но не объясняет именно этот кейс), runtime НЕ принуждался выбирать страницу 18. Room-314-specific исключение не добавлялось.

### Phase 2 — реализованный фикс для Novoslobodskaya (без OCR/visual fallback — не потребовался)

`api_service/app/domain/official_rule_packs.py`:
1. Новая `_act_subject_location(document)` — читает страницы 1–3 документа (уже открытого извлечения, `extract_original_pages`, тот же кэш), ищет маркер типовой формы `«акт освидетельствования скрытых работ»`, затем поле №7 `«разрешается производство последующих работ»`, и резолвит локацию из окна после него ТЕМИ ЖЕ anchor-правилами, что и `_concrete_location` (не новый словарь синонимов). Общий механизм для любого документа такой формы, не привязан к `F0004`.
2. `_extract_concrete_classes` — если локальный (35-словный) контекст не дал локации И нет KR-058-fallback, применяется `_act_subject_location(document)` как fallback-локация с пониженной confidence (0.75, как и существующий KR-058-fallback). Прямой локальный контекст всегда в приоритете и не переопределяется (проверено тестом).
3. **Обнаружена и исправлена fairness-проблема в shared fallback budget** (введён в checkpoint 29): при живом прогоне выяснилось, что confirm-only fallback для ОДНОЙ локации (`«Вертикальные конструкции подземной части»`, не связанной с искомым значением) потреблял ВЕСЬ общий бюджет (`documents_scanned=5, pages_scanned=80/80`), оставляя 0 страниц для следующей по очереди локации (`«Стена в грунте»`) — из-за чего фикс #1–2 сам по себе НЕ повлиял на метрику при первом прогоне (проверено эмпирически, не предположение). Добавлен `RULE_FALLBACK_MAX_PAGES_PER_ATTEMPT` (новый config, по умолчанию 60) — одна попытка (parameter, stage, location) не может забрать больше своей доли из общего пула; общий пул увеличен `RULE_FALLBACK_MAX_PAGES_TOTAL: 80 → 160` (линейно, НЕ per-gap, в отличие от отклонённого в checkpoint 29 дизайна с per-gap 300 страниц). `RULE_FALLBACK_MAX_DOCUMENTS`/`RULE_FALLBACK_MAX_OCR_PAGES` не менялись.

Никакой новый OCR-backend, table/stamp-parser, embedding-модель или LLM/VLM не добавлялись — они не потребовались, т.к. значение было текстовым и достижимым текстовым regex-fallback-ом после генерализации discovery. Соответствующие фазы задания (PaddleOCR ensemble, PP-Structure spike, hybrid lexical/semantic retrieval) рассмотрены и осознанно отклонены как избыточные для данного случая — см. «Отклонённые эксперименты» ниже.

### Phase 1 (продолжение) — machine-readable audit artifact для Tyumenskaya

Новый `evaluation/audit_localization.py` — независимый (gold используется только для сравнения ПОСЛЕ) forensic-скрипт: открывает `F0201` по манифесту/хэшу, проверяет `/PageLabels`, ищет токен помещения на gold-странице и на странице, независимо найденной runtime, и сверяет сиблинг-чеки той же `finding_group_id`. Результат сохранён в `evaluation/audits/localization_conflicts.json`:
- `public_gold_page=18`, `independently_observed_page=20`, `public_gold_page_contains_room_token=false`, `independently_observed_page_contains_room_token=true`;
- сиблинги `140/142/147/198` на той же странице 18 — `page_contains_own_room_token=true` (offset реально работает для них, значит это НЕ системная ошибка нумерации);
- `reason="POSSIBLE_GOLD_LOCALIZATION_CONFLICT"`, `extraction_method="official_rule:ios4-078-ocr"`, `evidence_bbox_pdf`, `page_labels=[]`.

`evaluation/metrics.py::evaluate_case10` получил новый опциональный параметр `localization_adjudications` (список таких кейсов). Строгая метрика (`evidence_localization_accuracy`, гейт `>=0.95`) считается **без изменений**, только против public gold. Добавлена отдельная, чисто информационная `evidence_localization_accuracy_source_verified` — засчитывает совпадение, если предсказанная страница совпадает с `independently_observed_page` документированного conflict-кейса (и только тогда; несовпадающая страница НЕ засчитывается «на всякий случай» — покрыто тестом `test_an_unmatched_adjudication_does_not_change_anything`). Ни один порог/gate на новую метрику не завязан — она не может «пройти» вместо строгой. `evaluation/smoke_real_data.py` подгружает `evaluation/audits/localization_conflicts.json`, если он существует, и пробрасывает в `evaluate_case10` для всех объектов.

### Metrics до/после (live Docker `lct-hack-2026-api-1`, пересобран из этого checkpoint)

`OBJ-NOVOSLOBODSKAYA`:
| metric | cp29 | cp30 | gate |
|---|---|---|---|
| `evidence_localization_accuracy` | 0.9167 (11/12) | **1.0 (12/12)** | `>=0.95`: **теперь PASS** |
| `document_linking_accuracy` | 0.9167 | **1.0** | `>=0.95`: **теперь PASS** |
| `normalized_value_and_status_accuracy` | 1.0 | 1.0 | `>=0.90`: PASS (сохранён) |
| `false_positive_rate` | 0.0 | 0.0 | `<=0.10`: PASS |
| `finding_confusion` | tp0/fp0/tn5/fn0 | tp0/fp0/tn5/fn0 | без изменений — новых candidate не создано |
| `coverage` / `abstention_rate` | 0.0227 / 0.9773 | 0.0227 / 0.9773 | без изменений — фикс касался evidence quality, не решений |

`OBJ-TYUMENSKAYA-5-GOLD-SEED` (без изменений в коде для этого объекта):
- `evidence_localization_accuracy` (strict, против public gold): **0.9167** — не изменилось и намеренно НЕ форсировалось к 1.0.
- `evidence_localization_accuracy_source_verified` (новая, информационная): **1.0** — единственное расхождение полностью объяснено и подтверждено forensic-артефактом.
- `document_linking_accuracy=1.0`, `finding_precision/recall/f1=1.0/1.0/1.0`, `normalized_value_and_status_accuracy=1.0` — без регрессии.

### Hidden blind (`OBJ-RECHNIKOV-7-7`)

Идентично checkpoint 27–29: `status_counts={CANDIDATE:1, NEGATIVE_VERIFIED:2, MISSING_EVIDENCE:2, NOT_COMPARABLE:128}`, `candidate_rate=0.007519` (лимит 0.15, практически не изменился относительно ~0.0075 из ТЗ), `gold_checks created/updated=0`, `hidden_labels_used=false`. Ни `_act_subject_location`-fallback, ни увеличенный page-budget не создали новых candidate на hidden-объекте.

### Performance / fallback wall time

- API `/status` p95: **0.024–0.030s** (Novoslobodskaya и Tyumenskaya, после restart) — лимит `0.25s`, цель `0.20s`.
- `process_seconds`: Novoslobodskaya **15–35s** (cold ~33–35s, warm ~15s), Tyumenskaya **~13–25s** — лимит 150s.
- Fallback discovery (audit log `RULE_FALLBACK_DISCOVERY`, Novoslobodskaya, финальный прогон): 5 попыток (по числу gap'ов), суммарно **8 документов, 120 страниц (из общего пула 160), 5 OCR-страниц, ~9.87s**. Попытка, нашедшая `KR-055 «Стена в грунте»`, отработала за **0.007s** (только text-layer, страницы документа уже в кэше от предыдущей попытки в этом же прогоне) — OCR не потребовался для закрытия итогового gap.
- Никаких full-dataset high-DPI рендеров/OCR не выполнялось; fallback остаётся bounded и запускается только после провала быстрого пути, как и в checkpoint 29.

### Отклонённые эксперименты (согласно Phase 9/пункту «STOP CONDITIONS» задания)

- **PaddleOCR PP-OCRv5 ensemble** — не пробовался. Root cause обоих оставшихся gap'ов не был OCR-recall проблемой (Novoslobodskaya — генерализуемый text-layer discovery-gap; Tyumenskaya — public GOLD conflict, не extraction). Добавление второго OCR-backend не повлияло бы ни на один из двух случаев, поэтому experiment не запускался — тратить бюджет на benchmark без целевого кейса было бы just tuning-ом «под среднюю метрику», что явно запрещено заданием.
- **PP-StructureV3 / table-aware region detection** — не пробовался. Первоначальная гипотеза checkpoint 29 (страница 5 — «нетекстовый штамп/скан») оказалась НЕВЕРНОЙ при прямой проверке: значение было обычным текстом. Табличный/layout-парсинг не потребовался.
- **Hybrid lexical/semantic page retrieval (BM25 + `intfloat/multilingual-e5-small`)** — не пробовался. Discovery-gap для Novoslobodskaya был не «страница не найдена в корпусе», а «страница найдена (через существующий confirm-only fallback), но у извлечённого совпадения не резолвилась локация» — чисто NLP/regex-логика на УЖЕ открытой странице, retrieval-слой был не нужен.
- **LayoutLM/Donut/ColPali/VLM** — не рассматривались, как и предписано (задание запрещает это без исчерпания lightweight-каскада; lightweight-каскад оказался достаточным).
- Все перечисленные пункты остаются описанными опциями на будущее, если появятся ДРУГИЕ, действительно визуальные/табличные gap'ы — не выполнялись «для галочки».

### Изменённые файлы

- `api_service/app/domain/official_rule_packs.py` — `_act_subject_location` (новая), `_extract_concrete_classes` (document-level fallback location, приоритет локального контекста сохранён), `find_confirming_fallback_observation` (`max_pages_this_attempt` + `attempt_pages_remaining`, per-attempt page cap поверх общего shared budget), `parse_rule_page` пробрасывает `document` в `_extract_concrete_classes`.
- `api_service/app/config.py` — `RULE_FALLBACK_MAX_PAGES_TOTAL: 80 → 160`, новый `RULE_FALLBACK_MAX_PAGES_PER_ATTEMPT=60`.
- `api_service/tests/test_case10_fallback.py` — `_document()` fixture получил опциональный `pages`; `test_document_cap_is_enforced`/`test_finds_confirming_value_on_an_untagged_document` обновлены на реалистичные явные page-count (были случайно завязаны на общий 60-страничный default-хинт); новый `test_per_attempt_cap_leaves_budget_for_a_later_gap`; новый класс `ActSubjectLocationFallbackTests` (3 теста: акт-декларированная локация используется без локального контекста, без AOSR-маркера локация НЕ придумывается, прямой локальный контекст в приоритете и не требует чтения других страниц документа).
- `api_service/tests/test_case10_evaluation.py` — `test_localization_adjudication_adds_a_source_verified_metric_without_changing_the_strict_one`, `test_an_unmatched_adjudication_does_not_change_anything`.
- `evaluation/metrics.py` — `evaluate_case10`/`_evaluate_evidence` получили `localization_adjudications`; новые ключи `evidence_localization_accuracy_strict_public_gold` (= прежней `evidence_localization_accuracy`, alias для явности), `evidence_localization_accuracy_source_verified`, `evidence_localization_adjudicated_cases_applied`.
- `evaluation/smoke_real_data.py` — загрузка `evaluation/audits/localization_conflicts.json`, если файл существует, проброс в `evaluate_case10`, добавлено поле в печатаемый summary.
- `evaluation/audit_localization.py` — новый forensic-скрипт (генерирует artifact, не хардкодит вывод).
- `evaluation/audits/localization_conflicts.json` — новый machine-readable artifact (1 задокументированный кейс).

### Проверки

- `python -m unittest discover -s tests` внутри `lct-hack-2026-api-1` (пересобран `docker compose build api`, тесты через `docker cp`) — **53/53 OK** (47 существующих из checkpoint 29 + 4 новых fallback-теста + 2 новых evaluation-теста, 2 существующих теста обновлены только в части synthetic fixture page-count, поведение самих проверяемых механизмов не менялось).
- `evaluation.smoke_real_data --verify-pages --verify-protocol` (оба объекта, финализация, PDF, audit) — **PASS**, `evaluation/reports/real_data_cp30_final/`. Промежуточные прогоны, зафиксировавшие diagnosis/fairness-баг и его фикс, сохранены как `evaluation/reports/real_data_cp30_{actlocation,budget_cap,cold}/` для трассируемости.
- `evaluation.smoke_hidden_blind` — **PASS**, идентичные counts, `candidate_rate` не вырос.
- `evaluation.performance_smoke` — **PASS** на обоих объектах, включая холодный старт после `docker compose restart api`.
- Отдельно проверено (`docker ps`), что рестарт/пересборка `api`-контейнера не задели посторонние сервисы этой же машины (`max-hack-*`) и что временная просадка порта у `lct-hack-2026-postgres-1` (побочный эффект `docker compose up -d api` без `--no-deps`, порт 5432 конфликтовал с чужим контейнером на этой машине) была замечена и восстановлена на исходный `POSTGRES_HOST_PORT=15432` — доступность IFC-сервиса не пострадала.

### Что теперь закрыто

1. **Novoslobodskaya `evidence_localization_accuracy`/`document_linking_accuracy` = 1.0 (12/12), gate `>=0.95` PASS.** Честно закрыт последний extraction gap — не hardcode, не подгонка: генерализуемый form-aware location fallback + исправление fairness-бага в существующем shared budget.
2. **Tyumenskaya `IOS4-078/314` доказательно задокументирован как public GOLD localization conflict**, не runtime-баг. Strict-метрика (`0.9167`) сохранена как есть (гейт формально не закрыт для этого объекта и не должен считаться закрытым через подмену метрики); source-verified-метрика (`1.0`) и полный forensic-артефакт с bbox/hash/sibling-cross-check предоставлены отдельно.

### Что осталось P0/P1

1. **Tyumenskaya `evidence_localization_accuracy` = 0.9167 < 0.95 gate по строгой (public gold) метрике.** Это осознанно НЕ закрыто хардкодом — задание прямо это запрещает. Если организаторы примут `evaluation/audits/localization_conflicts.json` как основание скорректировать эту одну public-gold запись, strict-метрика тоже станет 1.0; до тех пор гейт для этого объекта по строгой метрике остаётся FAIL, что является честным отражением состояния public-разметки, а не системы.
2. `finding_precision/recall/f1` для Novoslobodskaya — по-прежнему `null` (нет positive-gold denominator на этом объекте), не изменилось.
3. `OCR character accuracy`, `key fields exact match` — по-прежнему `null` (нет human-reference denominator), не изменилось.
4. Coverage остаётся низким (`0.0227`/`0.0152`) — те же 5 официальных rule-packs, как в checkpoint 29; не входило в scope этой сессии.
5. RabbitMQ (publisher есть, consumer/worker отсутствует) и React (frontend статический) — **не трогались**, как и предписано заданием этой сессии.
6. Managed retraining/model registry — не трогался; `test_case10_training_release` в зелёном наборе без изменений.

### Следующий шаг

Оба заявленных P0 quality-кейса этой сессии закрыты — один честным фиксом до 1.0, второй доказательно задокументирован как public-gold конфликт с раздельными strict/source-verified метриками, без hardcode и без OCR/embedding-моделей, которые не потребовались. Согласно Phase 10/9 задания: **не продолжать бесконечно тюнить OCR/extraction** на этих же двух объектах. Следующий архитектурный P0 — RabbitMQ job worker для CASE10 (пункт 10 предыдущих заданий): `publish_process_event` уже пишет в очередь `case10.process.events`, но consumer отсутствует; React — после RabbitMQ.

## 31. RabbitMQ worker / consumer: настоящая async Pull-схема CASE10, 18.09.2026

### Цель checkpoint

Довести CASE10 от частично асинхронной схемы (publisher/status-events уже были, но тяжёлый `run_process()` оставался в API-контексте) до Pull-модели из ТЗ:

`API / Node gateway -> RabbitMQ -> case10_worker -> Python pipeline -> DB/status -> polling API`.

Текущий quality contour из checkpoint 30 считался baseline: extraction/evidence logic, public GOLD, React и корректные старые тесты не переписывались ради PASS.

### Архитектурная схема после изменения

1. `POST /api/case10/projects/{project_id}/processes` быстро создаёт `process_id`, `InspectionProcess(status=QUEUED)` и `Case10ProcessJob(status=QUEUED)`, коммитит их в БД и публикует durable job в RabbitMQ `case10.process.jobs`.
2. `node_gateway` только проксирует start/status/protocol API; тяжёлый pipeline через gateway больше не запускается в HTTP-контексте.
3. `case10_worker` (`python -m app.worker_case10`) подключается к RabbitMQ, объявляет topology (`case10.process.jobs`, `case10.process.errors`, DLX), делает recovery scan по БД и потребляет jobs с `prefetch_count=1`.
4. Worker вызывает прежний `run_process()` без переписывания extraction/evidence logic. Status transitions пишутся в БД и продолжают публиковаться как status events.
5. `GET /api/case10/processes/{process_id}/status` только читает БД (`process_to_dict()` + latest job metadata) и не вызывает pipeline.

### Статусы

- Process: `PENDING` сохраняется как совместимость для старых записей; новая start-схема идёт `QUEUED -> PROCESSING -> PARSING -> READY` или `FAILED`.
- Job: `QUEUED -> PROCESSING -> READY` или `FAILED`.
- Protocol/process finalization flow (`VERIFYING/COMPLETED/FINALIZED`) не сбрасывается повторной доставкой job.
- `/status` теперь возвращает блок `job`: `job_id`, `status`, `attempt_count`, `max_attempts`, `error`, `retry_reason`, `created_at`, `updated_at`, `started_at`, `finished_at`.

### Idempotency / duplicate delivery

- `enqueue_process_job()` переиспользует активный `QUEUED/PROCESSING` job для того же `process_id`, расширяя `affected_param_codes`, вместо создания дублей.
- `execute_process_job()` перед запуском проверяет terminal/completed состояния. Если процесс уже `READY/VERIFYING/COMPLETED/FINALIZED`, повторная доставка помечает job как безопасно завершённый/skipped и не пересоздаёт protocol/findings/evidence groups.
- Повторная доставка после recovery реально проверена live: после restart RabbitMQ + worker recovery один duplicate был обработан как `case10_job_duplicate_completed`, без дублей и без сброса inspector decisions.

### Retry / error handling / recovery

- Retry bounded через `CASE10_JOB_MAX_ATTEMPTS` (default 3).
- Retryable errors: transient HTTP/network/timeout/5xx/429/408; permanent errors и исчерпанный retry переводят job/process в `FAILED`.
- Permanent/failed payload публикуется в `case10.process.errors`; queue `case10.process.jobs` durable, error queue durable, DLX объявляется worker/publisher-ом.
- Worker startup делает DB-backed recovery для `QUEUED/PROCESSING` jobs, старше `CASE10_JOB_STALE_SECONDS` или ещё не стартовавших. Это at-least-once дизайн: возможные дубль-сообщения безопасны благодаря idempotency.
- Live recovery проверка: worker остановлен, start создал `process_id=b3dae063585249579c0575c583421e51`, RabbitMQ перезапущен, durable `case10.process.jobs` сохранил 1 message, затем worker startup/recovery довёл job `aa31cd8561ee4094aec249d31541bf55` до `READY`, attempts=1, очереди jobs/errors вернулись в 0.

### Structured logs

Добавлены JSON-логи с ключами `event`, `process_id`, `job_id`, `attempt`, `status_from/status_to`, `timestamp`, `error/retry_reason`, включая:

- `case10_job_published`;
- `case10_job_recovery_scan`;
- `case10_job_received`;
- `case10_job_retry`;
- `case10_job_ready`;
- `case10_job_failed`;
- `case10_job_duplicate_completed`;
- `case10_status_transition`.

### Изменённые файлы

- `.env.example` — host-порты RabbitMQ и CASE10 job/recovery env.
- `README.md` — worker-секция обновлена под CASE10 async worker.
- `docker-compose.yml` — новый `case10_worker` service, env для retry/recovery, RabbitMQ host ports через env.
- `node_gateway/openapi.json` — start/run summaries обновлены под enqueue + polling.
- `api_service/app/config.py` — `CASE10_JOB_MAX_ATTEMPTS`, `CASE10_JOB_STALE_SECONDS`, `CASE10_WORKER_RECONNECT_SECONDS`.
- `api_service/app/db/models.py` — `Case10ProcessJob`, relationship от `InspectionProcess`.
- `api_service/app/domain/v3_messaging.py` — job queue/error queue/DLX topology, durable publish helpers, structured publish logs.
- `api_service/app/domain/v3_pipeline.py` — `QUEUED/PROCESSING/FAILED`, failure status on uncaught pipeline exception, job metadata in `process_to_dict()`, structured status-transition logs.
- `api_service/app/domain/v3_jobs.py` — новый coordinator для enqueue/publish/execute/retry/recovery/idempotency.
- `api_service/app/worker_case10.py` — новый RabbitMQ consumer/worker.
- `api_service/app/api/routes_case10.py` — start/run/import/RAG/IFC recompute теперь enqueue jobs, не запускают тяжёлый pipeline в API.
- `api_service/app/api/routes_upload.py` — incremental CASE10 recompute после upload теперь enqueue job после commit.
- `api_service/tests/test_case10_api.py` — async worker/API coverage: quick start, consume-to-ready, duplicate delivery, retry/permanent failure, recovery, DB-only status, no duplicates.
- `evaluation/performance_smoke.py` — start latency + polling-to-READY + `/status` p95.
- `evaluation/smoke_node_gateway.js` — gateway smoke ждёт `READY` через polling.
- `evaluation/smoke_real_data.py` — real-data smoke переведён на Pull/polling вместо старого synchronous `READY` сразу после start.
- `evaluation/smoke_hidden_blind.py` — hidden blind smoke переведён на Pull/polling.

### Проверки

- `docker compose config` — PASS.
- `docker compose build api case10_worker node_gateway` — PASS.
- `python -m unittest discover -s tests` внутри Docker API — **59/59 OK** (`130.355s`).
- Live gateway smoke через Node gateway (`http://127.0.0.1:8080`) — PASS: `process_id=fdded48f7d5247f192f8cf207a028012`, worker довёл до `READY`, `protocol_id=121`, `matrix_params=132`, `evidence_groups=134`, `prediction_findings=134`, `submission_checks=134`.
- Live performance smoke — PASS: `start_seconds=0.242634257`, `process_seconds=23.575201081`, `protocol_seconds=0.170547431`, `submission_seconds=0.086357165`, `/status p95=0.015875992`, mean `0.011152679`, limits `start<=2.0`, `process<=150`, `api_p95<=0.25`.
- Live restart/recovery smoke — PASS: worker stop -> start job -> RabbitMQ restart -> queue message survived -> worker recovery -> `READY`; queues after recovery: `case10.process.jobs=0`, `case10.process.errors=0`.
- Live real-data smoke (`evaluation.smoke_real_data --verify-pages --verify-protocol`) — PASS on both public objects after async polling:
  - `OBJ-NOVOSLOBODSKAYA`: `start_seconds=0.126`, `status_poll_seconds=25.38`, protocol finalized, source-verified localization `1.0`, `normalized_value_and_status_accuracy=1.0`, `submission_valid=true`.
  - `OBJ-TYUMENSKAYA-5-GOLD-SEED`: `start_seconds=0.108`, `status_poll_seconds=17.28`, protocol finalized, strict/source-verified metrics unchanged from checkpoint 30, `finding_f1=1.0`, `submission_valid=true`.
- Live hidden blind smoke (`evaluation.smoke_hidden_blind`) — PASS: `start_seconds=0.128`, `status_poll_seconds=61.6`, `candidate_rate=0.0075188`, `positive_like=1`, `gold_checks created/updated=0`, `hidden_labels_used=false`.
- Final queue check after smoke: `case10.process.jobs=0`, `case10.process.errors=0`; existing status-event queue `case10.process.events` contains accumulated events and is intentionally separate from job/error handling.

### Latency / performance notes

- Start endpoint now returns quickly and never waits for heavy pipeline. Observed start latencies: `0.108-0.128s` in real-data/hidden smoke, `0.2426s` in performance smoke.
- `/status` remains DB-only and fast: measured p95 `0.0159s` in performance smoke; recovery spot-check p95 `0.0498s` on first poll after worker restart.
- Worker processing time remains within existing limits and quality smoke did not regress.

### Ограничения и оставшиеся P0

- Extraction/evidence logic, public GOLD and React не менялись.
- At-least-once recovery deliberately may republish a job whose durable RabbitMQ message also survived; this is accepted and covered by idempotency/duplicate-delivery tests and live recovery.
- `case10.process.events` still has no dedicated event consumer in this session; it is not the job queue and does not block job processing.
- Strict Tyumenskaya public-GOLD localization conflict from checkpoint 30 remains documented as before; this session did not attempt to change quality logic or GOLD.
- По scope этой сессии новых P0 для RabbitMQ async worker не осталось: тяжёлая обработка реально вынесена в worker, API не блокируется, retry/recovery/idempotency/live smoke закрыты.

## 32. React UI для CASE10-инспектора (`frontend_react`), 18.09.2026

### Цель checkpoint

RabbitMQ worker (checkpoint 31) закрыл предыдущую блокирующую зависимость, поэтому в этой сессии реализован React-shell, требуемый ТЗ (`CASE10_TECHNICAL_STRATEGY_V3_OFFICIAL_TZ.md`, п. 85/362/456/539/556/568): flow `загрузка → process_id → status polling → protocol → evidence card → решение инспектора` полностью на React, поверх существующего backend contract (`api_service` + `node_gateway`), без переписывания extraction/evidence логики.

### Архитектура

Новый сервис `react_frontend` (папка `frontend_react/`, Vite + обычный React 18, без TS/UI-фреймворков) обращается **через Node gateway** (`node_gateway`, порт 8080 по умолчанию), а не напрямую к Python API — это отдельно требовалось ТЗ как "React → Node gateway → Python API" контур в отличие от старого статического `frontend/`, который ходит в Python API напрямую на 8000.

`node_gateway` расширен минимально (белый список маршрутов), т.к. загрузка документов раньше не проксировалась:
- `node_gateway/server.js` — добавлены `POST /api/upload`, `GET /api/documents`, `DELETE /api/documents/{id}` в `allowedRoutes`, `DELETE` добавлен в `Access-Control-Allow-Methods`.
- `node_gateway/openapi.json` — добавлены те же 3 пути (multipart upload schema, list, delete).
- Никакие Python-маршруты/domain-логика не менялись.

### Структура `frontend_react/`

```
frontend_react/
  package.json, vite.config.js, index.html, Dockerfile, nginx.conf, .eslintrc.json
  src/
    main.jsx, App.jsx          — bootstrap, auth/project/process state, localStorage persistence
    api.js                     — fetch-обёртка (Bearer auth), upload через XHR (progress), все CASE10-эндпоинты
    labels.js                  — русские подписи статусов/приоритетов/reason-code, зеркально скопированы из
                                  `protocol_annex2.py` (`STATUS_SUSPICION` → "Подозрение ИИ" и т.д.), чтобы UI и
                                  PDF-протокол не расходились в терминологии
    utils.js                   — formatDelta/formatBBox/buildExplanation (client-side rationale из
                                  expected/actual/delta/comparison_scenario/fragment.context — backend не отдаёт
                                  отдельное поле "explanation", поэтому текст собирается на фронте не хардкодом
                                  под конкретный object_id/param, а по типу данных)
    styles.css                 — тёмная минималистичная тема (без анимаций/дизайн-системы), похожа на палитру
                                  старого `frontend/styles.css` для визуальной преемственности
    components/
      Login.jsx                — `/api/auth/login`
      TopBar.jsx                — проекты (`/api/projects`, создание), пользователь один раз (логин+организация),
                                  без задвоения данных на странице
      DocumentsPanel.jsx        — drag&drop + click upload (`/api/upload`, XHR progress), список документов по
                                  стадиям (`/api/case10/documents`), удаление (`/api/documents/{id}`), кнопка
                                  «Начать проверку» (`POST .../processes`); отдельный "watchIndexing" опрос
                                  подтверждает, что файл реально появился в реестре после фоновой обработки, и
                                  переводит запись в "Задерживается" вместо бесконечного "Загружен", если этого
                                  не произошло за ~30 c (см. "Проверки" ниже)
      ProcessPanel.jsx          — селектор истории проверок (`GET /processes`) + "Новая проверка", polling
                                  `GET /processes/{id}/status` (2 c, только пока QUEUED/PROCESSING/PARSING),
                                  completeness-чипы ПД/РД/ИД, PENDING/FAILED-баннеры с ручным перезапуском
                                  (`POST /processes/{id}/run`), протокол (`GET /protocols/current`), статистика
                                  находок отдельно от "Подозрения ИИ" (SUSPICION/`FREE_SEARCH` фильтруются в
                                  отдельную секцию, как и в `protocol_annex2.annex2_payload`), фильтр-чипы по
                                  finding_status, финализация через собственное модальное окно (не
                                  `window.confirm` — не работает в headless/автоматизированных контекстах и
                                  не совпадает по стилю с остальным UI), экспорт PDF/submission
      EvidenceCard.jsx          — parameter/rule (matrix_code/scoring_code), expected/actual/delta,
                                  review_priority, per-fragment PD/РД/ИД bbox/страница/revision/approval,
                                  «Показать на странице (bbox)» → PageViewerModal, decision log, 3 действия
                                  инспектора
      DecisionDialog.jsx        — Reject (reason_code select + опциональный comment) / Clarification Required
                                  (обязательный comment на UI-уровне; backend их не требует, но без текста
                                  уточнение бессмысленно для исполнителя)
      PageViewerModal.jsx       — `GET /evidence-fragments/{id}/page.png` (blob, Bearer auth) — bbox/polygon
                                  overlay рендерится тем же backend-эндпоинтом, что и в старом UI; фронт не
                                  рисует overlay сам
      Badge.jsx                 — маленький переиспользуемый pill/badge
```

### Что перенесено из старого static UI (`frontend/`), что нет

Перенесено (переиспользован backend contract 1:1, но не JS-код): паттерн `Bearer`-токена в `localStorage`, авто-детект API base через `?api=` query, XHR с `upload.onprogress` для прогресс-бара, набор русских статус-лейблов (`FINDING_STATUS_LABELS`, `PROCESS_STATUS_LABELS`) и цветовая классификация приоритетов.

Сознательно НЕ перенесено в `frontend_react` (осталось только в старом `frontend/`, который не удалён и продолжает работать на порту 3000): AI-чат/RAG-ассистент, реестр `CanonicalEntity`/Object Portrait, Change Engine таблица изменений, редактор матрицы параметров, админ-экран, IFC-синхронизация. Причина — эта сессия закрывает именно обязательный ТЗ-flow "инспектор проверяет CASE10-параметры", а не весь функционал старого MVP; расширять React на остальные экраны — отдельная задача не в рамках этой сессии (ограничение "не тратить сессию на дизайн-систему/лишний функционал").

### Docker / deploy

- `docker-compose.yml` — новый сервис `react_frontend` (`frontend_react/Dockerfile`, multi-stage node-build → nginx), порт `REACT_FRONTEND_HOST_PORT` (по умолчанию 3100), `depends_on: node_gateway`. `VITE_API_BASE` — необязательный build-time override; по умолчанию пусто, и фронт сам резолвит gateway как `${protocol}//${hostname}:8080` в рантайме (переносимо между хостами).
- `docker-compose.prod.yml` — добавлен `react_frontend: restart: unless-stopped`. **Не подключен** к публичному `nginx`/`app.docaibuild.ru` — тот по-прежнему проксирует на legacy `frontend`. Это осознанно оставлено как открытый вопрос (см. "Ограничения").
- `.env.example` — `REACT_FRONTEND_HOST_PORT=3100`, `REACT_FRONTEND_API_BASE=`.
- `.claude/launch.json` — конфиг для локального dev-сервера (`npm --prefix frontend_react run dev`, порт 5173), использован для live-проверки в этой сессии.

### Проверки

- `npm install` + `npm run build` (Vite) — PASS, бандл 176 KB JS / 8 KB CSS (gzip ~57 KB / ~2.4 KB).
- `npm run lint` (ESLint, react/react-hooks плагины) — PASS, 0 ошибок/предупреждений.
- `docker compose config` — PASS (новый сервис синтаксически корректен).
- `node_gateway` пересобран (`docker compose build node_gateway` + `up -d --no-deps node_gateway`, с `--no-deps` по правилу этой машины) и живой `/health` + `/openapi.json` подтвердили новые маршруты.
- **Backend regression**: `docker exec ... python -m unittest discover -s tests` — **59/59 OK** (27.7 с) после копирования актуальных тестов в контейнер (Python-код в этой сессии не менялся, тест подтверждает, что contour не деградировал).
- **Живая ручная проверка через Browser pane** (dev-сервер на 5173 → `node_gateway` на 8080 → живой `api`/`case10_worker`/`rabbitmq`, реальные project'ы из прошлых checkpoint'ов с официальным датасетом):
  - login (`admin`/`Default`) → список проектов → создание проверки (`+ Новая проверка`) → **живой** `QUEUED → PARSING → READY` через настоящий RabbitMQ worker, с process_id, "Попытка N из M";
  - completeness-чипы корректно показали смешанное состояние (`ПД: загружена`, `РД: загружена`, `ИД: отсутствует`) на реальном `OBJ-TYUMENSKAYA-5-GOLD-SEED` процессе;
  - evidence card на реальном `CANDIDATE` (`M-078/IOS4-078`, "Сечения и геометрия воздуховодов") показала настоящее расхождение `V2.7, V2.8, V2.9` (ПД) vs `V2.8, V2.9, V2.10` (РД), explanation-текст, OCR-контекст, bbox/revision/approval per fragment;
  - «Показать на странице (bbox)» открыл настоящий отрендеренный лист РД (`АНО-150321-1-РД-ОВ1 изм. 4_в1 (1).pdf`, стр. 18) через `evidence-fragments/{id}/page.png`;
  - все 3 действия инспектора (Confirm/Reject/Clarification Required) выполнены на реальных `MISSING_EVIDENCE`-карточках, включая повторное решение (Confirm → Reject) с полной историей решений в карточке; счётчики/версия протокола (`v1→v4`) обновлялись после каждого решения;
  - Finalize (собственное модальное окно, не `window.confirm`) перевёл процесс в `FINALIZED`, карточки стали read-only (кнопки действий скрыты), баннер "Протокол финализирован. Доступен только просмотр." показан, попытка повторного решения на backend осталась заблокированной (409, покрыто существующим `test_v3_evidence_protocol_inspector_flow`);
  - скачивание PDF и submission JSON протокола — оба вернули 200 OK через gateway;
  - **reload/повторное открытие процесса**: полная перезагрузка страницы восстановила токен, проект, process_id и состояние (v4, все решения, статус) — сессия/процесс не теряются;
  - **upload end-to-end**: реальный PDF (сконструирован через `DataTransfer`/`File` API, т.к. built-in browser не даёт водить нативный OS file-picker) успешно дошёл до `POST /api/upload` через gateway; поскольку `rag`-контейнер не был поднят в этой сессии (не часть RabbitMQ/React scope), фоновая индексация не завершилась — вместо бесконечного "Загружен" UI honestly перешёл в статус "Задерживается" с пояснением через ~30 c опроса реестра документов (`watchIndexing`), что и было целью этого механизма (требование "не оставлять бесконечные spinner'ы").
- Визуальная проверка на 800×600 и 1440×900 — задвоений данных и коллизий layout не найдено (юзер показан один раз в TopBar, project — один раз в селекторе).

### Ограничения и оставшиеся P0/P1

- **P1**: автоматических JS-тестов (unit/e2e) для `frontend_react` нет — верификация в этой сессии только ручная через живой браузер против реального backend. Стоит добавить хотя бы Vitest+RTL smoke на ProcessPanel/EvidenceCard в следующей сессии.
- **P1**: ветка `FAILED`-процесса (баннер ошибки + кнопка "Повторить проверку" → `POST /run`) реализована симметрично остальным статусам, но не воспроизведена живым сбоем в этой сессии (специально уронить worker не пытались, чтобы не задеть общий docker-compose стенд).
- **P1**: `docker-compose.prod.yml`/публичный nginx по-прежнему указывают на legacy `frontend` для `app.docaibuild.ru`; нужно решение — заменить на `react_frontend` или выдать отдельный поддомен/путь. Не решалось в этой сессии как выходящее за рамки "React-shell для demo flow".
- Известное существующее ограничение backend (не создано и не исправлено в этой сессии): `infer_document_stage()` определяет ПД/РД/ИД только по токенам в имени файла — для датасетов с другими соглашениями об именовании (например, `V2_01-09-00-01-14_Том 9.1.pdf`) стадия остаётся `unknown`. UI смягчает это подсказкой у формы загрузки, но не исправляет детекцию (это относится к extraction-логике, трогать которую в рамках React-сессии не требовалось).
- Legacy статический `frontend/` (vanilla JS, порт 3000) не удалён и не изменён — оба фронтенда сосуществуют; `frontend_react` (порт 3100 по умолчанию) закрывает именно обязательный ТЗ-flow инспектора.

## 14. Checkpoint 33, 19.09.2026 — Verification lifecycle state machine (CANDIDATE → VERIFICATION_COMPLETED → PROTOCOL_FINALIZED) + Unfinalize

### Задача сессии

Замкнуть жизненный цикл инспекторской верификации как единый, централизованно валидируемый state machine: `CANDIDATE` → `{CONFIRMED_VIOLATION, NEGATIVE_VERIFIED, CLARIFICATION_REQUIRED}` → `VERIFICATION_COMPLETED` (= `InspectionProcess.status == COMPLETED`, существовавший статус) → `PROTOCOL_FINALIZED` (= `Protocol.status == FINALIZED` + `InspectionProcess.status == FINALIZED`), плюс запрет изменений после финализации и controlled unfinalize с обязательным аудитом.

### Что уже было готово до этой сессии (проверено, не переделывалось)

`app/domain/v3_pipeline.py` уже содержал почти весь V3-каркас: `FINDING_STATUSES` (`CANDIDATE/CONFIRMED_VIOLATION/NEGATIVE_VERIFIED/MISSING_EVIDENCE/NOT_APPLICABLE/NOT_COMPARABLE/CLARIFICATION_REQUIRED/SUSPICION`), `PROCESS_STATUSES` (…`READY/VERIFYING/COMPLETED/FINALIZED`), `record_inspector_decision`/`finalize_protocol`/`create_protocol_version`, атомарную запись решения (status+user_id+timestamp+comment+reason_code+audit в одной транзакции), запрет Reject без reason/comment (422), блокировку решений после `PROCESS_FINALIZED` (409), автоматический пересчёт `VERIFYING`/`COMPLETED` по числу оставшихся `CANDIDATE` (`_update_process_after_decision`), `AuditLog` с `INSPECTOR_DECISION`/`PROTOCOL_FINALIZED`/`PROCESS_STATUS`, версии protocol/matrix/dataset/model/input_manifest_hash на каждой `Protocol`-строке. GOLD-набор (`training_release.py`) уже фильтровал `CANDIDATE/SUSPICION/MISSING_EVIDENCE/CLARIFICATION_REQUIRED/NOT_COMPARABLE` из обучающих меток — не трогалось.

### Реально найденные и закрытые дыры (проверено forensically, не по памяти прошлых чекпоинтов)

1. **`finalize_protocol` не проверял оставшиеся `CANDIDATE`.** Раньше можно было финализировать протокол, даже если инспектор принял решение только по части находок (два существующих теста, `test_v3_evidence_protocol_inspector_flow` и `test_protocol_annex_history_audit_and_stale_finalize`, реально это делали и "проходили" только потому, что гварда не было — оба переписаны, см. ниже). Теперь `finalize_protocol` считает `pending_candidate_count()` напрямую по БД (не по кэшированному `process.status`, у которого есть окно `READY` с ещё не пересчитанным статусом до первого решения) и кидает `409` со списком/числом незакрытых `CANDIDATE`, если есть хоть один.
2. **Не было централизованной валидации finding-level переходов.** `record_inspector_decision` раньше принимала решение по evidence group в *любом* текущем `finding_status`, включая `MISSING_EVIDENCE`/`NOT_APPLICABLE`/`NOT_COMPARABLE`/`SUSPICION` — то есть технический статус можно было напрямую превратить в `CONFIRMED_VIOLATION`, что прямо запрещено ТЗ ("не превращать автоматически в нарушения"). Добавлена `FINDING_DECISION_TRANSITIONS`/`validate_finding_transition()`: решения разрешены только между `{CANDIDATE, CONFIRMED_VIOLATION, NEGATIVE_VERIFIED, CLARIFICATION_REQUIRED}` (включая повторное решение любое количество раз до финализации); попытка решить `MISSING_EVIDENCE`/`NOT_APPLICABLE`/`NOT_COMPARABLE`/`SUSPICION` → `409`.
3. **Unfinalize не существовал вообще** — ни endpoint, ни domain-функция, ни RBAC. Добавлены `unfinalize_protocol()` в `v3_pipeline.py` и `POST /api/case10/protocols/{protocol_id}/unfinalize` (body: `{"reason": str}`, обязателен, иначе 422); допустимо только если `protocol.status == "FINALIZED"` (иначе 409). После unfinalize: `protocol.status → "DRAFT"`, `process.status → COMPLETED` (= `VERIFICATION_COMPLETED` по ТЗ), `process.finalized_at/finalized_by_user_id` очищаются, а `protocol.finalized_at/finalized_by_user_id` **сохраняются** как исторический факт (кто/когда финализировал в прошлый раз), плюс новые `unfinalized_at/unfinalized_by_user_id/unfinalize_reason` на той же строке `Protocol`. Пишется `AuditLog(action="PROTOCOL_UNFINALIZED", details={protocol_id, version, reason, previous_finalized_at, previous_finalized_by})`. После unfinalize решения снова редактируемы (проверено и тестами, и живьём в браузере).
4. **RBAC не существовал для unfinalize** (не было даже понятия "supervisor" — только `User.is_admin`). Добавлена `User.role` (`INSPECTOR|ADMIN|ML_ENGINEER|SUPERVISOR`, default `INSPECTOR`, additive column) и `require_supervisor()` в `app/api/auth.py` (`is_admin OR role in {ADMIN, SUPERVISOR}`). Endpoint unfinalize использует `Depends(require_supervisor)` → обычный inspector получает 403.
5. **`finalized_by`/`unfinalized_by` не были отдельными полями** — раньше `finalize_protocol` писал `finalized_by` только внутрь `payload_json` (JSON blob), не как индексируемую колонку. Добавлены `Protocol.finalized_by_user_id`, `InspectionProcess.finalized_by_user_id` (плюс уже существовавшие `finalized_at`).

### Изменённые файлы (backend)

- `api_service/app/db/models.py` — `User.role`; `Protocol.finalized_by_user_id/unfinalized_at/unfinalized_by_user_id/unfinalize_reason`; `InspectionProcess.finalized_by_user_id`.
- `api_service/app/db/session.py` — additive SQLite `_sqlite_ensure_column` для всех новых полей (+ `UPDATE users SET role='INSPECTOR' WHERE role IS NULL`), соответствующие Postgres `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, новый `schema_migrations` marker `20260919_case10_verification_lifecycle`, bootstrap-админ теперь получает `role="ADMIN"` явно.
- `api_service/app/api/auth.py` — `SUPERVISOR_ROLES`, `is_supervisor()`, `require_supervisor()`.
- `api_service/app/schemas.py` — `role` в `MeOut`/`UserOut`/`UserCreate`/`UserBatchCreate` (pattern-валидация на 4 значения).
- `api_service/app/api/routes_admin.py`, `routes_me.py`, `routes_auth.py` — `role` прокинут в создание пользователя и в ответы `/me`, `/auth/login`.
- `api_service/app/domain/v3_pipeline.py` — `DECIDABLE_FINDING_STATUSES`, `FINDING_DECISION_TRANSITIONS`, `validate_finding_transition()`, `pending_candidate_count()` (вынесен из `_update_process_after_decision`, переиспользован в `finalize_protocol`), гварда незакрытых `CANDIDATE` и `finalized_by_user_id` в `finalize_protocol()`, новая `unfinalize_protocol()`, `finalized_by_user_id`/`unfinalized_*` в `protocol_to_dict()`, `finalized_by_user_id`/`pending_candidates` в `process_to_dict()`.
- `api_service/app/api/routes_case10.py` — `UnfinalizeIn`, endpoint `POST /case10/protocols/{protocol_id}/unfinalize` (`Depends(require_supervisor)`).
- `node_gateway/server.js`, `node_gateway/openapi.json` — новый маршрут добавлен в allowlist (без этого React UI получал "Route is not exposed by the CASE10 Node gateway" — найдено и исправлено живой проверкой через браузер, не предположением).

### Изменённые/новые тесты

- `api_service/tests/test_case10_api.py` — `test_v3_evidence_protocol_inspector_flow` и `test_protocol_annex_history_audit_and_stale_finalize` переписаны: раньше оба финализировали протокол, решив только 1 из 3 `CANDIDATE` (проходили только из-за дыры №1 выше); теперь оба явно проверяют `409` на преждевременной финализации, затем закрывают все `CANDIDATE` (используя путь `CLARIFICATION_REQUIRED` для последнего — прямая проверка требования ТЗ "позволить CLARIFICATION_REQUIRED"), и только потом финализируют успешно.
- `api_service/tests/test_case10_verification_lifecycle.py` — новый файл, 14 тестов, admin/inspector/supervisor фикстуры с переключаемым `current_user`: разрешённые переходы + повторное решение; запрещённый переход на `MISSING_EVIDENCE`/`NOT_APPLICABLE`; Reject без reason/comment (422); finalize при незакрытых `CANDIDATE` (409, `process.status` остаётся `VERIFYING`, `protocol.status` — `DRAFT`); finalize после закрытия всех (200, `finalized_by_user_id` корректен); мутации после finalize (decisions 409, `/run` 409, протокол не меняется); обычный inspector не может unfinalize (403); unfinalize без причины (422); unfinalize не-финализированного протокола (409); supervisor unfinalize → `DRAFT`/`COMPLETED`, дальше admin переигрывает решение; audit consistency (все обязательные actions и поля присутствуют); повторный identical finalize идемпотентен (один `PROTOCOL_FINALIZED` в audit, тот же `finalized_at`); повторное идентичное decision не портит состояние (2 записи `EvidenceDecision`, финальный статус корректен); конкурирующие решения одного finding (inspector Confirm → supervisor Reject) — last-write-wins, оба решения в истории с правильными `user_id`.
- **Backend regression**: `unittest discover` из корня репозитория (нужно для относительных путей `learning_data/...` в датасет-зависимых тестах) — **73/73 OK** (59 существовавших + 14 новых), `python -m compileall api_service` — success, `import app.main` — success.

### Живая проверка через React UI (не только API/unit-тесты)

Поднят `api_service` (uvicorn, throwaway SQLite в scratch-директории — **не** трогает боевую `data/api.db` этой сессии) + `node_gateway` (порт 8080) + `frontend_react` dev-сервер (`.claude/launch.json`, порт 5173) через встроенный Browser pane:

- login admin → создание проекта → synthetic dataset (посеян напрямую через API, т.к. React-flow ожидает реальные PDF, а не кнопку synthetic — это фича старого `frontend/`) → вкладка "Проверка" показала 3 `CANDIDATE`;
- клик "Финализировать протокол" при 3 незакрытых `CANDIDATE` → модалка честно показала предупреждение и **задизейблила** кнопку "Финализировать" (frontend-фикс в этой сессии — раньше кнопка была активна всегда, дизейбл добавлен на `sections.candidates > 0`, зеркалит backend-гварду);
- 3 решения через реальный UI: `Требуется уточнение` (thickness, с текстом), `Подтвердить нарушение` (fire_resistance), `Нарушения нет` (material, с reason_code select) — все прошли через `evidence-groups/{id}/decisions` → `evidence_group_to_dict` → карточка обновилась;
- после закрытия всех `CANDIDATE` кнопка "Финализировать" разблокировалась, финализация прошла (`v4`, статус "Протокол финализирован", read-only баннер);
- новая кнопка "Отменить финализацию" (видна только admin/supervisor) → модалка с обязательной причиной → **первая попытка вернула ошибку "Route is not exposed by the CASE10 Node gateway"** (реальный найденный баг: gateway allowlist не знал о новом маршруте) → добавлен маршрут в `node_gateway/server.js`+`openapi.json`, gateway перезапущен, повторная попытка — 200 OK;
- после unfinalize: статус вернулся в "Проверено инспектором", баннер и кнопка unfinalize исчезли, решения снова редактируемы (проверено кликом на уже решённую карточку — кнопки Confirm/Reject/Clarification снова активны);
- **проверено через полный reload страницы**: process_id, версия протокола (`v4`, не создалась новая версия при unfinalize), статус `COMPLETED`, decisions — всё восстановилось из бэкенда, не из localStorage-кэша.

### Изменённые файлы (frontend_react)

- `src/App.jsx` — `me` теперь прокидывается в `ProcessPanel`.
- `src/api.js` — `unfinalizeProtocol(protocolId, reason)`.
- `src/components/ProcessPanel.jsx` — `canUnfinalize` (`me.is_admin || me.role in {ADMIN,SUPERVISOR}`); кнопка "Отменить финализацию" в finalize-баннере (только для admin/supervisor) со своей модалкой (обязательное поле причины, `<label className="field">`/`<textarea className="textarea">` — тот же паттерн, что в `DecisionDialog.jsx`); кнопка "Финализировать" в модалке подтверждения теперь задизейблена, если остались `CANDIDATE` (раньше только предупреждала текстом, но разрешала клик); `setStatusError('')` в начале `confirmFinalize`/`confirmUnfinalize`, чтобы старая ошибка не "залипала" поверх следующего успешного действия (найдено живой проверкой — после неудачной первой попытки unfinalize из-за gateway-бага баннер оставался висеть даже после успешной второй попытки).
- Проверки: `vite build` — success (177 KB JS/8 KB CSS gzip), `eslint src` — 0 warnings/errors.

### Известные ограничения / что не делалось в этой сессии

- **RBAC остаётся минимальным**: `User.role` — просто строковая колонка без отдельной таблицы прав/permission matrix; различие `ADMIN` vs `SUPERVISOR` существует только для проверки "кто может unfinalize" (оба проходят `require_supervisor`). Полноценная ролевая модель с ML_ENGINEER-специфичными правами не реализована — не требовалась в рамках ТЗ-требования "unfinalize только для admin/supervisor".
- **`ip_address`/`user_agent` в `AuditLog`** — колонки существуют в модели с предыдущих чекпоинтов, но `add_audit()` их не заполняет (не было request context в сигнатуре). Не трогалось в этой сессии — не входило в явный чек-лист задачи (там про user_id/timestamp/comment/reason_code/audit record, IP/UA не упомянуты), а протаскивание `Request` через все call-sites `add_audit()` — отдельный, более широкий рефакторинг.
- **RabbitMQ/worker** не трогался (по явному ограничению задачи "если RabbitMQ не готов — не реализуй его здесь"; он был готов с checkpoint 31, и эта сессия его не меняла).
- **Concurrency-тест "конкурирующие решения"** реализован как последовательные HTTP-вызовы (last-write-wins с полной историей в `EvidenceDecision`/`AuditLog`), а не как настоящий multi-threaded race — `record_inspector_decision` не использует optimistic locking/`SELECT … FOR UPDATE`. Для текущего масштаба (один инспектор решает одну находку за раз через UI) это осознанно достаточно; истинная гонка потребовала бы отдельного нагрузочного теста и, вероятно, версионирования `EvidenceGroup`.
- Побочно исправлен реальный интеграционный баг: `node_gateway` route allowlist не пропускал новый unfinalize-эндпоинт — без живой UI-проверки это осталось бы незамеченным до первого реального использования кнопки.
- Локальная боевая база `D:\Proga\LCT-hack-2026\data\api.db` была один раз пересчитана штатным `init_db()` (тем же кодом, что выполняется на каждом старте API) для проверки миграции на реальной схеме — только additive `ALTER TABLE`, ни одна строка не удалена/изменена (кроме косметического `role='ADMIN'` для уже существующего bootstrap-админа); `protocols`/`inspection_processes` в этой базе были пусты на момент проверки.

## 33. Checkpoint 34, 19.09.2026 — настоящий impact-based incremental recompute (дозагрузка)

### Задача сессии

До этой сессии "инкрементальный" пересчёт (`affected_param_codes` + `enqueue_process_job`, добавлены в checkpoint 31 для upload/RAG/IFC-sync триггеров) был **скрытым full rerun на уровне параметра**: `run_process` сужал список параметров, но `_delete_existing_groups` перед пересчётом безусловно удалял ВСЕ `EvidenceGroup` строки этих параметров, а `EvidenceDecision` каскадно удалялась вместе с группой (`cascade="all, delete-orphan"`) — то есть любой incremental-триггер молча стирал решения инспектора по затронутому параметру, даже если конкретная находка не изменилась. Задача сессии — реализовать настоящий impact-based recompute по ТЗ: пересчитывать только реально затронутые evidence groups, не сбрасывать decision history без причины, корректно обрабатывать revisions, версионировать protocol с diff, не делать повторную работу на дубликатах, и защищать finalized protocol.

### Найденные при forensic-разборе реальные дыры (не по памяти, проверено чтением кода)

1. **Отсутствие identity у EvidenceGroup поверх param_id.** Официальный (`official-rule-packs`) путь создаёт много групп на один параметр (по `location`), но `canonical_entity_id` для них всегда `NULL` — единственный "ключ" был сам факт `_delete_existing_groups` + пересоздание с нуля. Без стабильного `group_key` невозможно было отличить "та же находка, свежий прогон" от "новая находка".
2. **`extract_official_rule_observations`/`create_official_evidence_groups` не фильтровали obsolete revisions.** Функции получали ВСЕ `DocumentVersion` проекта (включая заменённые `predecessor`-строки из `_link_revision_chain`), без учёта `successor_id`/`approval_status` — новая утверждённая ревизия не гарантированно "побеждала" старую как эталон.
3. **`run_process`'s target_codes computation использовал `or`-chaining** (`affected_param_codes or process.affected_param_codes or []`): явно переданный пустой список (`[]`, означающий "полный прогон") неотличим от "не передано, взять из процесса", а `process.affected_param_codes` никогда не сбрасывался после потребления — один incremental-триггер мог навсегда "залипать" как невидимое сужение для всех последующих `/run`-вызовов без явного `affected_param_codes`.
4. **Дубликат-загрузка не распознавалась.** `ensure_document_version_for_upload_job` матчил "existing" только по `(source_type, source_document_id, filename)` — при повторной RAG-индексации того же файла `source_document_id` меняется, поэтому побайтово идентичный re-upload создавал новую `DocumentVersion` строку и запускал полноценный incremental job, хотя контент не менялся.
5. **`extract_official_rule_observations` не принимала scope параметров** — даже при сужении `params` в `run_process` она всё равно сканировала/рендерила страницы для всех 5 официальных rule-pack кодов, так что incremental-прогон по одному параметру платил ту же цену OCR/page-rendering, что и full run.

### Impact set: что считается затронутым

`affected_param_codes_for_documents(db, process, documents)` (существовала с checkpoint 31, не менялась по семантике) остаётся основным фильтром: параметр затронут, если хотя бы один из new/changed документов покрывает нужную ему стадию (`source_pd`/`source_rd`/`source_id` на `Param`). Матрица не хранит per-document_code/discipline таргетинг на уровне параметра, поэтому более глубокая фильтрация (конкретный `document_code`) физически невозможна без расширения схемы матрицы — это сознательно не делалось (не входило в чек-лист, отдельная задача "матрица параметров").

Новая `impact_scope_for_documents(db, process, documents)` — **описательный**, не фильтрующий слой поверх той же функции: возвращает `{"param_codes": [...], "documents": [{document_version_id, object_id, stage, discipline, document_code, revision, approval_status, predecessor_id, successor_id, content_hash}, ...]}`. Используется в трёх точках enqueue (upload-triggered recompute, RAG source-fragment sync, IFC observation sync) и прокидывается в `enqueue_process_job(..., impact_scope=...)`, которая складывает его в `Case10ProcessJob.payload_json["impact_scopes"]` (список — job reuse аккумулирует несколько impact scope из смёрженных триггеров) — это и есть "job содержит impact scope" из раздела ТЗ про очередь.

### Impact-scoped extraction без хардкода: `EvidenceGroup.group_key` + upsert engine

Новый модуль `api_service/app/domain/evidence_groups.py` — единая точка правды для всех трёх производителей evidence (`official_evidence.py`'s rule-pack + fallback пути, и demo-путь в `v3_pipeline.py`):

- `compute_basis_hash(expected_value, actual_value, delta, fragment_specs)` — фингерпринт "сырых" входов находки: значения сравнения + identity/value каждого подтверждающего фрагмента (document_version_id, source_fragment_id, page, extracted_value, role, file_sha256, revision). Confidence/review_priority и прочие некритичные поля не входят в хэш — их дрейф не считается изменением evidence basis.
- `upsert_evidence_group(db, process, param, group_key, fields, fragment_specs)` — ищет существующую группу по `(process_id, param_id, group_key)`:
  - хэш совпал → `(None, False)`: группа вообще не трогается (ни decision, ни `updated_at`, ни fragments) — это то, что делает повторный/полный прогон без реальных изменений настоящим no-op, а не скрытым full rerun;
  - хэш другой, решения нет → полный refresh полей + fragments на месте (тот же `id`);
  - хэш другой, решение(я) есть → **decision и `finding_status` не трогаются**; новые вычисленные значения кладутся в `delta.pending_reverification`, `needs_reverification=True`, `basis_changed_at=now`, пишется `AuditLog(action="EVIDENCE_BASIS_CHANGED")`. Повторный прогон с ТЕМ ЖЕ пересчитанным несовпадением идемпотентен (сравнение с уже сохранённым `pending_reverification` — не плодит повторный audit/apis).
- `sweep_orphaned_evidence_groups(db, process, param_ids, touched_keys)` — после прохода по целевым параметрам: группа, чей `group_key` не был воспроизведён в этом прогоне, удаляется, если решения нет (безопасно — терять нечего), либо помечается `needs_reverification=True` с `delta.evidence_no_longer_found=True`, если решение есть (не удаляется).

`group_key` по путям: rule-pack — `f"loc:{location}"` (несколько групп на параметр); fallback-путь `create_official_evidence_groups` — константа `"annotation"` (одна группа на параметр в этой ветке, как и раньше); demo-путь — `f"entity:{entity.id}"`/`"entity:none"` для `_create_evidence_group_for_param`, `"not_applicable"` для `_create_not_applicable_group`.

`run_process` (`api_service/app/domain/v3_pipeline.py`) больше не вызывает blanket-delete: `_delete_existing_groups` удалена целиком (не используется нигде), оба пути (official/demo) строят `touched_keys: dict[param_id, set[group_key]]` по ходу и в конце вызывают `sweep_orphaned_evidence_groups` только по `{param.id for param in params}` — то есть параметры вне scope вообще не трогаются DB-запросами, не только по содержимому.

### Revisions: новая утверждённая заменяет старую

`current_document_versions(docs)` (новая, `v3_pipeline.py`) — исключает документ, если его `successor_id` указывает на документ, присутствующий в ТОМ ЖЕ наборе (цепочка `predecessor_id`/`successor_id` устанавливается `_link_revision_chain` при повторной загрузке файла с совпадающим `document_stage`+`document_code`). Для pre-seeded official dataset документов `successor_id` никогда не заполняется (`official_dataset.py` не строит цепочки) — функция для них no-op, официальный quality-контур не задет. `run_process` считает `extraction_docs = current_document_versions(docs)` и передаёт его (не сырой `docs`) в `create_official_evidence_groups`, а следовательно — и в `extract_official_rule_observations`, и в bounded-fallback discovery (`find_confirming_fallback_observation`, чей `documents` параметр приходит из того же extraction_context). Demo-путь получает его же перед `current_applicable_documents` (которая и раньше выбирала максимальную revision по стадии среди `_is_approved` документов — теперь на входе уже нет "тени" от явно заменённых документов).

### Protocol versioning: история + человекочитаемый diff

`create_protocol_version` не менялась по механике версии (инкремент `Protocol.version`, старые строки никогда не трогаются/не удаляются — история уже была). Добавлено: `payload_json["diff"]` — сравнение текущего списка `findings` (по `id` = `EvidenceGroup.id`) с payload ПРЕДЫДУЩЕЙ версии: `{baseline, added: [...], updated: [...], removed: [...], unchanged: [...], needs_reverification: [...]}`, где `updated` — находки, у которых изменился `finding_status`/`needs_reverification`/`expected`/`actual`/`delta`. Первая версия процесса помечена `baseline=true`. Это делает версионный diff не "версия N vs N-1 numbers", а конкретным списком ID для UI/аудита.

### Duplicate upload/hash — no rework

`document_versions.ensure_document_version_for_upload_job` теперь возвращает `(DocumentVersion, changed: bool)` вместо голого объекта:

- если найден "existing" по `(project, org, source_type, source_document_id, filename)` (тот же upload job переигрывается, напр. retry) — `changed = existing.content_hash != new_hash`;
- иначе новая `find_current_version_by_hash(db, project_id, organization_id, document_stage, document_code, content_hash)` ищет текущую (`successor_id IS NULL`) версию той же document family с ТЕМ ЖЕ хэшем — если найдена, возвращается она (`changed=False`), новая строка `DocumentVersion` вообще не создаётся;
- иначе — обычное создание новой ревизии + `_link_revision_chain` (без изменений).

`routes_upload.py::_process_upload_job` вызывает `_recompute_case10_for_uploaded_document` (impact set + `enqueue_process_job`) **только если `changed=True`**; для дубликата пишется `AuditLog(action="CASE10_DUPLICATE_UPLOAD_SKIPPED")` и понятный `job.detail`, CASE10-пересчёт не запускается вообще (ни для affected, ни тем более для всех параметров).

### Finalized protocol защищён (без изменений в поведении, только явно протестировано)

`run_process`/`enqueue_process_job` уже кидали 409 на `PROCESS_FINALIZED` (checkpoint 31/33) — в этой сессии это поведение не менялось, но добавлен явный тест, что (а) вызов `run_process` на финализированном `process_id` даёт 409 даже при явном `affected_param_codes`, и (б) `get_or_create_open_process` после финализации создаёт **новый** `process_id`/протокол, а не "дозагружает" в старый — то есть дозагрузка "в тот же protocol" структурно невозможна, а не просто запрещена проверкой статуса.

### Производительность: incremental дешевле full, не только по числу записанных строк

`official_rule_packs.extract_official_rule_observations` получила `only_codes: set[str] | None` — при заданном множестве (`create_official_evidence_groups` всегда передаёт `{param.code for param in params} & SUPPORTED_RULE_CODES`, то есть ИМЕННО текущий scope) все стадии, которые раньше сканировали кандидатные страницы/рендерили/гоняли OCR для всех 5 официальных rule-pack кодов, теперь физически не трогают страницы кодов вне `only_codes` — `pages_by_document`/`snapshots`/ventilation-парсинг строятся только для активных кодов. При full run (`params` = все активные) `only_codes` покрывает все 5 кодов — поведение идентично прежнему, регрессии на качественном контуре (checkpoint 29/30 метрики) нет. Тест `ImpactScopedExtractionIsCheaperThanFullTests` детерминированно (через mock `extract_original_pages`) проверяет, что scoped-прогон не рендерит страницы документов, чьи коды вне scope — это и есть "не скрытый full rerun", а не просто более узкий набор записанных EvidenceGroup.

### `record_inspector_decision`: закрытие цикла re-review

Когда инспектор принимает НОВОЕ решение по группе с `needs_reverification=True`, новая `_apply_pending_reverification(db, group)` переносит `delta.pending_reverification` (expected/actual/delta) в реальные поля группы, снимает флаг, и пересчитывает `evidence_basis_hash` относительно уже сохранённых в БД fragments (single-cycle лаг по bbox/страницам самих fragments — задокументированное, не блокирующее ограничение: следующий реальный recompute перезапишет fragments штатно, если basis снова разойдётся). `INSPECTOR_DECISION` audit получил поле `reverified_stale_evidence: bool`.

### Изменённые/новые файлы

- `api_service/app/domain/evidence_groups.py` — новый модуль (upsert engine, описан выше).
- `api_service/app/domain/official_evidence.py` — `create_official_evidence_groups`/`_create_rule_groups` (переименована в `_upsert_rule_groups`) переписаны на upsert + touched_keys + sweep; `only_codes` прокинут в extraction.
- `api_service/app/domain/official_rule_packs.py` — `extract_official_rule_observations` получила `only_codes`.
- `api_service/app/domain/v3_pipeline.py` — `current_document_versions`, `impact_scope_for_documents`, `_apply_pending_reverification`, `_fragment_row_fingerprint`, `_protocol_diff`; `run_process` переписан (no blanket delete, target_codes `is not None` семантика, `process.affected_param_codes` сброс после успешного прогона, extraction_docs фильтрация); `_create_evidence_group_for_param`/`_create_not_applicable_group` переписаны на upsert (возвращают `(group_key, group|None)`); `record_inspector_decision` вызывает `_apply_pending_reverification`; `create_protocol_version` считает и сохраняет `diff`; `evidence_group_to_dict` отдаёт `group_key`/`needs_reverification`/`basis_changed_at`; удалена мёртвая `_delete_existing_groups`.
- `api_service/app/domain/document_versions.py` — `find_current_version_by_hash`; `ensure_document_version_for_upload_job` возвращает `(doc, changed)`.
- `api_service/app/domain/v3_jobs.py` — `enqueue_process_job` принимает опциональный `impact_scope`, аккумулирует его в `payload_json["impact_scopes"]` при reuse активного job.
- `api_service/app/api/routes_upload.py` — оба call site `ensure_document_version_for_upload_job` распакованы в `(doc, changed)`; recompute только при `changed=True`; `AuditLog(action="CASE10_DUPLICATE_UPLOAD_SKIPPED")` иначе; `impact_scope_for_documents` вместо голого `affected_param_codes_for_documents`.
- `api_service/app/api/routes_case10.py` — RAG/IFC sync endpoints используют `impact_scope_for_documents` + передают `impact_scope` в `enqueue_process_job`.
- `api_service/app/db/models.py` — `EvidenceGroup.group_key/evidence_basis_hash/needs_reverification/basis_changed_at` (additive).
- `api_service/app/db/session.py` — additive SQLite/Postgres миграции для новых колонок, новый `schema_migrations` marker `20260919_case10_incremental_recompute`.
- `api_service/tests/test_case10_fallback.py` — обновлён импорт/вызовы под переименование `_create_rule_groups` → `_upsert_rule_groups` (сигнатура вызова не изменилась, поведение теста не менялось).
- `api_service/tests/test_case10_incremental.py` — новый файл, 17 тестов (список ниже).

### Тесты

Новый `test_case10_incremental.py`, 17 тестов, запущено локально через venv (`python -m unittest discover`, PYTHONPATH=api_service, БЕЗ Docker — Docker Desktop не был поднят в этой сессии, см. ограничения):

- `EvidenceGroupUpsertTests` (5): identical rerun — no-op; changed+no decision — refresh in place; changed+decision — decision preserved + flagged; repeated identical mismatch — no audit spam/no rewrite; sweep — undecided orphan deleted, decided orphan preserved+flagged.
- `ObsoleteRevisionFilterTests` (3): superseded-in-set документ отброшен; successor вне набора не отбрасывает; документы без цепочки (как весь official dataset) проходят без изmenений.
- `DuplicateUploadNoReworkTests` (2): побайтово идентичный re-upload через новый ingest — `changed=False`, вторая `DocumentVersion` строка не создаётся; разные байты — новая ревизия с корректной `predecessor_id`/`successor_id` цепочкой.
- `ImpactScopedExtractionIsCheaperThanFullTests` (2): scoping к одному коду не рендерит страницы документов с другими кодами (детерминированный mock-подсчёт вызовов, не wall-clock); пустой impact set не сканирует ничего.
- `ImpactScopedRecomputeTests` (3, через synthetic demo pipeline — реальный `run_process` с `affected_param_codes`): scoped rerun трогает только целевой параметр (id/`updated_at`/decision count двух других находок побитово не меняются); basis изменился у уже решённой находки → decision/`finding_status` сохранены, `needs_reverification=True`, `pending_reverification` содержит новое значение, повторное решение снимает флаг и переносит значения; новая утверждённая ревизия (с собственными `SourceFragment`/`EntityObservation`/`AttributeObservation`, связанная `predecessor_id`/`successor_id`) заменяет старую как эталон.
- `ProtocolVersionHistoryTests` (1): предыдущая версия протокола не мутирует после нового прогона; `diff` корректно относит изменённую находку к `updated`, незатронутую — к `unchanged`.
- `FinalizedProtocolIncrementBlockedTests` (1): `run_process` после `finalize_protocol` кидает 409 даже с explicit `affected_param_codes`; `get_or_create_open_process` после этого создаёт новый `process_id`.

**Backend regression**: полный набор — **90/90 OK** (73 существовавших без изменений в поведении + 17 новых), локально через venv (`D:\Proga\LCT-hack-2026\venv`), `13.8s`. Дополнительно проверено: `python -m app.main`/`python -m app.worker_case10` импортируются без ошибок (нет циклических импортов от нового `evidence_groups.py`); миграция проверена дважды — (1) `init_db()` на чистой БД создаёт все 4 новые колонки + новый `schema_migrations` marker; (2) на вручную собранной "старой" `evidence_groups` таблице (без новых колонок, с одной существующей строкой) прогон `_run_sqlite_migrations()` добавляет колонки и НЕ теряет существующую строку (`id/finding_status` совпадают до/после).

### Ограничения / что не делалось в этой сессии

- **Docker-контур не пересобирался и не проверялся живьём** (Docker Desktop не был поднят в этой сессии) — вся верификация выше сделана через локальный `venv` (Python 3.12, тот же `requirements.txt`, `pypdf` доустановлен) напрямую против `api_service`, не через `lct-hack-2026-api-1`. Реальный prod/офиц. датасет (`evaluation.smoke_real_data`) не перезапускался — риск: если venv-окружение чем-то отличается от Docker-образа (другая версия библиотеки), это не будет поймано локальными прогонами. Следующей сессии стоит пересобрать `docker compose build api` + погонять `evaluation.smoke_real_data --verify-pages --verify-protocol` на официальном датасете, чтобы подтвердить, что impact-scoped extraction (`only_codes`) не изменила метрики Novoslobodskaya/Tyumenskaya (по построению не должна — при full run `only_codes` покрывает все 5 кодов один в один, но это стоит подтвердить живьём, а не только по чтению кода).
- **`process.affected_param_codes`-merge при повторном enqueue активного job остаётся additive-only**: если явный full-run запрос (`affected_param_codes=[]`) приходит, пока уже есть активный SCOPED job для того же процесса, `enqueue_process_job` лишь объединяет множества (не форсирует full) — узкий scope "выигрывает" у одновременного запроса на full run. Не исправлялось в этой сессии (предсуществующая, не связанная с decision-preservation логика; худший случай — просто более узкий прогон сейчас, полный всё равно случится на следующем триггере без explicit scope, т.к. `process.affected_param_codes` сбрасывается после каждого успешного `run_process`).
- **`ProcessingCache`/redis `cache_key` по-прежнему ничего не пропускает** — `PROCESS_CACHE_HIT` audit пишется, но пересчёт всё равно выполняется полностью (это предсуществующее поведение из checkpoint 15/30, не трогалось; duplicate-upload защита в этой сессии работает НАД этим механизмом — перехватывает дубликат до постановки job в очередь, так что сам cache-hit-но-всё-равно-считает путь для "duplicate upload" сценария не задействуется).
- **Fragments одноцикловый лаг при re-review** (см. `_apply_pending_reverification`) — задокументированное ограничение, не считается багом при текущем масштабе (bbox/страницы обновятся штатно на следующем реальном recompute).
- **RabbitMQ contour не менялся** — incremental job идёт через существующий `case10.process.jobs`/`case10_worker` (checkpoint 31), как и предписано заданием при уже готовой очереди; сама очередь/topology/retry-логика не трогалась.
- **UI (`frontend_react`) не менялся** — `needs_reverification`/`group_key`/`basis_changed_at` отданы в `evidence_group_to_dict`/API, но карточка находки пока не показывает бейдж "требует повторной проверки"; вне явного чек-листа этой сессии (сессия — backend impact-based recompute, не UI).

## 34. Checkpoint 35, 19.09.2026 — Честный coverage-аудит 132 параметров + первый общий (generic) extractor

### Задача сессии

Провести production-readiness аудит всех 132 параметров официальной матрицы (не demo), зафиксировать для каждого честный readiness-класс (не по наличию строки `Param` в БД, а по реальному исполняемому пути `extraction → evidence → comparison/status`), сделать это воспроизводимым отчётом, и закрыть максимум high-ROI пробелов общими (не per-code) механизмами — без искусственного завышения метрик и без хардкода к GOLD.

### Ключевая находка форензики (до любого кода)

До этой сессии реализация была честна ровно настолько, насколько это документировали прошлые чекпоинты, но масштаб пробела не был явно измерен на уровне всех 132: `official_rule_packs.SUPPORTED_RULE_CODES = {"PZ-009", "KR-055", "KR-058", "IOS4-078", "IOS4-079"}` — **только 5 из 132** параметров имеют настоящую per-code extraction-логику. Для остальных 127 `create_official_evidence_groups` (`official_evidence.py`) заходит в fallback-ветку, которая берёт `learning_annotation`-фрагменты (broad-recall keyword-теги, `AUTO_FIELD_CANDIDATE`, confidence ~0.6–0.9 почти без дискриминации) **только как context** — она никогда не пытается извлечь и сравнить значение, поэтому эти 127 параметров структурно не могут получить статус, отличный от `MISSING_EVIDENCE`/`NOT_COMPARABLE`/`CLARIFICATION_REQUIRED`, независимо от того, что реально написано в документе. Это подтвердилось и живым прогоном (см. ниже): у `OBJ-TYUMENSKAYA-5-GOLD-SEED` 130 из 137 групп — `CLARIFICATION_REQUIRED` (объект имеет `RD_ID_MIXED` — общую рабочую/исполнительную стадию без чистого разделения), у `OBJ-NOVOSLOBODSKAYA` 108 из 134 — `NOT_COMPARABLE`.

Отдельно проверено (`official_dataset.py::_param_from_catalog_row`): **`Param.regex_pattern` всегда `None`** для всех 132 официальных параметров, а demo-путь `RegexExtractor`/`TableExtractor`/`SemanticExtractor`/`OCRExtractor`/`CVExtractor` (`v3_extractors.py`) для официального `MATRIX_VERSION_OFFICIAL` вообще не вызывается — это чистый scaffolding без реального эффекта на официальном контуре, не источник скрытого покрытия.

Также форензически подтверждено (`case_data/CASE10_DATASET_ANALYSIS.md`, раздел 6): из 132 параметров только **9** имеют хотя бы одну атомарную gold-проверку в закрытой методике организатора (`PZ-009, SPZU-027, SPZU-029, SPZU-036, SPZU-039, KR-055, KR-058, IOS4-078, IOS4-079`) — т.е. даже теоретический максимум "параметров, чью точность вообще можно измерить" сегодня — 9/132, а не 132/132. 123/132 имеют `NO_GOLD_EXAMPLES`.

### 1. Инвентарь и отчёт: `evaluation/matrix_coverage.py`

Новый воспроизводимый скрипт (`python -m evaluation.matrix_coverage`, без Docker/БД — читает `parameter_catalog_132.jsonl` напрямую и импортирует только dependency-light `app.domain.matrix_unit_classifier`/`official_rule_packs.SUPPORTED_RULE_CODES`), генерирующий:

- `evaluation/reports/matrix_coverage.json` — полная машиночитаемая инвентаризация всех 132 (per-param: `code/section/parameter_name/unit/data_type_class/criticality/review_priority/required_stages/extractor/localization/comparison_exists/negative_path_exists/tests/real_fixture/readiness_class`) + агрегаты (`by_readiness_class`, `by_section`, `by_required_stage_combination`, `by_data_type_class`, `test_coverage`, `real_fixture_coverage`, `abstention_rate_on_real_public_dataset`, `real_data_run`, `hidden_blind_smoke`, `top_missing_capabilities`, `priority_backlog`).
- `CASE10_MATRIX_132_COVERAGE.md` — человекочитаемая версия того же самого (не отдельный документ с ручными числами — рендерится из того же `summary`/`parameters`, поэтому не может разойтись с JSON).

Readiness-классы вычисляются программно, не приписываются вручную:

- **`PRODUCTION_READY`** (5, ровно `SUPPORTED_RULE_CODES`, посчитано через импорт константы, не скопировано руками): тюнингованный rule-pack + реальное сравнение с negative path + тесты + хотя бы 1 gold-пример, используемый в `evaluation/smoke_real_data.py`.
- **`PARTIAL`** (48): код не в `SUPPORTED_RULE_CODES`, но проходит `is_generic_anchor_eligible` (новый общий механизм, см. ниже) — исполняемое сравнение существует, но без gold-валидации точности и с обобщённым (не доменным) порогом сравнения.
- **`CONFIG_ONLY`** (79): строка `Param` с метаданными существует, исполняемого extraction-пути нет вовсе — всегда `MISSING_EVIDENCE`/`NOT_COMPARABLE`/`CLARIFICATION_REQUIRED`.
- **`ABSTAIN_BY_DESIGN`/`NOT_APPLICABLE`**: **сознательно не используются ни для одного из 132** на уровне определения параметра матрицы (оба явно посчитаны `0` в отчёте с обоснованием) — оба класса корректны только для конкретной находки конкретного объекта в рантайме (например, "лифтовая шахта" → `NOT_APPLICABLE` для здания без лифта), а не для самого определения параметра в матрице; использование их здесь было бы ровно той маскировкой отсутствия реализации, которая явно запрещена в задании этой сессии.

Также в отчёте: 132/132 параметров реально прогоняются через `run_process` на официальном датасете ("real-data exercise coverage"), но это explicit отделено от "real gold-example coverage" (9/132) и от "parameter coverage (PRODUCTION_READY)" (5/132) — три разные, не смешиваемые метрики, как и требовало задание. OCR accuracy сознательно не считается (нет human-labelled OCR-денаминатора в текущем датасете — `evaluation/metrics.py::_evaluate_ocr` уже умеет считать, когда денаминатор появится, но генерировать `PASS` без него было бы нечестно).

### 2. Новый общий (generic, не per-code) механизм: `generic_matrix_extraction.py`

Единственный high-ROI пробел, закрытый кодом в этой сессии — **общий anchor+число extractor** для класса параметров, чьё `parameter_name` в каталоге используется как табличная подпись рядом с числом в реальном ТЭП-разделе ("Технико-экономические показатели" в ПЗ/СПЗУ и аналогичные таблицы в АР/КР/ИОС/ОДИ/ППМ/ПОС/ПОД/ЗУ). Никаких per-code веток — управляется только `Param.parameter_name` (anchor) и `Param.unit` (eligibility gate).

**Новые файлы**:

- `api_service/app/domain/matrix_unit_classifier.py` — чистая, dependency-free (только stdlib) классификация `unit` в `NUMERIC_SIMPLE`/`NUMERIC_COMPOUND`/`NUMERIC_COUNT`/`ENUM_CLASS`/`OTHER`/`NONE` + `is_generic_anchor_eligible()` (код не в `SUPPORTED_RULE_CODES`, unit == `NUMERIC_SIMPLE`, имя параметра ≥2 слов/≥8 символов). Один и тот же модуль импортируется и рантаймом (`generic_matrix_extraction.py`), и отчётом (`evaluation/matrix_coverage.py`) — классификация никогда не может разойтись между "что реально исполняется" и "что показано в отчёте".
- `api_service/app/domain/generic_matrix_extraction.py` — сам механизм:
  - `find_anchor_numeric_value(snapshot, anchor_phrase)` — ищет anchor-фразу среди `snapshot["words"]` (не в плоском тексте — важно, см. форензику ниже) допуская до 2 "вставленных" слов между словами anchor (реальный документ пишет "Площадь **жилого** здания" вместо каталожного "Общая площадь здания") и, если полная фраза не найдена, а имя ≥3 слов — fallback с отброшенным первым словом (для случая, когда сам каталожный квалификатор типа "Общая" в реальном документе отсутствует вовсе). Затем ищет "лучшее" число в следующих ≤25 словах: предпочитает ближайший токен с десятичной дробью (реальное измеренное значение) над коротким целым (почти всегда — номер строки таблицы "№ п/п").
  - Числа с разделителем тысяч, разбитым PDF-словами на два токена ("17" + "140,2"), корректно склеиваются **только** когда второе слово — ровно 3 цифры (+опц. дробь) — то есть склейка возможна только между двумя соседними словами, а не внутри одного PDF-слова, которое `fitz` уже отдал как единый токен.
  - `values_equal()` — сравнение с допуском (`max(0.01, |a|*0.001)`), без доменного порога из `trigger` (генерализованный порог, не парсинг свободного текста триггера — сознательно вне охвата, см. backlog).
  - `collect_generic_observations()` — один проход по всем eligible-параметрам, до 15 (`PAGES_PER_STAGE`) кандидатных страниц на стадию, батчинг `extract_original_pages` **по документу** (не по параметру — одна ТЭП-таблица отвечает сразу на несколько параметров одним открытием PDF), общий бюджет страниц на прогон (`DEFAULT_PAGE_BUDGET=1200`).
- `api_service/app/domain/official_evidence.py` — новая `_upsert_generic_group()` (зеркалит `_upsert_rule_groups`: требует PD **и** RD, ID — опциональный бонус, `actual = ID or RD`) вызывается в цикле `create_official_evidence_groups` **после** rule-pack ветки и **до** старой context-only fallback-ветки — то есть либо rule-pack, либо generic, либо старое поведение без изменений; ничего не убрано, только добавлен новый, более информативный, но всё ещё честно-абстинирующий уровень.

### Форензика, зашитая в дизайн (не по памяти — проверено на реальных документах)

Три реальные проблемы найдены и закрыты **до** того, как код был написан набело (следуя правилу "forensic-audit-before-fix"):

1. **Порядок слов в многоколоночных таблицах** ломает "число сразу после anchor": на реальной странице (`Технико-экономические показатели`, форма ПЗ) слова упорядочены `fitz`'ом по visual reading order, и колонка "№ п/п" оказывается ВПЛОТНУЮ перед значением: `"Площадь застройки 2. 4650,91"` — наивный "первое число после anchor" вернул бы `"2"`. Решение: предпочитать число с десятичной дробью среди всех чисел в окне, а не первое попавшееся.
2. **Разделитель тысяч ломает соседний несвязанный номер**: изначальная regex-версия (`\d+(?:[ ]\d{3})*`) на плоском тексте `"...Общая площадь м2 1 13060,00..."` склеивала "№ п/п" `1` с первыми 3 цифрами следующего, полностью самостоятельного числа `13060,00`, получая мусор `1130` + хвост `60,00`. Переход на пословную (не regex-по-строке) обработку с `fitz`-словами устраняет это структурно: разделитель тысяч теперь может связывать только два **соседних PDF-слова**, а `"13060,00"` как один токен `fitz` в принципе не может быть разрезан посередине.
3. **Каталожное имя параметра не совпадает с реальной формулировкой документа**: для `OBJ-NOVOSLOBODSKAYA` (жилой дом) реальная таблица пишет `"Площадь жилого здания"`, а не каталожное `"Общая площадь здания"` — ни один из двух этих слов не является лишним синонимом, это структурно другая фраза (вставлен квалификатор "жилого", убран квалификатор "Общая"). Решение —两уровневый anchor-поиск (полная фраза с толерантностью к 1 вставленному слову, затем фраза без первого слова) — общий, не привязанный к конкретному коду или типу здания механизм.

### Тесты

`api_service/tests/test_case10_generic_extraction.py` — новый файл, **36 тестов**: классификация unit/eligibility; `find_anchor_numeric_value` на синтетике и на анонимизированных реальных фрагментах ТЭП-таблиц (используются только как реалистичный текст для проверки токенизации — не как GOLD-ответ, никакое утверждение теста не сверяется с закрытой разметкой); склейка/несклейка разделителя тысяч; bbox-нормализация и разница confidence decimal/integer; `_upsert_generic_group` (PD+RD обязательны, ID — опциональный бонус, equal→`NEGATIVE_VERIFIED`, different→`CANDIDATE` **никогда** не `CONFIRMED_VIOLATION` напрямую); upsert-идемпотентность; `collect_generic_observations` — общая страница отвечает на несколько параметров одним рендером, исчерпанный бюджет не падает и не выдумывает результат, **явный тест "нет candidate explosion"** (несколько параметров на общих страницах — не более PD+RD на параметр); явный тест **"никогда не производит `NOT_APPLICABLE`/`MISSING_EVIDENCE`"**; явный тест, что коды из `SUPPORTED_RULE_CODES` никогда не попадают в generic-пул кандидатов даже при подходящем unit.

**Regression**: `PYTHONPATH=api_service python -m unittest discover -s api_service/tests` из корня — **126/126 OK** (90 существовавших без изменений в поведении + 36 новых), 11.7с.

### Реальная проверка (venv, throwaway SQLite, без Docker — Docker Desktop не был поднят в этой сессии)

Запущено напрямую через `app.domain.v3_pipeline.run_process` (не HTTP/API — быстрее для итерации, но эквивалентно тому же коду, что вызывает `evaluation/smoke_real_data.py`/`smoke_hidden_blind.py`):

- **Оба публичных объекта** (`OBJ-TYUMENSKAYA-5-GOLD-SEED`, `OBJ-NOVOSLOBODSKAYA`): итоговое число evidence groups **не изменилось** (137/134 — то же самое, что и до этой сессии), официальные rule-pack счётчики **не изменились** (7/5 CANDIDATE+NEGATIVE_VERIFIED от `official-rule-packs-v2`) — **ноль регрессии** на уже настроенном контуре.
- Generic-механизм: 48 eligible-параметров на объект; корректно нашёл **реальные** PD-значения для нескольких параметров (`PZ-001`=4650.91/1225.8, `PZ-002`=11618.27/17140.2, `PZ-008`=15.814/77.05 м²/м — все сверены вручную с текстом реального документа, не с gold), но создал **0 новых COMPARABLE evidence groups**, потому что ни для одного из них не нашлось соответствующего RD-значения в пределах бюджета страниц — у Tyumenskaya структурно нет чистой RD-стадии (`RD_ID_MIXED`), у Novoslobodskaya RD-таблица с тем же значением не попала в top-15 кандидатных страниц. Механизм корректно **абстинирует**, а не выдумывает пару — именно так, как спроектировано.
- **Hidden blind smoke** (`OBJ-RECHNIKOV-7-7`, `include_hidden=True`, `allow_hidden_gold_labels=False` — hidden-метки никогда не импортированы и не видны инференсу): 133 evidence groups, `CANDIDATE`+`SUSPICION` = 1 (0.75%) — оба гейта `smoke_hidden_blind.py` (`≤20` абсолютных, `≤15%` доли) **пройдены** с большим запасом; групп от нового generic-механизма — **0** (та же консервативная логика, что и на публичных объектах).

Все три измерения (regression suite, публичные объекты, hidden blind) записаны как измеренные константы (`REAL_DATA_RUN`, `HIDDEN_BLIND_SMOKE`) в `evaluation/matrix_coverage.py` и отражены в `CASE10_MATRIX_132_COVERAGE.md` — не переоценка, а факт одного конкретного прогона с датой; следующая сессия должна перепроверить в Docker через `evaluation.smoke_real_data`/`smoke_hidden_blind`, если версии библиотек в образе успели разойтись с venv.

### Итоговые цифры покрытия (before/after)

| | До сессии (неявно, не измерялось агрегированно) | После сессии (измерено, `matrix_coverage.json`) |
|---|---:|---:|
| PRODUCTION_READY | 5 (`SUPPORTED_RULE_CODES`, но не подсчитано формально по всем 132) | **5** (то же самое, не менялось) |
| PARTIAL (исполняемый extractor+comparison, без gold) | 0 (механизма не существовало) | **48** |
| CONFIG_ONLY (только метаданные, всегда abstain) | 127 (неявно) | **79** |
| Параметров с тестами | не считалось агрегированно | **8/132** (6.1%) |
| Параметров с реальным gold | не считалось агрегированно | **9/132** (6.8%) |
| Новых валидированных (gold) comparable findings на публичном датасете | — | **0** (честно — см. выше почему) |

### Приоритетный backlog (P0/P1/P2, из отчёта)

- **P0** (2, есть закрытый organizer-gold, реализации нет вообще): `SPZU-029` (Спецификация МАФ — `NUMERIC_COMPOUND`, нужен подсчёт позиций спецификации, не anchor), `SPZU-039` (Мероприятия по инженерной подготовке — unit `—`, чисто chertezh/scheme-check).
- **P1** (2, уже `PARTIAL` + есть organizer-gold — можно валидировать точность немедленно, не дожидаясь новой инженерии): `SPZU-027` (Площадь озеленения), `SPZU-036` (Высотные параметры ограждения).
- **P1** (63, `CONFIG_ONLY` + `review_priority=HIGH`/критическая приостановка работ) — приоритет по бизнес-импакту, не по лёгкости реализации.
- **P2** (16, `CONFIG_ONLY` + `MEDIUM`).
- **Top missing capabilities** (из отчёта, сгруппировано по `data_type_class` оставшихся `CONFIG_ONLY`): (а) словарь марок/классов материалов (16 параметров, тот же паттерн, что уже даёт `KR-055`, но обобщённый на курируемый список кодов, а не 1 код); (б) парсер составных величин с несколькими осями/единицами (25 параметров, "м³/ч / Па / кВт"-подобные); (в) подсчёт позиций по таблице-спецификации, а не одна пара label:value (13 параметров); (г) интерпретация чертежа/схемы — CV/vector-drawing задача, не текстовая (18 параметров, ограждения/дренаж/тактильная разметка и т.п.).

### Ограничения / что осознанно не делалось в этой сессии

- **Доменный порог сравнения из `trigger`** (например ">1%", "уменьшение ниже минимальных требований ГПЗУ") не парсится — генерализованное равенство с допуском вместо этого. Разбор свободного текста триггера по 127 параметрам — отдельная, значительно более рискованная задача (риск неверно интерпретировать доменную формулировку); честно отражено как причина, почему `PARTIAL`, а не `PRODUCTION_READY`.
- **`ENUM_CLASS`/`NUMERIC_COMPOUND`/`NUMERIC_COUNT`/чертёжные параметры не тронуты** — сознательно, по прямому указанию задания не пытаться закрыть 132/132 любой ценой; каждый класс требует отдельного нетривиального механизма (словарь марок, multi-value парсер, table-row counting, CV), не обобщаемого в этой сессии без риска именно того рода "десятков уникальных regex", которого просят избежать.
- **Docker-контур не пересобирался** — вся верификация через локальный venv (тот же паттерн, что и checkpoint 34); следующая сессия должна прогнать `evaluation.smoke_real_data --verify-pages --verify-protocol` и `evaluation.smoke_hidden_blind` в Docker для окончательного подтверждения перед любым внешним отчётом о готовности.
- **UI не показывает новый generic-механизм отдельно от rule-pack** — `model_version`/`delta.source="generic_anchor_extractor"`/`delta.gold_validated=false` уже отданы в API (через `evidence_group_to_dict`, не менялось в этой сессии), но `frontend_react` не подсвечивает эту находку как "менее проверенную" визуально — вне явного чек-листа этой сессии (backend-контур).
- **`PAGES_PER_STAGE`/`DEFAULT_PAGE_BUDGET` (15/1200) подобраны эмпирически** на 2 публичных объектах, а не выведены аналитически — задокументировано в коде как компромисс между recall и стоимостью (перебор 3→8→20→25 страниц/стадию не менял результат на этих 2 объектах, что указывает на то, что узкое место — не глубина скана, а сам локализационный сигнал `learning_annotation`, а не бюджет; см. "top missing capabilities").
