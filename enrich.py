"""Gathers security/legitimacy signals for a token from all sources,
merging them into the flat dict scoring.score_token() expects.
"""
import asyncio

import config
from sources import blockscout, goplus, honeypot

CHAIN_ID = {"base": config.BASE_CHAIN_ID, "robinhood": config.ROBINHOOD_CHAIN_ID}


def _first_not_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


async def gather_signals(chain: str, address: str, liquidity_usd: float) -> dict:
    chain_id = CHAIN_ID[chain]
    results = await asyncio.gather(
        goplus.get_token_security(chain_id, address),
        honeypot.check(chain_id, address),
        blockscout.is_contract_verified(chain, address),
        blockscout.get_holder_concentration(chain, address),
        return_exceptions=True,
    )
    gp, hp, verified, holders = (r if not isinstance(r, Exception) else None for r in results)
    gp = gp or {}
    hp = hp or {}
    holders = holders or {}

    return {
        "liquidity_usd": liquidity_usd,
        "is_honeypot": _first_not_none(hp.get("is_honeypot"), gp.get("is_honeypot")),
        "contract_verified": _first_not_none(gp.get("is_open_source"), verified),
        "is_mintable": gp.get("is_mintable"),
        "can_take_back_ownership": gp.get("can_take_back_ownership"),
        "is_proxy": gp.get("is_proxy"),
        "selfdestruct": gp.get("selfdestruct"),
        "lp_locked_or_burned": gp.get("lp_locked_or_burned"),
        "top10_holder_pct": _first_not_none(gp.get("top10_holder_pct"), holders.get("top_n_pct")),
        "buy_tax": _first_not_none(hp.get("buy_tax"), gp.get("buy_tax")),
        "sell_tax": _first_not_none(hp.get("sell_tax"), gp.get("sell_tax")),
    }
