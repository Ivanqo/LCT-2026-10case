"""Phase 12 / S5: serve the inspector-workbench demo locally (no Docker): the FastAPI app on the demo database built
by `s5_demo_seed.py` + the Node gateway in front of it, the way `docker-compose.yml` wires them.

Usage (repo root as CWD):
    python evaluation/phase12/s5_demo_serve.py [--data-dir data/s5_demo] [--api-port 8000] [--gateway-port 8080] [--reset]
Frontend: `npm --prefix frontend_react run dev` -> http://127.0.0.1:5173 (talks to the gateway on :8080).
Originals are looked up (SHA-256-checked) under every root in CASE10_ORIGINALS_ROOTS; defaults cover the demo objects.
"""
from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_WORK = Path(os.environ.get("CASE10_BENCH_WORK", r"E:\case10_measurement"))
NEW_OBJECTS_ROOT = Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"))
DEFAULT_ROOTS = [BENCH_WORK / "public_docs" / "TYU", BENCH_WORK / "public_docs" / "NOV",
                 NEW_OBJECTS_ROOT / "Алтуфьевское, 79Б", NEW_OBJECTS_ROOT / "Полярная, 17", NEW_OBJECTS_ROOT / "Лосевская, 3А"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data" / "s5_demo")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--gateway-port", type=int, default=8080)
    parser.add_argument("--reset", action="store_true",
                        help="restore api.db from api.pristine.db (the seeded state before any inspector action)")
    args = parser.parse_args()
    if args.reset:
        import sqlite3

        pristine = args.data_dir / "api.pristine.db"
        source, target = sqlite3.connect(str(pristine)), sqlite3.connect(str(args.data_dir / "api.db"))
        source.backup(target)
        source.close()
        target.close()

    roots = [str(root) for root in DEFAULT_ROOTS if root.is_dir()]
    os.environ.update({
        "API_DATA_DIR": str(args.data_dir.resolve()),
        "CASE10_ORIGINALS_ROOTS": os.pathsep.join(filter(None, [os.environ.get("CASE10_ORIGINALS_ROOTS", ""), *roots])),
        "HF_HUB_OFFLINE": "1",
        "CASE10_LLM_VERIFIER_ENABLED": "0",
        "CORS_ORIGINS": os.environ.get("CORS_ORIGINS", "*"),
        "PYTHONIOENCODING": "utf-8",
    })
    os.environ.pop("CASE10_ORIGINALS_ROOT", None)  # one root would shadow the others; the list above covers them all
    gateway_env = {**os.environ, "PYTHON_API_BASE_URL": f"http://127.0.0.1:{args.api_port}", "PORT": str(args.gateway_port)}
    gateway = subprocess.Popen(["node", str(REPO_ROOT / "node_gateway" / "server.js")], env=gateway_env, cwd=str(REPO_ROOT))
    atexit.register(gateway.terminate)

    sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "api_service")]
    os.chdir(REPO_ROOT)
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=args.api_port, log_level="info")


if __name__ == "__main__":
    main()
