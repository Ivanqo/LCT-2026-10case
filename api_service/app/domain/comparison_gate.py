"""Comparability gate of the generic (non-rule-pack) mechanisms -- Phase 10, prompt B, items 2 and 4.

ТЗ 9.2 п.3 fixes the order in which a parameter may be judged:
    применимость -> комплектность -> актуальная редакция и сопоставимость -> ТОЛЬКО ПОТОМ сравнение.
Until now the generic mechanisms compared whatever two numbers they found. This module is the missing
front half: given the observations a mechanism extracted for a parameter, it decides whether a verdict
may be drawn at all, and if not, WHY (a machine-readable reason that ends up in `delta.reason`, in the
protocol and in the submission label).

Outcome -> submission label (see evaluation/exporter.py)
    MISSING_EVIDENCE + `missing_discipline_document`  -> MISSING_DOCUMENT (+ PD_/RD_/ID_MISSING status)
    NOT_COMPARABLE   + any other reason               -> COMPARISON_IMPOSSIBLE
Neither is ever a violation. A gate never turns an abstention into a verdict.

Reasons (all deterministic, all recorded)
    trigger_not_evaluable         the catalog trigger cannot be judged from a value comparison (spatial/registry)
    multi_instance_parameter      the parameter is per element (doors, corridors, pipes...) and the mechanism
                                  reads ONE number per stage -- which element it hit is arbitrary
    missing_discipline_document   a required stage has documents, but none of the section the catalog names
    evidence_discipline_mismatch  the cited page sits in a section the catalog does not name for that stage
    revision_conflict             the cited file is one of several competing editions that cannot be ordered
                                  (or the stage mixes two project series for that section)
    self_comparison               PD and RD evidence are the same file/bytes
    unit_mismatch                 the number is next to a DIFFERENT physical unit (height for a volume)
    unit_evidence_absent          no unit of the parameter's kind next to/on the row: not a measurement
    magnitude_mismatch            PD/RD values differ by >= 20x: different quantities or units
Rule-pack codes (official_rule_packs.SUPPORTED_RULE_CODES) are never gated here: that tier is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping

from . import document_facts as df
from . import value_plausibility as vp
from .trigger_policy import NOT_EVALUABLE, rule_for

STATUS_NOT_COMPARABLE = "NOT_COMPARABLE"
STATUS_MISSING_EVIDENCE = "MISSING_EVIDENCE"

REASON_TRIGGER_NOT_EVALUABLE = "trigger_not_evaluable"
REASON_MULTI_INSTANCE = "multi_instance_parameter"
REASON_MISSING_DISCIPLINE = "missing_discipline_document"
REASON_DISCIPLINE_MISMATCH = "evidence_discipline_mismatch"
REASON_REVISION_CONFLICT = "revision_conflict"
REASON_SELF_COMPARISON = "self_comparison"
REASON_UNIT_MISMATCH = "unit_mismatch"
REASON_UNIT_ABSENT = "unit_evidence_absent"
REASON_MAGNITUDE = "magnitude_mismatch"

MISSING_DOCUMENT_REASONS = frozenset({"missing_stage", REASON_MISSING_DISCIPLINE})
# A verdict between values >= 20x apart is a comparison of different quantities (m vs mm, a height vs a
# volume, an ordinal vs an area) -- no PD->RD design change moves a scalar by more than an order of magnitude.
MAGNITUDE_RATIO_LIMIT = Decimal(20)

# Parameters whose catalog wording is per ELEMENT (each door, corridor, stair flight, cable line, pipe run...).
# The generic mechanisms read one number per stage; without per-element extraction it is a coin toss which
# element was read, and a PD-vs-RD difference between two different elements is not a design change.
MULTI_INSTANCE_PARAMETERS: dict[str, str] = {
    "AR-040": "ширина каждого магистрального коридора",
    "AR-041": "ширина каждой эвакуационной двери (спецификация проёмов)",
    "AR-042": "высота каждого коридора/двери",
    "AR-047": "глубина тамбура каждой входной группы",
    "AR-048": "параметры каждого лестничного марша",
    "AR-049": "высота каждого ограждения (лестницы, балкон, кровля)",
    "KR-054": "шаг каждой координационной оси",
    "KR-060": "сечение каждой марки колонны/пилона",
    "KR-062": "диаметр арматуры по каждому элементу",
    "KR-064": "габариты каждой лифтовой шахты",
    "KR-065": "площадь каждого технологического проёма",
    "IOS1-069": "сечение каждой кабельной линии",
    "IOS2-071": "диаметр каждого стояка/магистрали",
    "IOS3-074": "диаметр каждого выпуска",
    "IOS4-076": "диаметр каждого стояка/магистрали отопления",
    "IOS4-077": "параметры каждого типа радиатора",
    "PPM-104": "ширина каждого эвакуационного прохода",
    "PPM-105": "ширина каждой двери наружного выхода",
    "ODI-116": "ширина каждого коридора на путях МГН",
    "ODI-117": "ширина каждого дверного проёма на путях МГН",
    "ODI-118": "высота порога каждого проёма",
    "ODI-119": "размеры каждой универсальной кабины",
    "ODI-121": "количество/ширина каждого парковочного места МГН",
    "POS-084": "ширина каждого участка временной дороги",
}

_UNIT_GATED_TIERS = frozenset({"numeric", "compound"})
_DISCIPLINE_GATED_TIERS = frozenset({"numeric", "compound", "table_count"})
# An enum class (fire-resistance degree, hazard class...) is legitimately restated in many PD volumes (ПЗ, ПБ, ОДИ, АР),
# so its PD side is not discipline-gated. On the RD side the catalog names the authoritative sheet ("АР/КР: Общие
# данные"): a class copied into the general notes of a lightning-protection or drainage set is not the design value.
_EVIDENCE_DISCIPLINE_STAGES = {"numeric": ("PD", "RD"), "compound": ("PD", "RD"), "table_count": ("PD", "RD"), "enum": ("RD",)}
_MULTI_INSTANCE_TIERS = frozenset({"numeric", "compound", "table_count"})
_STAGES = ("PD", "RD", "ID")


@dataclass(slots=True)
class GateDecision:
    status: str                      # NOT_COMPARABLE | MISSING_EVIDENCE
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    stages: list[str] = field(default_factory=list)

    def to_delta(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason, "gate": {"reason": self.reason, **self.details}}
        if self.stages:
            out["stages"] = list(self.stages)
        return out


class GateContext:
    """Per-run, per-object facts. Built once from the object's DocumentVersions; never opens a file."""

    def __init__(self, docs: Iterable[Any], *, rule_pack_codes: Iterable[str] = ()):
        self.docs = list(docs)
        self.facts: dict[int, df.DocFacts] = {}
        for doc in self.docs:
            if getattr(doc, "id", None) is not None:
                self.facts[int(doc.id)] = df.facts_from_document(doc)
        self.integrity = df.analyze_integrity(self.facts.values())
        excluded = self.integrity.excluded_keys
        self.revisions = df.analyze_revisions(self.facts.values(), exclude_keys=excluded)
        self.inventory = df.stage_inventory(self.facts.values(), exclude_keys=excluded)
        self.rule_pack_codes = frozenset(rule_pack_codes)
        self._key_to_doc_id = {fact.key: doc_id for doc_id, fact in self.facts.items()}
        self.excluded_doc_ids = frozenset(self._key_to_doc_id[k] for k in excluded if k in self._key_to_doc_id)
        self.superseded_doc_ids = frozenset(self._key_to_doc_id[k] for k in self.revisions.superseded if k in self._key_to_doc_id)
        self._hint_cache: dict[tuple[int, str], frozenset[str]] = {}

    # ---------------------------------------------------------------- document filters used at extraction
    def drop_from_generic_candidates(self, doc_id: int | None) -> bool:
        """Byte duplicates, unreadable/service files and superseded (older, orderable) revisions never
        feed the generic mechanisms. The rule-pack tier still sees every document (it is unchanged)."""
        return doc_id is not None and (int(doc_id) in self.excluded_doc_ids or int(doc_id) in self.superseded_doc_ids)

    # ---------------------------------------------------------------- catalog hints
    def _hints(self, param: Any, stage: str) -> frozenset[str]:
        key = (int(param.id), stage)
        if key not in self._hint_cache:
            from .anchor_search import source_hints

            self._hint_cache[key] = df.hint_tags(source_hints(param).get(stage, ""))
        return self._hint_cache[key]

    # ---------------------------------------------------------------- completeness
    def stage_completeness(self, param: Any, stage: str) -> tuple[str, dict[str, Any]]:
        """PRESENT | ABSENT | UNKNOWN | UNCONSTRAINED for the section the catalog names at `stage`.
        ABSENT is only ever claimed when the stage has files, none carries the named section, AND no file of
        the stage is unrecognisable (an unrecognisable file might be that section)."""
        required = self._hints(param, stage)
        if not required:
            return "UNCONSTRAINED", {}
        docs = self.inventory.get(stage) or []
        if not docs:
            return "STAGE_ABSENT", {"required_sections": sorted(required)}
        if any(fact.tags & required for fact in docs):
            return "PRESENT", {}
        unknown = [fact for fact in docs if fact.doc_class == "UNKNOWN"]
        if unknown:
            return "UNKNOWN", {"unknown_files": len(unknown)}
        return "ABSENT", {"required_sections": sorted(required), "files_in_stage": len(docs)}

    def evidence_discipline(self, param: Any, stage: str, doc: Any) -> tuple[str, dict[str, Any]]:
        required = self._hints(param, stage)
        if not required:
            return "UNCONSTRAINED", {}
        fact = self.facts.get(int(doc.id)) if getattr(doc, "id", None) is not None else None
        if fact is None or not fact.tags:
            return "UNKNOWN", {}
        if fact.tags & required:
            return "MATCH", {}
        return "MISMATCH", {"cited_sections": sorted(fact.primary_tags), "required_sections": sorted(required)}

    # ---------------------------------------------------------------- the gate
    def evaluate(self, param: Any, tier: str, observations: Mapping[str, Any]) -> GateDecision | None:
        """`observations`: {"PD": obs, "RD": obs, "ID": obs?} of one mechanism `tier` in
        {numeric, enum, compound, table_count}. Returns None when a verdict may be drawn."""
        code = str(param.code)
        if code in self.rule_pack_codes:
            return None
        rule = rule_for(code, trigger_logic=getattr(param, "trigger_logic", None))
        # 1. applicability ---------------------------------------------------------------------------
        if rule.kind == NOT_EVALUABLE:
            return GateDecision(STATUS_NOT_COMPARABLE, REASON_TRIGGER_NOT_EVALUABLE, {"trigger": rule.source_text})
        if tier in _MULTI_INSTANCE_TIERS and code in MULTI_INSTANCE_PARAMETERS:
            return GateDecision(STATUS_NOT_COMPARABLE, REASON_MULTI_INSTANCE, {"per_element": MULTI_INSTANCE_PARAMETERS[code]})
        # 2. completeness ----------------------------------------------------------------------------
        if tier in _DISCIPLINE_GATED_TIERS:
            for stage in ("PD", "RD"):
                obs = observations.get(stage)
                if obs is None:
                    continue
                verdict, info = self.stage_completeness(param, stage)
                if verdict == "ABSENT":
                    return GateDecision(STATUS_MISSING_EVIDENCE, REASON_MISSING_DISCIPLINE, {"stage": stage, **info}, [stage])
        # 3. current edition ---------------------------------------------------------------------------
        for stage in _STAGES:
            obs = observations.get(stage)
            if obs is None or getattr(obs, "document", None) is None:
                continue
            fact = self.facts.get(int(obs.document.id)) if obs.document.id is not None else None
            if fact is None:
                continue
            conflicts = self.revisions.conflicts_for(fact.key)
            if conflicts:
                return GateDecision(STATUS_NOT_COMPARABLE, REASON_REVISION_CONFLICT, {
                    "stage": stage, "cited_file": fact.file_id or fact.key,
                    "conflicts": [{"id": c.conflict_id, "type": c.type, "section": c.scope, "files": list(c.file_ids)} for c in conflicts],
                })
        # 4. comparability ------------------------------------------------------------------------------
        decision = self._self_comparison(observations)
        if decision:
            return decision
        for stage in _EVIDENCE_DISCIPLINE_STAGES.get(tier, ()):
            obs = observations.get(stage)
            if obs is None:
                continue
            verdict, info = self.evidence_discipline(param, stage, obs.document)
            if verdict == "MISMATCH":
                return GateDecision(STATUS_NOT_COMPARABLE, REASON_DISCIPLINE_MISMATCH, {"stage": stage, "cited_file": obs.document.dataset_file_id, **info})
        if tier in _UNIT_GATED_TIERS:
            decision = self._unit_gate(tier, observations)
            if decision:
                return decision
        if tier == "numeric":
            decision = self._magnitude_gate(observations)
            if decision:
                return decision
        return None

    # ---------------------------------------------------------------- comparability helpers
    def _self_comparison(self, observations: Mapping[str, Any]) -> GateDecision | None:
        seen: dict[Any, str] = {}
        for stage in _STAGES:
            obs = observations.get(stage)
            if obs is None or getattr(obs, "document", None) is None:
                continue
            doc = obs.document
            for token in (("id", doc.id), ("sha", doc.file_hash or doc.content_hash)):
                if token[1] in (None, ""):
                    continue
                if token in seen and seen[token] != stage:
                    return GateDecision(STATUS_NOT_COMPARABLE, REASON_SELF_COMPARISON, {
                        "stages": [seen[token], stage], "file": doc.dataset_file_id, "same": token[0]})
                seen.setdefault(token, stage)
        return None

    @staticmethod
    def _unit_gate(tier: str, observations: Mapping[str, Any]) -> GateDecision | None:
        for stage in _STAGES:
            obs = observations.get(stage)
            facts = getattr(obs, "gate_facts", None) if obs is not None else None
            if not facts:
                continue
            evidences = facts.get("unit_evidence")
            if isinstance(evidences, dict):
                evidences = [evidences]
            for evidence in evidences or []:
                verdict = evidence.get("verdict")
                if verdict == vp.MISMATCH:
                    return GateDecision(STATUS_NOT_COMPARABLE, REASON_UNIT_MISMATCH, {"stage": stage, **evidence})
                if verdict == vp.ABSENT:
                    return GateDecision(STATUS_NOT_COMPARABLE, REASON_UNIT_ABSENT, {"stage": stage, **evidence})
        return None

    @staticmethod
    def _magnitude_gate(observations: Mapping[str, Any]) -> GateDecision | None:
        expected = observations.get("PD")
        if expected is None:
            return None
        left = getattr(expected, "decimal_value", None)
        for stage in ("RD", "ID"):
            obs = observations.get(stage)
            right = getattr(obs, "decimal_value", None) if obs is not None else None
            if left is None or right is None or left <= 0 or right <= 0:
                continue
            ratio = max(left, right) / min(left, right)
            if ratio >= MAGNITUDE_RATIO_LIMIT:
                return GateDecision(STATUS_NOT_COMPARABLE, REASON_MAGNITUDE, {"stages": ["PD", stage], "ratio": str(ratio.quantize(Decimal("0.1")))})
        return None

    # ---------------------------------------------------------------- protocol section
    def protocol_section(self) -> dict[str, Any]:
        stages = {}
        for stage, facts in self.inventory.items():
            classes: dict[str, int] = {}
            for fact in facts:
                classes[fact.doc_class] = classes.get(fact.doc_class, 0) + 1
            stages[stage] = {"files": len(facts), "by_class": dict(sorted(classes.items()))}
        return {
            "schema_version": "case10-document-analysis-v1",
            "integrity": self.integrity.to_dict(),
            "revision_analysis": self.revisions.to_dict(),
            "stage_inventory": stages,
        }
