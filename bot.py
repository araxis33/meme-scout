"""Telegram bot: шлёт алерты, обрабатывает команды и кнопки.

Однопользовательский бот - отвечает только на config.TELEGRAM_CHAT_ID.

Алерты не уходят в чат напрямую: всё идёт через route_alert(), который
решает, пушить сейчас или отложить в дайджест-очередь (см. «тихий режим»).
"""
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db

log = logging.getLogger("meme-scout.bot")

EXPLORER_LINKS = {
    # Порядок важен: первым идёт то, что открывают чаще всего - график, а не
    # страница адреса в обозревателе блоков.
    "base": {
        "dexscreener": "https://dexscreener.com/base/{addr}",
        "geckoterminal": "https://www.geckoterminal.com/base/tokens/{addr}",
        "basescan": "https://basescan.org/address/{addr}",
    },
    "robinhood": {
        # DexScreener и GeckoTerminal проиндексировали Robinhood Chain уже
        # после того, как бот был написан, поэтому их тут не было. Сеть у
        # обоих называется 'robinhood'; GeckoTerminal - вообще тот самый
        # источник, из которого бот берёт цифры по этой сети.
        "dexscreener": "https://dexscreener.com/robinhood/{addr}",
        "geckoterminal": "https://www.geckoterminal.com/robinhood/tokens/{addr}",
        "blockscout": "https://robinhoodchain.blockscout.com/address/{addr}",
        "robinscan": "https://robinscan.io/address/{addr}",
    },
}

CHAIN_LABEL = {"base": "Base", "robinhood": "Robinhood Chain"}
CHAINS = ("base", "robinhood")

PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"

ALERT_TITLE = {
    "new_token": "новый токен",
    "pump": "памп",
    "dump": "дамп",
    "rug": "rug pull",
    "survivor": "выживший",
    "confirmed": "спрос подтверждён",
}


