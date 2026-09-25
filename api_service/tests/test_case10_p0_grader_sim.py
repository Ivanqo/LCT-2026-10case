"""Phase 12 / P0: evaluation/grader_sim.py on toy data (no dataset needed)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "api_service"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evaluation import grader_sim as gs  # noqa: E402


def check(code, location, label="VIOLATION_PRESENT", evidence=(), object_id="OBJ"):
    return {"parameter_code": code, "location": location, "violation_label": label,
            "evidence": [{"stage": s, "file_id": f, "pdf_page_number": p, "bbox_norm": list(b) if b else None,
                          "document_code": dc, "revision": rv} for s, f, p, b, dc, rv in
                         [(e + (None,) * (6 - len(e))) for e in evidence]]}


def preds(*checks, object_id="OBJ", world=None):
    return [gs.normalize_prediction(c, object_id=object_id, world=world) for c in checks]


POS = gs.gold_item(gold_id="G1", object_id="OBJ", label=gs.POSITIVE, code="IOS4-078", location="140",
                   evidence=[{"stage": "PD", "file_id": "F1", "page": 88, "rects": [[0.1, 0.1, 0.3, 0.3]]},
                             {"stage": "RD", "file_id": "F2", "page": 18, "rects": [[0.5, 0.5, 0.7, 0.7]]}],
                   violation_type="CONFIGURATION_MISMATCH")
NEG = gs.gold_item(gold_id="N1", object_id="OBJ", label=gs.NEGATIVE, code="KR-055", location="Фундаментная плита",
                   evidence=[{"stage": "PD", "file_id": "F3", "page": 49}, {"stage": "RD", "file_id": "F4", "page": 27}])
GOOD_EVIDENCE = (("PD", "F1", 88, (0.1, 0.1, 0.3, 0.3)), ("RD", "F2", 18, (0.5, 0.5, 0.7, 0.7)))


class GraderSimTest(unittest.TestCase):
    def test_wilson_and_iou(self):
        self.assertEqual(gs.wilson(0, 0)["rate"], None)
        w = gs.wilson(6, 6)
        self.assertEqual((w["rate"], w["wilson95"][1]), (1.0, 1.0))
        self.assertAlmostEqual(w["wilson95"][0], 0.6097, places=3)
        self.assertAlmostEqual(gs.iou([0, 0, 1, 1], [0, 0, 1, 0.5]), 0.5)
        self.assertEqual(gs.iou([0, 0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.6]), 0.0)

    def test_true_positive_counts_in_every_variant_and_codes_match_across_conventions(self):
        report = gs.evaluate([POS, NEG], preds(check("M-078", "140", evidence=GOOD_EVIDENCE)))
        for variant in gs.VARIANTS:
            s = report["variants"][variant]
            self.assertEqual((s["recall"]["k"], s["recall"]["n"], s["precision_closed"]["rate"]), (1, 1, 1.0), variant)
        self.assertEqual(report["variants"][gs.PRIMARY_VARIANT]["false_positive_rate"]["k"], 0)
        self.assertEqual(report["localization_completeness"]["file_page"]["rate"], 1.0)

    def test_right_key_with_a_wrong_page_is_a_miss_and_a_labelled_false_positive(self):
        report = gs.evaluate([POS], preds(check("IOS4-078", "140", evidence=(("PD", "F1", 88), ("RD", "F2", 19)))))
        key_only = report["variants"][gs.KEY_ONLY]
        primary = report["variants"][gs.PRIMARY_VARIANT]
        self.assertEqual(key_only["recall"]["k"], 1)
        self.assertEqual((primary["recall"]["k"], primary["outcomes"]), (0, {"fp_labelled_positive_key": 1}))
        self.assertEqual(primary["precision_closed"]["rate"], 0.0)
        self.assertEqual(report["localization_completeness"]["file_page"]["rate"], 0.0)

    def test_strict_variant_needs_iou_and_skips_gold_without_rectangles(self):
        shifted = (("PD", "F1", 88, (0.25, 0.25, 0.45, 0.45)), ("RD", "F2", 18, (0.5, 0.5, 0.7, 0.7)))
        report = gs.evaluate([POS], preds(check("IOS4-078", "140", evidence=shifted)))
        self.assertEqual(report["variants"][gs.EVIDENCE_PAGE]["recall"]["k"], 1)
        self.assertEqual(report["variants"][gs.EVIDENCE_STRICT]["recall"]["k"], 0)
        page_level = gs.gold_item(gold_id="G2", object_id="OBJ", label=gs.POSITIVE, code="IOS4-079", location="012",
                                  evidence=[{"stage": "PD", "file_id": "F1", "page": 104}, {"stage": "RD", "file_id": "F2", "page": 17}])
        report = gs.evaluate([page_level], preds(check("IOS4-079", "012", evidence=(("PD", "F1", 104), ("RD", "F2", 17)))))
        strict = report["variants"][gs.EVIDENCE_STRICT]
        self.assertEqual((strict["n_gold_positive_evaluable"], strict["outcomes"]), (0, {"not_evaluable": 1}))
        self.assertEqual(report["localization_completeness"]["strict_not_evaluable"], 1)

    def test_location_strict_and_loose(self):
        room = gs.gold_item(gold_id="G3", object_id="OBJ", label=gs.POSITIVE, code="IOS4-079", location="012",
                            evidence=[{"stage": "PD", "file_id": "F1", "page": 104}])
        report = gs.evaluate([room], preds(check("IOS4-079", "12", evidence=(("PD", "F1", 104),))))
        self.assertEqual(report["variants"][gs.EVIDENCE_PAGE]["recall"]["k"], 0)
        self.assertEqual(report["variants"][gs.EVIDENCE_PAGE_LOOSE]["recall"]["k"], 1)

    def test_false_positive_on_a_verified_negative_and_open_vs_closed_precision(self):
        report = gs.evaluate([POS, NEG], preds(
            check("IOS4-078", "140", evidence=GOOD_EVIDENCE),
            check("KR-055", "фундаментная  плита", evidence=(("PD", "F3", 49),)),     # flags the negative
            check("PZ-001", "SITE", evidence=(("PD", "F9", 1),)),                     # unlabelled key
            check("KR-055", "Стена в грунте", label="NO_VIOLATION"),                   # not a positive prediction
        ))
        s = report["variants"][gs.PRIMARY_VARIANT]
        self.assertEqual(s["false_positive_rate"]["k"], 1)
        self.assertEqual(s["negatives_flagged"], ["N1"])
        self.assertEqual(s["precision_closed"]["rate"], 0.5)                           # 1 TP / (1 + 1 on the negative)
        self.assertAlmostEqual(s["precision_open"]["rate"], 0.3333, places=4)          # + the unlabelled one
        self.assertEqual(s["outcomes"], {"tp": 1, "fp_labelled_negative_key": 1, "fp_unlabelled": 1})

    def test_page_pair_gold_matches_by_pages_and_assumed_code_separately(self):
        pair = gs.gold_item(gold_id="ALT-V01", object_id="OBJ-NEW-ALT", label=gs.POSITIVE, match_mode=gs.MATCH_PAGE_PAIR,
                            assumed_codes=("PZ-002", "PZ-003"), violation_type="EXPLICATION",
                            evidence=[{"stage": "PD", "file_id": "A15", "page": 19, "rects": [[0.78, 0.1, 0.91, 0.35]]},
                                      {"stage": "PD", "file_id": "A15", "page": 20, "rects": [[0.84, 0.16, 0.94, 0.4]]},
                                      {"stage": "RD", "file_id": "A77", "page": 4, "rects": [[0.8, 0.03, 0.93, 0.27]]}])
        wrong_code = check("PZ-001", "SITE", evidence=(("PD", "A15", 20, (0.84, 0.16, 0.94, 0.4)), ("RD", "A77", 4, (0.8, 0.03, 0.93, 0.27))))
        report = gs.evaluate([pair], preds(wrong_code, object_id="OBJ-NEW-ALT"))
        self.assertEqual(report["variants"][gs.KEY_ONLY]["recall"]["k"], 1)                # page pair found
        self.assertEqual(report["variants"][gs.EVIDENCE_PAGE]["recall"]["k"], 0)           # code not in the assumed table
        self.assertEqual(report["variants"][gs.EVIDENCE_PAGE]["outcomes"], {"fp_labelled_positive_key": 1})
        right_code = {**wrong_code, "parameter_code": "M-003"}
        report = gs.evaluate([pair], preds(right_code, object_id="OBJ-NEW-ALT"))
        self.assertEqual(report["variants"][gs.EVIDENCE_STRICT]["recall"]["k"], 1)
        one_side = check("PZ-003", "SITE", evidence=(("PD", "A15", 19),))
        self.assertEqual(gs.evaluate([pair], preds(one_side, object_id="OBJ-NEW-ALT"))["variants"][gs.KEY_ONLY]["recall"]["k"], 0)

    def test_page_pair_negative_is_flagged_by_any_positive_on_its_pages(self):
        neg_pair = gs.gold_item(gold_id="POL17-N01", object_id="OBJ-NEW-POL17", label=gs.NEGATIVE, match_mode=gs.MATCH_PAGE_PAIR,
                                evidence=[{"stage": "PD", "file_id": "P31", "page": 26}, {"stage": "RD", "file_id": "P96", "page": 7}])
        elsewhere = preds(check("PZ-002", "SITE", evidence=(("PD", "P31", 30),)), object_id="OBJ-NEW-POL17")
        on_page = preds(check("PZ-002", "SITE", evidence=(("RD", "P96", 7),)), object_id="OBJ-NEW-POL17")
        self.assertEqual(gs.evaluate([neg_pair], elsewhere)["variants"][gs.PRIMARY_VARIANT]["false_positive_rate"]["k"], 0)
        self.assertEqual(gs.evaluate([neg_pair], on_page)["variants"][gs.PRIMARY_VARIANT]["false_positive_rate"]["k"], 1)

    def test_worlds_keep_seeded_cases_apart(self):
        seed_pos = gs.gold_item(gold_id="S1", object_id="OBJ", world="case-1", label=gs.POSITIVE, code="PZ-002", location="SITE",
                                evidence=[{"stage": "PD", "file_id": "F1", "page": 1}, {"stage": "RD", "file_id": "F2", "page": 2}])
        seed_neg = gs.gold_item(gold_id="S2", object_id="OBJ", world="case-2", label=gs.NEGATIVE, code="PZ-002", location="SITE",
                                evidence=[{"stage": "PD", "file_id": "F1", "page": 1}, {"stage": "RD", "file_id": "F2", "page": 2}])
        flagged = check("PZ-002", "SITE", evidence=(("PD", "F1", 1), ("RD", "F2", 2)))
        report = gs.evaluate([seed_pos, seed_neg], preds(flagged, world="case-1") + preds({**flagged, "violation_label": "NO_VIOLATION"}, world="case-2"))
        s = report["variants"][gs.PRIMARY_VARIANT]
        self.assertEqual((s["recall"]["k"], s["false_positive_rate"]["k"], s["precision_closed"]["rate"]), (1, 0, 1.0))
        report = gs.evaluate([seed_pos, seed_neg], preds(flagged, world="case-2"))
        s = report["variants"][gs.PRIMARY_VARIANT]
        self.assertEqual((s["recall"]["k"], s["false_positive_rate"]["k"]), (0, 1))

    def test_document_linkage_against_the_registry(self):
        registry = {"OBJ": {"F1": {"stage": "PD"}, "F2": {"stage": "RD_ID_MIXED"}, "F5": {"stage": "RD", "excluded": True},
                            "F6": {"stage": "RD", "document_code": "C-6", "revision": "2"}}}
        linkage = gs.document_linkage(preds(
            check("IOS4-078", "140", evidence=(("PD", "F1", 1), ("RD", "F2", 2), ("RD", "F5", 3), ("ID", "F6", 4, None, "C-6", "2"))),
            check("PZ-001", "SITE", label="COMPARISON_IMPOSSIBLE", evidence=(("PD", "F9", 1),)),     # not a verdict: ignored
        ), registry)
        self.assertEqual((linkage["n_cited_evidence"], linkage["object_stage_exact"]["k"], linkage["cites_excluded_file"]), (4, 2, 1))
        self.assertEqual((linkage["code_revision_verifiable"], linkage["code_revision_exact"]["k"]), (1, 0))   # stage ID != RD
        self.assertEqual(gs.document_linkage([], None), {"available": False})

    def test_slices_and_critical_recall(self):
        free = gs.gold_item(gold_id="G4", object_id="OBJ", label=gs.POSITIVE, code="FREE-HEATING-001", location="267",
                            evidence=[{"stage": "PD", "file_id": "F1", "page": 99}], violation_type="MISSING_DESIGN_ELEMENT")
        report = gs.evaluate([POS, free, NEG], preds(check("IOS4-078", "140", evidence=GOOD_EVIDENCE)))
        self.assertEqual(report["by_type"]["MISSING_DESIGN_ELEMENT"]["recall"]["k"], 0)
        self.assertEqual(report["by_type"]["CONFIGURATION_MISMATCH"]["recall"]["k"], 1)
        self.assertEqual(report["by_section"]["FREE_SEARCH"]["n_pos"], 1)
        self.assertTrue(POS["critical"])
        self.assertFalse(free["critical"])
        self.assertEqual(report["variants"][gs.PRIMARY_VARIANT]["critical_recall"]["rate"], 1.0)
        self.assertIn("FREE-HEATING-001", [free["code_key"]])


if __name__ == "__main__":
    unittest.main()
