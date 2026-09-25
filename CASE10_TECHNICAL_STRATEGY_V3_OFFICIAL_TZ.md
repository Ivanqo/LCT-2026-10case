# CASE №10 - финальная техническая стратегия по официальному ТЗ

Версия: 3.2, финальный аудит после сверки `ТЗ.zip` от 16.09.2026  
Цель документа: дать практическую стратегию реализации и демонстрации CASE 10, которая одновременно закрывает обязательные требования ТЗ, использует фактический датасет, учитывает текущее состояние разработки и максимизирует конкурсную оценку.

## 1. Источники истины и главный вывод

Использованные источники:

- официальное ТЗ CASE 10: `C:/Users/Ivan/Downloads/10. Мосстройнадзор.pdf`;
- новый архив ТЗ: `C:/Users/Ivan/Downloads/ТЗ.zip`;
- `Матрица_параметров_редакция1.1.xlsx`;
- `Перечень_исполнительной_документации_редакция1_1.docx`;
- `Комплект_предметной_разметки.pdf` и `Комплект_предметной_разметки_с_пояснениями_.pdf`;
- машиночитаемая копия официального ТЗ и приложений из пакета участников;
- `case_data/CASE10_DATASET_ANALYSIS.md`;
- `case_data/CASE10_DATASET_IMPLEMENTATION_CONCLUSIONS.md`;
- `case_data/CASE10_DATASET_SOURCE_MAP.md`;
- `learning_data/extracted/LEARNING_DATA_ANALYSIS_STATS.json`;
- `CASE10_IMPLEMENTATION_PROGRESS.md`;
- предыдущее состояние этого файла.

Иерархия источников:

| Вопрос | Источник истины |
|---|---|
| Что должна делать система | официальное ТЗ и приложения |
| Какие данные реально доступны | фактический датасет и `learning_data` |
| Что уже реализовано | `CASE10_IMPLEMENTATION_PROGRESS.md` и текущие файлы проекта |
| Как выбирать приоритеты | влияние на ТЗ, scoring, демонстрацию и риск реализации |

Главный вывод аудита: новые файлы ТЗ не меняют предмет задачи и состав 132 параметров, но критично уточняют способ реализации. Канонической редакцией матрицы для стратегии становится `Матрица_параметров_редакция1.1.xlsx`: она совпадает по смыслу с уже найденной существенной редакцией, но задает официальные коды `M-001...M-132`, отдельные листы `СХЕМА GOLD`, `МЕТРИКИ` и `ПРИМЕРЫ РАЗМЕТКИ`. Текущий JSON `parameter_catalog_132.jsonl` и gold-checks используют legacy/scoring-коды `PZ/KR/IOS4/...`; поэтому P0 - не просто "импортировать 132", а закрепить двойную идентификацию: `matrix_code=M-xxx` + `scoring_code`/alias из датасета.

По текущему `CASE10_IMPLEMENTATION_PROGRESS.md` часть прежних P0 уже закрыта: импортированы 132 параметра из JSON-каталога, public train документы/страницы/аннотации, bbox, submission schema, evidence viewer, Annex 2 protocol и real-data smoke для Tyumenskaya/Novoslobodskaya. Главный незакрытый P0 теперь - независимое извлечение и сравнение реальных значений, особенно `IOS4-078/079` для positive e2e, а также приведение матрицы к редакции 1.1 без поломки существующих `PZ/KR/IOS4` метрик.

## 2. Решение, которое надо довести до сдачи

Итоговый продукт:

> `Инспектор ИИ` - evidence-centric система камеральной проверки ПД, РД и ИД. Она импортирует официальную матрицу 132 параметров, строит реестр документов и редакций, извлекает значения и доказательства, формирует предварительные `EvidenceGroup`, показывает инспектору проверяемые карточки с источниками, фиксирует решение человека, версионирует протокол и накапливает GOLD для управляемого улучшения моделей.

Архитектурный центр:

```text
Construction Object
  -> Document Registry / Revision Graph
  -> Official Matrix 132 Params
  -> Extraction / OCR / Table / Semantic / CV
  -> EvidenceGroup
  -> Inspector Decision
  -> Versioned Protocol
  -> GOLD Draft / Evaluation
```

Конкурентная надстройка:

```text
SourceFragment
  -> EntityObservation
  -> CanonicalEntity
  -> Object Portrait
  -> Change Timeline / Impact Graph / Suspicion
```

Важно: `CanonicalEntity`, `Object Portrait`, IFC и свободный поиск не заменяют проверку по 132 параметрам. Они нужны, чтобы повысить объяснимость, оригинальность и качество отбора кандидатов.

## 3. Факты из официального ТЗ

Обязательные функции:

- проверка трех массивов документации: ПД, РД, ИД;
- поддержка входных форматов `PDF`, `DOCX`, `XML`;
- выявление нарушений и несоответствий по 132 контролируемым параметрам;
- формирование юридически значимого протокола проверки;
- верификация нарушений инспектором с фиксацией решения и обоснования;
- обратная связь ИИ и дообучение на решениях инспектора;
- API для обмена с внешними ИС, целевая - ИАИС `РиН`;
- контроль комплектности документов;
- инкрементальная дозагрузка до финализации протокола.

Обязательный стек по ТЗ:

| Слой | Требование ТЗ | Практическое решение для хакатона |
|---|---|---|
| Frontend | React | P0: либо минимальный React-shell для ключевого demo flow, либо явное оформление текущего static UI как временного прототипа с риском по ТЗ. Для 5/5 по соответствию нужен React-shell. |
| Server/API | Node.js | P0: тонкий Node.js gateway/orchestrator с OpenAPI, который проксирует текущий Python API и демонстрирует внешний контракт ТЗ. |
| ML modules | Python 3.11+ | Сохранить текущие Python-сервисы как parsing/check/ML layer. |
| Взаимодействие | REST API + RabbitMQ | REST уже есть частично; RabbitMQ publisher есть, нужен consumer/worker или честный demo fallback. |
| Хранилища | БД, Redis cache | Сохранить Postgres/SQLite dev mode, Redis для OCR/parsing cache. |

Процесс проверки:

```text
PENDING -> PARSING -> READY -> VERIFYING -> COMPLETED -> FINALIZED
```

После `FINALIZED` запрещены дозагрузка в текущую проверку и изменение решений. Новые документы должны создавать новую проверку или новую версию процесса, а не мутировать подписанный протокол.

Сценарии загрузки:

```text
FULL
PD_RD_ONLY
PD_ID_ONLY
RD_ID_ONLY
SINGLE_ONLY
PARTIALLY_LOADED
```

Статусы комплектности:

```text
PD_UPLOADED / PD_PARTIAL / PD_MISSING
RD_UPLOADED / RD_PARTIAL / RD_MISSING
ID_UPLOADED / ID_PARTIAL / ID_MISSING
```

Статусы результата в протоколе ТЗ:

```text
OK
WARNING
CRITICAL
ID_MISSING
RD_MISSING
PD_MISSING
PARTIALLY_LOADED
COMPARISON_IMPOSSIBLE
```

Статусы верификации ТЗ:

```text
PENDING
CONFIRMED
REJECTED
CLARIFICATION_REQUIRED
PARTIALLY_CONFIRMED
VERIFICATION_COMPLETED
PROTOCOL_FINALIZED
```

Пороговые метрики из ТЗ:

| Метрика | Целевое значение |
|---|---:|
| OCR на сканах 300 DPI+ | точность текста не менее 95% |
| Загрузка до 10 файлов по 50 МБ | не более 2 минут, допустимое отклонение 30 секунд |
| OCR PDF до 100 страниц | не более 3 минут, допустимое отклонение 30 секунд |
| OCR PDF до 500 страниц | не более 10 минут, допустимое отклонение 60 секунд |
| Сравнение 132 параметров при полном комплекте | не более 2 минут, допустимое отклонение 30 секунд |
| Генерация протокола JSON/PDF | не более 30 секунд, допустимое отклонение 10 секунд |
| Отправка в ИАИС `РиН` | не более 30 секунд, допустимое отклонение 10 секунд |
| ML/NLP-анализ одного параметра | не более 500 мс, допустимое отклонение 100 мс |
| CV-анализ одного чертежа DWG | не более 30 секунд, допустимое отклонение 10 секунд |
| Инкрементальное обновление протокола | не более 1 минуты, допустимое отклонение 15 секунд |
| API p95 | не более 200 мс, допустимое отклонение 50 мс |
| Одновременная работа | не менее 100 инспекторов |
| SLA | 99.9% |
| RTO | не более 1 часа |
| RPO | не более 15 минут |
| Верификация протокола 132 параметра / 14 нарушений | не более 30 минут для опытного пользователя |
| Действий на одно нарушение | не более 3 кликов |
| Юзабилити-тест | 5 инспекторов |

Требования к дообучению:

- одна итерация дообучения - не менее 100 подтвержденных нарушений;
- валидация на тестовой выборке 30%;
- откат модели при деградации F1 более чем на 5%;
- журналирование в `ML_Retraining_Log`.

Для хакатона нельзя обещать настоящее дообучение, если нет 100 подтвержденных нарушений. Нужно показать корректный feedback loop: решения инспектора попадают в GOLD draft, считаются метрики, модель-кандидат не публикуется без acceptance gate.

## 4. Факты из датасета

Открытый пакет участников:

| Блок | Факт |
|---|---:|
| Всего документов | 416 |
| Объектов | 3 |
| PDF-страниц | 26 103 |
| `OBJ-RECHNIKOV-7-7` | 213 файлов, `TEST_HIDDEN` |
| `OBJ-NOVOSLOBODSKAYA` | 145 файлов, `TRAIN_PUBLIC` |
| `OBJ-TYUMENSKAYA-5-GOLD-SEED` | 58 файлов, `TRAIN_PUBLIC` |

Стадии в открытом пакете:

| Стадия | Файлов | PDF-страниц |
|---|---:|---:|
| `PD` | 172 | 13 892 |
| `ID` | 130 | 10 342 |
| `RD` | 103 | 1 032 |
| `RD_ID_MIXED` | 10 | 837 |
| `UNKNOWN` | 1 | 0 |

Официальная матрица:

- `Матрица_параметров_редакция1.1.xlsx` содержит 132 строки и четыре листа: `МАТРИЦА`, `СХЕМА GOLD`, `МЕТРИКИ`, `ПРИМЕРЫ РАЗМЕТКИ`;
- по содержанию 132 строки совпадают с уже найденной существенной редакцией матрицы и с JSON-каталогом, но официальные коды в XLSX теперь `M-001...M-132`;
- `parameter_catalog_132.jsonl` содержит те же параметры в формате, который используют текущий evaluator и gold-checks: `PZ-001`, `KR-055`, `IOS4-079` и т. п.;
- критичность / приоритет: 106 `HIGH` / критических параметров, 26 `MEDIUM` / существенных;
- покрытие разделов: `PZ` 23, `SPZU` 16, `AR` 14, `KR` 14, `PPM` 13, `POS` 9, `ODI` 9, `POD` 8, `ZU` 8, остальные инженерные и сметные разделы меньше;
- готовых `regex_pattern` в каталоге нет, поэтому правила надо строить из `trigger`, источников, единиц измерения, секций, семантических якорей и layout/table извлечений.

