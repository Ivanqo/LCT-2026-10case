#!/usr/bin/env python
"""CASE10 demo stand: one command brings the stand up, warms the caches, runs a package and opens the inspector UI.

    python deploy/demo/demo.py check                 # preflight (OCR, model, sources, ports, flags)
    python deploy/demo/demo.py all tyu               # = stage + start + prepare(backup) + live tyu + open   (the "one command")
    python deploy/demo/demo.py stage tyu             # hard-link the package + build its registry under <home>/packages
    python deploy/demo/demo.py prepare [pkg ...]     # BACKUP: run packages in-process into the stand DB ("[Заготовка] ...")
    python deploy/demo/demo.py start | stop          # API + gateway + UI + local worker (no RabbitMQ needed)
    python deploy/demo/demo.py live tyu              # LIVE: POST /api/case10/batch-runs, wait, print timings, open the UI
    python deploy/demo/demo.py snapshot tyu          # write deploy/demo/expected/<pkg>.json from a finished process
    python deploy/demo/demo.py verify tyu            # compare the last process of <pkg> with the expected file
    python deploy/demo/demo.py reset                 # restore the stand DB to the state right after `prepare`
    python deploy/demo/demo.py open [pkg]            # open the browser on the project/process of <pkg>

The same code path as the delivery image: server-side package + file registry -> `batch_package` -> `run_process`;
the stand only differs in having no RabbitMQ (a local worker polls the same job table). Nothing is keyed to a
package: object ids, paths and expectations come from packages.json and expected/*.json (data, not code).
The Docker variant (image with the working OCR) is deploy/demo/docker-compose.demo.yml, see README_DEMO.md.

Home directory: --home or CASE10_DEMO_HOME (default <repo>/data/demo_stand, git-ignored). Credentials: CASE10_DEMO_LOGIN /
CASE10_DEMO_PASSWORD (default admin / Case10-Demo-2026).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
API_PORT, GATEWAY_PORT, UI_PORT = 8000, 8080, 3100
LOGIN = os.environ.get("CASE10_DEMO_LOGIN", "admin")
PASSWORD = os.environ.get("CASE10_DEMO_PASSWORD", "Case10-Demo-2026")


# ----------------------------------------------------------------------------------------------- configuration
def home_dir(args) -> Path:
    return Path(args.home or os.environ.get("CASE10_DEMO_HOME") or REPO / "data" / "demo_stand").resolve()


def load_config() -> dict:
    config = json.loads((HERE / "packages.json").read_text(encoding="utf-8"))
    roots = {
        "REPO": str(REPO),
        "BENCH_WORK": os.environ.get("CASE10_BENCH_WORK", r"E:\case10_measurement"),
        "NEW_OBJECTS_ROOT": os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted"),
    }
    for package in config["packages"]:
        for key in ("manifest", "source"):
            value = package[key]
            for name, root in roots.items():
                value = value.replace("{" + name + "}", root)
            package[key] = value
    return config


def package_by_id(config: dict, package_id: str) -> dict:
    for package in config["packages"]:
        if package["id"] == package_id:
            return package
    raise SystemExit(f"unknown package {package_id!r}; known: {', '.join(p['id'] for p in config['packages'])}")


def stand_env(home: Path, config: dict) -> dict:
    """Environment of every stand process: flags from packages.json, local data, offline models."""
    data = home / "data"
    env = dict(os.environ)
    env.update(config["flags"])
    env.update({
        "API_DATA_DIR": str(data),
        "CASE10_BATCH_PACKAGES_ROOT": str(home / "packages"),
        "CASE10_ORIGINALS_ROOT": str(home / "packages"),
        # one cache root: tagger, page quality, explication tables and hypothesis tables live in sub-folders of it
        "CASE10_LIVE_TAGGER_CACHE_DIR": os.environ.get("CASE10_LIVE_TAGGER_CACHE_DIR", str(home / "cache")),
        "CASE10_PARAMETER_CODE_STYLE": "matrix11",
        "CASE10_OFFLINE_DELIVERY": "1",
        "API_AUTO_PROCESS_UPLOADS": "1",
        "HF_HUB_OFFLINE": "1",
        "RABBITMQ_URL": "",
        "REDIS_URL": "",
        "CORS_ORIGINS": "*",
        "DEFAULT_ADMIN_LOGIN": LOGIN,
        "DEFAULT_ADMIN_PASSWORD": PASSWORD,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join([str(REPO / "api_service"), str(REPO)]),
    })
    tessdata = Path(os.environ.get("CASE10_TESSDATA", r"C:\Program Files\Tesseract-OCR\tessdata"))
    if tessdata.is_dir():  # a stale machine-wide TESSDATA_PREFIX otherwise points at a removed Tesseract 3.02
        env["TESSDATA_PREFIX"] = str(tessdata)
    return env


def python_exe() -> str:
    venv = REPO / "venv" / "Scripts" / "python.exe"
    if not venv.exists():
        venv = REPO / "venv" / "bin" / "python"
    return str(venv if venv.exists() else sys.executable)


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


# ----------------------------------------------------------------------------------------------- HTTP helpers
def http(method: str, url: str, body=None, token: str | None = None, timeout: float = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def login() -> str:
    return http("POST", f"http://127.0.0.1:{API_PORT}/api/auth/login", {"login": LOGIN, "password": PASSWORD})["access_token"]


def wait_until(predicate, what: str, timeout: float, step: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(step)
    raise SystemExit(f"timeout waiting for {what} ({timeout:.0f}s)")


# ----------------------------------------------------------------------------------------------- check
def cmd_check(args) -> int:
    config, home = load_config(), home_dir(args)
    problems = 0

    def row(ok: bool | None, text: str) -> None:
        nonlocal problems
        mark = {True: "OK  ", False: "FAIL", None: "info"}[ok]
        problems += ok is False
        print(f"[{mark}] {text}")

    row(None, f"home: {home}")
    row(None, "flags: " + ", ".join(f"{k}={v}" for k, v in config["flags"].items()))
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    row(None, f"code: HEAD {git.stdout.strip()}{' + uncommitted changes' if dirty else ''}")
    env = stand_env(home, config)
    code = ("import sys,json; sys.path[:0]=[r'%s', r'%s']; from app.domain.dataset_sources import check_tesseract_health as c; "
            "print(json.dumps(c()))") % (REPO / "api_service", REPO)
    result = subprocess.run([python_exe(), "-c", code], env=env, capture_output=True, text=True, timeout=120)
    try:
        health = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        health = {"available": False, "error": (result.stderr or result.stdout)[-300:]}
    row(bool(health.get("available")), f"Tesseract (OCR сканов): {health.get('version') or health.get('error')}")
    row(shutil.which("node") is not None, "node (шлюз API)")
    row((REPO / "frontend_react" / "dist" / "index.html").exists(), "frontend_react/dist собран (npm --prefix frontend_react run build)")
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    row(any(cache.glob("*MiniLM*")) if cache.exists() else False, f"модель эмбеддингов MiniLM в кэше ({cache})")
    for package in config["packages"]:
        row(Path(package["source"]).is_dir() and Path(package["manifest"]).is_file(),
            f"пакет {package['id']}: источник {package['source']}")
    for port, name in ((API_PORT, "API"), (GATEWAY_PORT, "шлюз"), (UI_PORT, "интерфейс")):
        row(None, f"порт {port} ({name}): {'занят' if port_open(port) else 'свободен'}")
    row(None, "машина: " + f"{os.cpu_count()} логических ядер; на демо-ноутбуке закройте тяжёлые приложения заранее")
    print("\nРезультат:", "готово" if not problems else f"{problems} проблем(ы) — см. FAIL")
    return 1 if problems else 0


# ----------------------------------------------------------------------------------------------- stage
def safe_component(name: str, limit: int = 200) -> str:
    """A path component longer than `limit` UTF-8 bytes cannot exist on a Linux file system (NAME_MAX = 255) and makes a
    Docker bind mount of a Windows folder unreadable (I/O error). Such a name is shortened with a stable hash suffix."""
    if len(name.encode("utf-8")) <= limit:
        return name
    import hashlib

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    short = name
    while len((short + "~" + digest).encode("utf-8")) > limit:
        short = short[:-1]
    return short.rstrip() + "~" + digest


def cmd_stage(args) -> int:
    config, home = load_config(), home_dir(args)
    for package_id in args.packages or [p["id"] for p in config["packages"]]:
        package = package_by_id(config, package_id)
        target = home / "packages" / package_id
        source = Path(package["source"])
        rows = [json.loads(line) for line in Path(package["manifest"]).read_text(encoding="utf-8").splitlines() if line.strip()]
        linked = copied = 0
        for item in rows:
            raw_rel = Path(str(item["relative_path"]).replace("\\", "/"))
            rel = Path(*[safe_component(part) for part in raw_rel.parts])
            item["relative_path"] = rel.as_posix()  # the registry below names the file as it is staged
            src, dst = source / raw_rel, target / rel
            if not src.is_file():
                raise SystemExit(f"{package_id}: файл из манифеста не найден: {src}")
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)  # same volume: instant, no extra disk
                linked += 1
            except OSError:
                shutil.copy2(src, dst)
                copied += 1
        # organizer section codes (OV, VK, ...) -> registry marks (ОВ, ВК, ...); "OTHER" = no registered discipline
        discipline_map = config.get("discipline_map") or {}

        def discipline_of(section) -> str:
            section = str(section or "")
            return discipline_map.get(section, "" if section == "OTHER" else section)

        registry = home / "packages" / f"{package_id}.registry.csv"
        with registry.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            # Perechen 1.1 needs an approval status: without it every edition is "not approved" and the decision layer
            # (rightly) abstains. The demo registry is OURS: it marks every file APPROVED with one date (config
            # "registry_defaults"); in a real package this column is filled by the customer.
            defaults = config.get("registry_defaults") or {}
            writer.writerow(("object_id", "file_id", "file_name", "sha256", "doc_stage", "discipline", "approval_status", "approval_date"))
            for item in rows:
                rel_path = str(item["relative_path"]).replace("\\", "/")
                # the manifest calls a mixed RD+ID folder "RD"; a registry has to say which file is which. Rules in
                # packages.json ("stage_rules": file-name fragment -> stage) are the operator's reading of the names.
                stage = next((rule["stage"] for rule in package.get("stage_rules", []) if rule["match"].lower() in rel_path.lower()),
                             item.get("stage") or "")
                writer.writerow((item["object_id"], item["file_id"], rel_path,
                                 item["sha256"], stage, discipline_of(item.get("section")),
                                 defaults.get("approval_status", ""), defaults.get("approval_date", "")))
        log(f"stage {package_id}: {len(rows)} файлов ({linked} ссылок, {copied} копий), реестр {registry.name}")
    return 0


# ----------------------------------------------------------------------------------------------- start / stop
def pid_file(home: Path) -> Path:
    return home / "stand.pids.json"


def compose_cmd(args, home: Path, config: dict) -> tuple[list[str], dict]:
    """`docker compose` of the delivery stand + demo overlay (flags, packages dir). `--mount-code` additionally bind-mounts
    the working-tree code over the image's /app/app: for checking new code on an OLD image before E1 rebuilds it."""
    files = ["-f", str(REPO / "docker-compose.case10.yml"), "-f", str(HERE / "docker-compose.demo.yml")]
    if getattr(args, "mount_code", False):
        files += ["-f", str(HERE / "docker-compose.demo.code.yml")]
    env = {**os.environ, **config["flags"], "CASE10_PACKAGES_DIR": str(home / "packages"), "DOCKER_BIND_IP": "127.0.0.1"}
    return ["docker", "compose", *files], env


