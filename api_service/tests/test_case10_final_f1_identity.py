"""Step F1: one (parameter code, location) in the export names one object of comparison.

Before: the same room number of one floor compared with two RD volumes, a page-quality warning next to a clarification
request, two table pairs of one floor total -- all exported with an identical (code, location), and a consumer keyed by
object + parameter + location (the scorer) kept an arbitrary one of them. Synthetic groups only; the real demo packages
are checked by evaluation/phase12/final_f1_inventory.py.
"""
from __future__ import annotations

import copy
import itertools
import json
import random
import unittest
from collections import Counter

from evaluation.exporter import protocol_to_submission
from evaluation import export_identity as ei

OBJ = "OBJ-F1"


def fragment(file_id, page, stage="working", role="actual", value="1"):
    return {"file_id": file_id, "page": page, "stage": stage, "role": role, "value": value, "bbox_normalized": [0.1, 0.1, 0.2, 0.2]}


def pair_group(key, *, location="1", rd_file="RD-A", rd_page=4, pd_page=19, floor="1", status="CANDIDATE", pd_value="10.00", rd_value="10.50",
               field="AREA", dtype="AREA_CHANGED", rd_title="Экспликация помещений 1-го этажа", code="PZ-003", pd_title="Спецификация помещений 1-го этажа"):
    return {"id": hash(key) % 100000, "object_id": OBJ, "group_key": key, "finding_status": status, "parameter": {"code": code},
            "fragments": [fragment("PD-1", pd_page, "project", "expected", pd_value), fragment(rd_file, rd_page, "working", "actual", rd_value)],
            "delta": {"source": "explication_compare", "parameter_code": code, "location": location, "discrepancy_type": dtype, "field": field,
                      "scope": {"PD": {"floor": floor}, "RD": {"floor": floor}},
                      "tables": {"PD": {"file_id": "PD-1", "page": pd_page, "title": pd_title},
                                 "RD": {"file_id": rd_file, "page": rd_page, "title": rd_title}},
                      "values": {"PD": pd_value, "RD": rd_value}}}


def service_group(key, source, reason, status, code="PZ-001", extra=None):
    return {"id": hash(key) % 100000, "object_id": OBJ, "group_key": key, "finding_status": status, "parameter": {"code": code},
            "fragments": [], "delta": {"source": source, "reason": reason, "location": OBJ, **(extra or {})}}


def export(groups):
    return protocol_to_submission({"payload": {"object_id": OBJ, "findings": copy.deepcopy(groups)}})


def pairs_of(submission):
    return Counter((c["parameter_code"], c["location"]) for c in submission["checks"])


