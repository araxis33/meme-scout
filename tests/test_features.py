"""Тесты фич из обновления 2026-08-19.

Без сети и без живой базы: DB_PATH подменяется на временный файл ДО импорта
config, а единственный сетевой клиент (GeckoTerminal) проверяется только на
уровне circuit breaker, который до сети не доходит.
"""
import asyncio
import logging
import os
import tempfile
import unittest

_TMP_DB = os.path.join(tempfile.gettempdir(), "meme_scout_test_features.sqlite3")
os.environ["DB_PATH"] = os.path.relpath(_TMP_DB, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import config  # noqa: E402
import db  # noqa: E402
from sources import geckoterminal  # noqa: E402
from watchers import digest, discovery, outcomes, pump_dump  # noqa: E402


def setUpModule():
    # Circuit breaker намеренно шумит в лог - в выводе тестов это мусор.
    logging.disable(logging.WARNING)
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    db.init_db()


def tearDownModule():
    logging.disable(logging.NOTSET)
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)


class _Verdict:
    """Заглушка ScoreResult - _priority смотрит только на verdict."""

    def __init__(self, verdict):
        self.verdict = verdict


class MigrationTests(unittest.TestCase):
    def test_init_db_is_idempotent(self):
        db.init_db()
        db.init_db()  # второй прогон не должен падать на существующих таблицах

    def test_new_tables_exist(self):
        import sqlite3

        conn = sqlite3.connect(config.DB_PATH)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)")}
        conn.close()
        self.assertLessEqual({"ignored", "alert_events", "digest_queue", "outcomes"}, names)
        self.assertLessEqual({"manual", "last_checked", "dead_checks"}, columns)


class IgnoreListTests(unittest.TestCase):
    ADDR = "0x" + "a1" * 20

    def tearDown(self):
        db.unignore_token("base", self.ADDR)

    def test_ignore_then_unignore(self):
        self.assertFalse(db.is_ignored("base", self.ADDR))
        db.ignore_token("base", self.ADDR)
        self.assertTrue(db.is_ignored("base", self.ADDR))
        db.unignore_token("base", self.ADDR)
        self.assertFalse(db.is_ignored("base", self.ADDR))

    def test_ignoring_deactivates_watchlist_entry(self):
        db.add_to_watchlist("base", self.ADDR, baseline_price=1.0)
        db.ignore_token("base", self.ADDR)
        active = {(w["chain"], w["address"]) for w in db.get_watchlist()}
        self.assertNotIn(("base", self.ADDR), active)


class TrackedTests(unittest.TestCase):
    GREEN = "0x" + "b1" * 20
    RED = "0x" + "b2" * 20
    MANUAL = "0x" + "b3" * 20
    UNKNOWN = "0x" + "b4" * 20

    @classmethod
    def setUpClass(cls):
        db.record_outcome_start("base", cls.GREEN, "GREEN", 80, "green", 0.001, 100000)
        db.record_outcome_start("base", cls.RED, "RED", 10, "red", 0.001, 100000)
        db.add_to_watchlist_manual("base", cls.MANUAL)

    def test_green_token_is_tracked(self):
        self.assertTrue(db.is_tracked("base", self.GREEN))

    def test_red_token_is_not_tracked(self):
        self.assertFalse(db.is_tracked("base", self.RED))

    def test_manually_watched_token_is_tracked(self):
        self.assertTrue(db.is_tracked("base", self.MANUAL))

    def test_unknown_token_is_not_tracked(self):
        self.assertFalse(db.is_tracked("base", self.UNKNOWN))


class AlertPriorityTests(unittest.TestCase):
    """Тихий режим: что пушится сразу, а что копится до дайджеста."""

    def test_healthy_token_with_deep_liquidity_is_pushed(self):
        priority = discovery._priority(_Verdict("green"), config.PUSH_MIN_LIQUIDITY_USD + 1)
        self.assertEqual(priority, "high")

    def test_healthy_token_with_thin_liquidity_is_queued(self):
        priority = discovery._priority(_Verdict("green"), config.PUSH_MIN_LIQUIDITY_USD - 1)
        self.assertEqual(priority, "low")

    def test_red_token_is_queued_even_with_deep_liquidity(self):
        priority = discovery._priority(_Verdict("red"), config.PUSH_MIN_LIQUIDITY_USD * 10)
        self.assertEqual(priority, "low")

    def test_missing_liquidity_is_queued(self):
        self.assertEqual(discovery._priority(_Verdict("yellow"), None), "low")

    def test_move_on_tracked_token_is_pushed(self):
        self.assertEqual(pump_dump._priority(True), "high")

    def test_move_on_untracked_token_is_queued(self):
        self.assertEqual(pump_dump._priority(False), "low")


