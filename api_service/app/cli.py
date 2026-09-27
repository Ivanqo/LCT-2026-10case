"""CASE10 command line -- the server-side batch path (Phase 12, S6).

    python -m app.cli run --docs <folder|zip> [--registry <csv|xlsx|json>] --out result.json
    python -m app.cli validate result.json

`run` takes a whole package (no UI size limits), imports it with its file registry (Perechen ID 1.1), runs the
same `run_process` the service runs, and writes the submission JSON (`submission_schema.json` + GOLD 1.1 fields +
a `package` block describing what was read). The JSON is validated against the schema before it is written; a
schema violation is exit code 2. `result.json` carries no time, path or host specifics, so two runs of the same
image on the same package give the same SHA-256. Timings, peak memory and the tagger's coverage go to a separate
`--timings` file.

The run is fully in-process and offline: no queue, no cache server, no model download (HF_HUB_OFFLINE), no
network client is imported (tested with sockets disabled). The parameter matrix comes from the reference data
bundled with the code (`app/reference_data`), never from a mounted organizer package.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import functools
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

REFERENCE_ROOT = Path(__file__).resolve().parent / "reference_data" / "case_data"
SCHEMA_PATH = REFERENCE_ROOT / "submission_schema.json"
PROJECT_ID = 1
ORGANIZATION_ID = 1
EXIT_OK, EXIT_ERROR, EXIT_INVALID = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="package (+ registry) -> process -> submission JSON")
    run.add_argument("--docs", required=True, help="package folder or ZIP archive")
    run.add_argument("--registry", help="file registry per Perechen ID 1.1 (CSV/XLSX/JSON); without it the package "
                                        "is accepted as CLARIFICATION_REQUIRED")
    run.add_argument("--out", required=True, help="where to write result.json")
    run.add_argument("--object-id", help="object id when the registry has none or several")
    run.add_argument("--work-dir", help="unpacked ZIPs, manifest and the run database (default: <out>.work/)")
    run.add_argument("--db-url", help="SQLAlchemy URL of the run database (default: in-memory SQLite)")
    run.add_argument("--workers", type=int, help="live-tagger processes (0 = CPUs available to the container)")
    run.add_argument("--timings", help="write stage timings / peak memory / tagger coverage here (JSON)")
    run.add_argument("--protocol-pdf", help="also write the protocol PDF (Annex 2) here")
    run.add_argument("--schema", default=str(SCHEMA_PATH), help="submission JSON schema")

    validate = sub.add_parser("validate", help="validate a result JSON against the submission schema")
    validate.add_argument("result")
    validate.add_argument("--schema", default=str(SCHEMA_PATH))

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return _validate_file(Path(args.result), Path(args.schema))
        return _run(args)
    except Exception as exc:  # noqa: BLE001 -- a CLI reports, it does not print a traceback for user errors
        from .batch_package import PackageError

        if isinstance(exc, PackageError):
            _say(f"error: {exc}")
            return EXIT_ERROR
        raise


# ------------------------------------------------------------------------------------------------------ run


def _run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    out_path = Path(args.out).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else out_path.with_name(out_path.name + ".work")
    clock = _StageClock()
    memory = _PeakMemory()
    memory.start()

    # Environment first: `app.config.settings` is read once, at the first import of any app module below.
    from . import batch_package as bp

    with clock.stage("unpack"):
        package_root, package_name = bp.resolve_package(
            args.docs, unpack_root=work_dir / "unpacked",
            max_unpacked_bytes=int(os.getenv("CASE10_BATCH_MAX_UNPACKED_BYTES", str(256 * 1024**3))),
        )
    _prepare_environment(package_root=package_root, work_dir=work_dir, workers=args.workers)

    with clock.stage("package_scan_sha256"):
        files = bp.scan_package(package_root)
        registry = bp.read_registry(args.registry) if args.registry else None
        registry_sha = bp.sha256_file(Path(args.registry)) if args.registry else None
        plan = bp.build_plan(files, registry, package_name=package_name, object_id=args.object_id,
                             registry_sha256=registry_sha)
    _say(f"package: {len(files)} files, {plan.report['files_pdf']} PDF, {plan.report['bytes_total'] / 1e6:.1f} MB; "
         f"object {plan.object_id!r}; registry {plan.report['registry_status']}")

    with clock.stage("import_models"):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from .db.models import Base
        from .domain import v3_pipeline
        from .domain.official_dataset import MATRIX_VERSION_OFFICIAL
        from evaluation.exporter import protocol_to_submission

    engine = create_engine(args.db_url or "sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        with clock.stage("import"):
            documents = bp.import_plan(db, plan, project_id=PROJECT_ID, organization_id=ORGANIZATION_ID,
                                       manifest_path=work_dir / "manifest.jsonl")
            process = v3_pipeline.create_process(db, project_id=PROJECT_ID, organization_id=ORGANIZATION_ID,
                                                 object_id=plan.object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
            db.commit()
        tagger_diagnostics = clock.instrument_pipeline()
        with clock.stage("run_process"):
            v3_pipeline.run_process(db, process_id=process.id)
        protocol = v3_pipeline.latest_protocol(db, project_id=PROJECT_ID, organization_id=ORGANIZATION_ID,
                                               process_id=process.id)
        with clock.stage("export_validate"):
            submission = protocol_to_submission(v3_pipeline.protocol_to_dict(protocol))
            submission["package"] = plan.report
            errors = _validation_errors(submission, Path(args.schema))
            data = json.dumps(submission, ensure_ascii=False, indent=2).encode("utf-8")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
        if args.protocol_pdf:
            with clock.stage("protocol_pdf"):
                Path(args.protocol_pdf).write_bytes(v3_pipeline.protocol_pdf_bytes(protocol))
    finally:
        db.close()
    memory.stop()

    digest = hashlib.sha256(data).hexdigest()
    labels = Counter(check.get("violation_label") for check in submission.get("checks") or [])
    _say(f"result: {out_path.name} sha256={digest} checks={len(submission.get('checks') or [])} "
         f"labels={dict(sorted(labels.items(), key=lambda kv: str(kv[0])))} documents={documents}")
    if args.timings:
        report = {
            "result_sha256": digest,
            "object_id": plan.object_id,
            "package": {key: plan.report[key] for key in ("files_total", "files_pdf", "bytes_total", "pdf_pages_total", "documents_by_stage",
                                                          "registry_status")},
            "wall_seconds_total": round(time.perf_counter() - started, 2),
            "stages_seconds": clock.report(),
            "stage_calls": dict(clock.calls),
            "tagger": _tagger_summary(tagger_diagnostics),
            "memory": memory.report(),
            "gpu": _gpu_report(),
            "cpu_available": _cpu_available(),
            "workers_requested": os.getenv("CASE10_LIVE_TAGGER_WORKERS"),
            "validation_errors": errors,
        }
        Path(args.timings).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if errors:
        _say(f"INVALID: {len(errors)} schema/format errors, first: {errors[0]}")
        return EXIT_INVALID
    return EXIT_OK


def _prepare_environment(*, package_root: Path, work_dir: Path, workers: int | None) -> None:
    """Process environment for an offline, in-process run. Must happen before `app.config` is imported."""
    os.environ["CASE10_ORIGINALS_ROOT"] = str(package_root)      # the only place document bytes are read from
    os.environ["CASE10_DATASET_ROOT"] = str(REFERENCE_ROOT)      # matrix 1.1 / schema bundled with the code
    os.environ["RABBITMQ_URL"] = ""                              # no queue: the pipeline runs right here
    os.environ["REDIS_URL"] = ""                                 # no cache server
    os.environ["HF_HUB_OFFLINE"] = "1"                           # the embedding model is baked into the image
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("API_DATA_DIR", str(work_dir / "data"))   # live-tagger cache (image: /data)
    if workers is not None:
        os.environ["CASE10_LIVE_TAGGER_WORKERS"] = str(workers)
    if "app.config" in sys.modules:
        # settings are read once per process; an embedding caller must start a fresh interpreter (tests do)
        raise RuntimeError("app.cli run must be the first user of app.config in its process")


def _validation_errors(submission: dict[str, Any], schema: Path) -> list[str]:
    from evaluation.exporter import validate_submission_basic, validate_submission_schema

    return validate_submission_schema(submission, schema) + validate_submission_basic(submission)


def _validate_file(path: Path, schema: Path) -> int:
    submission = json.loads(path.read_text(encoding="utf-8"))
    errors = _validation_errors(submission, schema)
    for error in errors[:50]:
        _say(error)
    _say(f"{path.name}: {'valid' if not errors else f'{len(errors)} errors'} "
         f"({len(submission.get('checks') or [])} checks, sha256={hashlib.sha256(path.read_bytes()).hexdigest()})")
    return EXIT_INVALID if errors else EXIT_OK


# ------------------------------------------------------------------------------------------------- measuring


class _StageClock:
    """Exclusive wall time per stage. `instrument_pipeline` wraps a few pipeline functions (no behaviour change:
    arguments and return values pass through) so run_process splits into tagging / parsing+OCR / comparison of
    the 132 parameters / protocol. A nested stage's time is charged to it and removed from its parent."""

    def __init__(self) -> None:
        self.totals: dict[str, float] = defaultdict(float)
        self.calls: Counter[str] = Counter()
        self._stack: list[list[Any]] = []
        self._thread = threading.get_ident()

    def stage(self, name: str):
        clock = self

        class _Stage:
            def __enter__(self):
                clock._enter(name)

            def __exit__(self, *exc):
                clock._exit()

        return _Stage()

    def _enter(self, name: str) -> None:
        if threading.get_ident() == self._thread:
            self._stack.append([name, time.perf_counter(), 0.0])

    def _exit(self) -> None:
        if threading.get_ident() != self._thread or not self._stack:
            return
        name, start, children = self._stack.pop()
        elapsed = time.perf_counter() - start
        self.totals[name] += elapsed - children
        self.calls[name] += 1
        if self._stack:
            self._stack[-1][2] += elapsed

    def wrap(self, module: Any, attribute: str, stage: str, sink: Callable[[Any], None] | None = None) -> None:
        original = getattr(module, attribute, None)
        if original is None or getattr(original, "_case10_timed", False):
            return

        @functools.wraps(original)
        def timed(*args, **kwargs):
            self._enter(stage)
            try:
                result = original(*args, **kwargs)
            finally:
                self._exit()
            if sink is not None:
                sink(result)
            return result

        timed._case10_timed = True  # type: ignore[attr-defined]
        setattr(module, attribute, timed)

    def instrument_pipeline(self) -> list[Any]:
        import importlib

        from .domain import official_evidence, v3_pipeline

        diagnostics: list[Any] = []
        self.wrap(official_evidence, "tag_live_candidates", "tagging", sink=diagnostics.append)
        self.wrap(v3_pipeline, "create_official_evidence_groups", "compare_132")
        self.wrap(v3_pipeline, "create_protocol_version", "protocol")
        # Parsing/OCR outside the tagger. `live_candidate_tagger.extract_original_pages` is deliberately NOT wrapped:
        # the tagger treats a replaced page source as a test fixture and then scans in-process without the pool or
        # the SHA-256 cache (`_page_source_replaced`) -- wrapping it would change how fast the run is, not what it
        # measures. The tagger reports its own parse+OCR+match time (`scan_seconds`, `scan_cpu_seconds`).
        for module_name in ("dataset_sources", "official_rule_packs", "cross_stage_localization"):
            module = importlib.import_module(f"app.domain.{module_name}")
            for attribute in ("extract_original_pages", "ocr_page_snapshot", "ocr_original_clip"):
                self.wrap(module, attribute, "parse_ocr")
        from .domain import live_candidate_tagger

        if live_candidate_tagger._page_source_replaced():
            raise RuntimeError("timing instrumentation replaced the live tagger's page source")
        return diagnostics

    def report(self) -> dict[str, float]:
        """Exclusive seconds; `run_process` is what is left of it after the nested stages (matrix load, status
        bookkeeping, evidence-group writes)."""
        return {name: round(value, 2) for name, value in sorted(self.totals.items())}