class IdentityPlanTests(unittest.TestCase):
    def test_same_room_on_two_rd_volumes_gets_two_places(self):
        groups = [pair_group("a", rd_file="RD-A", rd_page=7, status="NEGATIVE_VERIFIED", rd_value="10.00"),
                  pair_group("b", rd_file="RD-B", rd_page=4, status="CANDIDATE", rd_value="10.50")]
        checks = export(groups)["checks"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)
        plain = [c for c in checks if c["location"] == "1"]
        self.assertEqual(len(plain), 1)
        self.assertEqual(plain[0]["violation_label"], "VIOLATION_PRESENT", "the strongest claim keeps the plain place (the organizer's naming)")
        other = [c for c in checks if c["location"] != "1"][0]
        self.assertEqual(other["location_base"], "1")
        self.assertIn("RD-A", other["location"])
        self.assertNotIn("PD-1", other["location"], "only what varies is named")
        self.assertEqual(len({c["finding_id"] for c in checks}), 2)
        self.assertEqual(len({c["evidence_group_id"] for c in checks}), 2)

    def test_two_tables_on_one_page_are_two_objects(self):
        """Floor 1 and the mezzanine of one PD page: same file and page, different tables (title) -> not folded."""
        groups = [pair_group("a", floor="1", pd_title="Спецификация помещений 1-го этажа", rd_value="10.50"),
                  pair_group("b", floor="антресоль", pd_title="Спецификация помещений антресольного этажа", rd_value="11.00")]
        checks = export(groups)["checks"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)
        self.assertTrue(any("антресоль" in c["location"] for c in checks))

    def test_identical_duplicates_collapse_to_one_record_and_keep_every_citation(self):
        first = pair_group("a", status="CANDIDATE")
        second = pair_group("b", status="CANDIDATE")
        second["fragments"].append(fragment("RD-A", 5, "working", "table", None))
        checks = export([first, second])["checks"]
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(check["location"], "1")
        self.assertEqual(len(check["related_group_ids"]), 1)
        self.assertTrue(check["superseded"][0]["evidence_merged"])
        self.assertIn(5, {item["pdf_page_number"] for item in check["evidence"]}, "the citation of the folded duplicate is kept")

    def test_contradiction_on_one_object_the_stronger_claim_wins_and_the_weak_one_is_referenced(self):
        weak = pair_group("a", status="NEGATIVE_VERIFIED", rd_value="10.00")
        strong = pair_group("b", status="CANDIDATE", rd_value="10.50")
        strong["delta"]["tables"]["RD"]["page"] = 4          # same file, same page and title: the same RD table
        weak["delta"]["tables"]["RD"]["page"] = 4
        for order in ((weak, strong), (strong, weak)):
            checks = export(list(order))["checks"]
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["violation_label"], "VIOLATION_PRESENT")
            self.assertEqual(checks[0]["superseded"][0]["violation_label"], "NO_VIOLATION")
            self.assertFalse(checks[0]["superseded"][0]["evidence_merged"])
            self.assertEqual(len(checks[0]["evidence"]), 2, "the weak statement's citations are not mixed into the winner")

    def test_different_fields_of_one_room_stay_two_records(self):
        groups = [pair_group("a", field="AREA"), pair_group("b", field="NAME", dtype="ROOM_FUNCTION_CHANGED", pd_value="Кухня", rd_value="Комната")]
        checks = export(groups)["checks"]
        self.assertEqual(len(checks), 2)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)
        self.assertTrue(any("наименование" in c["location"] for c in checks))

    def test_service_rows_get_a_suffix_by_reason(self):
        groups = [service_group("q", "page_quality", "LOW_QUALITY", "NOT_COMPARABLE"),
                  service_group("c", "file_registry", "CLARIFICATION_REQUIRED", "CLARIFICATION_REQUIRED"),
                  service_group("m", "document_annotations", "missing_stage", "MISSING_EVIDENCE", extra={"stages": ["ID"]})]
        checks = export(groups)["checks"]
        self.assertEqual(len(checks), 3)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)
        by_status = {c["finding_status_internal"]: c for c in checks}
        self.assertEqual(by_status["MISSING_EVIDENCE"]["location"], OBJ, "the substantive statement keeps the object id")
        self.assertEqual(by_status["NOT_COMPARABLE"]["location"], f"{OBJ} (предупреждение качества)")
        self.assertEqual(by_status["CLARIFICATION_REQUIRED"]["location"], f"{OBJ} (запрос уточнения)")

    def test_a_service_row_never_outranks_the_statement_it_accompanies(self):
        groups = [service_group("a", "page_quality", "LOW_QUALITY", "NOT_COMPARABLE"),
                  service_group("z", "document_annotations", "values_not_extracted", "NOT_COMPARABLE")]
        for order in (groups, list(reversed(groups))):
            checks = export(order)["checks"]
            plain = [c for c in checks if c["location"] == OBJ]
            self.assertEqual(len(plain), 1)
            self.assertEqual(plain[0]["rule_version"].split("/")[0], "document_annotations")

    def test_other_groups_fall_back_to_a_readable_qualifier_and_stay_unique(self):
        def other(key, field):
            return {"id": 1, "object_id": OBJ, "group_key": key, "finding_status": "CANDIDATE", "parameter": {"code": "PZ-003"}, "fragments": [],
                    "delta": {"source": "room_drawing_compare", "location": "005", "field": field}}
        checks = export([other("k1", "SYSTEM_ADDED"), other("k2", "SYSTEM_ADDED"), other("k3", "FLOW")])["checks"]
        self.assertEqual(len(checks), 3)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)

    def test_a_qualified_place_never_collides_with_a_plain_place(self):
        groups = [pair_group("a", rd_file="RD-A", rd_value="10.50"), pair_group("b", rd_file="RD-B", rd_value="10.70")]
        taken = export(groups)["checks"][1]["location"]          # whichever got the qualifier
        groups.append(pair_group("c", location=taken, rd_file="RD-C", rd_value="12.00"))
        checks = export(groups)["checks"]
        self.assertEqual(len(checks), 3)
        self.assertEqual(max(pairs_of({"checks": checks}).values()), 1)

    def test_no_collision_means_nothing_changes(self):
        groups = [pair_group("a", location="1"), pair_group("b", location="2"), service_group("s", "page_quality", "LOW_QUALITY", "NOT_COMPARABLE")]
        checks = export(groups)["checks"]
        self.assertEqual([c["location"] for c in checks], ["1", "2", OBJ])
        self.assertTrue(all("location_base" not in c and "related_group_ids" not in c for c in checks))
        plan = ei.plan_identity([(g, {"parameter_code": g["parameter"]["code"], "location": g["delta"]["location"]}) for g in groups])
        self.assertTrue(all(p["fold_into"] is None and p["qualifier"] is None for p in plan))


