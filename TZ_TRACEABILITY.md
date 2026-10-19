# Прослеживаемость требований ТЗ 1.1 → реализация → тест → статус

Дата составления: 2026-10-18, обновлено 2026-10-19 (флаги по умолчанию, шаги TZ-ON и Z2). Основа: `CASE10_FINAL_STATUS_REPORT.md` (состояние на `f0753c9`), отчёты шагов `evaluation/phase12/FINAL_*_REPORT.md`,
`FINAL_CARD_AUDIT.md` и код рабочего дерева. Документ не приукрашивает: «частично» и «нет» указаны там, где это так.

**Как читать.** Статус: **сделано** — есть код и тест, пункт работает на проверенных данных; **частично** — есть, но с названным ограничением;
**нет** — не сделано. «Сделано» не означает «качество принято»: числа качества стоят отдельно (раздел 7) и каждое привязано к отчёту замера.
Тесты — файлы в `api_service/tests/`; полный набор запускается из корня репозитория:
`PYTHONPATH="api_service;." venv/Scripts/python.exe -m pytest api_service/tests` (на дату `evaluation/phase12/FINAL_PAR_REPORT.md`: 1137 passed, 1 skipped).

**Флаги, меняющие поставку.** `CASE10_EXPLICATION_COMPARE` (S1), `CASE10_ROOM_DRAWING_COMPARE_ENABLED` (S2), `CASE10_FREE_HYPOTHESES` (гипотезы):
по решению от 18.10.2026 все три включены по умолчанию в поставке (`1` в `api_service/Dockerfile`, `docker-compose.case10.yml`,
`deploy/case10/case10.env.example`, таблица флагов в `README_GRADER.md`); соответствие документов и compose проверяет `evaluation/check_claims.py`.
Выключение — `0` в `.env`.

---

## 1. Состав системы — модули ТЗ раздела 7

| № | Модуль (приоритет ТЗ) | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|---|
| 1 | Загрузка и парсинг (High) | `app/domain/official_dataset.py`, `batch_package.py`, `file_registry.py`, `live_candidate_tagger.py`, `routes_upload.py`, `cli.py` | `test_case10_s3_file_registry.py`, `test_case10_live_candidate_tagger.py`, `test_case10_s6_delivery.py` | частично | Значения извлекаются из PDF; текст DOCX из интерфейса сохраняется (`CASE10_DOCX_TEXT_EXTRACTION_ENABLED=1`), значения по матрице из DOCX не извлекаются. Точность извлечения на отложенных объектах низкая (раздел 7) |
| 2 | Сравнение и протокол (High) | `v3_pipeline.py`, `comparison_gate.py`, `trigger_policy.py`, `evidence_groups.py`, `protocol_annex2.py` | `test_case10_comparison_gate.py`, `test_case10_trigger_policy.py`, `test_case10_domain.py`, `test_case10_protocol_document_analysis.py` | сделано | Полнота выявления расхождений — слабое место (раздел 7) |
| 3 | Верификация инспектором (High) | `inspector_workbench.py`, `inspector_card.py`, `v3_pipeline.record_inspector_decision`, `frontend_react/` | `test_case10_verification_lifecycle.py`, `test_case10_s5_workbench.py`, `test_case10_final_card.py` | сделано | Юзабилити-тест на 5 инспекторах не проводился |
| 4 | Обратная связь и дообучение (High) | `training_release.py`, таблицы `gold_draft_items`, `ml_retraining_logs`, `model_version_registry` | `test_case10_training_release.py` | частично | Есть воспроизводимый контур отбора и шлюз публикации; дообученной модели нет |
| 5 | Свободный поиск гипотез (High) | `free_hypotheses/`, `logical_analysis.py` | `test_case10_free_hypotheses.py`, `test_case10_logical_analysis.py`, `test_case10_final_r_recall.py` | частично | Из четырёх подходов на реальных данных работают два-три; ML-паттерн-анализ выключен (не прошёл порог точности), см. `FINAL_B2_HYPOTHESES_REPORT.md` |
| 6 | Интеграция с внешней ИС (Low) | `iais_rin_sync.py`, mock-сервер | `test_case10_iais_rin_sync.py` | частично | Эмуляция: реального контура ИАИС «РиН» и УКЭП нет; автоматического забора файлов из ИС нет |
| 7 | Дашборд инспектора (Medium) | `inspector_dashboard.py`, `GET /api/case10/dashboard`, `Dashboard.jsx`; экспорт: `protocol_export.py`, `GET /api/case10/protocols/{id}/pdf\|docx\|xml` | `test_case10_final_z_modules.py` | сделано | Цвет считается по текущим находкам, правило — в коде и в интерфейсе; фильтр дат — по дате создания проверки. Скриншоты — `FINAL_Z_TZ_MODULES_REPORT.md` |
| 8 | Управление нормативной базой (Medium) | `GET/POST/PATCH/DELETE /api/case10/normative-base`, таблица `normative_base`, `NormativeBase.jsx`, `free_hypotheses/normative.py` | `test_case10_tz10_tables.py`, `test_case10_free_hypotheses.py` | сделано | Вкладка администратора есть; активная запись с порогом участвует в нормативном анализе гипотез; пустая база не добавляет находок. Нормы предметными экспертами не валидировались (`FINAL_Z2_TZ_GAPS_REPORT.md`) |
| 9 | Журнал аудита (Medium) | `AuditLog`, `v3_pipeline.add_audit`, версии протоколов | `test_case10_api.py`, `test_upload_security.py`, `test_case10_verification_lifecycle.py` | сделано | — |
| 10 | Еженедельный отчёт по дообучению (Medium) | `ml_weekly_report.py`, `ml_weekly_scheduler.py`, `GET /api/case10/ml/reports/weekly`, `python -m app.cli weekly-report`, `MlReport.jsx` | `test_case10_final_z_weekly.py` | сделано | Отчёт строится по запросу, по команде CLI и встроенным расписанием в процессе API (`CASE10_ML_WEEKLY_REPORT_SCHEDULER_ENABLED=1`, `/data/reports`, период 7 суток). При малом числе решений отчёт пишет «данных мало» и не даёт рекомендаций (`FINAL_TZON_REPORT.md`) |
| 11 | Мониторинг и логирование (Medium) | `logging_setup.py`, `app/metrics.py` (формат Prometheus), таблица `monitoring_metrics` | `test_logging_json.py` | частично | Нет Grafana, ELK и алертинга |
| 12 | Обработка ошибок (High) | `batch_package.py`, `document_versions.py`, `routes_upload.py`, `v3_jobs.py` | `test_case10_ocr_hardening.py`, `test_upload_security.py`, `test_case10_api.py` | сделано | Время OCR ограничено по стенным часам (порог зависит от скорости машины) |