class SurvivorTests(unittest.TestCase):
    """Токен считается выжившим, только если жив, не сдулся и торгуется."""

    OUTCOME = {"liq_0": 100000, "price_0": 0.001, "alerted_at": 0, "score": 70}

    def test_growing_liquidity_with_volume_survives(self):
        data = {"liquidity_usd": 130000, "volume_24h": config.SURVIVOR_MIN_VOLUME_USD + 1}
        self.assertTrue(outcomes._is_survivor(self.OUTCOME, data))

    def test_shrinking_liquidity_does_not_survive(self):
        data = {"liquidity_usd": 80000, "volume_24h": 999999}
        self.assertFalse(outcomes._is_survivor(self.OUTCOME, data))

    def test_no_volume_does_not_survive(self):
        data = {"liquidity_usd": 130000, "volume_24h": 0}
        self.assertFalse(outcomes._is_survivor(self.OUTCOME, data))

    def test_delisted_token_does_not_survive(self):
        self.assertFalse(outcomes._is_survivor(self.OUTCOME, None))

    def test_unknown_starting_liquidity_does_not_survive(self):
        outcome = dict(self.OUTCOME, liq_0=0)
        data = {"liquidity_usd": 130000, "volume_24h": 999999}
        self.assertFalse(outcomes._is_survivor(outcome, data))


class RateLimitBackoffTests(unittest.TestCase):
    """Circuit breaker: 429 должен глушить запросы, а не ускорять их."""

    def setUp(self):
        # Свежий лок на каждый тест: asyncio.run() каждый раз создаёт новый loop.
        geckoterminal._rate_lock = asyncio.Lock()
        geckoterminal._note_ok()
        geckoterminal._cooldown_until = 0.0

    tearDown = setUp

    def test_starts_open(self):
        self.assertEqual(geckoterminal.cooldown_remaining(), 0)

    def test_rate_limit_arms_cooldown(self):
        geckoterminal._note_rate_limited("test")
        self.assertGreater(geckoterminal.cooldown_remaining(), 0)

    def test_burst_of_in_flight_failures_does_not_escalate(self):
        geckoterminal._note_rate_limited("test")
        armed = geckoterminal.cooldown_remaining()
        for _ in range(5):
            geckoterminal._note_rate_limited("test")
        self.assertLessEqual(geckoterminal.cooldown_remaining(), armed)

    def test_repeated_rate_limits_double_the_pause(self):
        geckoterminal._note_rate_limited("test")
        first = geckoterminal.cooldown_remaining()
        geckoterminal._cooldown_until = 0.0  # пауза истекла, снова получили 429
        geckoterminal._note_rate_limited("test")
        self.assertGreater(geckoterminal.cooldown_remaining(), first)

    def test_pause_is_capped(self):
        for _ in range(30):
            geckoterminal._cooldown_until = 0.0
            geckoterminal._note_rate_limited("test")
        self.assertLessEqual(
            geckoterminal.cooldown_remaining(), config.GECKOTERMINAL_COOLDOWN_MAX + 1
        )

    def test_no_requests_leave_while_cooling_down(self):
        geckoterminal._note_rate_limited("test")

        async def scenario():
            allowed = await geckoterminal._acquire()
            # Обе функции обязаны вернуть пустой результат, не ходя в сеть.
            pool = await geckoterminal.get_pool_by_token("base", "0x" + "11" * 20)
            pools = await geckoterminal.get_new_pools("base")
            return allowed, pool, pools

        allowed, pool, pools = asyncio.run(scenario())
        self.assertFalse(allowed)
        self.assertIsNone(pool)
        self.assertEqual(pools, [])

    def test_success_reopens_the_gate(self):
        geckoterminal._note_rate_limited("test")
        geckoterminal._note_ok()
        geckoterminal._cooldown_until = 0.0
        self.assertTrue(asyncio.run(geckoterminal._acquire()))