Практическое правило идентификаторов: внутри `Param` хранить `parameter_id`/`matrix_row`, `matrix_code=M-xxx`, `scoring_code` из JSON/gold и список aliases. В UI, протоколе и pitch показывать официальный `M-xxx` вместе с предметным кодом, например `M-079 / IOS4-079`. В evaluation/submission не ломать существующие `PZ/KR/IOS4` коды без явной таблицы соответствия.

Gold и scoring:

| Набор | Факт |
|---|---:|
| `public_train_checks.jsonl` в старом пакете | 10 checks, все positive, 3 кода |
| `public_gold_checks.jsonl` в `learning_data` | 15 checks: 10 positive, 5 negative/second review |
| `all_gold_checks.jsonl` организатора | 19 checks: 11 positive, 8 negative |
| Параметров с gold-примерами | 9 из 132 |
| Evidence links | 43, в основном file/page level |
| Параметров без gold-примеров | 123 из 132 |

Scoring proposal из организаторского пакета:

| Компонент | Вес |
|---|---:|
| `finding_detection_f1` | 60 |
| `source_localization_exact_file_page` | 15 |
| `normalized_value_and_status_accuracy` | 15 |
| `document_integrity_and_split_handling` | 10 |

Дополнительные приемочные метрики из листа `МЕТРИКИ` матрицы 1.1:

| Контур | Порог |
|---|---:|
| OCR character accuracy | >= 0.95 |
| Exact match ключевых полей | >= 0.90 |
| Exact linkage документов | >= 0.95 |
| Localization completeness evidence_group | >= 0.95 |
| Нарушения | Precision >= 0.90, Recall >= 0.80, F1 >= 0.85 |
| False Positive Rate на negative/stale cases | <= 0.10 |
| Object-level split | обязательно |
| Regression gate | ухудшение recall/FPR не более 2 п.п. |

Критичный gate: пропуск любой утвержденной критической контрольной точки ограничивает итоговый балл 59/100. Negative checks входят в precision, поэтому стратегия должна снижать false positives, а не только искать максимум кандидатов.

`learning_data`:

| Split | Файлы | Страницы | Аннотации | Что использовать |
|---|---:|---:|---:|---|
| `TRAIN_PUBLIC_203` | 203 | 10 146 | 30 318 | импорт реестра, page index, bbox, public checks, demo/eval |
| `TEST_HIDDEN_213` | 213 | 16 078 | 40 960 | только inference/leakage-safe проверка формата, не обучение на organizer-only labels |

Всего доступно 71 278 координатных аннотаций с `bbox_normalized`, но почти все они имеют статус `AUTO_FIELD_CANDIDATE`, а не `FINAL_GOLD_EXISTENCE`. Значит, их можно использовать для Evidence Viewer, локализации и candidate generation, но не как полноценный supervised GOLD для качества нарушений.

Multi-object annotation:

- 9 объектов;
- 8 929 файлов;
- 154 001 PDF-страница;
- 48 findings/candidates;
- 963 duplicate groups;
- 212 ID-RD links;
- 0 training-eligible labels.

Роль этого блока: data-quality правила, демонстрация инспекторского workflow, ревизии, дубликаты, missing/stale RD. Не использовать как подтвержденный обучающий GOLD.

## 5. Текущее состояние реализации

Что уже можно сохранить:

- микросервисный контур `api_service` + `rag_service` + `ifc_service` + frontend;
- Docker stack поднимается, health checks пройдены;
- RabbitMQ добавлен в `docker-compose.yml`, publisher создает события;
- V3 доменные модели: `ConstructionObject`, `MatrixVersion`, `Param`, `InspectionProcess`, `EvidenceGroup`, `EvidenceFragment`, `EvidenceDecision`, `Protocol`, `AuditLog`, `GoldDraftItem`, `ProcessingCache`;
- расширенный `DocumentVersion` с `object_id`, `doc_stage`, `document_code`, `approval_status`, `approval_date`, hash, predecessor/successor;
- V3 process state machine;
- Evidence Card в UI с fragments, bbox, решениями инспектора;
- синтетический e2e flow: candidate -> confirm -> protocol version -> finalize -> запрет изменений;
- XML upload test;
- `evaluation/` harness для OCR, key fields, document linking, evidence localization, finding precision/recall/F1, FPR, coverage, abstention;
- импорт official dataset через API: 132 параметра, 203 public train документа, 10 146 страниц, 30 318 аннотаций;
- Evidence Viewer отдает оригинальные страницы с SHA-256 проверкой и bbox;
- exporter формирует submission, который проходит official `submission_schema.json`;
- Annex 2 protocol реализован в JSON/PDF, кириллица и визуальный PDF QA проверены;
- реальные процессы Tyumenskaya/Novoslobodskaya доводились до `FINALIZED`, stale finalize и изменения после finalization дают HTTP 409;
- Novoslobodskaya уже имеет независимые negative checks по `PZ-009`, `KR-055`, `KR-058` с original file/page/bbox;
- проверены `compileall`, `frontend/app.js`, `docker compose config`, `unittest discover` на 23 теста и real-data smoke.

