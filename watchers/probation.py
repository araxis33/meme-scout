"""Испытательный срок: находка доказывает спрос, прежде чем попасть в чат.

Раньше алерт уходил в момент создания пула. Это и была причина того, что
в выдаче оказывался сплошной мусор: в первую секунду жизни пула о токене
нельзя узнать НИЧЕГО, кроме устройства контракта, а контракт у всех
лаунчпадных клонов одинаково безупречный.

Теперь кандидат сидит здесь и перепроверяется через 45 минут, потом каждые
полтора часа. Пушится только тот, кто к этому моменту показал живую
торговлю (см. traction.py). Не показал за 12 часов - тихо отчисляется,
пользователь о нём никогда не узнает.
"""
import asyncio
import logging
import time

import bot
import config
import db
import traction
from sources import geckoterminal

log = logging.getLogger("meme-scout.probation")

NETWORKS = {
    "base": config.GECKOTERMINAL_BASE_NETWORK,
    "robinhood": config.GECKOTERMINAL_ROBINHOOD_NETWORK,
}


async def _resolve_pools(chain: str, candidates: list[dict]) -> dict[str, dict]:
    """Свежие метрики по кандидатам: адрес токена -> пул.

    Пакетным запросом идут те, у кого известен адрес пула (это 1 запрос на
    30 кандидатов). Остальные - поштучно, поэтому адрес пула сохраняется
    при находке.
    """
    network = NETWORKS[chain]
    by_token: dict[str, dict] = {}

    with_pool = [c for c in candidates if c.get("pool_address")]
    if with_pool:
        pools = await geckoterminal.get_pools_multi(
            network, [c["pool_address"] for c in with_pool]
        )
        for cand in with_pool:
            pool = pools.get((cand["pool_address"] or "").lower())
            if pool:
                by_token[cand["address"]] = pool

    for cand in candidates:
        if cand["address"] in by_token:
            continue
        pool = await geckoterminal.get_pool_by_token(network, cand["address"])
        if pool:
            by_token[cand["address"]] = pool

    return by_token


async def _promote(application, cand: dict, pool: dict, result):
    """Кандидат доказал спрос - вот это и есть настоящий сигнал бота."""
    chain, address = cand["chain"], cand["address"]
    symbol = cand.get("symbol") or pool.get("symbol") or "?"

    db.close_probation(chain, address, "promoted",
                       f"спрос подтверждён: {result.buyers_h1} покупателей",
                       buyers=result.buyers_h1, volume=result.volume_h1)
    # В watchlist и в замер точности попадают только подтверждённые токены -
    # раньше туда сваливались все подряд, отсюда и 1600+ записей с 429 от API.
    db.add_to_watchlist(chain, address, baseline_price=pool.get("price_usd"))
    db.record_outcome_start(chain, address, symbol, result.score, "confirmed",
                            pool.get("price_usd"), pool.get("liquidity_usd"))

    age_hours = (time.time() - cand["added_at"]) / 3600
    lookalikes = db.count_lookalikes(chain, address, symbol, cand.get("name"),
                                     config.LOOKALIKE_WINDOW_DAYS * 86400)
    text = bot.format_confirmed_token_alert(chain, address, symbol, cand.get("name") or "?",
                                            pool, cand, result, age_hours, lookalikes)
    pushed = await bot.route_alert(
        application,
        chain=chain,
        address=address,
        symbol=symbol,
        alert_type="confirmed",
        text=text,
        priority=bot.PRIORITY_HIGH,
        summary=(f"✅ {symbol} - спрос подтверждён: {result.buyers_h1} покупателей, "
                 f"объём ${result.volume_h1:,.0f}"),
    )
    log.info("Probation PASSED %s %s: traction=%s buyers=%s vol=%.0f pushed=%s",
             chain, symbol, result.score, result.buyers_h1, result.volume_h1, pushed)


async def _process_chain(application, chain: str, candidates: list[dict]):
    pools = await _resolve_pools(chain, candidates)
    now = time.time()
    deadline = config.PROBATION_MAX_HOURS * 3600
    retry_delay = config.PROBATION_RECHECK_MINUTES * 60

    for cand in candidates:
        address = cand["address"]
        expired = (now - cand["added_at"]) >= deadline
        pool = pools.get(address)

        if not pool:
            # Пул исчез из индекса - почти всегда означает мёртвый токен.
            if expired:
                db.close_probation(chain, address, "rejected", "пул не найден, срок вышел")
            else:
                db.reschedule_probation(chain, address, retry_delay, "нет данных по пулу")
            continue

        result = traction.evaluate(pool, cand.get("liq_0") or 0, cand.get("price_0") or 0)

        if result.passed:
            await _promote(application, cand, pool, result)
            continue

        if result.fatal:
            db.close_probation(chain, address, "rejected", result.fail_reason)
            log.info("Probation REJECTED %s %s: %s", chain, cand.get("symbol"), result.fail_reason)
            continue

        if expired:
            db.close_probation(chain, address, "rejected", f"срок вышел ({result.fail_reason})")
            continue

        db.reschedule_probation(chain, address, retry_delay, result.fail_reason,
                                buyers=result.buyers_h1, volume=result.volume_h1)


async def run_probation_watcher(application):
    if not config.PROBATION_ENABLED:
        log.info("Probation отключён - алерты уходят сразу при находке")
        return
    while True:
        try:
            due = db.get_probation_due(config.PROBATION_BATCH)
            if due:
                by_chain: dict[str, list] = {}
                for cand in due:
                    by_chain.setdefault(cand["chain"], []).append(cand)
                for chain, candidates in by_chain.items():
                    if chain not in NETWORKS:
                        continue
                    await _process_chain(application, chain, candidates)
        except Exception:
            log.exception("probation loop error")
        await asyncio.sleep(config.PROBATION_POLL_SECONDS)
