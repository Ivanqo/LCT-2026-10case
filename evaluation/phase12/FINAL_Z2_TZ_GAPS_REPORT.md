# Z2 — закрытие выполнимых пробелов TZ_TRACEABILITY

Дата: 2026-10-19. Ветка: `codex/z2-tz-gaps`, база: `bd2a1e84`. Рабочее дерево изолировано; `TZ_TRACEABILITY.md` и защищённые файлы не менялись. Новых зависимостей нет.

## Пункт → предлагаемый новый статус

| Пункт TZ_TRACEABILITY | Новый статус | Что проверено / ограничение |
|---|---|---|
| Модуль 8, строка 28: управление нормативной базой | Сделано в приложении | Добавлена вкладка администратора: создать, изменить, деактивировать запись, задать min/max и период действия. Активная база участвует в проверке порогов; пустая база сохраняет прежний результат. Нормы предметных экспертов отдельно не валидировались. |
| Модуль 10, строка 30: еженедельный отчёт | Сделано, опционально | Фоновый планировщик выключен по умолчанию, настраивается через env, сохраняет JSON в `/data/reports`; доступны API и список во вкладке «Отчёт ML». Сводка планировщика — на организацию. |
| 9.4, строка 84: лог отклонений и споров | Сделано правилом | `RejectionLog` сравнивает исходный статус ИИ с решением инспектора; `DisputeLog` заполняет статус ожидания, пока решения нет. `suggested_fix` выбирается из детерминированного справочника `reason_code`. Это правило, не модель. |
| Строка 56: `PD_PARTIAL/RD_PARTIAL/ID_PARTIAL` | Сделано, opt-in | Сверяются уникальные строки реестра и загруженные записи пакета; статус попадает в API и Annex 2. Флаг выключен по умолчанию. |
| 9.6, строка 92: забор файлов и блокировка после финализации | Частично, эмуляция | Добавлен входящий pull-адаптер для mock ИС; финализированный протокол отклоняет загрузку, состояние перепроверяется после сетевого запроса. Реальный контур ИАИС «РиН» и УКЭП отсутствуют. |
| Строки 21 и 45: извлечение текста DOCX | Частично, opt-in | Извлекается текст основного WordprocessingML и добавляется в общий конвейер текстовых фрагментов. Нет геометрии листа/bbox и разбора табличной вёрстки как графического документа. Флаг выключен по умолчанию. |

Не выполнялись CV-анализ, шифрование данных в покое, Grafana/ELK, антивирус/IDS, реальная интеграция ИАИС «РиН» и УКЭП — согласно ограничению задачи.

## Переключатели

- `CASE10_ML_WEEKLY_REPORT_SCHEDULER_ENABLED=0` — планировщик по умолчанию выключен; интервал и путь задаются `CASE10_ML_WEEKLY_REPORT_INTERVAL_SECONDS` и `CASE10_ML_WEEKLY_REPORTS_DIR`.
- `CASE10_PARTIAL_UPLOAD_STATUS_ENABLED=0` — registry-aware `*_PARTIAL` выключен по умолчанию.
- `CASE10_IAIS_RIN_PULL_ENABLED=0` — mock-забор выключен по умолчанию.
- `CASE10_DOCX_TEXT_EXTRACTION_ENABLED=0` — извлечение DOCX выключено по умолчанию.
- Новая нормативная ссылка влияет только если администратор добавил активную запись с порогом, действующую на дату проверки; пустая таблица не добавляет находок.

## Проверки

- Целевой набор Python-тестов: **132 passed**, 337 предупреждений, 100.48 с.
- Полный `pytest api_service/tests -q`: **1074 passed, 1 skipped, 40 subtests passed**, 604 предупреждения, 271.36 с. Worktree использовал `case_data` и `learning_data` как junction к validation-данным основной копии; код их не импортирует для решений.
- `npm run lint`: чисто. `npm run build`: чисто, Vite обработал 55 модулей; build занял 1.23 с.
- При пустой нормативной базе и отключённых новых флагах сравнивались байты `result.json` с исходным `bd2a1e8`; для каждого прогона baseline и новая ветка использовали одну staged-папку, `--workers 1`, одинаковые demo-флаги, таймаут 25 минут на команду:

| Объект | Побайтно равно | Размер | SHA-256 | Checks |
|---|---:|---:|---|---:|
| NOV | Да | 383 073 байта | `dd5926aaf334a94aeeef0cd55bdca899df1542005e8aee2c9e45826cdd285d5f` | 140 |
| ALT79B | Да | 927 658 байт | `a7149d3586201e7371a9629a0afeea929cde32578fb178d222b8ef0ffbcd137a` | 192 |

- `git diff --check` с `core.whitespace=cr-at-eol`: чисто. Смешанные окончания CRLF/LF сохранены.

## Файлы

- Backend: `api_service/app/api/routes_case10.py`, `routes_upload.py`, `batch_package.py`, `clients/iais_rin_client.py`, `config.py`, `db/models.py`, `db/session.py`, `domain/free_hypotheses/normative.py`, `domain/iais_rin_sync.py`, `domain/ml_weekly_report.py`, `domain/protocol_annex2.py`, `domain/v3_pipeline.py`, `main.py`; новые `domain/ml_weekly_scheduler.py`, `domain/registry_completeness.py`, `domain/rejection_feedback.py`.
- Frontend: `frontend_react/src/App.jsx`, `api.js`, `components/CompletenessPanel.jsx`, `components/MlReport.jsx`, `components/NormativeBase.jsx`, `components/ProcessPanel.jsx`, `labels.js`, `styles.css`.
- Тесты: `api_service/tests/test_case10_api.py`, `test_case10_domain.py`, `test_case10_final_z_weekly.py`, `test_case10_free_hypotheses.py`, `test_case10_iais_rin_sync.py`, `test_case10_s5_workbench.py`, `test_case10_tz10_tables.py`; новый `test_case10_docx_source.py`.
- Отчёт: `evaluation/phase12/FINAL_Z2_TZ_GAPS_REPORT.md`.

## Не измерено и риски

- Не измерялись полнота/точность поиска, OCR на сканах и метрики скрытой оценки; изменение этих метрик не заявляется.
- DOCX проверен на простом тексте, не на реальных комплектах со сложной табличной вёрсткой.
- Pull и блокировка финализации проверены на mock-клиенте; гарантии реальной ИС, УКЭП и её SLA отсутствуют.
- Планировщик использует файловый маркер интервала; одновременный запуск нескольких API-worker процессов не измерялся.
