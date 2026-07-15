"""GoPlus Security token_security API client (free, no key required).

Docs: https://docs.gopluslabs.io/reference/token-security-api
Not guaranteed to cover very new chains (e.g. Robinhood Chain) - callers
must treat a None/empty result as "unsupported", not as "safe".
"""
import httpx

BASE_URL = "https://api.gopluslabs.io/api/v1/token_security"


def _top_holder_pct(holders: list) -> float | None:
    if not holders:
        return None
    total = 0.0
    for h in holders[:10]:
        try:
            total += float(h.get("percent", 0)) * 100
        except (TypeError, ValueError):
            continue
    return total


def _lp_locked_or_burned(lp_holders: list) -> bool | None:
    if not lp_holders:
        return None
    for h in lp_holders:
        tag = (h.get("tag") or "").lower()
        is_locked = str(h.get("is_locked", "0")) == "1"
        is_burn = "burn" in tag or h.get("address", "").lower() in (
            "0x000000000000000000000000000000000000dead",
            "0x0000000000000000000000000000000000000000",
        )
        if is_locked or is_burn:
            return True
    return False


async def get_token_security(chain_id: int, address: str) -> dict | None:
    """Returns a normalized signal dict, or None if the chain/token isn't covered."""
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/{chain_id}", params={"contract_addresses": address}
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        payload = resp.json()
        result = (payload.get("result") or {}).get(address.lower())
        if not result:
            return None

        def _bool(key):
            val = result.get(key)
            return None if val is None else str(val) == "1"

        def _num(key):
            try:
                return float(result.get(key))
            except (TypeError, ValueError):
                return None

        return {
            "is_open_source": _bool("is_open_source"),
            "is_honeypot": _bool("is_honeypot"),
            "is_mintable": _bool("is_mintable"),
            "is_proxy": _bool("is_proxy"),
            "can_take_back_ownership": _bool("can_take_back_ownership"),
            "is_blacklisted_func": _bool("is_blacklisted"),
            "selfdestruct": _bool("selfdestruct"),
            "buy_tax": _num("buy_tax"),
            "sell_tax": _num("sell_tax"),
            "holder_count": result.get("holder_count"),
            "lp_holder_count": result.get("lp_holder_count"),
            "top10_holder_pct": _top_holder_pct(result.get("holders", [])),
            "lp_locked_or_burned": _lp_locked_or_burned(result.get("lp_holders", [])),
            "owner_address": result.get("owner_address"),
        }
