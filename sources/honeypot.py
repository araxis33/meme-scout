"""honeypot.is buy/sell simulation API client (free, no key required).

Docs: https://honeypot.is/ (API used by the public web tool)
Not guaranteed to cover very new chains (e.g. Robinhood Chain) - callers
must treat a None result as "unsupported", not as "safe".
"""
import httpx

BASE_URL = "https://api.honeypot.is/v2/IsHoneypot"


async def check(chain_id: int, address: str) -> dict | None:
    async with httpx.AsyncClient(timeout=25) as client:
        try:
            resp = await client.get(BASE_URL, params={"address": address, "chainID": chain_id})
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if not payload.get("simulationSuccess", True) and not payload.get("honeypotResult"):
            return None

        honeypot_result = payload.get("honeypotResult") or {}
        simulation = payload.get("simulationResult") or {}
        summary = payload.get("summary") or {}

        return {
            "is_honeypot": honeypot_result.get("isHoneypot"),
            "honeypot_reason": honeypot_result.get("honeypotReason"),
            "buy_tax": simulation.get("buyTax"),
            "sell_tax": simulation.get("sellTax"),
            "transfer_tax": simulation.get("transferTax"),
            "risk": summary.get("risk"),
            "risk_level": summary.get("riskLevel"),
        }