def cmd_start(args) -> int:
    config, home = load_config(), home_dir(args)
    if port_open(API_PORT) or port_open(UI_PORT):
        log("стенд уже запущен (порты заняты); `stop` его остановит")
        return 0
    if getattr(args, "docker", False):
        (home / "packages").mkdir(parents=True, exist_ok=True)
        base, env = compose_cmd(args, home, config)
        # the UI container is not started: the local ui_server serves the same build and adds /go (open on the right object)
        subprocess.run([*base, "up", "-d", "api", "case10_worker", "rabbitmq", "node_gateway"], cwd=REPO, env=env, check=True)
        wait_until(lambda: http("GET", f"http://127.0.0.1:{API_PORT}/health", timeout=3) is not None, "API /health (Docker)", 240)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        stream = (home / "logs" / "ui.log").open("ab")
        ui = subprocess.Popen([python_exe(), str(HERE / "ui_server.py"), "--port", str(UI_PORT), "--dist", str(REPO / "frontend_react" / "dist"),
                               "--api", f"http://127.0.0.1:{API_PORT}"], cwd=REPO, env=env, stdout=stream, stderr=subprocess.STDOUT)
        pid_file(home).write_text(json.dumps({"ui": ui.pid, "docker": 1}), encoding="utf-8")
        wait_until(lambda: port_open(UI_PORT), "UI", 30)
        log(f"стенд в Docker запущен: UI http://127.0.0.1:{UI_PORT}  API http://127.0.0.1:{API_PORT}/docs  (вход {LOGIN})")
        return 0
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    env = stand_env(home, config)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    procs = {}

    def spawn(name: str, cmd: list[str], extra_env: dict | None = None) -> None:
        stream = (home / "logs" / f"{name}.log").open("ab")
        procs[name] = subprocess.Popen(cmd, cwd=REPO, env={**env, **(extra_env or {})}, stdout=stream, stderr=subprocess.STDOUT,
                                       creationflags=flags).pid

    spawn("api", [python_exe(), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT),
                  "--app-dir", str(REPO / "api_service")])
    wait_until(lambda: http("GET", f"http://127.0.0.1:{API_PORT}/health", timeout=3) is not None, "API /health", 180)
    spawn("worker", [python_exe(), str(HERE / "local_worker.py")])
    if shutil.which("node"):
        spawn("gateway", ["node", str(REPO / "node_gateway" / "server.js")],
              {"PYTHON_API_BASE_URL": f"http://127.0.0.1:{API_PORT}", "PORT": str(GATEWAY_PORT)})
    spawn("ui", [python_exe(), str(HERE / "ui_server.py"), "--port", str(UI_PORT), "--dist", str(REPO / "frontend_react" / "dist"),
                 "--api", f"http://127.0.0.1:{API_PORT}"])
    pid_file(home).write_text(json.dumps(procs), encoding="utf-8")
    wait_until(lambda: port_open(UI_PORT) and port_open(GATEWAY_PORT), "gateway/UI", 60)
    log(f"стенд запущен: UI http://127.0.0.1:{UI_PORT}  API http://127.0.0.1:{API_PORT}/docs  (вход {LOGIN})")
    return 0


