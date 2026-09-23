"""Calibration sample for the 1-10 risk score.

The weights in checker.assess were set by hand and checked on 11 coins with a
known truth. For a score other people will rely on, "risk 7" has to mean
something measurable: roughly the share of similar coins that died within two
weeks. This records the score AND every factor behind it for a broad sample
of Base coins, so that after 7 and 14 days the weights can be refitted on what
actually happened.

  python validation/calibration_sample.py            # record (about 1.5 h)
  python validation/calibration_sample.py --recheck  # outcomes; run at +7 and +14 days

Sample is chosen by TRADING VOLUME, across ages - picking by liquidity (the
first forward test) mostly returned pools nobody trades.
"""
import asyncio
import calendar
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "calibration-2026-09-23.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
        except Exception:
            time.sleep(8 + 8 * i)
    return None


def candidates(per_band=40):
    """Coins with real trading, in three age bands: <14 days, 14-180 days, older."""
    pools = {}
    paths = [f"pools?sort=h24_volume_usd_desc&page={p}" for p in range(1, 11)]
    paths += [f"trending_pools?duration=24h&page={p}" for p in range(1, 6)]
    paths += [f"new_pools?page={p}" for p in range(1, 11)]
    for path in paths:
        for p in (get(f"https://api.geckoterminal.com/api/v2/networks/base/{path}") or {}).get("data", []):
            a = p["attributes"]
            tok = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1].lower()
            vol = float((a.get("volume_usd") or {}).get("h24") or 0)
            liq = float(a.get("reserve_in_usd") or 0)
            if vol < 5000 or liq < 5000 or tok in pools:
                continue
            ca = a.get("pool_created_at")
            age = (time.time() - calendar.timegm(time.strptime(ca[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400 if ca else 999
            pools[tok] = {"address": tok, "name": a.get("name"), "age_d": round(age, 1), "vol": round(vol), "liq": round(liq)}
        time.sleep(2.5)
    # Young coins seen by the bot in the last two weeks add the band scanners care about most.
    db = sqlite3.connect(f"file:{HERE.parent / 'meme_scout.sqlite3'}?mode=ro", uri=True)
    seen = [r[0].lower() for r in db.execute(
        "select address from tokens where chain='base' and first_seen > ? and verdict != 'spam'", (time.time() - 14 * 86400,))]
    for i in range(0, len(seen), 30):
        for p in get("https://api.dexscreener.com/tokens/v1/base/" + ",".join(seen[i:i + 30])) or []:
            a = (p.get("baseToken") or {}).get("address", "").lower()
            vol = (p.get("volume") or {}).get("h24") or 0
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if vol >= 5000 and liq >= 5000 and a not in pools and p.get("pairCreatedAt"):
                pools[a] = {"address": a, "name": (p.get("baseToken") or {}).get("symbol"),
                            "age_d": round((time.time() - p["pairCreatedAt"] / 1000) / 86400, 1),
                            "vol": round(vol), "liq": round(liq)}
        time.sleep(0.3)
    bands = {"young": [], "mid": [], "old": []}
    for c in sorted(pools.values(), key=lambda c: -c["vol"]):
        band = "young" if c["age_d"] < 14 else "mid" if c["age_d"] < 180 else "old"
        if len(bands[band]) < per_band:
            bands[band].append({**c, "band": band})
    return [c for b in bands.values() for c in b]


async def record():
    rows = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    done = {r["address"] for r in rows}
    for c in candidates():
        if c["address"] in done:
            continue
        try:
            d = await checker.collect(c["address"])
        except Exception as exc:  # one bad coin must not stop the sample
            print("skip", c["name"], exc, flush=True)
            continue
        if d.get("error"):
            continue
        checker.assess(d)
        r = d["risk"]
        rows.append({**c, "symbol": d["symbol"], "score": r["score"], "raw": round(r["raw"], 2), "tier": r["tier"],
                     "hard": r["hard"], "up": r["up"], "down": r["down"],
                     "liq_at_check": round(d["liq"]), "vol_at_check": round(d["vol"]), "price_at_check": d["price"],
                     "checked_at": time.time()})
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(rows):3d} {c['band']:5s} {d['symbol'][:12]:12s} risk {r['score']:2d}", flush=True)


def recheck():
    rows = json.loads(OUT.read_text(encoding="utf-8"))
    for r in rows:
        pairs = get(f"https://api.dexscreener.com/tokens/v1/base/{r['address']}") or []
        liq = sum((p.get("liquidity") or {}).get("usd") or 0 for p in pairs)
        vol = sum((p.get("volume") or {}).get("h24") or 0 for p in pairs)
        price = max((float(p.get("priceUsd") or 0) for p in pairs), default=0)
        # Died = liquidity pulled, trading stopped, or the price collapsed.
        r["dead"] = (liq < 0.2 * r["liq_at_check"] or vol < 1000
                     or (r["price_at_check"] and price < 0.2 * r["price_at_check"]))
        r["liq_now"], r["vol_now"] = round(liq), round(vol)
        time.sleep(0.3)
    by = {}
    for r in rows:
        by.setdefault(r["score"], []).append(r["dead"])
    print("risk  coins  died")
    for s in sorted(by):
        v = by[s]
        print(f"{s:4d}  {len(v):5d}  {sum(v) / len(v) * 100:4.0f}%")
    stamp = time.strftime("%Y-%m-%d")
    (HERE / f"calibration-recheck-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    recheck() if "--recheck" in sys.argv else asyncio.run(record())
