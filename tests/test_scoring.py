import unittest

from scoring import score_token


class ScoreTokenTests(unittest.TestCase):
    def test_honeypot_caps_score_at_five_and_is_red(self):
        result = score_token({"is_honeypot": True}, min_liquidity_usd=50000)
        self.assertLessEqual(result.score, 5)
        self.assertEqual(result.verdict, "red")
        self.assertTrue(any("Honeypot" in r for r in result.reasons))

    def test_all_clean_signals_score_high_and_green(self):
        signals = {
            "is_honeypot": False,
            "contract_verified": True,
            "is_mintable": False,
            "can_take_back_ownership": False,
            "is_proxy": False,
            "selfdestruct": False,
            "lp_locked_or_burned": True,
            "top10_holder_pct": 15,
            "buy_tax": 1,
            "sell_tax": 1,
            "liquidity_usd": 200000,
        }
        result = score_token(signals, min_liquidity_usd=50000)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.verdict, "green")
        self.assertFalse(result.limited)

    def test_no_signals_available_is_limited_and_zero(self):
        result = score_token({}, min_liquidity_usd=50000)
        self.assertEqual(result.score, 0)
        self.assertTrue(result.limited)

    def test_unlocked_lp_is_flagged_but_not_a_hard_fail(self):
        result = score_token({"lp_locked_or_burned": False}, min_liquidity_usd=50000)
        self.assertTrue(any("LP не заблокирован" in r for r in result.reasons))

    def test_dangerous_ownership_flags_are_flagged(self):
        result = score_token(
            {"is_mintable": True, "can_take_back_ownership": False}, min_liquidity_usd=50000
        )
        self.assertTrue(any("Опасные функции контракта" in r for r in result.reasons))
        self.assertTrue(any("is_mintable" in r for r in result.reasons))

    def test_holder_concentration_scales_between_zero_and_full_credit(self):
        concentrated = score_token({"top10_holder_pct": 70}, min_liquidity_usd=50000)
        spread_out = score_token({"top10_holder_pct": 20}, min_liquidity_usd=50000)
        self.assertLess(concentrated.score, spread_out.score)

    def test_high_total_tax_is_flagged(self):
        result = score_token({"buy_tax": 8, "sell_tax": 8}, min_liquidity_usd=50000)
        self.assertTrue(any("Высокие налоги" in r for r in result.reasons))

    def test_low_total_tax_is_not_flagged_as_high(self):
        result = score_token({"buy_tax": 2, "sell_tax": 2}, min_liquidity_usd=50000)
        self.assertFalse(any("Высокие налоги" in r for r in result.reasons))

    def test_partial_coverage_below_fifty_points_possible_is_limited(self):
        result = score_token({"buy_tax": 1, "sell_tax": 1}, min_liquidity_usd=50000)
        self.assertTrue(result.limited)

    def test_full_coverage_is_not_limited(self):
        signals = {
            "is_honeypot": False,
            "contract_verified": True,
            "is_mintable": False,
            "lp_locked_or_burned": True,
            "top10_holder_pct": 15,
            "buy_tax": 1,
            "sell_tax": 1,
            "liquidity_usd": 200000,
        }
        result = score_token(signals, min_liquidity_usd=50000)
        self.assertFalse(result.limited)


if __name__ == "__main__":
    unittest.main()
