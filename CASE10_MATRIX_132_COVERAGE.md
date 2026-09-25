# CASE10 — Coverage-матрица 132 параметров

_Автоматически сгенерировано `evaluation/matrix_coverage.py` 2026-09-23 19:02 UTC. Пересчитывается детерминированно из `parameter_catalog_132.jsonl`, `parameter_coverage_132.jsonl`, `official_rule_packs.SUPPORTED_RULE_CODES` и `api_service/tests/*.py` — без ручной правки JSON._

## Методология

Параметр считается реализованным не по наличию строки в БД `Param`, а только если существует исполняемый путь `extraction → evidence → comparison/status`. Классы готовности:

- **PRODUCTION_READY** — тюнингованный rule-pack (`official_rule_packs.py`) + реальное сравнение (включая negative path) + тесты + хотя бы один валидированный gold-пример в реальной оценке (`evaluation/smoke_real_data.py`). Это ровно `SUPPORTED_RULE_CODES` (5 кодов), посчитано программно, не захардкожено в отчёте.
- **PARTIAL** — исполняемый extractor + сравнение существуют (в этой сессии — общий `generic_anchor_numeric` механизм), но нет gold-разметки для валидации точности, и/или логика сравнения обобщённая (equality с допуском), а не доменный порог из `trigger`.
- **CONFIG_ONLY** — строка `Param` с метаданными (секция/unit/trigger/источники) существует, но исполняемого extraction-пути нет вообще; система всегда возвращает `MISSING_EVIDENCE`/`NOT_COMPARABLE`/`CLARIFICATION_REQUIRED`.
- **ABSTAIN_BY_DESIGN** / **NOT_APPLICABLE** — намеренно **не используются** на уровне определения параметра матрицы: оба класса корректны только на уровне конкретной находки конкретного объекта в рантайме (например, параметр про лифтовую шахту — `NOT_APPLICABLE` для здания без лифта; параметр с данными лабораторных испытаний, отсутствующими во всех загруженных документах — `MISSING_EVIDENCE`, а не выдуманное значение). Использовать эти статусы на уровне определения параметра значило бы замаскировать отсутствие реализации — именно это явно запрещено в задании этой сессии.

## Итоговые цифры

- Всего параметров: **132**
  - `PRODUCTION_READY`: **5** (3.8%)
  - `PARTIAL`: **80** (60.6%)
  - `CONFIG_ONLY`: **47** (35.6%)
- Параметров с тестами: **20** (15.2%)
- Параметров с реальными gold-примерами (организаторская закрытая методика): **9** (6.8%) — коды: IOS4-078, IOS4-079, KR-055, KR-058, PZ-009, SPZU-027, SPZU-029, SPZU-036, SPZU-039
- PRODUCTION_READY коды: IOS4-078, IOS4-079, KR-055, KR-058, PZ-009
- PARTIAL коды (80): AR-040, AR-041, AR-042, AR-045, AR-046, AR-047, AR-048, AR-049, AR-050, AR-051, IOS1-069, IOS1-070, IOS2-071, IOS2-073, IOS3-074, IOS4-076, IOS4-077, IOS5-080, KR-054, KR-056, KR-057, KR-059, KR-060, KR-061, KR-062, KR-064, KR-065, KR-067, ODI-116, ODI-117, ODI-118, ODI-119, ODI-120, ODI-121, POD-090, POD-093, POD-097, POS-081, POS-083, POS-084, POS-088, PPM-104, PPM-105, PPM-107, PPM-108, PPM-110, PPM-112, PPM-113, PZ-001, PZ-002, PZ-003, PZ-004, PZ-005, PZ-006, PZ-008, PZ-010, PZ-011, PZ-012, PZ-015, PZ-019, PZ-020, PZ-021, PZ-022, PZ-023, SPZU-024, SPZU-025, SPZU-026, SPZU-027, SPZU-028, SPZU-029, SPZU-030, SPZU-031, SPZU-033, SPZU-036, SPZU-037, SPZU-038, ZU-124, ZU-125, ZU-128, ZU-129

**Важное разграничение метрик (по требованию задания — не смешивать):**

| Метрика | Значение | Что реально означает |
|---|---:|---|
| Parameter coverage (PRODUCTION_READY) | 5/132 | Полный, gold-валидированный pipeline |
| Parameter coverage (PRODUCTION_READY + PARTIAL) | 85/132 | Есть исполняемый extraction+comparison, БЕЗ gold-валидации точности для PARTIAL |
| Real-data exercise coverage | 132/132 | Все 132 параметра реально прогоняются через `run_process` на официальном датасете (не значит, что все дают сравнение — большинство честно абстинируют) |
| Real gold-example coverage | 9/132 | Есть хотя бы 1 атомарная проверка в закрытой методике организатора — НЕ то же самое, что precision/recall |
| OCR accuracy | не считается | Нет human-labelled OCR-деноминатора в текущем датасете — честно не генерируем PASS (см. `evaluation/metrics.py::_evaluate_ocr`, который умеет это считать, когда деноминатор появится) |

## По разделам ТЗ

| Раздел | Всего | PRODUCTION_READY | PARTIAL | CONFIG_ONLY |
|---|---:|---:|---:|---:|
| Раздел 1. ПЗ | 23 | 1 | 16 | 6 |
| Раздел 2. СПЗУ | 16 | 0 | 12 | 4 |
| Раздел 3. АР | 14 | 0 | 10 | 4 |
| Раздел 4. КР | 14 | 2 | 10 | 2 |
| Раздел 9. ППМ | 13 | 0 | 7 | 6 |
| Раздел 6. ПОС | 9 | 0 | 4 | 5 |
| Раздел 10. ОДИ | 9 | 0 | 6 | 3 |
| Раздел 7. ПОД | 8 | 0 | 3 | 5 |
| Раздел 11. ЗУ | 8 | 0 | 4 | 4 |
| Раздел 5. ИОС4 | 4 | 2 | 2 | 0 |
| Раздел 8. ООС | 4 | 0 | 0 | 4 |
| Раздел 5. ИОС1 | 3 | 0 | 2 | 1 |
| Раздел 5. ИОС2 | 3 | 0 | 2 | 1 |
| Раздел 5. ИОС3 | 2 | 0 | 1 | 1 |
| Раздел 5. ИОС5 | 1 | 0 | 1 | 0 |
| Раздел 12. СМ | 1 | 0 | 0 | 1 |

