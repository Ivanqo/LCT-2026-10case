# S5 — Рабочее место инспектора под сценарий экспертов (отчёт, 2026-09-25, Д1)

Источники требований: бэклог ред. 11 (разделы 0–2, «Общий контекст Фазы 12», промпт S5), конспект экспертной сессии §13–17, §19, §27, §35, §39.
Статус: **все 7 пунктов промпта сделаны и проверены в браузере на двух демо-объектах; тесты зелёные** (полный прогон 770/770, из них 15 новых S5). Функции можно дошлифовывать до заморозки вечером Д3.

## 1. Что сделано — по пунктам промпта

| # | Требование | Где | Проверка |
|---|---|---|---|
| 1 | Единое окно: синхронные панели ПД / РД / ИД — файл, шифр, редакция, статус утверждения, страница, выделенный bbox, «Источник ↗», масштаб; ИД-панель скрыта, если ИД нет | `StagePanels.jsx`; страницы — `GET /case10/document-versions/{id}/pages/{n}.png` (оригинал без подсветки, рамки рисует клиент), `…/{n}/geometry`, `…/{n}.pdf` (исходная страница, вырезанная из оригинала: вектор и текстовый слой) | скрины 02, 03, 11; тест `test_page_geometry_png_and_source_pdf_of_a_rotated_page` |
| 2 | Карточка доказательства со всеми полями §14 | `EvidenceCardFields.jsx`; `GET /case10/evidence-groups/{id}/workbench` → `card` (ID находки `F-…` / `EG-…` / #id, object_id, M-код + legacy, правило (rule_version, сценарий, экстракторы, триггер матрицы), стадии, файлы, шифр, редакция, статус, страница, bbox, эталон/извлечённое, описание, confidence, решение) | скрин 02; тест `test_queue_and_card_expose_expert_session_fields` |
| 3 | Решение за ~3 действия, горячие клавиши; комментарий обязателен при отклонении | `DecisionBar.jsx`, `Workbench.jsx`: открыть → **C / R / U** → **Enter** (Ctrl+Enter в поле комментария); J/K — следующий/предыдущий, +/−/A — масштаб, X — отметить; после сохранения — автопереход к следующему нерешённому | скрины 04–06; журнал: `DECISION_UI_METRICS.actions = 3` во всех замерах |
| 4 | Выбор редакции с обоснованием; добавление / удаление / уточнение фрагментов — новая версия с историей (user_id, timestamp, причина, ссылка на исходную сущность, прежнее значение); исходники, OCR и машинный вывод не перезаписываются | модель `InspectorEdit` (`db/models.py`) + миграция `alembic/versions/5a1c0e7d9b21_add_inspector_edits.py` + `domain/inspector_workbench.py` + API (`routes_case10.py`) + UI `EvidenceEditor.jsx`, `RevisionsPanel.jsx` | скрины 09–11, 14–16; 8 тестов версионирования и выбора редакции; миграция проверена upgrade → downgrade → upgrade |
| 5 | Групповые операции для однотипных; массовое отклонение — только с явным подтверждением | `CandidateQueue.jsx` (чекбоксы, «Отметить однотипные», панель групповых действий), `POST /case10/evidence-groups/bulk-decisions` | скрины 07, 08; тесты `test_bulk_rules`, `test_bulk_is_all_or_nothing_on_a_technical_status` |
| 6 | Очередь low-confidence / LOW_QUALITY, фильтры по разделам и статусам, панель комплектности «загружено / ожидалось / основание» | очереди в `CandidateQueue.jsx` (Кандидаты / Низкая уверенность-LOW_QUALITY / Уточнение / Решённые / Подозрения / Все + раздел + статус + поиск + порог); `CompletenessPanel.jsx` + `GET /case10/processes/{id}/completeness` | скрины 12, 17; тесты quality-флагов и комплектности |
| 7 | Замер времени верификации (от открытия протокола до финализации) — в журнал | `POST …/verification/open` (открытие протокола в рабочем месте запускает часы, один раз за цикл), финализация пишет `VERIFICATION_TIME_MEASURED` в `audit_logs`; каждое решение — `DECISION_UI_METRICS` (действия и время от открытия кандидата); виджет таймера в шапке | скрин 13; тест `test_verification_time_from_open_to_finalize_lands_in_audit_log` |

## 2. Как устроено версионирование (п. 4)

Одна таблица `inspector_edits`, только добавление строк: **одна строка = одна новая версия одной сущности**.

- `entity_type = EVIDENCE_FRAGMENT`, ключ `machine:<evidence_fragments.id>` (уточнён / исключён / восстановлен машинный фрагмент) или `manual:<id строки ADD>` (добавлен инспектором); `entity_type = REVISION_CHOICE`, ключ `revision:<scope>`.
- Поля §13: `user_id`, `created_at`, `reason` (обязательна, ≥ 3 символов), ссылка на исходную сущность (`source_fragment_id` / `source_document_version_id` / `previous_edit_id`), `previous_value` → `new_value` (полные снимки фрагмента), `version` (1, 2, 3… на ключ; уникальность `(process_id, entity_key, version)`).
- **Эффективные доказательства** = машинный вывод + воспроизведённые поверх него версии (`effective_fragments`). Строки `evidence_fragments`, `source_fragments` и исходные файлы не меняются никогда — тест сравнивает все колонки всех машинных фрагментов до и после серии правок побайтно.
- «Исключить» не удаляет, а создаёт версию со статусом `REMOVED`; «Восстановить» — версию `ACTIVE`. Если пересчёт заменил машинный фрагмент, правки инспектора остаются видимыми со статусом `ORPHANED` (история не теряется; FK `evidence_group_id … ON DELETE SET NULL`).
- Каждая правка дублируется в `audit_logs` (`EVIDENCE_FRAGMENT_ADDED/REFINED/REMOVED/RESTORED`, `REVISION_CHOSEN_BY_INSPECTOR`).
- Рамка: инспектор обводит область прямо на странице; хранится `bbox_norm` в видимой системе координат страницы (после /Rotate) и пересчитанный `bbox_pdf` — та же система, что у машинных фрагментов и у рендера. Проверено на повёрнутой на 90° странице (тест).
- После финализации правки запрещены (409), как и решения.

## 3. Контракт с S3 (реестр, редакции, комплектность)

Бэкенд S3 пока не существует (`file_registry.py` нет), поэтому сделаны заглушки с тем же контрактом. Как только S3 положит в `app/domain/file_registry.py` любую из функций ниже, рабочее место начнёт вызывать её без правок UI (`_s3_hook`, покрыто тестом с подменным модулем):

| Функция S3 | Что должна вернуть | Заглушка S5 до готовности |
|---|---|---|
| `revision_scopes(db, process) -> list[dict]` | `scope_key, stage, scope, type, system_status (RESOLVED / CLARIFICATION_REQUIRED), system_choice_id, basis, candidates[{document_version_id, file_id, file, document_code, revision, approval_status, approval_date, predecessor_id, successor_id}]` | существующий анализ редакций `document_facts` (конфликты серий и неупорядоченные редакции → `CLARIFICATION_REQUIRED`; замены по маркерам изм./ред. → `RESOLVED`) + цепочки predecessor/successor |
| `apply_inspector_revision_choice(db, process, choice) -> dict` | эффект выбора (например, постановка пересчёта затронутых параметров) | выбор записывается с обоснованием и историей, эффект `{"applied": false, "status": "RECORDED"}` — в UI честно написано, что пересчёт выполняет бэкенд реестра |
| `completeness_report(db, process) -> dict` | `status, basis, registry_present, stages[], rows[{stage, section, expected, expected_basis, uploaded, files, status, uncertainty}]` | ожидаемый манифест = разделы матрицы 1.1 по источникам ПД/РД/ИД, найдено = разделы по именам/шифрам файлов; разделы ПД без собственного комплекта РД (ПЗ, ПОС, ООС…) для РД/ИД — `NOT_APPLICABLE` с основанием; смешанная стадия РД/ИД — `UNCERTAIN` |

Выбор инспектора хранится у S5 (`inspector_edits`) в обоих режимах — S3 получает его через `apply_inspector_revision_choice`.
LOW_QUALITY: рабочее место показывает флаг **только если его поставил конвейер**: `quality_status | quality_flag | page_quality == "LOW_QUALITY"` или `"LOW_QUALITY" in quality_flags` на фрагменте, либо `comparability_status` / `delta.reason` / `delta.gate.reason == "LOW_QUALITY"`. Сейчас таких флагов нет (S3 их добавит) — на демо очередь заполнена только низкой уверенностью. Просьба к S3 — использовать одно из этих имён.

## 4. Сквозной проход в браузере

Демо-стенд: `evaluation/phase12/s5_demo_seed.py` строит **отдельную** SQLite-базу (`data/s5_demo/`, общий `data/api.db` не трогается) настоящим конвейером (`import_official_dataset` / манифест + `run_process`, без gold):
- **Тюменская, 5** (разметка организатора, набор A; прогон 176 с): 6 кандидатов M-078/M-079 на реальных листах ОВ ПД/РД — для решений, панелей, правок, групповых операций, финализации;
- **Алтуфьевское, 79Б** (SILVER-объект, прогон 56 с): 11 конфликтов редакций (разные серии шифров АР, ИОС4, ИОС5, КР в ПД) — для выбора редакции и комплектности. Кандидатов на нём нет: механизм экспликаций — зона S1.

Подъём: `.claude/launch.json` → `case10-s5-backend` (API + Node-шлюз, как в compose) / `case10-s5-backend-fresh` (то же с `--reset` из снимка до любых действий) и `case10-react-dev`.

**Проход 1 — интерактивно в панели браузера (preview_start), 1440×900.** Решение по клавишам (C → Enter, R + комментарий → Ctrl+Enter) и мышью, групповое уточнение 2 однотипных, отказ от группового отклонения на шаге подтверждения, проверка запрета смешанного выбора, уточнение рамки РД рисованием, добавление фрагмента ПД, исключение и восстановление, очередь низкой уверенности, выбор редакции на ALT79B, комплектность, финализация. Все шаги легли в журнал аудита. Найдено и исправлено по ходу (см. §6).

**Проход 2 — скриптом, со скриншотом каждого шага** (`frontend_react/scripts/s5_walkthrough.mjs`: локальный Chromium по DevTools-протоколу, встроенный WebSocket Node 22, без npm-зависимостей; тот же UI, та же база после `--reset`, 1600×1000). Скрины — `evaluation/phase12/s5_screens/` (описания — `shots.json`, выдержка журнала — `audit_excerpt.json`):

| Шаг | Скрин |
|---|---|
| Вход | [01_login](s5_screens/01_login.png) |
| Открыт протокол: очередь, карточка §14, панель решения, запущен таймер | [02_workbench](s5_screens/02_workbench.png) |
| Синхронные панели ПД/РД, bbox, масштаб, «Источник ↗», ИД скрыта | [03_panels_pd_rd](s5_screens/03_panels_pd_rd.png) |
| C — выбрано «Подтвердить» (действий 2) | [04_decision_confirm_chosen](s5_screens/04_decision_confirm_chosen.png) |
| Enter — сохранено, автопереход, счётчики обновлены | [05_after_save_autoadvance](s5_screens/05_after_save_autoadvance.png) |
| R — reason_code + обязательный комментарий | [06_reject_requires_comment](s5_screens/06_reject_requires_comment.png) |
| Групповое отклонение: «Отклонить N» заблокировано до подтверждения | [07_bulk_reject_dialog](s5_screens/07_bulk_reject_dialog.png) |
| Смешанный выбор M-078 + M-079 — групповые операции запрещены | [08_bulk_mixed_type_guard](s5_screens/08_bulk_mixed_type_guard.png) |
| Уточнение фрагмента РД: новая область рисуется на странице | [09_refine_draw_bbox](s5_screens/09_refine_draw_bbox.png) |
| История версий доказательств (v1–v3, причина, было → стало) | [10_evidence_versions_history](s5_screens/10_evidence_versions_history.png) |
| Панели после правок | [11_panels_after_edits](s5_screens/11_panels_after_edits.png) |
| Очередь низкой уверенности / LOW_QUALITY, порог, фильтры | [12_lowconf_queue](s5_screens/12_lowconf_queue.png) |
| Финализация, время верификации в журнале | [13_finalized_verification_time](s5_screens/13_finalized_verification_time.png) |
| Конфликты редакций → CLARIFICATION_REQUIRED | [14_revisions_conflicts](s5_screens/14_revisions_conflicts.png) |
| Выбор редакции с обоснованием | [15_revision_choice_dialog](s5_screens/15_revision_choice_dialog.png) |
| Выбрана инспектором, история | [16_revision_chosen_history](s5_screens/16_revision_chosen_history.png) |
| Комплектность: загружено / ожидалось / основание | [17_completeness](s5_screens/17_completeness.png) |

Повторить: `python evaluation/phase12/s5_demo_serve.py --reset` + `npm --prefix frontend_react run dev` + `node frontend_react/scripts/s5_walkthrough.mjs --out evaluation/phase12/s5_screens` (≈ 45 с).

## 5. Цифры для питча (честно)

| Что | Значение | Оговорка |
|---|---|---|
| Действий на решение (открыть → C/R/U → сохранить) | **3** — все 8 замеров `DECISION_UI_METRICS` (2 прохода × 4 решения поштучно; клавиатура и мышь) | Считается и «открытие», в том числе автопереход; ввод текста комментария действием не считается |
| Время верификации, скриптовый проход | 35 с на 6 решений + 4 правки доказательств | Скрипт — нижняя граница накладных расходов интерфейса, не человек |
| Время верификации, интерактивный проход | 10 мин 29 с на 6 решений (2 из них групповые) + 4 правки | Включает задержки инструментов агента; **человеческую цифру должен снять ДЕМО на репетиции** — механизм замера готов (`VERIFICATION_TIME_MEASURED` в `GET /case10/processes/{id}/audit`) |
| API рабочего места, p50 / p95 (Тюмень, 137 групп, 185 фрагментов) | очередь 92 / 254 мс; карточка 27 / 30 мс; редакции 22 / 25; комплектность 32 / 35; таймер 35 / 43; страница PNG из кэша 11 / 12 | Замер на общей машине при трёх тяжёлых процессах других потоков; очередь в процессе без HTTP — 69 мс. До оптимизации (N+1 запросов) очередь была 387 / 489 мс |

## 6. Найдено и исправлено по ходу проверки в браузере

1. Второй вызов `verification/open` из двойного эффекта React (StrictMode) писал второе `VERIFICATION_OPENED` → вызовы на один процесс в клиенте дедуплицируются (на сервере в таймере всегда берётся первое открытие цикла).
2. Длительность могла уйти в минус: открытие хранилось с микросекундами, финализация — секундами строки аудита → время финализации берётся из `Protocol.finalized_at`, длительность ограничена снизу нулём.
3. В режиме рисования заголовок панели терял файл/шифр/редакцию; после рисования «авто»-масштаб пересчитывался по новой рамке, страница перерендеривалась и прыгала → метаданные передаются, масштаб и центрирование в режиме рисования заморожены.
4. В новом браузере вкладка «Проверка» открывалась пустой, хотя проверка есть → открывается последняя проверка проекта.
5. N+1 запросов в очереди (387 → 92 мс p50).
6. Мои правки через Python `write_text` записали файлы с CRLF (на Windows) — все свои файлы вернул в LF до коммита. **Задело чужой файл**: `evaluation/phase12/s6_code_literal_scan.json` (S6, не в git) — концы строк сразу восстановлены (содержимое побайтно прежнее: 183 строки CRLF); файл S6 больше не трогал.

## 7. Решения и допущения

- **Комментарий при отклонении** обязателен в UI (одиночное и групповое решение) и в API групповых операций. Одиночный `POST /decisions` оставлен с прежним правилом «reason_code или comment», потому что тест ТЗ-10 (`test_rejection_log_falls_back_to_reason_code_when_comment_is_blank`, чужой файл) закрепляет отклонение без комментария. Ужесточить API — решение I.
- **Однотипные** = один код матрицы (M-xxx) в пределах одного процесса; пакет применяется целиком или никак (все переходы проверяются до первой записи). Для группы любого решения нужен общий комментарий; для отклонения — ещё и `confirm_bulk_reject=true`.
- **Порог низкой уверенности** по умолчанию 0,75 (`CASE10_LOW_CONFIDENCE_THRESHOLD`, в UI меняется на лету). Данных для обоснования мало: на Тюмени уверенности 0,72 и 0,80, порог отделяет кандидатов, у которых РД-значение — отсутствие системы в зоне помещения (0,72). Пересмотреть на итоговом коде (I).
- **Порядок очереди**: кандидаты → приоритет HIGH/MEDIUM → код → номер помещения (естественная сортировка).
- **Технические статусы** (MISSING_EVIDENCE / NOT_COMPARABLE / NOT_APPLICABLE / SUSPICION) решению не подлежат — как в машине состояний `v3_pipeline`; рабочее место объясняет это вместо кнопок.
- «Источник ↗» отдаёт исходную страницу, вырезанную из оригинального PDF (SHA-256 проверяется), а не весь многосотмегабайтный файл.
- Для стенда, где объекты распакованы в разные места, добавлен `CASE10_ORIGINALS_ROOTS` (несколько корней, SHA-256 обязателен) — только в модуле S5; `dataset_sources.py` не менялся.

## 8. Что нужно от других потоков / для I

1. **Протокол и экспорт не видят правок доказательств.** Карточки, панели и журнал их показывают, но `build_protocol_payload` / `evaluation/exporter.py` берут только машинные фрагменты. Предложение для I (3 строки в `v3_pipeline.evidence_group_to_dict`, файл вне моего владения): `payload["inspector_evidence"] = [i for i in inspector_workbench.effective_fragments(db, group, with_view=False) if i["edited"] or i["origin"] == "INSPECTOR"]`, а экспорт брать из эффективных фрагментов при наличии версий. Решение по экспорту — S3/I.
2. **S3**: подключить функции из §3; поле LOW_QUALITY — одно из имён §3. На демо видно, что вывод редакции по имени файла даёт мусор: `ред. ы` (ООС8.x), `ред. 1.pdf` («Изм. 1.pdf»), `ред. ПО` («ИЗМ ПО ЗАМЕЧАНИЯМ»), `ред. pdf` («Том 3.РЕД.pdf») — `document_versions.infer_revision`.
3. Групповая операция из N решений создаёт N версий протокола (вызывается штатный `record_inspector_decision`, каждая версия корректна). Собрать в одну версию — правка `v3_pipeline` (решение I).
4. Роли: правки доказательств и выбор редакции доступны любому пользователю организации (как решения); отмена финализации — только Admin/Supervisor (как было).

## 9. Файлы

Новые: `api_service/app/domain/inspector_workbench.py`, `api_service/alembic/versions/5a1c0e7d9b21_add_inspector_edits.py`, `api_service/tests/test_case10_s5_workbench.py`, `frontend_react/src/components/{Workbench,CandidateQueue,StagePanels,EvidenceCardFields,DecisionBar,EvidenceEditor,RevisionsPanel,CompletenessPanel,VerificationTimer}.jsx`, `frontend_react/scripts/s5_walkthrough.mjs`, `evaluation/phase12/{s5_demo_seed.py,s5_demo_serve.py,S5_REPORT.md,s5_screens/}`.
Изменены (строка S5): `api_service/app/api/routes_case10.py` (16 эндпоинтов + метрики решения + замер при финализации), `api_service/app/db/models.py` (`InspectorEdit`), `frontend_react/src/{App.jsx,api.js,labels.js,utils.js,styles.css,components/ProcessPanel.jsx}`, `node_gateway/{server.js,openapi.json}` (маршруты и контракт), `.claude/launch.json` (конфигурации демо-стенда).
Удалены (заменены окном кандидата): `EvidenceCard.jsx`, `DecisionDialog.jsx`, `PageViewerModal.jsx`.
Чужие файлы не менялись (кроме инцидента с концами строк в §6.6, исправленного).

Тесты: `PYTHONPATH="D:\Proga\LCT-hack-2026;D:\Proga\LCT-hack-2026\api_service" venv/Scripts/python.exe -m unittest discover -s api_service/tests -t api_service/tests` → **770/770 OK** (173 с); после финальных правок повторно S5 + машина состояний верификации + API — 31 + 13 OK. Фронтенд: `eslint src` чисто, `vite build` проходит.