Что нельзя считать готовым:

- текущий frontend статический, а не React;
- внешний server layer сейчас Python API, а не Node.js;
- матрица импортирована из JSON-каталога, но еще надо поддержать каноническую редакцию 1.1 XLSX с `M-001...M-132` и alias mapping к `PZ/KR/IOS4`;
- positive finding e2e еще не закрыт независимо: `IOS4-078` и `IOS4-079` для Tyumenskaya пока дают coverage 0 / F1 0;
- coverage по реальным rule packs пока низкий: Novoslobodskaya 3/132, Tyumenskaya 0/132;
- `RD_ID_MIXED` для Tyumenskaya нужно разрешать доказательно на уровне страниц/наблюдений, а не всей пачкой;
- RabbitMQ consumer/worker не реализован, обработка фактически synchronous API-triggered;
- Node gateway/OpenAPI и React-shell по-прежнему нужны для полного соответствия стеку ТЗ;
- Inspector UI для `Reject` использует `window.prompt`, это не уровень polished demo;
- нет полной проверки лимитов 50 МБ/200 МБ, performance targets, 100 пользователей, backup/restore, antivirus/IDS/DDoS/TLS 1.3.

## 6. Требования и метрики ТЗ -> реализация -> проверка

| Требование / метрика | Конкретная реализация | Способ проверки |
|---|---|---|
| ПД/РД/ИД как три массива | `DocumentVersion.doc_stage` хранит оригинальные `PD/RD/ID`, нормализованные aliases используются только внутри UI. | Импорт `document_manifest.jsonl` и `files_index.jsonl`; тест на корректные counts по стадиям; UI показывает три колонки. |
| PDF/DOCX/XML | Сохранить текущий upload, добавить real-data smoke для каждого формата; XML хранить как `SourceFragment` full-document. | Загрузить реальные PDF/DOCX/XML из пакета; проверить `DocumentVersion`, hash, stage, process_id. |
| 132 параметра | Зафиксировать `MatrixVersion` редакции 1.1: 132 active params, `matrix_code=M-001...M-132`, `scoring_code`/aliases из JSON/gold, demo matrix только fallback. | API `GET /api/case10/matrix` возвращает 132 active params; сверка 106/26, листов XLSX, aliases и совместимости с gold codes. |
| Источники `source_pd/source_rd/source_id` | Использовать их для applicability и page candidate search. | Для каждого param есть needed stages; missing stage дает MISSING, а не violation. |
| Сценарии `FULL`, `PD_RD_ONLY`, `PD_ID_ONLY`, `RD_ID_ONLY`, `SINGLE_ONLY`, `PARTIALLY_LOADED` | Отдельный detector по реестру документов и ожидаемым источникам матрицы. | Unit tests на все 6 сценариев; protocol содержит тип проверки. |
| Комплектность `PD/RD/ID_*` | Отдельные completeness statuses вне finding status. | Protocol section 1 как в приложении 2; отсутствующая стадия не создает `CONFIRMED`. |
| Process state machine | Сохранить V3 statuses; добавить worker-driven переходы через RabbitMQ или явно показать gateway status polling. | API tests: transitions, no mutation after `FINALIZED`, status endpoint. |
| Асинхронный Pull | `POST upload/process` возвращает `process_id`; heavy tasks идут через RabbitMQ. | Runtime test: message published, worker consumes, status меняется без ручного run endpoint. |
| Сравнение 132 параметров | `EvidenceGroup` как единица результата; `Checks`/export view совместим с ТЗ. | На 132 params сформированы `OK/MISSING/COMPARISON_IMPOSSIBLE/CANDIDATE`; exporter валидируется schema. |
| `OK/WARNING/CRITICAL` | Использовать как `protocol_status`, не как статус верификации. | В DB есть отдельные поля `protocol_status`, `finding_status`, `inspector_status`. |
| Верификация инспектором | `Confirm`, `Reject`, `Clarify`, `Partial` с reason/comment/user/timestamp. | UI/API test: каждое действие меняет только выбранный atomic group; reject требует причину. |
| Протокол JSON/PDF | Форматировать по приложению 2: загрузка, статистика, missing, critical, substantial, suspicions, резолютивная часть. | JSON schema validation + PDF render smoke с кириллицей. |
| Инкрементальная дозагрузка | `affected_param_codes` и новая версия протокола до finalization. | Test: upload после `READY` пересчитывает subset; после `FINALIZED` дает 409 или создает новую проверку. |
| OCR accuracy >= 95% | Включить OCR cache, считать CER/WER на размеченных страницах. | `evaluation` считает `ocr_character_accuracy >= 0.95` на выбранной контрольной выборке 300 DPI+ страницах. |
| OCR performance | Page cache, hash cache, selective OCR only pages without text / likely relevant pages. | Benchmark: 100 страниц <= 3 мин; 500 страниц <= 10 мин. |
| NLP <= 500 ms per param | Сначала rule/regex/table/semantic anchors, LLM только для объяснения/ambiguous cases. | Microbenchmark per param; p95 <= 500 ms. |
| Сравнение 132 params <= 2 мин | Batch extraction, cached fragments, skip `NOT_APPLICABLE`, incremental recompute. | Benchmark на объекте с полным комплектом. |
| API p95 <= 200 ms | Gateway/protocol endpoints читают подготовленные результаты, не запускают OCR inline. | Load test на key endpoints; p95 report. |
| Дообучение | GOLD draft + model registry + acceptance gate; настоящее retraining только при >=100 confirmed. | После решения инспектора создается GOLD row; попытка publish модели проверяет F1 деградацию <=5%. |
| Свободный поиск | `Suspicion` отдельно от official matrix violations. | `FREE-HEATING-001` показывается как `SUSPICION/FREE_SEARCH`, не портит official matrix metrics. |
| ИАИС `РиН` | Mock adapter + sync statuses; real integration documented. | Только finalized protocol можно отправить; 5xx retries 1/5/15 min; `PENDING_SYNC` не ломает протокол. |
| Security | Roles, audit log, hash integrity; TLS/IDS/DDoS/УКЭП как documented production adapters. | Demo показывает auth/roles/audit; checklist production gaps открыт. |
| Monitoring/logging | JSON logs, Prometheus metrics, RabbitMQ queue size. | `/metrics`, structured log sample, Grafana/Prometheus or documented local equivalent. |