## 2. Матрица контроля (раздел 8)

| Пункт ТЗ | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|
| Таблица `Params` (поля 8.1), 132 параметра | `db/models.py` (`Param`), `reference_data/`, `matrix_v11.py`, `matrix_v11_codes.json` | `test_case10_p0_conventions.py`, `test_case10_domain.py` | сделано | Коды выдачи M-001…M-132 (`CASE10_PARAMETER_CODE_STYLE=matrix11`) |
| Извлечение по каждому из 132 параметров | генерические извлекатели, rule-pack'и, экспликации | `test_case10_generic_extraction.py`, `test_case10_compound_extraction.py`, `test_case10_enum_class_extraction.py`, `test_case10_table_row_count_extraction.py`, `test_case10_s1_explication.py` | частично | Часть кодов не имеет механизма и честно даёт `MISSING_EVIDENCE`/`NOT_COMPARABLE`; перечень — `CASE10_MATRIX_132_COVERAGE.md` |

## 3. Модуль 9.1. Загрузка и парсинг

| Пункт ТЗ | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|
| Приём PDF, DOCX, XML | `routes_upload.py`, `batch_package.py` | `test_case10_api.py` (XML), `test_case10_s6_delivery.py`, `test_case10_docx_source.py` | частично | PDF — основной путь; XML — как источник фрагментов; DOCX через интерфейс сохраняется как текстовый фрагмент без геометрии, пакетный путь и CLI принимают только PDF |
| Определение стадии, шифра, редакции, статуса утверждения | `file_registry.py`, реестр файлов (Перечень ИД 1.1) | `test_case10_s3_file_registry.py` | сделано | Без реестра пакет принимается как `CLARIFICATION_REQUIRED` |
| Выбор актуальной редакции, конфликт → `CLARIFICATION_REQUIRED` | `document_versions.py`, `document_order.py`, `comparison_gate.py` | `test_case10_comparison_gate.py`, `test_case10_s3_file_registry.py` | сделано | — |
| OCR (Character Accuracy ≥ 0,95) | Tesseract 5.5 (rus+eng), `ocr_paddle.py` (не по умолчанию) | `test_case10_ocr_hardening.py`, `test_case10_ocr_paddle.py` | частично | Текстовый слой — норма выполнена; сканы — ниже нормы (раздел 7) |
| `LOW_QUALITY` / `ABSTAIN` для нечитаемых зон | `official_evidence._quality_for_page`, `page_quality_cache.py` | `test_case10_page_quality_perf.py`, `test_case10_ocr_hardening.py` | сделано | — |
| NLP: regex + Sentence-BERT | `live_candidate_tagger.py`, `semantic_similarity.py`, `anchor_search.py` | `test_case10_semantic_similarity.py`, `test_case10_anchor_search.py` | сделано | Модель `paraphrase-multilingual-MiniLM-L12-v2` вместо английской `all-MiniLM-L6-v2` (замена обоснована замером, `LICENSES_MODELS.md`) |
| CV-анализ чертежей: масштаб, линии, расстояния | — | — | нет | Компоненты компьютерного зрения нет; чертежи ОВ сравниваются по текстовым меткам (`room_drawing_compare.py`) |
| Сохранение file_id, SHA-256, стадии, шифра, редакции, статуса, листа, bbox в [0;1] с CropBox/MediaBox/Rotate | `dataset_sources.py`, `evidence_groups.py`, `EvidenceFragment` | `test_case10_rotation.py`, `test_case10_domain.py` | сделано | Поворот страницы учтён |
| Кэширование по хешу файла (Redis) | дисковый кэш по SHA-256 (`live_tagger_scan.py`, `page_quality_cache.py`) | `test_case10_live_tagger_determinism.py`, `test_case10_page_quality_perf.py` | частично | Вместо Redis — дисковый кэш; Redis предусмотрен настройкой, в офлайн-режиме отключён |
| Инкрементальная дозагрузка до финализации | `v3_pipeline` (пересчёт по затронутым параметрам), `SupplementDialog.jsx` | `test_case10_incremental.py`, `test_case10_final_card.py` | сделано | — |
| Ошибки загрузки: формат, битый PDF, 50 МБ, 200 МБ, таймаут | `routes_upload.py`, `config.py`, `v3_jobs.py` | `test_upload_security.py`, `test_case10_api.py` | сделано | Лимиты 50/200 МБ — только для интерфейса; пакетный путь без лимитов (позиция экспертов) |
| Статусы загрузки `PD_UPLOADED … ID_MISSING` | `v3_pipeline.document_completeness`, `registry_completeness.py`, `protocol_annex2.py` | `test_case10_api.py`, `test_case10_domain.py` | сделано | Есть `*_UPLOADED`, `*_PARTIAL` (уникальные строки реестра против загруженных записей; `CASE10_PARTIAL_UPLOAD_STATUS_ENABLED=1`) и `*_MISSING`; массив `checks` статусами не меняется (`FINAL_TZON_REPORT.md`) |
| Статусы процесса `PENDING … FINALIZED` | `v3_pipeline.py` | `test_case10_verification_lifecycle.py` | сделано | — |