## По комбинации требуемых стадий (source_pd/rd/id)

| Комбинация | Параметров |
|---|---:|
| PD,RD,ID | 132 |

## По классу типа данных (`unit`)

| Класс | Параметров | Пояснение |
|---|---:|---|
| NUMERIC_SIMPLE | 51 | одна физическая величина (м/мм/м²/м³/%/мм²) — целевой класс общего anchor-механизма |
| NUMERIC_COMPOUND | 26 | составная единица (несколько величин через "/") — вне охвата этой сессии |
| NONE | 18 | без единицы измерения (текст/чертёж/наличие) |
| ENUM_CLASS | 17 | категориальный код (марка/класс/степень) — нужен словарь домена, не общий regex |
| NUMERIC_COUNT | 13 | количество (шт./ед.) — риск спутать со счётчиком строк таблицы |
| OTHER | 7 | прочее |

## Real-data verification (венv, официальный публичный датасет, без Docker в этой сессии)

Запущено напрямую через `app.domain.v3_pipeline.run_process` на throwaway SQLite (не боевая `data/api.db`), официальный `matrix_version=official-132-v1.1`, оба публичных объекта:

**OBJ-TYUMENSKAYA-5-GOLD-SEED** — всего evidence groups: 137

| finding_status | count |
|---|---:|
| CLARIFICATION_REQUIRED | 130 |
| CANDIDATE | 6 |
| NEGATIVE_VERIFIED | 1 |

_Dominated by CLARIFICATION_REQUIRED: this object's working-stage documents are tagged RD_ID_MIXED (no clean RD split), which every non-rule-pack path -- old and new -- correctly refuses to guess through rather than silently pick one._

**OBJ-NOVOSLOBODSKAYA** — всего evidence groups: 134

| finding_status | count |
|---|---:|
| NOT_COMPARABLE | 108 |
| MISSING_EVIDENCE | 21 |
| NEGATIVE_VERIFIED | 5 |

### Общий anchor-механизм: измеренный реальный выход (не оценка)

- Eligible-параметров на объект: **48**
- Новых COMPARABLE evidence groups создано: **0**
- Реальные значения, найденные механизмом на PD-стадии (сверено вручную с текстом документа, НЕ с gold):


  **OBJ-TYUMENSKAYA-5-GOLD-SEED**

  - `PZ-001 (Площадь застройки)`: 4650.91 m2 -- PD only, matches manual forensic reading of the real ТЭП table
  - `PZ-002 (Общая площадь здания)`: 11618.27 m2 -- PD only, matches manual forensic reading
  - `PZ-008 (Высота здания)`: 15.814 m -- PD only, matches manual forensic reading

  **OBJ-NOVOSLOBODSKAYA**

  - `PZ-001 (Площадь застройки)`: 1225.8 m2 -- PD only
  - `PZ-002 (Общая площадь здания)`: 17140.2 m2 -- PD only (required the qualifier-tolerant anchor fallback: this project's real label is "Площадь жилого здания", not the catalog's "Общая площадь здания")
  - `PZ-008 (Высота здания)`: 77.05 m -- PD only

**Почему 0 новых сравнений**: The mechanism correctly finds a real PD-side value for several parameters on both objects (verified against the real document text, independent of gold), but produces zero new comparable findings because it never got a matching RD-side value for the SAME parameter within its page budget -- OBJ-TYUMENSKAYA-5-GOLD-SEED structurally lacks a clean RD stage (RD_ID_MIXED, see above) and OBJ-NOVOSLOBODSKAYA's RD-stage annotation-candidate localization for these codes did not surface the matching table page within the scanned budget. It abstains rather than pairing a PD value with an unrelated RD number, exactly as designed.

### Abstention rate на реальном публичном датасете

| Объект | Abstained | Всего | Rate |
|---|---:|---:|---:|
| OBJ-TYUMENSKAYA-5-GOLD-SEED | 130 | 137 | 94.9% |
| OBJ-NOVOSLOBODSKAYA | 129 | 134 | 96.3% |

### Hidden blind smoke (OBJ-RECHNIKOV-7-7 — без hidden-меток)

Тот же сценарий, что и `evaluation/smoke_hidden_blind.py` (`include_hidden=True`, `allow_hidden_gold_labels=False` — hidden-метки никогда не импортируются и не участвуют в инференсе), прогнан локально через venv тем же способом, что и публичные объекты выше — чтобы подтвердить, что новый generic-механизм не поднимает candidate-rate на скрытом объекте.

- Всего evidence groups: **133**
- `CANDIDATE`+`SUSPICION`: **1** (0.75%) — гейт: ≤20 и ≤15%
- Групп от нового generic-механизма: **0**
- **Гейт пройден: PASS**

## Фаза A/B/C этой сессии: ENUM_CLASS / NUMERIC_COMPOUND / NUMERIC_COUNT extractors

