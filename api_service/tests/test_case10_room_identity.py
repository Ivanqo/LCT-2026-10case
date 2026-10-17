"""Room identity, system linkage and PD/RD composition comparison (expert review, Step A).

The three reproducing examples come from the written expert review of
`room_drawing_compare.py`: duplicated room numbers were silently overwritten,
system labels were linked to a room by a fixed-confidence nearest-distance rule,
and the comparison only saw systems that disappeared.
"""
from __future__ import annotations

import itertools
import json
import unittest

from app.domain.room_drawing_compare import (
    compare_project_to_working,
    detect_sheet_context,
    extract_room_words,
    room_system_links,
    room_system_rows,
    room_zones_for_snapshot,
)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _strip(entries):
    """Comparable view of compare results (drops the heavy source rows)."""
    out = []
    for entry in entries:
        out.append({key: value for key, value in entry.items() if key not in {"expected", "actual", "records"}}
                   | {"record_systems": sorted(
                       (side, tuple(r.get("systems") or ())) for side, rows in (entry.get("records") or {}).items() for r in rows)})
    return out


def _row(location, systems, **extra):
    return {"location": location, "systems": list(systems), **extra}


def _snapshot(words, *, text="", width=1000.0, height=500.0, page=1):
    return {"page": page, "width": width, "height": height, "text": text, "words": words}


class ExpertExampleTests(unittest.TestCase):
    def test_duplicate_room_number_is_a_conflict_independent_of_record_order(self):
        rd = [_row("101", ["V2"])]
        results = []
        for order in itertools.permutations([_row("101", ["P1"]), _row("101", ["V2"])]):
            results.append(_canon(_strip(compare_project_to_working(list(order), rd))))
        self.assertEqual(len(set(results)), 1)
        entries = compare_project_to_working([_row("101", ["V2"]), _row("101", ["P1"])], rd)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "CONFLICT")
        self.assertEqual(entries[0]["reason"], "DUPLICATE_ROOM_KEY")
        self.assertEqual(entries[0]["missing_systems"], [])      # no decision is made on a conflicting key
        self.assertEqual(sorted(tuple(r["systems"]) for r in entries[0]["records"]["PD"]), [("P1",), ("V2",)])

    def test_system_added_in_working_is_reported_separately_in_composition_mode(self):
        pd, rd = [_row("101", ["P1"])], [_row("101", ["P1", "V2"])]
        self.assertEqual(compare_project_to_working(pd, rd), [])        # default: disappearance only
        entries = compare_project_to_working(pd, rd, mode="composition")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["added_systems"], ["V2"])
        self.assertEqual(entries[0]["missing_systems"], [])
        self.assertEqual(entries[0]["status"], "ADDED")

    def test_missing_and_added_are_kept_apart(self):
        entries = compare_project_to_working(
            [_row("101", ["P1", "P2"])], [_row("101", ["P2", "V2"])], mode="composition")
        self.assertEqual((entries[0]["missing_systems"], entries[0]["added_systems"]), (["P1"], ["V2"]))
        self.assertEqual(entries[0]["status"], "MISSING_AND_ADDED")
        only_missing = compare_project_to_working(
            [_row("101", ["P1", "P2"])], [_row("101", ["P2", "V2"])], mode="disappearance")
        self.assertEqual(only_missing[0]["missing_systems"], ["P1"])
        self.assertEqual(only_missing[0]["status"], "MISSING")

    def test_label_between_two_rooms_is_ambiguous_not_silently_assigned(self):
        words = [
            {"text": "101", "bbox": [200.0, 100.0, 218.0, 112.0]},
            {"text": "102", "bbox": [200.0, 160.0, 218.0, 172.0]},
            {"text": "В2", "bbox": [200.0, 130.0, 218.0, 142.0]},
        ]
        links = room_system_links(_snapshot(words))
        self.assertEqual(links["rows"], [])
        self.assertEqual(len(links["ambiguous"]), 1)
        item = links["ambiguous"][0]
        self.assertEqual(item["label"], "V2")
        self.assertEqual(sorted(c["location"] for c in item["candidates"]), ["101", "102"])
        self.assertEqual(room_system_rows(_snapshot(words)), [])

    def test_clear_winner_is_linked_with_separate_confidences(self):
        words = [
            {"text": "101", "bbox": [200.0, 100.0, 218.0, 112.0]},
            {"text": "102", "bbox": [600.0, 100.0, 618.0, 112.0]},
            {"text": "В2", "bbox": [205.0, 120.0, 223.0, 132.0]},
        ]
        rows = room_system_rows(_snapshot(words))
        self.assertEqual([(r["location"], r["systems"]) for r in rows], [("101", ["V2"])])
        row = rows[0]
        self.assertIsNone(row["text_confidence"])        # text-layer word: no recognition score exists
        self.assertNotIn(row["link_confidence"], (0.88, 0.72, 0.8))
        self.assertEqual(row["confidence"], row["link_confidence"])   # compat: min of the known values
        self.assertEqual(row["wall_check"], "not_evaluated")

    def test_ocr_word_confidence_is_carried_through(self):
        words = [
            {"text": "101", "bbox": [200.0, 100.0, 218.0, 112.0]},
            {"text": "В2", "bbox": [205.0, 120.0, 223.0, 132.0], "confidence": 0.41},
        ]
        row = room_system_rows(_snapshot(words))[0]
        self.assertAlmostEqual(row["text_confidence"], 0.41)
        self.assertLessEqual(row["confidence"], 0.41)


