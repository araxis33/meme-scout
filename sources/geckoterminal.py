"""GeckoTerminal public API client (no key required).

Primary discovery source for freshly created pools on Base.
Docs: https://apiguide.geckoterminal.com/
"""
import httpx

BASE_URL = "https://api.geckoterminal.com/api/v2"
_HEADERS = {"Accept": "application/json"}


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
            resp = await client.get(
                f"{BASE_URL}/networks/{network}/new_pools",
                params={"page": page, "include": "base_token,quote_token"},
            )
            if resp.status_code == 429:
                break
            resp.raise_for_status()
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
        resp = await client.get(
            f"{BASE_URL}/networks/{network}/tokens/{token_address}/pools",
            params={"include": "base_token,quote_token"},
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        included = _index_included(payload)
        pools = [p for p in (_normalize_pool(i, included) for i in payload.get("data", [])) if p]
        if not pools:
            return None
        return max(pools, key=lambda p: p["liquidity_usd"])
