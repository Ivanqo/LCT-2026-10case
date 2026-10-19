# CASE10 CLI / host parity report

Worktree branch: `codex/parity-tyu-20261019`. Final validation was completed on 2026-10-19 against the branch point `f3568e4`, using the main Python 3.11 venv and offline model/cache settings.

## Previously confirmed results (10:06 MSK)

| Route / input | Checks | M-078 cards | Result |
|---|---:|---:|---|
| Host `run_baseline.py` TYU, existing run `E:\case10_par\postpatch-parity\host` | 170 | 14 (13 candidates, 1 suggestion) | SHA-256 `a754b0df5884ed8c72506c617f8561acfdce2211b80c63c292e11508c199ddf0`; includes `/012` |
| CLI TYU, `demo.py stage` registry (PD 47 / RD 4 / ID 6) | 170 | **13 (12 candidates, 1 suggestion)** | Exact tuple match to `deploy/demo/expected/tyu.json`; SHA-256 `0df910a94d222ef18af2dab83d4de1a796b62466b69bd74d01357972d4cce538` |

CLI package report: registry `ACCEPTED`, 57/57 files imported, no registry rows without a file, no warnings. The host-only item is M-078 `/012`; all other TYU M-078 tuples and tiers match the expected demo set.

## Cause of the E1 zero-result path

`evaluation/phase12/FINAL_ACCEPTANCE_REPORT.md` §8a records three tested TYU registry variants: (1) missing approval status blocked documents as `revision_clarification_required`; (2) `APPROVED` with every package file assigned PD/RD left no ID and returned `missing_stage`; (3) approval present but stage absent collapsed the mixed RD/ID folder to `RD_ID_MIXED` and required clarification. Each variant produced zero M-078/M-079 cards. The staged demo registry instead carries per-file stage assignments (47 PD, 4 RD, 6 ID), which the current CLI uses; the completed TYU run produced the 13 expected cards.
E1's acceptance comparison was CLI 0/10 versus host 4/10 on the official points. The corrected staged-registry CLI now reaches the separate demo expectation of 13 tuples; the host import without that same registry yields 14 M-078 cards, so those two totals are reported separately rather than treated as identical metrics.

| E1 CLI registry variant | M-078/M-079 before | Reported blocking state |
|---|---:|---|
| Approval status omitted | 0/10 | `revision_clarification_required` from `UNKNOWN` approval |
| All files staged PD/RD | 0/10 | `missing_stage` because no ID stage was assigned |
| Stage omitted for mixed RD/ID folder | 0/10 | `RD_ID_MIXED` and clarification required |
| Current staged TYU registry | **13** | `ACCEPTED`, 57/57 files imported, exact expected tuple set |

The host importer without that same per-file registry still produces 14 cards and includes `/012`. This is a documented input-semantic difference: host import sees the mixed folder as RD_ID_MIXED, while the demo registry splits individual RD and ID files. A synthetic import-parity test now checks that `batch_package` and `_import_document_manifest` store identical document fields when both consume the same plan row.

There is also a code-level selector defect in the old path: TYU's staged registry has 8 rows marked `OV`/`ОВ`, while the prior ventilation discipline predicate accepted Cyrillic `ОВ` but not Latin `OV`. The same predicate gates both PD-sheet and RD-drawing selection. The new canonical section resolver maps catalog aliases before those selectors; a focused test covers both selector calls. This explains why matching registry stages alone was insufficient for all S2 evidence.

## Changes in this branch

- Canonicalize section marks across Latin/Cyrillic spellings and catalog codes before ventilation selection; `OV` now reaches both S2 sheet selectors.
- Preserve ambiguous combined stage labels for clarification instead of silently choosing the first stage token.
- Surface unrecognized/noncanonical registry headers (including `Статус документа`, `Стадия разработки`, `Код раздела проекта`) in `package.unrecognized_columns`, `package.registry_warnings`, and CLI output while retaining compatible value parsing.
- Default `CASE10_APPROVAL_UNKNOWN_POLICY=soft`: only a clean singleton with missing/`UNKNOWN` approval becomes a suggestion with `approval_unverified`; DRAFT, forbidden statuses and revision conflicts remain blocked.
- Emit selection-report approval-policy metadata only when at least one document actually uses the unverified-status assumption, avoiding empty metadata on fully approved packages.
- Add `evaluation/phase12/par_check.py` for host-vs-CLI TYU/ALT79B summaries, tuple differences, hashes and post-run TYU expected-set comparison.

### Registry compatibility exercised by focused tests

