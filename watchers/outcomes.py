"""Отслеживание судьбы токенов после алерта + сигнал «выживший».

98% найденных токенов умирают в первые сутки, поэтому сам факт «жив через
24 часа, ликвидность не просела, объём есть» - куда более редкое событие,
чем появление нового токена, и заслуживает отдельного алерта.

Побочный, но не менее важный эффект: контрольные точки 24ч и 7д копят
данные для «табло точности» в дайджесте - работает ли вообще наш скоринг.
"""
import asyncio
import logging

import bot
import config
import db
from sources import market

log = logging.getLogger("meme-scout.outcomes")

SEVEN_DAYS = 7 * 24 * 3600
BATCH = 40


def _is_survivor(outcome: dict, data: dict | None) -> bool:
    if not data:
        return False
    liq_0 = outcome.get("liq_0") or 0
    liq_now = data.get("liquidity_usd") or 0
    volume = data.get("volume_24h") or 0
    if liq_0 <= 0 or liq_now <= 0:
        return False
    if liq_now < liq_0 * config.SURVIVOR_MIN_LIQ_RATIO:
        return False
    return volume >= config.SURVIVOR_MIN_VOLUME_USD


async def _check_24h(application):
    due = db.get_outcomes_due("24h", config.SURVIVOR_MIN_AGE_HOURS * 3600, limit=BATCH)
    for outcome in due:
        chain, address = outcome["chain"], outcome["address"]
        try:
            data = await market.get_market_data(chain, address)
        except Exception:
            log.exception("24h checkpoint failed for %s %s", chain, address)
            continue

        db.fill_outcome_24h(
            chain,
            address,
            (data or {}).get("price_usd"),
            (data or {}).get("liquidity_usd"),
            (data or {}).get("volume_24h"),
        )

        if outcome.get("survivor_alerted"):
            continue
        if not _is_survivor(outcome, data):
            continue

        symbol = str((data or {}).get("symbol") or outcome.get("symbol") or "?")
        db.mark_survivor_alerted(chain, address)
        # Выжившего держим в watchlist принудительно, чтобы уборщик его не выбросил.
        db.add_to_watchlist_manual(chain, address, data.get("price_usd"))

        await bot.route_alert(
            application,
            chain=chain,
            address=address,
            symbol=symbol,
            alert_type="survivor",
            text=bot.format_survivor_alert(chain, address, symbol, outcome, data),
            priority=bot.PRIORITY_HIGH,
            summary=f"🏆 {symbol} пережил сутки",
        )
        log.info("Survivor alert sent: %s %s", chain, symbol)


async def _check_7d():
    due = db.get_outcomes_due("7d", SEVEN_DAYS, limit=BATCH)
    for outcome in due:
        chain, address = outcome["chain"], outcome["address"]
        try:
            data = await market.get_market_data(chain, address)
        except Exception:
            log.exception("7d checkpoint failed for %s %s", chain, address)
            continue
        db.fill_outcome_7d(chain, address, (data or {}).get("price_usd"), (data or {}).get("liquidity_usd"))


async def run_outcome_watcher(application):
    while True:
        try:
            await _check_24h(application)
            await _check_7d()
        except Exception:
            log.exception("Outcome watcher loop error")
        await asyncio.sleep(config.OUTCOME_POLL_SECONDS)
