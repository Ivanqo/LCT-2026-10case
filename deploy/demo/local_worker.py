"""Demo-only worker without RabbitMQ: polls the same job table the delivery worker consumes and executes the jobs with
the same `execute_process_job`. Run from `demo.py start` (env prepared there); one job at a time."""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "api_service"), str(REPO)]

from app.db.models import Case10ProcessJob  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.domain.v3_jobs import JOB_QUEUED, _message_payload, execute_process_job, recover_queued_jobs  # noqa: E402


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        recover_queued_jobs(db, publish=False)  # a job left in PROCESSING by a killed stand goes back to the queue
    finally:
        db.close()
    print("local worker: polling", flush=True)
    while True:
        db = SessionLocal()
        try:
            job = (db.query(Case10ProcessJob).filter(Case10ProcessJob.status == JOB_QUEUED)
                   .order_by(Case10ProcessJob.created_at.asc(), Case10ProcessJob.id.asc()).first())
            payload = _message_payload(job) if job is not None else None
            if payload is None:
                time.sleep(1.0)
                continue
            print(f"local worker: job {payload['job_id']} process {payload['process_id']} attempt {payload['attempt']}", flush=True)
            result = execute_process_job(db, payload)
            print(f"local worker: {result.outcome}", flush=True)
        except Exception as exc:  # keep polling; the job row carries the error
            print(f"local worker error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2.0)
        finally:
            db.close()


if __name__ == "__main__":
    main()
