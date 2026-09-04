"""Выгружает публичную статистику бота в JSON для сайта deftools.xyz.

Бот личный - отвечает только владельцу. А вот данные, которые он накопил,
показывать можно всем: это публичные ончейн-факты, ничего приватного.
Отсюда и такой обмен: страница на статике отдаёт статистику любому, а сам
бот остаётся однопользовательским и никуда не переезжает.

В JSON НЕ ПОПАДАЕТ ничего из .env - ни токен бота, ни chat_id.
"""
import argparse
import json
import logging
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("meme-scout.export")

DAILY_DAYS = 21
RECENT_FINDS = 20
RECENT_DRAINS = 15


def _conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def build_stats() -> dict:
    conn = _conn()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    first_seen = one("SELECT MIN(first_seen) FROM tokens")
    now = time.time()

    verdicts = {
        r["verdict"]: r["n"]
        for r in conn.execute("SELECT verdict, COUNT(*) n FROM tokens GROUP BY verdict")
    }
    chains = {
        r["chain"]: r["n"]
        for r in conn.execute("SELECT chain, COUNT(*) n FROM tokens GROUP BY chain")
    }
    scored = verdicts.get("green", 0) + verdicts.get("yellow", 0) + verdicts.get("red", 0)
    total = sum(chains.values())

    daily = [
        {"date": r["d"], "base": r["b"], "robinhood": r["r"]}
        for r in conn.execute(
            """SELECT date(first_seen,'unixepoch','localtime') d,
                      SUM(CASE WHEN chain='base' THEN 1 ELSE 0 END) b,
                      SUM(CASE WHEN chain='robinhood' THEN 1 ELSE 0 END) r
               FROM tokens WHERE first_seen >= ?
               GROUP BY d ORDER BY d""",
            (now - DAILY_DAYS * 86400,),
        )
    ]

    finds = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "chain": r["chain"],
            "address": r["address"],
            "score": r["score"],
            "verdict": r["verdict"],
            "liquidity_usd": r["liquidity_usd"],
            "first_seen": int(r["first_seen"]),
        }
        for r in conn.execute(
            """SELECT * FROM tokens WHERE verdict IN ('green','yellow')
               ORDER BY first_seen DESC LIMIT ?""",
            (RECENT_FINDS,),
        )
    ]

    # symbol живёт в tokens, а не в alerts_sent - поэтому джойн.
    drains = [
        {
            "symbol": r["symbol"] or "?",
            "chain": r["chain"],
            "address": r["address"],
            "ts": int(r["ts"]),
        }
        for r in conn.execute(
            """SELECT a.chain, a.address, a.ts, t.symbol
               FROM alerts_sent a LEFT JOIN tokens t
                 ON t.chain = a.chain AND t.address = a.address
               WHERE a.alert_type = 'rug' ORDER BY a.ts DESC LIMIT ?""",
            (RECENT_DRAINS,),
        )
    ]

    survivors = [
        {
            "symbol": r["symbol"] or "?",
            "chain": r["chain"],
            "address": r["address"],
            "score": r["score"],
            "alerted_at": int(r["alerted_at"]),
            "liq_0": r["liq_0"],
            "liq_24h": r["liq_24h"],
            "volume_24h": r["volume_24h"],
        }
        for r in conn.execute(
            # Считаем по фактическому критерию, а не по флагу survivor_alerted:
            # флаг проставлен и бэкфиллу, чтобы он не слал ретро-алерты, так что
            # как признак «выжил» он не годится.
            """SELECT * FROM outcomes
               WHERE checked_24h=1 AND liq_0 > 0 AND liq_24h >= liq_0
                     AND COALESCE(volume_24h, 0) >= ?
               ORDER BY alerted_at DESC LIMIT 10""",
            (config.SURVIVOR_MIN_VOLUME_USD,),
        )
    ]

    # Самая честная цифра, которую бот может о себе сообщить: предсказывал ли
    # его вердикт хоть что-нибудь. Ответ - нет, и это надо показывать, а не
    # прятать за словами «прошёл проверки».
    verdict_outcomes = [
        {
            "verdict": r["verdict"],
            "checked": r["n"],
            "held_liquidity": r["held"],
            "still_trading": r["traded"],
        }
        for r in conn.execute(
            """SELECT verdict, COUNT(*) n,
                      SUM(CASE WHEN liq_24h >= liq_0 * 0.5 THEN 1 ELSE 0 END) held,
                      SUM(CASE WHEN COALESCE(volume_24h,0) >= 1000 THEN 1 ELSE 0 END) traded
               FROM outcomes
               WHERE checked_24h=1 AND liq_0 > 0 AND liq_24h IS NOT NULL
                     AND verdict IN ('green','yellow','red')
               GROUP BY verdict ORDER BY n DESC"""
        )
    ]

    probation_states = {
        r["state"]: r["n"]
        for r in conn.execute("SELECT state, COUNT(*) n FROM probation GROUP BY state")
    }

    # Токены, доказавшие реальный спрос - единственный список на странице,
    # который что-то значит. Раньше на его месте стояли «прошедшие проверки».
    confirmed = [
        {
            "symbol": r["symbol"] or "?",
            "chain": r["chain"],
            "address": r["address"],
            "buyers_h1": r["best_buyers"],
            "volume_h1": r["best_volume"],
            "liquidity_usd": r["liq_0"],
            "confirmed_at": int(r["added_at"]),
            "hours_waited": round(max(0.0, (r["closed_guess"] or 0)) / 3600, 1),
        }
        for r in conn.execute(
            """SELECT p.symbol, p.chain, p.address, p.best_buyers, p.best_volume,
                      p.added_at, o.liq_0, (o.alerted_at - p.added_at) closed_guess
               FROM probation p LEFT JOIN outcomes o
                 ON o.chain = p.chain AND o.address = p.address
               WHERE p.state='promoted'
               ORDER BY p.added_at DESC LIMIT ?""",
            (RECENT_FINDS,),
        )
    ]

    alerts = {
        r["alert_type"]: r["n"]
        for r in conn.execute("SELECT alert_type, COUNT(*) n FROM alerts_sent GROUP BY alert_type")
    }

    stats = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_unix": int(now),
        "period": {
            "start": datetime.fromtimestamp(first_seen).strftime("%Y-%m-%d"),
            "days": round((now - first_seen) / 86400),
        },
        "totals": {
            "seen": total,
            "base": chains.get("base", 0),
            "robinhood": chains.get("robinhood", 0),
            "below_liquidity_threshold": verdicts.get("skipped_low_liquidity", 0),
            "spam_filtered": verdicts.get("spam", 0),
            "scored": scored,
            "green": verdicts.get("green", 0),
            "yellow": verdicts.get("yellow", 0),
            "red": verdicts.get("red", 0),
        },
        "liquidity_threshold_usd": config.MIN_LIQUIDITY_USD,
        "daily": daily,
        "recent_finds": finds,
        "confirmed": confirmed,
        "verdict_outcomes": verdict_outcomes,
        "probation": {
            "waiting": probation_states.get("pending", 0),
            "confirmed": probation_states.get("promoted", 0),
            "dropped": probation_states.get("rejected", 0),
        },
        "recent_drains": drains,
        "survivors": survivors,
        "alerts": alerts,
        "watchlist_active": one("SELECT COUNT(*) FROM watchlist WHERE active=1"),
        "price_snapshots": one("SELECT COUNT(*) FROM price_snapshots"),
        # Честная оговорка, которую страница показывает рядом с графиком:
        # счётчики по дням зависят от того, сколько сканер реально отработал.
        "caveat": "Daily counts reflect what the scanner saw, so they dip when it was rate-limited or offline.",
        "disclaimer": "Automated checks against public APIs. Not advice, not an endorsement - names can be copied and checks can be wrong. Contract checks find traps in the code; they cannot tell you whether a token is worth holding. Verify anything yourself before touching it.",
    }
    conn.close()
    return stats


