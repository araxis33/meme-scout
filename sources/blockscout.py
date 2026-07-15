"""Blockscout v2 API client (free, no key required).

Used for contract-verification status and holder concentration as a
fallback/cross-check, and as the primary source on chains GoPlus/honeypot.is
don't cover yet (e.g. Robinhood Chain).
"""
import httpx

INSTANCES = {
    "base": "https://base.blockscout.com",
    "robinhood": "https://robinhoodchain.blockscout.com",
}


async def is_contract_verified(chain: str, address: str) -> bool | None:
    base_url = INSTANCES.get(chain)
    if not base_url:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(f"{base_url}/api/v2/smart-contracts/{address}")
        except httpx.HTTPError:
            return None
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            return None
        return bool(resp.json().get("is_verified"))


async def get_holder_concentration(chain: str, address: str, top_n: int = 10) -> dict | None:
    """Returns {"holder_count": int, "top_n_pct": float} or None if unavailable."""
    base_url = INSTANCES.get(chain)
    if not base_url:
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token_resp = await client.get(f"{base_url}/api/v2/tokens/{address}")
            holders_resp = await client.get(
                f"{base_url}/api/v2/tokens/{address}/holders", params={"items_count": top_n}
            )
        except httpx.HTTPError:
            return None
        if token_resp.status_code != 200 or holders_resp.status_code != 200:
            return None

        token_data = token_resp.json()
        try:
            total_supply = float(token_data.get("total_supply") or 0)
        except (TypeError, ValueError):
            total_supply = 0
        holder_count = token_data.get("holders_count")

        items = holders_resp.json().get("items", [])[:top_n]
        if total_supply <= 0 or not items:
            return {"holder_count": holder_count, "top_n_pct": None}

        top_sum = 0.0
        for item in items:
            try:
                top_sum += float(item.get("value") or 0)
            except (TypeError, ValueError):
                continue

        return {"holder_count": holder_count, "top_n_pct": (top_sum / total_supply) * 100}
