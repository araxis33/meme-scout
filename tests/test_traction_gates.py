"""Ворота испытательного срока после правок 05.09.2026.

Порог находки опущен с $50 000 до $1 500, поэтому вся защита от мусора
переехала сюда, в traction. Эти тесты фиксируют ровно то, ради чего правка
делалась, и ровно то, чем за неё платим.

Без сети и без базы: traction.evaluate - чистая функция от словаря пула.
"""
import os
import tempfile
import unittest

_TMP_DB = os.path.join(tempfile.gettempdir(), "meme_scout_test_traction.sqlite3")
os.environ["DB_PATH"] = os.path.relpath(_TMP_DB, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import config  # noqa: E402
import traction  # noqa: E402


def pool(liq, vol_h1, buyers, sellers=5, buys=None, sells=None, price=1.0):
    return {
        "liquidity_usd": liq,
        "volume_h1": vol_h1,
        "buyers_h1": buyers,
        "sellers_h1": sellers,
        "buys_h1": buys if buys is not None else buyers,
        "sells_h1": sells if sells is not None else sellers,
        "price_usd": price,
    }


class TractionGates(unittest.TestCase):
    def test_small_pool_with_real_demand_still_waits_without_growth(self):
        """Спрос есть, а пул как был мелкий, так и остался - показывать рано."""
        result = traction.evaluate(pool(8000, 6000, 40), liq_0=7000, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertIn("рано показывать", result.fail_reason)
        self.assertFalse(result.fatal)

    def test_small_pool_that_multiplied_gets_through(self):
        """Тот же спрос, но пул вырос втрое - в него зашли чужие деньги."""
        result = traction.evaluate(pool(21000, 6000, 40), liq_0=7000, price_0=1.0)
        self.assertTrue(result.passed, result.fail_reason)

    def test_big_pool_passes_without_any_growth(self):
        """Пул уже торгуемого размера - рост не обязателен."""
        result = traction.evaluate(pool(120000, 40000, 90), liq_0=110000, price_0=1.0)
        self.assertTrue(result.passed, result.fail_reason)

    def test_tiny_pool_swing_is_not_a_rug(self):
        """У пула на $2K минус 40% - это качели, а не слив: не отчислять.

        Старое правило (fatal при падении ниже 70% от начальной) хоронило
        именно тех новичков, ради которых порог находки и опущен.
        """
        result = traction.evaluate(pool(1800, 200, 3), liq_0=3000, price_0=1.0)
        self.assertFalse(result.fatal, result.fail_reason)

    def test_real_pool_being_drained_is_still_fatal(self):
        """А вот у пула на $40K падение до половины - настоящий слив."""
        result = traction.evaluate(pool(20000, 9000, 40), liq_0=40000, price_0=1.0)
        self.assertTrue(result.fatal)
        self.assertIn("слита", result.fail_reason)

    def test_pool_below_the_discovery_floor_is_dead(self):
        result = traction.evaluate(pool(900, 5000, 40), liq_0=3000, price_0=1.0)
        self.assertTrue(result.fatal)

    def test_wash_trading_still_rejected(self):
        result = traction.evaluate(pool(30000, 900000, 40), liq_0=12000, price_0=1.0)
        self.assertTrue(result.fatal)
        self.assertIn("накрутку", result.fail_reason)

    def test_no_buyers_no_push_however_big_the_pool(self):
        result = traction.evaluate(pool(300000, 50000, 3), liq_0=300000, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertIn("покупателей", result.fail_reason)

    def test_thresholds_are_the_ones_the_change_intended(self):
        self.assertEqual(config.MIN_LIQUIDITY_USD, 1500)
        self.assertEqual(config.PUSH_MIN_LIQUIDITY_USD, 25000)
        self.assertEqual(config.PROBATION_PUSH_LIQ_GROWTH, 2.5)
        self.assertEqual(config.PROBATION_MAX_HOURS, 24)
        self.assertFalse(config.SCAN_ROBINHOOD)


if __name__ == "__main__":
    unittest.main()