# Бот запускается через pythonw, у которого своей консоли нет, поэтому каждый
# вызов git открывал бы новое чёрное окно на долю секунды - раз в час, а при
# коммите и пуше по четыре подряд. CREATE_NO_WINDOW убирает мигание.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd, cwd):
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=_NO_WINDOW,
    )


def publish(site_repo: Path, stats: dict, push: bool = True) -> bool:
    """Кладёт stats.json в клон base-tools и пушит. True, если что-то изменилось."""
    target = site_repo / "scout-stats.json"
    payload = json.dumps(stats, ensure_ascii=False, indent=1)

    if push:
        # Тянуть надо ДО записи файла: git rebase отказывается работать при
        # незакоммиченных изменениях, а свежий снимок - ровно такое изменение.
        pull = _run(["git", "pull", "--rebase", "--quiet", "origin", "main"], site_repo)
        if pull.returncode != 0:
            log.error("git pull failed: %s", pull.stderr.strip())
            return False

    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        # generated_at меняется всегда - сравниваем всё остальное, иначе
        # получим пустой коммит каждый запуск.
        if {k: v for k, v in old.items() if not k.startswith("generated_at")} == {
            k: v for k, v in stats.items() if not k.startswith("generated_at")
        }:
            log.info("Stats unchanged, skipping commit")
            return False

    target.write_text(payload, encoding="utf-8")
    if not push:
        return True

    _run(["git", "add", "scout-stats.json"], site_repo)
    msg = f"Refresh meme-scout stats snapshot ({stats['totals']['seen']} tokens seen)"
    commit = _run(["git", "commit", "-m", msg], site_repo)
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
        log.error("git commit failed: %s", commit.stdout.strip() or commit.stderr.strip())
        return False

    pushed = _run(["git", "push", "--quiet", "origin", "main"], site_repo)
    if pushed.returncode != 0:
        log.error("git push failed: %s", pushed.stderr.strip())
        return False

    log.info("Published stats snapshot to %s", site_repo.name)
    return True


def main():
    parser = argparse.ArgumentParser(description="Экспорт публичной статистики meme-scout")
    parser.add_argument("--site-repo", default=str(Path(config.BASE_DIR).parent / "base-tools"),
                        help="путь к локальному клону base-tools")
    parser.add_argument("--no-push", action="store_true", help="только записать файл, не пушить")
    parser.add_argument("--print", action="store_true", help="вывести JSON в консоль")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    stats = build_stats()

    if args.print:
        print(json.dumps(stats, ensure_ascii=False, indent=1)[:2000])

    repo = Path(args.site_repo)
    if not repo.exists():
        raise SystemExit(f"Нет клона сайта: {repo}. Склонируй base-tools туда.")
    changed = publish(repo, stats, push=not args.no_push)
    print("обновлено" if changed else "без изменений")


if __name__ == "__main__":
    main()
