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


async def _handle_candidate(application, chain: str, token_address: str, symbol: str,
                             name: str, liquidity_usd, price_usd, market_cap_usd):
    if db.is_token_seen(chain, token_address):
        return

    if (liquidity_usd or 0) < config.MIN_LIQUIDITY_USD:
        # Too little liquidity to be meaningful - still remember it so we don't reprocess forever.
        db.mark_token_seen(chain, token_address, symbol, name, None, "skipped_low_liquidity",
                            liquidity_usd, market_cap_usd)
        return

    signals = await enrich.gather_signals(chain, token_address, liquidity_usd)
    result = scoring.score_token(signals, config.MIN_LIQUIDITY_USD)

    db.mark_token_seen(chain, token_address, symbol, name, result.score, result.verdict,
                        liquidity_usd, market_cap_usd)
    db.add_to_watchlist(chain, token_address, baseline_price=price_usd)

    token = {
        "token_address": token_address,
        "symbol": symbol,
        "name": name,
        "liquidity_usd": liquidity_usd,
    }
    await bot.send_alert(application, bot.format_new_token_alert(chain, token, result))
    log.info("New token alert sent: %s %s score=%s verdict=%s", chain, symbol, result.score, result.verdict)


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
                )
        except Exception:
            log.exception("%s discovery loop error", chain)
        await asyncio.sleep(config.DISCOVERY_POLL_SECONDS)


async def run_discovery_base(application):
    await run_discovery(application, "base")


async def run_discovery_robinhood(application):
    await run_discovery(application, "robinhood")
