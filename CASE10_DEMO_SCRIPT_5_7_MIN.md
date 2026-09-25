# CASE 10 Demo Script, 5-7 минут

Дата: 17.09.2026

## Цель Демо

Показать, что CASE 10 P0 работает на official public data без GOLD replay:

- official matrix 1.1;
- реальные PDF evidence с file/page/bbox;
- Tyumenskaya positive detection по `IOS4-078/079`;
- Novoslobodskaya negative controls без false positive;
- inspector workflow;
- Annex 2 protocol;
- evaluation metrics.

## 0:00-0:45 - Official Matrix 1.1

Что показать:

- открыть CASE10 matrix view;
- показать `official-132-v1.1`;
- показать 132 параметра;
- показать двойной код:
  - `matrix_code`: `M-078`, `M-079`;
  - `scoring_code`: `IOS4-078`, `IOS4-079`;
  - aliases: `M-078 / IOS4-078`.

Что сказать:

> Мы не заменяем коды организатора внутренними id. Сохраняем две оси идентификации: официальный номер строки матрицы `M-xxx` и scoring/runtime code, который используется в оценке и submission.

## 0:45-2:00 - Tyumenskaya Positive Detection

Что показать:

- объект `OBJ-TYUMENSKAYA-5-GOLD-SEED`;
- фильтр или список статусов:
  - 6 `CANDIDATE`;
  - 4 `NEGATIVE_VERIFIED`;
  - 130 `CLARIFICATION_REQUIRED`;
- кандидаты:
  - `IOS4-079/012`;
  - `IOS4-078/140`;
  - `IOS4-078/142`;
  - `IOS4-078/147`;
  - `IOS4-078/198`;
  - `IOS4-078/314`.

Что сказать:

> Эти positive findings получены из original PDF/OCR/layout context. GOLD `check_id` не используется в inference. Для `IOS4-078` мы ищем независимые наблюдения по помещениям, системам и листам, а затем сравниваем нормализованные `PD/RD` значения.

## 2:00-3:00 - Evidence Viewer / Page PNG

Что показать:

- открыть Evidence Viewer для `IOS4-078/140`;
- показать:
  - `file_id = F0201`;
  - page `18`;
  - bbox normalized / bbox pdf;
  - страницу с выделением;
- открыть `IOS4-079/012`, page `17`;
- открыть `IOS4-078/314`, page `20`.

Готовые PNG artifacts:

- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_140_F0201_p18.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-079_012_F0201_p17.png`
- `evaluation/reports/real_data/key_evidence_pages/OBJ-TYUMENSKAYA-5-GOLD-SEED_IOS4-078_314_F0201_p20.png`

Что сказать про `314`:

> Здесь важно: public gold ожидает page 18, но независимая проверка text layer, layout tokens и OCR не нашла помещение `314` на page 18. На page 20 оно есть. Мы не подгоняем evidence под gold, поэтому честно оставляем exact-localization residual risk.

## 3:00-4:00 - Novoslobodskaya Negative Controls

Что показать:

- объект `OBJ-NOVOSLOBODSKAYA`;
- `PZ-009`: `159.95 -> 159.95`, `NEGATIVE_VERIFIED`;
- `KR-058`: `1000/1200 -> 1200/1500`, `NEGATIVE_VERIFIED`;
- `KR-055`: `B40 -> B40`, `NEGATIVE_VERIFIED`;
- `KR-055`: `B60 -> B60`, `NEGATIVE_VERIFIED`;
- FPR `0.0`.

Что сказать:

> Негативные контроли не превращаются в false positives. Это критично для demo: мы показываем не только нахождение нарушений, но и консервативность там, где нарушения нет.

## 4:00-5:00 - Inspector Decision

Что показать:

- кнопки `Confirm`, `Reject`, `Clarify`;
- результат inspector decision;
- protocol version history;
- audit log action `INSPECTOR_DECISION`.

Что сказать:

> Runtime выдает evidence groups, а инспектор принимает решение поверх доказательств. Решение фиксируется в audit trail и создает новую версию протокола. Stale finalize защищен: старую версию нельзя финализировать после изменения.

## 5:00-6:00 - Annex 2 Protocol

Что показать:

- finalized protocol;
- Annex 2;
- PDF export:
  - `evaluation/reports/real_data/OBJ-TYUMENSKAYA-5-GOLD-SEED.protocol.pdf`;
  - `evaluation/reports/real_data/OBJ-NOVOSLOBODSKAYA.protocol.pdf`;
- submission validation errors: `[]`.

Что сказать:

> Annex 2 собирается из тех же EvidenceGroup, которые видит инспектор. Submission проходит schema validation для обоих объектов.

## 6:00-7:00 - Evaluation Metrics / Honest Risks

Что показать:

- `evaluation/reports/real_data/summary.json`;
- Tyumenskaya:
  - precision `1.0`;
  - recall `1.0`;
  - F1 `1.0`;
  - value/status `1.0`;
  - localization `0.916667`;
- Novoslobodskaya:
  - FPR `0.0`;
  - coverage `0.022727`;
  - abstention `0.977273`.

Что сказать:

> P0 gates по precision, recall, F1, value/status и negative-control FPR закрыты. Ниже gate остается только exact localization из-за одного evidence item: `IOS4-078/314`, где real source page 20 расходится с public gold page 18. Это не исправлено искусственно, потому что independent OCR/layout context page 18 не подтверждает.

## Вопросы, Которые Стоит Предвосхитить

### Почему coverage низкий?

P0 включает только надежные official rule packs. Остальные параметры остаются `CLARIFICATION_REQUIRED`, `MISSING_EVIDENCE` или `NOT_COMPARABLE`, чтобы не надувать precision фальшивыми догадками.

### Почему Tyumenskaya FPR `null`?

В public Tyumenskaya fixture нет negative denominator. FPR корректно демонстрируется на Novoslobodskaya negative controls, где он равен `0.0`.

### Почему localization не 1.0?

Только `IOS4-078/314`: runtime source page `20`, gold page `18`. Page `18` проверена text/layout/OCR и не содержит независимого `314`.

### Почему не показываем IFC/CV/Postgres?

Это вне стабилизированного P0 demo. Сейчас цель - показать доказательный official-data workflow, метрики и протокол без GOLD replay.
