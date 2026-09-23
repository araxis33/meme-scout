"""Forward test of /check: record today's verdicts on live young Base coins,
then re-run with --recheck in 2-3 days and compare with who is still alive.

The only honest way to measure a young-coin verdict: nobody knows the answer
on the day, so we write it down first and look later.
"""
import asyncio, json, sys, time, calendar, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker

OUT = Path(__file__).resolve().parent / "forward-2026-09-23.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def get(u):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30))
        except Exception:
            time.sleep(10 + 10 * i)
    return {}

def candidates(n=20):
    seen, out = set(), []
    for page in (1, 2, 3):
        for path in (f"trending_pools?duration=24h&page={page}", f"pools?sort=h24_volume_usd_desc&page={page}"):
            for p in get(f"https://api.geckoterminal.com/api/v2/networks/base/{path}").get("data", []):
                a = p["attributes"]
                tok = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1].lower()
                ca = a.get("pool_created_at")
                if not ca or tok in seen:
                    continue
                age_d = (time.time() - calendar.timegm(time.strptime(ca[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
                liq = float(a.get("reserve_in_usd") or 0)
                if 1 <= age_d <= 10 and liq >= 20000:
                    seen.add(tok)
                    out.append({"address": tok, "name": a["name"], "age_d": round(age_d, 1), "liq0": round(liq)})
            time.sleep(2.5)
    return out[:n]

async def record():
    rows = []
    for c in candidates():
        d = await checker.collect(c["address"])
        if d.get("error"):
            continue
        v, stop, warn, good = checker.assess(d)
        rows.append({**c, "symbol": d["symbol"], "verdict": v, "stop": stop, "warn": warn,
                     "liq_at_check": round(d["liq"]), "price_at_check": d["price"], "checked_at": time.time()})
        print(f"{d['symbol'][:12]:12s} {v:40s} liq ${d['liq']:,.0f}", flush=True)
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

def recheck():
    rows = json.loads(OUT.read_text(encoding="utf-8"))
    for r in rows:
        pairs = get(f"https://api.dexscreener.com/tokens/v1/base/{r['address']}") or []
        liq = sum((p.get("liquidity") or {}).get("usd") or 0 for p in pairs) if isinstance(pairs, list) else 0
        r["liq_now"] = round(liq)
        r["alive"] = liq >= 0.5 * r["liq_at_check"] and liq >= 10000
    by = {}
    for r in rows:
        k = r["verdict"].split()[0] + " " + r["verdict"].split()[1]
        by.setdefault(k, []).append(r["alive"])
    for k, v in by.items():
        print(f"{k:20s} coins {len(v):2d}  alive {sum(v):2d}  ({sum(v) / len(v) * 100:.0f}%)")
    OUT.with_name(OUT.stem + "-recheck.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

if __name__ == "__main__":
    recheck() if "--recheck" in sys.argv else asyncio.run(record())