class _PeakMemory:
    """Peak resident memory of this process tree (the CLI plus live-tagger workers), sampled every 0.5 s, and the
    container's cgroup peak when the kernel exposes it."""

    def __init__(self, interval: float = 0.5) -> None:
        self.interval = interval
        self.peak_tree_bytes = 0
        self.peak_self_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sample()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self) -> None:
        try:
            own, tree = _tree_rss(os.getpid())
        except Exception:  # noqa: BLE001 -- measuring must never break a run
            return
        self.peak_self_bytes = max(self.peak_self_bytes, own)
        self.peak_tree_bytes = max(self.peak_tree_bytes, tree)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "peak_rss_cli_mb": round(self.peak_self_bytes / 2**20, 1),
            "peak_rss_process_tree_mb": round(self.peak_tree_bytes / 2**20, 1),
            "sample_interval_s": self.interval,
        }
        for name in ("memory.peak", "memory.max"):
            path = Path("/sys/fs/cgroup") / name
            try:
                value = path.read_text().strip()
            except OSError:
                continue
            out[f"cgroup_{name.replace('.', '_')}"] = value if not value.isdigit() else f"{int(value) / 2**20:.1f} MB"
        return out


def _tree_rss(root_pid: int) -> tuple[int, int]:
    try:
        import psutil  # dev machines

        root = psutil.Process(root_pid)
        own = root.memory_info().rss
        total = own
        for child in root.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return own, total
    except ImportError:
        pass
    # Linux without psutil (the image): walk /proc
    parents: dict[int, int] = {}
    rss: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        pid = int(entry.name)
        for line in status.splitlines():
            if line.startswith("PPid:"):
                parents[pid] = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                rss[pid] = int(line.split()[1]) * 1024
    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in tree and pid not in tree:
                tree.add(pid)
                changed = True
    return rss.get(root_pid, 0), sum(rss.get(pid, 0) for pid in tree)


