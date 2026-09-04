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
import bot  # noqa: E402
import traction  # noqa: E402
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
    """Приоритет алертов по движениям цены."""

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
                        if button.url:
                            continue  # кнопка-ссылка, callback_data у неё нет
                        self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_callback_data_round_trips(self):
        import bot

        keyboard = bot.build_alert_keyboard("robinhood", self.ADDR, "new_token")
        for row in keyboard.inline_keyboard:
            for button in row:
                if button.url:
                    continue  # кнопка-ссылка открывается напрямую, без callback
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


def _pool(**kw):
    """Пул с параметрами живого, но скучного токена - тесты меняют что нужно."""
    base = dict(
        liquidity_usd=70000.0, price_usd=1.0,
        volume_h1=40000.0, buyers_h1=60, sellers_h1=40, buys_h1=90, sells_h1=60,
    )
    base.update(kw)
    return base


class TractionTests(unittest.TestCase):
    """Ворота спроса - то, чего в первой версии бота не было вовсе."""

    def test_live_token_passes(self):
        result = traction.evaluate(_pool(), liq_0=70000.0, price_0=1.0)
        self.assertTrue(result.passed, result.fail_reason)
        self.assertGreater(result.score, 0)

    def test_empty_clone_with_huge_liquidity_is_rejected(self):
        """Реальный случай из базы: LP $450k, объём за час - $12, торгов нет."""
        pool = _pool(liquidity_usd=450000.0, volume_h1=12.0, buyers_h1=0,
                     sellers_h1=0, buys_h1=0, sells_h1=0)
        result = traction.evaluate(pool, liq_0=450000.0, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertFalse(result.fatal)  # объём мог ещё появиться - даём время

    def test_pulled_liquidity_is_fatal(self):
        """openhuman: $63 829 ликвидности превратились в $4 за сутки."""
        result = traction.evaluate(_pool(liquidity_usd=4.0), liq_0=63829.0, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertTrue(result.fatal)

    def test_wash_trading_is_fatal(self):
        pool = _pool(liquidity_usd=60000.0, volume_h1=60000.0 * 50)
        result = traction.evaluate(pool, liq_0=60000.0, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertTrue(result.fatal)
        self.assertIn("накрутку", result.fail_reason)

    def test_volume_without_distinct_buyers_is_rejected(self):
        """Объём накрутить легко, сотню разных кошельков - нет."""
        pool = _pool(buyers_h1=3, sellers_h1=2, buys_h1=200, sells_h1=150)
        result = traction.evaluate(pool, liq_0=70000.0, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertIn("покупателей", result.fail_reason)

    def test_crowd_exiting_is_rejected(self):
        pool = _pool(buyers_h1=30, sellers_h1=120)
        result = traction.evaluate(pool, liq_0=70000.0, price_0=1.0)
        self.assertFalse(result.passed)

    def test_price_collapse_is_fatal(self):
        result = traction.evaluate(_pool(price_usd=0.2), liq_0=70000.0, price_0=1.0)
        self.assertFalse(result.passed)
        self.assertTrue(result.fatal)

    def test_more_buyers_scores_higher(self):
        weak = traction.evaluate(_pool(buyers_h1=30), liq_0=70000.0, price_0=1.0)
        strong = traction.evaluate(_pool(buyers_h1=150), liq_0=70000.0, price_0=1.0)
        self.assertTrue(weak.passed and strong.passed)
        self.assertGreater(strong.score, weak.score)


class SpamFilterTests(unittest.TestCase):
    """Фабрики клонов и перезапуски одного мема - основной шум в выдаче."""

    def setUp(self):
        with db._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE chain='spamtest'")

    def _seen(self, symbol, liq):
        db.mark_token_seen("spamtest", "0x" + symbol.lower().ljust(8, "0"), symbol,
                           symbol, 80, "green", liq, None)

    def test_clone_batch_is_detected(self):
        """DERP/CLOWN/SGOOSE: пять пулов с LP $447k за 14 секунд."""
        for sym in ("derp", "clown", "sgoose"):
            self._seen(sym, 447627.0)
        reason = discovery._spam_reason("spamtest", "bfrg", "Bonk Frog", 447559.0)
        self.assertIsNotNone(reason)
        self.assertIn("клонов", reason)

    def test_distinct_liquidity_is_not_a_clone(self):
        self._seen("alpha", 61000.0)
        self._seen("beta", 88000.0)
        self.assertIsNone(discovery._spam_reason("spamtest", "gamma", "Gamma", 132000.0))

    def test_dust_pools_do_not_trigger_relaunch_filter(self):
        """Ложное срабатывание, поймано на живом боте 2026-08-20.

        У ходового тикера набираются десятки мусорных пулов на пару тысяч
        долларов. Если считать их, живой токен с популярным именем глушится,
        не начав торговаться - именно так был отсеян AGENT со 132 покупателями.
        """
        for i in range(30):
            db.mark_token_seen("spamtest", os.urandom(8).hex(), "AGENT",
                               "Some Other Agent", None, "skipped_low_liquidity", 2500.0, None)
        self.assertIsNone(discovery._spam_reason("spamtest", "AGENT", "Circle Agent", 70321.0))

    def test_same_ticker_different_project_is_not_a_relaunch(self):
        for name in ("1stAgentToken", "Meida Agent", "Agentic Trading Bot"):
            db.mark_token_seen("spamtest", os.urandom(8).hex(), "AGENT", name,
                               80, "green", 60000.0 + len(name) * 100, None)
        self.assertIsNone(discovery._spam_reason("spamtest", "AGENT", "Circle Agent", 70321.0))

    def test_repeated_relaunch_is_filtered(self):
        """lilcobie перезапускали 4 раза за сутки."""
        for i in range(config.RELAUNCH_MAX_COPIES):
            # Ликвидность заведомо разная, чтобы сработало правило перезапуска,
            # а не правило пачки клонов.
            db.mark_token_seen("spamtest", os.urandom(8).hex(), "lilcobie",
                               "lil cobie bot", 80, "green", 60000.0 + i * 9000, None)
        reason = discovery._spam_reason("spamtest", "lilcobie", "lil cobie bot", 123456.0)
        self.assertIsNotNone(reason)
        self.assertIn("перезапуск", reason)


class ProbationDbTests(unittest.TestCase):
    def test_candidate_flows_from_pending_to_promoted(self):
        db.add_to_probation("base", "0xprobation1", "0xpool1", "TEST", "Test",
                            price_0=1.0, liq_0=70000.0, score=80, verdict="green",
                            first_check_delay=-1)
        due = [c for c in db.get_probation_due(50) if c["address"] == "0xprobation1"]
        self.assertEqual(len(due), 1)

        db.reschedule_probation("base", "0xprobation1", 3600, "объём мал", buyers=12, volume=900)
        still_due = [c for c in db.get_probation_due(50) if c["address"] == "0xprobation1"]
        self.assertEqual(still_due, [], "перенесённый кандидат не должен быть в очереди")

        pending = [c for c in db.get_probation_pending(50) if c["address"] == "0xprobation1"]
        self.assertEqual(pending[0]["best_buyers"], 12)

        db.close_probation("base", "0xprobation1", "promoted", "спрос подтверждён")
        self.assertEqual(db.get_probation_due(50), [])
        self.assertTrue(db.is_in_probation("base", "0xprobation1"))


class ConfirmedTokenTrackingTests(unittest.TestCase):
    """Подтверждённый токен - самый ценный: памп по нему обязан пушиться."""

    def test_confirmed_outcome_counts_as_tracked(self):
        db.record_outcome_start("base", "0xconfirmed1", "OK", 71, "confirmed", 1.0, 70000.0)
        self.assertTrue(db.is_tracked("base", "0xconfirmed1"))
        self.assertEqual(pump_dump._priority(db.is_tracked("base", "0xconfirmed1")), "high")

    def test_unknown_token_is_not_tracked(self):
        self.assertFalse(db.is_tracked("base", "0xneverseen"))


class LookalikeTests(unittest.TestCase):
    """Одинаковые токены с разных адресов - то, что пользователь видел в чате."""

    def setUp(self):
        with db._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE chain='looktest'")

    def _seen(self, symbol, name, verdict="green"):
        db.mark_token_seen("looktest", os.urandom(8).hex(), symbol, name,
                           80, verdict, 70000.0, None)

    def test_exact_copies_are_counted(self):
        for _ in range(3):
            self._seen("openhuman", "openhuman")
        lk = db.count_lookalikes("looktest", "0xnew", "openhuman", "openhuman", 7 * 86400)
        self.assertEqual(lk["same_name"], 3)
        self.assertEqual(lk["same_ticker"], 0)

    def test_same_ticker_other_projects_are_counted_separately(self):
        self._seen("XTS", "First Project")
        self._seen("XTS", "Second Project")
        lk = db.count_lookalikes("looktest", "0xnew", "XTS", "Third Project", 7 * 86400)
        self.assertEqual(lk["same_name"], 0)
        self.assertEqual(lk["same_ticker"], 2)

    def test_dust_is_not_counted_as_a_lookalike(self):
        for _ in range(20):
            self._seen("DUST", "Dusty", verdict="skipped_low_liquidity")
        lk = db.count_lookalikes("looktest", "0xnew", "DUST", "Dusty", 7 * 86400)
        self.assertEqual(lk["same_name"], 0)

    def test_second_copy_is_now_filtered(self):
        """Порог снижен до 1: проходит только первая находка."""
        self._seen("copycat", "Copy Cat")
        reason = discovery._spam_reason("looktest", "copycat", "Copy Cat", 71000.0)
        self.assertIsNotNone(reason)
        self.assertIn("перезапуск", reason)

    def test_first_launch_still_passes(self):
        self.assertIsNone(discovery._spam_reason("looktest", "brandnew", "Brand New", 71000.0))

    def test_warning_is_empty_without_lookalikes(self):
        self.assertEqual(bot.format_lookalike_warning("X", {"same_name": 0, "same_ticker": 0}, 7), "")

    def test_russian_plurals(self):
        self.assertIn("1 другой проект", bot.format_lookalike_warning("X", {"same_ticker": 1}, 7))
        self.assertIn("2 других проекта", bot.format_lookalike_warning("X", {"same_ticker": 2}, 7))
        self.assertIn("5 других проектов", bot.format_lookalike_warning("X", {"same_ticker": 5}, 7))
        self.assertIn("2 раза", bot.format_lookalike_warning("X", {"same_name": 2}, 7))
        self.assertIn("18 раз", bot.format_lookalike_warning("X", {"same_name": 18}, 7))


class DexScreenerButtonTests(unittest.TestCase):
    """Кнопка на график: по Robinhood Chain бот раньше вёл только в обозреватель блоков."""

    ADDR = "0x" + "ab" * 20

    def test_both_chains_get_a_dexscreener_button(self):
        for chain in ("base", "robinhood"):
            keyboard = bot.build_alert_keyboard(chain, self.ADDR, "confirmed")
            urls = [b.url for row in keyboard.inline_keyboard for b in row if b.url]
            self.assertTrue(any("dexscreener.com" in u for u in urls),
                            f"нет кнопки на DexScreener для сети {chain}")
            self.assertTrue(any(f"/{chain}/" in u for u in urls),
                            f"ссылка ведёт не в ту сеть для {chain}")

    def test_chart_links_come_before_block_explorers(self):
        """Пользователь открывает график, а не страницу адреса."""
        for chain in ("base", "robinhood"):
            names = list(bot.EXPLORER_LINKS[chain].keys())
            self.assertEqual(names[0], "dexscreener",
                             f"для {chain} первым должен идти dexscreener, а не {names[0]}")
