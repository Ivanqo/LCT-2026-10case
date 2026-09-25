"""Guards for the measurement bench (evaluation/measurement_bench, Phase 10 prompt A).

Pinned here: the statistics and normalisation the numbers rest on, the corpus contract (size, split, freeze hash, no hidden
object), the taxonomy rules of Metric 1, the seed mutations, and the VERIFIED/WEAK/UNTESTED status rule of the critical-recall
matrix.  Run with PYTHONPATH=api_service like the other evaluation tests.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from evaluation.measurement_bench import build_corpus, common, location_convention as lc, seed_l1  # noqa: E402
from evaluation.measurement_bench import critical_recall_matrix as crm  # noqa: E402
from evaluation.measurement_bench import metric1, metric3  # noqa: E402
from evaluation.metrics import _wilson_interval  # noqa: E402

CORPUS = common.CORPUS_DIR / "value_corpus_v1.jsonl"
MANIFEST = common.CORPUS_DIR / "corpus_manifest.json"


class StatisticsTests(unittest.TestCase):
    def test_wilson_matches_the_project_implementation(self):
        for k, n in ((0, 5), (5, 5), (3, 10), (17, 40), (1, 1), (0, 0)):
            if n:
                a, b = metric1.wilson(k, n), _wilson_interval(k, n)
                self.assertAlmostEqual(a[0], b[0], places=3)
                self.assertAlmostEqual(a[1], b[1], places=3)
        self.assertEqual(metric1.wilson(0, 0), (0.0, 1.0))

    def test_verified_needs_about_sixteen_independent_pairs(self):
        self.assertEqual(crm.status_from_pairs("NUMERIC_TABLE", 16, 16), "VERIFIED")
        self.assertEqual(crm.status_from_pairs("NUMERIC_TABLE", 15, 15), "WEAK")          # lower bound 0.796 < 0.80
        self.assertEqual(crm.status_from_pairs("NUMERIC_TABLE", 4, 4), "WEAK")
        self.assertEqual(crm.status_from_pairs("NUMERIC_TABLE", 30, 28), "WEAK")          # recall 0.933 < 0.95
        self.assertEqual(crm.status_from_pairs("ENUM", 0, 0), "UNTESTED")
        self.assertEqual(crm.status_from_pairs("RULE_PACK", 20, 20), "UNTESTED")
        self.assertEqual(crm.status_from_pairs("NONE", 0, 0), "NO_MECHANISM")

    def test_prior_weights_are_a_distribution(self):
        codes = [r["parameter_code"] for r in common.catalog_rows() if common.is_critical(r)]
        self.assertEqual(len(codes), 106)
        w = crm.prior_weights(codes)
        self.assertAlmostEqual(sum(x["weight"] for x in w.values()), 1.0, places=2)
        self.assertGreater(w["IOS4-078"]["weight"], w["PZ-001"]["weight"])                 # pilot code outweighs a plain critical code


class NormalisationTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(common.norm_number("17 140,2"), "17140.2")
        self.assertEqual(common.norm_number("88264,00"), "88264")
        self.assertEqual(common.norm_number("1 331,4"), "1331.4")
        self.assertIsNone(common.norm_number("нет"))

    def test_enum_look_alikes_and_letter_o(self):
        self.assertEqual(common.norm_enum("С0"), "C0")            # Cyrillic С + digit zero
        self.assertEqual(common.norm_enum("СО"), "C0")            # Cyrillic С + Cyrillic letter О (seen in real RD text)
        self.assertEqual(common.norm_enum("А500С"), "A500C")
        self.assertEqual(common.norm_enum(" III, "), "III")

    def test_location_match_levels(self):
        self.assertEqual(lc.match_level("Фундаментная плита", " фундаментная  плита. "), "STRICT")
        self.assertEqual(lc.match_level("012", "12"), "LOOSE")
        self.assertEqual(lc.match_level("012", "013"), "NONE")
        self.assertEqual(lc.match_level("Стена в грунте", "SITE"), "NONE")
        self.assertTrue(lc.location_ok("PZ-002", "SITE")[0])
        self.assertFalse(lc.location_ok("KR-055", "SITE")[0])                                # element-level code cannot match the constant SITE
        self.assertEqual(lc.expected_location_type("IOS4-078"), "ROOM")

    def test_public_gold_conventions_are_read_from_public_rows_only(self):
        seen = lc.observed_conventions()
        self.assertEqual(seen["n_rows"], 15)
        self.assertEqual(set(seen["location_types_seen"]), {"ROOM", "CONSTRUCTION_ELEMENT"})
        self.assertEqual(seen["location_types_not_seen_in_public_gold"], ["SITE"])


class SeedMutationTests(unittest.TestCase):
    def _snap(self):
        words = [{"text": "Строительный", "bbox": [10, 10, 60, 20]}, {"text": "объем", "bbox": [62, 10, 90, 20]}, {"text": "88264,00", "bbox": [100, 10, 140, 20]}, {"text": "м3", "bbox": [142, 10, 155, 20]}]
        return {"words": words, "text": " ".join(w["text"] for w in words), "width": 200, "height": 100}

    def _case(self, mutation):
        return {"code": "PZ-004", "family": "NUMERIC_TABLE", "mutation": mutation,
                "rd_obs": {"value": "88264,00", "norm": "88264.00", "bbox_pdf": [100, 10, 140, 20]}}

    def test_decrease_is_beyond_any_tolerance_and_within_tol_is_not(self):
        snap = self._snap()
        dec, text = seed_l1.mutate(copy.deepcopy(snap), self._case("DEC_ABOVE"))
        self.assertLess(Decimal(common.norm_number(text)), Decimal("88264") * Decimal("0.95"))
        within, text2 = seed_l1.mutate(copy.deepcopy(snap), self._case("WITHIN_TOL"))
        rel = abs(Decimal(common.norm_number(text2)) - Decimal("88264")) / Decimal("88264")
        self.assertLessEqual(rel, Decimal("0.0005") + Decimal("0.0000001"))                # inside the system's own 0.1% tolerance
        self.assertNotEqual(text2, "88264,00")

    def test_format_only_keeps_the_number(self):
        _snap, text = seed_l1.mutate(copy.deepcopy(self._snap()), self._case("FORMAT_ONLY"))
        self.assertEqual(Decimal(common.norm_number(text)), Decimal("88264"))
        self.assertNotEqual(text, "88264,00")

    def test_mutated_snapshot_is_parsed_back_by_the_real_extractor(self):
        from app.domain.generic_matrix_extraction import find_anchor_numeric_value

        base = find_anchor_numeric_value(self._snap(), "Строительный объем")
        self.assertEqual(base.decimal_value, Decimal("88264.00"))
        snap, _t = seed_l1.mutate(copy.deepcopy(self._snap()), self._case("FORMAT_ONLY"))
        again = find_anchor_numeric_value(snap, "Строительный объем")
        self.assertEqual(again.decimal_value, Decimal("88264"))                       # thousands blank / comma-dot variant: same number
        snap, text = seed_l1.mutate(copy.deepcopy(self._snap()), self._case("DEC_ABOVE"))
        changed = find_anchor_numeric_value(snap, "Строительный объем")
        self.assertEqual(changed.decimal_value, Decimal(common.norm_number(text)))
        self.assertLess(changed.decimal_value, Decimal("88264") * Decimal("0.95"))

    def test_mutator_never_edits_the_cached_original(self):
        original = self._snap()
        calls = []

        def orig(document, pages):
            return {1: original}

        mut = seed_l1.SnapshotMutator(orig)
        mut.rules[("F1", 1)] = lambda s: (s["words"][2].update(text="1"), s)[1]
        doc = type("D", (), {"dataset_file_id": "F1"})()
        out = mut(doc, [1])
        self.assertEqual(out[1]["words"][2]["text"], "1")
        self.assertEqual(original["words"][2]["text"], "88264,00")

    def test_compound_component_decrease_edits_the_component_and_is_never_a_no_op(self):
        # regressions: (1) 2 * 0.8 printed with 0 decimals is "2" again; (2) the bbox spans the whole context, the first WORD is not the component
        words = [{"text": "см.", "bbox": [1, 10, 9, 20]}, {"text": "листе", "bbox": [10, 10, 30, 20]}, {"text": "2.", "bbox": [31, 10, 36, 20]}, {"text": "2.", "bbox": [37, 10, 42, 20]}]
        snap = {"words": words, "text": " ".join(w["text"] for w in words), "width": 100, "height": 100}
        case = {"code": "KR-067", "family": "COMPOUND", "mutation": "COMPONENT_DEC",
                "rd_obs": {"values": ["2", "2"], "norms": ["2", "2"], "bbox_pdf": [1, 10, 42, 20]}}
        out, text = seed_l1.mutate(copy.deepcopy(snap), case)
        self.assertIsNotNone(out)
        self.assertLess(Decimal(common.norm_number(text)), Decimal("2"))
        self.assertEqual([w["text"] for w in out["words"]][:2], ["см.", "листе"])            # the label words are untouched
        self.assertEqual(out["words"][2]["text"], "1.")                                        # the first component word was edited, punctuation kept

    def test_enum_downgrade_table_lowers_the_class(self):
        self.assertEqual(seed_l1.DOWNGRADE["C0"], "C1")
        self.assertEqual(seed_l1.DOWNGRADE["A500C"], "A400")
        self.assertEqual(seed_l1.DOWNGRADE["I"], "II")


class Metric1TaxonomyTests(unittest.TestCase):
    def _triple(self, **kw):
        base = {"triple_id": "X::PZ-004::PD", "obj": "X", "family": "NUMERIC_TABLE", "param_code": "PZ-004", "stage": "PD", "file_id": "X-000001", "page": 5,
                "true_norm": "100", "accepted_norm": ["100"], "locations": [{"file_id": "X-000001", "page": 5}], "superseded_or_conflicting": []}
        base.update(kw)
        return base

    def _ext(self, obs=None, frags=(), window=()):
        return {"observations": {"PZ-004": {"PD": obs}} if obs else {}, "fragments": list(frags), "rendered_window": {"PZ-004": {"PD": [list(w) for w in window]}}}

    def _run(self, triple, ext, marker=None, stage="PD"):
        docs = {"X-000001": {"file_id": "X-000001", "stage": stage, "marker": marker}}
        by_code: dict = {}
        for f in ext["fragments"]:
            by_code.setdefault(f["code"], []).append(f)
        return metric1.classify(triple, ext, docs, by_code, {}, {})

    def test_correct(self):
        obs = {"file_id": "X-000001", "page": 5, "norm": "100.0", "value": "100,0", "extractor": "e", "bbox_pdf": None}
        r = self._run(self._triple(), self._ext(obs))
        self.assertEqual((r["status"], r["sub"]), ("CORRECT", "page_verified"))

    def test_not_tagged_reasons(self):
        r = self._run(self._triple(), self._ext(), marker=None)
        self.assertEqual((r["status"], r["sub"]), ("NOT_TAGGED", "doc_not_scanned"))
        r = self._run(self._triple(), self._ext(), marker={"status": "scanned"})
        self.assertEqual((r["status"], r["sub"]), ("NOT_TAGGED", "anchor_not_on_page"))

    def test_gated_stage_not_admitted(self):
        r = self._run(self._triple(), self._ext(), stage="RD_ID_MIXED")
        self.assertEqual((r["status"], r["sub"]), ("GATED", "stage_not_admitted"))

    def test_page_cap_versus_no_value(self):
        frag = {"code": "PZ-004", "file_id": "X-000001", "page": 5}
        r = self._run(self._triple(), self._ext(frags=[frag], window=[("X-000002", 1)]), marker={"status": "scanned"})
        self.assertEqual((r["status"], r["sub"]), ("GATED", "page_cap_outside_render_window"))
        r = self._run(self._triple(), self._ext(frags=[frag], window=[("X-000001", 5)]), marker={"status": "scanned"})
        self.assertEqual((r["status"], r["sub"]), ("NO_VALUE", "tagged_but_no_value_parsed"))

    def test_wrong_value_other_page_and_superseded(self):
        obs = {"file_id": "X-000009", "page": 1, "norm": "77", "value": "77", "extractor": "e", "bbox_pdf": None}
        r = self._run(self._triple(), self._ext(obs))
        self.assertEqual((r["status"], r["sub"]), ("WRONG_VALUE", "other_page"))
        t = self._triple(superseded_or_conflicting=[{"value_norm": "77", "file_id": "X-000009", "page": 1}])
        r = self._run(t, self._ext(obs))
        self.assertEqual((r["status"], r["sub"]), ("WRONG_VALUE", "superseded_or_conflicting_place"))

    def test_wrong_row_when_value_is_on_the_true_page(self):
        obs = {"file_id": "X-000001", "page": 5, "norm": "77", "value": "77", "extractor": "e", "bbox_pdf": None}
        r = self._run(self._triple(), self._ext(obs))
        self.assertEqual(r["status"], "WRONG_ROW")

    def test_value_equal_by_family(self):
        self.assertTrue(metric1.value_equal("ENUM", {"canonical": "C0"}, ["C0"]))
        self.assertTrue(metric1.value_equal("TABLE_COUNT", {"row_count": 231}, ["231"]))
        self.assertTrue(metric1.value_equal("COMPOUND", {"norms": ["10.02", "55.7"]}, [["10.02", "55.70"]]))
        self.assertFalse(metric1.value_equal("NUMERIC_TABLE", {"norm": "1076.49"}, ["1061.49"]))

    def test_citation_and_ordinal_rules(self):
        self.assertTrue(metric1._NORM_NUM_RE.match("54.13330"))
        self.assertTrue(metric1._ORD_RE.match("2.10"))
        self.assertIsNone(metric1._ORD_RE.match("1076,49"))


class CorpusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CORPUS.exists() or not MANIFEST.exists():
            raise unittest.SkipTest("corpus not built/frozen")
        cls.rows = common.read_jsonl(CORPUS)
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_frozen_and_unmodified(self):
        self.assertEqual(common.sha256_file(CORPUS), self.manifest["corpus_sha256"])
        self.assertEqual(self.manifest["plan_uncovered"], [])

    def test_size_split_and_families(self):
        self.assertGreaterEqual(len(self.rows), 120)
        hold = [r for r in self.rows if r["split"] == "holdout"]
        self.assertGreaterEqual(len(hold) / len(self.rows), 0.60)
        self.assertEqual({r["family"] for r in self.rows}, set(common.FAMILIES))
        dev_objects = {r["obj"] for r in self.rows if r["split"] == "dev"}
        hold_objects = {r["obj"] for r in self.rows if r["split"] == "holdout"}
        self.assertFalse(dev_objects & hold_objects)                                       # split is by OBJECT

    def test_hidden_test_object_is_not_in_the_corpus(self):
        self.assertFalse(any("RECHNIKOV" in r["object_id"] for r in self.rows))

    def test_every_triple_is_pinned_to_a_page_and_marked_silver(self):
        for r in self.rows:
            self.assertTrue(r["file_id"] and r["page"] >= 1 and r["line_text"], r["triple_id"])
            self.assertEqual(r["annotation_tier"], "SILVER")
            self.assertFalse(r["training_eligible"])
            self.assertTrue(r["labelled_before_extractor_run"])
            self.assertIn(r["stage"], ("PD", "RD", "ID"))

    def test_provenance_is_computed_and_system_selection_is_flagged(self):
        sel = [r for r in self.rows if r["selected_by_system_output"]]
        self.assertTrue(all(r["provenance"] == "PASS1_MECHANISM_SELECTED" for r in sel))
        self.assertTrue(any(r["provenance"] == "PLAN" for r in self.rows))

    def test_probe_plan_is_deterministic(self):
        from evaluation.measurement_bench import make_probe_plan

        fresh = make_probe_plan.build()
        stored = json.loads((common.CORPUS_DIR / "probe_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(fresh["attempts"], stored["attempts"])


class Metric3RuleTests(unittest.TestCase):
    """Only an exact-page VIOLATION_PRESENT is a catch; a same-document positive is incidental; one wrong side makes a positive a VALUE_ERROR."""

    def _check(self, code, label, pages):
        return {"parameter_code": code, "location": "SITE", "violation_label": label, "protocol_status": "CRITICAL",
                "evidence": [{"file_id": f, "pdf_page_number": pg} for f, pg in pages]}

    def _ann(self, fid, obj_file, page, stage="RD", status="candidate"):
        return {"finding_id": fid, "status": status, "object": "x", "source_file_id": obj_file, "stage": stage, "page": page, "expected": "e", "actual": "a"}

    def test_same_document_positive_is_not_a_catch(self):
        ann = [self._ann("LOS3A-V01", "LOS3A-000069", 8), self._ann("LOS3A-V01", "LOS3A-000069", 15)]
        run = {"LOS3A": {"checks": [self._check("PZ-002", "VIOLATION_PRESENT", [("LOS3A-000069", 11)])], "groups": []}}
        res = metric3.evaluate(ann, run, {}, [])
        case = res["cases"][0]
        self.assertEqual(case["verdict"], "MISSED")
        self.assertEqual(case["violation_present_exact_page"], 0)
        self.assertEqual(case["violation_present_same_document_other_pages"], 1)
        self.assertEqual(res["summary"]["caught_exact_page"], 0)

    def test_exact_page_positive_is_a_catch(self):
        ann = [self._ann("LOS3A-V01", "LOS3A-000069", 8)]
        run = {"LOS3A": {"checks": [self._check("AR-041", "VIOLATION_PRESENT", [("LOS3A-000069", 8), ("LOS3A-000009", 41)])], "groups": []}}
        res = metric3.evaluate(ann, run, {}, [])
        self.assertEqual(res["cases"][0]["verdict"], "CAUGHT_EXACT_PAGE")

    def test_negative_pair_false_positive_needs_a_pair_page(self):
        ann = [self._ann("POL17-N01", "POL17-000096", 7, status="negative")]
        far = {"POL17": {"checks": [self._check("AR-041", "VIOLATION_PRESENT", [("POL17-000096", 13)])], "groups": []}}
        near = {"POL17": {"checks": [self._check("AR-041", "VIOLATION_PRESENT", [("POL17-000096", 7)])], "groups": []}}
        self.assertFalse(metric3.evaluate(ann, far, {}, [])["cases"][0]["false_positive_on_pair"])
        self.assertTrue(metric3.evaluate(ann, near, {}, [])["cases"][0]["false_positive_on_pair"])

    def test_missing_object_is_reported_not_skipped(self):
        ann = [self._ann("UNDMS-V01", "UNDMS-000252", 10)]
        res = metric3.evaluate(ann, {}, {}, [])
        self.assertEqual(res["cases"][0]["availability"], "not_available")
        self.assertEqual(res["summary"]["not_available"], 1)

    def test_adjudication_one_wrong_side_is_a_value_error_even_if_the_other_side_has_no_triple(self):
        m1 = [{"obj": "O", "param_code": "PZ-005", "stage": "RD", "status": "WRONG_ROW", "true_norm": "1"}]
        self.assertEqual(metric3._adjudicate_positive("O", "PZ-005", ["PD", "RD"], m1)["verdict"], "VALUE_ERROR")
        both = [{"obj": "O", "param_code": "PZ-005", "stage": "PD", "status": "CORRECT", "true_norm": "1"},
                {"obj": "O", "param_code": "PZ-005", "stage": "RD", "status": "CORRECT", "true_norm": "2"}]
        self.assertEqual(metric3._adjudicate_positive("O", "PZ-005", ["PD", "RD"], both)["verdict"], "GENUINE_CANDIDATE")
        self.assertEqual(metric3._adjudicate_positive("O", "PZ-009", ["PD", "RD"], both)["verdict"], "NOT_ADJUDICABLE")


if __name__ == "__main__":
    unittest.main()
