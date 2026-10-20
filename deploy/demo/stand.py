#!/usr/bin/env python
"""One-command Docker demo stand: delivery compose + the demo packages (ALT79B, NOV) already checked and loaded.

    python deploy/demo/stand.py up        # stage packages, build/start the stand, run both packages, print the URLs
    python deploy/demo/stand.py status    # containers, health, the two demo checks
    python deploy/demo/stand.py down      # stop the containers (the data volume with the finished checks stays)
    python deploy/demo/stand.py reset     # delete the stand data (down -v) and bring it up again with fresh checks

Stdlib only; run it from anywhere (it finds the repository next to itself). It uses `docker-compose.case10.yml` as it is,
under its own compose project `case10demo`, so it never touches containers or volumes of another stand. Image tag:
`CASE10_IMAGE_TAG` (default `hostfix`); a missing image is built, an existing one is reused. Ports: the compose variables
`API_HOST_PORT` (8000), `GATEWAY_HOST_PORT` (8080), `UI_HOST_PORT` (3100); the UI is built for the gateway port you choose.
The package sources (organizer data, not in the repository) are found as in `demo.py`: `CASE10_BENCH_WORK` (NOV) and
`CASE10_NEW_OBJECTS_ROOT` (ALT79B). Login: `CASE10_DEMO_LOGIN` / `CASE10_DEMO_PASSWORD` (default admin / Case10-Demo-2026).
`--home DIR` (or `CASE10_DEMO_HOME`) is where the staged packages live (default `<repo>/data/demo_stand`, ignored by git).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import demo  # noqa: E402  (staging of the packages and their registries is shared with demo.py)

PROJECT = "case10demo"
COMPOSE_FILE = REPO / "docker-compose.case10.yml"
LOGIN = os.environ.get("CASE10_DEMO_LOGIN", "admin")
PASSWORD = os.environ.get("CASE10_DEMO_PASSWORD", "Case10-Demo-2026")
DEMO_PACKAGES = ("nov", "alt79b")  # the project list is newest first: ALT79B (created last) is the one the UI opens
READY_STATES = {"READY", "VERIFYING", "COMPLETED", "FINALIZED"}
TAG = os.environ.get("CASE10_IMAGE_TAG", "hostfix")
API_PORT = int(os.environ.get("API_HOST_PORT", "8000"))
GATEWAY_PORT = int(os.environ.get("GATEWAY_HOST_PORT", "8080"))
UI_PORT = int(os.environ.get("UI_HOST_PORT", "3100"))


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # a cp1251 console must not stop the script on a character it cannot show
    except Exception:
        pass


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def home_of(args) -> Path:
    return Path(args.home or os.environ.get("CASE10_DEMO_HOME") or REPO / "data" / "demo_stand").resolve()


def compose(args, *tail: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, "CASE10_IMAGE_TAG": TAG, "CASE10_PACKAGES_DIR": str(home_of(args) / "packages"),
           "API_HOST_PORT": str(API_PORT), "GATEWAY_HOST_PORT": str(GATEWAY_PORT), "UI_HOST_PORT": str(UI_PORT)}
    if GATEWAY_PORT != 8080:  # the UI image is built for the gateway address; the default resolves it at run time
        env.setdefault("REACT_FRONTEND_API_BASE", f"http://127.0.0.1:{GATEWAY_PORT}")
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *tail]
    return subprocess.run(cmd, cwd=REPO, env=env, check=check, text=True, encoding="utf-8", errors="replace",
                          capture_output=capture)


def call(method: str, path: str, body=None, token: str | None = None, timeout: float = 60, port: int | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port or API_PORT}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def wait_for(predicate, what: str, timeout: float, step: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(step)
    raise SystemExit(f"timeout waiting for {what} ({timeout:.0f} s)")


# ------------------------------------------------------------------------------------------------ docker preflight
def docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_exists(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def container_network_ok() -> bool:
    """A build downloads packages from inside a container; right after Docker Desktop starts that fails with
    'Connection refused' for a minute or two."""
    probe = subprocess.run(["docker", "run", "--rm", "python:3.11-slim", "python", "-c",
                            "import urllib.request;urllib.request.urlopen('http://deb.debian.org/debian/',timeout=10)"],
                           capture_output=True, timeout=180)
    return probe.returncode == 0


def preflight(args) -> None:
    if not docker_ready():
        raise SystemExit("Docker не отвечает: запустите Docker Desktop, дождитесь зелёного значка и повторите команду")
    needs_build = not all(image_exists(f"case10-{name}:{TAG}") for name in ("api", "gateway", "ui"))
    if needs_build:
        log(f"образов case10-*:{TAG} нет: будет сборка (при пустом кэше слоёв около 30 мин, нужна сеть)")
        for attempt in range(1, 6):
            if container_network_ok():
                break
            log(f"сеть из контейнера ещё не готова (попытка {attempt}/5), жду 30 с - обычно проходит через 1–2 минуты после старта Docker")
            time.sleep(30)
        else:
            raise SystemExit("из контейнера нет сети (Connection refused). Перезапустите Docker Desktop (wsl --shutdown) и повторите")


# ------------------------------------------------------------------------------------------------ demo checks
def demo_projects(config: dict) -> list[dict]:
    return [{"package": demo.package_by_id(config, pid), "title": "Демо: " + demo.package_by_id(config, pid)["title"].split(" — ")[0]}
            for pid in DEMO_PACKAGES]


def ensure_checks(config: dict) -> None:
    token = call("POST", "/api/auth/login", {"login": LOGIN, "password": PASSWORD})["access_token"]
    projects = {p["name"]: p for p in call("GET", "/api/projects", token=token)}
    pending = []
    for item in demo_projects(config):
        package, title = item["package"], item["title"]
        project = projects.get(title) or call("POST", "/api/projects", {"name": title}, token)
        existing = call("GET", f"/api/case10/processes?project_id={project['id']}", token=token)
        if any(row.get("status") in READY_STATES for row in existing):
            log(f"{title}: проверка уже есть")
            continue
        run = call("POST", "/api/case10/batch-runs", {"project_id": project["id"], "package": package["id"],
                                                       "registry": f"{package['id']}.registry.csv",
                                                       "object_id": package["object_id"]}, token, timeout=600)
        process_id = run["process"]["process_id"]
        log(f"{title}: принято документов {run['documents_imported']}, реестр {run['package'].get('registry_status', '?')}, процесс {process_id[:8]}")
        pending.append((title, process_id))
    started = time.monotonic()
    for title, process_id in pending:
        def done() -> bool:
            status = call("GET", f"/api/case10/processes/{process_id}/status", token=token)["status"]
            if status == "FAILED":
                raise SystemExit(f"{title}: проверка завершилась с ошибкой (docker compose -p {PROJECT} logs case10_worker)")
            return status in READY_STATES
        wait_for(done, f"проверка «{title}»", 1500, step=3)
        log(f"{title}: готово (+{time.monotonic() - started:.0f} с)")


def print_urls() -> None:
    log(f"интерфейс  http://127.0.0.1:{UI_PORT}   вход {LOGIN} / {PASSWORD}")
    log(f"API        http://127.0.0.1:{API_PORT}/docs   шлюз http://127.0.0.1:{GATEWAY_PORT}/openapi.json")
    log("в интерфейсе: проект «Демо: Алтуфьевское, 79Б», вкладка «Проверка» (карточка M-002); «Панель объектов» — оба объекта")


# ------------------------------------------------------------------------------------------------ commands
def cmd_up(args) -> int:
    config = demo.load_config()
    preflight(args)
    home = home_of(args)
    (home / "packages").mkdir(parents=True, exist_ok=True)
    log("пакеты демо: ссылки и реестры в " + str(home / "packages"))
    demo.cmd_stage(argparse.Namespace(home=str(home), packages=list(DEMO_PACKAGES)))
    log(f"docker compose -p {PROJECT} build, затем up -d (тег образов {TAG})")
    compose(args, "build")  # build first: `up --build` would try to pull the worker image case10-api:<tag> from Docker Hub
    compose(args, "up", "-d")
    wait_for(lambda: call("GET", "/health", timeout=5) is not None, "API /health", 300)
    ensure_checks(config)
    print_urls()
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{UI_PORT}")
    return 0


def cmd_status(args) -> int:
    compose(args, "ps", check=False)
    try:
        log("health: " + json.dumps(call("GET", "/health", timeout=5), ensure_ascii=False))
        token = call("POST", "/api/auth/login", {"login": LOGIN, "password": PASSWORD})["access_token"]
        for project in call("GET", "/api/projects", token=token):
            rows = call("GET", f"/api/case10/processes?project_id={project['id']}", token=token)
            log(f"проект «{project['name']}»: " + (", ".join(f"{r['process_id'][:8]} {r['status']}" for r in rows) or "проверок нет"))
    except (urllib.error.URLError, OSError) as exc:
        log(f"стенд не отвечает на порту {API_PORT}: {exc}")
    return 0


def cmd_down(args) -> int:
    compose(args, "stop")
    log("стенд остановлен; данные сохранены (`up` поднимет его с готовыми проверками)")
    return 0


def cmd_reset(args) -> int:
    compose(args, "down", "-v")
    log("данные стенда удалены, поднимаю заново")
    return cmd_up(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", help="папка пакетов демо (по умолчанию <repo>/data/demo_stand)")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--open", action="store_true", help="открыть интерфейс в браузере")
    up.set_defaults(func=cmd_up)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("down").set_defaults(func=cmd_down)
    reset = sub.add_parser("reset")
    reset.add_argument("--open", action="store_true")
    reset.set_defaults(func=cmd_reset)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
