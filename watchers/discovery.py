"""Discovery loops: find newly created tokens/pools, score them, alert."""
import asyncio
import logging

import bot
import config
import db
import enrich
import scoring
from sources import geckoterminal

log = logging.getLogger("meme-scout.discovery")

# chain name -> GeckoTerminal network slug. Both confirmed live via direct API checks.
NETWORKS = {
    "base": config.GECKOTERMINAL_BASE_NETWORK,
    "robinhood": config.GECKOTERMINAL_ROBINHOOD_NETWORK,
}


def _spam_reason(chain: str, symbol: str, name: str, liquidity_usd) -> str | None:
    """Отсечь ботов-фабрик прямо на входе, не тратя на них проверки и запросы."""
    if config.CLONE_BATCH_ENABLED and liquidity_usd:
        twins = db.count_recent_similar_liquidity(
            chain, liquidity_usd, config.CLONE_BATCH_WINDOW_SECONDS,
            config.CLONE_BATCH_LIQ_TOLERANCE,
        )
        # Себя мы ещё не записали, поэтому сравниваем с порогом минус один.
        if twins >= max(1, config.CLONE_BATCH_MIN_COUNT - 1):
            return f"пачка клонов: {twins + 1} пулов с одинаковой ликвидностью подряд"

    copies = db.count_recent_same_name(
        chain, symbol, name, config.RELAUNCH_WINDOW_DAYS * 86400,
    )
    if copies >= config.RELAUNCH_MAX_COPIES:
        return f"перезапуск того же мема ({copies} копий за {config.RELAUNCH_WINDOW_DAYS:.0f} дн.)"
    return None


async def _handle_candidate(application, chain: str, token_address: str, symbol: str,
                             name: str, liquidity_usd, price_usd, market_cap_usd,
                             pool_address=None):
    if db.is_token_seen(chain, token_address):
        return
    if db.is_ignored(chain, token_address):
        return

    if (liquidity_usd or 0) < config.MIN_LIQUIDITY_USD:
        # Too little liquidity to be meaningful - still remember it so we don't reprocess forever.
        db.mark_token_seen(chain, token_address, symbol, name, None, "skipped_low_liquidity",
                            liquidity_usd, market_cap_usd)
        return

    spam = _spam_reason(chain, symbol, name, liquidity_usd)
    if spam:
        db.mark_token_seen(chain, token_address, symbol, name, None, "spam",
                            liquidity_usd, market_cap_usd)
        log.info("Spam filtered %s %s: %s", chain, symbol, spam)
        return

    signals = await enrich.gather_signals(chain, token_address, liquidity_usd)
    result = scoring.score_token(signals, config.MIN_LIQUIDITY_USD)

    db.mark_token_seen(chain, token_address, symbol, name, result.score, result.verdict,
                        liquidity_usd, market_cap_usd)

    # Красный - это honeypot или опасный контракт. Такому испытательный срок
    # не нужен: сколько бы спроса он ни показал, покупать его нельзя.
    if result.verdict == "red":
        await bot.route_alert(
            application,
            chain=chain,
            address=token_address,
            symbol=symbol,
            alert_type="new_token",
            text=bot.format_new_token_alert(chain, {
                "token_address": token_address, "symbol": symbol,
                "name": name, "liquidity_usd": liquidity_usd,
            }, result),
            priority=bot.PRIORITY_LOW,
            summary=f"{result.verdict_emoji} {symbol} - score {result.score}, отсеян по контракту",
        )
        return

    if not config.PROBATION_ENABLED:
        # Аварийный режим: старое поведение - пушить сразу при находке.
        db.add_to_watchlist(chain, token_address, baseline_price=price_usd)
        db.record_outcome_start(chain, token_address, symbol, result.score, result.verdict,
                                 price_usd, liquidity_usd)
        liquidity_note = f"${liquidity_usd:,.0f}" if liquidity_usd else "н/д"
        await bot.route_alert(
            application, chain=chain, address=token_address, symbol=symbol,
            alert_type="new_token",
            text=bot.format_new_token_alert(chain, {
                "token_address": token_address, "symbol": symbol,
                "name": name, "liquidity_usd": liquidity_usd,
            }, result),
            priority=bot.PRIORITY_HIGH,
            summary=f"{result.verdict_emoji} {symbol} - score {result.score}, ликвидность {liquidity_note}",
        )
        return

    # Основной путь: тихо взять на карандаш. Пользователь узнает о токене
    # только если тот через час-другой покажет живую торговлю.
    db.add_to_probation(
        chain, token_address, pool_address, symbol, name,
        price_0=price_usd, liq_0=liquidity_usd,
        score=result.score, verdict=result.verdict,
        first_check_delay=config.PROBATION_FIRST_CHECK_MINUTES * 60,
    )
    log.info("Probation ENROLLED %s %s score=%s liq=%.0f", chain, symbol, result.score,
             liquidity_usd or 0)


async def run_discovery(application, chain: str):
    network = NETWORKS[chain]
    while True:
        try:
            pools = await geckoterminal.get_new_pools(network)
            for pool in pools:
                if pool["token_address"].lower() in config.ROBINHOOD_KNOWN_QUOTE_TOKENS:
                    continue
                await _handle_candidate(
                    application, chain, pool["token_address"], pool["symbol"], pool["name"],
                    pool["liquidity_usd"], pool["price_usd"], pool["market_cap_usd"],
                    pool_address=pool.get("pool_address"),
                )
        except Exception:
            log.exception("%s discovery loop error", chain)
        await asyncio.sleep(config.DISCOVERY_POLL_SECONDS)


async def run_discovery_base(application):
    await run_discovery(application, "base")


async def run_discovery_robinhood(application):
    await run_discovery(application, "robinhood")
