"""Project read for /check: product, development, backers, partners.

Gemini with Google Search grounding, on the free AI Studio key in
GEMINI_API_KEY. The model gets the on-chain facts checker.py already
measured and is told to add only what it can find a source for, so the
verdict it writes is anchored to numbers, not vibes. Without a key this
returns None and /check still answers with the on-chain part.
"""
import html
import json
import os

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: list[str] = []

PROMPT = """Ты аналитик криптопроектов. Проверь токен и дай НЕЗАВИСИМЫЙ вывод по-русски простым языком.

Токен: {symbol} ({name}), сеть {chain}, контракт {address}
Сайт и соцсети из DexScreener: {links}
Что уже измерено по блокчейну (это факты, не пересказывай их целиком, опирайся на них):
{facts}
Правила проверки уже вынесли вердикт: {verdict}

Найди в интернете (сайт проекта, X/Twitter, GitHub, новости, CoinGecko, DexScreener) и ответь РОВНО пятью пунктами:
1. Продукт — есть ли работающий продукт или это просто монета/мем. Что именно работает.
2. Развитие — есть ли обновления, код, активность команды за последний месяц.
3. Кто стоит — команда (публичная или анонимная), инвесторы, спонсоры, фонды.
4. Партнёрства — коллаборации, интеграции, листинги. Только подтверждённые источником.
5. Вывод — брать, не брать или ждать, почему и при каком условии. 2–3 предложения. Учти измеренные факты выше.

Жёсткие правила: ничего не выдумывай. Если не нашёл — так и пиши «не найдено». Не путай с другими
проектами с таким же тикером: сверяй адрес контракта или ссылки. Без вступлений, без markdown, без звёздочек.
Каждый пункт — одна-две короткие строки. Всего не больше 1200 символов."""


def _key():
    return os.getenv("GEMINI_API_KEY", "").strip()


async def _models(client, key):
    if _model_cache:
        return _model_cache
    r = await client.get(f"{API}/models", params={"key": key, "pageSize": 200})
    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    skip = ("lite", "image", "tts", "live", "audio", "embedding", "thinking", "exp", "preview", "learnlm", "gemma")
    flash = [n for n in names if "flash" in n and not any(s in n for s in skip)]
    pro = [n for n in names if "pro" in n and not any(s in n for s in skip)]
    order = sorted(flash, reverse=True) + sorted(pro, reverse=True)
    _model_cache.extend(order or ["gemini-2.5-flash"])
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
    return "\n".join(lines), verdict


async def project_read(d: dict, assessment) -> str | None:
    key = _key()
    if not key:
        return None
    facts, verdict = _facts(d, assessment)
    links = ", ".join(d["websites"] + [u for _, u in d["socials"]]) or "нет"
    prompt = PROMPT.format(symbol=d["symbol"], name=d["name"], chain=d["chain"], address=d["address"],
                           links=links, facts=facts, verdict=verdict)
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2}}
    async with httpx.AsyncClient(timeout=90) as client:
        last = ""
        for model in (await _models(client, key))[:4]:
            r = await client.post(f"{API}/models/{model}:generateContent", params={"key": key}, json=body)
            if r.status_code in (429, 503, 500, 404):
                last = f"{model}: {r.status_code}"
                continue
            if r.status_code != 200:
                return f"<i>ИИ-разбор не получился: {html.escape(r.text[:150])}</i>"
            cand = (r.json().get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
            if not text:
                last = f"{model}: пустой ответ"
                continue
            text = text.replace("**", "").replace("*", "")
            chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
            src = []
            for c in chunks:
                w = c.get("web") or {}
                if w.get("uri") and w.get("title") and w["title"] not in [t for t, _ in src]:
                    src.append((w["title"], w["uri"]))
            out = html.escape(text)
            if src:
                out += "\nИсточники: " + " · ".join(
                    f'<a href="{html.escape(u)}">{html.escape(t[:30])}</a>' for t, u in src[:5])
            return out + f"\n<i>ИИ: {html.escape(model)}</i>"
    return f"<i>ИИ-разбор не получился: все модели заняты ({html.escape(last)})</i>"