class RoomIdentityTests(unittest.TestCase):
    def test_same_number_on_different_floors_is_not_merged(self):
        pd = [_row("101", ["P1"], floor="1"), _row("101", ["V2"], floor="2")]
        rd = [_row("101", ["P1"], floor="1"), _row("101", ["V2"], floor="2")]
        self.assertEqual(compare_project_to_working(pd, rd), [])
        rd_missing = [_row("101", ["P1"], floor="1"), _row("101", [], floor="2")]
        entries = compare_project_to_working(pd, rd_missing)
        self.assertEqual([(e["floor"], e["missing_systems"]) for e in entries], [("2", ["V2"])])

    def test_duplicate_full_key_is_conflict_but_identical_duplicates_are_merged(self):
        pd = [_row("101", ["P1"], floor="1"), _row("101", ["P1"], floor="1")]
        self.assertEqual(compare_project_to_working(pd, [_row("101", ["P1"], floor="1")]), [])
        pd = [_row("101", ["P1"], floor="1"), _row("101", ["P2"], floor="1")]
        entries = compare_project_to_working(pd, [_row("101", ["P1"], floor="1")])
        self.assertEqual((entries[0]["status"], entries[0]["reason"]), ("CONFLICT", "DUPLICATE_ROOM_KEY"))

    def test_unknown_floor_on_one_side_with_several_candidates_is_ambiguous(self):
        pd = [_row("101", ["P1"])]                                   # floor not recoverable from the sheet
        rd = [_row("101", ["P1"], floor="1"), _row("101", [], floor="2")]
        entries = compare_project_to_working(pd, rd)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["status"], entries[0]["reason"]), ("AMBIGUOUS", "AMBIGUOUS_ROOM_MATCH"))
        self.assertEqual(entries[0]["missing_systems"], [])

    def test_unknown_floor_with_single_candidate_matches_by_number_and_says_so(self):
        entries = compare_project_to_working(
            [_row("101", ["P1"])], [_row("101", ["V2"], floor="1")], mode="composition")
        self.assertEqual(entries[0]["match_basis"], "room_number_only")
        self.assertEqual(entries[0]["missing_systems"], ["P1"])

    def test_rows_of_unknown_floor_on_different_sheets_are_not_one_room(self):
        pd = [_row("101", ["P1"], sheet="1:5"), _row("101", ["V2"], sheet="1:6")]
        rd = [_row("101", ["P1"], sheet="2:3")]
        entries = compare_project_to_working(pd, rd)
        # both sheet-scoped PD rooms are candidates for the single RD room: nothing is overwritten
        self.assertTrue(all(e["status"] in {"AMBIGUOUS", "CONFLICT"} for e in entries))
        self.assertNotIn("MISSING", {e["status"] for e in entries})

    def test_ambiguous_label_is_not_reported_as_missing(self):
        pd = [_row("101", ["P1", "V2"])]
        rd = [_row("101", ["P1"], ambiguous_systems=["V2"])]
        self.assertEqual(compare_project_to_working(pd, rd), [])
        entries = compare_project_to_working(pd, rd, include_unresolved=True)
        self.assertEqual(entries[0]["unresolved_systems"], ["V2"])
        self.assertEqual(entries[0]["missing_systems"], [])

    def test_result_is_invariant_to_row_order(self):
        pd = [_row("101", ["P1"], floor="1"), _row("102", ["P2", "V3"], floor="1"), _row("101", ["V1"], floor="2")]
        rd = [_row("101", ["P1"], floor="1"), _row("102", ["P2", "V4"], floor="1"), _row("101", ["V1", "V9"], floor="2")]
        seen = set()
        for pd_order in itertools.permutations(pd):
            for rd_order in itertools.permutations(rd):
                seen.add(_canon(_strip(compare_project_to_working(list(pd_order), list(rd_order), mode="composition"))))
        self.assertEqual(len(seen), 1)


