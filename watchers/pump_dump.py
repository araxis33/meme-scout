"""Watchlist loop: detects pumps, dumps, and sudden liquidity drains (rug pulls)."""
import asyncio
import logging

import bot
import config
import db
from sources import dexscreener, geckoterminal

log = logging.getLogger("meme-scout.pump_dump")

DEXSCREENER_CHAIN_ID = {"base": "base", "robinhood": "robinhood"}


GECKOTERMINAL_NETWORK = {"base": config.GECKOTERMINAL_BASE_NETWORK, "robinhood": config.GECKOTERMINAL_ROBINHOOD_NETWORK}


async def _get_market_data(chain: str, address: str) -> dict | None:
    data = await dexscreener.get_token_data(DEXSCREENER_CHAIN_ID[chain], address)
    if data:
        return data
    return await geckoterminal.get_pool_by_token(GECKOTERMINAL_NETWORK[chain], address)


async def _check_token(application, chain: str, address: str):
    data = await _get_market_data(chain, address)
    if not data or not data.get("price_usd"):
        return

    price = data["price_usd"]
    liquidity = data.get("liquidity_usd") or 0
    symbol = data.get("symbol", "?")

    db.record_price_snapshot(chain, address, price, liquidity, data.get("volume_24h"))

    prev_1h = db.get_snapshot_before(chain, address, 3600)
    if prev_1h and prev_1h["price"]:
        pct_change = (price - prev_1h["price"]) / prev_1h["price"] * 100
        if pct_change >= config.PUMP_THRESHOLD_PCT and not db.alert_recently_sent(
            chain, address, "pump", config.ALERT_COOLDOWN_SECONDS
        ):
            await bot.send_alert(application, bot.format_pump_alert(chain, address, symbol, pct_change, price))
            db.mark_alert_sent(chain, address, "pump")
        elif pct_change <= -config.DUMP_THRESHOLD_PCT and not db.alert_recently_sent(
            chain, address, "dump", config.ALERT_COOLDOWN_SECONDS
        ):
            await bot.send_alert(application, bot.format_dump_alert(chain, address, symbol, pct_change, price))
            db.mark_alert_sent(chain, address, "dump")

    prev_10m = db.get_snapshot_before(chain, address, 600)
    if (
        prev_10m
        and prev_10m["liquidity_usd"]
        and liquidity < prev_10m["liquidity_usd"] * (1 - config.LIQUIDITY_DROP_THRESHOLD_PCT / 100)
        and not db.alert_recently_sent(chain, address, "rug", config.ALERT_COOLDOWN_SECONDS)
    ):
        await bot.send_alert(
            application, bot.format_rug_alert(chain, address, symbol, prev_10m["liquidity_usd"], liquidity)
        )
        db.mark_alert_sent(chain, address, "rug")


async def run_pump_dump_watcher(application):
    while True:
        try:
            for entry in db.get_watchlist():
                await _check_token(application, entry["chain"], entry["address"])
            db.prune_old_snapshots()
        except Exception:
            log.exception("Pump/dump watcher loop error")
        await asyncio.sleep(config.WATCHLIST_POLL_SECONDS)