def _is_authorized(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(config.TELEGRAM_CHAT_ID)


def links_block(chain: str, address: str) -> str:
    parts = [f'<a href="{url.format(addr=address)}">{name}</a>' for name, url in EXPLORER_LINKS[chain].items()]
    return " | ".join(parts)


def _fmt_price(price) -> str:
    if not price:
        return "н/д"
    return f"${price:.8f}" if price < 1 else f"${price:,.4f}"


def _fmt_usd(value) -> str:
    return f"${value:,.0f}" if value else "н/д"


def _pct(new, old):
    """Изменение в процентах, None если посчитать не из чего."""
    if not old or new is None:
        return None
    return (new - old) / old * 100


def _fmt_pct(value) -> str:
    if value is None:
        return "н/д"
    return f"{value:+.0f}%"


# --- форматирование алертов ---------------------------------------------

def format_new_token_alert(chain: str, token: dict, score_result) -> str:
    addr = token["token_address"]
    symbol = html.escape(str(token.get("symbol", "?")))
    name = html.escape(str(token.get("name", "?")))
    limited_note = "\n⚠️ <i>Часть проверок недоступна (новая сеть) - оценка неполная</i>" if score_result.limited else ""
    reasons = "\n".join(f"  {r}" for r in score_result.reasons)
    return (
        f"{score_result.verdict_emoji} <b>Новый токен: {symbol}</b> ({name})\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Адрес: <code>{addr}</code>\n"
        f"Ликвидность: {_fmt_usd(token.get('liquidity_usd'))}\n"
        f"Score: <b>{score_result.score}/100</b>\n\n"
        f"{reasons}"
        f"{limited_note}\n\n"
        f"{links_block(chain, addr)}"
    )


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русские окончания: 1 проект, 2 проекта, 5 проектов."""
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def format_lookalike_warning(symbol: str, lookalikes: dict, window_days: float) -> str:
    """Предупреждение о токенах-двойниках. Пусто, если их нет."""
    if not lookalikes:
        return ""
    parts = []
    same_name = lookalikes.get("same_name") or 0
    same_ticker = lookalikes.get("same_ticker") or 0
    if same_name:
        raz = _plural(same_name, "раз", "раза", "раз")
        parts.append(f"точно такой же токен выходил ещё {same_name} {raz} с других адресов")
    if same_ticker:
        proj = _plural(same_ticker, "проект", "проекта", "проектов")
        drug = _plural(same_ticker, "другой", "других", "других")
        parts.append(f"тикер {html.escape(str(symbol))} за это же время использовали "
                     f"{same_ticker} {drug} {proj}")
    if not parts:
        return ""
    return ("\n⚠️ <b>Двойники за {d:.0f} дн.:</b> {body}.\n"
            "<i>Сверь адрес контракта - копии выглядят неотличимо.</i>\n").format(
        d=window_days, body="; ".join(parts))


def format_confirmed_token_alert(chain: str, address: str, symbol: str, name: str,
                                 pool: dict, cand: dict, result, age_hours: float,
                                 lookalikes: dict | None = None) -> str:
    """Главный сигнал бота: токен прожил час-другой и показал живую торговлю.

    Намеренно на первом месте стоят цифры спроса, а не score контракта:
    именно подмена одного другим и превращала выдачу в поток мусора.
    """
    liq_0 = cand.get("liq_0")
    liq_now = pool.get("liquidity_usd")
    reasons = "\n".join(f"  {r}" for r in result.reasons)
    safety = cand.get("score")
    safety_line = f"Проверка контракта: {safety}/100\n" if safety is not None else ""
    return (
        f"✅ <b>Спрос подтверждён: {html.escape(str(symbol))}</b> ({html.escape(str(name))})\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"<b>Сила сигнала: {result.score}/100</b>\n"
        f"{reasons}\n\n"
        f"Найден {age_hours:.1f} ч назад при ликвидности {_fmt_usd(liq_0)}, "
        f"сейчас {_fmt_usd(liq_now)}\n"
        f"{safety_line}"
        f"{format_lookalike_warning(symbol, lookalikes, config.LOOKALIKE_WINDOW_DAYS)}\n"
        f"<i>Токен не показывался сразу: он ждал на испытательном сроке, "
        f"пока рынок подтвердит интерес.</i>\n\n"
        f"{links_block(chain, address)}"
    )


def format_pump_alert(chain: str, address: str, symbol: str, pct_change: float, price: float) -> str:
    return (
        f"🚀 <b>Памп: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Изменение цены: +{pct_change:.0f}% за час\n"
        f"Текущая цена: {_fmt_price(price)}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"{links_block(chain, address)}"
    )


def format_dump_alert(chain: str, address: str, symbol: str, pct_change: float, price: float) -> str:
    return (
        f"📉 <b>Дамп: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Изменение цены: {pct_change:.0f}% за час\n"
        f"Текущая цена: {_fmt_price(price)}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"{links_block(chain, address)}"
    )


def format_rug_alert(chain: str, address: str, symbol: str, liq_before: float, liq_after: float) -> str:
    return (
        f"🚨 <b>ВОЗМОЖНЫЙ RUG PULL: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Ликвидность упала: ${liq_before:,.0f} → ${liq_after:,.0f}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"{links_block(chain, address)}"
    )


def format_survivor_alert(chain: str, address: str, symbol: str, outcome: dict, market: dict) -> str:
    """Токен прожил сутки и не сдулся - самый редкий и ценный сигнал бота."""
    liq_change = _pct(market.get("liquidity_usd"), outcome.get("liq_0"))
    price_change = _pct(market.get("price_usd"), outcome.get("price_0"))
    age_hours = (time.time() - outcome["alerted_at"]) / 3600
    score_line = f"Score при находке: {outcome['score']}/100\n" if outcome.get("score") is not None else ""
    return (
        f"🏆 <b>Выживший: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Возраст с момента находки: {age_hours:.0f} ч\n"
        f"{score_line}"
        f"Ликвидность: {_fmt_usd(outcome.get('liq_0'))} → {_fmt_usd(market.get('liquidity_usd'))} "
        f"({_fmt_pct(liq_change)})\n"
        f"Цена: {_fmt_price(outcome.get('price_0'))} → {_fmt_price(market.get('price_usd'))} "
        f"({_fmt_pct(price_change)})\n"
        f"Объём 24ч: {_fmt_usd(market.get('volume_24h'))}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"<i>Пережил сутки с растущей ликвидностью - таких единицы из тысяч.</i>\n\n"
        f"{links_block(chain, address)}"
    )


# --- кнопки --------------------------------------------------------------

def build_alert_keyboard(chain: str, address: str, alert_type: str) -> InlineKeyboardMarkup:
    """callback_data ограничен 64 байтами - хватает на 'код|сеть|адрес'."""
    def cb(action: str) -> str:
        return f"{action}|{chain}|{address}"

    rows = [
        [
            InlineKeyboardButton("📊 Подробнее", callback_data=cb("d")),
            InlineKeyboardButton("📈 График", callback_data=cb("c")),
        ]
    ]
    # Кнопка-ссылка: открывает график на DexScreener в один тап, без поиска
    # нужной ссылки в тексте. Работает для обеих сетей.
    dex_url = EXPLORER_LINKS.get(chain, {}).get("dexscreener")
    if dex_url:
        rows.append([InlineKeyboardButton("🔗 DexScreener", url=dex_url.format(addr=address))])
    if alert_type in ("new_token", "confirmed", "survivor", "pump"):
        rows.append([InlineKeyboardButton("💰 Купить", callback_data=cb("b"))])
    rows.append(
        [
            InlineKeyboardButton("👀 Следить", callback_data=cb("w")),
            InlineKeyboardButton("🚫 Игнор", callback_data=cb("i")),
        ]
    )
    return InlineKeyboardMarkup(rows)


# --- маршрутизация алертов ----------------------------------------------

def _is_muted(chain: str) -> bool:
    return db.get_state("muted") == "1" or db.get_state(f"muted_{chain}") == "1"


async def _send(application: Application, text: str, keyboard=None) -> bool:
    try:
        await application.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return True
    except Exception:
        log.exception("Failed to send Telegram alert")
        return False


async def send_alert(application: Application, text: str) -> bool:
    """Безусловная отправка (служебные сообщения: дайджест, ошибки)."""
    if db.get_state("muted") == "1":
        return False
    return await _send(application, text)


async def route_alert(application, *, chain: str, address: str, symbol: str, alert_type: str,
                       text: str, priority: str, summary: str) -> bool:
    """Пушит немедленно либо откладывает в дайджест - в зависимости от приоритета.

    Возвращает True, только если сообщение реально ушло в чат.
    """
    addr = (address or "").lower()
    if addr and db.is_ignored(chain, addr):
        return False

    if _is_muted(chain):
        db.enqueue_digest(chain, addr, alert_type, symbol, summary)
        db.log_alert_event(chain, addr, alert_type, symbol, pushed=False)
        return False

    if priority == PRIORITY_HIGH:
        keyboard = build_alert_keyboard(chain, addr, alert_type) if addr else None
        sent = await _send(application, text, keyboard)
        db.log_alert_event(chain, addr, alert_type, symbol, pushed=sent)
        return sent

    db.enqueue_digest(chain, addr, alert_type, symbol, summary)
    db.log_alert_event(chain, addr, alert_type, symbol, pushed=False)
    return False


# --- обработчики кнопок --------------------------------------------------

def _parse_cb(data: str):
    parts = (data or "").split("|")
    if len(parts) != 3 or parts[1] not in CHAINS:
        return None
    return parts[0], parts[1], parts[2]


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_authorized(update):
        await query.answer()
        return

    parsed = _parse_cb(query.data)
    if not parsed:
        await query.answer("Не понял кнопку")
        return
    action, chain, address = parsed

    if action == "w":
        db.add_to_watchlist_manual(chain, address)
        db.unignore_token(chain, address)
        await query.answer("Добавлено в watchlist")
        return

    if action == "i":
        db.ignore_token(chain, address)
        await query.answer("Больше не алертит по этому токену")
        return

    if action == "b":
        await query.answer()
        await _reply_buy(query, chain, address)
        return

    if action == "d":
        await query.answer("Собираю данные...")
        await _reply_details(query, chain, address)
        return

    if action == "c":
        await query.answer("Рисую график...")
        await _reply_chart(query, chain, address)
        return

    await query.answer()


async def _reply_buy(query, chain: str, address: str):
    token = db.get_token(chain, address) or {}
    symbol = html.escape(str(token.get("symbol") or "?"))
    if chain == "robinhood":
        text = (
            f"💰 <b>{symbol}</b> на Robinhood Chain\n\n"
            "Покупка через Claude тут <b>не работает</b> - Base MCP эту сеть не поддерживает. "
            "Придётся вручную из своего кошелька (сеть добавляется по RPC "
            "<code>rpc.mainnet.chain.robinhood.com</code>).\n\n"
            f"Адрес токена:\n<code>{address}</code>"
        )
    else:
        text = (
            f"💰 <b>Купить {symbol}</b> (Base)\n\n"
            "Скопируй это в чат с Claude, подставив сумму:\n\n"
            f"<code>купи на $50 токен {symbol} {address} (base)</code>\n\n"
            "Claude соберёт своп через Base MCP, а подтверждать сделку будешь "
            "сам в приложении Base Account."
        )
    await query.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _reply_details(query, chain: str, address: str):
    from sources import market

    token = db.get_token(chain, address) or {}
    outcome = db.get_outcome(chain, address)
    data = await market.get_market_data(chain, address)
    symbol = html.escape(str((data or {}).get("symbol") or token.get("symbol") or "?"))

    lines = [f"📊 <b>{symbol}</b> - {CHAIN_LABEL[chain]}", f"Адрес: <code>{address}</code>", ""]

    if not data:
        lines.append("Рыночных данных нет - пара, похоже, больше не индексируется.")
    else:
        lines += [
            f"Цена: {_fmt_price(data.get('price_usd'))}",
            f"Ликвидность: {_fmt_usd(data.get('liquidity_usd'))}",
            f"Объём 24ч: {_fmt_usd(data.get('volume_24h'))}",
            f"Капитализация: {_fmt_usd(data.get('market_cap_usd'))}",
        ]
        if outcome:
            age_h = (time.time() - outcome["alerted_at"]) / 3600
            lines += [
                "",
                f"С момента алерта ({age_h:.0f} ч назад):",
                f"  цена {_fmt_pct(_pct(data.get('price_usd'), outcome.get('price_0')))}, "
                f"ликвидность {_fmt_pct(_pct(data.get('liquidity_usd'), outcome.get('liq_0')))}",
            ]

    if token.get("score") is not None:
        lines.append(f"Score при находке: {token['score']}/100 ({token.get('verdict')})")
    if db.is_ignored(chain, address):
        lines.append("🚫 Токен в игноре")

    lines += ["", links_block(chain, address)]
    await query.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def _reply_chart(query, chain: str, address: str):
    import chart

    token = db.get_token(chain, address) or {}
    symbol = str(token.get("symbol") or "?")
    png, note = chart.render_token_chart(chain, address, symbol)
    if png is None:
        await query.message.reply_text(note)
        return
    await query.message.reply_photo(photo=png, caption=note)


# --- команды -------------------------------------------------------------

HELP_TEXT = (
    "Meme-Scout. Слежу за новыми токенами на Base и Robinhood Chain.\n"
    "Показываю только те, что доказали живую торговлю: новые находки сначала\n"
    "молча ждут на испытательном сроке и попадают в чат, лишь когда их\n"
    "реально начали покупать разные люди.\n"
    "⚠️ Для Robinhood Chain часть проверок недоступна - сеть совсем новая.\n\n"
    "Команды:\n"
    "/status - статус и размер watchlist\n"
    "/stats - табло точности: что стало с прошлыми алертами\n"
    "/digest - прислать дайджест прямо сейчас\n"
    "/chart &lt;base|robinhood&gt; &lt;адрес&gt; - график цены и ликвидности\n"
    "/watch &lt;base|robinhood&gt; &lt;адрес&gt; - добавить в watchlist\n"
    "/watchlist - показать watchlist\n"
    "/candidates - кто сейчас на испытательном сроке и чего ждёт\n"
    "/ignore &lt;base|robinhood&gt; &lt;адрес&gt; - больше не алертить\n"
    "/ignored - список игнора (и как убрать)\n"
    "/unignore &lt;base|robinhood&gt; &lt;адрес&gt; - вернуть из игнора\n"
    "/mute [base|robinhood] - выключить алерты (совсем или по одной сети)\n"
    "/unmute [base|robinhood] - включить обратно"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    muted_parts = []
    if db.get_state("muted") == "1":
        muted_parts.append("всё")
    for chain in CHAINS:
        if db.get_state(f"muted_{chain}") == "1":
            muted_parts.append(CHAIN_LABEL[chain])
    mute_line = "🔇 muted: " + ", ".join(muted_parts) if muted_parts else "🔔 активен"

    pending = len(db.get_pending_digest(limit=1000))
    day_ago = time.time() - 86400
    counts = db.count_alert_events(day_ago)
    pushed = sum(n for (_, p), n in counts.items() if p)
    queued = sum(n for (_, p), n in counts.items() if not p)

    await update.message.reply_text(
        f"Статус: {mute_line}\n"
        f"В watchlist: {db.watchlist_size()} токенов\n"
        f"За сутки: {pushed} пушей, {queued} в тихую очередь\n"
        f"Ждёт в дайджесте: {pending}\n"
        f"Дайджест в {config.DIGEST_HOUR:02d}:{config.DIGEST_MINUTE:02d}\n"
        f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    from watchers import digest

    await update.message.reply_text(
        digest.build_scoreboard(), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    from watchers import digest

    await digest.send_digest(context.application, manual=True)


def _parse_chain_address(args):
    if len(args) != 2 or args[0] not in CHAINS:
        return None
    address = args[1].strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return None
    return args[0], address


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    parsed = _parse_chain_address(context.args)
    if not parsed:
        await update.message.reply_text("Использование: /watch <base|robinhood> <адрес контракта>")
        return
    chain, address = parsed
    db.add_to_watchlist_manual(chain, address)
    db.unignore_token(chain, address)
    await update.message.reply_text(f"Добавлено в watchlist: {chain} {address}")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    watchlist = db.get_watchlist()
    if not watchlist:
        await update.message.reply_text("Watchlist пуст.")
        return
    manual = [w for w in watchlist if w.get("manual")]
    lines = [f"Всего активных: {len(watchlist)}"]
    if manual:
        lines.append("\nДобавлены вручную:")
        lines += [f"  {w['chain']}: {w['address']}" for w in manual[:30]]
    lines.append("\nПоследние автоматические:")
    auto = sorted((w for w in watchlist if not w.get("manual")), key=lambda w: w["added_at"], reverse=True)
    lines += [f"  {w['chain']}: {w['address']}" for w in auto[:20]]
    await update.message.reply_text("\n".join(lines))


async def cmd_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кто сейчас на испытательном сроке и почему ещё не показан."""
    if not _is_authorized(update):
        return
    pending = db.get_probation_pending(15)
    day = db.probation_counts(time.time() - 86400)
    header = (
        f"За сутки: взято на карандаш {sum(day.values())}, "
        f"подтвердили спрос {day.get('promoted', 0)}, "
        f"отсеяно {day.get('rejected', 0)}, "
        f"ещё думают {day.get('pending', 0)}"
    )
    if not pending:
        await update.message.reply_text(header + "\n\nСейчас в очереди никого.")
        return
    lines = [header, "", "В очереди (лучшие сверху):"]
    for c in pending:
        age = (time.time() - c["added_at"]) / 3600
        lines.append(
            f"  {c['symbol']} - {age:.1f} ч, покупателей максимум {c['best_buyers']}, "
            f"объём ${c['best_volume']:,.0f}"
        )
        if c.get("last_reason"):
            lines.append(f"     ждём: {c['last_reason']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    parsed = _parse_chain_address(context.args)
    if not parsed:
        await update.message.reply_text("Использование: /ignore <base|robinhood> <адрес контракта>")
        return
    chain, address = parsed
    db.ignore_token(chain, address)
    await update.message.reply_text(f"В игноре: {chain} {address}")


async def cmd_unignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    parsed = _parse_chain_address(context.args)
    if not parsed:
        await update.message.reply_text("Использование: /unignore <base|robinhood> <адрес контракта>")
        return
    chain, address = parsed
    db.unignore_token(chain, address)
    await update.message.reply_text(f"Убрано из игнора: {chain} {address}")


async def cmd_ignored(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    rows = db.get_ignored()
    if not rows:
        await update.message.reply_text("Игнор-лист пуст.")
        return
    lines = [f"{r['chain']}: {r['address']}" for r in rows[:50]]
    lines.append("\nВернуть: /unignore <сеть> <адрес>")
    await update.message.reply_text("\n".join(lines))


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    import chart

    parsed = _parse_chain_address(context.args)
    if not parsed:
        await update.message.reply_text("Использование: /chart <base|robinhood> <адрес контракта>")
        return
    chain, address = parsed
    token = db.get_token(chain, address) or {}
    png, note = chart.render_token_chart(chain, address, str(token.get("symbol") or "?"))
    if png is None:
        await update.message.reply_text(note)
        return
    await update.message.reply_photo(photo=png, caption=note)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if context.args and context.args[0] in CHAINS:
        chain = context.args[0]
        db.set_state(f"muted_{chain}", "1")
        await update.message.reply_text(
            f"Алерты по {CHAIN_LABEL[chain]} выключены - события уйдут в дайджест."
        )
        return
    db.set_state("muted", "1")
    await update.message.reply_text("Алерты выключены полностью.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if context.args and context.args[0] in CHAINS:
        chain = context.args[0]
        db.set_state(f"muted_{chain}", "0")
        await update.message.reply_text(f"Алерты по {CHAIN_LABEL[chain]} включены.")
        return
    db.set_state("muted", "0")
    await update.message.reply_text("Алерты включены.")


def build_application() -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(CommandHandler("chart", cmd_chart))
    application.add_handler(CommandHandler("watch", cmd_watch))
    application.add_handler(CommandHandler("watchlist", cmd_watchlist))
    application.add_handler(CommandHandler("candidates", cmd_candidates))
    application.add_handler(CommandHandler("ignore", cmd_ignore))
    application.add_handler(CommandHandler("unignore", cmd_unignore))
    application.add_handler(CommandHandler("ignored", cmd_ignored))
    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    application.add_handler(CallbackQueryHandler(on_callback))
    return application
