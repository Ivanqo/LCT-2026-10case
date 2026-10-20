"""Tests that need the organizer's data skip, with the reason, when that data is not in the checkout.

The repository ships only the parameter catalog and the submission schema (`app/reference_data/case_data`); the public and
hidden gold files, the organizer's participant package and the matrix workbook stay with the organizer. A test that reads
them cannot pass on a clean clone, so it is skipped here instead of failing. On a machine that has the data the same
tests run as before.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _public_train_data() -> bool:
    return Path("learning_data/extracted/train_public_203/data").is_dir()


def _hidden_gold() -> bool:
    return Path("learning_data/extracted/test_hidden_213/data/hidden_gold_checks_organizer_only.jsonl").is_file()


def _participant_package() -> bool:
    try:
        from evaluation.measurement_bench.common import PARTICIPANT_DATA
    except Exception:
        return False
    return PARTICIPANT_DATA is not None


def _matrix_workbook() -> bool:
    try:
        from app.domain.official_dataset import find_dataset_paths
    except Exception:
        return False
    path = find_dataset_paths().get("matrix_v11_xlsx")
    return bool(path and Path(path).exists())


# (nodeid fragment, availability check, what is missing)
_NEEDS_ORGANIZER_DATA = (
    ("test_case10_evaluation.py::Case10EvaluationTests::test_public_metric_fixture_keeps_ocr_key_fields_null_with_reasons",
     _public_train_data, "the organizer's public training split (learning_data/extracted/train_public_203)"),
    ("test_case10_measurement_bench.py::StatisticsTests::test_prior_weights_are_a_distribution", _participant_package, "the organizer's participant package (parameter catalog, public gold)"),
    ("test_case10_measurement_bench.py::NormalisationTests::test_public_gold_conventions_are_read_from_public_rows_only", _participant_package, "the organizer's participant package (parameter catalog, public gold)"),
    ("test_case10_measurement_bench.py::SeedMutationTests::", _participant_package, "the organizer's participant package (parameter catalog, public gold)"),
    ("test_case10_measurement_bench.py::CorpusContractTests::test_probe_plan_is_deterministic", _participant_package, "the organizer's participant package (parameter catalog, public gold)"),
    ("test_case10_official.py::OfficialDataTests::test_matrix_v11_xlsx_has_official_codes_and_priority_split",
     _matrix_workbook, "the organizer's matrix workbook (Матрица параметров, редакция 1.1)"),
    ("test_case10_training_release.py::Case10TrainingReleaseTests::test_hidden_labels_are_blocked_and_runtime_process_does_not_use_gold_fixture_groups",
     _hidden_gold, "the organizer's hidden gold file (learning_data/extracted/test_hidden_213)"),
)


def pytest_collection_modifyitems(config, items):
    verdicts: dict = {}
    for item in items:
        for fragment, available, missing in _NEEDS_ORGANIZER_DATA:
            if fragment not in item.nodeid.replace("\\", "/"):
                continue
            if available not in verdicts:
                verdicts[available] = available()
            if not verdicts[available]:
                item.add_marker(pytest.mark.skip(reason=f"needs {missing}, which is not part of the repository"))
