"""Watchlist loop: детектит пампы, дампы и резкий слив ликвидности (rug pull).

Watchlist пополняется автоматически и разрастается до тысяч записей, поэтому
за один цикл обходим только порцию (самых давно не проверявшихся), а мёртвые
и просроченные записи выбрасываем. Без этого обход не укладывался в интервал
и упирался в 429 от GeckoTerminal.
"""
import asyncio
import logging

import bot
import config
import db
from sources import market

log = logging.getLogger("meme-scout.pump_dump")


def _priority(tracked: bool) -> str:
    """Памп/дамп/rug по «нашему» токену - пуш, по случайному мусору - в дайджест."""
    return bot.PRIORITY_HIGH if tracked else bot.PRIORITY_LOW


async def _check_token(application, chain: str, address: str):
    if db.is_ignored(chain, address):
        db.deactivate_watchlist_entry(chain, address)
        return

    # «Наш» токен получает и пуши, и право на запасной источник данных;
    # мусору хватает DEX Screener - иначе выедаем квоту GeckoTerminal.
    tracked = db.is_tracked(chain, address)

    data = await market.get_market_data(chain, address, allow_fallback=tracked)
    if not data or not data.get("price_usd"):
        db.mark_watchlist_checked(chain, address, dead=True)
        return

    price = data["price_usd"]
    liquidity = data.get("liquidity_usd") or 0
    symbol = data.get("symbol", "?")

    db.record_price_snapshot(chain, address, price, liquidity, data.get("volume_24h"))
    db.mark_watchlist_checked(chain, address, dead=liquidity < config.WATCHLIST_DEAD_LIQUIDITY_USD)

    priority = _priority(tracked)

    prev_1h = db.get_snapshot_before(chain, address, 3600)
    if prev_1h and prev_1h["price"]:
        pct_change = (price - prev_1h["price"]) / prev_1h["price"] * 100
        if pct_change >= config.PUMP_THRESHOLD_PCT and not db.alert_recently_sent(
            chain, address, "pump", config.ALERT_COOLDOWN_SECONDS
        ):
            await bot.route_alert(
                application,
                chain=chain, address=address, symbol=symbol, alert_type="pump",
                text=bot.format_pump_alert(chain, address, symbol, pct_change, price),
                priority=priority,
                summary=f"🚀 {symbol} +{pct_change:.0f}% за час",
            )
            db.mark_alert_sent(chain, address, "pump")
        elif pct_change <= -config.DUMP_THRESHOLD_PCT and not db.alert_recently_sent(
            chain, address, "dump", config.ALERT_COOLDOWN_SECONDS
        ):
            await bot.route_alert(
                application,
                chain=chain, address=address, symbol=symbol, alert_type="dump",
                text=bot.format_dump_alert(chain, address, symbol, pct_change, price),
                priority=priority,
                summary=f"📉 {symbol} {pct_change:.0f}% за час",
            )
            db.mark_alert_sent(chain, address, "dump")

    prev_10m = db.get_snapshot_before(chain, address, 600)
    if (
        prev_10m
        and prev_10m["liquidity_usd"]
        and liquidity < prev_10m["liquidity_usd"] * (1 - config.LIQUIDITY_DROP_THRESHOLD_PCT / 100)
        and not db.alert_recently_sent(chain, address, "rug", config.ALERT_COOLDOWN_SECONDS)
    ):
        await bot.route_alert(
            application,
            chain=chain, address=address, symbol=symbol, alert_type="rug",
            text=bot.format_rug_alert(chain, address, symbol, prev_10m["liquidity_usd"], liquidity),
            priority=priority,
            summary=f"🚨 {symbol}: ликвидность ${prev_10m['liquidity_usd']:,.0f} → ${liquidity:,.0f}",
        )
        db.mark_alert_sent(chain, address, "rug")


async def _check_batch(application, entries):
    semaphore = asyncio.Semaphore(config.WATCHLIST_CONCURRENCY)

    async def guarded(entry):
        async with semaphore:
            try:
                await _check_token(application, entry["chain"], entry["address"])
            except Exception:
                log.exception("Check failed for %s %s", entry["chain"], entry["address"])

    await asyncio.gather(*(guarded(entry) for entry in entries))


async def run_pump_dump_watcher(application):
    while True:
        try:
            entries = db.get_watchlist_batch(config.WATCHLIST_BATCH)
            if entries:
                await _check_batch(application, entries)
            dropped = db.sweep_watchlist(
                config.WATCHLIST_DEAD_CHECKS, config.WATCHLIST_MAX_AGE_DAYS * 86400
            )
            if dropped:
                log.info("Watchlist sweep: dropped %s dead/expired entries (%s left)",
                         dropped, db.watchlist_size())
            db.prune_old_snapshots()
        except Exception:
            log.exception("Pump/dump watcher loop error")
        await asyncio.sleep(config.WATCHLIST_POLL_SECONDS)
