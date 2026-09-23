"""Project read for /check: product, development, backers, partners.

The free AI Studio key (GEMINI_API_KEY) does not include Google Search
grounding - every grounded call answers 429 "check your plan and billing"
(checked 23.09.2026 on 3.5-3.8 Flash and Flash-Lite). So the search is ours:
we gather the project's own site, its CoinGecko card, GitHub activity and a
few web searches, number the sources, and the model only reads them. That
keeps it free, and every claim has to point at a numbered source.

Stale and wrong-project information is the main risk, so: the model is told
today's date and must mark anything undated or older than three months; web
results that don't mention the project's name, site or contract are dropped
before the model sees them (tickers are reused all the time).

When every Gemini model is busy (the free tier answers 503 "high demand" for
hours), the same prompt goes to Groq (GROQ_API_KEY, also free): gpt-oss-120b
translated most accurately of the models on that key in a 23.09.2026 test.

Without any key this returns None and /check still answers with the on-chain part.
"""
import asyncio
import html
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

API = "https://generativelanguage.googleapis.com/v1beta"
GROQ = "https://api.groq.com/openai/v1"
GROQ_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130 Safari/537.36"}
_model_cache: list[str] = []

PROMPT = """Ты независимый аналитик криптопроектов. Сегодня {today}.
Ниже — материалы, которые я собрал сам, пронумерованные [1], [2]… Опирайся ТОЛЬКО на них и на ончейн-факты.
Ничего не добавляй из своей памяти: она устарела, а мне нужна только проверяемая информация.

Токен: {symbol} ({name}), сеть {chain}, контракт {address}, сайт {site}
Ончейн-факты (измерены сегодня, это правда):
{facts}
Правила проверки уже вынесли вердикт: {verdict}

Материалы:
{materials}

Ответь по-русски простым языком РОВНО пятью пунктами, каждый — одна-две короткие строки, с номерами источников:
1. Продукт — есть ли работающий продукт или это просто монета/мем. Что именно работает.
2. Развитие — обновления, код, активность за последний месяц.
3. Кто стоит — команда (публичная или анонимная), инвесторы, спонсоры, фонды.
4. Партнёрства — коллаборации, интеграции, листинги.
5. Вывод — начни ровно с одного из трёх: «Не брать», «Ждать» или «Можно смотреть», дальше почему и при каком
   условии. Учти ончейн-факты: если они говорят «не брать», хорошие новости этого не отменяют.
   Вердикт правил отвечает на вопрос «ловушка ли это», а не «хороший ли проект». Права команды, крупные
   кошельки и незаблокированная ликвидность у зрелого большого проекта (годы торгов, крупные биржи, сотни тысяч
   держателей) — это нормальное управление, а не причина писать «Не брать». «Не брать» — только если есть
   настоящие признаки ловушки или мёртвого проекта.
   Никаких советов, сколько вкладывать, «небольшими суммами», «как ставку» — это не твоё дело.

Жёсткие правила:
- Каждое утверждение — с номером источника в скобках. Нет в материалах — пиши ровно «в собранных материалах
  не найдено». НИКОГДА не превращай «не нашёл» в «нет»: не пиши «команда не раскрыта», «инвесторов нет»,
  «аудита нет», если материал прямо этого не говорит. Отсутствие сведений не минус проекту и не довод в выводе.
- Даты: если у сведения нет даты — допиши «(дата неизвестна)»; если оно старше трёх месяцев от сегодня — «(устарело, <месяц год>)».
  Раунд инвестиций двухлетней давности — это история, а не свежая новость.
- Материал может быть про другой проект с таким же тикером. Если он не совпадает по сайту, адресу или описанию — не используй.
- Различай, КТО говорит. Сайт проекта о себе — это заявление проекта: пиши «по словам проекта».
  Посты пользователей (Binance Square, X, Reddit, Medium, форумы) — это не официальные новости:
  пиши «пост пользователя, не подтверждено». Листинг считай подтверждённым, только если источник — сама биржа
  или CoinGecko. Страницы обозревателя блоков (basescan) — это не новости и не развитие, про них не пиши.
- Без вступлений, без markdown, без звёздочек. Всего не больше 1300 символов."""


def _key():
    return os.getenv("GEMINI_API_KEY", "").strip()


def _groq_key():
    return os.getenv("GROQ_API_KEY", "").strip()


def _clean(t, n):
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()[:n]


async def _get(client, url, **kw):
    try:
        r = await client.get(url, headers=BROWSER, **kw)
        return r if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


SEARCH_STATE = {"blocked": False}


def _tavily_key():
    return os.getenv("TAVILY_API_KEY", "").strip()


