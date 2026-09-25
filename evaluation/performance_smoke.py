"""Small CASE10 HTTP performance smoke for key API/protocol limits."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--project-name", default="CASE10 performance smoke")
    parser.add_argument("--object-id", default="OBJ-NOVOSLOBODSKAYA")
    parser.add_argument("--api-p95-limit-sec", type=float, default=0.25)
    parser.add_argument("--start-limit-sec", type=float, default=2.0)
    parser.add_argument("--process-limit-sec", type=float, default=150.0)
    parser.add_argument("--protocol-limit-sec", type=float, default=40.0)
    parser.add_argument("--poll-timeout-sec", type=float, default=240.0)
    args = parser.parse_args()
    headers = {"Content-Type": "application/json"}

    def request(method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(args.base_url + path, data=data, headers=headers, method=method)
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=900) as response:
            payload = json.load(response)
        return payload, time.perf_counter() - started

    login, _ = request("POST", "/auth/login", {
        "login": os.getenv("DEFAULT_ADMIN_LOGIN", "admin"),
        "password": os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin123!ChangeMe"),
    })
    headers["Authorization"] = "Bearer " + login["access_token"]
    projects, _ = request("GET", "/projects")
    project = next((row for row in projects if row["name"] == args.project_name), None)
    if project is None:
        project, _ = request("POST", "/projects", {"name": args.project_name, "description": "CASE10 performance smoke"})
    project_id = project["id"]
    request("POST", f"/case10/projects/{project_id}/official-dataset/import", {"run_processes": False})

    process, start_seconds = request("POST", f"/case10/projects/{project_id}/processes", {"object_id": args.object_id})
    process_id = process["process_id"]
    status_timings = []
    ready_started = time.perf_counter()
    while True:
        status, elapsed = request("GET", f"/case10/processes/{process_id}/status")
        status_timings.append(elapsed)
        if status["status"] == "READY":
            break
        if status["status"] == "FAILED":
            raise AssertionError(status)
        if time.perf_counter() - ready_started > args.poll_timeout_sec:
            raise TimeoutError(f"Process {process_id} did not reach READY within {args.poll_timeout_sec}s: {status}")
        time.sleep(0.5)
    process_seconds = time.perf_counter() - ready_started
    protocol, protocol_seconds = request("GET", f"/case10/protocols/current?project_id={project_id}&process_id={process_id}")
    submission, submission_seconds = request("GET", f"/case10/protocols/{protocol['id']}/submission")
    assert not submission["validation_errors"], submission

    for _ in range(20):
        _, elapsed = request("GET", f"/case10/processes/{process_id}/status")
        status_timings.append(elapsed)
    p95 = sorted(status_timings)[int(len(status_timings) * 0.95) - 1]
    result = {
        "project_id": project_id,
        "object_id": args.object_id,
        "process_id": process_id,
        "protocol_id": protocol["id"],
        "start_seconds": start_seconds,
        "process_seconds": process_seconds,
        "protocol_seconds": protocol_seconds,
        "submission_seconds": submission_seconds,
        "status_endpoint_p95_seconds": p95,
        "status_endpoint_mean_seconds": statistics.mean(status_timings),
        "limits": {
            "start_seconds": args.start_limit_sec,
            "process_seconds": args.process_limit_sec,
            "protocol_seconds": args.protocol_limit_sec,
            "api_p95_seconds": args.api_p95_limit_sec,
        },
    }
    assert start_seconds <= args.start_limit_sec, result
    assert process_seconds <= args.process_limit_sec, result
    assert protocol_seconds <= args.protocol_limit_sec, result
    assert submission_seconds <= args.protocol_limit_sec, result
    assert p95 <= args.api_p95_limit_sec, result
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