class ScoreboardTests(unittest.TestCase):
    """Табло точности: выживаемость и медиана по группе токенов."""

    def test_counts_survivors_by_liquidity_ratio(self):
        rows = [
            {"liq_0": 100, "liq_24h": 100, "price_0": 1, "price_24h": 2},   # выжил
            {"liq_0": 100, "liq_24h": 60, "price_0": 1, "price_24h": 1},    # выжил
            {"liq_0": 100, "liq_24h": 10, "price_0": 1, "price_24h": 0.5},  # не выжил
        ]
        stats = digest._cohort_stats(rows, "price_24h", "liq_24h")
        self.assertEqual(stats["n"], 3)
        self.assertEqual(stats["survived"], 2)
        self.assertEqual(stats["median"], 0.0)
        self.assertEqual(stats["best"], 100.0)

    def test_missing_prices_are_skipped_not_counted_as_zero(self):
        rows = [
            {"liq_0": 100, "liq_24h": 100, "price_0": 1, "price_24h": None},
            {"liq_0": 100, "liq_24h": 100, "price_0": 1, "price_24h": 3},
        ]
        stats = digest._cohort_stats(rows, "price_24h", "liq_24h")
        self.assertEqual(stats["median"], 200.0)

    def test_empty_cohort_has_no_median(self):
        stats = digest._cohort_stats([], "price_24h", "liq_24h")
        self.assertEqual(stats["n"], 0)
        self.assertIsNone(stats["median"])

    def test_scoreboard_renders_without_data(self):
        self.assertIn("Табло точности", digest.build_scoreboard())

    def test_digest_renders_and_fits_telegram_limits(self):
        text, _ = digest.build_digest()
        self.assertIn("Дайджест Meme-Scout", text)
        for chunk in digest._split(text, 3900):
            self.assertLessEqual(len(chunk), 3900)

    def test_long_text_is_split_into_sendable_chunks(self):
        chunks = digest._split("строка\n" * 3000, 3900)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3900)


class AlertButtonTests(unittest.TestCase):
    """callback_data у Telegram ограничен 64 байтами - это легко проглядеть."""

    ADDR = "0x" + "cd" * 20

    def test_callback_data_fits_telegram_limit(self):
        import bot

        for chain in ("base", "robinhood"):
            for alert_type in ("new_token", "survivor", "pump", "rug"):
                keyboard = bot.build_alert_keyboard(chain, self.ADDR, alert_type)
                for row in keyboard.inline_keyboard:
                    for button in row:
                        self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_callback_data_round_trips(self):
        import bot

        keyboard = bot.build_alert_keyboard("robinhood", self.ADDR, "new_token")
        for row in keyboard.inline_keyboard:
            for button in row:
                parsed = bot._parse_cb(button.callback_data)
                self.assertIsNotNone(parsed)
                _, chain, address = parsed
                self.assertEqual(chain, "robinhood")
                self.assertEqual(address, self.ADDR)

    def test_malformed_callback_data_is_rejected(self):
        import bot

        for junk in ("", "garbage", "d|solana|0xabc", "d|base", "d|base|0x1|extra"):
            self.assertIsNone(bot._parse_cb(junk))


class ChartTests(unittest.TestCase):
    ADDR = "0x" + "ef" * 20

    def test_returns_explanation_when_history_is_too_short(self):
        import chart

        image, note = chart.render_token_chart("base", "0x" + "77" * 20, "NODATA")
        self.assertIsNone(image)
        self.assertIn("мало данных", note)

    def test_renders_png_once_enough_snapshots_exist(self):
        import chart

        for i in range(6):
            db.record_price_snapshot("base", self.ADDR, 0.001 * (1 + i), 1000 * (1 + i), 500)
        image, note = chart.render_token_chart("base", self.ADDR, "TEST")
        self.assertIsNotNone(image, note)
        self.assertEqual(image.getvalue()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