## 4. Модули 9.2 и 9.3. Сравнение, протокол, верификация

| Пункт ТЗ | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|
| Сценарии `FULL / PD_RD_ONLY / PD_ID_ONLY / RD_ID_ONLY / SINGLE_ONLY / PARTIALLY_LOADED` | `v3_pipeline.upload_scenario` | `test_case10_domain.py` | сделано | — |
| Порядок «применимость → комплектность → актуальность → предметное сравнение» | `comparison_gate.py` | `test_case10_comparison_gate.py` | сделано | — |
| Статусы параметров (`CANDIDATE`, `NEGATIVE_VERIFIED`, `MISSING_EVIDENCE`, `NOT_APPLICABLE`, `NOT_COMPARABLE`, `CLARIFICATION_REQUIRED`, `SUSPICION`) | `v3_pipeline.py`, `evidence_tier.py` | `test_case10_final_b1_tiers.py`, `test_case10_verification_lifecycle.py` | сделано | `CONFIRMED_VIOLATION` ставит только инспектор |
| Триггеры матрицы | `trigger_policy.py` (таблица 132 триггеров) | `test_case10_trigger_policy.py` | частично | 38 триггеров помечены для экспертного ревью |
| ПД — эталон | `comparison_gate.py`, `official_evidence.py` | `test_case10_comparison_gate.py` | частично | 15 кодов с абсолютным порогом сравниваются с нормой, а не с ПД |
| Протокол JSON/PDF по Приложению 2; разделы; версии | `protocol_annex2.py`, `protocol_export.py`, `Protocol` | `test_case10_protocol_document_analysis.py`, `test_case10_final_z_modules.py` | сделано | PDF, DOCX и XML строятся из одних данных |
| Карточка доказательства (все поля 9.2) | `evidence_groups.py`, `inspector_card.py`, `evaluation/exporter.py` | `test_case10_evaluation.py`, `test_case10_final_card.py` | сделано | Вероятность нарушения не заявляется: способа расчёта нет |
| Инкрементальное обновление, предыдущая версия протокола сохраняется | `v3_pipeline.create_protocol_version`, `_protocol_diff` | `test_case10_incremental.py`, `test_case10_api.py` | сделано | — |
| Подтвердить / отклонить (`reason_code`, комментарий) / уточнить | `record_inspector_decision`, `DecisionBar.jsx` | `test_case10_verification_lifecycle.py` | сделано | — |
| Одновременный просмотр ПД/РД/ИД с выделенными областями | `Workbench.jsx`, `StagePanels.jsx`, `inspector_card.py` | `test_case10_final_card.py`, `test_case10_s5_workbench.py` | сделано | Проверено в браузере на демо-пакетах (`FINAL_DEMO_REPORT.md`) |
| Составной кандидат разделяется на атомарные части | `inspector_card.record_part_decisions` | `test_case10_final_card.py` | сделано | — |
| Выбор редакции с обоснованием, правки доказательств без перезаписи | `inspector_workbench.py`, `InspectorEdit` | `test_case10_s5_workbench.py` | сделано | — |
| Финализация / отмена финализации (администратор, супервизор, причина) | `finalize_protocol`, `unfinalize_protocol` | `test_case10_verification_lifecycle.py` | сделано | — |
| ≤ 3 действий на нарушение; ≤ 30 мин на протокол; юзабилити-тест на 5 инспекторах | `VerificationTimer.jsx`, `VERIFICATION_TIME_MEASURED` | `test_case10_s5_workbench.py` | частично | Число действий замерено; время и тест с инспекторами не проводились |