class ExportInvarianceTests(unittest.TestCase):
    def _package(self):
        return [pair_group("a", rd_file="RD-A", rd_page=7, status="NEGATIVE_VERIFIED", rd_value="10.00"),
                pair_group("b", rd_file="RD-B", rd_page=4, status="CANDIDATE", rd_value="10.50"),
                pair_group("b2", rd_file="RD-B", rd_page=4, status="CANDIDATE", rd_value="10.50"),
                pair_group("c", rd_file="RD-B", rd_page=5, floor="2", pd_page=20, rd_title="Экспликация помещений 2-го этажа", rd_value="9.00"),
                pair_group("d", location="2", field="NAME", dtype="ROOM_FUNCTION_CHANGED", pd_value="Кухня", rd_value="Спальня"),
                pair_group("d2", location="2", field="AREA"),
                service_group("q", "page_quality", "LOW_QUALITY", "NOT_COMPARABLE"),
                service_group("c1", "file_registry", "CLARIFICATION_REQUIRED", "CLARIFICATION_REQUIRED"),
                service_group("q2", "page_quality", "LOW_QUALITY", "NOT_COMPARABLE", code="PZ-002"),
                service_group("m2", "document_annotations", "missing_stage", "MISSING_EVIDENCE", code="PZ-002", extra={"stages": ["ID"]}),
                service_group("n3", "document_annotations", "missing_stage", "MISSING_EVIDENCE", code="PZ-002", extra={"stages": ["ID"]})]

    @staticmethod
    def _canon(submission):
        return sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in submission["checks"])

    def test_export_does_not_depend_on_the_order_of_the_groups(self):
        groups = self._package()
        base = self._canon(export(groups))
        rng = random.Random(7)
        for _ in range(40):
            shuffled = list(groups)
            rng.shuffle(shuffled)
            self.assertEqual(self._canon(export(shuffled)), base)
        self.assertEqual(self._canon(export(list(reversed(groups)))), base)

    def test_no_shared_code_and_place_with_different_content(self):
        submission = export(self._package())
        self.assertEqual(max(pairs_of(submission).values()), 1)
        self.assertEqual(len({c["finding_id"] for c in submission["checks"]}), len(submission["checks"]))
        self.assertEqual(len({c["evidence_group_id"] for c in submission["checks"]}), len(submission["checks"]))

    def test_two_runs_are_byte_identical(self):
        groups = self._package()
        first = json.dumps(export(groups), sort_keys=False, ensure_ascii=False)
        second = json.dumps(export(copy.deepcopy(groups)), sort_keys=False, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_folding_reaches_a_fixed_point(self):
        """Exporting what was exported again from the same groups gives the same answer (no state)."""
        groups = self._package()
        self.assertEqual(self._canon(export(groups)), self._canon(export(groups)))


if __name__ == "__main__":
    unittest.main()