async def web_search(client, query, n=6):
    """Tavily when a key is set (built for this, 1,000 free searches a month);
    otherwise DuckDuckGo's HTML page, which starts serving a captcha after a few
    dozen queries from one address (seen 23.09.2026) - then we say so."""
    key = _tavily_key()
    if key:
        try:
            r = await client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                                  json={"query": query, "max_results": n, "search_depth": "basic"})
            if r.status_code == 200:
                return [{"title": _clean(x.get("title") or "", 100), "url": x.get("url") or "",
                         "text": _clean(((x.get("published_date") or "")[:10] + " " + (x.get("content") or "")).strip(),
                                        400)}
                        for x in r.json().get("results") or [] if x.get("url")]
        except (httpx.HTTPError, ValueError):
            pass
    r = await _get(client, "https://html.duckduckgo.com/html/", params={"q": query})
    if not r:
        return []
    if "anomaly" in r.text[:20000].lower() and "result__a" not in r.text:
        SEARCH_STATE["blocked"] = True
        return []
    out = []
    for href, title, snip in re.findall(
            r'class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
        if "uddg=" in href:  # DuckDuckGo wraps links in a redirect
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        out.append({"title": _clean(title, 100), "url": href, "text": _clean(snip, 300)})
        if len(out) >= n:
            break
    return out


async def coingecko(client, chain, address):
    platform = {"base": "base"}.get(chain)
    if not platform:
        return None
    r = await _get(client, f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{address}")
    if not r:
        return None
    d = r.json()
    links = d.get("links") or {}
    return {
        "title": f"CoinGecko: {d.get('name')} (карточка обновлена {str(d.get('last_updated') or '?')[:10]})",
        "url": f"https://www.coingecko.com/en/coins/{d.get('id')}",
        "text": _clean(f"Категории: {', '.join((d.get('categories') or [])[:6])}. Дата запуска: {d.get('genesis_date') or '?'}. "
                       + ((d.get("description") or {}).get("en") or ""), 900),
        "repos": [u for u in (links.get("repos_url") or {}).get("github", []) if u],
        "homepage": [u for u in links.get("homepage") or [] if u],
    }


_LLAMA = {"at": 0, "protocols": []}


async def defillama(client, d, cg_name):
    """DefiLlama's protocol card: category, audits, description, TVL. Free and
    keyless (its funding-rounds list went paid in 2026, so investors aren't here).
    Matched by the project's X handle or site, never by ticker alone."""
    if time.time() - _LLAMA["at"] > 12 * 3600:
        r = await _get(client, "https://api.llama.fi/protocols")
        if r:
            _LLAMA.update(at=time.time(), protocols=r.json())
    handles = {u.rstrip("/").rsplit("/", 1)[-1].lower() for t, u in d["socials"] if t in ("twitter", "x") and u}
    hosts = {urlparse(u).netloc.lower().removeprefix("www.") for u in d["websites"] if u}
    names = {n.lower() for n in (d["name"], cg_name) if n}
    for p in _LLAMA["protocols"]:
        host = urlparse(p.get("url") or "").netloc.lower().removeprefix("www.").removeprefix("app.")
        if ((p.get("twitter") or "").lower() in handles or (host and any(host in h or h in host for h in hosts))
                or (p.get("name") or "").lower() in names):
            # Zero TVL and "0 audits" mean "not tracked" far more often than "none":
            # Virtuals is not a DeFi pool and the model read TVL $0 as a weakness.
            tvl = p.get("tvl") or 0
            audits = int(p.get("audits") or 0)
            extra = (f"TVL ${tvl:,.0f}. " if tvl > 0 else "") + (f"Аудитов по DefiLlama: {audits}. " if audits else "")
            return {"title": f"DefiLlama: {p.get('name')} (данные на сегодня)",
                    "url": f"https://defillama.com/protocol/{p.get('slug')}",
                    "text": f"Категория: {p.get('category')}. {extra}Сети: {', '.join((p.get('chains') or [])[:6])}. "
                            f"{p.get('description') or ''}"[:600]}
    return None


async def website(client, url):
    r = await _get(client, url, follow_redirects=True)
    if not r or "text/html" not in r.headers.get("content-type", ""):
        return None, []
    repos = sorted(set(re.findall(r"https?://github\.com/[\w.-]+(?:/[\w.-]+)?", r.text)))[:3]
    title = re.search(r"(?is)<title>(.*?)</title>", r.text)
    return {"title": f"Сайт проекта: {_clean(title.group(1), 80) if title else url} (открыт сегодня)", "url": url,
            "text": _clean(r.text, 1500)}, repos


async def github_activity(client, repo_url):
    m = re.match(r"https?://github\.com/([\w.-]+)(?:/([\w.-]+))?", repo_url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    api = "https://api.github.com"
    if not repo:  # an organisation: take its most recently pushed repo
        r = await _get(client, f"{api}/orgs/{owner}/repos", params={"sort": "pushed", "per_page": 1}) or \
            await _get(client, f"{api}/users/{owner}/repos", params={"sort": "pushed", "per_page": 1})
        if not r or not r.json():
            return None
        repo = r.json()[0]["name"]
    info = await _get(client, f"{api}/repos/{owner}/{repo}")
    if not info:
        return None
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400))
    commits = await _get(client, f"{api}/repos/{owner}/{repo}/commits", params={"since": since, "per_page": 100})
    j = info.json()
    n = len(commits.json()) if commits else "?"
    return {"title": f"GitHub {owner}/{repo} (проверено сегодня)", "url": f"https://github.com/{owner}/{repo}",
            "text": f"Звёзд {j.get('stargazers_count')}, последний пуш {j.get('pushed_at', '')[:10]}, "
                    f"коммитов за 30 дней: {n}{'+' if n == 100 else ''}. {j.get('description') or ''}"}


# Pages every token gets automatically: they prove nothing about the project, and
# a model reads "web3.binance.com/token/..." as "listed on Binance" (seen 23.09.2026).
AUTO_PAGES = ("dexscreener.com", "geckoterminal.com", "basescan.org", "etherscan.io", "blockscout.com",
              "web3.binance.com", "dextools.io", "birdeye.so", "defined.fi", "coinstats.app", "gmgn.ai")
# Anyone can post there; the model must not read it as an announcement.
USER_POSTS = ("binance.com/en/square", "binance.com/square", "x.com/", "twitter.com/", "reddit.com", "medium.com",
              "t.me/", "youtube.com", "tiktok.com", "facebook.com", "substack.com", "warpcast.com", "farcaster")


def _about_this_project(m, d, site_host):
    """Keep a web result only if it names this project, its site or its contract."""
    blob = f"{m['title']} {m['url']} {m['text']}".lower()
    if d["address"].lower() in blob or (site_host and site_host in blob):
        return True
    # A bare ticker matches half the internet; the project name must appear.
    for name in {d["name"].lower().strip(), (d.get("alt_name") or "").lower().strip()}:
        if len(name) >= 4 and name != d["symbol"].lower() and name in blob:
            return True
    return False


async def gather(d):
    """Numbered materials about the project, from its own links and the open web."""
    name, sym = d["name"], d["symbol"]
    site_url = (d["websites"] or [None])[0]
    site_host = urlparse(site_url).netloc.lower().removeprefix("www.") if site_url else None
    async with httpx.AsyncClient(timeout=15) as client:
        async def no_site():
            return None, []
        # Founders and backers were "not found" even for VIRTUAL (23.09.2026): the
        # general queries return exchanges and price pages. Ask for them directly,
        # and on the sites that track funding rounds.
        searches = [f'"{name}" {sym} crypto', f'"{name}" founders team co-founder',
                    f'"{name}" raised funding round investors',
                    f'{name} {sym} site:cryptorank.io OR site:rootdata.com OR site:messari.io',
                    f'"{name}" partnership integration', f'{d["address"]}']
        cg, (site, site_repos), *found = await asyncio.gather(
            coingecko(client, d["chain"], d["address"]),
            website(client, site_url) if site_url else no_site(),
            *(web_search(client, q) for q in searches))
        if not site and cg and cg["homepage"]:
            site, site_repos = await website(client, cg["homepage"][0])
            site_host = site_host or urlparse(cg["homepage"][0]).netloc.lower().removeprefix("www.")
        repos = (cg or {}).get("repos", []) + site_repos + [u for t, u in d["socials"] if "github" in (t or "")]
        cg_name = ((cg or {}).get("title") or "").removeprefix("CoinGecko: ").split(" (")[0]
        gh, llama = await asyncio.gather(
            github_activity(client, repos[0]) if repos else asyncio.sleep(0),
            defillama(client, d, cg_name))

    # Every query empty means the search itself is down (captcha), not that
    # the web knows nothing: the report has to say which.
    SEARCH_STATE["blocked"] = not any(found)
    # The coin's name differs between sources ("Virtual Protocol" on DexScreener,
    # "Virtuals Protocol" everywhere else) - match either.
    d = {**d, "alt_name": cg_name}
    materials = [m for m in (site, cg, gh, llama) if m]
    seen = {m["url"] for m in materials}
    dropped = 0
    for group in found:
        for m in group:
            if m["url"] in seen or any(b in m["url"] for b in AUTO_PAGES):
                continue
            seen.add(m["url"])
            if _about_this_project(m, d, site_host):
                if any(b in m["url"] for b in USER_POSTS):
                    m = {**m, "title": f"ПОСТ ПОЛЬЗОВАТЕЛЯ, не официальная новость: {m['title']}"}
                materials.append(m)
            else:
                dropped += 1
    return materials[:18], dropped


async def _models(client, key):
    if _model_cache:
        return _model_cache
    r = await client.get(f"{API}/models", params={"key": key, "pageSize": 200})
    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]

    # Newest numbered Flash first (gemini-3.8-flash > 3.7 ...), then Flash-Lite as a
    # fallback: the newest ones are often "high demand" (503) on the free tier.
    def numbered(suffix):
        v = {}
        for n in names:
            m = re.fullmatch(rf"gemini-(\d+(?:\.\d+)?)-{suffix}", n)
            if m:
                v[n] = float(m.group(1))
        return sorted(v, key=v.get, reverse=True)

    _model_cache.extend(numbered("flash")[:4] + numbered("flash-lite")[:2] or ["gemini-flash-latest"])
    return _model_cache