## 5. Модули 9.4–9.6

| Пункт ТЗ | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|
| 9.4 GOLD только из `CONFIRMED_VIOLATION` и `NEGATIVE_VERIFIED` с полными карточками | `training_release.py`, `GoldDraftItem` | `test_case10_training_release.py` | сделано | — |
| 9.4 Разбиение по объектам, хеши наборов, шлюз публикации (регресс ≤ 2 п. п.) | `training_release.py`, `MLRetrainingLog` | `test_case10_training_release.py` | сделано | Решение о публикации подписывается человеком; автоматической публикации нет |
| 9.4 Лог отклонений и спорных случаев | `RejectionLog`, `DisputeLog`, `rejection_feedback.py`, `POST /case10/evidence-groups/{id}/disputes` | `test_case10_tz10_tables.py` | сделано | `ai_verdict` сравнивает исходный статус ИИ с решением инспектора; `suggested_fix` берётся из детерминированного справочника по `reason_code` — это правило, не модель (`FINAL_Z2_TZ_GAPS_REPORT.md`) |
| 9.4 Еженедельный отчёт (модуль 10) | `ml_weekly_report.py`, `ml_weekly_scheduler.py` | `test_case10_final_z_weekly.py` | сделано | См. строку 10 раздела 1 |
| 9.5 Логический анализ | `logical_analysis.py`, `free_hypotheses/logic_rules.py` | `test_case10_logical_analysis.py`, `test_case10_free_hypotheses.py` | частично | Три правила по СП в рабочем виде; правила LR-101…106 выключены по умолчанию |
| 9.5 Семантический диссонанс | `free_hypotheses/` (SEMANTIC_DISSONANCE) | `test_case10_free_hypotheses.py` | частично | Подтверждено на малой выборке (2 из 2, `FINAL_B2_HYPOTHESES_REPORT.md`) |
| 9.5 Нормативный анализ | `free_hypotheses/` (NORMATIVE_ANALYSIS) | `test_case10_free_hypotheses.py` | частично | Тихий на реальных данных: нет входных значений |
| 9.5 ML-паттерн-анализ | `free_hypotheses/` (ML) | `test_case10_free_hypotheses.py` | нет | Реализован, но выключен: не прошёл порог точности (`FINAL_B2_HYPOTHESES_REPORT.md`) |
| 9.5 Структура `Suspicion`, дедупликация, `SUSPICION` не входит в число нарушений | `Suspicion`, `evidence_tier.py`, `protocol_annex2.py` | `test_case10_free_hypotheses.py`, `test_case10_final_b1_tiers.py` | сделано | Гипотеза преобразуется в кандидата только при полных источниках и по решению инспектора |
| 9.6 Передача результатов в ИАИС «РиН», только `PROTOCOL_FINALIZED`, повторы 1/5/15 мин, `PENDING_SYNC` | `iais_rin_sync.py` | `test_case10_iais_rin_sync.py` | частично | Работает против mock-сервера; реальной интеграции и УКЭП нет |
| 9.6 Забор файлов из ИС; блокировка автозагрузки при финализации | `iais_rin_sync.py`, `clients/iais_rin_client.py` | `test_case10_iais_rin_sync.py` | частично | Входящий pull-адаптер работает против mock-сервера и по умолчанию выключен (`CASE10_IAIS_RIN_PULL_ENABLED=0`); финализированный протокол отклоняет загрузку. Реальной ИС нет |