Три новых generic-механизма (enum-class, compound, table-row-count) прогнаны тем же способом (2026-09-20 (local venv, throwaway sqlite, no Docker)) на тех же 2 публичных объектах. Итоговое число evidence groups и распределение по finding_status на обоих объектах **не изменилось** относительно real-data run выше (без изменений) — ни один из 32 новых PARTIAL-кодов не нашёл сопоставимую пару PD+RD в рамках бюджета на этих конкретных объектах; это тот же честный результат, что и у исходного numeric-механизма в checkpoint 35, а не регрессия.

Реальные значения, найденные напрямую через `collect_*_observations` (не через полный pipeline, который требует пары PD+RD) — подтверждает, что новые пути реально исполняются против настоящих fitz-снимков страниц, а не только против синтетических тестовых фикстур:

**OBJ-NOVOSLOBODSKAYA**

- `PZ-021 (Класс энергетической эффективности, ENUM)`: PD-side canonical value 'B' -- matches real page text, PD only

**OBJ-TYUMENSKAYA-5-GOLD-SEED**

- `PZ-023 (Класс конструктивной пожарной опасности, ENUM)`: PD-side canonical value 'C0' -- matches real page text, PD only
- `SPZU-037 (Количество и разметка парковочных мест, TABLE_ROW_COUNT)`: PD-side row_count=1 -- table-title match found, single content row, PD only (not independently verified against the source page, unlike the ENUM hits above)

_Same root cause as the pre-existing numeric mechanism (checkpoint 35): each new mechanism still requires an independent PD-side AND RD-side hit for the SAME parameter before producing a comparable finding, and the upstream page-locator signal (learning_annotation keyword tagging) is broad-recall/low-precision by design, so a matching RD-side page rarely surfaces within the shared page budget on these 2 objects._

## Фаза D этой сессии: честная валидация против organizer gold (SPZU-027/029/036/039)

Прогнан полный pipeline вслепую (`include_hidden=True`, `allow_hidden_gold_labels=False` — hidden-метки никогда не импортируются в инференс) на объекте `OBJ-RECHNIKOV-7-7` (2026-09-20 (local venv, throwaway sqlite, no Docker, blind hidden import)), затем gold загружен ОТДЕЛЬНО и ТОЛЬКО постфактум, исключительно для честной сверки — не влиял на экстракцию, результат не использовался для подгонки логики извлечения (по явному правилу проекта — не хардкодить под gold).

**Результат: ALL FOUR ABSTAINED (NOT_COMPARABLE) -- no PD+RD comparable pair found within budget for any of the four**

| Код | Gold | Результат pipeline |
|---|---|---|
| `SPZU-027` | | gold VIOLATION_PRESENT (area decreased PD->RD); pipeline abstained -- a missed detection (false negative) from a scoring perspective |
| `SPZU-029` | | gold NO_VIOLATION (RD expands the spec, not a reduction); pipeline abstained -- a safe true negative, not a wrong answer |
| `SPZU-036` | | gold NO_VIOLATION (PD/RD equal); pipeline abstained -- a safe true negative |
| `SPZU-039` | | gold NO_VIOLATION (drainage present both stages); pipeline abstained -- expected, this code has no extractor at all (unit '--') |

Метрики, посчитанные той же функцией `evaluate_case10` (не оценка вручную), по этим 4 проверкам отдельно (НЕ смешано с публичной n=6/n=12 метрикой из checkpoint 37, чтобы не путать dev-метрику на публичных данных с честной проверкой на скрытом объекте):

- `finding_precision`: None (нет ни одного положительного предсказания — 0/0)
- `finding_recall`: 0.0 (sample_size=1, 95% CI [0.0, 0.7935])
- `finding_f1`: 0.0
- `false_positive_rate`: 0.0 (sample_size=3)

**Важно**: все 4 из этих gold-проверок сами помечены организатором как `score_eligible: false` / `GOLD_READY_SECOND_REVIEW` (требуют второй экспертной проверки) — тот же статус, что и Novoslobodskaya PZ-009/KR-055/KR-058 проверки, уже использованные для публичной `finding_false_positive_rate` (n=5) метрики в checkpoint 37. Включены на том же основании, не трактуются как финальные.

**Форензик-находки, НЕ использованные для подгонки экстрактора** (только для честного отчёта):

