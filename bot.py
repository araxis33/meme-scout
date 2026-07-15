"""Telegram bot: sends alerts, handles a handful of commands.

Single-user bot - only responds to config.TELEGRAM_CHAT_ID.
"""
import html
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db

log = logging.getLogger("meme-scout.bot")

EXPLORER_LINKS = {
    "base": {
        "basescan": "https://basescan.org/address/{addr}",
        "dexscreener": "https://dexscreener.com/base/{addr}",
        "geckoterminal": "https://www.geckoterminal.com/base/tokens/{addr}",
    },
    "robinhood": {
        "blockscout": "https://robinhoodchain.blockscout.com/address/{addr}",
        "robinscan": "https://robinscan.io/address/{addr}",
    },
}

CHAIN_LABEL = {"base": "Base", "robinhood": "Robinhood Chain"}


def _is_authorized(update: Update) -> bool:
    return str(update.effective_chat.id) == str(config.TELEGRAM_CHAT_ID)


def links_block(chain: str, address: str) -> str:
    parts = [f'<a href="{url.format(addr=address)}">{name}</a>' for name, url in EXPLORER_LINKS[chain].items()]
    return " | ".join(parts)


def format_new_token_alert(chain: str, token: dict, score_result) -> str:
    addr = token["token_address"]
    symbol = html.escape(str(token.get("symbol", "?")))
    name = html.escape(str(token.get("name", "?")))
    limited_note = "\n⚠️ <i>Часть проверок недоступна (новая сеть) - оценка неполная</i>" if score_result.limited else ""
    reasons = "\n".join(f"  {r}" for r in score_result.reasons)
    liquidity_usd = token.get("liquidity_usd")
    liquidity_line = f"${liquidity_usd:,.0f}" if liquidity_usd is not None else "неизвестна"
    return (
        f"{score_result.verdict_emoji} <b>Новый токен: {symbol}</b> ({name})\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Адрес: <code>{addr}</code>\n"
        f"Ликвидность: {liquidity_line}\n"
        f"Score: <b>{score_result.score}/100</b>\n\n"
        f"{reasons}"
        f"{limited_note}\n\n"
        f"{links_block(chain, addr)}"
    )


def format_pump_alert(chain: str, address: str, symbol: str, pct_change: float, price: float) -> str:
    return (
        f"🚀 <b>Памп: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Изменение цены: +{pct_change:.0f}% за час\n"
        f"Текущая цена: ${price:.8f}\n"
        f"Адрес: <code>{address}</code>\n\n"
        f"{links_block(chain, address)}"
    )


def format_dump_alert(chain: str, address: str, symbol: str, pct_change: float, price: float) -> str:
    return (
        f"📉 <b>Дамп: {html.escape(symbol)}</b>\n"
        f"Сеть: {CHAIN_LABEL[chain]}\n"
        f"Изменение цены: {pct_change:.0f}% за час\n"
        f"Текущая цена: ${price:.8f}\n"
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


async def send_alert(application: Application, text: str):
    if db.get_state("muted") == "1":
        return
    try:
        await application.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Failed to send Telegram alert")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Meme-Scout запущен. Слежу за новыми токенами на Base и Robinhood Chain.\n"
        "⚠️ Для Robinhood Chain часть проверок может быть недоступна - сеть совсем новая.\n\n"
        "Команды:\n"
        "/status - статус\n"
        "/watch <base|robinhood> <address> - добавить в watchlist вручную\n"
        "/watchlist - показать watchlist\n"
        "/mute, /unmute - вкл/выкл алерты"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    watchlist = db.get_watchlist()
    muted = db.get_state("muted") == "1"
    last_base_block = db.get_state("last_block_base", "n/a")
    last_rh_block = db.get_state("last_block_robinhood", "n/a")
    await update.message.reply_text(
        f"Статус: {'🔇 muted' if muted else '🔔 активен'}\n"
        f"В watchlist: {len(watchlist)} токенов\n"
        f"Последний блок Base: {last_base_block}\n"
        f"Последний блок Robinhood Chain: {last_rh_block}\n"
        f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if len(context.args) != 2 or context.args[0] not in ("base", "robinhood"):
        await update.message.reply_text("Использование: /watch <base|robinhood> <адрес контракта>")
        return
    chain, address = context.args[0], context.args[1]
    db.add_to_watchlist(chain, address, baseline_price=None)
    await update.message.reply_text(f"Добавлено в watchlist: {chain} {address}")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    watchlist = db.get_watchlist()
    if not watchlist:
        await update.message.reply_text("Watchlist пуст.")
        return
    lines = [f"{w['chain']}: {w['address']}" for w in watchlist[:50]]
    await update.message.reply_text("\n".join(lines))


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    db.set_state("muted", "1")
    await update.message.reply_text("Алерты выключены.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    db.set_state("muted", "0")
    await update.message.reply_text("Алерты включены.")


def build_application() -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("watch", cmd_watch))
    application.add_handler(CommandHandler("watchlist", cmd_watchlist))
    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    return application
