"""/check: paste a token address, get a dossier and a plain verdict.

The bot stopped hunting new coins on 23.09.2026; this is what it does instead.
Standard checks (contract traps, sell simulation, liquidity, holders) are the
same ones every scanner runs. What they don't show, and this does:

- the deployer's record: what else this wallet launched and how much of it is
  still alive, and where the wallet's first money came from;
- holder clusters: top holders that were funded by the same address;
- real demand: distinct buyers, not just volume, which is cheap to fake;
- the exit: what selling $500 and $5,000 actually returns right now;
- a project read (product, development, backers, partners) from ai.py, which
  searches the web, when a Gemini key is configured.

The verdict never says "buy". Our own audit showed that a clean contract does
not predict a coin surviving, so the best it says is "worth a look".

Run from the shell for testing:  python checker.py <address>
"""
import asyncio
import calendar
import html
import re
import time

import httpx

UA = {"User-Agent": "Mozilla/5.0 (meme-scout checker)"}
BLOCKSCOUT = {"base": "https://base.blockscout.com", "robinhood": "https://robinhoodchain.blockscout.com"}
CHAIN_ID = {"base": 8453, "robinhood": 4663}
GECKO_NET = {"base": "base", "robinhood": "robinhood"}
USDC = {"base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
BURN = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}
ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")

# Measured 23.09.2026 on 14,000 Base coins the bot had seen since 05.09: about
# one in fifty was still trading with real liquidity two weeks later.
BASE_RATE_NOTE = "Из новых монет на Base через две недели живёт примерно 1 из 50 (наш замер 23.09)."


# ---------------------------------------------------------------- fetching

# Blockscout without a key allows roughly five calls a second; faster than
# that it answers "too many requests" and the whole check slows down.
_BS_GATES: dict = {}

# Blockscout answers a busy token (VIRTUAL, 1.2M holders) at ~7 s a call, so
# the slow parts of a check run on a time budget and report what they skipped.
DEPLOYER_BUDGET_S = 35
HOLDERS_BUDGET_S = 30


def _bs_gate():
    loop = asyncio.get_running_loop()
    if loop not in _BS_GATES:
        _BS_GATES[loop] = asyncio.Semaphore(2)
    return _BS_GATES[loop]


async def _get(client, url, params=None, tries=4):
    gate = _bs_gate() if "blockscout" in url else None
    for i in range(tries):
        try:
            if gate:
                async with gate:
                    r = await client.get(url, params=params, headers=UA)
                    await asyncio.sleep(0.35)
            else:
                r = await client.get(url, params=params, headers=UA)
            if r.status_code == 429:
                await asyncio.sleep(2 + 3 * i)
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            # Blockscout's etherscan-style API says "too many requests" with a 200.
            if isinstance(data, dict) and "Too many requests" in str(data.get("message", "")):
                await asyncio.sleep(2 + 3 * i)
                continue
            return data
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(1 + i)
    return None


