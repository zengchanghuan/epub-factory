"""Offline AI translation price regressions; no payment or model calls."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import app.main as main
from test_epub_fixture import minimal_epub_bytes


class TranslationPricingTests(unittest.TestCase):
    def test_standard_flash_tier_boundaries(self):
        cases = {
            0: "3.99",
            10: "3.99",
            79_800: "3.99",
            300_000: "15.00",
            727_100: "29.95",
            1_000_000: "39.50",
            2_000_000: "64.50",
        }
        for chars, expected in cases.items():
            with self.subTest(chars=chars):
                self.assertEqual(main._calc_translation_price(chars), expected)

    def test_quality_modes_have_explicit_cost_multipliers(self):
        self.assertEqual(main._calc_translation_price(300_000, "standard", "deepseek-flash"), "15.00")
        self.assertEqual(main._calc_translation_price(300_000, "high", "deepseek-flash"), "22.50")
        self.assertEqual(main._calc_translation_price(300_000, "literary", "deepseek-flash"), "30.00")

    def test_pro_multiplier_combines_with_quality_multiplier(self):
        self.assertEqual(main._calc_translation_price(727_100, "standard", "deepseek-v4-pro"), "101.82")
        self.assertEqual(main._calc_translation_price(727_100, "high", "deepseek-v4-pro"), "152.74")

    def test_quality_floor_is_also_multiplied(self):
        self.assertEqual(main._calc_translation_price(1, "high", "deepseek-flash"), "5.99")
        self.assertEqual(main._calc_translation_price(1, "literary", "deepseek-flash"), "7.98")
        self.assertEqual(main._calc_translation_price(1, "standard", "deepseek-v4-pro"), "13.57")

    def test_fixed_operator_price_remains_an_exact_override(self):
        with patch.object(main, "_TRANSLATION_FIXED_PRICE", "12.34"):
            self.assertEqual(main._calc_translation_price(2_000_000, "literary", "deepseek-v4-pro"), "12.34")

    def test_epub_estimate_applies_selected_quality_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixture.epub"
            source.write_bytes(minimal_epub_bytes())
            high = main._estimate_translation_pricing(
                str(source), "zh-CN", translation_quality="high", translation_model="deepseek-flash"
            )
            pro = main._estimate_translation_pricing(
                str(source), "zh-CN", translation_quality="standard", translation_model="deepseek-v4-pro"
            )
        self.assertEqual(high["price_cny"], "5.99")
        self.assertEqual(pro["price_cny"], "13.57")

    def test_price_cap_is_applied_after_tiers_and_multipliers(self):
        with patch.object(main, "TRANSLATION_MAX_PRICE", main.Decimal("50")):
            self.assertEqual(main._calc_translation_price(2_000_000, "literary", "deepseek-v4-pro"), "50.00")


if __name__ == "__main__":
    unittest.main()