def _facts(d, assessment):
    verdict, stop, warn, good = assessment
    dv = d["dev"]
    launches = dv.get("launches") or []
    lines = [
        f"капа ${d['mcap']:,.0f}, ликвидность ${d['liq']:,.0f}, объём 24ч ${d['vol']:,.0f}, возраст {d['age_h'] or 0:.0f} ч",
        f"покупателей за сутки {d['buyers']}, продавцов {d['sellers']}",
    ]
    if launches:
        lines.append(f"автор запускал ещё {len(launches)} монет, живы {sum(1 for l in launches if l['liq'] >= 1000)}")
    lines += [f"стоп: {s}" for s in stop] + [f"риск: {s}" for s in warn] + [f"плюс: {s}" for s in good]
    lines += [f"права команды (справка, у зрелого проекта это не тревога): {s}" for s in d.get("team_rights") or []]
    return "\n".join(lines), verdict


async def _ask_gemini(client, key, prompt):
    """Newest Gemini that answers. Returns (text, model) or (None, why)."""
    if not key:
        return None, "нет ключа"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
    last = ""
    for model in await _models(client, key):
        try:
            r = await client.post(f"{API}/models/{model}:generateContent", headers={"x-goog-api-key": key}, json=body)
        except httpx.HTTPError as exc:
            last = f"{model}: {type(exc).__name__}"
            continue
        if r.status_code != 200:
            last = f"{model}: {r.status_code}"
            continue
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        if text:
            return text, model
        last = f"{model}: пустой ответ"
    return None, last


