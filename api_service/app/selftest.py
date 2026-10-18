"""`python -m app.cli selftest` -- a smoke check of the installed service on generated documents.

It writes small synthetic PDFs (Latin parameter names, no data of any competition object), runs them through the same
evidence-group pipeline the service runs (`create_official_evidence_groups`: live tagger, extractors, comparison gate,
group store) and compares the observable outcome with what a correct installation must do: report a real difference,
stay silent on equal values, abstain where a comparison is not possible, give the same result for any file order and
for a repeated run, keep an inspector's decision. Room/system composition scenarios call the comparison functions on
structured rows.

THIS IS NOT A QUALITY METRIC. About two dozen hand-made cases cannot say how well the service finds real
discrepancies in real documents; they say that the pipeline is installed, runs offline, and has not lost basic
behaviour. The sentence-embedding model is switched off here (it only affects ranking among several candidates), so the
check needs no model files. Real quality numbers: `evaluation/grader_sim.py`, `FINAL_*_REPORT.md`.

Exit code: 0 all scenarios passed, 1 at least one failed.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
from unittest.mock import patch

STAGE_NAMES = {"PD": "project", "RD": "working", "ID": "as_built"}
AREA = "Total building area"
SCENARIOS: list[tuple[str, str, Callable[[], str]]] = []     # (name, kind, function -> detail; raises AssertionError)


def scenario(name: str, kind: str):
    def register(function: Callable[[], str]) -> Callable[[], str]:
        SCENARIOS.append((name, kind, function))
        return function

    return register


# ------------------------------------------------------------------------------------------------ factories


def write_pdf(path: Path, pages: list[list[str]], *, rotation: int = 0) -> str:
    """A PDF with one text line per list item (built-in Latin font); returns its SHA-256."""
    import fitz

    pdf = fitz.open()
    for lines in pages:
        page = pdf.new_page(width=595, height=842)
        y = 80
        for line in lines:
            page.insert_text((60, y), line, fontname="helv", fontsize=11)
            y += 18
        if rotation:
            page.set_rotation(rotation)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(path))
    pdf.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Harness:
    """One in-memory database and one originals directory; `add_doc` order == DocumentVersion id order."""

    def __init__(self, params: list[tuple[str, str, str, str]] | None = None):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from .db.models import Base, InspectionProcess, MatrixVersion, Param

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "originals"
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.engine = engine
        self.db = sessionmaker(bind=engine)()
        matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1", status="ACTIVE", source_filename="selftest", source_hash="selftest")
        self.db.add(matrix)
        self.db.flush()
        self.params = []
        for code, name, unit, hint in params or [("ZZ-001", AREA, "м²", "")]:
            row = Param(matrix_version_id=matrix.id, project_id=1, organization_id=1, code=code, matrix_code=code, scoring_code=code, section="x",
                        parameter_name=name, unit=unit, source_pd=True, source_rd=True, source_id=False, trigger_logic="", review_priority="HIGH",
                        data_type="number", is_active=True, other_normative=json.dumps({"source_pd": hint, "source_rd": hint, "source_id": ""}))
            self.db.add(row)
            self.params.append(row)
        self.docs: list[Any] = []
        self.process = InspectionProcess(id="proc-selftest", project_id=1, organization_id=1, object_id="OBJ-SELFTEST", status="PARSING", upload_scenario="FULL",
                                         completeness_status={}, matrix_version="official-132-v1", dataset_version="selftest", model_version="selftest")
        self.db.add(self.process)
        self.db.flush()
        self._stack = ExitStack()
        self._stack.enter_context(patch.dict(os.environ, {"CASE10_ORIGINALS_ROOT": str(self.root)}))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_WORKERS", 1))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.settings.LIVE_TAGGER_CACHE_ENABLED", False))
        self._stack.enter_context(patch("app.domain.live_candidate_tagger.semantic_text_similarity", return_value=None))
        self._stack.enter_context(patch("app.domain.cross_stage_localization.semantic_text_similarity", return_value=None))

    def close(self) -> None:
        self._stack.close()
        self.db.close()
        self.engine.dispose()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def add_doc(self, file_id: str, stage: str, pages: list[list[str]], *, discipline: str = "AR", rotation: int = 0, same_bytes_as: Path | None = None):
        from .db.models import DocumentVersion

        relative = f"{stage}/{file_id}.pdf"
        if same_bytes_as is not None:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_bytes(same_bytes_as.read_bytes())
            sha = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
        else:
            sha = write_pdf(self.root / relative, pages, rotation=rotation)
        manifest = {"file_id": file_id, "stage": stage, "relative_path": relative, "section": discipline, "sha256": sha,
                    "pdf_pages": len(pages), "extension": ".pdf", "size_bytes": 1000}
        doc = DocumentVersion(project_id=1, organization_id=1, source_type="case10_dataset", dataset_file_id=file_id, object_id="OBJ-SELFTEST",
                              filename=f"{file_id}.pdf", doc_stage=STAGE_NAMES[stage], document_stage=STAGE_NAMES[stage], dataset_stage=stage,
                              dataset_section=discipline, discipline=discipline, document_code=file_id, file_hash=sha, content_hash=sha,
                              approval_status="UNKNOWN", dataset_metadata={"document_manifest": manifest})
        self.db.add(doc)
        self.db.flush()
        self.docs.append(doc)
        return doc

    def run(self) -> None:
        from .domain.official_evidence import create_official_evidence_groups

        create_official_evidence_groups(self.db, self.process, list(self.params), list(self.docs))
        self.db.flush()

    def groups(self) -> dict[tuple[str, str], Any]:
        from .db.models import EvidenceGroup

        return {(g.param.code, g.group_key): g for g in self.db.query(EvidenceGroup).all()}

    def one(self, code: str = "ZZ-001"):
        rows = [g for (c, _), g in self.groups().items() if c == code and g.group_key != "low_quality"]
        assert len(rows) == 1, f"expected one group for {code}, got {[g.group_key for g in rows]}"
        return rows[0]

    def snapshot(self) -> list[tuple]:
        """Everything a consumer sees of the persisted groups, free of database ids."""
        from .db.models import DocumentVersion, EvidenceGroup

        files = {d.id: d.dataset_file_id for d in self.db.query(DocumentVersion).all()}
        out = []
        for g in self.db.query(EvidenceGroup).all():
            fragments = sorted((files.get(f.document_version_id), f.page, f.role, f.extracted_value) for f in g.fragments)
            out.append((g.param.code, g.group_key, g.finding_status, g.expected_value, g.actual_value, json.dumps(g.delta, sort_keys=True, default=str), fragments))
        return sorted(out, key=lambda row: (row[0], row[1]))


def run_case(pd: list[list[str]] | None, rd: list[list[str]] | None, **options: Any):
    h = Harness(options.get("params"))
    try:
        if pd is not None:
            h.add_doc("PD-1", "PD", pd, discipline=options.get("pd_discipline", "AR"), rotation=options.get("pd_rotation", 0))
        if rd is not None:
            h.add_doc("RD-1", "RD", rd, discipline=options.get("rd_discipline", "AR"), rotation=options.get("rd_rotation", 0))
        h.run()
        group = h.one()
        return {"status": group.finding_status, "expected": group.expected_value, "actual": group.actual_value, "delta": dict(group.delta or {}),
                "fragments": [(f.role, f.page, f.dataset_file_id if hasattr(f, "dataset_file_id") else None, f.bbox, f.extracted_value) for f in group.fragments],
                "snapshot": h.snapshot()}
    finally:
        h.close()


def _same(a: Any, b: Any, what: str) -> None:
    assert a == b, f"{what}: {a!r} != {b!r}"


def _line(value: str) -> list[list[str]]:
    return [[f"{AREA} {value} m2"]]


# ------------------------------------------------------------------------------------------------ PDF scenarios


@scenario("changed value is reported as a candidate with sources", "positive")
def _changed():
    r = run_case(_line("1000,5"), _line("1200,0"))
    _same(r["status"], "CANDIDATE", "status")
    _same((r["expected"], r["actual"]), ("1000,5", "1200,0"), "values")
    assert r["delta"].get("evidence_tier") == "VIOLATION_CANDIDATE", f"tier {r['delta'].get('evidence_tier')!r}"
    roles = {role for role, *_ in r["fragments"]}
    assert {"expected", "actual"} <= roles, f"cited roles {roles}"
    return "CANDIDATE 1000,5 -> 1200,0"


@scenario("evidence carries page and a bbox inside the page", "positive")
def _evidence():
    r = run_case([["page one"], ["page two"], [f"{AREA} 1000,5 m2"]], _line("1200,0"))
    pages = {role: page for role, page, *_ in r["fragments"]}
    _same(pages.get("expected"), 3, "page of the PD value")
    for role, page, _file, bbox, _value in r["fragments"]:
        assert isinstance(bbox, list) and len(bbox) == 4 and all(0.0 <= float(x) <= 1.0 for x in bbox), f"{role}: bbox {bbox}"
    return "PD value cited on page 3, bboxes in [0;1]"


@scenario("evidence of each side points to the file of that stage", "positive")
def _provenance():
    r = run_case(_line("1000,5"), _line("1200,0"))
    files = {role: f for role, _p, f, _b, _v in r["fragments"] if role in {"expected", "actual"}}
    if all(files.values()):
        _same(files, {"expected": "PD-1", "actual": "RD-1"}, "cited files")
    values = {role: v for role, _p, _f, _b, v in r["fragments"]}
    _same((values.get("expected"), values.get("actual")), ("1000,5", "1200,0"), "cited values")
    return "expected from PD, actual from RD"


@scenario("equal values give no violation", "negative")
def _equal():
    r = run_case(_line("1000,5"), _line("1000,5"))
    _same(r["status"], "NEGATIVE_VERIFIED", "status")
    return "NEGATIVE_VERIFIED"


@scenario("decimal comma and decimal point are the same number", "negative")
def _comma():
    _same(run_case(_line("1000,5"), _line("1000.5"))["status"], "NEGATIVE_VERIFIED", "status")
    return "1000,5 == 1000.5"


@scenario("a deviation inside the tolerance is not a violation", "negative")
def _tolerance():
    _same(run_case(_line("1000,5"), _line("1000,9"))["status"], "NEGATIVE_VERIFIED", "status")
    return "1000,5 vs 1000,9 within tolerance"


@scenario("another unit of measure: abstain, no candidate", "abstain")
def _unit():
    r = run_case(_line("1000,5"), [[f"{AREA} 1000,5 kg"]])
    assert r["status"] != "CANDIDATE", f"status {r['status']}"
    _same(r["status"], "NOT_COMPARABLE", "status")
    return f"NOT_COMPARABLE ({r['delta'].get('reason')})"


@scenario("no RD document: missing evidence, not a violation", "abstain")
def _no_rd():
    r = run_case(_line("1000,5"), None)
    _same(r["status"], "MISSING_EVIDENCE", "status")
    return f"MISSING_EVIDENCE ({r['delta'].get('reason')})"


@scenario("RD does not state the value: not comparable", "abstain")
def _no_value():
    r = run_case(_line("1000,5"), [["Some unrelated drawing notes"]])
    assert r["status"] in {"NOT_COMPARABLE", "MISSING_EVIDENCE"}, f"status {r['status']}"
    assert r["status"] != "CANDIDATE"
    return f"{r['status']} ({r['delta'].get('reason')})"


@scenario("a blank page does not crash the run or invent a finding", "abstain")
def _blank():
    r = run_case(_line("1000,5"), [[]])
    assert r["status"] != "CANDIDATE", f"status {r['status']}"
    return r["status"]


@scenario("a page with /Rotate is flagged as low quality, not guessed", "abstain")
def _rotated():
    h = Harness()
    try:
        h.add_doc("PD-1", "PD", _line("1000,5"))
        h.add_doc("RD-1", "RD", _line("1200,0"), rotation=90)
        h.run()
        groups = {key: g for (code, key), g in h.groups().items() if code == "ZZ-001"}
        assert not any(g.finding_status == "CANDIDATE" for g in groups.values()), f"a rotated page produced a candidate: {sorted(groups)}"
        quality = groups.get("low_quality")
        assert quality is not None and quality.finding_status == "NOT_COMPARABLE", f"no low-quality group: {sorted(groups)}"
    finally:
        h.close()
    return "LOW_QUALITY (ROTATED_PAGE): the value of a rotated page is not extracted"


@scenario("document of the wrong section is not used as the source", "abstain")
def _foreign_section():
    params = [("ZZ-001", AREA, "м²", "AR")]
    r = run_case(_line("1000,5"), _line("1200,0"), params=params, rd_discipline="KR")
    assert r["status"] != "CANDIDATE", f"a KR document was compared for an AR parameter: {r['status']}"
    return f"{r['status']} ({r['delta'].get('reason')})"


def _two_docs(order: str, stage: str, a: str, b: str):
    h = Harness()
    try:
        docs = [("A", _line(a)), ("B", _line(b))]
        if order == "reversed":
            docs.reverse()
        if stage == "PD":
            for name, pages in docs:
                h.add_doc(f"PD-{name}", "PD", pages)
            h.add_doc("RD-1", "RD", _line("1000,5"))
        else:
            h.add_doc("PD-1", "PD", _line("1000,5"))
            for name, pages in docs:
                h.add_doc(f"RD-{name}", "RD", pages)
        h.run()
        return h.snapshot()
    finally:
        h.close()


@scenario("file order of two PD documents does not change the result", "stability")
def _order_pd():
    _same(_two_docs("natural", "PD", "1000,5", "1300,0"), _two_docs("reversed", "PD", "1000,5", "1300,0"), "snapshot")
    return "same result for both orders"


@scenario("file order of two conflicting RD documents does not change the result", "stability")
def _order_rd():
    _same(_two_docs("natural", "RD", "1100,0", "1200,0"), _two_docs("reversed", "RD", "1100,0", "1200,0"), "snapshot")
    return "same result for both orders"


@scenario("a byte-identical duplicate file does not change the result", "stability")
def _duplicate_file():
    single = run_case(_line("1000,5"), _line("1200,0"))
    h = Harness()
    try:
        h.add_doc("PD-1", "PD", _line("1000,5"))
        rd = h.add_doc("RD-1", "RD", _line("1200,0"))
        h.add_doc("RD-2", "RD", [], same_bytes_as=h.root / "RD/RD-1.pdf")
        h.run()
        group = h.one()
        _same((group.finding_status, group.expected_value, group.actual_value), (single["status"], single["expected"], single["actual"]), "result with a duplicate")
        assert rd is not None
    finally:
        h.close()
    return "same finding with the file uploaded twice"


@scenario("a repeated run on a fresh database gives the same result", "stability")
def _repeat_fresh():
    _same(run_case(_line("1000,5"), _line("1200,0"))["snapshot"], run_case(_line("1000,5"), _line("1200,0"))["snapshot"], "snapshot")
    return "identical"


@scenario("running twice on the same database adds no duplicate groups", "stability")
def _repeat_same():
    h = Harness()
    try:
        h.add_doc("PD-1", "PD", _line("1000,5"))
        h.add_doc("RD-1", "RD", _line("1200,0"))
        h.run()
        first = h.snapshot()
        h.run()
        _same(h.snapshot(), first, "snapshot after a second run")
    finally:
        h.close()
    return "no new groups, nothing changed"


@scenario("an inspector's decision survives when the evidence disappears", "stability")
def _decision_kept():
    from .db.models import EvidenceDecision

    h = Harness()
    try:
        h.add_doc("PD-1", "PD", _line("1000,5"))
        rd = h.add_doc("RD-1", "RD", _line("1200,0"))
        h.run()
        group = h.groups()[("ZZ-001", "generic:anchor")]
        _same(group.finding_status, "CANDIDATE", "status before the decision")
        h.db.add(EvidenceDecision(evidence_group_id=group.id, decision="Confirm", reason_code=None, user_id=1))
        group.finding_status = "CONFIRMED_VIOLATION"
        h.db.flush()
        h.docs.remove(rd)
        h.run()
        kept = h.groups()[("ZZ-001", "generic:anchor")]
        _same(kept.finding_status, "CONFIRMED_VIOLATION", "decided status")
        assert kept.needs_reverification, "the decided group must be flagged for re-check"
    finally:
        h.close()
    return "decision kept, group flagged for re-verification"


# ------------------------------------------------------------------------------------------------ room scenarios


def _row(location: str, systems: list[str], **extra: Any) -> dict[str, Any]:
    return {"location": location, "systems": list(systems), **extra}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _strip(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in entry.items() if k not in {"expected", "actual", "records"}} for entry in entries]


@scenario("room number repeated in PD: conflict, never an overwrite", "positive")
def _room_duplicate():
    from .domain.room_drawing_compare import compare_project_to_working

    entries = compare_project_to_working([_row("101", ["V2"]), _row("101", ["P1"])], [_row("101", ["V2"])])
    _same(len(entries), 1, "entries")
    _same((entries[0]["status"], entries[0]["reason"]), ("CONFLICT", "DUPLICATE_ROOM_KEY"), "status/reason")
    _same(entries[0]["missing_systems"], [], "missing systems of a conflicting key")
    return "CONFLICT / DUPLICATE_ROOM_KEY, both records kept"


@scenario("room rows in any order give the same comparison", "stability")
def _room_permutation():
    from .domain.room_drawing_compare import compare_project_to_working

    rows = [_row("101", ["P1"]), _row("101", ["V2"]), _row("102", ["P1", "V1"])]
    rd = [_row("101", ["V2"]), _row("102", ["P1"])]
    results = {_canon(_strip(compare_project_to_working(list(order), rd))) for order in itertools.permutations(rows)}
    _same(len(results), 1, "distinct results over 6 orders")
    return "6 orders, one result"


@scenario("the same number on different floors is two rooms", "negative")
def _room_floors():
    from .domain.room_drawing_compare import compare_project_to_working

    pd = [_row("101", ["P1"], floor="1"), _row("101", ["V2"], floor="2")]
    rd = [_row("101", ["P1"], floor="1"), _row("101", ["V2"], floor="2")]
    _same(compare_project_to_working(pd, rd), [], "entries for equal floors")
    return "no difference, no merge"


@scenario("equal system composition gives no finding", "negative")
def _room_equal():
    from .domain.room_drawing_compare import compare_project_to_working

    _same(compare_project_to_working([_row("101", ["P1", "V2"])], [_row("101", ["V2", "P1"])], mode="composition"), [], "entries")
    return "no difference"


@scenario("a system that disappeared from RD is reported as missing", "positive")
def _room_missing():
    from .domain.room_drawing_compare import compare_project_to_working

    entries = compare_project_to_working([_row("101", ["P1", "P2"])], [_row("101", ["P2"])])
    _same((entries[0]["status"], entries[0]["missing_systems"]), ("MISSING", ["P1"]), "status/missing")
    return "MISSING P1"


@scenario("a system added in RD is reported separately", "positive")
def _room_added():
    from .domain.room_drawing_compare import compare_project_to_working

    entries = compare_project_to_working([_row("101", ["P1"])], [_row("101", ["P1", "V2"])], mode="composition")
    _same((entries[0]["status"], entries[0]["added_systems"], entries[0]["missing_systems"]), ("ADDED", ["V2"], []), "status/added/missing")
    return "ADDED V2"


@scenario("room number with an unknown floor and several candidates is ambiguous", "abstain")
def _room_ambiguous():
    from .domain.room_drawing_compare import compare_project_to_working

    pd = [_row("101", ["P1"], floor="1"), _row("101", ["P1", "V2"], floor="2")]
    entries = compare_project_to_working(pd, [_row("101", ["P1"])])
    assert entries and entries[0]["status"] == "AMBIGUOUS", f"entries {[e.get('status') for e in entries]}"
    return "AMBIGUOUS, no guess"


# ------------------------------------------------------------------------------------------------ runner


def _prepare_environment(work_dir: Path) -> None:
    """Offline, in-process: no queue, no cache server, no model download. Must run before `app.config` is imported."""
    os.environ["RABBITMQ_URL"] = ""
    os.environ["REDIS_URL"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("API_DATA_DIR", str(work_dir / "data"))


def run_selftest(*, only: str | None = None, as_json: bool = False, say: Callable[[str], None] = print) -> int:
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    selected = [item for item in SCENARIOS if not only or only.lower() in item[0].lower()]
    with tempfile.TemporaryDirectory() as work:
        _prepare_environment(Path(work))
        for name, kind, function in selected:
            begun = time.perf_counter()
            try:
                detail, status = function(), "PASS"
            except AssertionError as exc:
                detail, status = str(exc) or "assertion failed", "FAIL"
            except Exception as exc:  # noqa: BLE001 -- a crash is a failed scenario, not a failed tool
                detail, status = f"{type(exc).__name__}: {exc}", "FAIL"
            results.append({"scenario": name, "kind": kind, "result": status, "detail": detail, "seconds": round(time.perf_counter() - begun, 2)})
            if not as_json:
                say(f"[{status}] ({kind}) {name} -- {detail}")
    failed = sum(1 for item in results if item["result"] != "PASS")
    summary = {"scenarios": len(results), "passed": len(results) - failed, "failed": failed, "seconds": round(time.perf_counter() - started, 1),
               "note": "smoke check on generated documents, not a quality metric"}
    if as_json:
        say(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    else:
        say(f"selftest: {summary['scenarios']} scenarios, {summary['passed']} PASS, {summary['failed']} FAIL in {summary['seconds']} s "
            "(smoke check on generated documents -- NOT a quality metric)")
    return 1 if failed else 0