class SheetContextTests(unittest.TestCase):
    def test_floor_and_building_come_from_sheet_text(self):
        ctx = detect_sheet_context(_snapshot([], text="Вентиляция. План 3 этажа на отм. +9.000. Корпус 2"))
        self.assertEqual((ctx["floor"], ctx["building"]), ("3", "2"))
        self.assertEqual(ctx["floor_source"], "sheet_text")

    def test_several_floors_on_one_sheet_leave_the_floor_undefined(self):
        ctx = detect_sheet_context(_snapshot([], text="План 1 этажа  План 2 этажа"))
        self.assertIsNone(ctx["floor"])
        self.assertEqual(ctx["floor_status"], "multiple")

    def test_no_marker_means_unknown_not_guessed(self):
        ctx = detect_sheet_context(_snapshot([], text="Спецификация оборудования"))
        self.assertEqual((ctx["floor"], ctx["building"], ctx["floor_status"]), (None, None, "not_found"))

    def test_rows_inherit_floor_from_their_sheet(self):
        words = [{"text": "101", "bbox": [200.0, 100.0, 218.0, 112.0]},
                 {"text": "В2", "bbox": [205.0, 120.0, 223.0, 132.0]}]
        rows = room_system_rows(_snapshot(words, text="План 2 этажа"))
        self.assertEqual(rows[0]["floor"], "2")


class SameNumberOnOneSheetTests(unittest.TestCase):
    def test_two_labels_with_the_same_number_stay_two_rooms(self):
        words = [
            {"text": "101", "bbox": [100.0, 100.0, 118.0, 112.0]},
            {"text": "В2", "bbox": [105.0, 120.0, 123.0, 132.0]},
            {"text": "101", "bbox": [700.0, 100.0, 718.0, 112.0]},
            {"text": "В3", "bbox": [705.0, 120.0, 723.0, 132.0]},
        ]
        rows = room_system_rows(_snapshot(words))
        self.assertEqual(sorted((r["location"], tuple(r["systems"])) for r in rows),
                         [("101", ("V2",)), ("101", ("V3",))])
        # and the comparison reports the duplicate instead of keeping one of them
        entries = compare_project_to_working(rows, [_row("101", ["V3"])])
        self.assertEqual(entries[0]["status"], "CONFLICT")

    def test_zone_index_flags_duplicate_numbers(self):
        words = [{"text": "101", "bbox": [100.0, 100.0, 118.0, 112.0]},
                 {"text": "101", "bbox": [700.0, 100.0, 718.0, 112.0]},
                 {"text": "205", "bbox": [400.0, 300.0, 418.0, 312.0]}]
        zones = room_zones_for_snapshot(_snapshot(words))
        self.assertTrue(zones["101"]["duplicate_on_sheet"])
        self.assertFalse(zones["205"].get("duplicate_on_sheet"))


class ExcludedZoneTests(unittest.TestCase):
    def test_rooms_in_the_title_block_zone_are_reported_not_silently_dropped(self):
        words = [{"text": "101", "bbox": [200.0, 100.0, 218.0, 112.0]},
                 {"text": "777", "bbox": [200.0, 450.0, 218.0, 462.0]}]
        snapshot = _snapshot(words)
        self.assertEqual([r["room"] for r in extract_room_words(snapshot)], ["101"])
        links = room_system_links(snapshot)
        self.assertEqual([r["room"] for r in links["excluded_zone_rooms"]], ["777"])
        self.assertEqual(links["excluded_zone_rooms"][0]["bbox"], [200.0, 450.0, 218.0, 462.0])


if __name__ == "__main__":
    unittest.main()
