"""Coverage for `app/domain/gpu.py`: CUDA detection, the per-process GPU
memory-fraction cap (grading-server GPUs are shared across teams -- see the
module docstring), and the "never degrade silently" startup observability
this mirrors from `check_tesseract_health`/`ocr_tesseract_available`. Runs
against the real, locally installed CPU-only torch build (no GPU on this dev
machine -- the GPU-present branch is exercised by mocking `torch.cuda.*`,
never by requiring actual hardware)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import torch

from app.domain import gpu


def _gauge_value(gauge) -> float:
    return gauge._value.get()  # noqa: SLF001 -- test-only introspection


class DetectGpuNoHardwareTests(unittest.TestCase):
    """This dev machine genuinely has no CUDA device -- these exercise the
    real `torch.cuda.is_available()` call, not a mock."""

    def setUp(self):
        gpu._cached_status = None  # noqa: SLF001

    def test_reports_unavailable_with_no_error(self):
        result = gpu.detect_gpu()
        self.assertFalse(result["available"])
        self.assertIsNone(result["device_name"])
        self.assertEqual(result["device_count"], 0)
        self.assertIsNone(result["error"])

    def test_logs_info_not_warning_when_gpu_not_expected(self):
        with patch.object(gpu, "EXPECT_GPU", False):
            with self.assertLogs(gpu.logger, level="INFO") as captured:
                gpu.detect_gpu()
        self.assertTrue(any("No GPU detected" in msg for msg in captured.output))
        self.assertFalse(any(msg.startswith("WARNING") for msg in captured.output))

    def test_logs_warning_when_gpu_expected_but_absent(self):
        with patch.object(gpu, "EXPECT_GPU", True):
            with self.assertLogs(gpu.logger, level="WARNING") as captured:
                gpu.detect_gpu()
        self.assertTrue(any("CASE10_EXPECT_GPU=1" in msg for msg in captured.output))

    def test_sets_the_availability_gauge_to_zero(self):
        gpu.detect_gpu()
        self.assertEqual(_gauge_value(gpu.gpu_available), 0)

    def test_preferred_device_is_cpu(self):
        self.assertEqual(gpu.preferred_device(), "cpu")


class DetectGpuMockedHardwareTests(unittest.TestCase):
    """Exercises the GPU-present branch (device info + memory-fraction cap)
    without requiring real hardware, by mocking `torch.cuda.*` directly --
    `gpu.py`'s `import torch` inside `detect_gpu()` resolves to this same
    already-imported module object, so patching it here is effective."""

    def setUp(self):
        gpu._cached_status = None  # noqa: SLF001

    def test_available_gpu_reports_device_info_and_caps_memory_fraction(self):
        with patch.object(torch.cuda, "is_available", return_value=True), \
                patch.object(torch.cuda, "device_count", return_value=1), \
                patch.object(torch.cuda, "get_device_name", return_value="NVIDIA H100 80GB HBM3") as mocked_name, \
                patch.object(torch.cuda, "set_per_process_memory_fraction") as mocked_fraction:
            result = gpu.detect_gpu()

        self.assertTrue(result["available"])
        self.assertEqual(result["device_name"], "NVIDIA H100 80GB HBM3")
        self.assertEqual(result["device_count"], 1)
        mocked_name.assert_called_once_with(0)
        mocked_fraction.assert_called_once_with(gpu.GPU_MEMORY_FRACTION, device=0)

    def test_available_gpu_sets_the_gauge_to_one(self):
        with patch.object(torch.cuda, "is_available", return_value=True), \
                patch.object(torch.cuda, "device_count", return_value=1), \
                patch.object(torch.cuda, "get_device_name", return_value="NVIDIA H100 80GB HBM3"), \
                patch.object(torch.cuda, "set_per_process_memory_fraction"):
            gpu.detect_gpu()
        self.assertEqual(_gauge_value(gpu.gpu_available), 1)

    def test_available_gpu_logs_info_with_device_name(self):
        with patch.object(torch.cuda, "is_available", return_value=True), \
                patch.object(torch.cuda, "device_count", return_value=1), \
                patch.object(torch.cuda, "get_device_name", return_value="NVIDIA H100 80GB HBM3"), \
                patch.object(torch.cuda, "set_per_process_memory_fraction"), \
                self.assertLogs(gpu.logger, level="INFO") as captured:
            gpu.detect_gpu()
        self.assertTrue(any("GPU detected: NVIDIA H100 80GB HBM3" in msg for msg in captured.output))

    def test_preferred_device_is_cuda_once_detected(self):
        with patch.object(torch.cuda, "is_available", return_value=True), \
                patch.object(torch.cuda, "device_count", return_value=1), \
                patch.object(torch.cuda, "get_device_name", return_value="NVIDIA H100 80GB HBM3"), \
                patch.object(torch.cuda, "set_per_process_memory_fraction"):
            gpu.detect_gpu()
        self.assertEqual(gpu.preferred_device(), "cuda")

    def test_memory_fraction_setup_failure_is_logged_but_still_reports_available(self):
        with patch.object(torch.cuda, "is_available", return_value=True), \
                patch.object(torch.cuda, "device_count", return_value=1), \
                patch.object(torch.cuda, "get_device_name", return_value="NVIDIA H100 80GB HBM3"), \
                patch.object(torch.cuda, "set_per_process_memory_fraction", side_effect=RuntimeError("boom")), \
                self.assertLogs(gpu.logger, level="WARNING") as captured:
            result = gpu.detect_gpu()
        self.assertTrue(result["available"])
        self.assertIsNotNone(result["error"])
        self.assertTrue(any("could not be configured" in msg for msg in captured.output))


class TorchNotImportableTests(unittest.TestCase):
    """Simulates a deployment missing torch entirely (e.g. a dev machine that
    never installed the optional semantic-anchor dependencies at all) --
    `semantic_similarity.py` already treats this as "feature unavailable",
    and this module must degrade the same way rather than raising."""

    def setUp(self):
        gpu._cached_status = None  # noqa: SLF001

    def test_import_failure_is_reported_as_unavailable_not_raised(self):
        real_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("simulated: torch not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked_import):
            result = gpu.detect_gpu()
        self.assertFalse(result["available"])
        self.assertIn("torch not importable", result["error"])


class GpuHealthStatusCachingTests(unittest.TestCase):
    def setUp(self):
        gpu._cached_status = None  # noqa: SLF001

    def test_health_status_runs_detection_on_first_call_when_never_probed(self):
        result = gpu.gpu_health_status()
        self.assertIn("available", result)
        self.assertIsNotNone(gpu._cached_status)  # noqa: SLF001

    def test_health_status_returns_a_copy_not_the_cached_object(self):
        gpu.detect_gpu()
        first = gpu.gpu_health_status()
        first["available"] = "mutated"
        second = gpu.gpu_health_status()
        self.assertNotEqual(second["available"], "mutated")

    def test_health_status_does_not_reprobe_once_cached(self):
        gpu.detect_gpu()
        with patch.object(torch.cuda, "is_available") as mocked:
            gpu.gpu_health_status()
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
