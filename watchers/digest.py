"""Утренний дайджест + табло точности.

Дайджест решает проблему шума: всё, что не дотянуло до пуша, копится в
digest_queue и приходит одним сообщением раз в сутки. Табло точности
показывает, что стало с прошлыми алертами - без него непонятно, какие
пороги крутить.
"""
import asyncio
import html
import logging
import statistics
import time
from datetime import datetime

import bot
import config
import db

log = logging.getLogger("meme-scout.digest")

VERDICT_LABEL = {"green": "🟢 зелёные", "yellow": "🟡 жёлтые", "red": "🔴 красные"}
TYPE_LABEL = {
    "new_token": "новые токены",
    "pump": "пампы",
    "dump": "дампы",
    "rug": "rug pull",
    "survivor": "выжившие",
}
# Ликвидность просела больше чем вдвое - считаем, что токен не пережил период.
SURVIVAL_LIQ_RATIO = 0.5
MAX_QUEUE_LINES = 12


def _pct(new, old):
    if not old or new is None:
        return None
    return (new - old) / old * 100


def _cohort_stats(rows: list[dict], price_field: str, liq_field: str) -> dict:
    """Считает по группе токенов: сколько выжило и какая медиана по цене."""
    survived = 0
    changes = []
    for row in rows:
        liq_0 = row.get("liq_0") or 0
        liq_now = row.get(liq_field) or 0
        if liq_0 > 0 and liq_now >= liq_0 * SURVIVAL_LIQ_RATIO:
            survived += 1
        change = _pct(row.get(price_field), row.get("price_0"))
        if change is not None:
            changes.append(change)
    return {
        "n": len(rows),
        "survived": survived,
        "median": statistics.median(changes) if changes else None,
        "best": max(changes) if changes else None,
    }


def _scoreboard_section(title: str, rows: list[dict], price_field: str, liq_field: str) -> list[str]:
    if not rows:
        return [f"<b>{title}</b>", "  пока нет дозревших данных"]

    lines = [f"<b>{title}</b>"]
    for verdict in ("green", "yellow", "red"):
        group = [r for r in rows if r.get("verdict") == verdict]
        if not group:
            continue
        stats = _cohort_stats(group, price_field, liq_field)
        median = f"{stats['median']:+.0f}%" if stats["median"] is not None else "н/д"
        lines.append(
            f"  {VERDICT_LABEL[verdict]}: {stats['n']} шт → выжило {stats['survived']}, "
            f"медиана {median}"
        )
    best = _cohort_stats(rows, price_field, liq_field)["best"]
    if best is not None:
        lines.append(f"  лучший результат: {best:+.0f}%")
    return lines


def build_scoreboard() -> str:
    """Табло точности - что стало с токенами, про которые мы алертили."""
    rows_24h = db.get_outcomes_since(time.time() - 7 * 86400, field="24h")
    rows_7d = db.get_outcomes_since(time.time() - 30 * 86400, field="7d")

    lines = ["📋 <b>Табло точности</b>", ""]
    lines += _scoreboard_section("Через 24 часа (алерты за 7 дней)", rows_24h, "price_24h", "liq_24h")
    lines.append("")
    lines += _scoreboard_section("Через 7 дней (алерты за 30 дней)", rows_7d, "price_7d", "liq_7d")
    lines += [
        "",
        "<i>«Выжил» = ликвидность не упала больше чем вдвое от уровня на момент алерта.</i>",
    ]
    return "\n".join(lines)


def _discovery_section(since: float) -> list[str]:
    counts = db.count_tokens_since(since)
    lines = ["🔍 <b>Разведка за сутки</b>"]
    for chain in ("base", "robinhood"):
        chain_counts = {v: n for (c, v), n in counts.items() if c == chain}
        total = sum(chain_counts.values())
        if not total:
            continue
        skipped = chain_counts.get("skipped_low_liquidity", 0)
        scored = total - skipped
        detail = ", ".join(
            f"{VERDICT_LABEL[v]} {chain_counts[v]}" for v in ("green", "yellow", "red") if chain_counts.get(v)
        )
        lines.append(
            f"  {bot.CHAIN_LABEL[chain]}: {total} найдено, {skipped} отсеяно по ликвидности, "
            f"{scored} проверено"
            + (f" ({detail})" if detail else "")
        )
    if len(lines) == 1:
        lines.append("  ничего нового")
    return lines


def _alerts_section(since: float) -> list[str]:
    counts = db.count_alert_events(since)
    if not counts:
        return ["🔔 <b>Алерты за сутки</b>", "  тишина"]
    lines = ["🔔 <b>Алерты за сутки</b>"]
    for alert_type in ("survivor", "new_token", "pump", "rug", "dump"):
        pushed = counts.get((alert_type, True), 0)
        quiet = counts.get((alert_type, False), 0)
        if not pushed and not quiet:
            continue
        lines.append(f"  {TYPE_LABEL.get(alert_type, alert_type)}: {pushed} пуш / {quiet} тихо")
    return lines


def _queue_section(pending: list[dict]) -> list[str]:
    if not pending:
        return ["🤫 <b>Тихая очередь</b>", "  пусто"]

    by_type: dict[str, list[dict]] = {}
    for item in pending:
        by_type.setdefault(item["alert_type"] or "other", []).append(item)

    lines = [f"🤫 <b>Тихая очередь ({len(pending)})</b>"]
    for alert_type, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  <b>{TYPE_LABEL.get(alert_type, alert_type)}</b> - {len(items)}")
        for item in items[:MAX_QUEUE_LINES // max(1, len(by_type))]:
            lines.append(f"    · {html.escape(item['summary'])}")
        if len(items) > MAX_QUEUE_LINES // max(1, len(by_type)):
            lines.append("    · ...")
    return lines


def build_digest() -> tuple[str, int]:
    """-> (текст, максимальный id очереди, который можно пометить съеденным)."""
    since = time.time() - 86400
    pending = db.get_pending_digest(limit=1000)
    max_id = max((item["id"] for item in pending), default=0)

    lines = [f"☀️ <b>Дайджест Meme-Scout</b> - {datetime.now().strftime('%d.%m.%Y')}", ""]
    lines += _discovery_section(since)
    lines.append("")
    lines += _alerts_section(since)
    lines.append("")
    lines += _queue_section(pending)
    lines.append("")
    lines.append(build_scoreboard())
    lines += ["", f"В watchlist сейчас: {db.watchlist_size()}"]
    return "\n".join(lines), max_id


async def send_digest(application, manual: bool = False) -> bool:
    text, max_id = build_digest()
    # Telegram режет сообщения на 4096 символах.
    chunks = _split(text, 3900)
    sent = False
    for chunk in chunks:
        sent = await bot.send_alert(application, chunk) or sent
    if sent and max_id:
        db.mark_digest_consumed(max_id)
    if sent:
        db.prune_digest_queue()
        log.info("Digest sent (manual=%s, queue up to id=%s)", manual, max_id)
    return sent


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _due_now() -> bool:
    now = datetime.now()
    target_reached = (now.hour, now.minute) >= (config.DIGEST_HOUR, config.DIGEST_MINUTE)
    return target_reached and db.get_state("last_digest_date") != now.strftime("%Y-%m-%d")


async def run_digest_watcher(application):
    if not config.DIGEST_ENABLED:
        log.info("Digest disabled via config")
        return
    while True:
        try:
            if _due_now():
                if await send_digest(application):
                    db.set_state("last_digest_date", datetime.now().strftime("%Y-%m-%d"))
        except Exception:
            log.exception("Digest watcher loop error")
        await asyncio.sleep(config.DIGEST_POLL_SECONDS)
