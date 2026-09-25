"""Optional LLM candidate verifier (`llm_candidate_verifier.py`): the pure,
model-free parts -- strict verdict parsing, prompt construction, per-candidate
context extraction, and the disabled-by-default / shadow-by-default gating in
`cross_stage_localization`. No model is loaded anywhere in this file; the
model-backed path is mocked at `llm_candidate_verifier.verify`. The measured
pilot (real model, real candidate pools) lives in
evaluation/LLM_CANDIDATE_VERIFIER_PILOT_REPORT.md, not in unit tests.
"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.domain import llm_candidate_verifier as verifier


def _word(text: str, x0: float, y0: float, width: float = 40.0, height: float = 10.0) -> dict:
    return {"text": text, "bbox": [x0, y0, x0 + width, y0 + height]}


class ParseVerdictTests(unittest.TestCase):
    def test_valid_choice_is_converted_to_zero_based(self):
        self.assertEqual(verifier.parse_verdict('{"choice": 2, "reason": "строка ТЭП"}', 3), (1, "строка ТЭП"))

    def test_null_choice_means_none_of_them(self):
        self.assertEqual(verifier.parse_verdict('{"choice": null, "reason": "нет"}', 3), (None, "нет"))

    def test_json_embedded_in_surrounding_text_is_found(self):
        self.assertEqual(verifier.parse_verdict('Ответ:\n```json\n{"choice": 1, "reason": "ok"}\n```', 2), (0, "ok"))

    def test_numeric_string_choice_is_accepted(self):
        self.assertEqual(verifier.parse_verdict('{"choice": "3", "reason": ""}', 3), (2, ""))

    def test_out_of_range_choice_is_unusable_not_clamped(self):
        self.assertIsNone(verifier.parse_verdict('{"choice": 4, "reason": "x"}', 3))
        self.assertIsNone(verifier.parse_verdict('{"choice": 0, "reason": "x"}', 3))

    def test_invented_value_instead_of_index_is_unusable(self):
        self.assertIsNone(verifier.parse_verdict('{"choice": 25036.27, "reason": "x"}', 3))
        self.assertIsNone(verifier.parse_verdict('{"choice": true, "reason": "x"}', 3))

    def test_free_text_is_unusable(self):
        self.assertIsNone(verifier.parse_verdict("Кандидат 2 подходит лучше всего.", 3))
        self.assertIsNone(verifier.parse_verdict("", 3))
        self.assertIsNone(verifier.parse_verdict('{"answer": 2}', 3))


class BuildMessagesTests(unittest.TestCase):
    def _param(self):
        return SimpleNamespace(
            code="PZ-002", parameter_name="Общая площадь здания", unit="м²", trigger_logic="Расхождение > 0",
            sp_reference=None, gost_reference=None, fz_reference=None,
            other_normative=json.dumps({"source_pd": "Раздел ПЗ: Таблица ТЭП", "source_rd": "Раздел АР: Сводная экспликация"}, ensure_ascii=False),
        )

    def test_definition_uses_the_stage_specific_catalog_hint(self):
        definition = verifier.parameter_definition(self._param(), "RD")
        self.assertEqual(definition["source_hint"], "Раздел АР: Сводная экспликация")
        self.assertEqual(definition["parameter_name"], "Общая площадь здания")
        self.assertEqual(definition["normative"], "")

    def test_prompt_numbers_candidates_in_given_order_and_asks_for_json(self):
        definition = verifier.parameter_definition(self._param(), "PD")
        messages = verifier.build_messages(definition, [
            {"document": "a.pdf", "page": 10, "value": "16867.90", "row_text": "Площадь помещений здания 16867,90", "context": None},
            {"document": "b.pdf", "page": 14, "value": "25036.27", "row_text": "Площадь здания 25036,27", "context": "ТЭП\nПлощадь здания 25036,27"},
        ])
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        user = messages[1]["content"]
        self.assertLess(user.index("[1] Документ: a.pdf, стр. 10"), user.index("[2] Документ: b.pdf, стр. 14"))
        self.assertIn("Раздел ПЗ: Таблица ТЭП", user)
        self.assertIn("JSON", messages[0]["content"])

    def test_prompt_never_carries_anything_but_the_shown_candidates(self):
        # The resolved PD value must never leak into an RD/ID verification
        # prompt (it would bias the model toward agreement) -- build_messages
        # has no parameter through which it could, so the only numbers in the
        # prompt are the candidates' own.
        definition = verifier.parameter_definition(self._param(), "RD")
        user = verifier.build_messages(definition, [{"document": "r.pdf", "page": 1, "value": "1.0", "row_text": None, "context": None}])[1]["content"]
        self.assertNotIn("25036", user)


class CandidateContextTests(unittest.TestCase):
    def test_value_row_plus_rows_above_and_below_in_visual_order(self):
        words = [
            _word("Таблица", 10, 10), _word("ТЭП", 60, 10),
            _word("1.", 10, 30), _word("Площадь", 40, 30), _word("участка", 90, 30), _word("5,00", 200, 30),
            _word("2.", 10, 50), _word("Площадь", 40, 50), _word("застройки", 90, 50), _word("1076,49", 200, 50),
            _word("3.", 10, 70), _word("Этажность", 40, 70), _word("9", 200, 70),
            _word("4.", 10, 90), _word("Прочее", 40, 90),
        ]
        # Content-stream order puts the value before its label (real
        # multi-column reflow pattern) -- visual row clustering is immune.
        words = [words[9]] + words[:9] + words[10:]
        row, context = verifier.build_candidate_context(words, [200, 50, 240, 60])
        self.assertEqual(row, "2. Площадь застройки 1076,49")
        self.assertEqual(context.split("\n"), [
            "Таблица ТЭП", "1. Площадь участка 5,00", "2. Площадь застройки 1076,49", "3. Этажность 9",
        ])

    def test_missing_inputs_yield_no_context(self):
        self.assertEqual(verifier.build_candidate_context([], [0, 0, 1, 1]), (None, None))
        self.assertEqual(verifier.build_candidate_context([_word("x", 0, 0)], None), (None, None))

    def test_context_is_length_capped(self):
        words = [_word("слово" * 40, 10, 10 + i * 20, width=500) for i in range(5)]
        _row, context = verifier.build_candidate_context(words, [10, 50, 510, 60], max_chars=100)
        self.assertLessEqual(len(context), 102)


class GatingTests(unittest.TestCase):
    def test_disabled_and_shadow_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CASE10_LLM_VERIFIER_ENABLED", None)
            os.environ.pop("CASE10_LLM_VERIFIER_MODE", None)
            self.assertFalse(verifier.is_enabled())
            self.assertEqual(verifier.mode(), "shadow")

    def test_unknown_mode_falls_back_to_shadow(self):
        with patch.dict(os.environ, {"CASE10_LLM_VERIFIER_MODE": "autopilot"}):
            self.assertEqual(verifier.mode(), "shadow")

    def test_verify_without_candidates_never_loads_a_model(self):
        with patch.object(verifier, "_load", side_effect=AssertionError("must not load")):
            self.assertIsNone(verifier.verify({"code": "X"}, []))

    def test_unusable_reply_is_no_signal(self):
        with patch.object(verifier, "_load", return_value=True), patch.object(verifier, "generate", return_value="не знаю"):
            self.assertIsNone(verifier.verify({"code": "X"}, [{"document": "a", "page": 1, "value": "1"}]))

    def test_usable_reply_becomes_verdict(self):
        with patch.object(verifier, "_load", return_value=True), patch.object(verifier, "generate", return_value='{"choice": 1, "reason": "ok"}'):
            verdict = verifier.verify({"code": "X"}, [{"document": "a", "page": 1, "value": "1"}])
        self.assertEqual((verdict.choice, verdict.reason), (0, "ok"))


def _param():
    return SimpleNamespace(
        code="PZ-002", parameter_name="Общая площадь здания", unit="м²", trigger_logic=None,
        sp_reference=None, gost_reference=None, fz_reference=None, other_normative=None,
    )


def _candidate(value: str, page: int):
    from app.domain.cross_stage_localization import StageCandidate
    from app.domain.generic_matrix_extraction import GenericObservation
    from decimal import Decimal

    doc = SimpleNamespace(id=page, filename=f"doc{page}.pdf", document_code=None, dataset_stage="PD", discipline=None)
    payload = GenericObservation(
        value=value, normalized_value=value, decimal_value=Decimal(value), confidence=0.5, document=doc, page=page,
        bbox_normalized=[0, 0, 1, 1], bbox_pdf=[0, 0, 1, 1], page_width=1.0, page_height=1.0,
        extractor="x", context="", source_fragment=None,
    )
    return StageCandidate(fragment=SimpleNamespace(), document=doc, page=page, payload=payload, fingerprint=None)


class PrecisionChoiceTests(unittest.TestCase):
    def test_sizes_against_the_allowed_share_not_the_whole_card(self):
        self.assertEqual(verifier.choose_precision(80.0, 0.2), "bf16")   # grading H100, default share = 16 GB
        self.assertIsNone(verifier.choose_precision(6.0, 0.2))          # 6 GB laptop, default share = 1.2 GB
        self.assertEqual(verifier.choose_precision(6.0, 0.95), "4bit")  # same laptop, share raised explicitly
        self.assertEqual(verifier.choose_precision(24.0, 0.3), "4bit")

    def test_explicit_request_wins(self):
        self.assertEqual(verifier.choose_precision(6.0, 0.1, "4bit"), "4bit")
        self.assertEqual(verifier.choose_precision(6.0, 0.1, "bf16"), "bf16")


class ReviewPickPolicyTests(unittest.TestCase):
    def _verdict(self, choice):
        return verifier.Verdict(choice=choice, reason="причина", raw="", latency_seconds=1.0, model="m")

    def test_never_consulted_when_deterministic_ranking_abstained(self):
        with patch.object(verifier, "verify", side_effect=AssertionError("must not be called")):
            self.assertEqual(verifier.review_pick([_candidate("1", 1)], None, _param()), (None, False))

    def test_never_consulted_without_a_param(self):
        cands = [_candidate("1", 1)]
        with patch.object(verifier, "verify", side_effect=AssertionError("must not be called")):
            self.assertEqual(verifier.review_pick(cands, cands[0], ("titles",)), (cands[0], False))

    def test_unusable_verdict_keeps_deterministic_pick_and_records_nothing(self):
        cands = [_candidate("1", 1), _candidate("2", 2)]
        with patch.object(verifier, "verify", return_value=None):
            self.assertEqual(verifier.review_pick(cands, cands[0], _param()), (cands[0], False))
        self.assertIsNone(cands[0].payload.llm_verification)

    def test_shadow_mode_keeps_pick_and_attaches_disagreeing_verdict(self):
        cands = [_candidate("16867.90", 10), _candidate("25036.27", 14)]
        with patch.dict(os.environ, {"CASE10_LLM_VERIFIER_MODE": "shadow"}), patch.object(verifier, "verify", return_value=self._verdict(1)):
            pick, verified = verifier.review_pick(cands, cands[0], _param())
        self.assertIs(pick, cands[0])
        self.assertTrue(verified)
        record = cands[0].payload.llm_verification
        self.assertFalse(record["agrees_with_deterministic_pick"])
        self.assertEqual(record["choice"], {"document": "doc14.pdf", "page": 14, "value": "25036.27"})
        self.assertEqual(record["reason"], "причина")

    def test_rerank_mode_replaces_pick(self):
        cands = [_candidate("16867.90", 10), _candidate("25036.27", 14)]
        with patch.dict(os.environ, {"CASE10_LLM_VERIFIER_MODE": "rerank"}), patch.object(verifier, "verify", return_value=self._verdict(1)):
            pick, _ = verifier.review_pick(cands, cands[0], _param())
        self.assertIs(pick, cands[1])
        self.assertEqual(cands[1].payload.llm_verification["mode"], "rerank")

    def test_rerank_mode_none_of_them_abstains(self):
        cands = [_candidate("1", 1)]
        with patch.dict(os.environ, {"CASE10_LLM_VERIFIER_MODE": "rerank"}), patch.object(verifier, "verify", return_value=self._verdict(None)):
            self.assertEqual(verifier.review_pick(cands, cands[0], _param()), (None, True))

    def test_shadow_mode_none_of_them_is_recorded_not_applied(self):
        cands = [_candidate("1", 1)]
        with patch.dict(os.environ, {"CASE10_LLM_VERIFIER_MODE": "shadow"}), patch.object(verifier, "verify", return_value=self._verdict(None)):
            pick, _ = verifier.review_pick(cands, cands[0], _param())
        self.assertIs(pick, cands[0])
        self.assertIsNone(cands[0].payload.llm_verification["choice"])

    def test_param_is_recovered_from_every_mechanism_extra_shape(self):
        param = _param()
        self.assertIs(verifier._param_from_extra(param), param)
        self.assertIs(verifier._param_from_extra((param, ["family"])), param)
        self.assertIsNone(verifier._param_from_extra(["title a", "title b"]))


class ResolveStageRoundIntegrationTests(unittest.TestCase):
    def _run(self, env):
        from app.domain import cross_stage_localization as csl

        docs = {1: SimpleNamespace(id=1, discipline=None, filename="a.pdf", document_code=None, dataset_stage="PD")}
        fragments = [SimpleNamespace(document_version_id=1, page=p, confidence=0.5) for p in (1, 2)]
        snapshot = {"words": [{"text": "Площадь", "bbox": [0, 0, 10, 10]}, {"text": "5,0", "bbox": [20, 0, 30, 10]}]}

        def match_fn(snap, fragment, doc, page, extra):
            return _candidate(str(page), page).payload, None, None

        entries = [("k", fragments, None, None, _param(), None)]
        with patch.dict(os.environ, env), \
                patch.object(csl, "extract_original_pages", side_effect=lambda d, pages: {p: snapshot for p in pages}), \
                patch.object(csl, "semantic_text_similarity", return_value=None), \
                patch.object(verifier, "verify", return_value=verifier.Verdict(1, "r", "", 0.1, "m")) as verify:
            result = csl.resolve_stage_round(entries=entries, by_id=docs, pages_per_stage=5, budget={"pages": 10}, match_fn=match_fn)
        return result["k"], verify

    def test_disabled_flag_is_a_strict_no_op(self):
        best, verify = self._run({"CASE10_LLM_VERIFIER_ENABLED": "0"})
        verify.assert_not_called()
        self.assertEqual(best.page, 1)
        self.assertIsNone(best.payload.llm_verification)
        self.assertIsNone(best.verifier_context)

    def test_enabled_shadow_attaches_verdict_without_changing_pick(self):
        best, verify = self._run({"CASE10_LLM_VERIFIER_ENABLED": "1", "CASE10_LLM_VERIFIER_MODE": "shadow"})
        verify.assert_called_once()
        shown = verify.call_args[0][1]
        self.assertEqual([c["page"] for c in shown], [1, 2])
        self.assertEqual(shown[0]["row_text"], "Площадь 5,0")
        self.assertEqual(best.page, 1)
        self.assertEqual(best.payload.llm_verification["choice"]["page"], 2)

    def test_enabled_rerank_applies_verdict(self):
        best, _ = self._run({"CASE10_LLM_VERIFIER_ENABLED": "1", "CASE10_LLM_VERIFIER_MODE": "rerank"})
        self.assertEqual(best.page, 2)


class EvidenceDeltaTests(unittest.TestCase):
    def test_delta_unchanged_without_any_verdict(self):
        from app.domain.official_evidence import _with_llm_verification

        delta = {"source": "x"}
        self.assertEqual(_with_llm_verification(dict(delta), {"PD": SimpleNamespace(llm_verification=None)}), delta)

    def test_delta_carries_per_stage_verdicts(self):
        from app.domain.official_evidence import _with_llm_verification

        out = _with_llm_verification({}, {"PD": SimpleNamespace(llm_verification={"reason": "r"}), "RD": SimpleNamespace(llm_verification=None)})
        self.assertEqual(out, {"llm_verification": {"PD": {"reason": "r"}}})


if __name__ == "__main__":
    unittest.main()
