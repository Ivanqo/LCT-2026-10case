"""Time `python -m app.cli run` inside the delivery image, the way the ТЗ §11 budget is judged.

Each run is a fresh container with `--network none`, CPU/RAM limits and (optionally) `--gpus all`; the package is
mounted read-only; the live-tagger cache lives on a named volume, so run 1 is cold (after --fresh-cache) and later
runs are warm. The CLI's own `timings.json` gives the stage split (load / parse+OCR / tagging / comparison of the
132 parameters / protocol), the process-tree and cgroup memory peaks and the torch VRAM peak; this driver adds the
container wall time, the host GPU memory peak (nvidia-smi, if present) and the SHA-256 of every result.json.

    python deploy/tools/docker_timing.py --image case10-api:latest --docs "D:/packages/obj" \
        --registry D:/packages/obj.csv --label OBJ --out-dir runs/OBJ --cpus 3 --memory 10g --gpus --runs 2 --fresh-cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time


def _gpu_used_mb() -> int | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return sum(int(line.strip()) for line in out.splitlines() if line.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class _GpuPeak(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.baseline = _gpu_used_mb()
        self.peak = self.baseline
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(1.0):
            used = _gpu_used_mb()
            if used is not None and (self.peak is None or used > self.peak):
                self.peak = used

    def stop(self) -> dict:
        self._stop_event.set()
        self.join(timeout=5)
        if self.baseline is None:
            return {"nvidia_smi": False}
        return {"host_gpu_used_mb_baseline": self.baseline, "host_gpu_used_mb_peak": self.peak,
                "host_gpu_delta_mb": (self.peak or 0) - self.baseline}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--docs", required=True, help="package folder or ZIP on the host")
    ap.add_argument("--registry")
    ap.add_argument("--label", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cpus", default="3")
    ap.add_argument("--memory", default="10g")
    ap.add_argument("--gpus", action="store_true")
    ap.add_argument("--workers", default=None, help="live-tagger workers (default: the --cpus quota)")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--cache-volume", default=None)
    ap.add_argument("--fresh-cache", action="store_true")
    ap.add_argument("--network", default="none")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    volume = args.cache_volume or f"case10_timing_cache_{args.label.lower()}"
    if args.fresh_cache:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
    docs = Path(args.docs).resolve()
    docs_target = "/in/package" + (".zip" if docs.is_file() else "")
    summary = {"label": args.label, "image": args.image, "cpus": args.cpus, "memory": args.memory, "gpus": args.gpus,
               "network": args.network, "runs": []}
    image_id = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", args.image], capture_output=True, text=True).stdout.strip()
    summary["image_id"] = image_id
    for index in range(1, args.runs + 1):
        run_dir = out_dir / f"run{index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        cmd = ["docker", "run", "--rm", "--network", args.network, "--cpus", str(args.cpus), "--memory", args.memory,
               "-v", f"{docs}:{docs_target}:ro", "-v", f"{out_dir}:/out", "-v", f"{volume}:/data"]
        if args.gpus:
            cmd += ["--gpus", "all"]
        cli = ["python", "-m", "app.cli", "run", "--docs", docs_target, "--out", f"/out/run{index}/result.json",
               "--timings", f"/out/run{index}/timings.json", "--work-dir", "/tmp/case10_work",
               "--workers", str(args.workers or int(float(args.cpus)))]
        if args.registry:
            registry = Path(args.registry).resolve()
            cmd += ["-v", f"{registry}:/in/registry{registry.suffix}:ro"]
            cli += ["--registry", f"/in/registry{registry.suffix}"]
        gpu = _GpuPeak()
        gpu.start()
        started = time.perf_counter()
        proc = subprocess.run(cmd + [args.image] + cli, capture_output=True, text=True, encoding="utf-8", errors="replace")
        wall = round(time.perf_counter() - started, 1)
        gpu_report = gpu.stop()
        (run_dir / "stderr.log").write_text(proc.stderr, encoding="utf-8")
        result = run_dir / "result.json"
        entry = {"run": index, "exit_code": proc.returncode, "container_wall_seconds": wall, **gpu_report,
                 "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest() if result.exists() else None}
        timings = run_dir / "timings.json"
        if timings.exists():
            entry["timings"] = json.loads(timings.read_text(encoding="utf-8"))
        summary["runs"].append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k != "timings"}, ensure_ascii=False), flush=True)
        if proc.returncode != 0:
            print(proc.stderr[-3000:])
            break
    shas = {run["result_sha256"] for run in summary["runs"]}
    summary["identical_results"] = len(shas) == 1 and None not in shas
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("identical_results:", summary["identical_results"])


if __name__ == "__main__":
    main()