def cmd_stop(args) -> int:
    home = home_dir(args)
    path = pid_file(home)
    if path.exists():
        pids = json.loads(path.read_text(encoding="utf-8"))
        if pids.pop("docker", None):
            base, env = compose_cmd(args, home, load_config())
            subprocess.run([*base, "stop"], cwd=REPO, env=env)  # `stop`, not `down -v`: the data volume (and the backup) stays
        for name, pid in pids.items():
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        path.unlink()
        log("стенд остановлен")
    else:
        log("pid-файл не найден; ничего не остановлено")
    return 0


# ----------------------------------------------------------------------------------------------- prepare (backup)
CHILD = r'''
import json, sys, time
from pathlib import Path
from app.db.session import SessionLocal, init_db
from app.db.models import Organization, Project, User
from app import batch_package as bp
from app.config import settings
from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, run_process

spec = json.loads(sys.argv[1])
init_db()
db = SessionLocal()
admin = db.query(User).filter(User.login == spec["login"]).one()
org = db.get(Organization, int(admin.organization_id))
root = settings.BATCH_PACKAGES_ROOT
name = spec["project"]
project = db.query(Project).filter(Project.organization_id == org.id, Project.name == name).first()
if project is None:
    project = Project(name=name, description="Demo stand (backup)", organization_id=org.id)
    db.add(project); db.commit(); db.refresh(project)
t0 = time.monotonic()
package_root, package_name = bp.resolve_package(root / spec["package"], unpack_root=root / "_unpacked",
                                                max_unpacked_bytes=settings.BATCH_MAX_UNPACKED_BYTES)
registry_path = root / spec["registry"]
plan = bp.build_plan(bp.scan_package(package_root), bp.read_registry(registry_path), package_name=package_name,
                     object_id=spec["object_id"], registry_sha256=bp.sha256_file(registry_path),
                     originals_prefix=package_root.relative_to(root).as_posix())
manifest = settings.DATA_DIR / "batch_manifests" / (spec["package"] + ".jsonl")
documents = bp.import_plan(db, plan, project_id=project.id, organization_id=org.id, manifest_path=manifest)
process = create_process(db, project_id=project.id, organization_id=org.id, object_id=plan.object_id,
                         matrix_version=MATRIX_VERSION_OFFICIAL)
db.commit()
t1 = time.monotonic()
run_process(db, process_id=process.id, user_id=int(admin.id))
print("RESULT " + json.dumps({"project_id": project.id, "process_id": process.id, "documents": documents,
                              "import_s": round(t1 - t0, 1), "run_s": round(time.monotonic() - t1, 1)}, default=str))
'''


