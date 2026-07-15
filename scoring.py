"""Combines signals from goplus/honeypot/blockscout into a single score.

Gracefully degrades: any signal that is None (unavailable / unsupported
chain) is excluded from the weighted total rather than penalizing the
token, but the result is flagged `limited=True` so alerts can warn the
user that the check coverage was partial (expected on very new chains).
"""
from dataclasses import dataclass, field

CATEGORY_WEIGHTS = {
    "honeypot": 25,
    "verified": 15,
    "ownership": 15,
    "lp_lock": 15,
    "holder_concentration": 15,
    "tax": 10,
    "liquidity_depth": 5,
}


@dataclass
class ScoreResult:
    score: int
    verdict: str
    verdict_emoji: str
    reasons: list = field(default_factory=list)
    limited: bool = False


def score_token(signals: dict, min_liquidity_usd: float) -> ScoreResult:
    achieved = 0.0
    possible = 0.0
    reasons: list[str] = []
    hard_fail = False

    is_honeypot = signals.get("is_honeypot")
    if is_honeypot is not None:
        possible += CATEGORY_WEIGHTS["honeypot"]
        if is_honeypot:
            hard_fail = True
            reasons.append("🚨 Honeypot: продажа заблокирована")
        else:
            achieved += CATEGORY_WEIGHTS["honeypot"]
            reasons.append("✅ Не honeypot, продажа проходит")

    verified = signals.get("contract_verified")
    if verified is not None:
        possible += CATEGORY_WEIGHTS["verified"]
        if verified:
            achieved += CATEGORY_WEIGHTS["verified"]
            reasons.append("✅ Контракт верифицирован")
        else:
            reasons.append("⚠️ Контракт НЕ верифицирован")

    danger_flags = {
        "is_mintable": signals.get("is_mintable"),
        "can_take_back_ownership": signals.get("can_take_back_ownership"),
        "is_proxy": signals.get("is_proxy"),
        "selfdestruct": signals.get("selfdestruct"),
    }
    known_flags = {k: v for k, v in danger_flags.items() if v is not None}
    if known_flags:
        possible += CATEGORY_WEIGHTS["ownership"]
        if any(known_flags.values()):
            bad = [k for k, v in known_flags.items() if v]
            reasons.append(f"⚠️ Опасные функции контракта: {', '.join(bad)}")
        else:
            achieved += CATEGORY_WEIGHTS["ownership"]
            reasons.append("✅ Нет опасных владельческих функций")

    lp_locked = signals.get("lp_locked_or_burned")
    if lp_locked is not None:
        possible += CATEGORY_WEIGHTS["lp_lock"]
        if lp_locked:
            achieved += CATEGORY_WEIGHTS["lp_lock"]
            reasons.append("✅ LP заблокирован/сожжён")
        else:
            reasons.append("⚠️ LP не заблокирован — риск rug pull")

    top10 = signals.get("top10_holder_pct")
    if top10 is not None:
        possible += CATEGORY_WEIGHTS["holder_concentration"]
        frac = max(0.0, min(1.0, (70 - top10) / 50))
        achieved += CATEGORY_WEIGHTS["holder_concentration"] * frac
        if top10 >= 50:
            reasons.append(f"⚠️ Топ-10 держат {top10:.0f}% supply — высокая концентрация")
        else:
            reasons.append(f"✅ Топ-10 держат {top10:.0f}% supply")

    buy_tax = signals.get("buy_tax")
    sell_tax = signals.get("sell_tax")
    if buy_tax is not None and sell_tax is not None:
        possible += CATEGORY_WEIGHTS["tax"]
        total_tax = buy_tax + sell_tax
        if total_tax < 10:
            achieved += CATEGORY_WEIGHTS["tax"]
            reasons.append(f"✅ Налоги низкие (buy {buy_tax:.0f}% / sell {sell_tax:.0f}%)")
        else:
            reasons.append(f"⚠️ Высокие налоги (buy {buy_tax:.0f}% / sell {sell_tax:.0f}%)")

    liquidity_usd = signals.get("liquidity_usd")
    if liquidity_usd is not None:
        possible += CATEGORY_WEIGHTS["liquidity_depth"]
        if liquidity_usd >= min_liquidity_usd * 2:
            achieved += CATEGORY_WEIGHTS["liquidity_depth"]
        reasons.append(f"ℹ️ Ликвидность ${liquidity_usd:,.0f}")

    limited = possible < 50
    score = 0 if possible <= 0 else round(100 * achieved / possible)

    if hard_fail:
        score = min(score, 5)
        verdict, emoji = "red", "🔴"
    elif score >= 70:
        verdict, emoji = "green", "🟢"
    elif score >= 40:
        verdict, emoji = "yellow", "🟡"
    else:
        verdict, emoji = "red", "🔴"

    return ScoreResult(score=score, verdict=verdict, verdict_emoji=emoji, reasons=reasons, limited=limited)
