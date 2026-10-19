"""Check the wording of the delivery documents: no unsupported absolutes, no metric number without a source.

    python evaluation/check_claims.py [FILE ...] [--json]

Default files: README.md, README_GRADER.md, DEPLOYMENT.md, TZ_TRACEABILITY.md (repository root). Exit code 0 -- nothing to fix,
1 -- at least one violation, 2 -- a file is missing. Two rules, both applied per "segment" (a table row, a list item with
its continuation lines, or a paragraph; fenced code blocks are skipped):

  ABSOLUTE   "100 %", "готово", "полностью", "гарантируем", "без ошибок", "идеально" ... A segment that says one of them
             needs a reference (below) or a negation in front of the word ("не полностью", "не гарантируем").
  METRIC     a number that reads as a quality metric (0,95 / 85 % / 5 из 19 next to accuracy, precision, recall, F1,
             FPR, CER, точность, полнота ...) needs a reference in the same segment.

  FLAG       a documented default of a `CASE10_*` flag (table row "| `CASE10_X` | `0` | ...") that differs from the default in
             `docker-compose.case10.yml` (`${CASE10_X:-0}`): the document and the delivery configuration must say the same.

A reference is a report or file name (`FINAL_*_REPORT.md`, `evaluation/...`, `*.json`, `*.py`, a test file) or an explicit
statement that the number was not measured ("не измерено", "не перемерено", "оценка", "порог ТЗ"). A line can be
exempted on purpose with `<!-- claims-ok: reason -->` (the reason is required and is printed with `--json`).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILES = ("README.md", "README_GRADER.md", "DEPLOYMENT.md", "TZ_TRACEABILITY.md")

ABSOLUTE = re.compile(
    r"(?<![\w])(?:100\s?%|100\s?процент\w*|готов(?:о|а|ы|ый|ая|ые)?(?![\w])|полностью|полноценн\w*|гарантир\w*|"
    r"безошибочн\w*|без\s+ошибок|идеальн\w*|fully|guarantee\w*)",
    re.IGNORECASE,
)
NEGATION_BEFORE = re.compile(r"(?:\bне|\bнет|\bбез|\bnot|\bno)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
METRIC_WORDS = re.compile(
    r"точност|accuracy|precision|recall|\bF1\b|\bFPR\b|\bCER\b|\bWER\b|полнот|ложн\w+\s+срабатыван|доля\s+(?:найден|подтвержд|верн)|"
    r"character|exact\s+match|уверенност|охват|coverage|IoU",
    re.IGNORECASE,
)
METRIC_NUMBER = re.compile(r"(?<![\w.])(?:0[.,]\d{2,4}|\d{1,3}(?:[.,]\d+)?\s?%|\d+\s?(?:/|из)\s?\d+)(?![\w])")
REFERENCE = re.compile(
    r"[\w./\\-]+\.(?:md|json|py|csv|txt|png|jpg)\b|FINAL_\w+|\w+_REPORT|S\d_REPORT|PHASE12_\w+|не\s+(?:измер|перемер|проверял|подтвержд)\w*|"
    r"порог\w*\s+ТЗ|пороги?\s+ТЗ|оценк\w+|историческ\w+",
    re.IGNORECASE,
)
EXEMPT = re.compile(r"<!--\s*claims-ok:\s*(\S.*?)\s*-->")


def segments(text: str) -> Iterator[tuple[int, str]]:
    """(first line number, text) of every table row / list item / paragraph outside fenced code blocks."""
    lines = text.splitlines()
    buffer: list[str] = []
    start = 0
    fenced = False

    def flush() -> Iterator[tuple[int, str]]:
        nonlocal buffer
        if buffer:
            yield start, "\n".join(buffer)
        buffer = []

    for number, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            yield from flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        stripped = line.strip()
        is_row = stripped.startswith("|")
        is_item = bool(re.match(r"(?:[-*+]|\d+[.)])\s+", stripped))
        if not stripped or is_row or is_item or stripped.startswith("#"):
            yield from flush()
        if not stripped:
            continue
        if not buffer:
            start = number
        buffer.append(line)
        if is_row or stripped.startswith("#"):
            yield from flush()
    yield from flush()


FLAG_ROW = re.compile(r"^\|\s*`(CASE10_[A-Z0-9_]+)`\s*\|\s*`([^`]*)`\s*\|")
COMPOSE_DEFAULT = re.compile(r"(CASE10_[A-Z0-9_]+)\s*:\s*\$\{(CASE10_[A-Z0-9_]+):-([^}]*)\}")


def check_flag_defaults(text: str, compose_text: str) -> list[dict[str, object]]:
    """Documented flag defaults against the compose file; flags the compose file does not set are not checked."""
    actual = {name: value.strip() for name, same, value in COMPOSE_DEFAULT.findall(compose_text) if name == same}
    found: list[dict[str, object]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = FLAG_ROW.match(line.strip())
        if match and match.group(1) in actual and match.group(2).strip() != actual[match.group(1)]:
            found.append({"line": number, "rule": "FLAG", "text": match.group(1),
                          "context": f"documented default {match.group(2).strip()!r}, docker-compose.case10.yml sets {actual[match.group(1)]!r}"})
    return found


def check_text(text: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for line_number, segment in segments(text):
        exempt = EXEMPT.search(segment)
        if exempt:
            continue
        has_reference = bool(REFERENCE.search(segment))
        for match in ABSOLUTE.finditer(segment):
            if NEGATION_BEFORE.search(segment[max(0, match.start() - 30):match.start()]) or has_reference:
                continue
            found.append({"line": line_number, "rule": "ABSOLUTE", "text": match.group(0),
                          "context": " ".join(segment.split())[:200]})
        if not has_reference and METRIC_WORDS.search(segment):
            numbers = [m.group(0) for m in METRIC_NUMBER.finditer(segment)]
            if numbers:
                found.append({"line": line_number, "rule": "METRIC", "text": ", ".join(dict.fromkeys(numbers)),
                              "context": " ".join(segment.split())[:200]})
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="documents to check (default: the delivery documents in the repository root)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    paths = [Path(item) for item in args.files] or [REPO_ROOT / name for name in DEFAULT_FILES]
    report: dict[str, list[dict[str, object]]] = {}
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print("missing file(s): " + ", ".join(missing), file=sys.stderr)
        return 2
    compose = REPO_ROOT / "docker-compose.case10.yml"
    compose_text = compose.read_text(encoding="utf-8") if compose.is_file() else ""
    for path in paths:
        content = path.read_text(encoding="utf-8")
        report[path.name] = check_text(content) + check_flag_defaults(content, compose_text)
    total = sum(len(items) for items in report.values())
    if args.json:
        print(json.dumps({"violations": total, "files": report}, ensure_ascii=False, indent=2))
    else:
        for name, items in report.items():
            for item in items:
                print(f"{name}:{item['line']}: [{item['rule']}] {item['text']!r} -- {item['context']}")
        print(f"check_claims: {total} violation(s) in {len(paths)} file(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
