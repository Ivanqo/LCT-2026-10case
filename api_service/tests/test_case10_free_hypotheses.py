"""Final B2: the free hypothesis module (TZ 9.5) -- UNMATCHED_DIFF, SEMANTIC_DISSONANCE, internal-consistency logic rules,
NORMATIVE and the optional statistical baseline; de-duplication, limits, incremental upsert (inspector reviews preserved),
the SUSPICION -> CANDIDATE conversion and the invariant that a hypothesis is never a violation.

Every detector has a positive case, a negative case, a de-duplication case and an incremental case. The tables are built
from `table_parser` shapes (no PDF); the embedding model is replaced by a deterministic fake."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    AuditLog, DocumentVersion, EvidenceDecision, EvidenceFragment, EvidenceGroup, InspectionProcess, InspectorEdit, MatrixVersion, NormativeBaseEntry, Param, Suspicion,
)
from app.db.session import Base
from app.domain import explication_compare as ec
from app.domain import table_parser as tpar
from app.domain.free_hypotheses import base, logic_rules, ml_stat, normative, semantic_dissonance, store, unmatched_diff
from app.domain.free_hypotheses.runner import RunContext, collect_candidates, matrix_reported_keys, run_free_hypotheses
from app.domain.logical_analysis import run_logical_analysis_module


# ---------------------------------------------------------------- builders
def make_table(rows, *, floor="1", kind=tpar.KIND_ROOMS, totals=None, title="Экспликация помещений 1 этажа", columns=None):
    cols = columns or [tpar.Column(role=tpar.ROLE_NUMBER, label="№", x0=0, x1=10), tpar.Column(role=tpar.ROLE_NAME, label="Наименование", x0=10, x1=60),
                       tpar.Column(role=tpar.ROLE_AREA, label="Площадь", x0=60, x1=90)]
    out = []
    for index, (key, name, area) in enumerate(rows):
        y = 20 + index * 10
        out.append(tpar.Row(key=key, cells={0: key, 1: name, 2: str(area)}, bbox=[0, y, 100, y + 9], key_bbox=[0, y, 10, y + 9]))
    return tpar.RoomTable(kind=kind, title=title, title_bbox=None, scope=tpar.Scope(floor=floor), columns=cols, rows=out,
                          totals=totals or [], bbox=[0, 0, 100, 20 + 10 * len(rows)], page=1, width=842.0, height=595.0)


def make_doc(doc_id, stage, file_id=None):
    return SimpleNamespace(id=doc_id, dataset_file_id=file_id or f"F{doc_id:04d}", document_code=f"AR-{doc_id}", filename=f"{file_id or doc_id}.pdf",
                           file_hash=f"{doc_id:064x}", content_hash=f"{doc_id:064x}", discipline="AR", revision="0", approval_status="UNKNOWN",
                           dataset_stage=stage, doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage])


def ref_of(table, stage, doc_id, file_id=None):
    doc = make_doc(doc_id, stage, file_id)
    return ec.TableRef(table=table, stage=stage, file_id=doc.dataset_file_id, doc_key=doc_id, document=doc)


BASE_ROWS = [("1", "Кабинет", "20.00"), ("2", "Коридор", "10.00"), ("3", "Санузел", "4.00"), ("4", "Склад", "12.00"), ("5", "Тамбур", "3.50")]


def pair_results(pd_rows, rd_rows, *, pd_floor="1", rd_floor="1"):
    pd = ref_of(make_table(pd_rows, floor=pd_floor), "PD", 1)
    rd = ref_of(make_table(rd_rows, floor=rd_floor), "RD", 2)
    return ec.compare_pairs([pd], [rd])


def with_changes(rows, **changes):
    out = []
    for key, name, area in rows:
        spec = changes.get(key)
        if spec == "drop":
            continue
        out.append((key, spec[0] if isinstance(spec, tuple) and spec[0] else name, spec[1] if isinstance(spec, tuple) and spec[1] else area))
    return out


class DbCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1")
        self.db.add(self.matrix)
        self.db.flush()
        self.process = InspectionProcess(id="proc-free", project_id=1, organization_id=1, object_id="OBJ-FREE", matrix_version="official-132-v1",
                                         dataset_version="synthetic-v1", upload_scenario="FULL", status="RUNNING")
        self.db.add(self.process)
        self.db.flush()
        self.params = [self.add_param(code, name, unit) for code, name, unit in (
            ("PZ-001", "Площадь застройки", "м²"), ("PZ-002", "Общая площадь здания", "м²"), ("PZ-003", "Полезная / Расчетная площадь", "м²"),
            ("PZ-004", "Строительный объем (Общий)", "м³"), ("PZ-005", "Строительный объем (Подземный)", "м³"),
            ("PZ-006", "Строительный объем (Надземный)", "м³"), ("PZ-007", "Этажность (надземная)", "ед."), ("PZ-008", "Высота здания", "м"),
            ("PZ-010", "Количество квартир", "шт."))]
        self.by_code = {p.code: p for p in self.params}

    def tearDown(self):
        self.db.close()

    def add_param(self, code, name, unit):
        param = Param(matrix_version_id=self.matrix.id, project_id=1, organization_id=1, code=code, parameter_name=name, unit=unit,
                      section="Раздел 1", review_priority="MEDIUM", is_active=True)
        self.db.add(param)
        self.db.flush()
        return param

    def add_document(self, stage="PD", name="doc.pdf"):
        doc = DocumentVersion(project_id=1, organization_id=1, object_id="OBJ-FREE", filename=name, dataset_stage=stage,
                              doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage], document_code=f"AR-{name}", file_hash="a" * 64)
        self.db.add(doc)
        self.db.flush()
        return doc

    def add_value_group(self, code, stage_values, *, confidence=0.6, group_key="generic:anchor"):
        """A matrix group that carries values per stage plus one cited fragment per stage."""
        param = self.by_code[code]
        group = EvidenceGroup(process_id=self.process.id, project_id=1, organization_id=1, object_id="OBJ-FREE", param_id=param.id,
                              matrix_version="official-132-v1", finding_status="CANDIDATE", comparability_status="COMPARABLE",
                              group_key=group_key + ":" + code, confidence=confidence,
                              delta={"values": {k: str(v) for k, v in stage_values.items()}, "matrix_scope": "MATRIX"})
        self.db.add(group)
        self.db.flush()
        for stage, value in stage_values.items():
            doc = self.add_document(stage, f"{code}-{stage}.pdf")
            self.db.add(EvidenceFragment(
                evidence_group_id=group.id, document_version_id=doc.id, stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage],
                page=3, bbox=[0.1, 0.1, 0.2, 0.2], bbox_pdf=[84.2, 59.5, 168.4, 119.0], page_width=842.0, page_height=595.0,
                extracted_value=str(value), role="expected" if stage == "PD" else "actual", dataset_file_id=f"F-{code}-{stage}", file_sha256="a" * 64))
        self.db.flush()
        return group


# ---------------------------------------------------------------- UNMATCHED_DIFF
class UnmatchedDiffTests(unittest.TestCase):
    def test_changed_area_is_a_hypothesis_with_both_sources_and_boxes(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"3": (None, "4.60")}))
        found, stats = unmatched_diff.detect(results, object_id="OBJ", already_reported=set())
        self.assertEqual(len(found), 1, stats)
        hypothesis = found[0]
        self.assertEqual(hypothesis.method, base.METHOD_UNMATCHED)
        self.assertEqual((hypothesis.expected, hypothesis.actual), ("4.00", "4.60"))
        self.assertEqual({ref.stage for ref in hypothesis.evidence}, {"PD", "RD"})
        self.assertTrue(all(ref.bbox_pdf and ref.page for ref in hypothesis.evidence))
        self.assertIn("матрицей не установлен", hypothesis.description)          # the 1 % is a noise floor, not M-002's threshold
        self.assertEqual(hypothesis.details["evidence_required"], ["PD", "RD"])

    def test_identical_tables_and_rounding_noise_give_nothing(self):
        self.assertEqual(unmatched_diff.detect(pair_results(BASE_ROWS, BASE_ROWS), object_id="OBJ", already_reported=set())[0], [])
        noisy = with_changes(BASE_ROWS, **{"1": (None, "20.10")})                      # 0.5 % < 1 %
        self.assertEqual(unmatched_diff.detect(pair_results(BASE_ROWS, noisy), object_id="OBJ", already_reported=set())[0], [])

    def test_room_present_in_one_stage_only(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"5": "drop"}))
        found, _ = unmatched_diff.detect(results, object_id="OBJ", already_reported=set())
        self.assertEqual([h.detector for h in found], [unmatched_diff.DETECTOR_PRESENCE])
        self.assertIn("не найдено в экспликации РД", found[0].description)
        self.assertTrue(all(ref.bbox_pdf for ref in found[0].evidence))               # absence is cited with the RD table box

    def test_name_changes_are_left_to_the_semantic_detector(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"4": ("Склад ГСМ", None)}))
        self.assertEqual(unmatched_diff.detect(results, object_id="OBJ", already_reported=set())[0], [])

    def test_rooms_of_different_floors_are_not_joined_by_number(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"3": (None, "9.00")}), pd_floor="1", rd_floor="2")
        self.assertEqual(results, [])                                                  # incompatible scopes: no pair, no hypothesis

    def test_difference_already_written_by_the_matrix_path_is_not_repeated(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"3": (None, "4.60")}))
        first = unmatched_diff.detect(results, object_id="OBJ", already_reported=set())[0][0]
        reported = {(base.norm_token("3"), tpar.ROLE_AREA, ec.TYPE_AREA_CHANGED, base.norm_token("4.00"), base.norm_token("4.60"))}
        found, stats = unmatched_diff.detect(results, object_id="OBJ", already_reported=reported)
        self.assertEqual(found, [])
        self.assertEqual(stats["skipped_reported_by_matrix"], 1)
        self.assertTrue(first.dedup_key)

    def test_same_claim_from_two_copies_is_one_hypothesis(self):
        results = pair_results(BASE_ROWS, with_changes(BASE_ROWS, **{"3": (None, "4.60")}))
        one = unmatched_diff.detect(results, object_id="OBJ", already_reported=set())[0]
        two = unmatched_diff.detect(results, object_id="OBJ", already_reported=set())[0]
        other_object = unmatched_diff.detect(results, object_id="OTHER", already_reported=set())[0]
        merged, dropped = base.merge_duplicates(one + two + other_object)
        self.assertEqual((len(merged), dropped), (2, 1))                                # dedup only inside one object
        self.assertNotEqual(one[0].dedup_key, other_object[0].dedup_key)


# ---------------------------------------------------------------- SEMANTIC_DISSONANCE
FAKE_SIM = {frozenset({"Туалет", "Санузел"}): 0.9, frozenset({"Техническое", "Склад ГСМ"}): 0.31, frozenset({"Санузел", "Сан. узел"}): 0.93, frozenset({"Склад", "Кладовая"}): 0.64}


def fake_similarity(a, b):
    return 1.0 if a == b else FAKE_SIM.get(frozenset({a, b}), 0.5)


def fake_embed(text):
    return ("vec", text)


class SemanticDissonanceTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(semantic_dissonance.sem, "similarity", side_effect=fake_similarity),
                   mock.patch.object(semantic_dissonance.sem, "embed", side_effect=fake_embed),
                   mock.patch.object(semantic_dissonance.sem, "cosine", side_effect=lambda a, b: fake_similarity(a[1], b[1])),
                   mock.patch.object(semantic_dissonance.sem, "is_enabled", return_value=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self, pd_name, rd_name):
        pd_rows = [("1", "Кабинет", "20.00"), ("2", "Коридор", "10.00"), ("3", pd_name, "4.00"), ("4", "Тамбур", "12.00"), ("5", "Холл", "3.50")]
        rd_rows = [("1", "Кабинет", "20.00"), ("2", "Коридор", "10.00"), ("3", rd_name, "4.00"), ("4", "Тамбур", "12.00"), ("5", "Холл", "3.50")]
        return pair_results(pd_rows, rd_rows)

    def test_the_tz_example_is_a_hypothesis(self):
        found, stats = semantic_dissonance.detect(self.rows("Техническое", "Склад ГСМ"), object_id="OBJ", already_reported=set())
        self.assertEqual(len(found), 1, stats)
        self.assertEqual(found[0].method, base.METHOD_SEMANTIC)
        self.assertIn("«Техническое»", found[0].description)
        self.assertIn("«Склад ГСМ»", found[0].description)
        self.assertEqual({ref.stage for ref in found[0].evidence}, {"PD", "RD"})
        self.assertLess(found[0].details["similarity"], semantic_dissonance.dissonance_max_sim())

    def test_a_respelled_name_is_not_a_dissonance(self):
        found, stats = semantic_dissonance.detect(self.rows("Туалет", "Санузел"), object_id="OBJ", already_reported=set())
        self.assertEqual(found, [])
        self.assertEqual(stats["not_dissonant"], 1)

    def test_names_with_a_shared_stem_are_not_a_dissonance(self):
        found, _ = semantic_dissonance.detect(self.rows("Склад", "Склад инвентаря"), object_id="OBJ", already_reported=set())
        self.assertEqual(found, [])

    def test_without_the_model_the_detector_abstains_and_says_so(self):
        with mock.patch.object(semantic_dissonance.sem, "embed", return_value=None):
            found, stats = semantic_dissonance.detect(self.rows("Техническое", "Склад ГСМ"), object_id="OBJ", already_reported=set())
        self.assertEqual(found, [])
        self.assertEqual(stats["abstained"], "semantic_model_unavailable")

    def test_matrix_reported_name_change_is_not_repeated(self):
        reported = {(base.norm_token("3"), tpar.ROLE_NAME, ec.TYPE_NAME_CHANGED, base.norm_token("Техническое"), base.norm_token("Склад ГСМ"))}
        found, stats = semantic_dissonance.detect(self.rows("Техническое", "Склад ГСМ"), object_id="OBJ", already_reported=reported)
        self.assertEqual((found, stats["skipped_reported_by_matrix"]), ([], 1))

    def test_classes_come_from_the_objects_own_names(self):
        classes = semantic_dissonance.functional_classes(["Санузел", "Сан. узел", "Техническое"])
        self.assertEqual(classes["Санузел"], classes["Сан. узел"])
        self.assertNotEqual(classes["Санузел"], classes["Техническое"])


# ---------------------------------------------------------------- LOGICAL_ANALYSIS (internal consistency)
class LogicRuleTests(DbCase):
    def run_relations(self, scope=None):
        return logic_rules.detect_relations(self.db, self.process, scope or {p.code for p in self.params}, object_id="OBJ-FREE")

    def test_volume_mismatch_fires_with_the_cited_values(self):
        self.add_value_group("PZ-004", {"PD": "10000"})
        self.add_value_group("PZ-005", {"PD": "2000"})
        self.add_value_group("PZ-006", {"PD": "7000"})
        found, stats = self.run_relations()
        hit = [h for h in found if h.detector == "LR-101"]
        self.assertEqual(len(hit), 1, stats)
        self.assertEqual(hit[0].method, base.METHOD_LOGICAL)
        self.assertIsNone(hit[0].normative_base)                                  # arithmetic consistency: no norm is claimed
        self.assertEqual(len(hit[0].evidence), 3)
        self.assertTrue(all(ref.bbox_pdf and ref.page for ref in hit[0].evidence))

    def test_consistent_values_and_missing_inputs_give_nothing(self):
        self.add_value_group("PZ-004", {"PD": "10000"})
        self.add_value_group("PZ-005", {"PD": "3000"})
        self.add_value_group("PZ-006", {"PD": "7000"})
        self.assertEqual([h for h in self.run_relations()[0] if h.detector == "LR-101"], [])
        self.add_value_group("PZ-002", {"PD": "5000"})                            # LR-102 lacks its second input: abstention, no hypothesis
        self.assertEqual([h for h in self.run_relations()[0] if h.detector == "LR-102"], [])

    def test_stages_are_never_mixed(self):
        self.add_value_group("PZ-004", {"PD": "10000"})
        self.add_value_group("PZ-005", {"RD": "2000"})
        self.add_value_group("PZ-006", {"PD": "7000"})
        self.assertEqual([h for h in self.run_relations()[0] if h.detector == "LR-101"], [])

    def test_hypotheses_and_clarifications_are_not_inputs(self):
        group = self.add_value_group("PZ-002", {"PD": "5000"})
        useful = self.add_value_group("PZ-003", {"PD": "9000"})
        self.assertTrue([h for h in self.run_relations()[0] if h.detector == "LR-102"])
        group.finding_status = "CLARIFICATION_REQUIRED"
        useful.group_key = "hyp:logical_analysis:x"
        self.db.flush()
        self.assertEqual([h for h in self.run_relations()[0] if h.detector == "LR-102"], [])

    def test_rule_out_of_the_runs_parameter_scope_is_not_evaluated(self):
        self.add_value_group("PZ-002", {"PD": "5000"})
        self.add_value_group("PZ-003", {"PD": "9000"})
        found, stats = self.run_relations(scope={"PZ-002"})
        self.assertEqual(found, [])
        self.assertFalse(stats["scope_complete"])

    def test_height_per_floor_rule(self):
        self.add_value_group("PZ-007", {"PD": "9"})
        self.add_value_group("PZ-008", {"RD": "20"}); self.add_value_group("PZ-007", {"RD": "9"}, group_key="g2")
        found, _ = self.run_relations()
        self.assertEqual([h.details["stage"] for h in found if h.detector == "LR-103"], ["RD"])

    def test_schedule_total_must_equal_the_sum_of_rooms(self):
        total = tpar.Total(label="Итого", values=["45.00"], bbox=[0, 80, 100, 90])
        table = make_table(BASE_ROWS, totals=[total])                              # rooms sum to 49.5
        refs = {"PD": [ref_of(table, "PD", 1)], "RD": []}
        found, stats = logic_rules.detect_schedule_rules(refs, object_id="OBJ", db=self.db, process=self.process, params_in_scope=set())
        self.assertEqual([h.detector for h in found], ["LR-105"], stats)
        ok_total = tpar.Total(label="Итого", values=["49.50"], bbox=[0, 80, 100, 90])
        refs = {"PD": [ref_of(make_table(BASE_ROWS, totals=[ok_total]), "PD", 1)], "RD": []}
        self.assertEqual(logic_rules.detect_schedule_rules(refs, object_id="OBJ", db=self.db, process=self.process, params_in_scope=set())[0], [])

    def test_a_total_far_from_the_sum_is_a_misread_cell_not_a_claim(self):
        far = tpar.Total(label="Итого", values=["6.40"], bbox=[0, 80, 100, 90])
        refs = {"PD": [ref_of(make_table(BASE_ROWS, totals=[far]), "PD", 1)], "RD": []}
        found, stats = logic_rules.detect_schedule_rules(refs, object_id="OBJ", db=self.db, process=self.process, params_in_scope=set())
        self.assertEqual(found, [])
        self.assertEqual(stats["lr105_unreliable_total"], 1)

    def test_a_gap_in_the_row_numbers_means_the_parse_is_incomplete_and_nothing_is_claimed(self):
        for rows in ([("1", "А", "10.00"), ("2", "Б", "10.00"), ("4", "Г", "10.00")],                 # 3 is missing
                     [("3", "А", "10.00"), ("4", "Б", "10.00"), ("5", "Г", "10.00")]):                # does not start at 1
            self.check_incomplete(rows)

    def check_incomplete(self, rows):
        total = tpar.Total(label="Итого", values=["40.00"], bbox=[0, 80, 100, 90])
        refs = {"PD": [ref_of(make_table(rows, totals=[total]), "PD", 1)], "RD": []}
        found, stats = logic_rules.detect_schedule_rules(refs, object_id="OBJ", db=self.db, process=self.process, params_in_scope=set())
        self.assertEqual(found, [])
        self.assertEqual(stats["lr105_incomplete_parse"], 1)

    def test_the_three_original_rules_are_unchanged(self):
        from app.domain.logical_analysis import LOGICAL_RULES

        self.assertEqual([(r.rule_id, r.condition_param_code, r.condition_value, r.confidence) for r in LOGICAL_RULES],
                         [("LR-001", "PZ-007", 6, 0.55), ("LR-002", "PZ-007", 6, 0.45), ("LR-003", "PZ-012", 0, 0.5)])


# ---------------------------------------------------------------- NORMATIVE
class NormativeTests(DbCase):
    def entry(self, **kw):
        row = NormativeBaseEntry(document_name="Тестовый свод", document_number="СП-тест", section="п. 1", parameter_name="Высота здания",
                                 is_active=True, **kw)
        self.db.add(row)
        self.db.flush()
        return row

    def test_value_outside_an_admin_entry_is_a_hypothesis_with_the_entrys_basis(self):
        self.add_value_group("PZ-008", {"PD": "18"})
        param = next(p for p in self.params if p.code == "PZ-008")
        param.min_value, param.max_value = 100.0, 200.0  # the active normative-base entry supplies the effective bounds
        self.entry(max_value=15.0)
        found, stats = normative.detect(self.db, self.process, self.params, object_id="OBJ-FREE")
        self.assertEqual(len(found), 1, stats)
        self.assertEqual(found[0].method, base.METHOD_NORMATIVE)
        self.assertIn("Тестовый свод СП-тест, п. 1", found[0].normative_base)
        self.assertIn("не более 15", found[0].expected)

    def test_empty_normative_base_preserves_behavior_even_if_param_bounds_are_set(self):
        self.add_value_group("PZ-008", {"PD": "18"})
        param = next(p for p in self.params if p.code == "PZ-008")
        param.min_value, param.max_value = 100.0, 200.0
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])

    def test_value_inside_range_inactive_entry_expired_entry_and_empty_base_give_nothing(self):
        self.add_value_group("PZ-008", {"PD": "12"})
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])      # empty base
        row = self.entry(max_value=15.0)
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])      # inside range
        row.max_value = 10.0
        row.is_active = False
        self.db.flush()
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])      # inactive
        row.is_active = True
        row.effective_to = datetime.utcnow() - timedelta(days=1)
        self.db.flush()
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])      # no longer in force
        row.effective_to = None
        self.db.flush()
        self.assertEqual(len(normative.detect(self.db, self.process, self.params, object_id="O")[0]), 1)  # in force and violated

    def test_entry_matches_the_parameter_by_code_too(self):
        self.add_value_group("PZ-008", {"PD": "5"})
        self.entry(min_value=7.0).parameter_name = "PZ-008"
        self.assertEqual(len(normative.detect(self.db, self.process, self.params, object_id="O")[0]), 1)

    def test_no_bounds_are_invented_without_a_value(self):
        self.entry(max_value=15.0)
        self.assertEqual(normative.detect(self.db, self.process, self.params, object_id="O")[0], [])

    def test_active_entry_window_accepts_timezone_aware_dates(self):
        from datetime import timezone

        now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        row = self.entry(max_value=15.0, effective_from=now - timedelta(days=1), effective_to=now + timedelta(days=1))
        self.assertTrue(normative._in_force(row, now))


# ---------------------------------------------------------------- ML_PATTERN_STAT
class MlStatTests(unittest.TestCase):
    def refs(self, areas):
        rows = [(str(i + 1), "Спальня", f"{a:.2f}") for i, a in enumerate(areas)]
        return {"PD": [ref_of(make_table(rows), "PD", 1)], "RD": []}

    def test_outlier_in_a_group_of_comparable_rooms(self):
        found, _ = ml_stat.detect(self.refs([12.0, 12.4, 11.8, 12.2, 12.1, 11.9, 12.3, 40.0]), object_id="OBJ")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].method, base.METHOD_ML)
        self.assertIn("не обученная модель", found[0].description)

    def test_uniform_group_and_small_group_give_nothing(self):
        self.assertEqual(ml_stat.detect(self.refs([12.0, 12.4, 11.8, 12.2, 12.1, 11.9, 12.3, 13.0]), object_id="OBJ")[0], [])
        self.assertEqual(ml_stat.detect(self.refs([12.0, 40.0, 12.1]), object_id="OBJ")[0], [])

    def test_off_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            for name in ("CASE10_FREE_HYPOTHESES_ML", "CASE10_FREE_HYPOTHESES_METHODS"):
                __import__("os").environ.pop(name, None)
            self.assertNotIn(base.METHOD_ML, base.enabled_methods())
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES_ML": "1"}):
            self.assertIn(base.METHOD_ML, base.enabled_methods())


    def test_default_methods_are_the_measured_ones(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            for name in ("CASE10_FREE_HYPOTHESES_ML", "CASE10_FREE_HYPOTHESES_METHODS"):
                __import__("os").environ.pop(name, None)
            self.assertEqual(base.enabled_methods(), (base.METHOD_UNMATCHED, base.METHOD_CHANGE_SHEET, base.METHOD_LAYERS, base.METHOD_SEMANTIC, base.METHOD_NORMATIVE))
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES_METHODS": "LOGICAL_ANALYSIS,UNMATCHED_DIFF"}):
            self.assertEqual(base.enabled_methods(), (base.METHOD_UNMATCHED, base.METHOD_LOGICAL))


# ---------------------------------------------------------------- limits
class LimitTests(unittest.TestCase):
    def candidates(self, n, doc_id=2):
        doc = make_doc(doc_id, "RD")
        out = []
        for i in range(n):
            ref = base.EvidenceRef(document=doc, page=1, stage="RD", role="actual", bbox_pdf=[0, 0, 1, 1], page_width=10, page_height=10)
            out.append(base.HypothesisCandidate(method=base.METHOD_UNMATCHED, detector="d", dedup_key=f"k{i}", description="x", confidence=0.5 + i / 1000,
                                                evidence=[ref]))
        return out

    def test_per_document_and_per_object_limits_cut_the_weakest_and_report_it(self):
        kept, cut = base.apply_limits(self.candidates(10), per_document=4, per_object=100)
        self.assertEqual((len(kept), cut["cut_by_document_limit"]), (4, 6))
        self.assertEqual([c.dedup_key for c in kept], ["k9", "k8", "k7", "k6"])           # the strongest first
        kept, cut = base.apply_limits(self.candidates(5, 2) + self.candidates(5, 3), per_document=100, per_object=7)
        self.assertEqual((len(kept), cut["cut_by_object_limit"]), (7, 3))


# ---------------------------------------------------------------- store: incremental behaviour
def one_candidate(doc_id=2, value="4.60", key="k"):
    pd_doc, rd_doc = make_doc(1, "PD"), make_doc(doc_id, "RD")
    evidence = [base.EvidenceRef(document=pd_doc, page=1, stage="PD", role="expected", bbox_pdf=[0, 0, 50, 10], page_width=842, page_height=595, value="4.00"),
                base.EvidenceRef(document=rd_doc, page=2, stage="RD", role="actual", bbox_pdf=[0, 0, 50, 10], page_width=842, page_height=595, value=value)]
    return base.HypothesisCandidate(method=base.METHOD_UNMATCHED, detector="unmatched_diff.area", dedup_key=key, description=f"area {value}",
                                    confidence=0.7, evidence=evidence, location="3", expected="4.00", actual=value,
                                    details={"evidence_required": ["PD", "RD"]}, editions=(1, doc_id))


class StoreTests(DbCase):
    def setUp(self):
        super().setUp()
        for doc_id in (1, 2):                                   # fragment rows reference real documents
            self.db.add(DocumentVersion(id=doc_id, project_id=1, organization_id=1, filename=f"{doc_id}.pdf"))
        self.db.flush()
        self.sentinel = store.ensure_free_search_param(self.db, self.process, self.params)

    def run_store(self, candidates, ran=(base.METHOD_UNMATCHED,)):
        stats = store.store_candidates(self.db, self.process, self.sentinel, candidates)
        stale = store.reconcile_stale(self.db, self.process, self.sentinel, stats["keys"], ran_methods=ran)
        self.db.flush()
        return stats, stale

    def group(self):
        return self.db.query(EvidenceGroup).filter(EvidenceGroup.param_id == self.sentinel.id).one()

    def test_hypothesis_is_a_suspicion_outside_the_matrix_with_the_tz_structure(self):
        self.run_store([one_candidate()])
        group = self.group()
        self.assertEqual((group.finding_status, group.delta["matrix_scope"]), ("SUSPICION", "FREE_SEARCH"))
        row = self.db.query(Suspicion).one()
        self.assertEqual((row.discovery_method, row.inspector_status), ("UNMATCHED_DIFF", "PENDING"))
        self.assertIsNotNone(row.pd_reference)
        self.assertIsNotNone(row.rd_reference)
        self.assertEqual(row.normative_base, "не определено")
        self.assertFalse(self.sentinel.is_active)                                  # never one of the matrix parameters
        self.assertEqual(self.db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == group.id).count(), 2)

    def test_same_evidence_twice_writes_nothing_new(self):
        self.run_store([one_candidate()])
        stats, _ = self.run_store([one_candidate()])
        self.assertEqual((stats["created"], stats["unchanged"]), (0, 1))
        self.assertEqual(self.db.query(EvidenceGroup).filter(EvidenceGroup.param_id == self.sentinel.id).count(), 1)

    def test_unreviewed_hypothesis_is_refreshed_and_a_vanished_one_removed(self):
        self.run_store([one_candidate(value="4.60")])
        stats, _ = self.run_store([one_candidate(value="4.80")])
        self.assertEqual(stats["refreshed"], 1)
        self.assertEqual(self.group().actual_value, "4.80")
        _, stale = self.run_store([])
        self.assertEqual(stale["removed"], 1)
        self.assertEqual(self.db.query(EvidenceGroup).filter(EvidenceGroup.param_id == self.sentinel.id).count(), 0)
        self.assertEqual(self.db.query(Suspicion).count(), 0)                       # the Suspicions row goes with the group

    def test_reviewed_hypothesis_keeps_its_review_when_the_evidence_changes_or_vanishes(self):
        self.run_store([one_candidate(value="4.60")])
        self.db.query(Suspicion).one().inspector_status = "REJECTED"
        self.db.flush()
        stats, _ = self.run_store([one_candidate(value="4.80")])
        group = self.group()
        self.assertEqual(stats["flagged"], 1)
        self.assertEqual(group.actual_value, "4.60")                                 # the reviewed values stay
        self.assertTrue(group.needs_reverification)
        self.assertEqual(group.delta["pending_reverification"]["actual_value"], "4.80")
        self.assertEqual(self.db.query(Suspicion).one().inspector_status, "REJECTED")
        _, stale = self.run_store([])
        self.assertEqual((stale["removed"], stale["flagged"]), (0, 1))
        self.assertTrue(self.group().delta["evidence_no_longer_found"])

    def test_methods_that_did_not_run_are_not_reconciled(self):
        self.run_store([one_candidate()])
        _, stale = self.run_store([], ran=(base.METHOD_SEMANTIC,))
        self.assertEqual(stale, {"removed": 0, "flagged": 0})
        self.assertEqual(self.db.query(EvidenceGroup).filter(EvidenceGroup.param_id == self.sentinel.id).count(), 1)

    def test_conversion_needs_file_page_and_box_on_every_cited_place(self):
        self.run_store([one_candidate()])
        group = self.group()
        self.assertEqual(store.conversion_blockers(self.db, group), [])
        fragment = self.db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == group.id, EvidenceFragment.role == "actual").one()
        fragment.bbox = fragment.bbox_pdf = None
        self.db.flush()
        self.assertIn("missing bbox", "; ".join(store.conversion_blockers(self.db, group)))
        with self.assertRaises(store.ConversionRefused):
            store.convert_to_candidate(self.db, group, reason="r", user_id=1, process=self.process)
        self.assertEqual(group.finding_status, "SUSPICION")

    def test_conversion_needs_every_stage_the_claim_cites(self):
        self.run_store([one_candidate()])
        group = self.group()
        self.db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == group.id, EvidenceFragment.role == "actual").delete()
        self.db.flush()
        self.assertIn("no evidence from stage RD", store.conversion_blockers(self.db, group))

    def test_converted_candidate_is_out_of_the_matrix_and_a_rerun_never_turns_it_back(self):
        self.run_store([one_candidate()])
        group = self.group()
        store.convert_to_candidate(self.db, group, reason="проверено по листам", user_id=7, process=self.process)
        self.assertEqual((group.finding_status, group.delta["matrix_scope"]), ("CANDIDATE", "OUT_OF_MATRIX"))
        self.assertTrue(group.delta["converted_to_candidate"])
        stats, _ = self.run_store([one_candidate(value="5.10")])
        group = self.group()
        self.assertEqual(group.finding_status, "CANDIDATE")                          # not reverted to SUSPICION
        self.assertEqual(stats["kept_converted"], 1)
        self.assertTrue(group.needs_reverification)
        _, stale = self.run_store([])
        self.assertEqual((stale["removed"], stale["flagged"]), (0, 1))

    def test_a_group_that_is_not_an_open_hypothesis_is_never_converted(self):
        self.run_store([one_candidate()])
        group = self.group()
        group.finding_status = "CANDIDATE"
        self.db.flush()
        self.assertEqual(store.conversion_blockers(self.db, group), ["not_an_open_hypothesis"])


# ---------------------------------------------------------------- runner + invariants
class RunnerTests(DbCase):
    def test_switch_off_does_nothing_and_is_the_documented_default(self):
        self.assertEqual(base.DEFAULT_ENABLED in ("0", "1"), True)
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "0"}):
            self.assertEqual(run_free_hypotheses(self.db, self.process, self.params, {}, by_id={1: make_doc(1, "PD")}), {"enabled": False})
        self.assertEqual(self.db.query(EvidenceGroup).count(), 0)

    def test_logical_module_hook_runs_the_package_only_when_the_flag_is_on(self):
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "0"}), \
             mock.patch("app.domain.free_hypotheses.runner.run_free_hypotheses") as runner:
            run_logical_analysis_module(self.db, self.process, self.params, {}, fragments=[], by_id={})
            runner.assert_not_called()
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "1"}), \
             mock.patch("app.domain.free_hypotheses.runner.run_free_hypotheses", return_value={"ok": 1}) as runner:
            run_logical_analysis_module(self.db, self.process, self.params, {}, fragments=[], by_id={})
            runner.assert_called_once()

    def test_a_failing_detector_run_never_breaks_the_matrix_run(self):
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "1"}), \
             mock.patch("app.domain.free_hypotheses.runner.run_free_hypotheses", side_effect=RuntimeError("boom")):
            run_logical_analysis_module(self.db, self.process, self.params, {}, fragments=[], by_id={})
        self.assertEqual(self.db.query(EvidenceGroup).count(), 0)                    # nothing half-written, session still usable

    def test_full_run_on_values_stores_audits_and_counts_no_violation(self):
        self.add_value_group("PZ-002", {"PD": "5000"})
        self.add_value_group("PZ-003", {"PD": "9000"})
        docs = {1: make_doc(1, "PD")}
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "1"}), \
             mock.patch("app.domain.free_hypotheses.runner._load_tables", side_effect=lambda ctx: setattr(ctx, "refs", {"PD": [], "RD": []})), \
             mock.patch("app.domain.comparison_gate.GateContext", return_value=SimpleNamespace(facts={})):
            summary = run_free_hypotheses(self.db, self.process, self.params, {}, by_id=docs, methods=[base.METHOD_LOGICAL])
        self.assertEqual(summary["by_method"], {"LOGICAL_ANALYSIS": 1}, summary)
        group = self.db.query(EvidenceGroup).filter(EvidenceGroup.group_key.like("hyp:%")).one()
        self.assertEqual(group.finding_status, "SUSPICION")
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "FREE_HYPOTHESES_RUN").count(), 1)
        with mock.patch.dict("os.environ", {"CASE10_FREE_HYPOTHESES": "1"}),              mock.patch("app.domain.free_hypotheses.runner._load_tables", side_effect=lambda ctx: setattr(ctx, "refs", {"PD": [], "RD": []})),              mock.patch("app.domain.comparison_gate.GateContext", return_value=SimpleNamespace(facts={})):
            run_free_hypotheses(self.db, self.process, self.params, {}, by_id=docs, methods=[base.METHOD_LOGICAL])
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "FREE_HYPOTHESES_RUN").count(), 1)   # identical re-run: no new entry
        # a hypothesis is neither a violation in the protocol statistics nor an exported matrix check
        from app.domain.protocol_annex2 import annex2_payload
        from evaluation.exporter import protocol_to_submission
        from app.domain.v3_pipeline import evidence_group_to_dict

        finding = evidence_group_to_dict(self.db, group, include_fragments=True)
        annex = annex2_payload([finding], [], object_info={}, matrix_size=132)
        stats = {row["name"]: row["count"] for row in annex["section_2_statistics"]}
        self.assertEqual((stats["Подозрений ИИ"], stats["Подтвержденных нарушений"], stats["Кандидатов на проверку"]), (1, 0, 0))
        self.assertEqual(len(annex["section_6_ai_suspicions"]), 1)
        self.assertEqual(protocol_to_submission({"object_id": "OBJ-FREE", "findings": [finding]})["checks"], [])
        self.assertEqual(len(protocol_to_submission({"object_id": "OBJ-FREE", "findings": [finding]}, include_suspicions=True)["checks"]), 1)

    def test_collect_survives_a_table_scan_failure(self):
        ctx = RunContext(db=self.db, process=self.process, params=self.params, documents=[], object_id="OBJ-FREE", gate=None)
        with mock.patch("app.domain.free_hypotheses.runner._load_tables", side_effect=RuntimeError("scan")):
            candidates, stats, complete = collect_candidates(ctx, [base.METHOD_UNMATCHED, base.METHOD_NORMATIVE])
        self.assertEqual(candidates, [])
        self.assertIn(base.METHOD_NORMATIVE, complete)
        self.assertNotIn(base.METHOD_UNMATCHED, stats)

    def test_matrix_reported_keys_reads_the_s1_groups(self):
        self.db.add(EvidenceGroup(process_id=self.process.id, project_id=1, organization_id=1, object_id="OBJ-FREE", param_id=self.params[0].id,
                                  group_key="explication:abc", finding_status="CANDIDATE",
                                  delta={"location": "3", "field": "AREA", "discrepancy_type": "AREA_CHANGED", "values": {"PD": "4.00", "RD": "4.60"}}))
        self.db.flush()
        self.assertIn((base.norm_token("3"), "AREA", "AREA_CHANGED", base.norm_token("4.00"), base.norm_token("4.60")),
                      matrix_reported_keys(self.db, self.process))


# ---------------------------------------------------------------- "Преобразовать в кандидата" (endpoint)
from test_case10_s5_workbench import WorkbenchTestBase  # noqa: E402


class ConversionEndpointTests(WorkbenchTestBase):
    def make_hypothesis(self):
        group = self.seed()
        row = self.db.get(EvidenceGroup, group["id"])
        row.finding_status = "SUSPICION"
        row.delta = {**(row.delta or {}), "matrix_scope": "FREE_SEARCH", "discovery_method": "UNMATCHED_DIFF", "description": "d", "criticality": "СУЩЕСТВЕННОЕ"}
        self.db.commit()
        return group["id"]

    def url(self, gid):
        return f"/api/case10/evidence-groups/{gid}/hypothesis-to-candidate"

    def test_conversion_is_an_explicit_action_with_a_reason_and_an_audit_trail(self):
        gid = self.make_hypothesis()
        self.assertEqual(self.post(self.url(gid), {"comment": ""}).status_code, 422)
        done = self.post(self.url(gid), {"comment": "значения подтверждены по обоим листам"})
        self.assertEqual(done.status_code, 200, done.text)
        group = self.db.get(EvidenceGroup, gid)
        self.assertEqual((group.finding_status, group.delta["matrix_scope"]), ("CANDIDATE", "OUT_OF_MATRIX"))
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "HYPOTHESIS_CONVERTED_TO_CANDIDATE").count(), 1)
        edit = self.db.query(InspectorEdit).filter(InspectorEdit.evidence_group_id == gid, InspectorEdit.entity_type == "HYPOTHESIS_CONVERSION").one()
        self.assertEqual((edit.action, edit.reason), ("TO_CANDIDATE", "значения подтверждены по обоим листам"))
        self.assertEqual(self.db.query(EvidenceDecision).filter(EvidenceDecision.evidence_group_id == gid).count(), 0)   # no violation was confirmed
        again = self.post(self.url(gid), {"comment": "ещё раз"})
        self.assertEqual(again.status_code, 422, again.text)                                                             # no longer an open hypothesis

    def test_conversion_is_refused_without_concrete_coordinates(self):
        gid = self.make_hypothesis()
        fragment = self.db.query(EvidenceFragment).filter(EvidenceFragment.evidence_group_id == gid).first()
        fragment.role, fragment.bbox, fragment.bbox_pdf = "expected", None, None
        self.db.commit()
        refused = self.post(self.url(gid), {"comment": "основание"})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("bbox", refused.json()["detail"])
        self.assertEqual(self.db.get(EvidenceGroup, gid).finding_status, "SUSPICION")

    def test_plain_findings_and_finalized_protocols_are_refused(self):
        group = self.seed()
        self.assertEqual(self.post(self.url(group["id"]), {"comment": "основание"}).status_code, 422)
        gid = self.make_hypothesis()
        process = self.db.get(InspectionProcess, self.process_id)
        process.status = "FINALIZED"
        self.db.commit()
        self.assertEqual(self.post(self.url(gid), {"comment": "основание"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
