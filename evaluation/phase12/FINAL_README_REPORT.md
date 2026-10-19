# FINAL_README_REPORT — главный README и чистый pull по нему

Дата: 2026-10-19. Ветка `claude/readme-final-9171ec` (от `main` `dbd0723`), ничего не запушено. Код приложения, Dockerfile, compose, `deploy/make_delivery.py`,
`node_gateway/*` не менялись.

## 1. Что сделано

| Файл | Изменение |
|---|---|
| `README.md` | переписан как главная страница: что это и поток данных, технический комплект (чек-лист по ожиданиям экспертов), выход, 12 модулей ТЗ, замечания экспертов → что сделано → где проверить, измеренное качество и ограничения, воспроизводимость, структура, тесты, поддержка |
| `docs/legacy_services.md` | новый: прежний текст корневого README (RAG, IFC, лендинг); в README одна ссылка |
| `README_GRADER.md` | §1: ссылка «см. §3» на реестр исправлена на §2; в описание корня JSON добавлен `source_selection`; в таблице API у `protocols/current` указан обязательный параметр `project_id`, добавлены `docx` и `xml` |
| `DEPLOYMENT.md` | ссылка на CLI «раздел 2» исправлена на раздел 1 |
| `TZ_TRACEABILITY.md` | обновлены устаревшие строки (флаги по умолчанию из шагов TZ-ON и Z2): модули 8 и 10, статусы `*_PARTIAL`, DOCX, лог отклонений, забор файлов, усиление среды, замеры раздела 11, пункты раздела 8; число тестов — со ссылкой на отчёт |
| `evaluation/check_claims.py` | `README.md` добавлен в список проверяемых по умолчанию (правила не ослаблялись) |

Ссылки: каждая относительная ссылка в `README.md`, `README_GRADER.md`, `DEPLOYMENT.md` ведёт на файл, который есть в git (проверено скриптом по `git ls-files`
в чистом clone). Неотслеживаемые `CASE10_EXPERT_FEEDBACK_FINAL.md` и другие файлы корня в этих трёх документах как доступные не названы.
`evaluation/check_claims.py`: 0 нарушений в четырёх файлах (README.md, README_GRADER.md, DEPLOYMENT.md, TZ_TRACEABILITY.md), в том числе в clone.

## 2. Чистый pull

Клон: `git clone D:\Proga\LCT-hack-2026 E:\case10_clean\clone` (не из рабочего дерева). База `main` `dbd0723`; затем `git checkout claude/readme-final-9171ec`
(коммит с README `8e5117f363552f0902f46f56586e445ff5c59abd`; дальнейшие правки по итогам pull — в следующем коммите ветки). Все тяжёлые шаги — под слотом
`E:\case10_locks\heavy` (получен 21:36 после ожидания, снят по окончании). Docker Desktop был выключен: включён (готов за 24 с), по окончании остановлен.

**Отступления от README, вызванные средой:** порты 8000, 8080 и 3100 заняты локальным демо-стендом другого агента (node, uvicorn, `ui_server.py`), поэтому стенд поднят
как `CASE10_IMAGE_TAG=readme API_HOST_PORT=18000 GATEWAY_HOST_PORT=18080 UI_HOST_PORT=13100 docker compose -p case10clean -f docker-compose.case10.yml up -d --build`
(тег и имя проекта — чтобы не затереть чужие образ `latest` и тома `case10_*`). Образ API для CLI собран командой README, но с тегом `case10-api:readme`. Больше ничего не менялось.