## 6. Разделы 10–13. Данные, производительность, безопасность, мониторинг

| Пункт ТЗ | Реализация | Тест | Статус | Ограничение |
|---|---|---|---|---|
| Раздел 10: 16 таблиц | `db/models.py`, `alembic/`; часть таблиц под другими именами (`evidence_groups` = Checks, `document_versions` = Files, `gold_draft_items` = Dataset_Items) | `test_case10_tz10_tables.py` | сделано | Названия полей следуют словарю проекта, не буквам ТЗ |
| Раздел 11: время сравнения, протокола, OCR, API | `evaluation/performance_smoke.py`, замеры S6 | `test_case10_s6_delivery.py` (запуск), замеры — `S6_REPORT.md` | частично | Замеры CLI и стенда — `evaluation/phase12/FINAL_ACCEPTANCE_E2_REPORT.md`, `FINAL_PERF2_REPORT.md`, `FINAL_STAND_REPORT.md`; 100 одновременных пользователей, SLA, RTO/RPO, CV-анализ DWG не проверялись. Ответ `protocols/current`: p95 220–240 мс в Docker при пороге ТЗ 200 мс (`FINAL_ACCEPTANCE_E2_REPORT.md`); после правок на хосте p95 33–54 мс, в Docker не перемерено (`FINAL_STAND_REPORT.md`) |
| Раздел 12.1–12.2: аутентификация, роли | `routes_auth.py`, `api/auth.py` (`require_ml_engineer`, `require_supervisor`, `require_admin`) | `test_upload_security.py` | сделано | — |
| 12.3 Шифрование при передаче и в покое | `deploy/case10/nginx.https.conf.template` | — | частично | TLS — шаблон стенда; по умолчанию локальный HTTP; шифрования данных в покое нет |
| 12.4 Журнал аудита с IP | `AuditLog.ip_address`, `request_context.py` | `test_upload_security.py` | сделано | — |
| 12.5, 12.8–12.9, 12.11 Сроки хранения, резервное копирование, IDS/DDoS, антивирус | — | — | нет | Не реализовано (вторичные критерии по экспертной сессии) |
| 12.9 Усиление среды выполнения (необязательное) | `docker-compose.case10.hardened.yml`: `no-new-privileges`, `cap_drop ALL`, `read_only` | — (проверено вручную, `evaluation/phase12/FINAL_ACCEPTANCE_E2_REPORT.md`) | частично | Включается вторым `-f`; на образе `:e2` поднимается, корневая ФС api только для чтения, NOV и ALT79B обработаны до конца; api и worker работают от root (модель лежит в `/root/.cache`), для запуска не от root нужен новый образ |
| 12.6–12.7 Персональные данные, целостность | SHA-256 файлов и цепочек редакций | `test_case10_s3_file_registry.py` | частично | Ежедневной сверки хешей нет |
| 12.10 Подпись запросов УКЭП | точка интеграции в `iais_rin_sync.py` | `test_case10_iais_rin_sync.py` | частично | Эмуляция |
| 13.1–13.2 Структурированные JSON-логи с обязательными полями | `logging_setup.py` | `test_logging_json.py` | сделано | — |
| 13.4 Метрики Prometheus | `app/metrics.py`, `MonitoringMetric` | `test_case10_tz10_tables.py` | частично | Набор метрик неполный (нет RabbitMQ, сессий) |
| 13.3, 13.5–13.8 Хранение логов, Prometheus+Grafana, ELK, алертинг, проверка хешей | — | — | нет | Не реализовано |

