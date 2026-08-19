"""Единая точка получения текущей рыночной картинки по токену.

Раньше эта логика жила внутри pump_dump; теперь ей пользуются ещё watcher
исходов («выживший») и команды бота, поэтому вынесена отдельно.
"""
import config

from sources import dexscreener, geckoterminal

DEXSCREENER_CHAIN_ID = {"base": "base", "robinhood": "robinhood"}

GECKOTERMINAL_NETWORK = {
    "base": config.GECKOTERMINAL_BASE_NETWORK,
    "robinhood": config.GECKOTERMINAL_ROBINHOOD_NETWORK,
}


async def get_market_data(chain: str, address: str, allow_fallback: bool = True) -> dict | None:
    """DEX Screener первым (лимиты мягче), GeckoTerminal - запасной вариант.

    allow_fallback=False бережёт квоту GeckoTerminal: для тысяч мусорных
    токенов в watchlist хватает DEX Screener, а «не индексируется нигде» для
    них и так означает «мёртв». Запасной путь оставляем важным токенам.
    """
    data = await dexscreener.get_token_data(DEXSCREENER_CHAIN_ID[chain], address)
    if data:
        return data
    if not allow_fallback:
        return None
    return await geckoterminal.get_pool_by_token(GECKOTERMINAL_NETWORK[chain], address)
