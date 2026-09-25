# CASE10 V3 Evaluation

Minimal JSON-based evaluation harness for the official V3 metrics.

Run:

```powershell
python -m evaluation.run_evaluation --gold path\to\gold.json --predictions path\to\predictions.json --output metrics.json
```

Inputs are JSON objects with optional sections:

- `ocr`: rows with `id` and `text`
- `key_fields`: rows with `id` and `value`
- `document_links`: rows with `id` and `target_document_id`
- `evidence`: rows with `id`, `page`, and normalized `bbox`
- `findings`: rows with `id`, `status` or `is_violation`, and optional `category`
- `total_params`: optional denominator for `coverage` and `abstention_rate`

Reported metrics:

- `ocr_cer`
- `ocr_wer`
- `ocr_character_accuracy`
- `ocr_coverage`
- `key_field_exact_match`
- `document_linking_accuracy`
- `evidence_page_accuracy`
- `evidence_bbox_iou`
- `evidence_localization_accuracy`
- `source_localization_exact_file_page`
- `normalized_value_and_status_accuracy`
- `finding_precision`
- `finding_recall`
- `finding_f1`
- `false_positive_rate`
- `object_level_split_valid`
- `coverage`
- `abstention_rate`
- `per_category_metrics`
- `quality_gates`

Positive predicted finding statuses are `CANDIDATE` and `CONFIRMED_VIOLATION`.
`SUSPICION` is not counted as a positive official matrix finding. Abstention statuses are `MISSING_EVIDENCE`, `NOT_APPLICABLE`, `NOT_COMPARABLE`, `CLARIFICATION_REQUIRED`, `ABSTAIN`, `LOW_QUALITY`, and `SUSPICION`.

## SILVER metrics (separate from the official public-gold metric)

`silver_labels/` holds manually reviewed, single-annotator, second-review-PENDING labels; they are never
merged into the official n=6 metric and are never training-eligible.

- `silver_labels/new_objects_review_pass1.jsonl` and `new_objects_value_review_pass1.jsonl` label the
  comparable evidence groups the pipeline produces on the 6 new SILVER objects (LOS3A, ALT79B, POL17,
  DOO25, OKT103, IZM12). Method, run configuration, metrics and limitations:
  [NEW_OBJECTS_SILVER_REVIEW_PASS1_REPORT.md](NEW_OBJECTS_SILVER_REVIEW_PASS1_REPORT.md).
- `python silver_new_objects_pass1/evaluate_silver.py` recomputes the SILVER metric (it calls
  `evaluate_case10()` on the determinate checks only) into
  `reports/new_objects_silver_pass1/silver_metrics.json`, tagged `metric_tier: SILVER_NEW_OBJECTS_PASS1`.

## Live tagger: scale and determinism (CASE10 E1)

`live_tagger_scale_check.py` measures the live candidate tagger on the real SILVER new objects and checks that
its output is reproducible. Report, numbers and limitations:
[LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md](LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md).

- `python evaluation/live_tagger_scale_check.py timing --object DOO25 --workers 1 12` -- cold tagging time, with the
  plan/scan/merge split and worker CPU seconds (`scan_cpu_seconds`, the load-robust figure on a shared machine).
- `python evaluation/live_tagger_scale_check.py determinism --object LOS3A --code-root <frozen copy>` -- full
  `run_process` in several fresh interpreters (1 vs N workers, idle vs CPU-hog load, 1/2/default torch threads,
  cold/warm result cache) and a SHA-256 comparison of the resulting `submission.json`. Use `--code-root` when other
  jobs edit the working tree (a code change between runs is indistinguishable from non-determinism).
- Entry-point scripts that start the tagger's process pool MUST have an `if __name__ == "__main__":` guard
  (standard `multiprocessing` spawn rule); the tagger refuses to nest pools inside a worker and logs a warning.

## Measurement bench (Phase 10, prompt A)

`evaluation/measurement_bench/` holds the hand-labelled value corpus (frozen, SILVER), the three metrics (extraction accuracy by
family, seeded violations L1/L2, real discrepancies) and the regenerable critical-recall matrix
(`evaluation/reports/critical_recall_matrix.{json,md}`).  Run order and commands: `evaluation/measurement_bench/README.md`;
findings: `evaluation/MEASUREMENT_BENCH_REPORT.md`.