## 7. Архитектура итогового решения

Целевая архитектура для соответствия ТЗ:

```text
React UI
  |
  v
Node.js API Gateway / OpenAPI / Auth facade
  |
  +--> Python API compatibility layer
  |       |
  |       +--> Case10 Check Engine
  |       +--> Document Registry / Revision Resolver
  |       +--> Protocol / Evaluation Export
  |
  +--> RabbitMQ
          |
          +--> Parsing Worker
          +--> OCR/Layout Worker
          +--> Check Worker
          +--> Protocol Worker

PostgreSQL / SQLite dev mode
Redis OCR and parsing cache
File storage with SHA-256 manifest
RAG service for text/layout source fragments
IFC service as optional differentiator
```

Практическая стратегия по стеку:

1. До сдачи не переписывать рабочий Python API целиком.
2. Добавить тонкий Node.js gateway с OpenAPI и `/api/v1/*` контрактом ТЗ.
3. Минимальный React-shell должен закрывать только конкурсный demo: upload, process status, matrix, candidates/evidence, protocol, audit.
4. Существующий static frontend можно оставить как fallback/admin prototype, но не позиционировать как финальное соответствие ТЗ.
5. RabbitMQ должен иметь хотя бы одного worker-consumer для V3 process events; publisher-only демонстрация слабее для критерия технической проработки.

## 8. AI/ML/CV логика и использование датасета

Принцип: не обучать “черный ящик” на 10 positive examples. Датасет мал по confirmed violations и широк по документам, поэтому сильная стратегия - deterministic evidence pipeline + controlled AI assistance.

Слои извлечения:

1. `Document registry`: hash, file_id, object_id, stage, section, document_code, revision, approval status.
2. `Page registry`: page number, sheet number, page size, OCR flag, text source, annotation_count.
3. `Native text/table extraction`: PDF text layer, DOCX tables, XML fields, pdfplumber tables.
4. `OCR fallback`: только для страниц без text layer и релевантных crops; результаты кэшируются по file hash + page + crop.
5. `Semantic anchors`: поиск страниц по `parameter_name`, `trigger`, `source_pd/source_rd/source_id`, room/location/mark aliases.
6. `Coordinate normalization`: перевод `bbox_pdf` и найденных regions в `[0;1]`; сохранение `bbox_normalized`.
7. `Param rules`: правила сравнения по типам `number/string/boolean/enum/coordinate`, delta/status/rationale.
8. `Entity resolution`: связывает aliases, помещения, марки, элементы и атрибуты, чтобы EvidenceGroup показывал объект изменения.
9. `Suspicion engine`: логический, семантический, нормативный и ML-pattern поиск только в статусе `SUSPICION`.

Использование источников:

| Источник | Как использовать | Как не использовать |
|---|---|---|
| `document_manifest.jsonl` | основной реестр открытого пакета | не заменять filename heuristics без hash/file_id |
| `Матрица_параметров_редакция1.1.xlsx` | каноническая редакция матрицы, `M-001...M-132`, GOLD schema, acceptance metrics | не заменять existing gold codes без alias mapping |
| `parameter_catalog_132.jsonl` | runtime/scoring импорт тех же 132 параметров с `PZ/KR/IOS4/...` кодами | не считать его отдельной матрицей с другим содержанием |
| `Перечень_исполнительной_документации_редакция1_1.docx` | обязательный состав metadata для source selection: hash, revision, approval, predecessor/successor, sheet/page range, signature | не сравнивать stale/conflict revisions как нарушения |
| `Комплект_предметной_разметки_с_пояснениями_.pdf` | компактный demo/reference по графической фиксации вентиляционных расхождений | не считать его GOLD без связи с expert status |
| `submission_schema.json` | exporter/validator | не расширять submission так, чтобы ломать schema |
| `split_policy.json` | object-level leakage guard | не смешивать train/hidden по страницам |
| `public_gold_checks.jsonl` | smoke/e2e и public eval | не считать balanced training set |
| `all_gold_checks.jsonl` | локальная проверка scoring и edge cases | не обучаться на hidden organizer-only answers |
| `evidence_index.jsonl` | file/page evidence baseline | не требовать bbox там, где его нет |
| `learning_data/*/annotations.jsonl` | bbox/page viewer, candidate localization, weak labels | не считать AUTO_FIELD_CANDIDATE подтвержденным GOLD |
| `multi_object_annotation` | data quality, revision, stale/missing RD, duplicate cases | не считать SILVER/REVIEW нарушениями |

Приоритетные rule packs для demo/eval:

