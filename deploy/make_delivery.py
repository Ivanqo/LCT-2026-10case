"""Build the CASE10 technical delivery bundle and check its hygiene (Phase 12, S6).

    python deploy/make_delivery.py bundle [--ref HEAD] [--out dist]     # dist/case10-delivery-<sha>/ + .zip
    python deploy/make_delivery.py check-image case10-api:latest       # what is inside the delivery image
    python deploy/make_delivery.py scan-code                            # object/file-name literals in app code

`bundle` takes ONLY files tracked by git at `--ref` (untracked data can never slip in) and only those on the
whitelist below: code, Dockerfiles, compose files, deployment docs, README_GRADER.md, LICENSES_MODELS.md and one
example output. Internal reports (CASE10_*.md, evaluation reports), agent memory (.claude), organizer data
(case_data, learning_data, *.pdf, gold/label files), the legacy DocuRAG services and secrets are not whitelisted;
the scan after copying fails the build if any of them got in anyway. `scan-code` lists literals in the shipped
code that name a concrete object, file id or package path of the development corpus -- the "no dependency on
concrete file and object names" requirement -- as a report for the integrator (the mechanisms are not S6's code).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

REPO = Path(__file__).resolve().parents[1]

INCLUDE = (
    "api_service/app/**", "api_service/alembic/**", "api_service/alembic.ini", "api_service/requirements.txt",
    "api_service/Dockerfile", "api_service/tests/**",
    "evaluation/__init__.py", "evaluation/metrics.py", "evaluation/exporter.py", "evaluation/fixtures.py",
    "evaluation/candidate_coverage.py", "evaluation/grader_sim.py",
    "evaluation/phase12/example_submission_gold11.json", "evaluation/phase12/submission_schema.json",
    "frontend_react/**", "node_gateway/**",
    "deploy/**",
    "docker-compose.case10.yml", "docker-compose.case10.gpu.yml", ".dockerignore", ".gitignore",
    "README_GRADER.md", "LICENSES_MODELS.md", "DEPLOYMENT.md",
)
EXCLUDE = (
    "**/__pycache__/**", "**/*.pyc", "frontend_react/node_modules/**", "frontend_react/dist/**",
    "deploy/nginx/templates/**",           # legacy DocuRAG site (landing + RAG app), not the CASE10 stand
    "deploy/bootstrap-ubuntu.sh",
)
# Anything matching these must not be in the bundle, whatever the whitelist says.
FORBIDDEN_PATHS = (
    "case_data/*", "learning_data/*", "New_data/*", "*.pdf", "*.zip", "*.rar", "*.7z", "*.db", "*.sqlite*",
    ".claude/*", "*/memory/*", "*MEMORY.md", "CASE10_*.md", "*REPORT*.md", "*BACKLOG*",
    "evaluation/silver*", "evaluation/reports/*", "evaluation/audits/*", "evaluation/measurement_bench/*",
    "*initial_admin_credentials*", "*.env", "FreeQwenApi/*", "shared/*", "rag_service/*", "ifc_service/*",
    "landing/*",
)
# Data files a delivery may carry (everything else that looks like data is flagged).
ALLOWED_DATA = (
    "api_service/app/reference_data/case_data/parameter_catalog_132.jsonl",
    "api_service/app/reference_data/case_data/submission_schema.json",
    "api_service/app/domain/matrix_v11_codes.json",
    "api_service/app/domain/anchor_vocab/*.json",
    "evaluation/phase12/example_submission_gold11.json", "evaluation/phase12/submission_schema.json",
    "node_gateway/openapi.json", "node_gateway/package.json", "frontend_react/package.json",
    "frontend_react/package-lock.json", "frontend_react/.eslintrc.json",
)
# Content markers of organizer answer files / closed materials and of agent memory.
FORBIDDEN_CONTENT = (
    re.compile(r'"violation_label"\s*:\s*"VIOLATION_PRESENT".*"gold', re.I),
    re.compile(r"^---\s*\nname:\s*case10", re.M),                      # memory file front matter
    re.compile("origin" + "SessionId"),                                # memory metadata (split: no self-match)
)
# Literals naming the development corpus: objects, organizer file ids, package-specific paths.
CORPUS_LITERALS = re.compile(
    r"OBJ-(?:TYUMENSKAYA|NOVOSLOBODSKAYA|RECHNIKOV|NEW-)[A-Z0-9-]*|\b(?:LOS3A|DOO25|ALT79B|OKT103|IZM12|POL1[67]|SOSH25|UNDMS)\b"
    r"|\bF0\d{3}\b|РД-ОВ1|Тюменск|Новослободск|Речников|Лосевск|Алтуфьевск|Октябрьская 103|Изумрудн|Полярн"
)


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True).stdout


def _match(path: str, patterns) -> bool:
    """fnmatch semantics: `*` also crosses `/`, so `dir/**` is everything below `dir`."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def bundle(ref: str, out: Path) -> int:
    sha = _git("rev-parse", "--short=12", ref).decode().strip()
    files = [line for line in _git("ls-tree", "-r", "--name-only", ref).decode("utf-8").splitlines()]
    chosen = sorted(path for path in files if _match(path, INCLUDE) and not _match(path, EXCLUDE))
    target = out / f"case10-delivery-{sha}"
    if target.exists():
        import shutil

        shutil.rmtree(target)
    manifest = []
    for path in chosen:
        data = _git("show", f"{ref}:{path}")
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
    (target / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    problems = scan_tree(target, chosen)
    report = {"ref": ref, "commit": sha, "files": len(chosen), "problems": problems,
              "bytes": sum((target / p).stat().st_size for p in chosen)}
    (out / f"case10-delivery-{sha}.check.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if problems:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print("BUNDLE REJECTED: forbidden content", file=sys.stderr)
        return 1
    archive = out / f"case10-delivery-{sha}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in chosen + ["MANIFEST.sha256"]:
            zf.write(target / path, f"case10-delivery-{sha}/{path}")
    print(f"{len(chosen)} files, {report['bytes'] / 1e6:.1f} MB -> {archive}")
    return 0


def scan_tree(root: Path, paths: list[str]) -> list[dict]:
    problems = []
    for path in paths:
        if _match(path, FORBIDDEN_PATHS):
            problems.append({"path": path, "why": "forbidden path"})
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in (".json", ".jsonl", ".csv", ".xlsx", ".tsv") and not _match(path, ALLOWED_DATA):
            problems.append({"path": path, "why": "data file not on the allowed list"})
        if suffix in (".py", ".md", ".json", ".jsonl", ".txt", ".yml", ".yaml"):
            text = (root / path).read_text(encoding="utf-8", errors="replace")
            for pattern in FORBIDDEN_CONTENT:
                if pattern.search(text):
                    problems.append({"path": path, "why": f"content matches {pattern.pattern[:40]!r}"})
    return problems


def scan_code() -> int:
    """Corpus literals in shipped Python, split by where they occur: `code` (a string literal the program uses)
    versus `comment` / `docstring` (provenance notes, harmless)."""
    import tokenize

    hits: list[dict] = []
    for path in sorted((REPO / "api_service" / "app").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        with path.open("rb") as stream:
            for token in tokenize.tokenize(stream.readline):
                if token.type == tokenize.COMMENT:
                    kind = "comment"
                elif token.type == tokenize.STRING:
                    kind = "docstring" if token.string.lstrip("rbuRBUfF").startswith(('"""', "'''")) else "code"
                else:
                    continue
                for match in CORPUS_LITERALS.finditer(token.string):
                    hits.append({"file": rel, "line": token.start[0], "kind": kind, "literal": match.group(0),
                                 "text": token.line.strip()[:160]})
    out = REPO / "evaluation" / "phase12" / "s6_code_literal_scan.json"
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")
    code = [hit for hit in hits if hit["kind"] == "code"]
    print(f"{len(hits)} corpus literal(s): {len(code)} in code, {len(hits) - len(code)} in comments/docstrings -> {out}")
    for hit in code:
        print(f"  CODE {hit['file']}:{hit['line']} {hit['literal']!r}".encode("ascii", "backslashreplace").decode())
    return 0


def check_image(image: str) -> int:
    listing = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c",
         "find /app /data /root -xdev -type f 2>/dev/null | grep -v -e '/site-packages/' -e '/__pycache__/' | sort"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    files = [line for line in listing.stdout.splitlines() if line]
    problems = []
    for path in files:
        rel = path.lstrip("/")
        if rel.startswith("root/.cache/huggingface/"):
            continue
        app_rel = rel[len("app/"):] if rel.startswith("app/") else rel
        if _match(app_rel, FORBIDDEN_PATHS) or app_rel.startswith(("case_data/", "learning_data/")):
            problems.append({"path": path, "why": "forbidden path"})
        elif PurePosixPath(rel).suffix in (".json", ".jsonl", ".csv", ".xlsx") and not _match("api_service/" + app_rel, ALLOWED_DATA):
            problems.append({"path": path, "why": "data file not on the allowed list"})
        elif PurePosixPath(rel).suffix == ".md" and not rel.endswith("reference_data/README.md"):
            problems.append({"path": path, "why": "document in image"})
    top = sorted({"/".join(path.split("/")[:3]) for path in files})
    print(json.dumps({"image": image, "files": len(files), "top_level": top, "problems": problems}, ensure_ascii=False, indent=1))
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bundle")
    b.add_argument("--ref", default="HEAD")
    b.add_argument("--out", default=str(REPO / "dist"))
    i = sub.add_parser("check-image")
    i.add_argument("image")
    sub.add_parser("scan-code")
    args = ap.parse_args()
    if args.command == "bundle":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        return bundle(args.ref, out)
    if args.command == "check-image":
        return check_image(args.image)
    return scan_code()


if __name__ == "__main__":
    sys.exit(main())