- SPZU-029's real gold pd_value/rd_value are phrased as a POSITION/ROW COUNT of the MAF specification table ("N positions"), not a two-field "count / complete-set" pair -- suggesting the parameter's real semantic may be closer to this session's table-row-count mechanism (Phase C) than the compound mechanism (Phase B) it was wired to per classify_unit('шт. / компл.') == NUMERIC_COMPOUND. Not reclassified this session: one hidden, pending-second-review example is not enough evidence to safely move a code between mechanisms without risking exactly the single-example overfit the project's no-hardcoding-to-gold rule forbids -- flagged here as an open question for a future session with more evidence.
- SPZU-036's real gold pd_value/rd_value carry TWO numbers (fence height AND length: "h=2.0 м; 174.45 м") even though its catalog unit is 'м' (NUMERIC_SIMPLE, single value) -- the PRE-EXISTING (not this session's) numeric mechanism is therefore also likely structurally unable to capture both fields for this code. Noted, not fixed this session (out of the four extractors this session's scope covers).
- This session's four new generic mechanisms (numeric, enum, compound, table-row-count) never set a per-instance `location` field in their evidence groups' delta (they group one finding per PARAMETER, not per structural element, unlike the tuned rule packs) -- `evaluation/exporter.py::evidence_group_to_prediction` would still leave `location=""` for any of them, which cannot align with a gold check that expects a specific non-empty location (e.g. SPZU-027's "SITE"). This does not change the result above (an abstained prediction scores identically whether or not it would have aligned), but means a FUTURE comparable finding from any generic-tier mechanism against a location-scoped gold check would need this addressed first.

## Сигнал кросс-стадийной локализации этой сессии (document-manifest narrowing + table fingerprint)

Добавлены два новых, чисто аддитивных сигнала поверх всех 4 generic-механизмов (2026-09-21 (local venv, throwaway sqlite, no Docker)): document-manifest discipline narrowing (cross_stage_localization.narrow_candidate_fragments); structural table fingerprint reranking (anchor_search.extract_table_fingerprint / fingerprint_similarity). Прогнаны на тех же 2 публичных объектах тем же способом, что и real-data run выше — итоговые числа evidence groups **не изменились** (без изменений), новых сравнимых generic-tier evidence groups создано: 0.

Честная причина (установлена прямой диагностикой в этой сессии, не переформулировкой прежней гипотезы):

**OBJ-TYUMENSKAYA-5-GOLD-SEED**

Confirmed by direct inspection: 0/48 numeric-eligible codes (and 0/9 enum, 0/15 compound, 0/8 table-row-count codes) have even one RD-stage candidate fragment tagged by the upstream annotator at all -- this object's non-PD documents are all dataset_stage=RD_ID_MIXED, which every stage-bucketing step in this pipeline (old and new) correctly excludes rather than guessing. No ranking or fingerprinting signal can act on a candidate pool that is empty to begin with.

**OBJ-NOVOSLOBODSKAYA**

DOES have 10 genuine RD-stage documents, and 17/48 numeric-eligible codes DO have at least one RD-stage candidate fragment (a new finding this session, more precise than checkpoint 35's framing). But calling collect_generic_observations directly shows only 3/48 codes resolve on ANY stage at all, none on both PD+RD. Forensically reading the one RD candidate page tagged for PZ-002 (confidence 0.62) shows it is a real, unrelated document (a "Стена в грунте"/wall-in-ground construction-note-and-materials-bill sheet) that merely shares a few generic words -- a genuine false positive, not a lower-ranked correct page. Checking all 10 of this object's real RD-stage files confirms every one is a КР (structural: rebar/wall/shoring) drawing; none is a ПЗ/ГП-style document that would ever restate an object-level ТЭП scalar like "Общая площадь здания". The gap here is candidate EXISTENCE (no RD document of the right type contains the value at all), not candidate RANKING among several already-tagged pages -- a within-candidate-pool reranking signal cannot fix a value that was never written into any RD-stage document.

_This sharpens, rather than contradicts, the checkpoint 35/38 diagnosis. Their framing ("broad-recall/low-precision keyword tagging") suggested the right page exists among several tagged candidates but gets outranked; this session's direct inspection shows the more common failure on these two specific public objects is that the right page was never tagged at all (Tyumenskaya) or never existed in the RD-stage document set for this parameter class in the first place (Novoslobodskaya). The signal built this session is real, tested (27 new unit tests, 317/317 total suite green), purely additive (byte-identical evidence-group totals on both objects, zero regression), and would help in the scenario it targets -- multiple tagged candidates exist and one is structurally correct -- but that scenario essentially does not occur for the codes examined on these two objects. Phase D (SPZU-027/029/036/039, hidden object, blind) was re-run after this change: finding_recall is still 0.0 (n=1, identical to checkpoint 38) -- all four still abstain, unchanged._

## Семантические якоря (Sentence-BERT), ТЗ 9.1 п.2

До этой сессии ни один generic-механизм не использовал embedding-модель — только строковое/токенное сопоставление (`anchor_search.py`). Буквальный пункт ТЗ 9.1.2 ("регулярные выражения ... и семантические якоря. Используется модель Sentence-BERT (all-MiniLM-L6-v2) или её совместимые аналоги") был не закрыт. Добавлен новый опциональный модуль `semantic_similarity.py` (2026-09-21 (local venv, throwaway sqlite, no Docker)).

**Модель**: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (sentence-transformers==2.6.1, already used by rag_service).

The literal TZ model (all-MiniLM-L6-v2, English-trained) measured 0.53 vs 0.49 cosine similarity for a genuine Russian parameter match vs. a different-but-lexically-related Russian parameter on real project text -- a 0.04 margin, not usable as a filter. The multilingual variant already in production in this repo's rag_service separated the same real pair 0.62 vs 0.41-0.49. Adopted as the TZ's own allowed 'compatible analogue' instead of the literal model, trading ~460MB disk for a model that actually discriminates the language the documents are written in.

**Встроен в двух местах**:

- cross_stage_localization.pick_best_candidate (re-rank/filter already-tagged candidates, ON by default)
- anchor_search.find_anchor_end_index_for_phrase (semantic anchor-location fallback, OFF by default)

**Почему семантический fallback anchor-поиска выключен по умолчанию**:

Real regression testing (this session's own new unit tests, not a hypothetical) caught the fallback confusing two DIFFERENT, unrelated same-domain ТЭП tables' own 'Площадь' rows -- a building-area parameter's anchor search matched a site-area table's row purely on embedding similarity, reviving a collision test_case10_generic_extraction.py's existing deterministic-matcher tests were specifically written to prevent. This is a genuine precision cost of comparing short, vocabulary-overlapping label text, not a threshold-tuning artifact -- raising SEMANTIC_MIN_SIMILARITY does not fix it because the genuine near-miss score (site-area row vs. building-area query) and the genuine match score occupy overlapping ranges. Re-ranking (a) has no equivalent failure mode because it only ever narrows a pool a TEXTUAL anchor already independently corroborated -- it cannot invent a match from nothing.

**Результат на публичных объектах** (тот же способ, что и real-data run выше): итоговые числа evidence groups и их `status_counts`/`source_counts` **не изменились** — status_counts and source_counts dicts verified byte-identical per object between the semantic layer OFF and ON (default), not just total counts -- see CASE10_MATRIX_132_COVERAGE.md. Новых сравнимых generic-tier evidence groups: 0.

Механизм при этом подтверждённо **реально работает** на реальных данных (не мёртвый код): инструментирование `pick_best_candidate` во время реального прохода по OBJ-NOVOSLOBODSKAYA показало 10 вычислений кандидатов, все 10 получили реальный score (диапазон 0.269–0.729), 1 из них ниже порога 0.45 и был бы отфильтрован.

_Checkpoint 40 already established that on both public objects the bottleneck is candidate EXISTENCE, not candidate RANKING/relevance -- OBJ-TYUMENSKAYA-5-GOLD-SEED has zero RD-stage candidates for any generic-tier code at all (RD_ID_MIXED), and OBJ-NOVOSLOBODSKAYA's real RD-stage document set for these codes never contains the value in the first place (all 10 RD documents are КР drawings, not ПЗ/ГП-style). A re-ranking/filtering signal over an existing candidate pool cannot, by construction, fix an empty or wrong-type pool -- exactly the scenario this session's real-data run confirms._

**Производительность** (бюджет ТЗ раздел 11: 500ms +/- 100ms per parameter): прогретый инференс 1.4 мс/текст батчем, 47 мс первый одиночный вызов — на 1-2 порядка меньше бюджета. One-time cold load happens once per process lifetime (lazy singleton), not per parameter -- amortized cost after warmup is 1-50ms, far under budget even without additional caching. This session's own sandbox intermittently added 30-140s to that one-time load from retried HTTPS HEAD requests against a self-signed TLS interception on this specific machine (see CASE10_MATRIX_132_COVERAGE.md); a production container should set HF_HUB_OFFLINE=1 with the model pre-baked into the image at build time (added to api_service/Dockerfile this session) to avoid any network dependency at runtime.

**Тесты**: test_case10_semantic_similarity.py (real model, real forensic text), test_case10_anchor_search.py, plus new cases in test_case10_cross_stage_localization.py (mocked scores, including an end-to-end synthetic reproduction of the checkpoint-40 finding). Full suite: 379/379 (351 baseline + 28 new).

## Top missing capabilities (что закрывает остаток CONFIG_ONLY)

### Material/fire/structural class or grade lookup (7 параметров)

Small controlled per-domain vocabulary extractor (concrete/steel grades, fire-resistance classes, material marks) -- same shape as the existing KR-055 rule pack, generalized across a curated code list instead of one code.

Примеры кодов: IOS2-072, IOS3-075, OOS-098, OOS-099, OOS-100, OOS-101, PPM-109

### Multi-axis numeric specification (10 параметров)

Structured multi-value/multi-unit parser (e.g. "м3/ч / Па / кВт" fan curves, "шт. / м" counts-with-dimension) -- the generic anchor mechanism only ever extracts a single scalar.

Примеры кодов: AR-052, KR-066, POD-094, PPM-114, PZ-016, PZ-017, PZ-018, ZU-126

### Equipment/room/fixture count (5 параметров)

Table-row counting over a specification/schedule list, not a single label:value pair -- needs a table-structure extractor, not an anchor-phrase one.

Примеры кодов: POS-082, POS-086, PPM-103, PZ-007, PZ-013

### Presence/scheme/drawing-only check (18 параметров)

Drawing or scheme interpretation (fencing type on a plan, drainage layout presence, landscaping composition) -- inherently a CV/vector-drawing comparison problem, not text.

Примеры кодов: AR-043, AR-053, KR-063, ODI-115, ODI-122, ODI-123, POD-091, POD-092

## Приоритетный backlog

### P0 — есть реальный gold, реализации нет (1)

Real organizer-provided gold checks exist for these codes (closed methodology package) but no extractor exists at all -- highest-value next target since correctness can be measured immediately, not just asserted.

- `SPZU-039` Мероприятия по инженерной подготовке (дренажи) (—, NONE)

### P1 — PARTIAL + реальный gold, готово к немедленной валидации (3)

Already PARTIAL (generic anchor mechanism applies) AND has real gold checks -- the cheapest next step is running the existing mechanism against the closed gold for these specific codes to get a first real precision/recall reading before investing in anything new.

Коды: SPZU-027, SPZU-029, SPZU-036

### P1 — CONFIG_ONLY, HIGH review priority (35)

CONFIG_ONLY parameters whose criticality is "Критическое (приостановка работ)" -- highest business impact if a real violation is missed, prioritize extractor engineering here over MEDIUM-priority CONFIG_ONLY parameters.

Коды: AR-043, AR-044, IOS1-068, KR-063, KR-066, ODI-115, ODI-123, OOS-098, OOS-099, OOS-100, OOS-101, POD-091, POD-092, POD-096, POS-085, POS-087, PPM-102, PPM-103, PPM-106, PPM-109, PPM-111, PPM-114, PZ-007, PZ-013, PZ-014, PZ-016, PZ-017, PZ-018, SPZU-032, SPZU-034, SPZU-035, SPZU-039, ZU-126, ZU-127, ZU-131

### P2 — CONFIG_ONLY, MEDIUM review priority (12)

CONFIG_ONLY, MEDIUM review priority -- lowest business impact, defer.

Коды: AR-052, AR-053, IOS2-072, IOS3-075, ODI-122, POD-094, POD-095, POS-082, POS-086, POS-089, SM-132, ZU-130

## Полная таблица (132/132)

| Код | Раздел | Параметр | Unit | Класс | Extractor | Readiness | Тесты | Gold |
|---|---|---|---|---|---|---|---|---|
| PZ-001 | Раздел 1. ПЗ | Площадь застройки | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL | ✓ |  |
| PZ-002 | Раздел 1. ПЗ | Общая площадь здания | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL | ✓ |  |
| PZ-003 | Раздел 1. ПЗ | Полезная / Расчетная площадь | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-004 | Раздел 1. ПЗ | Строительный объем (Общий) | м³ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL | ✓ |  |
| PZ-005 | Раздел 1. ПЗ | Строительный объем (Подземный) | м³ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-006 | Раздел 1. ПЗ | Строительный объем (Надземный) | м³ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-007 | Раздел 1. ПЗ | Этажность (надземная) | ед. | NUMERIC_COUNT | NONE | CONFIG_ONLY | ✓ |  |
| PZ-008 | Раздел 1. ПЗ | Высота здания | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-009 | Раздел 1. ПЗ | Абсолютная отметка 0.000 | м | NUMERIC_SIMPLE | OFFICIAL_RULE_PACK | PRODUCTION_READY | ✓ | 1 |
| PZ-010 | Раздел 1. ПЗ | Количество квартир | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL | ✓ |  |
| PZ-011 | Раздел 1. ПЗ | Квартирография | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| PZ-012 | Раздел 1. ПЗ | Количество машино-мест (подземных) | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL | ✓ |  |
| PZ-013 | Раздел 1. ПЗ | Технологическая мощность / Вместимость | ед. | NUMERIC_COUNT | NONE | CONFIG_ONLY |  |  |
| PZ-014 | Раздел 1. ПЗ | Расчетная электрическая мощность | кВт | OTHER | NONE | CONFIG_ONLY |  |  |
| PZ-015 | Раздел 1. ПЗ | Категория надежности электроснабжения | Кат. | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| PZ-016 | Раздел 1. ПЗ | Суточный расход водопотребления | м³/сут | NUMERIC_COMPOUND | NONE | CONFIG_ONLY | ✓ |  |
| PZ-017 | Раздел 1. ПЗ | Суммарная тепловая нагрузка | Гкал/ч | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| PZ-018 | Раздел 1. ПЗ | Максимальный часовой расход газа | м³/ч | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| PZ-019 | Раздел 1. ПЗ | Коэффициент застройки (КЗ) | % | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-020 | Раздел 1. ПЗ | Коэффициент использования территории (КИТ) | % | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PZ-021 | Раздел 1. ПЗ | Класс энергетической эффективности | Буква | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| PZ-022 | Раздел 1. ПЗ | Степень огнестойкости здания | Степень | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| PZ-023 | Раздел 1. ПЗ | Класс конструктивной пожарной опасности | Класс (С0, С1) | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| SPZU-024 | Раздел 2. СПЗУ | Объем грунта (выемка/насыпь) | м³ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-025 | Раздел 2. СПЗУ | Площадь асфальтобетонного покрытия | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-026 | Раздел 2. СПЗУ | Площадь тротуарного плиточного покрытия | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-027 | Раздел 2. СПЗУ | Площадь озеленения и газонов | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL | ✓ | 1 |
| SPZU-028 | Раздел 2. СПЗУ | Площадь детских и спортивных площадок | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-029 | Раздел 2. СПЗУ | Спецификация МАФ | шт. / компл. | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL | ✓ | 1 |
| SPZU-030 | Раздел 2. СПЗУ | Ширина внутриплощадочных дорог и проездов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-031 | Раздел 2. СПЗУ | Радиусы поворота дорог и пожарных проездов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-032 | Раздел 2. СПЗУ | Конструкция дорожной одежды | мм (слои) | OTHER | NONE | CONFIG_ONLY |  |  |
| SPZU-033 | Раздел 2. СПЗУ | Уклоны дорог и проездов | ‰ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| SPZU-034 | Раздел 2. СПЗУ | Точки и координаты подключения внешних сетей | Коорд. | OTHER | NONE | CONFIG_ONLY |  |  |
| SPZU-035 | Раздел 2. СПЗУ | Охранные зоны существующих коммуникаций | — | NONE | NONE | CONFIG_ONLY |  |  |
| SPZU-036 | Раздел 2. СПЗУ | Тип и высотные параметры ограждения территории | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  | 1 |
| SPZU-037 | Раздел 2. СПЗУ | Количество и разметка парковочных мест на участке | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| SPZU-038 | Раздел 2. СПЗУ | Количество парковочных мест для МГН на участке | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| SPZU-039 | Раздел 2. СПЗУ | Мероприятия по инженерной подготовке (дренажи) | — | NONE | NONE | CONFIG_ONLY |  | 1 |
| AR-040 | Раздел 3. АР | Ширина магистральных эвакуационных коридоров | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-041 | Раздел 3. АР | Ширина эвакуационных выходов (дверей) | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-042 | Раздел 3. АР | Высота путей эвакуации и проемов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-043 | Раздел 3. АР | Направление открывания эвакуационных дверей | — | NONE | NONE | CONFIG_ONLY |  |  |
| AR-044 | Раздел 3. АР | Послойный состав пирога кровли | мм (слои) | OTHER | NONE | CONFIG_ONLY |  |  |
| AR-045 | Раздел 3. АР | Уклоны кровли и система водостока | % / ° | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| AR-046 | Раздел 3. АР | Общее количество и габариты оконных блоков | шт. / м² | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| AR-047 | Раздел 3. АР | Габариты и глубина тамбуров входных групп | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-048 | Раздел 3. АР | Количество и параметры лестничных маршей и ступеней | шт. / мм | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| AR-049 | Раздел 3. АР | Высота и тип ограждений лестниц, балкона, кровли | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-050 | Раздел 3. АР | Спецификация и типы внутренней отделки помещений | Класс (КМ) | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| AR-051 | Раздел 3. АР | Параметры естественного освещения (КЕО) | % | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| AR-052 | Раздел 3. АР | Цветовое решение и материалы облицовки фасадов | RAL / Артикул | NUMERIC_COMPOUND | NONE | CONFIG_ONLY | ✓ |  |
| AR-053 | Раздел 3. АР | Конструктивные мероприятия по защите от шума | — | NONE | NONE | CONFIG_ONLY |  |  |
| KR-054 | Раздел 4. КР | Шаг и привязка координационных осей несущего каркаса | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-055 | Раздел 4. КР | Класс прочности бетона монолитных конструкций | Марка (B) | ENUM_CLASS | OFFICIAL_RULE_PACK | PRODUCTION_READY | ✓ | 3 |
| KR-056 | Раздел 4. КР | Марка стали и класс прочности металлопроката | Марка (С) | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL | ✓ |  |
| KR-057 | Раздел 4. КР | Марка и класс прочности рабочей арматуры | Класс (А) | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| KR-058 | Раздел 4. КР | Толщина монолитной фундаментной плиты / ростверка | мм | NUMERIC_SIMPLE | OFFICIAL_RULE_PACK | PRODUCTION_READY | ✓ | 1 |
| KR-059 | Раздел 4. КР | Толщина монолитных плит перекрытий и покрытия | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-060 | Раздел 4. КР | Геометрические сечения несущих колонн и пилонов | мм² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-061 | Раздел 4. КР | Толщина несущих монолитных стен (ядра жесткости) | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-062 | Раздел 4. КР | Диаметр продольной (рабочей) арматуры в колоннах/пилонах | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-063 | Раздел 4. КР | Наличие и конструктивное исполнение деформационных швов | — | NONE | NONE | CONFIG_ONLY |  |  |
| KR-064 | Раздел 4. КР | Привязки и габариты лифтовых шахт | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL | ✓ |  |
| KR-065 | Раздел 4. КР | Схемы расположения и площади технологических проемов | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| KR-066 | Раздел 4. КР | Антикоррозионная и огнезащитная обработка конструкций | Марка / Толщина | NUMERIC_COMPOUND | NONE | CONFIG_ONLY | ✓ |  |
| KR-067 | Раздел 4. КР | Ведомости расхода материалов (бетон, сталь) | м³ / т | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| IOS1-068 | Раздел 5. ИОС1 | Номинальная аппаратная защита ВРУ/ГРЩ | А (Ампер) | OTHER | NONE | CONFIG_ONLY |  |  |
| IOS1-069 | Раздел 5. ИОС1 | Сечение жил и маркировка распределительных кабелей | мм² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| IOS1-070 | Раздел 5. ИОС1 | Параметры и контуры заземления и молниезащиты | Ом / мм² | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| IOS2-071 | Раздел 5. ИОС2 | Диаметры стояков и разводящих трубопроводов В1/Т3 | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| IOS2-072 | Раздел 5. ИОС2 | Материал и класс давления напорных труб В1/Т3 | Марка | ENUM_CLASS | NONE | CONFIG_ONLY | ✓ |  |
| IOS2-073 | Раздел 5. ИОС2 | Характеристики насосных станций хоз-питьевого водоснабжения | м³/ч / м / кВт | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| IOS3-074 | Раздел 5. ИОС3 | Диаметры выпусков и магистралей К1/К2 | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| IOS3-075 | Раздел 5. ИОС3 | Материал и тип канализационных труб и фасонных частей | Марка | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| IOS4-076 | Раздел 5. ИОС4 | Диаметры магистралей и стояков отопления (Т1, Т2) | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| IOS4-077 | Раздел 5. ИОС4 | Технические параметры отопительных приборов (радиаторов) | Вт / шт. | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| IOS4-078 | Раздел 5. ИОС4 | Сечения и геометрия воздуховодов общеобменной вентиляции | мм² | NUMERIC_SIMPLE | OFFICIAL_RULE_PACK | PRODUCTION_READY | ✓ | 5 |
| IOS4-079 | Раздел 5. ИОС4 | Характеристики вентиляторов общеобменной вентиляции | м³/ч / Па / кВт | NUMERIC_COMPOUND | OFFICIAL_RULE_PACK | PRODUCTION_READY | ✓ | 1 |
| IOS5-080 | Раздел 5. ИОС5 | Состав и емкость систем пожарной сигнализации (АПС) | шт. / зоны | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| POS-081 | Раздел 6. ПОС | Границы опасных зон работы грузоподъемных кранов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| POS-082 | Раздел 6. ПОС | Продолжительность этапов строительства (Календарный график) | дни | NUMERIC_COUNT | NONE | CONFIG_ONLY |  |  |
| POS-083 | Раздел 6. ПОС | Посадка и габариты временных зданий и сооружений | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| POS-084 | Раздел 6. ПОС | Схемы движения и ширина временных автодорог | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| POS-085 | Раздел 6. ПОС | Места размещения площадок складирования материалов | — | NONE | NONE | CONFIG_ONLY |  |  |
| POS-086 | Раздел 6. ПОС | Потребность в кадрах (Максимальная численность) | чел. | NUMERIC_COUNT | NONE | CONFIG_ONLY | ✓ |  |
| POS-087 | Раздел 6. ПОС | Технологическая последовательность возведения | — | NONE | NONE | CONFIG_ONLY |  |  |
| POS-088 | Раздел 6. ПОС | Точки подключения и мощности временных ресурсов | кВт / м³ | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| POS-089 | Раздел 6. ПОС | Расположение пунктов мойки колес и выездов | — | NONE | NONE | CONFIG_ONLY |  |  |
| POD-090 | Раздел 7. ПОД | Границы опасных зон развала и обрушения конструкций | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| POD-091 | Раздел 7. ПОД | Методы и технологическая последовательность демонтажа | — | NONE | NONE | CONFIG_ONLY |  |  |
| POD-092 | Раздел 7. ПОД | Мероприятия по защите действующих коммуникаций | — | NONE | NONE | CONFIG_ONLY |  |  |
| POD-093 | Раздел 7. ПОД | Общие объемы демонтируемых конструкций (по типам) | м³ | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| POD-094 | Раздел 7. ПОД | Масса и класс опасности образующихся отходов | т / Класс | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| POD-095 | Раздел 7. ПОД | Мероприятия по пылеподавлению и снижению шума | — | NONE | NONE | CONFIG_ONLY |  |  |
| POD-096 | Раздел 7. ПОД | Узлы временного расчленения сохраняемых конструкций | — | NONE | NONE | CONFIG_ONLY |  |  |
| POD-097 | Раздел 7. ПОД | Места и габариты площадок временного складирования лома | м² | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| OOS-098 | Раздел 8. ООС | Регистрация лимитов в АИС "ОСИГ" | Статус | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| OOS-099 | Раздел 8. ООС | Верфикация транспортных средств (ГЛОНАСС) | Статус | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| OOS-100 | Раздел 8. ООС | Фиксация рейсов через мобильный КПТС | Статус | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| OOS-101 | Раздел 8. ООС | Сверка легитимности полной утилизации | Статус | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| PPM-102 | Раздел 9. ППМ | Деление здания на пожарные отсеки | — | NONE | NONE | CONFIG_ONLY |  |  |
| PPM-103 | Раздел 9. ППМ | Пределы огнестойкости противопожарных дверей и ворот (EI) | мин | NUMERIC_COUNT | NONE | CONFIG_ONLY |  |  |
| PPM-104 | Раздел 9. ППМ | Ширина и высота эвакуационных проходов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PPM-105 | Раздел 9. ППМ | Ширина эвакуационных дверей наружных выходов | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| PPM-106 | Раздел 9. ППМ | Направление открывания дверей на путях эвакуации | — | NONE | NONE | CONFIG_ONLY |  |  |
| PPM-107 | Раздел 9. ППМ | Класс пожарной опасности отделочных материалов | Класс (КМ) | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| PPM-108 | Раздел 9. ППМ | Количество и расстановка извещателей АПС | шт. / м | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| PPM-109 | Раздел 9. ППМ | Пожарная маркировка кабелей систем ПЗ (СПЗ) | Марка | ENUM_CLASS | NONE | CONFIG_ONLY |  |  |
| PPM-110 | Раздел 9. ППМ | Количество и расстановка оповещателей СОУЭ | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| PPM-111 | Раздел 9. ППМ | Расположение и пределы огнестойкости клапанов (ОЗК) | мин (EI) | OTHER | NONE | CONFIG_ONLY |  |  |
| PPM-112 | Раздел 9. ППМ | Производительность вентиляторов ДУ и подпора воздуха | м³/ч / Па | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| PPM-113 | Раздел 9. ППМ | Расход воды и количество струй внутреннего пожаротушения (ВПВ) | л/с / шт. | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| PPM-114 | Раздел 9. ППМ | Расход воды на наружное пожаротушение (НПВ) | л/с | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| ODI-115 | Раздел 10. ОДИ | Технические параметры и наличие инвалидных подъемников | — | NONE | NONE | CONFIG_ONLY |  |  |
| ODI-116 | Раздел 10. ОДИ | Ширина коридоров на путях движения МГН | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| ODI-117 | Раздел 10. ОДИ | Ширина дверных проемов на путях движения МГН | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| ODI-118 | Раздел 10. ОДИ | Высота порогов в дверных проемах на путях МГН | м | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| ODI-119 | Раздел 10. ОДИ | Габариты и планировка санузлов для инвалидов (универсальные кабины) | м² / м | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| ODI-120 | Раздел 10. ОДИ | Спецификация опорных стационарных и откидных поручней | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| ODI-121 | Раздел 10. ОДИ | Количество и габариты парковочных мест для инвалидов | шт. / м | NUMERIC_COMPOUND | GENERIC_ANCHOR_COMPOUND | PARTIAL |  |  |
| ODI-122 | Раздел 10. ОДИ | Наличие и расстановка тактильно-контрастных указателей | — | NONE | NONE | CONFIG_ONLY |  |  |
| ODI-123 | Раздел 10. ОДИ | Состав систем двухсторонней связи и вызова помощника | — | NONE | NONE | CONFIG_ONLY |  |  |
| ZU-124 | Раздел 11. ЗУ | Класс энергетической эффективности здания | Буква | ENUM_CLASS | GENERIC_ANCHOR_ENUM | PARTIAL |  |  |
| ZU-125 | Раздел 11. ЗУ | Толщина теплоизоляционного слоя наружных стен | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| ZU-126 | Раздел 11. ЗУ | Коэффициент теплопроводности (λ) утеплителя стен | Вт/(м·С) | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| ZU-127 | Раздел 11. ЗУ | Коэффициент сопротивления теплопередаче окон (Ro) | м²·С/Вт | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| ZU-128 | Раздел 11. ЗУ | Толщина утеплителя чердачных перекрытий / кровли | мм | NUMERIC_SIMPLE | GENERIC_ANCHOR_TABLE | PARTIAL |  |  |
| ZU-129 | Раздел 11. ЗУ | Наличие приборов учета расхода энергоресурсов | шт. | NUMERIC_COUNT | GENERIC_TABLE_ROW_COUNT | PARTIAL |  |  |
| ZU-130 | Раздел 11. ЗУ | Установка энергосберегающего осветительного оборудования | — | NONE | NONE | CONFIG_ONLY |  |  |
| ZU-131 | Раздел 11. ЗУ | Удельный годовой расход тепловой энергии на отопление | кВт·ч/м² | NUMERIC_COMPOUND | NONE | CONFIG_ONLY |  |  |
| SM-132 | Раздел 12. СМ | Итоговая стоимость по Сводному сметному расчету (ССР) | тыс. руб. | OTHER | NONE | CONFIG_ONLY |  |  |
