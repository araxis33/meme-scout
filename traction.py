"""Оценка РЕАЛЬНОГО спроса на токен - второй этап отбора после scoring.py.

Зачем это вообще появилось. scoring.py проверяет контракт: не honeypot,
LP сожжён, владелец отказался от прав. Всё это можно проверить в секунду
рождения пула - и именно поэтому первая версия бота судила токены в этот
момент. Проблема в том, что проверки контракта проходит ЛЮБОЙ лаунчпадный
токен: они все чеканятся одной фабрикой по одному шаблону. В базе бота это
видно прямым текстом: у токенов, умерших за сутки, средний score 74, а у
выживших - 51. Оценка безопасности не просто бесполезна для прогноза, она
отрицательно скоррелирована с ним.

Причина простая: в момент создания пула торгов ещё нет физически. Нечего
измерять. Поэтому здесь мы меряем не контракт, а спрос - и не в момент
рождения, а через час-другой, когда рынок уже высказался:

  * сколько РАЗНЫХ кошельков купило (объём накрутить легко, сотню разных
    покупателей - дорого);
  * объём относительно размера пула (снизу - торговля вообще идёт,
    сверху - защита от wash-trading);
  * не слита ли ликвидность относительно момента находки;
  * покупают или уже разбегаются.
"""
from dataclasses import dataclass, field

import config


@dataclass
class TractionResult:
    passed: bool
    score: int
    reasons: list = field(default_factory=list)
    fail_reason: str = ""
    fatal: bool = False          # безнадёжен - отчислять сразу, не ждать
    buyers_h1: int = 0
    volume_h1: float = 0.0
    liq_ratio: float = 0.0


def _pct(new, old):
    if not old:
        return None
    return (new - old) / old * 100


def evaluate(pool: dict, liq_0: float, price_0: float) -> TractionResult:
    """Пропускать ли кандидата в чат. Все ворота должны быть пройдены."""
    liq = float(pool.get("liquidity_usd") or 0)
    vol_h1 = float(pool.get("volume_h1") or 0)
    buyers = int(pool.get("buyers_h1") or 0)
    sellers = int(pool.get("sellers_h1") or 0)
    buys = int(pool.get("buys_h1") or 0)
    sells = int(pool.get("sells_h1") or 0)
    price = float(pool.get("price_usd") or 0)

    liq_ratio = (liq / liq_0) if liq_0 else 0.0
    vol_liq = (vol_h1 / liq) if liq else 0.0

    base = dict(buyers_h1=buyers, volume_h1=vol_h1, liq_ratio=liq_ratio)

    def fail(reason: str, fatal: bool = False) -> TractionResult:
        return TractionResult(passed=False, score=0, fail_reason=reason, fatal=fatal, **base)

    # --- ворота, после которых ждать уже бессмысленно --------------------
    if liq < config.MIN_LIQUIDITY_USD:
        return fail(f"ликвидность упала до ${liq:,.0f}", fatal=True)
    # Слив меряем только там, где было чему сливаться: у пула на пару тысяч
    # обычные качели дают -40% без всякого злого умысла, и это правило хоронило
    # ровно тех новичков, ради которых порог находки и опущен.
    if (liq_0 >= config.PROBATION_SLIDE_FLOOR_USD
            and liq_ratio < config.PROBATION_MIN_LIQ_RATIO):
        return fail(f"ликвидность слита: {liq_ratio*100:.0f}% от начальной", fatal=True)
    price_drop = _pct(price, price_0)
    if price_drop is not None and price_drop < -config.PROBATION_MAX_PRICE_DROP_PCT:
        return fail(f"цена сложилась на {abs(price_drop):.0f}%", fatal=True)

    # --- ворота, по которым имеет смысл дать ещё один шанс ---------------
    if vol_h1 < config.PROBATION_MIN_VOLUME_H1:
        return fail(f"объём за час всего ${vol_h1:,.0f}")
    if vol_liq < config.PROBATION_MIN_VOL_LIQ:
        return fail(f"торговли почти нет: объём {vol_liq*100:.1f}% от пула")
    if vol_liq > config.PROBATION_MAX_VOL_LIQ:
        return fail(f"похоже на накрутку: объём в {vol_liq:.0f}× больше пула", fatal=True)
    if buyers < config.PROBATION_MIN_BUYERS_H1:
        return fail(f"покупателей всего {buyers} - интереса нет")
    if sellers and (buyers / sellers) < config.PROBATION_MIN_BUY_SELL_RATIO:
        return fail(f"из токена выходят: {buyers} покупателей против {sellers} продавцов")
    # Последние ворота, и они про деньги, а не про интерес. Показывать имеет
    # смысл то, из чего можно выйти: либо пул уже торгуемого размера, либо он
    # вырос в разы с момента находки - а это чужие деньги, зашедшие после нас.
    # Без этого опущенный порог находки вывалил бы в чат весь мелкий шум.
    if liq < config.PUSH_MIN_LIQUIDITY_USD and liq_ratio < config.PROBATION_PUSH_LIQ_GROWTH:
        return fail(f"пул всего ${liq:,.0f} и вырос лишь в {liq_ratio:.1f}× - рано показывать")

    # --- прошёл: считаем силу сигнала ------------------------------------
    reasons = []
    score = 0.0

    # Разные покупатели - самый весомый признак.
    buyer_pts = min(40.0, 40.0 * buyers / (config.PROBATION_MIN_BUYERS_H1 * 4))
    score += buyer_pts
    reasons.append(f"👥 {buyers} разных покупателей за час (продавцов {sellers})")

    # Оборот относительно пула.
    vol_pts = min(25.0, 25.0 * vol_liq / 2.0)
    score += vol_pts
    reasons.append(f"💵 Объём за час ${vol_h1:,.0f} ({vol_liq:.2f}× пула)")

    # Давление покупки.
    if buys + sells:
        buy_share = buys / (buys + sells)
        score += max(0.0, min(15.0, (buy_share - 0.35) / 0.30 * 15.0))
        reasons.append(f"📊 Сделок: {buys} покупок / {sells} продаж")

    # Ликвидность держится или растёт.
    liq_change = _pct(liq, liq_0)
    if liq_change is not None:
        score += max(0.0, min(10.0, 10.0 * (liq_ratio - 0.7) / 0.6))
        arrow = "растёт" if liq_change > 5 else ("держится" if liq_change > -15 else "проседает")
        reasons.append(f"🏦 Ликвидность ${liq:,.0f} ({liq_change:+.0f}%, {arrow})")

    # Цена.
    if price_drop is not None:
        score += max(0.0, min(10.0, (price_drop + 50) / 100 * 10.0))
        reasons.append(f"💹 Цена с момента находки: {price_drop:+.0f}%")

    return TractionResult(
        passed=True,
        score=round(min(100.0, score)),
        reasons=reasons,
        **base,
    )
