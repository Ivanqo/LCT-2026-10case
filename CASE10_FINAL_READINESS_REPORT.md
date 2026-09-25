# CASE 10 Final P0 / Demo Readiness Report

Дата: 17.09.2026

## Executive Summary

CASE 10 готов к P0 demo по official public baseline:

- используется official matrix 1.1: `official-132-v1.1`;
- сохранена двойная идентификация параметров: `matrix_code` (`M-xxx`) и scoring/runtime code (`IOS4-078`, `IOS4-079`, `PZ-009`, `KR-055`, `KR-058` и т.д.);
- official public dataset импортируется без GOLD replay в inference;
- Tyumenskaya positive path по `IOS4-078/079` работает независимо от `check_id`;
- Novoslobodskaya negative controls не дают false positive;
- Evidence Viewer отдает реальные страницы PDF с bbox;
- submission schema validation проходит для обоих объектов;
- Annex 2 protocol, inspector decision, audit и PDF protocol проверены через HTTP API smoke.

Финальный P0 baseline не пытается подгонять `IOS4-078/314` под public gold page `18`: независимый text/layout/OCR context этого не подтверждает. Runtime evidence остается на реальной `F0201` page `20`.

## Что Закрыто По ТЗ

### Official Matrix 1.1

- `MatrixVersion`: `official-132-v1.1`.
- Параметров: 132.
- В API/UI доступны:
  - `matrix_code`: `M-001...M-132`;
  - `scoring_code`: `PZ/KR/IOS4/...`;
  - aliases: например `M-078 / IOS4-078`.

### Public Dataset Import

Финальный smoke import:

- matrix params: 132;
- public documents: 203;
- pages: 10 146;
- annotations: 30 318;
- hidden skipped: 213.

Импорт hidden/organizer labels не используется для inference.

### Independent Rule Packs

Runtime positive extraction закрыт для:

- `IOS4-078`;
- `IOS4-079`.

Runtime negative controls закрыты для:

- `PZ-009`;
- `KR-055`;
- `KR-058`.

Inference использует original PDF/OCR/layout context и locator annotations без `check_id`. GOLD labels не экспортируются как runtime evidence.

### RD_ID_MIXED

`RD_ID_MIXED` не превращается в общий статус документа. Evidence сохраняется на уровне конкретной страницы/наблюдения:

- `file_id`;
- stage;
- page;
- `bbox_normalized`;
- `bbox_pdf`.

Если стадия не доказана на уровне observation, такие параметры остаются `CLARIFICATION_REQUIRED`, `MISSING_EVIDENCE` или `NOT_COMPARABLE`.

### Evidence Viewer

Проверено через HTTP endpoint `/case10/evidence-fragments/{id}/page.png`.

Сгенерированы PNG для ключевых demo findings:

- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-079_012_F0201_p17.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_140_F0201_p18.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_142_F0201_p18.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_147_F0201_p18.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_198_F0201_p18.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_314_F0201_p20.png`
- Novoslobodskaya negative-control PNG for `PZ-009`, `KR-055`, `KR-058`.

### Inspector / Protocol

Финальный smoke проверил:

- inspector decision endpoint;
- stale finalize rejection;
- protocol version history;
- audit log;
- finalized protocol;
- Annex 2 structure;
- protocol PDF export.

Свежие protocol PDFs:

- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.protocol.pdf`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.protocol.pdf`

## Финальные Метрики

### Tyumenskaya

Object: `OBJ-TYUMENSKAYA-5-GOLD-SEED`

- statuses: 130 `CLARIFICATION_REQUIRED`, 6 `CANDIDATE`, 4 `NEGATIVE_VERIFIED`;
- positive findings:
  - `IOS4-079/012`;
  - `IOS4-078/140`;
  - `IOS4-078/142`;
  - `IOS4-078/147`;
  - `IOS4-078/198`;
  - `IOS4-078/314`;
- precision: `1.0`;
- recall: `1.0`;
- F1: `1.0`;
- source localization exact file/page: `0.916667`;
- evidence localization accuracy: `0.916667`;
- normalized value/status accuracy: `1.0`;
- coverage: `0.015152`;
- abstention rate: `0.984848`;
- submission schema validation: OK.

### Novoslobodskaya

Object: `OBJ-NOVOSLOBODSKAYA`

- statuses: 108 `NOT_COMPARABLE`, 4 `NEGATIVE_VERIFIED`, 21 `MISSING_EVIDENCE`;
- negative controls:
  - `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-058`: `1000/1200 -> 1200/1500`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
  - `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`, `NON_TRIGGERING`;
- FPR: `0.0`;
- coverage: `0.022727`;
- abstention rate: `0.977273`;
- submission schema validation: OK.

## Sample Size И 95% Доверительные Интервалы (ТЗ 14.3)

ТЗ 14.3 требует публиковать вместе с каждой точечной метрикой размер выборки и
95%-й доверительный интервал (Wilson score interval, без scipy), а также
считать метрики отдельно по разделам/категориям нарушения. Это реализовано в
`evaluation/metrics.py` (`_wilson_interval`/`_metric_with_ci`) и пересчитано
здесь по тем же сохранённым артефактам (`*.predictions.json` +
`public_gold_checks.jsonl`), что и в исходном прогоне — без Docker/live-сервера.

**Это не баг, а ожидаемый результат задачи**: точечная оценка `1.0` на выборке
`n=6` выглядит как безупречный результат, но статистически это очень мало
данных — интервал обязан быть широким, и он широкий. Скрывать эту
неопределённость до отправки на закрытый тест — не цель, а именно её сделать
видимой была цель данной работы.

### Tyumenskaya (`OBJ-TYUMENSKAYA-5-GOLD-SEED`)

| Метрика | Точка | n | 95% CI |
|---|---|---|---|
| finding_precision | 1.0 | 6 | [0.610, 1.0] |
| finding_recall | 1.0 | 6 | [0.610, 1.0] |
| finding_f1 | 1.0 | 12 | [0.758, 1.0] |
| evidence_localization_accuracy | 0.9167 | 12 | [0.646, 0.985] |
| false_positive_rate | null | null | null (no negative denominator on this object) |
| per_category (`IOS4`) precision/recall | 1.0 / 1.0 | 6 / 6 | [0.610, 1.0] / [0.610, 1.0] |

### Novoslobodskaya (`OBJ-NOVOSLOBODSKAYA`)

| Метрика | Точка | n | 95% CI |
|---|---|---|---|
| false_positive_rate | 0.0 | 5 | [0.0, 0.434] |
| evidence_localization_accuracy | 0.4167 | 12 | [0.193, 0.680] |
| finding_precision / recall / f1 | null | null | null (no positive findings on this object) |
| per_category (`KR`, `PZ`) | null | null | null (no comparable findings in either category yet) |

**Reading this honestly**: precision/recall of `1.0` at `n=6` does not mean
"the model is perfect" -- the true rate could plausibly be as low as `~61%`
and still be consistent with what was observed. Same for Novoslobodskaya's
FPR `0.0` at `n=5`: the interval reaches `43%`, i.e. a single additional false
positive on the next few objects would not be a surprise given this sample
size. Both are exactly what a 95% Wilson interval on n=5-6 should look like --
the fix here is making that visible, not tightening it artificially. Widening
these intervals requires more labeled objects (currently 1 positive + 1
negative pilot object), not a change to the metric formula.

Refreshed artifacts:
`evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.metrics.json`,
`evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.metrics.json`.

## Gates

### Passed

- Tyumenskaya precision: `1.0 >= 0.90`.
- Tyumenskaya recall: `1.0 >= 0.80`.
- Tyumenskaya F1: `1.0 >= 0.85`.
- Tyumenskaya value/status: `1.0 >= 0.90`.
- Novoslobodskaya FPR: `0.0 <= 0.10`.
- Object-level split: valid.
- Submission schema validation: OK for both objects.

### Below Or Not Applicable

- Tyumenskaya localization: `0.916667 < 0.95`.
  - Only known miss: `IOS4-078/314`, runtime `F0201` page `20`, public gold `F0201` page `18`.
- OCR accuracy: `null`.
- Key fields exact match: `null`.
  - Reason (both above): public fixture does not contain denominators for these metric families (`ocr_items`/`key_fields` counts are `0`, not `12` — genuinely no comparable rows, so `metrics.py` correctly reports `null` rather than `0`).
- Document linkage accuracy: **not `null`** — corrected 2026-09-20.
  - The canonical `evaluation/reports/real_data/*.metrics.with_ci.json` artifacts (recomputed at checkpoint 37) show `document_linking_accuracy = 0.0` at `n = 12` for both objects, previously undocumented here. This is **not** an evaluation-code bug and **not** a real document-linking failure: `_evaluate_exact_match` in `evaluation/metrics.py` already returns `null` for a genuinely empty denominator (line ~195), and the gold denominator here is genuinely `12` (from `public_gold_checks.jsonl` evidence `file_id` rows, synthesized by `gold_checks_to_evaluation_fixture`).
  - Root cause: the specific `evaluation/reports/real_data/*.predictions.json` snapshots used for that recompute were captured on 2026-09-17 11:34, **before** `evaluation/exporter.py::protocol_to_evaluation_predictions` started populating a `document_links` array (that field is entirely absent from those two files). With zero predicted links, all 12 gold links go unmatched -> `0.0`.
  - Reproducible proof it is fixed at the export layer: any later predictions snapshot (e.g. `evaluation/reports/real_data_rotation_fix/OBJ-NOVOSLOBODSKAYA.predictions.json`, `.../OBJ-TYUMENSKAYA-5-GOLD-SEED.predictions.json`, both from 2026-09-20) already carries a populated `document_links` array (202 entries, synthesized 1:1 from evidence `file_id`s). Re-running `python -m evaluation.run_evaluation --gold-checks-jsonl learning_data/extracted/train_public_203/data/public_gold_checks.jsonl --predictions evaluation/reports/real_data_rotation_fix/OBJ-NOVOSLOBODSKAYA.predictions.json --object-id OBJ-NOVOSLOBODSKAYA --allow-hidden-labels` against the same public gold yields `document_linking_accuracy = 1.0` (`n = 12`, 95% CI `[0.758, 1.0]`); the same command for `OBJ-TYUMENSKAYA-5-GOLD-SEED` also yields `1.0` (`n = 12`, CI `[0.758, 1.0]`).
  - The `real_data/*.metrics.json` files themselves are left as an untouched historical record of that specific (stale-fixture) run rather than hand-edited or silently regenerated with a newer predictions snapshot, since the latter would also shift other metrics (e.g. localization, which moved with the rotation-normalization fix) that are outside this correction's scope.
- Tyumenskaya FPR: `null`.
  - Reason: public Tyumenskaya fixture has no negative denominator.

## Почему `IOS4-078/314` Не Подгоняется

Runtime found `314` on real source `F0201` page `20`.

Independent page `18` audit:

- text layer: no token `314`;
- drawing-room layout tokens: no room `314`;
- OCR full page: no `314`;
- OCR quadrants: no `314`;
- page `20`: text/layout contains `314`.

Changing primary evidence to page `18` would be fitting to public gold localization, not evidence-based inference. For demo and pitch, present this as a known exact-localization residual risk: finding/value/status are correct, exact public file/page differs for one evidence item.

## Demo Scenarios To Show

1. Matrix 1.1 and aliases:
   - show `official-132-v1.1`;
   - show `M-078 / IOS4-078`, `M-079 / IOS4-079`, plus `PZ/KR` examples.
2. Tyumenskaya positive path:
   - filter `CANDIDATE`;
   - show `IOS4-078` rooms `140/142/147/198/314`;
   - show `IOS4-079` room `012`.
3. Evidence Viewer:
   - open page image for `IOS4-078/140` on `F0201` page `18`;
   - open `IOS4-078/314` on `F0201` page `20` and explicitly explain why it is not forced to page `18`.
4. Novoslobodskaya negative controls:
   - show `PZ-009`, `KR-055`, `KR-058` as `NEGATIVE_VERIFIED`;
   - highlight FPR `0.0`.
5. Inspector workflow:
   - make or show `Clarification Required` / `Confirm` / `Reject`;
   - show protocol version change and audit trail.
6. Annex 2 protocol:
   - show finalized protocol;
   - show protocol PDF;
   - show Annex 2 sections.
7. Evaluation metrics:
   - show precision/recall/F1 `1.0 / 1.0 / 1.0`;
   - show value/status `1.0`;
   - show localization residual `0.916667`;
   - show Novoslobodskaya FPR `0.0`.

## Pitch Risks To Name Honestly

- Exact localization gate is still below threshold because of `IOS4-078/314` page `20` vs public gold page `18`.
- Coverage is intentionally low in P0 because only high-confidence official rule packs are enabled. Broad recall across all 132 parameters is outside current P0.
- OCR/key-field/document-link metrics are structurally `null` until the public fixture supplies denominator rows.
- Novoslobodskaya localization/value-status metrics are not the main P0 acceptance target; current P0 target for that object is negative-control FPR, and it passes.
- IFC/CV/RabbitMQ/Postgres production architecture is outside this stabilized demo layer and should not be pulled into the pitch as completed.

## Artifacts

- `evaluation/reports/real_data/summary.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.metrics.json`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.metrics.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.submission.json`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.submission.json`
- `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.protocol.pdf`
- `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.protocol.pdf`
- `evaluation/reports/real_data/key_evidence_pages/`
