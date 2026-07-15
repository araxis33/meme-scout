"""DEX Screener public API client (no key required).

Used mainly as the price/liquidity/volume source for the pump & dump
watcher, and as a fallback discovery signal (latest boosted tokens).
Docs: https://docs.dexscreener.com/api/reference
"""
import httpx

BASE_URL = "https://api.dexscreener.com"


def _pick_best_pair(pairs: list[dict]) -> dict | None:
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def _normalize_pair(pair: dict) -> dict:
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    base_token = pair.get("baseToken") or {}
    return {
        "token_address": base_token.get("address"),
        "symbol": base_token.get("symbol") or "?",
        "name": base_token.get("name") or "?",
        "pair_address": pair.get("pairAddress"),
        "price_usd": float(pair.get("priceUsd") or 0),
        "liquidity_usd": float(liquidity.get("usd") or 0),
        "market_cap_usd": float(pair.get("marketCap") or pair.get("fdv") or 0),
        "volume_24h": float(volume.get("h24") or 0),
        "pair_created_at": pair.get("pairCreatedAt"),
        "url": pair.get("url"),
    }


async def get_token_data(chain_id: str, token_address: str) -> dict | None:
    """Fetch best (highest liquidity) pair for a token. Returns None if not indexed."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{BASE_URL}/tokens/v1/{chain_id}/{token_address}")
        if resp.status_code != 200:
            return None
        pairs = resp.json()
        if not isinstance(pairs, list):
            return None
        best = _pick_best_pair(pairs)
        return _normalize_pair(best) if best else None


async def get_latest_boosted() -> list[dict]:
    """Latest boosted (paid-promotion) tokens across all chains - supplementary discovery signal."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{BASE_URL}/token-boosts/latest/v1")
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