| Input variant | Handling |
|---|---|
| Russian/English aliases for file, stage, section, approval and revision headers | Parsed into the canonical Perechen fields; legacy headings named in E1 are also surfaced in `unrecognized_columns` for review. |
| Stages `ПД/РД/ИД`, `Проект/Проектная/Рабочая/Исполнительная`, `P/W/A` | Canonicalized to `PD/RD/ID`; combined `Стадия П/Р` stays ambiguous. |
| Approval labels `Утверждено`, `Согласовано`, `Действует`, `В производство работ`, `Не утверждено`, `Аннулировано`, `Заменён`, English labels, blank and unknown | Canonicalized to `APPROVED`, `FOR_CONSTRUCTION`, `DRAFT`, `CANCELLED`, `SUPERSEDED`, or `UNKNOWN`; blank/unknown is separately warned. |
| Section aliases `OV/ОВ/ОВиК/ОВК`, mixed Latin/Cyrillic, `IOS4/ИОС4/ИОС 4.1`, catalog tags | Canonicalized where a catalog token is recognized; unrecognized prose is retained as input. |
| Unknown registry headings, including `Статус документа`, `Стадия разработки`, `Код раздела проекта` | Preserved as header diagnostics in package report and CLI warning even when a compatibility alias still supplies the field value. |

No registry source is synthesized when absent. Missing rows/files and unsafe registry paths remain package warnings and affect package status according to the existing rules.
No image, model, or package dependency changes were needed; Docker files and the protected delivery scripts remain untouched.

## Final validation

- Full suite after all code changes: **1137 passed, 1 skipped, 145 subtests passed**, 604 warnings, in 256.22 s. The final serialization regression test passed: **1 passed, 50 deselected** in 1.00 s. Synthetic host-import/batch-package field parity and targeted approval-policy tests also passed; the full suite includes them.
- `par_check.py` confirms staged CLI findings: TYU 13 cards, exact `deploy/demo/expected/tyu.json` tuple set; host import has 14 and one host-only `/012`. ALT79B host and staged CLI both have 0 findings. The current TYU result was repeated after the final serialization change: both runs SHA-256 `0df910a94d222ef18af2dab83d4de1a796b62466b69bd74d01357972d4cce538`, 170 checks, 12 candidates + 1 suggestion.
- With registries that omit approval status, final CLI runs completed successfully: NOV 140 checks / 0 candidates / 2 suggestions / 46 unverified files (SHA-256 `67bf118e34548e65355241f02307c602d174e0ba1479d5b72ceca04e1a4f1a44`); TYU 170 / 0 / 14 / 57 (SHA-256 `cfbe48d363acd99ae384a1e30a5ba8ecca97e105dd467f5652f143e74cc67126`); ALT79B 192 / 0 / 48 / 78 (SHA-256 `0d59ada5796723eca1df9b22a233180c56fa31496ef1938213144840e5a6b700`). Every suggestion had `reason_code=approval_unverified`, `violation_claimed=false`; no NOV candidate cards were emitted.
- Approved-registry byte gate against a detached worktree at the exact branch base `f3568e4`, same host venv and input paths: NOV base/current both SHA-256 `0dce34b3106d26ced22310fbbb156d43c92a6cf085d4b065ec4d4636e9baf7e9` (140 checks, 0 findings); ALT79B base/current both `8a1c71a3adb1793783a906d2cc1daf042d8c1876f3aff3c99eadc5380c75ca92` (192 checks, 5 candidates + 34 suggestions). **Both same-host pre-branch byte gates pass.**
- The archived E1 Docker result hashes are not byte-identical to the same-host pre-branch output: NOV differs only by four empty `package` diagnostics (`approval_unverified_files`, `invalid_registry_paths`, `registry_warnings`, `unregistered_files`); ALT79B has those plus two `LOW_QUALITY / PDF_REPAIRED_DURING_OPEN` `file_statuses`. The same-host baseline and final branch have identical bytes, so these Docker artifacts are not used as the before-branch gate. The report does not infer why the image emitted a different package shape.
- No model, package, Docker, compose, or protected delivery changes were needed. `par_check.py` is diagnostic and uses no organizer labels to produce findings.

## Limits and risks

- Hidden-set recall, precision, and suggestion correctness were not measured. No hidden evaluation data or organizer gold labels were used for inference; approval-unverified suggestions remain assumptions for inspector review.
- We did not isolate the individual contribution of `OV` normalization versus stage/registry fixes with an ablation run.
- The submission schema has no dedicated `SUGGESTION` value for `violation_label`: compatibility output may say `VIOLATION_PRESENT` while `evidence_tier=SUGGESTION`, `reason_code=approval_unverified`, and `violation_claimed=false`. Consumers must use tier/claim fields as well as the label.