async def detect_chain(client, address):
    data = await _get(client, f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    pairs = (data or {}).get("pairs") or []
    chains = {}
    for p in pairs:
        chains[p.get("chainId")] = chains.get(p.get("chainId"), 0) + ((p.get("liquidity") or {}).get("usd") or 0)
    if not chains:
        return None, []
    chain = max(chains, key=chains.get)
    return chain, [p for p in pairs if p.get("chainId") == chain]


async def goplus_raw(client, chain, address):
    cid = CHAIN_ID.get(chain)
    if not cid:
        return None
    data = await _get(client, f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                      {"contract_addresses": address})
    return ((data or {}).get("result") or {}).get(address.lower())


async def honeypot_sim(client, chain, address):
    cid = CHAIN_ID.get(chain)
    if not cid:
        return None
    return await _get(client, "https://api.honeypot.is/v2/IsHoneypot", {"address": address, "chainID": cid})


async def gecko_pools(client, chain, address):
    net = GECKO_NET.get(chain)
    if not net:
        return []
    data = await _get(client, f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{address}/pools")
    return (data or {}).get("data") or []


async def bs(client, chain, path, params=None):
    base = BLOCKSCOUT.get(chain)
    return await _get(client, f"{base}{path}", params) if base else None


async def address_txs(client, chain, address, max_pages=6):
    """A wallet's transactions, newest first, and whether we reached its first one.

    Blockscout's etherscan-style endpoint can sort oldest-first, but it is
    rate-limited hard and stalls for half a minute when tripped (seen
    23.09.2026), so this pages the v2 endpoint back instead.
    """
    items, params = [], None
    for _ in range(max_pages):
        data = await bs(client, chain, f"/api/v2/addresses/{address}/transactions", params)
        if not data:
            return items, False
        items += data.get("items") or []
        params = data.get("next_page_params")
        if not params:
            return items, True
    return items, False


# A wallet holding this much native coin is an exchange or a bridge, not an
# insider. Seen 23.09.2026: an unlabeled wallet with 35,000 ETH "funded" two
# VIRTUAL whales, and a honeypot's dev sent it 13% of his supply for show.
EXCHANGE_ETH = 500


async def wallet_profile(client, chain, address, cache={}):
    key = (chain, address)
    if key not in cache:
        d = await bs(client, chain, f"/api/v2/addresses/{address}") or {}
        tags = [t.get("display_name") for t in (d.get("metadata") or {}).get("tags", []) if t.get("display_name")]
        try:
            eth = int(d.get("coin_balance") or 0) / 1e18
        except (TypeError, ValueError):
            eth = 0
        cache[key] = {"name": d.get("name") or (tags[0] if tags else None), "eth": eth,
                      "exchange": eth >= EXCHANGE_ETH or any("exchange" in t.lower() for t in tags)}
    return cache[key]


def _ts(iso):
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


async def funder_of(client, chain, address, txs=None, complete=None, max_pages=6, before=None):
    """First address that sent this wallet real money (not dust), optionally before a moment."""
    if txs is None:
        txs, complete = await address_txs(client, chain, address, max_pages)
    if not complete:
        return None, None, None
    for t in reversed(txs):
        if int(t.get("value") or 0) < 5 * 10 ** 14:  # under 0.0005 ETH is dust or address poisoning
            continue
        if before and (_ts(t.get("timestamp")) or 0) > before:
            continue
        to = ((t.get("to") or {}).get("hash") or "").lower()
        frm = ((t.get("from") or {}).get("hash") or "").lower()
        if to == address.lower() and frm != address.lower() and int(t.get("value") or 0) > 0:
            return (t.get("from") or {}).get("hash", "").lower(), _ts(t.get("timestamp")), int(t["value"]) / 1e18
    return None, None, None


async def kyber_sell(client, chain, address, amount_raw):
    if chain not in USDC or amount_raw <= 0:
        return None
    data = await _get(client, f"https://aggregator-api.kyberswap.com/{chain}/api/v1/routes",
                      {"tokenIn": address, "tokenOut": USDC[chain], "amountIn": str(int(amount_raw))})
    rs = ((data or {}).get("data") or {}).get("routeSummary") or {}
    try:
        return float(rs.get("amountInUsd") or 0), float(rs.get("amountOutUsd") or 0)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- analysis

async def deployer_record(client, chain, token, creation_tx):
    """Who launched it, what else they launched, and where their money came from."""
    out = {"dev": None, "factory": None, "launches": [], "funder": None, "funder_label": None,
           "funded_before_deploy_min": None, "deploy_ts": None}
    tx = await bs(client, chain, f"/api/v2/transactions/{creation_tx}") if creation_tx else None
    if not tx:
        return out
    dev = (tx.get("from") or {}).get("hash", "").lower()
    to = tx.get("to") or {}
    out["dev"] = dev
    try:
        out["deploy_ts"] = calendar.timegm(time.strptime(tx["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        pass
    if to and to.get("hash"):
        out["factory"] = to.get("name") or to.get("hash")

    txs, complete = await address_txs(client, chain, dev)
    created = [t["created_contract"]["hash"].lower() for t in txs if t.get("created_contract")]
    if to and to.get("hash"):
        same_factory = [t["hash"] for t in txs if ((t.get("to") or {}).get("hash") or "").lower()
                        == to["hash"].lower() and t.get("status") == "ok"][:10]
        for h in same_factory:
            internal = await bs(client, chain, f"/api/v2/transactions/{h}/internal-transactions")
            for it in (internal or {}).get("items") or []:
                if it.get("created_contract"):
                    created.append(it["created_contract"]["hash"].lower())
    created = [c for c in dict.fromkeys(created) if c != token.lower()]

    if created:
        launches = {}
        for i in range(0, min(len(created), 60), 30):
            data = await _get(client, f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(created[i:i + 30]))
            for p in data or []:
                a = (p.get("baseToken") or {}).get("address", "").lower()
                liq = (p.get("liquidity") or {}).get("usd") or 0
                o = launches.setdefault(a, {"symbol": (p.get("baseToken") or {}).get("symbol"), "liq": 0, "vol": 0})
                o["liq"] += liq
                o["vol"] += (p.get("volume") or {}).get("h24") or 0
        out["launches"] = [dict(address=a, **o) for a, o in launches.items()]
        out["contracts_created"] = len(created)

    out["dev_tx_count"] = len(txs) if complete else f"{len(txs)}+"
    funder, ts, amount = await funder_of(client, chain, dev, txs, complete, before=out["deploy_ts"])
    out["funder"] = funder
    out["funder_amount"] = amount
    if funder:
        prof = await wallet_profile(client, chain, funder)
        out["funder_label"] = prof["name"] or ("биржа или мост" if prof["exchange"] else None)
    if ts and out["deploy_ts"]:
        out["funded_before_deploy_min"] = (out["deploy_ts"] - ts) / 60
    return out


async def holder_map(client, chain, token, total_supply, dev, lp_addrs):
    """Top holders, what share is outside pools and burns, and who funded them."""
    data = await bs(client, chain, f"/api/v2/tokens/{token}/holders")
    items = (data or {}).get("items") or []
    rows = []
    for h in items[:20]:
        a = h.get("address") or {}
        addr = (a.get("hash") or "").lower()
        try:
            pct = float(h.get("value") or 0) / total_supply * 100 if total_supply else 0
        except (TypeError, ValueError):
            pct = 0
        kind = "burn" if addr in BURN else "pool" if addr in lp_addrs else "contract" if a.get("is_contract") else "wallet"
        rows.append({"address": addr, "pct": pct, "kind": kind, "name": a.get("name"), "is_dev": addr == dev})

    wallets = [r for r in rows if r["kind"] == "wallet"][:10]

    async def fund(r):
        # Two pages: insider wallets are fresh; an old busy wallet's first
        # funder says nothing about this coin anyway.
        r["funder"], _, _ = await funder_of(client, chain, r["address"], max_pages=2)
        # Where the coins themselves came from: bought from a pool, or handed
        # over by someone. The earliest transfer on the first page is enough.
        tt = await bs(client, chain, f"/api/v2/addresses/{r['address']}/token-transfers", {"token": token})
        items = (tt or {}).get("items") or []
        if items:
            src = ((items[-1].get("from") or {}).get("hash") or "").lower()
            r["got_from"] = "pool" if src in lp_addrs else src

    tasks = [asyncio.ensure_future(fund(r)) for r in wallets]
    done, pending = await asyncio.wait(tasks, timeout=HOLDERS_BUDGET_S) if tasks else (set(), set())
    for t in pending:
        t.cancel()
    unchecked = len(pending)
    from_dev = [r for r in wallets if dev and r.get("got_from") == dev]
    for r in from_dev:
        r["exchange"] = (await wallet_profile(client, chain, r["address"]))["exchange"]
    handed = {}
    for r in wallets:
        g = r.get("got_from")
        if g and g not in ("pool", dev) and g not in BURN:
            handed.setdefault(g, []).append(r)
    groups = {}
    for r in wallets:
        if r.get("funder"):
            groups.setdefault(r["funder"], []).append(r)
    # Two small wallets fed by one exchange hot wallet mean nothing; a group
    # only counts when together it holds a real share.
    clusters = [(f, g) for f, g in groups.items() if len(g) >= 2 and sum(r["pct"] for r in g) >= 3]
    clusters.sort(key=lambda x: -sum(r["pct"] for r in x[1]))
    kept = []
    for f, g in clusters[:3]:
        if not (await wallet_profile(client, chain, f))["exchange"]:
            kept.append((f, g))
    clusters = kept
    dev_funded = [r for r in wallets if dev and r.get("funder") == dev]
    handed = [(f, g) for f, g in handed.items() if len(g) >= 2 and sum(r["pct"] for r in g) >= 3]
    return {"rows": rows, "clusters": clusters, "dev_funded": dev_funded, "from_dev": from_dev, "handed": handed,
            "unchecked": unchecked}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def collect(address: str) -> dict:
    """Everything we know about a token, gathered in parallel where possible."""
    address = address.lower()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        chain, pairs = await detect_chain(client, address)
        if not chain:
            return {"error": "Монета не найдена ни на одной бирже DexScreener. Проверь адрес."}
        if chain not in BLOCKSCOUT:
            return {"error": f"Монета торгуется в сети «{chain}». Пока проверяю только Base и Robinhood Chain."}

        gp, hp, pools, info, token_info = await asyncio.gather(
            goplus_raw(client, chain, address), honeypot_sim(client, chain, address),
            gecko_pools(client, chain, address), bs(client, chain, f"/api/v2/addresses/{address}"),
            bs(client, chain, f"/api/v2/tokens/{address}"))

        decimals = int((token_info or {}).get("decimals") or 18)
        total_supply = _num((token_info or {}).get("total_supply")) or 0
        lp_addrs = {(p.get("pairAddress") or "").lower() for p in pairs}
        for p in pools:
            lp_addrs.add(p["attributes"].get("address", "").lower())
        creation = (info or {}).get("creation_transaction_hash")
        tx = await bs(client, chain, f"/api/v2/transactions/{creation}") if creation else None
        dev = ((tx or {}).get("from") or {}).get("hash", "").lower() or None

        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        price = _num(best.get("priceUsd")) or 0

        async def sell(usd):
            return usd, (await kyber_sell(client, chain, address, usd / price * 10 ** decimals)) if price > 0 else None

        async def dev_part():
            try:
                return await asyncio.wait_for(deployer_record(client, chain, address, creation), DEPLOYER_BUDGET_S)
            except asyncio.TimeoutError:
                return {"dev": dev, "launches": [], "timeout": True}

        dev_info, holders, q500, q5000 = await asyncio.gather(
            dev_part(),
            holder_map(client, chain, address, total_supply, dev, lp_addrs),
            sell(500), sell(5000))
        exits = {usd: q for usd, q in (q500, q5000) if q and q[0] > 0}

    liq = sum((p.get("liquidity") or {}).get("usd") or 0 for p in pairs)
    vol = sum((p.get("volume") or {}).get("h24") or 0 for p in pairs)
    buyers = sum(((p["attributes"].get("transactions") or {}).get("h24") or {}).get("buyers") or 0 for p in pools)
    sellers = sum(((p["attributes"].get("transactions") or {}).get("h24") or {}).get("sellers") or 0 for p in pools)
    buys = sum(((p["attributes"].get("transactions") or {}).get("h24") or {}).get("buys") or 0 for p in pools)
    created_ms = min((p.get("pairCreatedAt") or 9e15) for p in pairs)
    info_block = best.get("info") or {}
    return {
        "chain": chain, "address": address,
        "symbol": (best.get("baseToken") or {}).get("symbol") or "?",
        "name": (best.get("baseToken") or {}).get("name") or "?",
        "price": price, "liq": liq, "vol": vol, "mcap": _num(best.get("marketCap") or best.get("fdv")) or 0,
        "age_h": (time.time() - created_ms / 1000) / 3600 if created_ms < 9e15 else None,
        "pools": len(pairs), "buyers": buyers, "sellers": sellers, "buys": buys,
        "change_h24": ((best.get("priceChange") or {}).get("h24")),
        "websites": [w.get("url") for w in info_block.get("websites") or []],
        "socials": [(s.get("type"), s.get("url")) for s in info_block.get("socials") or []],
        "verified": (info or {}).get("is_verified"),
        "holder_count": (token_info or {}).get("holders_count") or (gp or {}).get("holder_count"),
        "gp": gp or {}, "hp": hp or {}, "dev": dev_info, "holders": holders, "exits": exits,
        "url": best.get("url"),
    }


# ---------------------------------------------------------------- verdict

def assess(d: dict) -> tuple[str, list[str], list[str], list[str]]:
    """(verdict, stop reasons, warnings, good signs). Rules, not a model."""
    stop, warn, good = [], [], []
    gp, hp = d["gp"], d["hp"]
    flag = lambda k: str(gp.get(k)) == "1"

    hp_res = hp.get("honeypotResult") or {}
    sim = hp.get("simulationResult") or {}
    if hp_res.get("isHoneypot") or flag("is_honeypot"):
        stop.append("продать нельзя: это ловушка (honeypot)")
    sell_tax = _num(sim.get("sellTax"))
    if sell_tax is None and gp.get("sell_tax") not in (None, ""):
        sell_tax = (_num(gp.get("sell_tax")) or 0) * 100
    if sell_tax is not None and sell_tax > 10:
        stop.append(f"налог на продажу {sell_tax:.0f}%")
    elif sell_tax is not None and sell_tax > 3:
        warn.append(f"налог на продажу {sell_tax:.0f}%")
    owner = (gp.get("owner_address") or "").lower()
    owner_live = owner and owner not in BURN
    if flag("is_mintable") and owner_live:
        stop.append("владелец может допечатать монеты")
    if flag("can_take_back_ownership"):
        stop.append("владелец может вернуть себе контроль")
    if flag("hidden_owner"):
        warn.append("у контракта есть скрытые права управления (роли вне владельца)")
        if flag("owner_change_balance"):
            warn.append("кто-то с правами может менять балансы")
        if flag("is_mintable"):
            warn.append("кто-то с правами может допечатать монеты")
    if flag("is_blacklisted") and owner_live:
        warn.append("владелец может заблокировать кошелёк")
    if flag("slippage_modifiable") and owner_live:
        stop.append("владелец может поменять налог")
    if flag("cannot_sell_all"):
        stop.append("нельзя продать всё сразу")
    if flag("is_proxy"):
        warn.append("контракт можно подменить (прокси)")
    if d.get("verified") is False:
        warn.append("код контракта не опубликован")

    if d["liq"] < 5000:
        stop.append(f"ликвидность всего ${d['liq']:,.0f}")
    honeypot = any(s.startswith("продать нельзя") for s in stop)
    loss500 = None
    if 500 in d["exits"] and not honeypot:
        i, o = d["exits"][500]
        loss500 = (1 - o / i) * 100
        if loss500 > 15:
            stop.append(f"при продаже на $500 теряешь {loss500:.0f}%")
        elif loss500 > 5:
            warn.append(f"при продаже на $500 теряешь {loss500:.0f}%")
        else:
            good.append(f"$500 продаются почти без потерь ({loss500:.1f}%)")

    h = d["holders"]
    wallets = [r for r in h["rows"] if r["kind"] == "wallet"]
    top10 = sum(r["pct"] for r in wallets[:10])
    if top10 > 50:
        stop.append(f"10 крупнейших кошельков держат {top10:.0f}% монет")
    elif top10 > 30:
        warn.append(f"10 крупнейших кошельков держат {top10:.0f}%")
    dev_row = next((r for r in h["rows"] if r["is_dev"]), None)
    if dev_row and dev_row["pct"] > 5:
        (stop if dev_row["pct"] > 15 else warn).append(f"у автора {dev_row['pct']:.0f}% монет")
    if h["clusters"]:
        f, g = h["clusters"][0]
        share = sum(r["pct"] for r in g)
        text = f"кошельков, получивших деньги с одного адреса: {len(g)}, вместе {share:.0f}% монет"
        (stop if share > 20 else warn).append(text)
    to_exchange = [r for r in h["from_dev"] if r.get("exchange")]
    if to_exchange:
        warn.append(f"автор сам отправил {sum(r['pct'] for r in to_exchange):.0f}% монет на кошельки бирж — "
                    "так делают, чтобы держатели выглядели солиднее")
    if h["from_dev"]:
        share = sum(r["pct"] for r in h["from_dev"])
        text = f"кошельков, получивших монеты прямо от автора: {len(h['from_dev'])}, вместе {share:.0f}% монет"
        (stop if share > 15 else warn).append(text)
    for f, g in h["handed"][:1]:
        warn.append(f"кошельков, получивших монеты с одного адреса: {len(g)}, вместе {sum(r['pct'] for r in g):.0f}%")
    if h["dev_funded"]:
        warn.append(f"автор сам профинансировал {len(h['dev_funded'])} из крупных держателей")

    dv = d["dev"]
    launches = dv.get("launches") or []
    if launches:
        dead = sum(1 for l in launches if l["liq"] < 1000)
        if len(launches) >= 3 and dead / len(launches) >= 0.8:
            stop.append(f"автор запускал {len(launches)} монет, мертвы {dead}")
        elif dead:
            warn.append(f"автор запускал {len(launches)} монет, мертвы {dead}")
    if dv.get("funded_before_deploy_min") is not None and dv["funded_before_deploy_min"] < 60 \
            and not dv.get("funder_label") and (dv.get("funder_amount") or 0) < 0.05:
        warn.append(f"кошелёк автора получил деньги за {dv['funded_before_deploy_min']:.0f} мин до запуска — одноразовый")

    if d["buyers"] >= 300:
        good.append(f"{d['buyers']} разных покупателей за сутки")
    elif d["buyers"] < 50:
        warn.append(f"всего {d['buyers']} разных покупателей за сутки")
    if d["liq"] and d["vol"] / d["liq"] > 20:
        warn.append("объём в 20+ раз больше ликвидности — похоже на накрутку")
    if d["buyers"] and d["buys"] / d["buyers"] > 8:
        warn.append(f"в среднем {d['buys'] / d['buyers']:.0f} покупок на кошелёк — похоже на ботов")
    if d["age_h"] is not None and d["age_h"] < 24:
        warn.append(f"монете {d['age_h']:.0f} ч")

    if stop:
        verdict = "⛔ НЕ БРАТЬ"
    elif len(warn) >= 3:
        verdict = "⚠️ ОСТОРОЖНО"
    else:
        verdict = "🟢 МОЖНО СМОТРЕТЬ"
    return verdict, stop, warn, good


# ---------------------------------------------------------------- report

def _usd(x):
    if x is None:
        return "?"
    if x >= 1e9:
        return f"${x / 1e9:.1f} млрд"
    if x >= 1e6:
        return f"${x / 1e6:.1f} млн"
    if x >= 1e3:
        return f"${x / 1e3:.0f} тыс."
    return f"${x:,.0f}"


def _short(a):
    return f"{a[:6]}…{a[-4:]}" if a else "?"


def render(d: dict, project: str | None = None) -> str:
    if d.get("error"):
        return d["error"]
    e = html.escape
    verdict, stop, warn, good = assess(d)
    L = [f"<b>{e(d['symbol'])}</b> · {e(d['name'])} · {'Base' if d['chain'] == 'base' else 'Robinhood Chain'}",
         f"<code>{d['address']}</code>", "", f"<b>{verdict}</b>"]
    for s in stop:
        L.append(f"⛔ {e(s)}")
    for s in warn:
        L.append(f"⚠️ {e(s)}")
    for s in good:
        L.append(f"✅ {e(s)}")

    age = f"{d['age_h'] / 24:.0f} дн." if d["age_h"] and d["age_h"] >= 48 else f"{d['age_h'] or 0:.0f} ч"
    L += ["", "<b>Рынок</b>",
          f"Капа {_usd(d['mcap'])} · ликвидность {_usd(d['liq'])} · объём за сутки {_usd(d['vol'])}",
          f"Возраст {age} · пулов {d['pools']} · держателей {d['holder_count'] or '?'}",
          f"За сутки: {d['buyers']} покупателей, {d['sellers']} продавцов"]
    if d["exits"] and any(s.startswith("продать нельзя") for s in stop):
        L.append("Маршрут обмена обещает выкуп, но симуляция продажи падает — этим цифрам не верь")
    elif d["exits"]:
        parts = [f"${k:,} → ${o:,.0f}" for k, (i, o) in sorted(d["exits"].items())]
        L.append("Продать сейчас: " + " · ".join(parts))

    dv = d["dev"]
    L += ["", "<b>Кто запустил</b>"]
    if dv.get("dev"):
        via = f" через {e(str(dv['factory']))}" if dv.get("factory") and not str(dv["factory"]).startswith("0x") else ""
        L.append(f"Автор <code>{_short(dv['dev'])}</code>{via}")
        if dv.get("funder"):
            label = f" ({e(dv['funder_label'])})" if dv.get("funder_label") else ""
            amt = f" {dv['funder_amount']:.4f} ETH" if dv.get("funder_amount") else ""
            when = ""
            if dv.get("funded_before_deploy_min") is not None:
                m = dv["funded_before_deploy_min"]
                when = (f", за {m:.0f} мин до запуска" if m < 120 else
                        f", за {m / 60:.0f} ч до запуска" if m < 2880 else f", за {m / 1440:.0f} дн. до запуска")
            L.append(f"Первые деньги: <code>{_short(dv['funder'])}</code>{label}{amt}{when}")
        launches = dv.get("launches") or []
        if dv.get("timeout"):
            L.append("<i>Историю автора не успел проверить — обозреватель блоков отвечает медленно</i>")
        elif launches:
            alive = [l for l in launches if l["liq"] >= 1000]
            names = ", ".join(f"{e(str(l['symbol']))} {_usd(l['liq'])}" for l in sorted(alive, key=lambda x: -x["liq"])[:4])
            L.append(f"Другие его монеты: {len(launches)}, живы {len(alive)}" + (f" ({names})" if names else ""))
        elif dv.get("contracts_created") is not None:
            L.append("Других торгуемых монет у автора не найдено")
        else:
            L.append("Других запусков с этого кошелька не видно")
    else:
        L.append("Автора определить не удалось")

    h = d["holders"]
    L += ["", "<b>Держатели</b>"]
    for r in h["rows"][:6]:
        tag = {"pool": "пул", "burn": "сожжено", "contract": "контракт"}.get(r["kind"], "")
        if r["is_dev"]:
            tag = "автор"
        elif r.get("got_from") and r["got_from"] == d["dev"].get("dev"):
            tag = "биржа, получила от автора" if r.get("exchange") else "получил от автора"
        elif r.get("got_from") == "pool":
            tag = "купил"
        if r["kind"] == "contract" and r.get("name"):
            tag = f"контракт {e(str(r['name'])[:30])}"
        L.append(f"{r['pct']:.1f}% <code>{_short(r['address'])}</code> {tag}")
    if h.get("unchecked"):
        L.append(f"<i>Не успел проверить связи кошельков: {h['unchecked']}</i>")
    for f, g in h["clusters"][:2]:
        L.append(f"🔗 кошельков с деньгами от <code>{_short(f)}</code>: {len(g)}, вместе {sum(r['pct'] for r in g):.0f}%")

    if d["websites"] or d["socials"]:
        links = [f'<a href="{e(u)}">сайт</a>' for u in d["websites"][:1]]
        links += [f'<a href="{e(u)}">{e(t)}</a>' for t, u in d["socials"][:3]]
        L += ["", "Ссылки: " + " · ".join(links)]
    else:
        L += ["", "У монеты нет ни сайта, ни соцсетей в DexScreener"]

    if project:
        L += ["", "<b>Проект</b>", project]
    L += ["", f"<i>{e(BASE_RATE_NOTE)} Это не совет покупать — это проверка рисков.</i>"]
    if d.get("url"):
        L.append(f'<a href="{e(d["url"])}">График</a> · <a href="https://basescan.org/token/{d["address"]}">Basescan</a>')
    return "\n".join(L)


async def check(address: str) -> str:
    d = await collect(address)
    project = None
    if not d.get("error"):
        try:
            import ai
            project = await ai.project_read(d, assess(d))
        except Exception as exc:  # the on-chain part must survive any AI failure
            project = f"<i>ИИ-разбор не получился: {html.escape(str(exc)[:120])}</i>"
    return render(d, project)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    t = time.time()
    print(asyncio.run(check(sys.argv[1])))
    print(f"\n[{time.time() - t:.1f} s]")