| Priority | Параметры / кейсы | Причина |
|---|---|---|
| P0 | `IOS4-078`, `IOS4-079` | публичные confirmed ventilation violations, лучший e2e demo |
| P0 | `KR-055`, `KR-058` | есть public/negative checks, хорошо показывает что система не overdetect |
| P0 | `PZ-009` | покрывает ПЗ и value/status comparison |
| P0 | `SPZU-027`, `SPZU-029`, `SPZU-036`, `SPZU-039` | hidden-style/site checks, важны для generalization и negative controls |
| P0 | `FREE-HEATING-001` | показывать только как `SUSPICION`, не official matrix violation |
| P1 | Top-annotated params из `learning_data` | быстро расширяют coverage dashboard |
| P2 | остальные 123 params без gold | applicability/status coverage, rules после стабилизации P0 |

## 9. Необходимые эксперименты и метрики качества

Эксперименты P0:

| Эксперимент | Метрики | Gate |
|---|---|---|
| Import integrity | counts files/pages/params/checks, SHA-256, split | 132 params, 203 train files, 213 hidden files, no missing IDs |
| Public e2e | finding precision/recall/F1, evidence page accuracy | Tyumenskaya positive найден, Novoslobodskaya negatives не превращены в violations |
| Submission exporter | JSON schema valid, required fields present | 100% valid against `submission_schema.json` |
| Evidence localization | exact file/page, bbox IoU where bbox available | file/page exact для scoring; bbox IoU >= 0.50 как внутренний gate |
| Value/status accuracy | normalized value match, protocol_status match | no mixed status fields; MISSING is not violation |
| OCR quality | CER/WER/character accuracy/coverage | character accuracy >= 0.95 на контрольных 300 DPI+ страницах |
| Performance smoke | upload, OCR, 132 checks, protocol, incremental, API p95 | не хуже порогов ТЗ на демо-выборке |
| Leakage guard | object-level split, hidden labels block | hidden organizer-only labels не доступны training code |

Метрики из scoring proposal должны быть видны в dashboard/evaluation report:

- `finding_detection_f1`;
- `source_localization_exact_file_page`;
- `normalized_value_and_status_accuracy`;
- `document_integrity_and_split_handling`;
- `false_positive_rate`;
- `coverage`;
- `abstention_rate`;
- per-section/per-parameter breakdown.

Интерпретация качества:

- высокий `recall` без контроля `precision` опасен, потому что negative checks входят в precision;
- `SUSPICION` не должен автоматически засчитываться как `VIOLATION_PRESENT`;
- `MISSING_DOCUMENT`, `COMPARISON_IMPOSSIBLE`, `NOT_COMPARABLE`, `CLARIFICATION_REQUIRED` должны повышать честность решения, а не скрываться;
- по 123 параметрам без gold нельзя заявлять обученную ML-точность, но можно показать deterministic coverage и transparent abstention.

## 10. Критичные доработки текущего проекта

### P0 - обязательно до финальной демонстрации

1. Добавить поддержку матрицы редакции 1.1 как `MatrixVersion official-132-v1.1`: `matrix_code=M-001...M-132`, `scoring_code`/aliases `PZ/KR/IOS4/...`, проверка 132 строк и 106/26 приоритетов.
2. Реализовать независимые positive rule packs для Tyumenskaya: `IOS4-078` и `IOS4-079` по room/system/equipment/duct observations из оригинального PDF, без чтения GOLD/check_id при inference.
3. Доказательно разрешить `RD_ID_MIXED` для Tyumenskaya на уровне конкретной страницы/наблюдения: если стадия не доказана, оставлять `CLARIFICATION_REQUIRED`, а не создавать finding.
4. Перезапустить real-data smoke и evaluation после `IOS4-078/079`: цель - хотя бы один независимый `CANDIDATE` positive e2e + сохранение Novoslobodskaya negative controls без false positives.
5. Зафиксировать метрики листа `МЕТРИКИ` в evaluation report: OCR accuracy, key fields exact match, document linkage, localization completeness, precision/recall/F1, FPR, object-level split, regression gate.
6. Заменить `window.prompt` для `Reject` на inline form/modal с reason code и comment, чтобы demo соответствовало инспекторскому workflow.
7. Сделать performance smoke по ключевым порогам ТЗ и отдельно указать, где результат измерен на demo corpus, а где остается production gap.
8. Добавить Node.js gateway с OpenAPI 3.0 для внешнего API ТЗ или явно вынести отсутствие Node в pitch risk.
9. Сделать минимальный React-shell для demo flow или явно вынести статический frontend в pitch risk.
10. Добавить RabbitMQ consumer/worker для V3 processing или честно показать synchronous fallback как demo mode и риск по технической проработке.
11. Обновить pitch materials: один independent positive сценарий, один negative/control сценарий, один Suspicion/Object Portrait сценарий, один slide с alias mapping `M-xxx <-> scoring code`.

### P1 - сильно повышает балл и надежность

1. Автоматический sync RAG SourceFragments после upload/reindex.
2. OCR page/crop cache с отчетом coverage и quality.
3. Table extraction для ТЭП, спецификаций, ведомостей, экспликаций.
4. Revision resolver по полям из перечня ИД 1.1: `approval_status`, `approval_date`, `predecessor_id`, `successor_id`, `sheet_page_range`, `signature_status`.
5. Data-quality checks: exact duplicates, same logical name different content, corrupt/unreadable, filename/title/code mismatch, temporary artifacts.
6. Coverage dashboard по 132 параметрам: checked/candidate/negative/missing/not comparable/not applicable плюс mapping `M-xxx/scoring_code`.
7. Inspector queue с сортировкой по priority/HIGH, confidence, source quality, novelty.
8. GOLD draft UI: что ушло в дообучение, почему, с какой меткой.
9. Mock IAIS adapter с retries, `PENDING_SYNC`, audit.
10. Prometheus metrics и структурированные JSON logs для demo.

