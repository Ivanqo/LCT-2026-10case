"""Reproducible production-readiness audit of all 132 CASE10 matrix
parameters.

A parameter counts as PRODUCTION_READY only when a full, tested,
gold-validated `extraction -> evidence -> comparison/status` path exists for
it -- never just because a `Param` row exists in the database. See
CASE10_MATRIX_132_COVERAGE.md for the full methodology and findings this
script's output backs.

Run from the repo root:
    python -m evaluation.matrix_coverage

Writes:
    evaluation/reports/matrix_coverage.json
    CASE10_MATRIX_132_COVERAGE.md
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from app.domain.matrix_unit_classifier import (  # noqa: E402
    classify_unit,
    is_compound_eligible,
    is_enum_class_eligible,
    is_generic_anchor_eligible,
    is_table_count_eligible,
)

PARAMETER_CATALOG_PATH = REPO_ROOT / (
    "case_data/extracted/01_participant_package/ХАКАТОН_УЧАСТНИКАМ_ГОТОВО_К_ПЕРЕДАЧЕ/"
    "02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ/data/parameter_catalog_132.jsonl"
)
PARAMETER_COVERAGE_PATH = REPO_ROOT / (
    "case_data/extracted/02_gold_methodology/hackathon_gold_20260811/"
    "ОРГАНИЗАТОР_ЗАКРЫТЫЙ/data/parameter_coverage_132.jsonl"
)
TESTS_DIR = REPO_ROOT / "api_service" / "tests"
OUTPUT_JSON = REPO_ROOT / "evaluation" / "reports" / "matrix_coverage.json"
OUTPUT_MD = REPO_ROOT / "CASE10_MATRIX_132_COVERAGE.md"

# The only five codes with a tuned, per-code rule pack -- kept as a literal
# copy of `official_rule_packs.SUPPORTED_RULE_CODES` (imported directly, not
# hand-maintained) so this report can never silently drift from what the
# runtime pipeline actually executes.
from app.domain.official_rule_packs import SUPPORTED_RULE_CODES  # noqa: E402

# Measured, not estimated: the result of an actual local run of `run_process`
# against the real official public dataset (OBJ-TYUMENSKAYA-5-GOLD-SEED,
# OBJ-NOVOSLOBODSKAYA) through the venv, both before and after this
# session's changes. See CASE10_MATRIX_132_COVERAGE.md ("Real-data
# verification") for how these were produced and re-verify with
# evaluation.smoke_real_data in Docker. Kept as a literal constant (not
# re-executed by this script) because a live run needs a full DB/dataset
# environment this reporting script intentionally does not depend on.
REAL_DATA_RUN = {
    "measured_at": "2026-09-19 (local venv, throwaway sqlite, no Docker)",
    "objects": {
        "OBJ-TYUMENSKAYA-5-GOLD-SEED": {
            "total_evidence_groups": 137,
            "by_finding_status": {"CLARIFICATION_REQUIRED": 130, "CANDIDATE": 6, "NEGATIVE_VERIFIED": 1},
            "by_model_version": {"official-evidence-baseline-v1": 130, "official-rule-packs-v2": 7},
            "note": "Dominated by CLARIFICATION_REQUIRED: this object's working-stage documents are "
                    "tagged RD_ID_MIXED (no clean RD split), which every non-rule-pack path -- old and "
                    "new -- correctly refuses to guess through rather than silently pick one.",
        },
        "OBJ-NOVOSLOBODSKAYA": {
            "total_evidence_groups": 134,
            "by_finding_status": {"MISSING_EVIDENCE": 21, "NOT_COMPARABLE": 108, "NEGATIVE_VERIFIED": 5},
            "by_model_version": {"official-evidence-baseline-v1": 129, "official-rule-packs-v2": 5},
        },
    },
    "generic_mechanism_yield": {
        "eligible_parameters_per_object": 48,
        "new_comparable_evidence_groups_created": 0,
        "single_stage_real_values_found": {
            "OBJ-TYUMENSKAYA-5-GOLD-SEED": {
                "PZ-001 (Площадь застройки)": "4650.91 m2 -- PD only, matches manual forensic reading of the real ТЭП table",
                "PZ-002 (Общая площадь здания)": "11618.27 m2 -- PD only, matches manual forensic reading",
                "PZ-008 (Высота здания)": "15.814 m -- PD only, matches manual forensic reading",
            },
            "OBJ-NOVOSLOBODSKAYA": {
                "PZ-001 (Площадь застройки)": "1225.8 m2 -- PD only",
                "PZ-002 (Общая площадь здания)": "17140.2 m2 -- PD only (required the qualifier-tolerant "
                                                   "anchor fallback: this project's real label is "
                                                   "\"Площадь жилого здания\", not the catalog's "
                                                   "\"Общая площадь здания\")",
                "PZ-008 (Высота здания)": "77.05 m -- PD only",
            },
        },
        "why_zero_new_findings": (
            "The mechanism correctly finds a real PD-side value for several parameters on both objects "
            "(verified against the real document text, independent of gold), but produces zero new "
            "comparable findings because it never got a matching RD-side value for the SAME parameter "
            "within its page budget -- OBJ-TYUMENSKAYA-5-GOLD-SEED structurally lacks a clean RD stage "
            "(RD_ID_MIXED, see above) and OBJ-NOVOSLOBODSKAYA's RD-stage annotation-candidate localization "
            "for these codes did not surface the matching table page within the scanned budget. It "
            "abstains rather than pairing a PD value with an unrelated RD number, exactly as designed."
        ),
    },
}

# Measured, not estimated: a local run of the same blind-hidden-object check
# `evaluation/smoke_hidden_blind.py` performs over HTTP (OBJ-RECHNIKOV-7-7,
# `include_hidden=True`, `allow_hidden_gold_labels=False` -- hidden labels
# never imported, never seen), run in-process against the venv the same way
# as REAL_DATA_RUN above. Confirms this session's new generic mechanism does
# not push the hidden object's CANDIDATE+SUSPICION rate over that script's
# own gates (<=20 absolute, <=15% of findings).
HIDDEN_BLIND_SMOKE = {
    "measured_at": "2026-09-19 (local venv, throwaway sqlite, no Docker)",
    "object_id": "OBJ-RECHNIKOV-7-7",
    "total_evidence_groups": 133,
    "by_finding_status": {"NOT_COMPARABLE": 128, "CANDIDATE": 1, "NEGATIVE_VERIFIED": 2, "MISSING_EVIDENCE": 2},
    "by_model_version": {"official-evidence-baseline-v1": 130, "official-rule-packs-v2": 3},
    "generic_mechanism_groups": 0,
    "positive_like": 1,
    "candidate_rate": 0.0075,
    "gate_max_candidates": 20,
    "gate_max_candidate_rate": 0.15,
    "gate_passes": True,
}

# Measured, not estimated: this session's three new generic mechanisms
# (ENUM_CLASS, NUMERIC_COMPOUND, NUMERIC_COUNT/table-row-count) run
# in-process against the same two public objects as REAL_DATA_RUN, same
# throwaway-SQLite method, no Docker. Total evidence-group counts and
# finding_status distributions on both public objects are BYTE-IDENTICAL to
# REAL_DATA_RUN above (134/137, same status breakdown) -- zero regression --
# because none of the 32 newly PARTIAL codes found a comparable PD+RD pair
# within budget on these two specific objects either, the same honest
# outcome the original numeric mechanism reported in checkpoint 35. Unlike
# that mechanism, real single-stage hits were also directly confirmed here
# (calling collect_enum_observations/collect_compound_observations/
# collect_table_count_observations directly, bypassing the PD+RD-pairing
# requirement) -- proof the new code paths execute against real fitz page
# snapshots, not just synthetic unit-test fixtures.
PHASE_ABC_SESSION_YIELD = {
    "measured_at": "2026-09-20 (local venv, throwaway sqlite, no Docker)",
    "public_object_totals_unchanged": True,
    "single_stage_real_values_found": {
        "OBJ-NOVOSLOBODSKAYA": {"PZ-021 (Класс энергетической эффективности, ENUM)": "PD-side canonical value 'B' -- matches real page text, PD only"},
        "OBJ-TYUMENSKAYA-5-GOLD-SEED": {
            "PZ-023 (Класс конструктивной пожарной опасности, ENUM)": "PD-side canonical value 'C0' -- matches real page text, PD only",
            "SPZU-037 (Количество и разметка парковочных мест, TABLE_ROW_COUNT)": "PD-side row_count=1 -- table-title match found, single content row, PD only (not independently verified against the source page, unlike the ENUM hits above)",
        },
    },
    "compound_mechanism_real_hits_this_run": 0,
    "why_zero_new_comparable_findings": (
        "Same root cause as the pre-existing numeric mechanism (checkpoint 35): each new mechanism still requires "
        "an independent PD-side AND RD-side hit for the SAME parameter before producing a comparable finding, and "
        "the upstream page-locator signal (learning_annotation keyword tagging) is broad-recall/low-precision by "
        "design, so a matching RD-side page rarely surfaces within the shared page budget on these 2 objects."
    ),
}

# Phase D: honest, blind (no hidden gold in inference), after-the-fact
# scoring of the 4 matrix codes with real organizer gold that this session's
# work could plausibly reach (SPZU-027/036: pre-existing NUMERIC_SIMPLE
# generic mechanism; SPZU-029: this session's new NUMERIC_COMPOUND
# mechanism; SPZU-039: no extractor, unit "--"). Run once against
# OBJ-RECHNIKOV-7-7 (TEST_HIDDEN split) via the real pipeline; gold loaded
# SEPARATELY and ONLY afterward, purely to score -- never fed back into
# extraction, and the result was not used to adjust any extractor logic
# (per the project's explicit no-hardcoding-to-gold rule). All 4 of these
# gold rows are themselves `score_eligible: false` / `GOLD_READY_SECOND_REVIEW`
# in the organizer's own methodology (pending a second expert review, same
# status as the pre-existing Novoslobodskaya PZ-009/KR-055/KR-058 rows
# already used for the public finding_false_positive_rate n=5 measurement in
# checkpoint 37) -- included on the same precedent, not treated as final.
PHASE_D_HIDDEN_VALIDATION = {
    "measured_at": "2026-09-20 (local venv, throwaway sqlite, no Docker, blind hidden import)",
    "object_id": "OBJ-RECHNIKOV-7-7",
    "codes": ["SPZU-027", "SPZU-029", "SPZU-036", "SPZU-039"],
    "result": "ALL FOUR ABSTAINED (NOT_COMPARABLE) -- no PD+RD comparable pair found within budget for any of the four",
    "per_code": {
        "SPZU-027": "gold VIOLATION_PRESENT (area decreased PD->RD); pipeline abstained -- a missed detection (false negative) from a scoring perspective",
        "SPZU-029": "gold NO_VIOLATION (RD expands the spec, not a reduction); pipeline abstained -- a safe true negative, not a wrong answer",
        "SPZU-036": "gold NO_VIOLATION (PD/RD equal); pipeline abstained -- a safe true negative",
        "SPZU-039": "gold NO_VIOLATION (drainage present both stages); pipeline abstained -- expected, this code has no extractor at all (unit '--')",
    },
    "metrics_computed_via_evaluate_case10_on_these_4_checks_only": {
        "finding_precision": None,
        "finding_recall": 0.0,
        "finding_recall_sample_size": 1,
        "finding_recall_confidence_interval_95": [0.0, 0.7935],
        "finding_f1": 0.0,
        "false_positive_rate": 0.0,
        "false_positive_rate_sample_size": 3,
    },
    "forensic_findings_not_used_to_tune_the_extractor": [
        "SPZU-029's real gold pd_value/rd_value are phrased as a POSITION/ROW COUNT of the MAF specification table "
        "(\"N positions\"), not a two-field \"count / complete-set\" pair -- suggesting the parameter's real semantic "
        "may be closer to this session's table-row-count mechanism (Phase C) than the compound mechanism (Phase B) "
        "it was wired to per classify_unit('шт. / компл.') == NUMERIC_COMPOUND. Not reclassified this session: one "
        "hidden, pending-second-review example is not enough evidence to safely move a code between mechanisms "
        "without risking exactly the single-example overfit the project's no-hardcoding-to-gold rule forbids -- "
        "flagged here as an open question for a future session with more evidence.",
        "SPZU-036's real gold pd_value/rd_value carry TWO numbers (fence height AND length: \"h=2.0 м; 174.45 м\") "
        "even though its catalog unit is 'м' (NUMERIC_SIMPLE, single value) -- the PRE-EXISTING (not this session's) "
        "numeric mechanism is therefore also likely structurally unable to capture both fields for this code. Noted, "
        "not fixed this session (out of the four extractors this session's scope covers).",
        "This session's four new generic mechanisms (numeric, enum, compound, table-row-count) never set a "
        "per-instance `location` field in their evidence groups' delta (they group one finding per PARAMETER, not "
        "per structural element, unlike the tuned rule packs) -- `evaluation/exporter.py::evidence_group_to_prediction` "
        "would still leave `location=\"\"` for any of them, which cannot align with a gold check that expects a "
        "specific non-empty location (e.g. SPZU-027's \"SITE\"). This does not change the result above (an abstained "
        "prediction scores identically whether or not it would have aligned), but means a FUTURE comparable finding "
        "from any generic-tier mechanism against a location-scoped gold check would need this addressed first.",
    ],
}

# Cross-stage-localization session: two new, purely additive signals added on
# top of all 4 existing generic-tier mechanisms (see `cross_stage_
# localization.py`'s module docstring for the full design) --
# (1) document-manifest discipline narrowing: once a parameter's PD-stage
# document is resolved, RD/ID candidate pages whose own document shares its
# discipline/dataset_section are ranked ahead of the rest, within the SAME
# PAGES_PER_STAGE=15 cap (never widened); (2) structural table fingerprint:
# among pages actually rendered for a stage, if more than one independently
# parses a value, the one whose column-header vocabulary + column count best
# matches PD's is preferred over an arbitrary first-match pick. Measured, not
# estimated: same in-process venv method as REAL_DATA_RUN, re-run after this
# change. Went further than checkpoints 35/38 by directly diagnosing WHY the
# result is still zero, with concrete per-object/per-code evidence, not just
# re-stating the prior "broad-recall/low-precision keyword tagger" framing.
CROSS_STAGE_LOCALIZATION_SESSION = {
    "measured_at": "2026-09-21 (local venv, throwaway sqlite, no Docker)",
    "signals_added": [
        "document-manifest discipline narrowing (cross_stage_localization.narrow_candidate_fragments)",
        "structural table fingerprint reranking (anchor_search.extract_table_fingerprint / fingerprint_similarity)",
    ],
    "wired_into": [
        "generic_matrix_extraction (numeric)", "generic_enum_extraction", "generic_compound_extraction", "generic_table_row_count",
    ],
    "public_object_totals_unchanged": True,
    "new_comparable_evidence_groups_created": 0,
    "why_still_zero": {
        "OBJ-TYUMENSKAYA-5-GOLD-SEED": (
            "Confirmed by direct inspection: 0/48 numeric-eligible codes (and 0/9 enum, 0/15 compound, 0/8 "
            "table-row-count codes) have even one RD-stage candidate fragment tagged by the upstream annotator "
            "at all -- this object's non-PD documents are all dataset_stage=RD_ID_MIXED, which every stage-"
            "bucketing step in this pipeline (old and new) correctly excludes rather than guessing. No ranking "
            "or fingerprinting signal can act on a candidate pool that is empty to begin with."
        ),
        "OBJ-NOVOSLOBODSKAYA": (
            "DOES have 10 genuine RD-stage documents, and 17/48 numeric-eligible codes DO have at least one "
            "RD-stage candidate fragment (a new finding this session, more precise than checkpoint 35's "
            "framing). But calling collect_generic_observations directly shows only 3/48 codes resolve on ANY "
            "stage at all, none on both PD+RD. Forensically reading the one RD candidate page tagged for "
            "PZ-002 (confidence 0.62) shows it is a real, unrelated document (a \"Стена в грунте\"/wall-in-"
            "ground construction-note-and-materials-bill sheet) that merely shares a few generic words -- a "
            "genuine false positive, not a lower-ranked correct page. Checking all 10 of this object's real "
            "RD-stage files confirms every one is a КР (structural: rebar/wall/shoring) drawing; none is a "
            "ПЗ/ГП-style document that would ever restate an object-level ТЭП scalar like \"Общая площадь "
            "здания\". The gap here is candidate EXISTENCE (no RD document of the right type contains the "
            "value at all), not candidate RANKING among several already-tagged pages -- a within-candidate-"
            "pool reranking signal cannot fix a value that was never written into any RD-stage document."
        ),
    },
    "conclusion": (
        "This sharpens, rather than contradicts, the checkpoint 35/38 diagnosis. Their framing (\"broad-recall/"
        "low-precision keyword tagging\") suggested the right page exists among several tagged candidates but "
        "gets outranked; this session's direct inspection shows the more common failure on these two specific "
        "public objects is that the right page was never tagged at all (Tyumenskaya) or never existed in the "
        "RD-stage document set for this parameter class in the first place (Novoslobodskaya). The signal built "
        "this session is real, tested (27 new unit tests, 317/317 total suite green), purely additive "
        "(byte-identical evidence-group totals on both objects, zero regression), and would help in the "
        "scenario it targets -- multiple tagged candidates exist and one is structurally correct -- but that "
        "scenario essentially does not occur for the codes examined on these two objects. Phase D "
        "(SPZU-027/029/036/039, hidden object, blind) was re-run after this change: finding_recall is still "
        "0.0 (n=1, identical to checkpoint 38) -- all four still abstain, unchanged."
    ),
}

# Sentence-embedding session: closes the literal TZ 9.1 item 2 gap ("Поиск по
# 132 параметрам с использованием регулярных выражений ... и семантических
# якорей. Используется модель Sentence-BERT (all-MiniLM-L6-v2) или её
# совместимые аналоги") -- until this session, every generic-tier mechanism
# was pure string/token matching (anchor_search.py), no embedding model
# anywhere in the pipeline. New module `semantic_similarity.py` adds an
# OPTIONAL layer on top of the existing deterministic anchor search, used in
# two places (see `cross_stage_localization.py`'s module docstring, signal
# 3, and `anchor_search.find_anchor_end_index_for_phrase`'s fallback):
# (a) re-ranks/filters already keyword-tagged RD/ID candidates by cosine
# similarity of the matched row's own text against the parameter's name +
# catalog source hint, on by default (can only narrow a pool a textual
# anchor already corroborated, never invent a match); (b) a semantic
# anchor-location fallback for when no literal/qualifier-tolerant phrase
# match exists at all, OFF by default (see below).
#
# Model choice was NOT the literal all-MiniLM-L6-v2: it is English-trained
# and, measured on real project text, fails to separate two different
# Russian area parameters (0.53 vs 0.49 cosine, a 0.04 margin -- not usable
# as a filter). `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
# -- already this repo's own `rag_service` production embedding model for
# the same Russian document corpus -- separates the same real pair by 0.62
# vs. 0.41, and was adopted as the TZ's explicitly-allowed "compatible
# analogue" instead. Trade-off taken deliberately: ~460MB on disk vs. the
# TZ's informal "~80MB", because a same-size model that cannot tell two
# Russian phrases apart would not close the actual gap. See
# `semantic_similarity.py`'s module docstring for the full comparison.
SEMANTIC_SIMILARITY_SESSION = {
    "measured_at": "2026-09-21 (local venv, throwaway sqlite, no Docker)",
    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (sentence-transformers==2.6.1, already used by rag_service)",
    "model_choice_rationale": (
        "The literal TZ model (all-MiniLM-L6-v2, English-trained) measured 0.53 vs 0.49 cosine similarity for a "
        "genuine Russian parameter match vs. a different-but-lexically-related Russian parameter on real project "
        "text -- a 0.04 margin, not usable as a filter. The multilingual variant already in production in this "
        "repo's rag_service separated the same real pair 0.62 vs 0.41-0.49. Adopted as the TZ's own allowed "
        "'compatible analogue' instead of the literal model, trading ~460MB disk for a model that actually "
        "discriminates the language the documents are written in."
    ),
    "wired_into": [
        "cross_stage_localization.pick_best_candidate (re-rank/filter already-tagged candidates, ON by default)",
        "anchor_search.find_anchor_end_index_for_phrase (semantic anchor-location fallback, OFF by default)",
    ],
    "anchor_fallback_disabled_by_default_why": (
        "Real regression testing (this session's own new unit tests, not a hypothetical) caught the fallback "
        "confusing two DIFFERENT, unrelated same-domain ТЭП tables' own 'Площадь' rows -- a building-area "
        "parameter's anchor search matched a site-area table's row purely on embedding similarity, reviving a "
        "collision test_case10_generic_extraction.py's existing deterministic-matcher tests were specifically "
        "written to prevent. This is a genuine precision cost of comparing short, vocabulary-overlapping label "
        "text, not a threshold-tuning artifact -- raising SEMANTIC_MIN_SIMILARITY does not fix it because the "
        "genuine near-miss score (site-area row vs. building-area query) and the genuine match score occupy "
        "overlapping ranges. Re-ranking (a) has no equivalent failure mode because it only ever narrows a pool a "
        "TEXTUAL anchor already independently corroborated -- it cannot invent a match from nothing."
    ),
    "public_object_totals_unchanged": True,
    "new_comparable_evidence_groups_created": 0,
    "evidence_group_level_diff": "status_counts and source_counts dicts verified byte-identical per object between the semantic layer OFF and ON (default), not just total counts -- see CASE10_MATRIX_132_COVERAGE.md.",
    "semantic_scoring_activity_confirmed_live": {
        "method": "Instrumented pick_best_candidate during a real collect_*_observations pass over OBJ-NOVOSLOBODSKAYA's real annotation/document data",
        "candidate_evaluations": 10,
        "evaluations_with_a_computed_score": 10,
        "score_range_observed": [0.269, 0.729],
        "evaluations_below_the_0.45_floor": 1,
        "conclusion": "The mechanism is genuinely active and producing plausible scores on real data (matches the range measured directly on the checkpoint 40 example elsewhere in this doc), not silently dead code -- it simply had no comparable-group outcome to change on these two specific objects, consistent with checkpoint 40's candidate-existence diagnosis.",
    },
    "why_still_zero_new_comparable_groups": (
        "Checkpoint 40 already established that on both public objects the bottleneck is candidate EXISTENCE, not "
        "candidate RANKING/relevance -- OBJ-TYUMENSKAYA-5-GOLD-SEED has zero RD-stage candidates for any "
        "generic-tier code at all (RD_ID_MIXED), and OBJ-NOVOSLOBODSKAYA's real RD-stage document set for these "
        "codes never contains the value in the first place (all 10 RD documents are КР drawings, not "
        "ПЗ/ГП-style). A re-ranking/filtering signal over an existing candidate pool cannot, by construction, fix "
        "an empty or wrong-type pool -- exactly the scenario this session's real-data run confirms."
    ),
    "latency": {
        "warm_embedding_ms_per_text_batched": 1.4,
        "warm_embedding_ms_single_call": 47,
        "budget_per_TZ_11": "500ms +/- 100ms per parameter",
        "one_time_model_load_seconds_offline_cache_only": 11.6,
        "note": (
            "One-time cold load happens once per process lifetime (lazy singleton), not per parameter -- "
            "amortized cost after warmup is 1-50ms, far under budget even without additional caching. This "
            "session's own sandbox intermittently added 30-140s to that one-time load from retried HTTPS HEAD "
            "requests against a self-signed TLS interception on this specific machine (see CASE10_MATRIX_132_"
            "COVERAGE.md); a production container should set HF_HUB_OFFLINE=1 with the model pre-baked into the "
            "image at build time (added to api_service/Dockerfile this session) to avoid any network dependency "
            "at runtime."
        ),
    },
    "tests_added": "test_case10_semantic_similarity.py (real model, real forensic text), test_case10_anchor_search.py, plus new cases in test_case10_cross_stage_localization.py (mocked scores, including an end-to-end synthetic reproduction of the checkpoint-40 finding). Full suite: 379/379 (351 baseline + 28 new).",
}

# Live candidate tagger session (TZ 9.1 item 2, the literal "regex_pattern +
# semantic anchors as a first pass over raw text" requirement): closes the
# architectural gap project_case10_new_objects_no_tagger_gap found -- every
# extraction mechanism in this pipeline built its candidate pool exclusively
# from organizer-supplied learning_annotation training data (3 official
# objects only), so any other object structurally produced 0 SourceFragment
# rows and 0 comparable EvidenceGroups regardless of document richness. New
# module api_service/app/domain/live_candidate_tagger.py scans a document's
# own pages for each matrix parameter's anchor phrase (anchor_search.py,
# literal/qualifier-tolerant matching), semantically filters weak matches
# (semantic_similarity.py), and writes SourceFragment rows with a new
# source_system="live_tagger" that both official_evidence.inference_
# annotation() and official_rule_packs.is_inference_locator() now accept
# alongside (never instead of) organizer data.
LIVE_CANDIDATE_TAGGER_SESSION = {
    "measured_at": "2026-09-22 (local venv, throwaway sqlite, no Docker)",
    "new_module": "api_service/app/domain/live_candidate_tagger.py",
    "wired_into": [
        "official_evidence.inference_annotation (widened source_system filter)",
        "official_rule_packs.is_inference_locator (widened source_system filter, identical shape)",
        "official_evidence.create_official_evidence_groups (calls tag_live_candidates against the FULL matrix catalog before building by_code, not just the run's own possibly-narrowed param scope)",
    ],
    "real_bugs_found_and_fixed_along_the_way": [
        {
            "bug": "dataset_sources._original_page_snapshots corrected a rotated page's word BBOXES for /Rotate but never re-sorted the word ORDER, which fitz's get_text('words', sort=True) computes in the raw, unrotated content-stream frame -- on a 90/270deg page (~32% of real pages per project_case10_rotation_fix) this silently scrambles reading order for every literal anchor-phrase matcher in the whole generic-tier family, not just the new tagger.",
            "evidence": "Real LOS3A RD page (19-0322-OK-1_N-1-AR2..., p.11, rotation=90): 0/132 catalog anchors matched pre-fix on this exact page, 5/132 post-fix (this bug alone), 9/132 after the two anchor_search.py fixes below too.",
            "fix": "Re-cluster words by their already-corrected visible bbox (anchor_search.cluster_rows, same primitive used elsewhere for row fingerprinting) whenever rotation != 0; the far more common 0deg case (already covered by existing tests/fixtures) is untouched.",
        },
        {
            "bug": "28/132 catalog parameter_name values end in a disambiguating parenthesized qualifier (e.g. PZ-004 'Строительный объем (Общий)') that a real document's own table label never repeats verbatim (real LOS3A RD row: just 'Строительный объём').",
            "fix": "anchor_search.anchor_word_variants also tries the phrase with any trailing '(...)' qualifier stripped (same technique derive_title_candidates already used for source-hint text, widened past its 12-char cap).",
        },
        {
            "bug": "find_anchor_end_word_index returned the FIRST literal match by reading order, not the tightest one. Real LOS3A PD page (01-01-00-02-PZ.pdf, p.14): the 2-word leading-word-dropped variant ['площадь','здания'] matched an EARLIER, gap-2 coincidence inside unrelated flowing prose about a different sub-quantity ('...площадь подземной части здания...') before ever reaching the LATER, genuine, gap-0 table label ('4. Площадь здания ... 25036,27'). This silently returned an unrelated row's number (1061.49) for PZ-002.",
            "fix": "Scan every literal match on the page and keep the one with the smallest total inter-word gap, tie-broken by earliest position -- strictly more specific than reading order alone, so a page with only one match is unaffected.",
            "verified": "Same real PD page 14, PZ-002 ('Общая площадь здания'): 25036.27 post-fix, matching the manually-verified real value exactly (was 1061.49 pre-fix).",
        },
        {
            "bug": "checkpoint 41's semantic anchor-location fallback (still OFF by default) confuses a site-area row for a building-area query purely on embedding similarity (see SEMANTIC_SIMILARITY_SESSION above) -- shipped disabled, not re-fixed, at the time.",
            "fix": "Added a lexical scope-word gate (anchor_search._scope_compatible: building vs. site/territory nouns) that rejects a row whose own scope word contradicts the anchor's BEFORE the embedding ever ranks it -- real regression test (test_case10_anchor_search.py's SemanticAnchorFallbackScopeGateTests) reproduces the exact checkpoint-41 collision fixture and proves it no longer wins even when the embedding score is mocked to prefer it. Still shipped OFF by default (per checkpoint 41's own reasoning: this closes ONE known collision class, not a blanket guarantee against every future one) -- an explicit scoping choice per the user's 'try to fix, not just re-enable' brief, not a silent regression re-hide.",
        },
    ],
    "budget_sizing_phase_c": {
        "dev_machine": "8 physical / 12 logical core i5-12450H (not the organizer-confirmed 24-physical-core/640GB/GPU grading server -- see gpu.py/checkpoint 42)",
        "measured_single_threaded_throughput": "~10-12 pages/sec on a dense, drawing-sheet-heavy real RD document (full-page word extraction + all 132 anchor phrases); ~160 pages/sec on a flowing-text PD document. Anchor matching itself added no measurable overhead over bare text extraction.",
        "settings": {
            "CASE10_LIVE_TAGGER_MAX_DOCUMENTS": 500, "CASE10_LIVE_TAGGER_MAX_PAGES_TOTAL": 6000,
            "CASE10_LIVE_TAGGER_MAX_PAGES_PER_DOCUMENT": 220, "CASE10_LIVE_TAGGER_MAX_SECONDS": 600,
        },
        "superseded_by": "Phase 10 E1 (evaluation/LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md): the wall-clock MAX_SECONDS budget was removed (output depended on machine speed); caps are now deterministic documents/pages (3000/30000/220 per document), the scan runs on a process pool with a SHA-256-keyed disk cache, and non-PDF files no longer consume the document cap. Historical settings above kept as the record of this session.",
        "honest_limitation": "Sizing is extrapolated from real dev-machine measurement, scaled conservatively (no credit taken for the grading server's extra cores) -- this session did NOT implement cross-document parallelism (a deliberate scope decision, not an oversight); the tagging pass is a ONE-TIME cost per document (idempotent, see live_tagger_scanned_at), so it does not compete with the TZ 11 '<=2 minutes per 132-parameter comparison' budget for any run after a document's first.",
    },
    "real_data_run_public_objects": {
        "method": "in-process venv, throwaway sqlite, no Docker/HTTP -- import_official_dataset with include_annotations True/False, create_process/run_process, same method as checkpoints 35-41",
        "note_on_code_version": "Run before the tightest-match-gap and discipline-hard-filter fixes above; those two fixes only affect PD-side VALUE-SELECTION precision on an already-open document, not RD-stage candidate EXISTENCE, which is the actual established bottleneck on these two objects (confirmed 3 independent ways: checkpoints 40, 41, and this session's own control run below) -- a re-run is very unlikely to change the comparable-group outcome, but was not re-verified with the final code state; flagged honestly rather than silently assumed.",
        "with_organizer_tags_plus_live_tagger": {
            "OBJ-NOVOSLOBODSKAYA": {"comparable_groups": 5, "live_fragments": 25, "live_pages": 25, "organizer_tagged_pages": 1795, "overlap_pages": 24},
            "OBJ-TYUMENSKAYA-5-GOLD-SEED": {"comparable_groups": 7, "live_fragments": 16, "live_pages": 13, "organizer_tagged_pages": 2015, "overlap_pages": 12},
        },
        "control_organizer_tags_live_tagger_disabled": {
            "OBJ-NOVOSLOBODSKAYA": {"comparable_groups": 5, "status_counts_byte_identical_to_above": True},
            "OBJ-TYUMENSKAYA-5-GOLD-SEED": {"comparable_groups": 7, "status_counts_byte_identical_to_above": True},
            "conclusion": "The 5/7 comparable groups are entirely PRE-EXISTING official-rule-packs-v2 tier behavior with organizer tags present, unaffected by the live tagger -- byte-identical with it on vs. off. The live tagger contributes ZERO net new comparable groups on these two specific public objects. This is NOT a tagger failure: it reconfirms, a third independent way, checkpoints 40/41's diagnosis that candidate EXISTENCE (not ranking/tagging mechanism) is the bottleneck here -- Tyumenskaya has zero real RD-stage documents at all (RD_ID_MIXED only) and Novoslobodskaya's RD set is real but the wrong document type (KR drawings, never a TEP-style restatement).",
        },
        "live_tagger_only_organizer_tags_not_imported": {
            "OBJ-NOVOSLOBODSKAYA": {"comparable_groups": 0, "live_fragments": 25, "organizer_tagged_pages": 0},
            "OBJ-TYUMENSKAYA-5-GOLD-SEED": {"comparable_groups": 0, "live_fragments": 16, "organizer_tagged_pages": 0},
            "conclusion": "Same live_fragments count as the with-tags run (deterministic, does not depend on organizer data being present) -- the 92-96% overlap between the tagger's own found pages and the organizer's own tagged pages (24/25 and 12/13) is an implicit silver validation of tagger quality against data with a known expected answer, without hardcoding that answer into the tagger. 0 comparable groups here is the honest, structurally-expected outcome (same document-availability constraint as above), not a regression.",
        },
    },
    "real_data_run_new_objects": {
        "method": "in-process venv, throwaway sqlite, no Docker/HTTP -- manifest built from the multi_object_annotation_20260817 SILVER registry, SHA-256 RE-VERIFIED against the actual extracted bytes at manifest-build time (not trusted from the registry), original_document_bytes monkeypatched to read from the verified local extraction root, create_process/run_process unmodified. All 6 objects previously measured at EXACTLY 0 SourceFragment / 0 comparable EvidenceGroup regardless of document richness (project_case10_new_objects_no_tagger_gap) -- this is the direct, decisive test of whether this session's work closes that gap.",
        "results": {
            "LOS3A": {"live_fragments": 451, "comparable_groups": 10},
            "ALT79B": {"live_fragments": 381, "comparable_groups": 3},
            "POL17": {"live_fragments": 319, "comparable_groups": 5},
            "OKT103": {"live_fragments": 271, "comparable_groups": 0},
            "IZM12": {"live_fragments": 216, "comparable_groups": 0},
            "DOO25": {"live_fragments": 119, "comparable_groups": 1},
        },
        "total_comparable_groups_created": 19,
        "OKT103_IZM12_honest_zero": "Both structurally mirror OBJ-NOVOSLOBODSKAYA's pattern (candidate_coverage.py: narrow, single-discipline RD set, mostly KR/KZh) per NEW_OBJECTS_ANALYSE.md section 3 -- real candidates were found (216/271 live_fragments) but did not pair into full PD+RD comparable groups, an honest structural non-result, not a tagger bug.",
        "DOO25_note": "Lowest fragment count despite being the largest object (1770 documents) because 71.7% of its PDFs have no text layer (project_case10_ocr_fallback_hardening/case10_ocr_tesseract_quality_tuning) -- this tagger only scans the text layer by design (forced OCR is deliberately not used for the tagging pass, see Phase C budget reasoning); an honest, documented scope limit, not a silent gap.",
    },
    "los3a_forensic_deep_dive_pz001_pz002_pz004": {
        "why": "The task's own success criterion named these 3 specific codes and this specific document pair (RD 19-0322-OK-1_N-1-AR2..., p.11; PD 01-01-00-02-PZ.pdf, p.14) -- verified forensically rather than accepting the 10-comparable headline number at face value, per this project's standing forensic-audit-before-claiming-success rule.",
        "PZ-001": {
            "comparable": False,
            "finding": "The live tagger found candidates on BOTH the named RD page (p.11, correct row) AND a genuinely correct PD page (p.14 of the named PZ document, value 1076.49) among its 10 tagged fragments for this code -- but candidate-RANKING (cross_stage_localization.pick_best_candidate, pre-existing infrastructure, not modified this session) selected a DIFFERENT PD candidate instead: an empty, unfilled certificate/template form field on an unrelated document ('Общая площадь участка застройки – Га', no number present). The generic mechanism correctly abstained (no value on the selected PD candidate) rather than fabricate one -- an honest MISSING_EVIDENCE-adjacent outcome, not a wrong value. A genuinely available BETTER candidate existed but was not the one ranking chose; a real, now-newly-exposed (candidates finally exist to rank at all) precision gap in the pre-existing ranking machinery, left open for a future session.",
        },
        "PZ-002": {
            "comparable": True,
            "RD_value": "25036.27 (page 11 of the exact named RD document -- forensically verified correct, matches NEW_OBJECTS_ANALYSE.md section 6's manual reading exactly)",
            "PD_value": "16867.90 (WRONG -- from a different PD document, a fire-safety ПБ.1.pdf page 10, matching a different, related normative metric 'площадь помещений здания по СП 118.13330.2012 прил. Г.5', not 'Общая площадь здания по СП 54.13330.2016' the catalog actually wants)",
            "root_cause": "A semantic near-miss the leading-word-drop anchor fallback cannot distinguish (both share 'площадь'+'здания' within gap tolerance) compounded by candidate ranking preferring this document's page over the correct PZ-document page-14 candidate that was also tagged. Not fixed this session -- scoped out as a deeper cross-document semantic-disambiguation problem, not a quick patch.",
        },
        "PZ-004": {
            "comparable": True,
            "RD_value": "88264.00 (a different, also-genuinely-correct RD document/page than the one named -- AR3 Техническое пространство p.6, not AR2 p.11 -- but the exact same named AR2/p.11 page independently verified to also contain this value)",
            "PD_value": "0.5120 (WRONG -- the documented tied-zero-gap collision between the table's own descriptive preamble sentence and its real numbered row, see the tightest-match fix note above; both are equally 'tight' so the fix's tie-break defaults to the earlier, wrong one)",
        },
        "honest_summary": "Real, substantial, forensically-verified recall improvement (0 -> 10 comparable groups on LOS3A alone, 19 across all 6 new objects) with an honestly-documented remaining precision gap: none of the 3 named codes yet produces a fully-correct comparable PAIR end to end, though 2/3 (PZ-002, PZ-004) DO have a forensically-correct RD side, and all 3 had a genuinely correct candidate tagged somewhere in their pool. This is NOT the 'validation gives 0 on LOS3A' failure scenario the task explicitly warned about -- it is a real, non-zero, partially-precise result with clearly-identified, pre-existing (not newly-introduced) follow-up work in candidate ranking and cross-document semantic disambiguation.",
    },
    "tests_added": "29 new (test_case10_live_candidate_tagger.py: 22; test_case10_anchor_search.py: 4 scope-gate; test_case10_generic_extraction.py: 3 qualifier-strip/tightest-match). Full suite: 422/422 (393 baseline + 29 new).",
    "what_is_next": "Candidate-ranking precision (why pick_best_candidate chose the empty PZ-001 template over the correct page; PZ-002's cross-document semantic near-miss) and PZ-004-style tied-gap anchor/table-row disambiguation are the sharpest, now-concretely-reproducible next targets -- previously invisible because candidates essentially did not exist to rank at all outside the 3 official objects. Optional, explicitly scoped out this session: populating Param.regex_pattern (still None for all 132 catalog params) as an additional candidate signal alongside anchor phrases.",
}

# Follow-up session: three targeted, independently testable ranking/
# disambiguation fixes for the exact PZ-001/PZ-002/PZ-004 gaps
# LIVE_CANDIDATE_TAGGER_SESSION left open above -- each re-opened forensically
# against the real LOS3A PDF bytes on the local E:\ extraction root (not a
# re-paraphrase of the prior session's memory), then re-verified end-to-end
# through the real (unmodified beyond the fix itself) `find_anchor_numeric_
# value`/`pick_best_candidate`/`resolve_stage_round`/`live_candidate_tagger.
# tag_live_candidates` + `collect_generic_observations` pipeline, throwaway
# sqlite, no Docker. See CASE10_TZ_COMPLIANCE_AUDIT.md's "Часть 0" 2026-09-23
# update for the full narrative this dict backs.
LOS3A_FORENSIC_FIXES_SESSION = {
    "measured_at": "2026-09-23 (local venv, throwaway sqlite, no Docker, real bytes from local E:\\ extraction root)",
    "pz004_row_item_prefix_tiebreak": {
        "fix": "anchor_search.find_anchor_end_word_index now breaks a tied (equal total_gap) match by whether a real table row's own numeric item prefix (e.g. '5.') immediately precedes the match -- a pure tie-breaker on top of, never instead of, the existing gap comparison.",
        "real_bug_reproduced": "Real PD page 14 (1. 01-01-00-02-ПЗ.pdf) has two exact gap-0 matches for 'Строительный объем': the section's own descriptive preamble sentence and the real numbered row ('5. Строительный объем в т.ч. 88264,00 ...'). Pre-fix, direct find_anchor_numeric_value call on the real page returned 0.5120 (an unrelated row's value, picked up from the preamble match's lookahead window) -- confirmed by re-running the exact assertion against pre-fix code.",
        "post_fix_direct_call": "find_anchor_numeric_value on the same real page now returns 88264.00.",
        "post_fix_full_pipeline": "Real tag_live_candidates + collect_generic_observations run across all 150 real LOS3A PD/RD PDFs (7026 pages, discipline-classified from filename tokens): PD=88264.00, RD=88264.00 -- comparable AND correct, unconditionally (no catalog dependency).",
        "status": "CONFIRMED_FIXED_END_TO_END",
    },
    "pz002_normative_reference_mismatch": {
        "fix": "anchor_search.extract_normative_references/expected_normative_references (regex for 'СП/СНиП/ГОСТ NN.NNNNN' near the found value, compared against Param.sp_reference/gost_reference/fz_reference) feeds cross_stage_localization.StageCandidate.normative_mismatch, which demotes (never drops) a candidate whose nearby text cites a different normative document than the parameter's own catalog-declared one.",
        "real_bug_reproduced": "The same real document (9. 01-09-00-01-15-ПБ.1.pdf) legitimately anchor-matches 'Общая площадь здания' twice: page 9's real ТЭП row ('Площадь здания (по СП 54.13330.2016, прил. А.1.2)' = 25036.27) and page 10's related-but-distinct quantity ('Площадь помещений здания (по СП 118.13330.2012, прил. Г.5)' = 16867.90).",
        "post_fix_full_pipeline_without_catalog_sp_reference": "Today's actual catalog state (Param.sp_reference never populated by official_dataset.py's importer for any of the 132 parameters): PD=16867.90 -- the bug reproduces byte-for-byte, unfixed in production today.",
        "post_fix_full_pipeline_with_catalog_sp_reference": "With Param.sp_reference='СП 54.13330.2016' set (simulating a populated catalog, NOT applied to the real catalog this session -- see note below): PD=25036.27, RD=25036.27 -- comparable AND correct.",
        "why_not_populated_this_session": "The real parameter_catalog_132.jsonl has no sp_reference field at all; authoring 132 correct normative citations from scratch is a separate, much larger catalog-enrichment task outside this session's 3-fix scope, and would risk being wrong without an authoritative source -- left as real, tested, dormant infrastructure, not silently claimed as a live fix.",
        "status": "REAL_FIX_VERIFIED_DORMANT_PENDING_CATALOG_DATA",
    },
    "pz001_empty_candidate_ranking": {
        "fix": "cross_stage_localization.StageCandidate.is_empty_value (a found value of exactly zero, a typical unfilled template/certificate placeholder) is ranked below a non-empty candidate in pick_best_candidate, both in the reference-fingerprint branch (as the dominant signal, ahead of fingerprint/semantic score) and the no-reference (PD round 1) branch -- never a hard filter, so an empty candidate still wins when it is the only one.",
        "real_document_relocation": "A targeted full-text search of every real LOS3A PD/RD/ID PDF for the forensic report's own quoted phrase ('Общая площадь участка застройки – Га') did not locate the original empty-template/certificate document this session -- the fix is verified at the unit/StageCandidate level against the documented structural pattern (a generalized, non-LOS3A-specific fixture), not re-confirmed end-to-end against the original real document.",
        "new_orthogonal_finding": "The real full-pipeline LOS3A run surfaced a DIFFERENT, previously undocumented bug on the same PD page 14: a multi-column reflow where the real value (1076.49) is emitted BEFORE its own label ('2. Площадь застройки') in fitz's own reading order, while the first number found AFTER the label is an unrelated parenthetical threshold ('4,5' м, a cantilever-height allowance) -- producing PD=4.5, still wrong, for a reason unrelated to case 1's empty-candidate collision. is_empty_value correctly does not fire here (4.5 is non-zero), confirming the fix is precisely scoped rather than accidentally masking this different problem. Left unfixed -- outside this session's 3-case scope.",
        "status": "INFRASTRUCTURE_BUILT_AND_UNIT_TESTED_NOT_RECONFIRMED_END_TO_END",
    },
    "honest_summary": "Measurable improvement from the documented 0/3: PZ-004 is confirmed fixed end to end in the real pipeline today, unconditionally. PZ-002's fix is real and end-to-end verified against the actual forensic collision on real PDF bytes, but is dormant until Param.sp_reference is populated in the catalog (confirmed: today's actual catalog still reproduces the original bug byte-for-byte). PZ-001's fix is real, tested infrastructure for the documented empty-candidate pattern, but the original document was not relocated this session, and a full pipeline run surfaced a second, different, still-open bug on the same page.",
    "tests_added": "4 in test_case10_anchor_search.py (row-item-prefix tie-break, normative-reference extraction), 7 in test_case10_generic_extraction.py (real LOS3A PD-page-14 fixture, is_empty_value/normative_mismatch unit tests), 8 in test_case10_cross_stage_localization.py (existence-signal ranking tests, real end-to-end PZ-002 reproduction via resolve_stage_round with the real PB.1.pdf page 9/10 excerpts). Full suite: 470/470 (run from the repo root with PYTHONPATH covering both the repo root and api_service/ -- see case10_checkpoint42_gpu_docker memory for this environment quirk).",
    "what_is_next": "Populate Param.sp_reference/gost_reference for the 132-parameter catalog from an authoritative normative source (activates the PZ-002 fix and any other SP/GOST-citation collision in the same family) -- explicitly NOT done this session to avoid guessing normative citations without a source. Re-locate PZ-001's original empty-certificate document (or accept the newly-found value-before-label reflow bug as PZ-001's actual current blocker and fix that instead -- a distinct, now-concretely-reproducible target: real LOS3A PD page 14, 'Площадь застройки', value 1076.49 emitted before its own label in fitz reading order).",
}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _has_source(value: object) -> bool:
    return bool(str(value or "").strip())


def _review_priority(criticality: str) -> str:
    return "HIGH" if "критическ" in str(criticality or "").lower() else "MEDIUM"


def _load_test_corpus() -> dict[str, str]:
    corpus = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        corpus[path.name] = path.read_text(encoding="utf-8")
    return corpus


def _tests_referencing(code: str, corpus: dict[str, str]) -> list[str]:
    return [name for name, text in corpus.items() if code in text]


def build_inventory() -> dict:
    catalog_rows = _read_jsonl(PARAMETER_CATALOG_PATH)
    if len(catalog_rows) != 132:
        raise SystemExit(f"Expected 132 catalog rows, got {len(catalog_rows)}")
    coverage_rows = {row["parameter_code"]: row for row in _read_jsonl(PARAMETER_COVERAGE_PATH)}
    test_corpus = _load_test_corpus()

    parameters = []
    for row in catalog_rows:
        code = row["parameter_code"]
        unit = row.get("unit")
        data_type_class = classify_unit(unit)
        is_rule_pack = code in SUPPORTED_RULE_CODES
        is_generic_eligible = (not is_rule_pack) and is_generic_anchor_eligible(
            code=code, unit=unit, parameter_name=row.get("parameter_name"), excluded_codes=frozenset(SUPPORTED_RULE_CODES),
        )
        is_enum_eligible = (not is_rule_pack) and is_enum_class_eligible(
            code=code, unit=unit, parameter_name=row.get("parameter_name"), excluded_codes=frozenset(SUPPORTED_RULE_CODES),
        )
        is_compound_eligible_flag = (not is_rule_pack) and is_compound_eligible(
            code=code, unit=unit, parameter_name=row.get("parameter_name"), excluded_codes=frozenset(SUPPORTED_RULE_CODES),
        )
        is_table_count_eligible_flag = (not is_rule_pack) and is_table_count_eligible(
            code=code, unit=unit, excluded_codes=frozenset(SUPPORTED_RULE_CODES),
        )
        coverage_row = coverage_rows.get(code) or {}
        gold_check_count = int(coverage_row.get("gold_check_count") or 0)
        has_real_fixture = gold_check_count > 0
        tests = _tests_referencing(code, test_corpus)
        required_stages = [
            stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id"))
            if _has_source(row.get(field))
        ]

        if is_rule_pack:
            readiness_class = "PRODUCTION_READY"
            extractor = "OFFICIAL_RULE_PACK"
            comparison_exists = True
            negative_path_exists = True
            localization = "VALUE_GROUNDED"
        elif is_generic_eligible:
            readiness_class = "PARTIAL"
            extractor = "GENERIC_ANCHOR_TABLE"
            comparison_exists = True
            negative_path_exists = True
            localization = "VALUE_GROUNDED"
        elif is_enum_eligible:
            readiness_class = "PARTIAL"
            extractor = "GENERIC_ANCHOR_ENUM"
            comparison_exists = True
            negative_path_exists = True
            localization = "VALUE_GROUNDED"
        elif is_compound_eligible_flag:
            readiness_class = "PARTIAL"
            extractor = "GENERIC_ANCHOR_COMPOUND"
            comparison_exists = True
            negative_path_exists = True
            localization = "VALUE_GROUNDED"
        elif is_table_count_eligible_flag:
            readiness_class = "PARTIAL"
            extractor = "GENERIC_TABLE_ROW_COUNT"
            comparison_exists = True
            negative_path_exists = True
            localization = "VALUE_GROUNDED"
        else:
            readiness_class = "CONFIG_ONLY"
            extractor = "NONE"
            comparison_exists = False
            negative_path_exists = False
            localization = "CONTEXT_ONLY"

        parameters.append({
            "parameter_id": row["parameter_id"],
            "code": code,
            "section": row.get("pd_section"),
            "parameter_name": row.get("parameter_name"),
            "unit": unit,
            "data_type_class": data_type_class,
            "criticality": row.get("criticality"),
            "review_priority": _review_priority(row.get("criticality")),
            "required_stages": required_stages,
            "extractor": extractor,
            "localization": localization,
            "comparison_exists": comparison_exists,
            "negative_path_exists": negative_path_exists,
            "tests": tests,
            "has_tests": bool(tests),
            "real_fixture": {
                "has_gold_examples": has_real_fixture,
                "gold_check_count": gold_check_count,
                "positive_count": int(coverage_row.get("positive_count") or 0),
                "negative_count": int(coverage_row.get("negative_count") or 0),
                "coverage_status": coverage_row.get("coverage_status"),
            },
            "readiness_class": readiness_class,
        })

    return {"parameters": parameters}


def summarize(inventory: dict) -> dict:
    params = inventory["parameters"]
    total = len(params)
    by_class = Counter(p["readiness_class"] for p in params)
    by_extractor = Counter(p["extractor"] for p in params)
    by_section = {}
    for p in params:
        section = p["section"]
        bucket = by_section.setdefault(section, Counter())
        bucket[p["readiness_class"]] += 1
    by_section = {section: dict(counter) for section, counter in by_section.items()}

    by_stage_combo = Counter(",".join(p["required_stages"]) or "NONE" for p in params)
    by_data_type = Counter(p["data_type_class"] for p in params)

    tested = [p for p in params if p["has_tests"]]
    real_fixture = [p for p in params if p["real_fixture"]["has_gold_examples"]]
    production_ready = [p for p in params if p["readiness_class"] == "PRODUCTION_READY"]
    partial = [p for p in params if p["readiness_class"] == "PARTIAL"]
    config_only = [p for p in params if p["readiness_class"] == "CONFIG_ONLY"]

    production_ready_with_real_fixture = [p for p in production_ready if p["real_fixture"]["has_gold_examples"]]
    real_fixture_without_extractor = [p for p in real_fixture if p["extractor"] == "NONE"]

    return {
        "total_parameters": total,
        "by_readiness_class": dict(by_class),
        "by_readiness_class_rate": {k: round(v / total, 4) for k, v in by_class.items()},
        "by_extractor_mechanism": dict(by_extractor),
        "by_section": by_section,
        "by_required_stage_combination": dict(by_stage_combo),
        "by_data_type_class": dict(by_data_type),
        "test_coverage": {
            "parameters_with_tests": len(tested),
            "parameters_with_tests_rate": round(len(tested) / total, 4),
            "codes_with_tests": sorted(p["code"] for p in tested),
        },
        "real_fixture_coverage": {
            "parameters_with_gold_examples": len(real_fixture),
            "parameters_with_gold_examples_rate": round(len(real_fixture) / total, 4),
            "codes_with_gold_examples": sorted(p["code"] for p in real_fixture),
            "gold_backed_but_no_extractor_yet": sorted(p["code"] for p in real_fixture_without_extractor),
        },
        "production_ready": {
            "count": len(production_ready),
            "rate": round(len(production_ready) / total, 4),
            "codes": sorted(p["code"] for p in production_ready),
            "count_with_real_fixture": len(production_ready_with_real_fixture),
        },
        "partial": {
            "count": len(partial),
            "rate": round(len(partial) / total, 4),
            "codes": sorted(p["code"] for p in partial),
        },
        "config_only": {
            "count": len(config_only),
            "rate": round(len(config_only) / total, 4),
        },
        "abstain_by_design": {"count": 0, "note": "Not used at the matrix-definition level -- see methodology."},
        "not_applicable": {"count": 0, "note": "Not used at the matrix-definition level -- see methodology."},
        "abstention_rate_on_real_public_dataset": _abstention_rate_from_real_run(),
        "real_data_run": REAL_DATA_RUN,
        "hidden_blind_smoke": HIDDEN_BLIND_SMOKE,
        "phase_abc_session_yield": PHASE_ABC_SESSION_YIELD,
        "phase_d_hidden_validation": PHASE_D_HIDDEN_VALIDATION,
        "cross_stage_localization_session": CROSS_STAGE_LOCALIZATION_SESSION,
        "semantic_similarity_session": SEMANTIC_SIMILARITY_SESSION,
        "live_candidate_tagger_session": LIVE_CANDIDATE_TAGGER_SESSION,
        "los3a_forensic_fixes_session": LOS3A_FORENSIC_FIXES_SESSION,
        "top_missing_capabilities": _top_missing_capabilities(params),
        "priority_backlog": _priority_backlog(params),
    }


def _priority_backlog(params: list[dict]) -> dict:
    gold_backed_config_only = [p for p in params if p["readiness_class"] == "CONFIG_ONLY" and p["real_fixture"]["has_gold_examples"]]
    partial_gold_backed = [p for p in params if p["readiness_class"] == "PARTIAL" and p["real_fixture"]["has_gold_examples"]]
    high_priority_config_only = [p for p in params if p["readiness_class"] == "CONFIG_ONLY" and p["review_priority"] == "HIGH"]
    rest_config_only = [
        p for p in params
        if p["readiness_class"] == "CONFIG_ONLY" and p["review_priority"] != "HIGH"
    ]
    return {
        "P0_gold_backed_but_unimplemented": {
            "description": "Real organizer-provided gold checks exist for these codes (closed methodology "
                            "package) but no extractor exists at all -- highest-value next target since "
                            "correctness can be measured immediately, not just asserted.",
            "codes": sorted(p["code"] for p in gold_backed_config_only),
            "detail": [
                {"code": p["code"], "name": p["parameter_name"], "unit": p["unit"], "data_type_class": p["data_type_class"]}
                for p in sorted(gold_backed_config_only, key=lambda p: p["code"])
            ],
        },
        "P1_partial_with_gold_ready_to_validate": {
            "description": "Already PARTIAL (generic anchor mechanism applies) AND has real gold checks -- "
                            "the cheapest next step is running the existing mechanism against the closed "
                            "gold for these specific codes to get a first real precision/recall reading "
                            "before investing in anything new.",
            "codes": sorted(p["code"] for p in partial_gold_backed),
        },
        "P1_high_priority_config_only": {
            "description": "CONFIG_ONLY parameters whose criticality is \"Критическое (приостановка работ)\" "
                            "-- highest business impact if a real violation is missed, prioritize extractor "
                            "engineering here over MEDIUM-priority CONFIG_ONLY parameters.",
            "count": len(high_priority_config_only),
            "codes": sorted(p["code"] for p in high_priority_config_only),
        },
        "P2_remaining_config_only": {
            "description": "CONFIG_ONLY, MEDIUM review priority -- lowest business impact, defer.",
            "count": len(rest_config_only),
            "codes": sorted(p["code"] for p in rest_config_only),
        },
    }


_ABSTAIN_STATUSES = {"MISSING_EVIDENCE", "NOT_APPLICABLE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED", "SUSPICION"}


def _abstention_rate_from_real_run() -> dict:
    out = {}
    for object_id, data in REAL_DATA_RUN["objects"].items():
        total = data["total_evidence_groups"]
        abstained = sum(count for status, count in data["by_finding_status"].items() if status in _ABSTAIN_STATUSES)
        out[object_id] = {"abstained": abstained, "total": total, "rate": round(abstained / total, 4) if total else None}
    return out


def _top_missing_capabilities(params: list[dict]) -> list[dict]:
    config_only = [p for p in params if p["readiness_class"] == "CONFIG_ONLY"]

    def bucket(predicate, label, need):
        matches = [p for p in config_only if predicate(p)]
        if not matches:
            return None
        return {
            "capability_needed": need,
            "label": label,
            "parameter_count": len(matches),
            "example_codes": sorted(p["code"] for p in matches)[:8],
        }

    buckets = [
        bucket(
            lambda p: p["data_type_class"] == "ENUM_CLASS", "Material/fire/structural class or grade lookup",
            "Small controlled per-domain vocabulary extractor (concrete/steel grades, fire-resistance "
            "classes, material marks) -- same shape as the existing KR-055 rule pack, generalized across "
            "a curated code list instead of one code.",
        ),
        bucket(
            lambda p: p["data_type_class"] == "NUMERIC_COMPOUND", "Multi-axis numeric specification",
            "Structured multi-value/multi-unit parser (e.g. \"м3/ч / Па / кВт\" fan curves, \"шт. / м\" "
            "counts-with-dimension) -- the generic anchor mechanism only ever extracts a single scalar.",
        ),
        bucket(
            lambda p: p["data_type_class"] == "NUMERIC_COUNT", "Equipment/room/fixture count",
            "Table-row counting over a specification/schedule list, not a single label:value pair -- "
            "needs a table-structure extractor, not an anchor-phrase one.",
        ),
        bucket(
            lambda p: p["unit"] in ("—", "-", None, ""), "Presence/scheme/drawing-only check",
            "Drawing or scheme interpretation (fencing type on a plan, drainage layout presence, "
            "landscaping composition) -- inherently a CV/vector-drawing comparison problem, not text.",
        ),
        bucket(
            lambda p: p["data_type_class"] == "NUMERIC_SIMPLE", "Numeric parameter the generic anchor missed",
            "Same mechanism as the 26 PARTIAL parameters, just not yet eligible (short/ambiguous catalog "
            "name) or not yet confirmed to find a real match on the 2 available public objects -- widen "
            "eligibility criteria or curate a short synonym per name once more real objects are available.",
        ),
    ]
    return [b for b in buckets if b]


def render_markdown(inventory: dict, summary: dict) -> str:
    lines = []
    lines.append("# CASE10 — Coverage-матрица 132 параметров")
    lines.append("")
    lines.append(f"_Автоматически сгенерировано `evaluation/matrix_coverage.py` {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Пересчитывается детерминированно из `parameter_catalog_132.jsonl`, `parameter_coverage_132.jsonl`, `official_rule_packs.SUPPORTED_RULE_CODES` и `api_service/tests/*.py` — без ручной правки JSON._")
    lines.append("")
    lines.append("## Методология")
    lines.append("")
    lines.append("Параметр считается реализованным не по наличию строки в БД `Param`, а только если существует исполняемый путь `extraction → evidence → comparison/status`. Классы готовности:")
    lines.append("")
    lines.append("- **PRODUCTION_READY** — тюнингованный rule-pack (`official_rule_packs.py`) + реальное сравнение (включая negative path) + тесты + хотя бы один валидированный gold-пример в реальной оценке (`evaluation/smoke_real_data.py`). Это ровно `SUPPORTED_RULE_CODES` (5 кодов), посчитано программно, не захардкожено в отчёте.")
    lines.append("- **PARTIAL** — исполняемый extractor + сравнение существуют (в этой сессии — общий `generic_anchor_numeric` механизм), но нет gold-разметки для валидации точности, и/или логика сравнения обобщённая (equality с допуском), а не доменный порог из `trigger`.")
    lines.append("- **CONFIG_ONLY** — строка `Param` с метаданными (секция/unit/trigger/источники) существует, но исполняемого extraction-пути нет вообще; система всегда возвращает `MISSING_EVIDENCE`/`NOT_COMPARABLE`/`CLARIFICATION_REQUIRED`.")
    lines.append("- **ABSTAIN_BY_DESIGN** / **NOT_APPLICABLE** — намеренно **не используются** на уровне определения параметра матрицы: оба класса корректны только на уровне конкретной находки конкретного объекта в рантайме (например, параметр про лифтовую шахту — `NOT_APPLICABLE` для здания без лифта; параметр с данными лабораторных испытаний, отсутствующими во всех загруженных документах — `MISSING_EVIDENCE`, а не выдуманное значение). Использовать эти статусы на уровне определения параметра значило бы замаскировать отсутствие реализации — именно это явно запрещено в задании этой сессии.")
    lines.append("")
    lines.append("## Итоговые цифры")
    lines.append("")
    lines.append(f"- Всего параметров: **{summary['total_parameters']}**")
    for cls in ("PRODUCTION_READY", "PARTIAL", "CONFIG_ONLY"):
        count = summary["by_readiness_class"].get(cls, 0)
        rate = summary["by_readiness_class_rate"].get(cls, 0.0)
        lines.append(f"  - `{cls}`: **{count}** ({rate:.1%})")
    lines.append(f"- Параметров с тестами: **{summary['test_coverage']['parameters_with_tests']}** ({summary['test_coverage']['parameters_with_tests_rate']:.1%})")
    lines.append(f"- Параметров с реальными gold-примерами (организаторская закрытая методика): **{summary['real_fixture_coverage']['parameters_with_gold_examples']}** ({summary['real_fixture_coverage']['parameters_with_gold_examples_rate']:.1%}) — коды: {', '.join(summary['real_fixture_coverage']['codes_with_gold_examples'])}")
    lines.append(f"- PRODUCTION_READY коды: {', '.join(summary['production_ready']['codes'])}")
    lines.append(f"- PARTIAL коды ({summary['partial']['count']}): {', '.join(summary['partial']['codes'])}")
    lines.append("")
    lines.append("**Важное разграничение метрик (по требованию задания — не смешивать):**")
    lines.append("")
    lines.append("| Метрика | Значение | Что реально означает |")
    lines.append("|---|---:|---|")
    lines.append(f"| Parameter coverage (PRODUCTION_READY) | {summary['production_ready']['count']}/132 | Полный, gold-валидированный pipeline |")
    lines.append(f"| Parameter coverage (PRODUCTION_READY + PARTIAL) | {summary['production_ready']['count'] + summary['partial']['count']}/132 | Есть исполняемый extraction+comparison, БЕЗ gold-валидации точности для PARTIAL |")
    lines.append(f"| Real-data exercise coverage | 132/132 | Все 132 параметра реально прогоняются через `run_process` на официальном датасете (не значит, что все дают сравнение — большинство честно абстинируют) |")
    lines.append(f"| Real gold-example coverage | {summary['real_fixture_coverage']['parameters_with_gold_examples']}/132 | Есть хотя бы 1 атомарная проверка в закрытой методике организатора — НЕ то же самое, что precision/recall |")
    lines.append(f"| OCR accuracy | не считается | Нет human-labelled OCR-деноминатора в текущем датасете — честно не генерируем PASS (см. `evaluation/metrics.py::_evaluate_ocr`, который умеет это считать, когда деноминатор появится) |")
    lines.append("")
    lines.append("## По разделам ТЗ")
    lines.append("")
    lines.append("| Раздел | Всего | PRODUCTION_READY | PARTIAL | CONFIG_ONLY |")
    lines.append("|---|---:|---:|---:|---:|")
    for section, counts in sorted(summary["by_section"].items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        lines.append(f"| {section} | {total} | {counts.get('PRODUCTION_READY', 0)} | {counts.get('PARTIAL', 0)} | {counts.get('CONFIG_ONLY', 0)} |")
    lines.append("")
    lines.append("## По комбинации требуемых стадий (source_pd/rd/id)")
    lines.append("")
    lines.append("| Комбинация | Параметров |")
    lines.append("|---|---:|")
    for combo, count in sorted(summary["by_required_stage_combination"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {combo} | {count} |")
    lines.append("")
    lines.append("## По классу типа данных (`unit`)")
    lines.append("")
    lines.append("| Класс | Параметров | Пояснение |")
    lines.append("|---|---:|---|")
    explain = {
        "NUMERIC_SIMPLE": "одна физическая величина (м/мм/м²/м³/%/мм²) — целевой класс общего anchor-механизма",
        "NUMERIC_COMPOUND": "составная единица (несколько величин через \"/\") — вне охвата этой сессии",
        "NUMERIC_COUNT": "количество (шт./ед.) — риск спутать со счётчиком строк таблицы",
        "ENUM_CLASS": "категориальный код (марка/класс/степень) — нужен словарь домена, не общий regex",
        "OTHER": "прочее",
        "NONE": "без единицы измерения (текст/чертёж/наличие)",
    }
    for cls, count in sorted(summary["by_data_type_class"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {cls} | {count} | {explain.get(cls, '')} |")
    lines.append("")
    lines.append("## Real-data verification (венv, официальный публичный датасет, без Docker в этой сессии)")
    lines.append("")
    lines.append("Запущено напрямую через `app.domain.v3_pipeline.run_process` на throwaway SQLite (не боевая `data/api.db`), официальный `matrix_version=official-132-v1.1`, оба публичных объекта:")
    lines.append("")
    for object_id, data in summary["real_data_run"]["objects"].items():
        lines.append(f"**{object_id}** — всего evidence groups: {data['total_evidence_groups']}")
        lines.append("")
        lines.append("| finding_status | count |")
        lines.append("|---|---:|")
        for status, count in sorted(data["by_finding_status"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {status} | {count} |")
        if data.get("note"):
            lines.append("")
            lines.append(f"_{data['note']}_")
        lines.append("")
    lines.append("### Общий anchor-механизм: измеренный реальный выход (не оценка)")
    lines.append("")
    yield_data = summary["real_data_run"]["generic_mechanism_yield"]
    lines.append(f"- Eligible-параметров на объект: **{yield_data['eligible_parameters_per_object']}**")
    lines.append(f"- Новых COMPARABLE evidence groups создано: **{yield_data['new_comparable_evidence_groups_created']}**")
    lines.append("- Реальные значения, найденные механизмом на PD-стадии (сверено вручную с текстом документа, НЕ с gold):")
    lines.append("")
    for object_id, hits in yield_data["single_stage_real_values_found"].items():
        lines.append("")
        lines.append(f"  **{object_id}**")
        lines.append("")
        for code, value in hits.items():
            lines.append(f"  - `{code}`: {value}")
    lines.append("")
    lines.append(f"**Почему 0 новых сравнений**: {yield_data['why_zero_new_findings']}")
    lines.append("")
    lines.append("### Abstention rate на реальном публичном датасете")
    lines.append("")
    lines.append("| Объект | Abstained | Всего | Rate |")
    lines.append("|---|---:|---:|---:|")
    for object_id, rate_data in summary["abstention_rate_on_real_public_dataset"].items():
        lines.append(f"| {object_id} | {rate_data['abstained']} | {rate_data['total']} | {rate_data['rate']:.1%} |")
    lines.append("")
    lines.append("### Hidden blind smoke (OBJ-RECHNIKOV-7-7 — без hidden-меток)")
    lines.append("")
    hidden = summary["hidden_blind_smoke"]
    lines.append(
        "Тот же сценарий, что и `evaluation/smoke_hidden_blind.py` (`include_hidden=True`, "
        "`allow_hidden_gold_labels=False` — hidden-метки никогда не импортируются и не участвуют в "
        "инференсе), прогнан локально через venv тем же способом, что и публичные объекты выше — "
        "чтобы подтвердить, что новый generic-механизм не поднимает candidate-rate на скрытом объекте."
    )
    lines.append("")
    lines.append(f"- Всего evidence groups: **{hidden['total_evidence_groups']}**")
    lines.append(f"- `CANDIDATE`+`SUSPICION`: **{hidden['positive_like']}** ({hidden['candidate_rate']:.2%}) — гейт: ≤{hidden['gate_max_candidates']} и ≤{hidden['gate_max_candidate_rate']:.0%}")
    lines.append(f"- Групп от нового generic-механизма: **{hidden['generic_mechanism_groups']}**")
    lines.append(f"- **Гейт пройден: {'PASS' if hidden['gate_passes'] else 'FAIL'}**")
    lines.append("")
    lines.append("## Фаза A/B/C этой сессии: ENUM_CLASS / NUMERIC_COMPOUND / NUMERIC_COUNT extractors")
    lines.append("")
    yield_abc = summary["phase_abc_session_yield"]
    lines.append(
        f"Три новых generic-механизма (enum-class, compound, table-row-count) прогнаны тем же способом "
        f"({yield_abc['measured_at']}) на тех же 2 публичных объектах. Итоговое число evidence groups и "
        f"распределение по finding_status на обоих объектах **не изменилось** относительно real-data run выше "
        f"({'без изменений' if yield_abc['public_object_totals_unchanged'] else 'ИЗМЕНИЛОСЬ'}) — ни один из 32 "
        f"новых PARTIAL-кодов не нашёл сопоставимую пару PD+RD в рамках бюджета на этих конкретных объектах; "
        f"это тот же честный результат, что и у исходного numeric-механизма в checkpoint 35, а не регрессия."
    )
    lines.append("")
    lines.append("Реальные значения, найденные напрямую через `collect_*_observations` (не через полный pipeline, "
                  "который требует пары PD+RD) — подтверждает, что новые пути реально исполняются против настоящих "
                  "fitz-снимков страниц, а не только против синтетических тестовых фикстур:")
    lines.append("")
    for object_id, hits in yield_abc["single_stage_real_values_found"].items():
        lines.append(f"**{object_id}**")
        lines.append("")
        for code, note in hits.items():
            lines.append(f"- `{code}`: {note}")
        lines.append("")
    lines.append(f"_{yield_abc['why_zero_new_comparable_findings']}_")
    lines.append("")
    lines.append("## Фаза D этой сессии: честная валидация против organizer gold (SPZU-027/029/036/039)")
    lines.append("")
    phase_d = summary["phase_d_hidden_validation"]
    lines.append(
        f"Прогнан полный pipeline вслепую (`include_hidden=True`, `allow_hidden_gold_labels=False` — hidden-метки "
        f"никогда не импортируются в инференс) на объекте `{phase_d['object_id']}` ({phase_d['measured_at']}), "
        f"затем gold загружен ОТДЕЛЬНО и ТОЛЬКО постфактум, исключительно для честной сверки — не влиял на "
        f"экстракцию, результат не использовался для подгонки логики извлечения (по явному правилу проекта — "
        f"не хардкодить под gold)."
    )
    lines.append("")
    lines.append(f"**Результат: {phase_d['result']}**")
    lines.append("")
    lines.append("| Код | Gold | Результат pipeline |")
    lines.append("|---|---|---|")
    for code in phase_d["codes"]:
        lines.append(f"| `{code}` | | {phase_d['per_code'][code]} |")
    lines.append("")
    metrics_d = phase_d["metrics_computed_via_evaluate_case10_on_these_4_checks_only"]
    lines.append("Метрики, посчитанные той же функцией `evaluate_case10` (не оценка вручную), по этим 4 проверкам "
                  "отдельно (НЕ смешано с публичной n=6/n=12 метрикой из checkpoint 37, чтобы не путать dev-метрику "
                  "на публичных данных с честной проверкой на скрытом объекте):")
    lines.append("")
    lines.append(f"- `finding_precision`: {metrics_d['finding_precision']} (нет ни одного положительного предсказания — 0/0)")
    lines.append(f"- `finding_recall`: {metrics_d['finding_recall']} (sample_size={metrics_d['finding_recall_sample_size']}, 95% CI {metrics_d['finding_recall_confidence_interval_95']})")
    lines.append(f"- `finding_f1`: {metrics_d['finding_f1']}")
    lines.append(f"- `false_positive_rate`: {metrics_d['false_positive_rate']} (sample_size={metrics_d['false_positive_rate_sample_size']})")
    lines.append("")
    lines.append("**Важно**: все 4 из этих gold-проверок сами помечены организатором как `score_eligible: false` / "
                  "`GOLD_READY_SECOND_REVIEW` (требуют второй экспертной проверки) — тот же статус, что и "
                  "Novoslobodskaya PZ-009/KR-055/KR-058 проверки, уже использованные для публичной "
                  "`finding_false_positive_rate` (n=5) метрики в checkpoint 37. Включены на том же основании, не "
                  "трактуются как финальные.")
    lines.append("")
    lines.append("**Форензик-находки, НЕ использованные для подгонки экстрактора** (только для честного отчёта):")
    lines.append("")
    for note in phase_d["forensic_findings_not_used_to_tune_the_extractor"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## Сигнал кросс-стадийной локализации этой сессии (document-manifest narrowing + table fingerprint)")
    lines.append("")
    csl = summary["cross_stage_localization_session"]
    lines.append(
        f"Добавлены два новых, чисто аддитивных сигнала поверх всех 4 generic-механизмов ({csl['measured_at']}): "
        f"{csl['signals_added'][0]}; {csl['signals_added'][1]}. Прогнаны на тех же 2 публичных объектах тем же "
        f"способом, что и real-data run выше — итоговые числа evidence groups **не изменились** "
        f"({'без изменений' if csl['public_object_totals_unchanged'] else 'ИЗМЕНИЛОСЬ'}), новых сравнимых "
        f"generic-tier evidence groups создано: {csl['new_comparable_evidence_groups_created']}."
    )
    lines.append("")
    lines.append("Честная причина (установлена прямой диагностикой в этой сессии, не переформулировкой прежней гипотезы):")
    lines.append("")
    for object_id, note in csl["why_still_zero"].items():
        lines.append(f"**{object_id}**")
        lines.append("")
        lines.append(note)
        lines.append("")
    lines.append(f"_{csl['conclusion']}_")
    lines.append("")
    lines.append("## Семантические якоря (Sentence-BERT), ТЗ 9.1 п.2")
    lines.append("")
    sem = summary["semantic_similarity_session"]
    lines.append(
        f"До этой сессии ни один generic-механизм не использовал embedding-модель — только строковое/токенное "
        f"сопоставление (`anchor_search.py`). Буквальный пункт ТЗ 9.1.2 (\"регулярные выражения ... и "
        f"семантические якоря. Используется модель Sentence-BERT (all-MiniLM-L6-v2) или её совместимые аналоги\") "
        f"был не закрыт. Добавлен новый опциональный модуль `semantic_similarity.py` ({sem['measured_at']})."
    )
    lines.append("")
    lines.append(f"**Модель**: {sem['model']}.")
    lines.append("")
    lines.append(sem["model_choice_rationale"])
    lines.append("")
    lines.append("**Встроен в двух местах**:")
    lines.append("")
    for item in sem["wired_into"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("**Почему семантический fallback anchor-поиска выключен по умолчанию**:")
    lines.append("")
    lines.append(sem["anchor_fallback_disabled_by_default_why"])
    lines.append("")
    lines.append(
        f"**Результат на публичных объектах** (тот же способ, что и real-data run выше): итоговые числа evidence "
        f"groups и их `status_counts`/`source_counts` **не изменились** — {sem['evidence_group_level_diff']} "
        f"Новых сравнимых generic-tier evidence groups: {sem['new_comparable_evidence_groups_created']}."
    )
    lines.append("")
    activity = sem["semantic_scoring_activity_confirmed_live"]
    lines.append(
        f"Механизм при этом подтверждённо **реально работает** на реальных данных (не мёртвый код): "
        f"инструментирование `pick_best_candidate` во время реального прохода по OBJ-NOVOSLOBODSKAYA показало "
        f"{activity['candidate_evaluations']} вычислений кандидатов, все {activity['evaluations_with_a_computed_score']} "
        f"получили реальный score (диапазон {activity['score_range_observed'][0]}–{activity['score_range_observed'][1]}), "
        f"{activity['evaluations_below_the_0.45_floor']} из них ниже порога 0.45 и был бы отфильтрован."
    )
    lines.append("")
    lines.append(f"_{sem['why_still_zero_new_comparable_groups']}_")
    lines.append("")
    lat = sem["latency"]
    lines.append(
        f"**Производительность** (бюджет ТЗ раздел 11: {lat['budget_per_TZ_11']}): прогретый инференс "
        f"{lat['warm_embedding_ms_per_text_batched']} мс/текст батчем, {lat['warm_embedding_ms_single_call']} мс "
        f"первый одиночный вызов — на 1-2 порядка меньше бюджета. {lat['note']}"
    )
    lines.append("")
    lines.append(f"**Тесты**: {sem['tests_added']}")
    lines.append("")
    lines.append("## Top missing capabilities (что закрывает остаток CONFIG_ONLY)")
    lines.append("")
    for item in summary["top_missing_capabilities"]:
        lines.append(f"### {item['label']} ({item['parameter_count']} параметров)")
        lines.append("")
        lines.append(item["capability_needed"])
        lines.append("")
        lines.append(f"Примеры кодов: {', '.join(item['example_codes'])}")
        lines.append("")
    lines.append("## Приоритетный backlog")
    lines.append("")
    backlog = summary["priority_backlog"]
    p0 = backlog["P0_gold_backed_but_unimplemented"]
    lines.append(f"### P0 — есть реальный gold, реализации нет ({len(p0['codes'])})")
    lines.append("")
    lines.append(p0["description"])
    lines.append("")
    for item in p0["detail"]:
        lines.append(f"- `{item['code']}` {item['name']} ({item['unit'] or '—'}, {item['data_type_class']})")
    lines.append("")
    p1a = backlog["P1_partial_with_gold_ready_to_validate"]
    lines.append(f"### P1 — PARTIAL + реальный gold, готово к немедленной валидации ({len(p1a['codes'])})")
    lines.append("")
    lines.append(p1a["description"])
    lines.append("")
    lines.append(f"Коды: {', '.join(p1a['codes']) or '(нет)'}")
    lines.append("")
    p1b = backlog["P1_high_priority_config_only"]
    lines.append(f"### P1 — CONFIG_ONLY, HIGH review priority ({p1b['count']})")
    lines.append("")
    lines.append(p1b["description"])
    lines.append("")
    lines.append(f"Коды: {', '.join(p1b['codes'])}")
    lines.append("")
    p2 = backlog["P2_remaining_config_only"]
    lines.append(f"### P2 — CONFIG_ONLY, MEDIUM review priority ({p2['count']})")
    lines.append("")
    lines.append(p2["description"])
    lines.append("")
    lines.append(f"Коды: {', '.join(p2['codes'])}")
    lines.append("")
    lines.append("## Полная таблица (132/132)")
    lines.append("")
    lines.append("| Код | Раздел | Параметр | Unit | Класс | Extractor | Readiness | Тесты | Gold |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for p in inventory["parameters"]:
        lines.append(
            f"| {p['code']} | {p['section']} | {p['parameter_name']} | {p['unit'] or ''} | "
            f"{p['data_type_class']} | {p['extractor']} | {p['readiness_class']} | "
            f"{'✓' if p['has_tests'] else ''} | {p['real_fixture']['gold_check_count'] or ''} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    inventory = build_inventory()
    summary = summarize(inventory)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "parameters": inventory["parameters"],
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUTPUT_MD.write_text(render_markdown(inventory, summary), encoding="utf-8")
    print(json.dumps({
        "total": summary["total_parameters"],
        "production_ready": summary["production_ready"]["count"],
        "partial": summary["partial"]["count"],
        "config_only": summary["config_only"]["count"],
        "written": [str(OUTPUT_JSON), str(OUTPUT_MD)],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
