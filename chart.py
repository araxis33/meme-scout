"""Рисует график цены и ликвидности по снимкам из price_snapshots.

Снимки живут 7 дней (prune_old_snapshots), так что это всегда «недельная»
картинка, а не история за всё время.
"""
import io
import logging
import time

import matplotlib

matplotlib.use("Agg")  # без GUI - бот работает как фоновый процесс

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

import db  # noqa: E402

log = logging.getLogger("meme-scout.chart")

MIN_POINTS = 3


def render_token_chart(chain: str, address: str, symbol: str, days: float = 7.0):
    """-> (BytesIO|None, подпись). None означает «данных не хватило»."""
    since = time.time() - days * 86400
    try:
        snapshots = db.get_snapshots(chain, address, since)
    except Exception:
        log.exception("Failed to load snapshots for %s %s", chain, address)
        return None, "Не смог достать историю из базы."

    points = [s for s in snapshots if s.get("price")]
    if len(points) < MIN_POINTS:
        return None, (
            f"По {symbol} пока мало данных для графика "
            f"({len(points)} точек). Снимки копятся, пока токен в watchlist - "
            "загляни позже."
        )

    times = [mdates.date2num(_dt(s["ts"])) for s in points]
    prices = [s["price"] for s in points]
    liquidity = [s.get("liquidity_usd") or 0 for s in points]

    fig, (ax_price, ax_liq) = plt.subplots(
        2, 1, figsize=(9, 5.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )

    ax_price.plot(times, prices, color="#2f7d32", linewidth=1.8)
    ax_price.fill_between(times, prices, min(prices), color="#2f7d32", alpha=0.12)
    ax_price.set_ylabel("Цена, $")
    ax_price.grid(alpha=0.25)
    ax_price.set_title(f"{symbol} - {chain}", loc="left", fontsize=12, fontweight="bold")

    ax_liq.plot(times, liquidity, color="#1565c0", linewidth=1.4)
    ax_liq.fill_between(times, liquidity, 0, color="#1565c0", alpha=0.12)
    ax_liq.set_ylabel("Ликвидность, $")
    ax_liq.grid(alpha=0.25)

    ax_liq.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M"))
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=110)
    plt.close(fig)
    buffer.seek(0)

    change = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0
    span_hours = (points[-1]["ts"] - points[0]["ts"]) / 3600
    caption = (
        f"{symbol} ({chain}): {change:+.1f}% за {span_hours:.0f} ч наблюдений, "
        f"{len(points)} точек. Ликвидность сейчас ${liquidity[-1]:,.0f}."
    )
    return buffer, caption


def _dt(ts: float):
    from datetime import datetime

    return datetime.fromtimestamp(ts)