async def _ask_groq(client, key, prompt):
    """Same prompt on Groq. Returns (text, model) or (None, why)."""
    if not key:
        return None, "нет ключа"
    last = ""
    for model in GROQ_MODELS:
        body = {"model": model, "max_tokens": 2000, "temperature": 0.1,
                "messages": [{"role": "user", "content": prompt}]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "medium"
        try:
            r = await client.post(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        except httpx.HTTPError as exc:
            last = f"{model}: {type(exc).__name__}"
            continue
        if r.status_code != 200:
            last = f"{model}: {r.status_code}"
            if r.status_code in (401, 429):  # the key or its quota: other models won't help
                break
            continue
        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if text.strip():
            return text.strip(), model
        last = f"{model}: пустой ответ"
    return None, last


async def project_read(d: dict, assessment) -> str | None:
    key, groq_key = _key(), _groq_key()
    if not key and not groq_key:
        return None
    materials, dropped = await gather(d)
    if not materials:
        return "Про проект в открытых источниках ничего не нашлось — ни сайта, ни упоминаний."
    facts, verdict = _facts(d, assessment)
    mat = "\n".join(f"[{i}] {m['title']} — {m['url']}\n{m['text']}" for i, m in enumerate(materials, 1))
    prompt = PROMPT.format(today=time.strftime("%d.%m.%Y"), symbol=d["symbol"], name=d["name"], chain=d["chain"],
                           address=d["address"], site=(d["websites"] or ["нет"])[0], facts=facts, verdict=verdict,
                           materials=mat)
    async with httpx.AsyncClient(timeout=60) as client:
        text, model = await _ask_gemini(client, key, prompt)
        if not text:
            gemini_why = model
            text, model = await _ask_groq(client, groq_key, prompt)
            if not text:
                return (f"<i>ИИ-разбор не получился: Gemini — {html.escape(gemini_why)}; "
                        f"Groq — {html.escape(model)}</i>")
            model = f"{model} (Groq: Gemini был занят)" if key else model
    text = text.replace("**", "").replace("*", "")
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(materials)})
    out = html.escape(text)
    if cited:
        out += "\nИсточники: " + " · ".join(
            f'<a href="{html.escape(materials[i - 1]["url"])}">[{i}]</a>' for i in cited)
    note = f"ИИ: {model}, материалов {len(materials)}"
    if SEARCH_STATE["blocked"]:
        note += "; поиск в интернете временно недоступен — разбор только по сайту, CoinGecko, DefiLlama и GitHub"
    if dropped:
        note += f", отброшено про другие проекты: {dropped}"
    return out + f"\n<i>{html.escape(note)}</i>"