### P2 - после стабилизации P0/P1

1. Расширение rules на все 132 параметра сверх статусов применимости и комплектности.
2. Сложный CV для чертежей: геометрия, линии, помещения, масштабные измерения, visual diff.
3. IFC geometry validation как дополнительный источник, не обязательный для ТЗ.
4. Реальное active learning/retraining после накопления >=100 confirmed violations.
5. Production security: TLS 1.3, encryption at rest, antivirus, IDS/DDoS, backup/restore automation, УКЭП.
6. Нагрузочное тестирование на 100 инспекторов и SLA/RTO/RPO контур.

## 11. Критерии готовности решения

Решение готово к финальной демонстрации, если выполнены все P0 gates:

| Gate | Условие готовности |
|---|---|
| Official matrix | API/UI показывают 132 параметра редакции 1.1: `M-001...M-132` + aliases `PZ/KR/IOS4/...`; demo matrix не используется как default. |
| Real data import | Уже импортированные real file/page/annotation индексы воспроизводимо проверяются; stage/object counts совпадают со статистикой датасета. |
| Evidence | Для candidates есть file_id, SHA-256, stage, page, role, extracted value; bbox показывается там, где доступен. |
| Public eval | Tyumenskaya `IOS4-078/079` находятся независимо от GOLD; Novoslobodskaya `PZ-009/KR-055/KR-058` остаются negative без critical false positives. |
| Submission | JSON валидируется against `submission_schema.json`. |
| Protocol | JSON/PDF содержит разделы приложения 2 и корректную кириллицу. |
| Inspector workflow | Confirm/Reject/Clarify/Finalize работают; reject требует reason; after-finalize mutation запрещена. |
| Incremental | Дозагрузка до finalization пересчитывает только affected params и создает новую protocol version. |
| Async | RabbitMQ worker потребляет process event или demo честно показывает fallback and risk. |
| Evaluation | Predictions export подключен к `evaluation/`; отчет показывает F1, localization, status/value, split integrity. |
| Performance | Есть benchmark report хотя бы на demo corpus с порогами ТЗ. |
| Pitch | Сценарий демонстрируется на реальном датасете, а synthetic используется только как fallback. |

## 12. Демонстрационный сценарий

Демо должно быть коротким, измеримым и проверяемым.

1. **Старт: официальный контур.** Показать матрицу 132 параметров редакции 1.1: `M-001...M-132`, aliases `PZ/KR/IOS4`, версии матрицы/датасета/модели, split guard и real document registry.
2. **Загрузка и обработка.** Загрузить или открыть заранее импортированный объект `OBJ-TYUMENSKAYA-5-GOLD-SEED`; показать stages `PD/RD/ID`, completeness, process status, RabbitMQ/event trail.
3. **Positive candidate.** Открыть `M-079 / IOS4-079` или `M-078 / IOS4-078`: side-by-side ПД/РД, file/page, highlighted bbox/page region, expected/actual/delta, rationale. Если независимый extractor еще не закрыт, этот шаг нельзя подменять GOLD replay.
4. **Инспекторское решение.** Нажать `Confirm`, показать audit log, protocol version increment, GOLD draft item.
5. **Negative control.** Открыть `OBJ-NOVOSLOBODSKAYA` по `PZ-009/KR-055/KR-058` и показать уже полученные независимые `NEGATIVE_VERIFIED`: система умеет не красить все отличия в critical.
6. **Дозагрузка.** Добавить документ до финализации, показать affected params и новую версию протокола; затем `FINALIZE` и запрет дальнейших изменений.
7. **Свободный поиск.** Показать `FREE-HEATING-001` как `SUSPICION`: это оригинальность, но не смешение с официальной матрицей.
8. **Object Portrait.** Открыть связанную сущность/помещение/систему и показать историю наблюдений, aliases, связанные изменения и evidence.
9. **Метрики.** Завершить отчетом evaluation: F1/localization/status/value/coverage/abstention/performance и явные риски.

Не показывать в финальном питче:

- synthetic dataset как основной кейс;
- claims о полном ML-обучении на 132 параметра;
- hidden organizer-only labels как часть обучения;
- автоматическое присвоение юридически значимого нарушения без инспектора;
- IFC как обязательный источник ТЗ.

## 13. Стратегия максимизации конкурсной оценки

