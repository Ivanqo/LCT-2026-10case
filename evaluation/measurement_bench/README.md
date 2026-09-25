# Стенд измерений (Фаза 10, промпт A)

Единый размеченный корпус значений + три метрики + матрица критического recall.  Полный отчёт: `../MEASUREMENT_BENCH_REPORT.md`.

## Порядок (всё воспроизводится из кода и seed 20260924; тяжёлые данные — вне репозитория, `E:\case10_measurement`)

```bash
# 0. разметка (независимое чтение PDF, до запуска экстрактора)
python -m evaluation.measurement_bench.make_probe_plan        # детерминированный план попыток (seed)
python -m evaluation.measurement_bench.probe_object OBJ       # чтение по плану (reader.py, без конвейера)
python -m evaluation.measurement_bench.build_corpus --freeze  # value_corpus_v1.jsonl + corpus_manifest.json (sha256, frozen_at)
python -m evaluation.measurement_bench.verify_labels          # каждая цитата (файл, страница) есть в тексте оригинала
# 1. прогон реального тэггера + 4 коллекторов (отказывается стартовать без замороженного корпуса)
CASE10_BENCH_APP_ROOT=<dir с app/> python -m evaluation.measurement_bench.run_extraction [OBJ ...]
# 2. метрики
python -m evaluation.measurement_bench.metric1                # точность по семействам + таксономия
python -m evaluation.measurement_bench.seed_l1                # посевы, уровень снимка страницы (+ протокольный уровень)
python -m evaluation.measurement_bench.seed_l2 --n 24         # реальная правка PDF (fitz), остановка при хрупкости
python -m evaluation.measurement_bench.metric3                # реальные расхождения annotations.jsonl (--reeval: только оценка сохранённых прогонов)
python -m evaluation.measurement_bench.critical_recall_matrix # evaluation/reports/critical_recall_matrix.{json,md}
python -m evaluation.measurement_bench.location_convention    # конвенции location по public_gold_checks
python -m evaluation.measurement_bench.render_tables          # evaluation/reports/measurement_bench/tables.md
```

`CASE10_BENCH_APP_ROOT` указывает на замороженную копию `api_service` (другие промпты правят дерево параллельно; отпечаток файлов пишется в каждый вывод).
Тесты: `PYTHONPATH=api_service pytest api_service/tests/test_case10_measurement_bench.py`.
Речников (скрытый тест) в корпус не входит; закрытый gold не читается.

Пост-заморозочные заметки аудита (корпус не редактируется): `corpus/errata_post_freeze.json` (документальные конфликты по PZ-022 и чувствительность метрики 1).
Точка входа для чтения результатов: `../MEASUREMENT_BENCH_REPORT.md` (короткий ответ — раздел 0), сводные таблицы — `../reports/measurement_bench/tables.md`, матрица — `../reports/critical_recall_matrix.md`.
