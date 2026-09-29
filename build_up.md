
Вот как запустить **весь проект через Docker** (docker-compose) и проверить, что всё поднялось.

## 1) Перейти в папку проекта

```bash
cd /path/to/your/project
```

## 2) (Рекомендуется) Проверить, что Docker и Compose есть

```bash
docker --version
docker compose version
```

## 3) Запуск

Сборка + запуск всех сервисов:

```bash
docker compose up --build
```

Если хочешь запуск в фоне:

```bash
docker compose up -d --build
```

## 4) Проверка, что контейнеры поднялись

```bash
docker compose ps
```

Логи всех сервисов:

```bash
docker compose logs -f
```

Логи конкретного сервиса (например api / rag / ifc / worker / postgres / redis):

```bash
docker compose logs -f api
```

## 5) Проверка доступности (по портам)

Обычно у тебя так (как в README):

* API: `http://localhost:8000`
* RAG: `http://localhost:8001`
* IFC: `http://localhost:8002`

Быстрая проверка, что API отвечает (пример):

```bash
curl http://localhost:8000/api/projects
```

## 6) Остановка

Остановить контейнеры:

```bash
docker compose down
```

Остановить **и удалить тома** (⚠️ удалит данные БД/хранилищ, если они в volumes):

```bash
docker compose down -v
```

## 7) Если менял код и нужно пересобрать

```bash
docker compose build --no-cache
docker compose up -d
```

---

### Генерация ответов

Qwen Proxy не входит в проектный запуск. RAG возвращает найденные текстовые фрагменты и ссылки на документы без внешней генерации; CASE10-проверки работают отдельно и не требуют LLM.

---

Если хочешь — скинь сюда твой `docker-compose.yml` (или скажи как он называется), и я дам **точные команды под твою конфигурацию** + подскажу, где лучше прописать `.env` и какие переменные точно нужны.