| Критерий | Что показать | Чем доказать | Что должно быть реализовано |
|---|---|---|---|
| 1. Подход к решению задачи - уникальность и оригинальность | Не “чат с PDF”, а evidence-centric inspector workflow: official matrix + evidence cards + Object Portrait + Suspicion search. | Живой сценарий: нарушение -> evidence -> решение инспектора -> GOLD; рядом Object Portrait и free-search suspicion, отделенный от official matrix. | `EvidenceGroup`, `EvidenceFragment`, `InspectorDecision`, `GoldDraftItem`, `Object Portrait`, `Suspicion` statuses, side-by-side viewer. |
| 2. Техническая проработка решения | Версионирование матрицы/датасета/модели, SHA-256, revision graph, split guard, RabbitMQ, evaluation, audit. | Показать API/OpenAPI, DB records, event log, evaluation report, protocol version history, no-mutation after finalized. | Official importers, Node gateway, RabbitMQ worker, prediction exporter, audit log, protocol versions, evaluation CLI/API. |
| 3. Соответствие решения поставленной задаче | 132 параметра редакции 1.1, ПД/РД/ИД, PDF/DOCX/XML, протокол приложения 2, статусы ТЗ, верификация инспектором. | Matrix count = 132; `M-xxx` и aliases видны; protocol JSON validates; demo uses real CASE10 files; statuses match ТЗ. | Matrix 1.1 loader/alias mapping, document registry, scenario detector, protocol exporter, React/Node compliance layer or documented risk. |
| 4. Эффективность решения в рамках задачи | Сокращение ручной проверки: candidates with evidence, negative controls, incremental recompute, prioritized queue. | Метрики: F1, false positive rate, localization exact file/page, OCR accuracy, linkage, API p95, processing time, verification clicks/time. | Evaluation dashboard по листу `МЕТРИКИ`, performance smoke, P0 rules, abstention taxonomy, incremental update, inspector UI <=3 clicks per finding. |
| 5. Выступление на питче | Простая история: “нашли реальное несоответствие, доказали страницами, инспектор решил, протокол обновился, система учится”. | 7-минутный script, real-data demo, финальный metrics slide, честная карта рисков и roadmap. | Preloaded demo data, stable UI path, fallback screenshots/video, seeded metrics report, one-page architecture diagram. |

## 14. Главные риски после аудита

| Риск | Почему опасен | Как снизить |
|---|---|---|
| Матрица без редакции 1.1 / aliases | Можно пройти count=132, но не объяснить расхождение `M-xxx` и `PZ/KR/IOS4` в новых файлах ТЗ | P0 `matrix_code` + `scoring_code` + aliases; показать в UI и protocol metadata. |
| Нет independent positive e2e | Эксперты увидят real-data контур, но не увидят самостоятельного найденного нарушения | P0 `IOS4-078/079` extractor на оригинальном PDF без GOLD replay. |
| Static UI / Python API против React/Node ТЗ | Потеря баллов за соответствие и техническую архитектуру | Тонкий Node gateway + минимальный React-shell. |
| RabbitMQ без consumer | Асинхронность выглядит декоративной | Worker для processing events или честный fallback. |
| Мало confirmed gold | Нельзя доказать full ML quality | Rule/evidence strategy + transparent metrics + abstention. |
| False positives | Negative checks входят в precision | Negative controls, `CLARIFICATION_REQUIRED`, thresholds, inspector queue. |
| Низкое coverage после честного inference | Метрики могут выглядеть слабо без объяснения | Показать coverage/abstention честно и объяснить узкое gold-покрытие; расширять только P0 rules. |
| RD_ID_MIXED в Tyumenskaya | Легко создать ложное нарушение неправильной стадией | Доказательная классификация page/observation stage; иначе `CLARIFICATION_REQUIRED`. |
| Hidden leakage | Дисквалификационный/этический риск | Object-level split guard, organizer-only labels только eval/admin, не training. |
| Переусложнение IFC/CV | Съест время без закрытия ТЗ | IFC/CV оставлять P1/P2 после official matrix/evidence/protocol. |

## 15. Финальный порядок работ

1. Зафиксировать baseline: текущие tests, docker stack, known issues.
2. Ввести `MatrixVersion official-132-v1.1` из XLSX и alias mapping к уже работающим scoring-кодам.
3. Реализовать independent `IOS4-078/079` extraction для Tyumenskaya и снять `RD_ID_MIXED` только там, где стадия доказана.
4. Перезапустить real-data runtime smoke/evaluation и зафиксировать coverage, F1/FPR, localization, status/value accuracy.
5. Привести evaluation dashboard к метрикам листа `МЕТРИКИ`.
6. Улучшить inspector UI reject/clarify без `window.prompt`.
7. Добавить Node gateway/OpenAPI и минимальный React-shell или явно оформить эти пункты как pitch risk.
8. Добавить RabbitMQ worker или финально оформить synchronous fallback risk.
9. Прогнать performance smoke.
10. Подготовить pitch: demo script, metrics screenshot, architecture, alias mapping, risk slide.

## 16. Что считать выполнением CASE 10

Минимально сильная сдача - это не “все 132 параметра идеально распознаны ML-моделью”. С учетом фактического датасета это должно быть:

- 132 официальных параметра редакции 1.1 загружены и трассируемы через `M-xxx` и aliases `PZ/KR/IOS4/...`;
- каждый параметр получает честный статус: checked/candidate/missing/not applicable/not comparable/clarification;
- по покрытым gold параметрам есть реальные candidates/negative controls и измеримые метрики;
- каждый candidate имеет evidence card с file/page/value/status и bbox там, где он доступен;
- инспектор принимает финальное решение, а система не присваивает юридический статус сама;
- протокол версионируется, валидируется и воспроизводится по hash/matrix/model/dataset version;
- demo показывает реальный корпус, а не только synthetic;
- дополнительные фичи `Object Portrait`, `Suspicion`, IFC/RAG используются как усилители, не как подмена ТЗ.

Именно такая стратегия дает лучший шанс на 5/5 по каждому экспертному критерию: она честная к данным, практичная для хакатона, измеримая, демонстрируемая и при этом достаточно оригинальная, чтобы выделяться.