## 7. Раздел 14. Приёмка качества

Числа — только со ссылкой на отчёт замера. Приёмка поставки в Docker выполнена шагами E1 и E2 (`evaluation/phase12/FINAL_ACCEPTANCE_REPORT.md`, `evaluation/phase12/FINAL_ACCEPTANCE_E2_REPORT.md`); метрики качества на образе `:e2` не пересчитывались.

| Метрика (порог ТЗ 14.3) | Где измерено | Статус | Комментарий |
|---|---|---|---|
| OCR Character Accuracy ≥ 0,95 | `evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md`, `OCR_FALLBACK_HARDENING_REPORT.md` | частично | Текстовый слой — выше порога; сканы Tesseract — ниже порога |
| Ключевые поля Exact Match ≥ 0,90 | `evaluation/OCR_GOLD_REVIEW_PASS1_REPORT.md` | частично | Выборка мала (8 примеров), порог не подтверждён |
| Связка документов ≥ 0,95 | `evaluation/phase12/S3_REPORT.md`, `test_case10_s3_file_registry.py` | сделано | На публичных объектах; выборка мала |
| Локализация доказательства (IoU ≥ 0,5) ≥ 0,95 | `evaluation/phase12/S1_REPORT.md` | частично | Подтверждено на найденных кейсах (2 из 2); репрезентативной выборки нет |
| Precision ≥ 0,90; Recall ≥ 0,80; F1 ≥ 0,85 | `evaluation/phase12/PHASE12_BASELINE.md`, `S1_REPORT.md`, `FINAL_R_RECALL_REPORT.md` | нет (recall) | Recall по официальным примерам без разметки организатора ниже порога; точность на размеченных выборках выше порога, но выборки малы |
| FPR ≤ 0,10 на отрицательных и устаревших редакциях | `evaluation/phase12/S1_REPORT.md`, `evaluation/phase12/FINAL_B_TIERS_REPORT.md` | сделано | На проверенных отрицательных и посевах; выборки малы |
| Интервалы Уилсона, размер выборки, coverage в метриках | `evaluation/metrics.py`, `evaluation/grader_sim.py` | сделано | `test_case10_evaluation.py`, `test_case10_p0_grader_sim.py` |
| Версии `dataset_version`, `matrix_version`, `model_version`, `input_manifest_hash` в каждом результате | `evaluation/exporter.py`, `protocol_annex2.py` | сделано | `test_case10_evaluation.py` |

## 8. Что не закрыто (кратко)

1. Полнота выявления реальных расхождений на новых документах (раздел 7, строка Recall); измерение на финальном коде — шаг E1.
2. Нет CV-анализа чертежей, извлечения значений матрицы из DOCX, реальной интеграции ИАИС «РиН» и УКЭП.
3. Нет шифрования в покое, резервного копирования, антивирусной проверки, IDS/DDoS, Grafana/ELK/алертинга, ежедневной сверки хешей.
4. Забор файлов из внешней ИС и интеграция ИАИС «РиН» — только эмуляция на mock-сервере.
5. Нет юзабилити-теста на 5 инспекторах и замера времени верификации на людях.
6. Лицензия PyMuPDF (AGPL-3.0) — для коммерческого внедрения нужна коммерческая лицензия или замена (`LICENSES_MODELS.md`).

Средства самопроверки (не метрики качества): `python -m app.cli selftest` — около двух десятков синтетических сценариев на сгенерированных PDF
(`app/selftest.py`, тесты `test_case10_final_z_selftest.py`); `python evaluation/check_claims.py` — проверка формулировок в документах поставки.

Проверка формулировок в документах поставки: `python evaluation/check_claims.py` (код выхода не нуль при утверждениях без ссылки на замер).
