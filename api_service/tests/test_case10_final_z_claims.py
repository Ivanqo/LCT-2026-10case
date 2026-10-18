"""Final / step Z: `evaluation/check_claims.py` -- unsupported absolutes and metric numbers without a source in the delivery documents."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from evaluation import check_claims as cc

ROOT = Path(__file__).resolve().parents[2]


class ClaimsRuleTests(unittest.TestCase):
    def rules(self, text):
        return [(item["rule"], item["text"].lower()) for item in cc.check_text(text)]

    def test_absolute_words_need_a_reference_or_a_negation(self):
        self.assertEqual(self.rules("Сервис полностью автоматический."), [("ABSOLUTE", "полностью")])
        self.assertEqual(self.rules("Всё работает на 100 %."), [("ABSOLUTE", "100 %")])
        self.assertEqual(self.rules("Работа готова."), [("ABSOLUTE", "готова")])
        self.assertEqual(self.rules("Мы гарантируем результат."), [("ABSOLUTE", "гарантируем")])
        self.assertEqual(self.rules("Покрытие не полностью проверено."), [])
        self.assertEqual(self.rules("Мы не гарантируем скорость."), [])
        self.assertEqual(self.rules("Работает полностью (см. evaluation/phase12/S6_REPORT.md)."), [])
        self.assertEqual(self.rules("Работает полностью. <!-- claims-ok: описание режима, не качество -->"), [])

    def test_metric_numbers_need_a_source(self):
        self.assertEqual([r for r, _ in self.rules("Recall 0,85 на тесте.")], ["METRIC"])
        self.assertEqual([r for r, _ in self.rules("Точность распознавания 96 %.")], ["METRIC"])
        self.assertEqual([r for r, _ in self.rules("Найдено 12 из 19 по полноте.")], ["METRIC"])
        self.assertEqual(self.rules("Recall 0,85 (evaluation/phase12/S1_REPORT.md)."), [])
        self.assertEqual(self.rules("Recall 0,85, не измерено на финальном коде."), [])
        self.assertEqual(self.rules("В образе 3 слоя, 12 потоков, 85 % диска."), [])      # numbers without a metric word

    def test_reference_in_the_same_table_row_or_list_item_only(self):
        text = "| Recall | 0,85 | нет |\n| Precision | 0,97 | S1_REPORT.md |\n"
        self.assertEqual([item["line"] for item in cc.check_text(text)], [1])
        text = "- Recall 0,85 на тесте\n- Precision 0,97 (FINAL_B_TIERS_REPORT.md)\n"
        self.assertEqual([item["line"] for item in cc.check_text(text)], [1])
        paragraph = "Recall 0,85 на тесте,\nподробности: evaluation/phase12/S1_REPORT.md.\n"
        self.assertEqual(cc.check_text(paragraph), [])

    def test_code_blocks_are_skipped(self):
        self.assertEqual(cc.check_text("```bash\necho полностью 100 %\n```\n"), [])

    def test_flag_default_must_match_the_compose_file(self):
        compose = "X:\n  CASE10_EXPLICATION_COMPARE: ${CASE10_EXPLICATION_COMPARE:-1}\n  CASE10_A: ${CASE10_B:-0}\n"
        self.assertEqual(cc.check_flag_defaults("| `CASE10_EXPLICATION_COMPARE` | `1` | on |", compose), [])
        wrong = cc.check_flag_defaults("| `CASE10_EXPLICATION_COMPARE` | `0` | off |", compose)
        self.assertEqual([(item["rule"], item["text"]) for item in wrong], [("FLAG", "CASE10_EXPLICATION_COMPARE")])
        self.assertEqual(cc.check_flag_defaults("| `CASE10_UNKNOWN` | `0` | - |", compose), [])


class ClaimsCliTests(unittest.TestCase):
    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp, "good.md"), Path(tmp, "bad.md")
            good.write_text("Всё описано в evaluation/phase12/S1_REPORT.md.\n", encoding="utf-8")
            bad.write_text("Система полностью готова.\n", encoding="utf-8")
            self.assertEqual(cc.main([str(good)]), 0)
            self.assertEqual(cc.main([str(bad)]), 1)
            self.assertEqual(cc.main([str(good), str(Path(tmp, "missing.md"))]), 2)

    def test_delivery_documents_pass(self):
        """The documents that go into the delivery contain no unsupported claim (rule fixed in step Z)."""
        self.assertEqual(cc.main([str(ROOT / name) for name in cc.DEFAULT_FILES]), 0)


if __name__ == "__main__":
    unittest.main()