def _gpu_report() -> dict[str, Any]:
    torch = sys.modules.get("torch")
    if torch is None:
        return {"torch_imported": False}
    try:
        if not torch.cuda.is_available():
            return {"cuda_available": False}
        return {
            "cuda_available": True,
            "device": torch.cuda.get_device_name(0),
            "peak_vram_allocated_mb": round(torch.cuda.max_memory_allocated() / 2**20, 1),
            "peak_vram_reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20, 1),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _cpu_available() -> Any:
    try:
        from .domain.live_tagger_scan import available_cpu_count

        return available_cpu_count()
    except Exception:  # noqa: BLE001
        return os.cpu_count()


def _tagger_summary(diagnostics: list[Any]) -> Any:
    if not diagnostics or not isinstance(diagnostics[-1], dict):
        return None
    keep = ("workers", "plan_seconds", "scan_seconds", "merge_seconds", "scan_cpu_seconds", "documents_planned",
            "documents_scanned", "pages_scanned", "fragments_created", "documents_failed", "cache_hits", "coverage")
    return {key: diagnostics[-1].get(key) for key in keep if key in diagnostics[-1]}


def _say(message: str) -> None:
    stream = sys.stderr
    try:
        stream.write(message + "\n")
    except UnicodeEncodeError:
        stream.write(message.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    stream.flush()


if __name__ == "__main__":
    sys.exit(main())