| Шаг | Результат | Время |
|---|---|---|
| `docker compose … up -d --build` из clone | exit 0, 5 сервисов; слои ОС и torch из кэша, слой `pip install -r requirements.txt` собирался со скачиванием (не из кэша) | 1 149 с |
| `/health` API | `ok`, `tesseract 5.5.0`, GPU нет; шлюз `/health` и `/openapi.json` 200, интерфейс 200, `/docs` 200 | — |
| вход `admin` / `Case10-Demo-2026` через API и шлюз | токен выдан; в интерфейсе вход выполнен, проект с 46 документами виден | — |
| `POST /api/projects`, `POST /api/case10/batch-runs` (NOV, реестр `registry_NOV_approved.csv` положен как `packages/nov.registry.csv`) | процесс `READY`, `result` = 140 проверок, `free_search_hypotheses` пуст | 89 с |
| протокол: `GET /api/case10/protocols/{id}/pdf`, `docx`, `xml`, `json`, `submission` | PDF 34 страницы (198 КБ), DOCX (zip, 22 КБ), XML парсится (263 КБ), `submission` — валидный JSON (278 КБ) | — |
| CLI: `docker run --rm --network none … python -m app.cli run` (команда README, пакет NOV 46 PDF, 303,7 МБ) | exit 0, реестр `ACCEPTED`, 140 проверок (`COMPARISON_IMPOSSIBLE` 9, `MISSING_DOCUMENT` 129, `NO_VIOLATION` 2) | 89 с |
| `python -m app.cli validate /out/result.json` | `valid (140 checks)`, код 0 | — |
| `python -m app.cli selftest` (`--network none`) | 25 сценариев, 25 PASS, 0 FAIL | 81,9 с |
| сверка | `checks` CLI = `checks` стенда (`package` различается, как и ожидается); `checks` CLI = `checks` результата E2 для NOV (SHA-256 E2 `ff2a67b9…`, этого прогона `f870a3ed…`; файлы различаются только ключом `free_search_hypotheses`, причину различия не разбирал) | — |
| тесты (две пробы из README: `test_case10_final_z_claims.py`, `test_case10_comparison_gate.py`) | 35 passed, 5 skipped | 17,6 с |

Уборка: стенд снят с `down -v` (тома `case10clean_*` удалены), теги `case10-*:readme` удалены, рабочие файлы остались в `E:\case10_clean\` (`clone`, `work`, `out`, `up.log`, `cli_run.log`).

## 3. Найденные расхождения README и действительности

| № | Расхождение | Исправление |
|---|---|---|
| 1 | `GET /api/case10/protocols/current` без `project_id` возвращает 422 (в таблицах API параметр не указан) | в README и README_GRADER указан `?project_id=…`, добавлены `docx`/`xml` |
| 2 | Git Bash на Windows: путь `/in/docs` искажается (`C:/Program Files/Git/in/docs`), CLI завершается ошибкой «`--docs` must be a folder» | в README добавлено `export MSYS_NO_PATHCONV=1` (проверено); PowerShell и WSL не проверялись |
| 3 | При смене `GATEWAY_HOST_PORT` интерфейс по-прежнему ходит на `:8080` («Нет соединения с сервером») | в README описано `REACT_FRONTEND_API_BASE`; после пересборки интерфейса с ним вход работает |
| 4 | В Git Bash пример `curl … -d '{"name":"Объект 1"}'` из README_GRADER даёт «error parsing the body» (кириллица в аргументе командной строки); с `--data-binary @file` или в Linux работает | не исправлялось (особенность оболочки Windows); пример README_GRADER оставлен |
| 5 | README_GRADER §1 ссылался на «§3» вместо §2; DEPLOYMENT ссылался на «раздел 2» вместо раздела 1 | исправлено |
| 6 | `TZ_TRACEABILITY.md` расходился с таблицей флагов README_GRADER (планировщик отчёта, `*_PARTIAL`, DOCX, нормативная база, лог отклонений) | строки обновлены по отчётам TZ-ON и Z2 |
| 7 | `PYTHONPATH` в Git Bash на Windows: разделитель `:` не работает, нужен `;` | в README приведены варианты для bash (Linux/macOS) и PowerShell; вариант Linux (`:`) на этой машине не запускался |

Для основного агента (вне моих файлов): `deploy/demo/README_DEMO.md` ссылается на неотслеживаемый `CASE10_EXPERT_FEEDBACK_FINAL.md` (строка 5: «чек-листа экспертов (`CASE10_EXPERT_FEEDBACK_FINAL.md` §9)»);
в публичном репозитории этого файла нет, ссылку стоит заменить на `evaluation/phase12/FINAL_CARD_AUDIT.md`. Другие закоммиченные документы с ссылками на файлы вне git не искались.

## 4. Что не проверено

- Холодная сборка образа без кэша слоёв и время сети; `docker build` образа комплекта из `make_delivery.py bundle`.
- Загрузка пакета через файловый диалог интерфейса, финализация протокола, усиленный режим, GPU-оверлей, `deploy/case10/bootstrap.sh` (публичный стенд с HTTPS).
- Полный набор тестов (запущены только два файла) и команда `PYTHONPATH="api_service:."` на Linux.
- Клон с GitHub: на момент работы там только `da07dea`; README указывает адрес `https://github.com/Ivanqo/LCT-2026-10case.git`, а проверка клона с GitHub после push остаётся за основным агентом.
- Числа качества в README взяты из перечисленных отчётов и в этой работе не пересчитывались.