def cmd_prepare(args) -> int:
    config, home = load_config(), home_dir(args)
    if port_open(API_PORT):
        raise SystemExit("prepare пишет в базу стенда напрямую: остановите стенд (`stop`) или запустите prepare до `start`")
    for package_id in args.packages or [p["id"] for p in config["packages"]]:
        package = package_by_id(config, package_id)
        env = {**stand_env(home, config), **package.get("flags", {})}  # a package may override flags (hypothesis scenario)
        cmd_stage(argparse.Namespace(home=args.home, packages=[package_id]))  # idempotent: links + registry
        spec = {"login": LOGIN, "project": f"[Заготовка] {package['title']}", "package": package_id,
                "registry": f"{package_id}.registry.csv", "object_id": package["object_id"]}
        log(f"prepare {package_id}: прогон процесса (заготовка для запасного сценария)")
        started = time.monotonic()
        result = subprocess.run([python_exe(), "-c", CHILD, json.dumps(spec, ensure_ascii=False)], cwd=REPO / "api_service", env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
        line = next((ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if result.returncode or not line:
            print(result.stdout[-1500:], result.stderr[-1500:])
            raise SystemExit(f"prepare {package_id} не удался (код {result.returncode})")
        info = json.loads(line[7:])
        log(f"prepare {package_id}: готово за {time.monotonic() - started:.0f} с (импорт {info['import_s']} с, процесс {info['run_s']} с) "
            f"project={info['project_id']} process={info['process_id']}")
        (home / f"prepared_{package_id}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    pristine = home / "data" / "api.pristine.db"
    import sqlite3
    src, dst = sqlite3.connect(str(home / "data" / "api.db")), sqlite3.connect(str(pristine))
    src.backup(dst)
    src.close(); dst.close()
    log(f"состояние сохранено для `reset`: {pristine}")
    return 0


def cmd_reset(args) -> int:
    home = home_dir(args)
    was_running = port_open(API_PORT)
    if was_running:
        cmd_stop(args)
        time.sleep(2)
    import sqlite3
    src, dst = sqlite3.connect(str(home / "data" / "api.pristine.db")), sqlite3.connect(str(home / "data" / "api.db"))
    src.backup(dst)
    src.close(); dst.close()
    log("база возвращена к состоянию после prepare (решения инспектора и живые прогоны удалены)")
    if was_running:
        cmd_start(args)
    return 0


# ----------------------------------------------------------------------------------------------- live
def cmd_live(args) -> int:
    config, home = load_config(), home_dir(args)
    package = package_by_id(config, args.package)
    if not port_open(API_PORT):
        raise SystemExit("стенд не запущен: `start`")
    cmd_stage(argparse.Namespace(home=args.home, packages=[args.package]))
    token = login()
    base = f"http://127.0.0.1:{API_PORT}"
    name = (f"[Заготовка] {package['title']}" if args.backup else f"[Живой {time.strftime('%H:%M')}] {package['title']}")
    project = http("POST", f"{base}/api/projects", {"name": name}, token)
    started = time.monotonic()
    log(f"live {args.package}: пакет + реестр -> /api/case10/batch-runs (ожидание ≈ {package['live_seconds_warm']} с на тёплом кэше)")
    run = http("POST", f"{base}/api/case10/batch-runs", {"project_id": project["id"], "package": args.package,
                                                           "registry": f"{args.package}.registry.csv",
                                                           "object_id": package["object_id"]}, token, timeout=600)
    process_id = run["process"]["process_id"]
    log(f"  документов принято: {run['documents_imported']}; реестр: {run['package'].get('registry_status', '?')}; процесс {process_id}")
    last = ""

    def done() -> bool:
        nonlocal last
        status = http("GET", f"{base}/api/case10/processes/{process_id}/status", token=token)["status"]
        if status != last:
            log(f"  статус: {status} (+{time.monotonic() - started:.0f} с)")
            last = status
        if status == "FAILED":
            raise SystemExit("процесс завершился FAILED: см. data/logs; запасной сценарий — `open` заготовки")
        return status in {"READY", "VERIFYING", "COMPLETED", "FINALIZED"}

    wait_until(done, "завершение процесса", args.timeout, step=2)
    elapsed = time.monotonic() - started
    log(f"live {args.package}: готово за {elapsed:.0f} с")
    (home / (f"prepared_{args.package}.json" if args.backup else f"live_{args.package}.json")).write_text(json.dumps(
        {"project_id": project["id"], "process_id": process_id, "seconds": round(elapsed, 1)}), encoding="utf-8")
    if not args.no_open and not args.backup:
        open_ui(home, project["id"], process_id)
    return 0


# ----------------------------------------------------------------------------------------------- snapshot / verify / open
def queue_of(process_id: str, token: str) -> list[dict]:
    summary = http("GET", f"http://127.0.0.1:{API_PORT}/api/case10/processes/{process_id}/workbench", token=token, timeout=120)
    rows = summary.get("queue") or summary.get("items") or summary.get("groups") or []
    return rows


def digest(rows: list[dict]) -> dict:
    keep = [r for r in rows if r.get("evidence_tier") or r.get("finding_status") in {"CANDIDATE", "SUSPICION"}]
    items = sorted((str(r.get("matrix_code") or r.get("parameter_code") or r.get("code")), str(r.get("location") or r.get("entity_name") or ""),
                    str(r.get("evidence_tier") or r.get("finding_status") or "")) for r in keep)  # a list: repeats count
    counts: dict[str, int] = {}
    for _, _, tier in items:
        counts[tier] = counts.get(tier, 0) + 1
    return {"counts": counts, "items": [list(item) for item in items]}


def latest_process(home: Path, package_id: str, prefer: str) -> tuple[int, str]:
    for name in ((f"live_{package_id}.json", f"prepared_{package_id}.json") if prefer == "live" else
                 (f"prepared_{package_id}.json", f"live_{package_id}.json")):
        path = home / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data["project_id"]), str(data["process_id"])
    raise SystemExit(f"для {package_id} нет ни live, ни prepared прогона")


def cmd_snapshot(args) -> int:
    home = home_dir(args)
    _, process_id = latest_process(home, args.package, args.prefer)
    data = digest(queue_of(process_id, login()))
    out = HERE / "expected" / f"{args.package}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"package": args.package, "process_id_when_recorded": process_id, **data}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    log(f"snapshot {args.package}: {data['counts']} -> {out}")
    return 0


def cmd_verify(args) -> int:
    home = home_dir(args)
    _, process_id = latest_process(home, args.package, args.prefer)
    expected = json.loads((HERE / "expected" / f"{args.package}.json").read_text(encoding="utf-8"))
    actual = digest(queue_of(process_id, login()))
    from collections import Counter

    want, got = Counter(tuple(i) for i in expected["items"]), Counter(tuple(i) for i in actual["items"])
    print(f"ожидалось {sum(want.values())}, получено {sum(got.values())}")
    for item in sorted((want - got).elements()):
        print("  НЕТ  ", item)
    for item in sorted((got - want).elements()):
        print("  ЛИШНЕЕ", item)
    print("СОВПАДАЕТ" if want == got else "РАСХОЖДЕНИЕ — проверьте код/флаги/кэш до показа")
    return 0 if want == got else 1


def open_ui(home: Path, project_id: int, process_id: str) -> None:
    url = f"http://127.0.0.1:{UI_PORT}/go?project={project_id}&process={process_id}"
    log(f"интерфейс: {url}")
    webbrowser.open(url)


def cmd_open(args) -> int:
    home = home_dir(args)
    project_id, process_id = latest_process(home, args.package, args.prefer)
    open_ui(home, project_id, process_id)
    return 0


def cmd_all(args) -> int:
    """The one command: package staged, backup scenario prepared (once), stand up, live run, UI opened on it."""
    home = home_dir(args)
    cmd_stage(argparse.Namespace(home=args.home, packages=[args.package]))
    if getattr(args, "docker", False):
        cmd_start(args)
        if not (home / f"prepared_{args.package}.json").exists():
            backup = argparse.Namespace(**{**vars(args), "backup": True, "no_open": True})
            cmd_live(backup)  # Docker keeps its own DB in a volume: the backup is a prepared run through the same API
    else:
        if not (home / "data" / "api.pristine.db").exists():
            cmd_prepare(argparse.Namespace(home=args.home, packages=[args.package], timeout=args.timeout))
        cmd_start(args)
    return cmd_live(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home")
    parser.add_argument("--docker", action="store_true", help="start/stop the stand with docker compose (image with working OCR)")
    parser.add_argument("--mount-code", action="store_true", help="with --docker: mount the working-tree code over the image's /app/app")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, func in (("check", cmd_check), ("start", cmd_start), ("stop", cmd_stop), ("reset", cmd_reset)):
        sub.add_parser(name).set_defaults(func=func)
    for name, func in (("stage", cmd_stage), ("prepare", cmd_prepare)):
        item = sub.add_parser(name)
        item.add_argument("packages", nargs="*")
        item.add_argument("--timeout", type=int, default=1800)
        item.set_defaults(func=func)
    for name, func in (("live", cmd_live), ("all", cmd_all)):
        item = sub.add_parser(name)
        item.add_argument("package")
        item.add_argument("--timeout", type=int, default=900)
        item.add_argument("--no-open", action="store_true")
        item.add_argument("--backup", action="store_true", help="name the project '[Заготовка]' (Docker mode: prepared run instead of in-process prepare)")
        item.set_defaults(func=func)
    for name, func in (("snapshot", cmd_snapshot), ("verify", cmd_verify), ("open", cmd_open)):
        item = sub.add_parser(name)
        item.add_argument("package")
        item.add_argument("--prefer", choices=("live", "prepared"), default="live")
        item.set_defaults(func=func)
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
