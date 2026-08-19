"""GeckoTerminal public API client (no key required).

Primary discovery source for freshly created pools on Base.
Docs: https://apiguide.geckoterminal.com/
"""
import asyncio
import logging
import time

import httpx

import config

log = logging.getLogger("meme-scout.geckoterminal")

BASE_URL = "https://api.geckoterminal.com/api/v2"
_HEADERS = {"Accept": "application/json"}

# Бесплатный тариф GeckoTerminal - ~30 запросов в минуту на всё приложение.
# Без общего троттлинга discovery и watcher вместе легко пробивают лимит и
# получают сплошные 429 (именно это и происходило).
_rate_lock = asyncio.Lock()
_last_request_ts = 0.0

# Одного троттлинга мало: если лимит уже пробит, GeckoTerminal держит IP в бане
# и отвечает 429 даже на одиночный запрос с паузой в 10 секунд. Продолжать
# стучаться - значит продлевать бан, поэтому после 429 уходим в паузу с
# удваивающимся интервалом и просто не ходим туда, пока она не кончится.
_cooldown_until = 0.0
_backoff_seconds = 0.0


async def _acquire() -> bool:
    """False - сидим в бане, запрос делать не надо."""
    global _last_request_ts
    async with _rate_lock:
        if time.monotonic() < _cooldown_until:
            return False
        wait = config.GECKOTERMINAL_MIN_INTERVAL - (time.monotonic() - _last_request_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts = time.monotonic()
        return True


def _note_rate_limited(context: str):
    global _cooldown_until, _backoff_seconds
    if time.monotonic() < _cooldown_until:
        # Запрос был уже в полёте, когда пауза взвелась (их до
        # WATCHLIST_CONCURRENCY штук). Это тот же самый отказ, а не новый -
        # иначе одна пачка сразу разгоняет паузу до получаса.
        return
    _backoff_seconds = min(
        (_backoff_seconds * 2) if _backoff_seconds else config.GECKOTERMINAL_COOLDOWN_START,
        config.GECKOTERMINAL_COOLDOWN_MAX,
    )
    _cooldown_until = time.monotonic() + _backoff_seconds
    log.warning("GeckoTerminal 429 on %s - пауза %.0f с", context, _backoff_seconds)


def _note_ok():
    global _backoff_seconds
    _backoff_seconds = 0.0


def cooldown_remaining() -> float:
    return max(0.0, _cooldown_until - time.monotonic())


def _index_included(payload: dict) -> dict:
    return {item["id"]: item.get("attributes", {}) for item in payload.get("included", [])}


def _normalize_pool(item: dict, included: dict) -> dict | None:
    attrs = item.get("attributes", {})
    rel = item.get("relationships", {})
    base_token_id = rel.get("base_token", {}).get("data", {}).get("id")
    token_attrs = included.get(base_token_id, {})
    token_address = token_attrs.get("address")
    if not token_address:
        return None

    def _f(key, default=0.0):
        val = attrs.get(key)
        try:
            return float(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    return {
        "token_address": token_address,
        "symbol": token_attrs.get("symbol") or "?",
        "name": token_attrs.get("name") or "?",
        "pool_address": attrs.get("address"),
        "price_usd": _f("base_token_price_usd"),
        "liquidity_usd": _f("reserve_in_usd"),
        "market_cap_usd": _f("market_cap_usd") or _f("fdv_usd"),
        "volume_24h": float(((attrs.get("volume_usd") or {}).get("h24")) or 0.0),
        "created_at": attrs.get("pool_created_at"),
    }


async def get_new_pools(network: str, pages: int = 1) -> list[dict]:
    """Fetch newest pools for a GeckoTerminal network slug (e.g. 'base')."""
    results = []
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        for page in range(1, pages + 1):
            if not await _acquire():
                break
            resp = await client.get(
                f"{BASE_URL}/networks/{network}/new_pools",
                params={"page": page, "include": "base_token,quote_token"},
            )
            if resp.status_code == 429:
                _note_rate_limited(f"{network} new_pools")
                break
            resp.raise_for_status()
            _note_ok()
            payload = resp.json()
            included = _index_included(payload)
            for item in payload.get("data", []):
                pool = _normalize_pool(item, included)
                if pool:
                    results.append(pool)
    return results


async def get_pool_by_token(network: str, token_address: str) -> dict | None:
    """Look up current price/liquidity for a token by searching its top pool."""
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        if not await _acquire():
            return None
        resp = await client.get(
            f"{BASE_URL}/networks/{network}/tokens/{token_address}/pools",
            params={"include": "base_token,quote_token"},
        )
        if resp.status_code == 429:
            _note_rate_limited(f"{network} token lookup")
            return None
        if resp.status_code != 200:
            return None
        _note_ok()
        payload = resp.json()
        included = _index_included(payload)
        pools = [p for p in (_normalize_pool(i, included) for i in payload.get("data", [])) if p]
        if not pools:
            return None
        return max(pools, key=lambda p: p["liquidity_usd"])
