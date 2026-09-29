# Интеграционные тесты поиска по документации

Этот набор проверяет пока только корректность поиска RAG по загруженной проектной документации:

- поднимает приложение `rag_service.app.main` через `FastAPI TestClient`;
- индексирует три PDF в изолированную SQLite-базу и временные папки;
- подменяет тяжелый vectorstore/embedding детерминированными заглушками;
- вызывает реальный `/ask` и проверяет, что в `sources` вернулись нужные документы, точные листы и фрагменты текста;
- пишет расширенный статистический отчет в JSON и HTML.

## Входные PDF

По умолчанию тесты ищут файлы здесь:

```powershell
C:\Users\Ivan\Downloads\Telegram Desktop
```

Ожидаемые имена:

```text
318-ПБ-Р-К2-2023-КЖ1.24.pdf
3-18-ПБ-Р-К2-2023-КМ1.pdf
318-ПБ-Р-К2-2023-АР3.pdf
```

Если файлы лежат в другом месте, укажите папку:

```powershell
$env:PD_RDMVP_DOC_SEARCH_FIXTURE_DIR="D:\path\to\pdfs"
```

Или перечислите конкретные файлы через `|`:

```powershell
$env:PD_RDMVP_DOC_SEARCH_FIXTURES="D:\docs\a.pdf|D:\docs\b.pdf|D:\docs\c.pdf"
```

## Запуск

### Вариант A. Только интеграционные тесты поиска

Для текущих тестов отдельно запускать `rag`/`api`/`frontend` не нужно. Тест использует `FastAPI TestClient`: приложение `rag_service.app.main` поднимается внутри процесса теста, а данные пишутся в изолированную временную SQLite-базу.

Из корня репозитория, в окружении с зависимостями `rag_service/requirements.txt`:

```powershell
python -m unittest rag_service.tests.integration.test_documentation_search -v
```

### Вариант A2. Контракт ответа после LLM (снят с запуска)

Qwen Proxy и внешний LLM-вызов удалены из проекта. RAG теперь возвращает найденные фрагменты без генерации свободного ответа; перечисленные ниже критерии относятся только к архивному harness и больше не подтверждают текущую реализацию.

Интеграционный тест класса `LlmAnswerContractIntegrationTest` намеренно помечен `SkipTest`; он сохранён как исторический harness, но не запускает внешний сервис.

Ранее созданные отчеты (если они есть) находятся здесь; пропущенный harness новых отчетов не создает:

```text
rag_service/test_reports/llm_answer_contract/index.html
rag_service/test_reports/llm_answer_contract/llm_answer_contract_report.json
```

Если запускаете через локальный `venv`, сначала проверьте, что в нем есть зависимости RAG:

```powershell
.\venv\Scripts\activate
python -m pip install -r rag_service\requirements.txt
python -c "import fitz; print('PyMuPDF OK')"
python -m unittest rag_service.tests.integration.test_documentation_search -v
```

Ошибка `PyMuPDF is required for PDF processing` означает, что в активном Python-окружении не импортируется модуль `fitz` из пакета `pymupdf`. Это лечится переустановкой зависимостей выше или точечно:

```powershell
python -m pip install --force-reinstall pymupdf==1.24.2
```

В Docker-контейнере RAG удобнее использовать уже установленные зависимости образа, но примонтировать текущий репозиторий как `/repo`, потому что штатный образ копирует только runtime-код сервиса:

```bash
docker compose run --rm \
  -v "$PWD:/repo" \
  -v "/c/Users/Ivan/Downloads/Telegram Desktop:/fixtures:ro" \
  -w /repo \
  -e PYTHONPATH=/repo \
  -e PD_RDMVP_DOC_SEARCH_FIXTURE_DIR=/fixtures \
  rag \
  python -m unittest rag_service.tests.integration.test_documentation_search -v
```

### Вариант B. Запуск всего проекта

Полный проект нужен для ручной проверки UI/API или будущих end-to-end тестов против живых сервисов. Для текущего теста поиска это не требуется.

```powershell
docker compose up --build
```

После старта сервисы доступны локально:

```text
frontend: http://localhost:3000
api:      http://localhost:8000
rag:      http://localhost:8001
ifc:      http://localhost:8002
```

Быстрая проверка RAG:

```powershell
Invoke-RestMethod http://localhost:8001/health
```

Остановка:

```powershell
docker compose down
```

## Отчет

После прогона создаются:

```text
rag_service/test_reports/document_search/index.html
rag_service/test_reports/document_search/document_search_report.json
```

HTML-файл показывает статистику: сколько кейсов прошло, сколько точных страниц найдено, сколько источников вернул поиск, какие документы были загружены, какие страницы ожидались, какие страницы реально вернулись, какие фрагменты попали в выдачу и какие ошибки возникли.

Папку отчета можно переопределить:

```powershell
$env:RAG_SEARCH_REPORT_DIR="D:\tmp\rag-search-report"
```

## Что именно проверяется

Сейчас покрыты три поисковых сценария:

- `КЖ1.24`: поиск армирования `Лм-9.6`, ожидаемый лист `8`;
- `КМ1`: поиск закладной детали `М-1` для сборного марша, ожидаемый лист `5`;
- `АР3`: поиск указаний по кладке инженерных шахт, ожидаемый лист `7`.

`test_documentation_search` проверяет ingestion текстовых чанков и поиск релевантных источников с точными листами; свободный ответ LLM не генерируется.

`test_llm_answer_contract` больше не является активным acceptance-тестом: свободная генерация ответов через внешний proxy отключена.
