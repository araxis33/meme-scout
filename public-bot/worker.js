/**
 * Публичный Telegram-бот meme-scout на Cloudflare Workers.
 *
 * Личный бот (main.py) остаётся как есть: он крутится на ноутбуке, отвечает
 * только владельцу и шлёт ему алерты. Этот - другой, отдельный бот: отвечает
 * кому угодно, но только читает и только статистику.
 *
 * Ключевая идея: у воркера НЕТ базы. Он берёт тот же снимок, который уже
 * лежит на сайте (deftools.xyz/scout-stats.json), - поэтому ему не нужен ни
 * включённый ноутбук, ни доступ к SQLite. Ноутбук обновляет снимок раз в час,
 * воркер его читает.
 *
 * Почему обязательно ОТДЕЛЬНЫЙ бот, а не тот же самый: у одного токена не
 * может быть одновременно webhook и long polling. Повесив вебхук на личного
 * бота, мы бы его сломали.
 */

const STATS_URL = "https://deftools.xyz/scout-stats.json";
const PAGE_URL = "https://deftools.xyz/scout.html";
const REPO_URL = "https://github.com/araxis33/meme-scout";
const CACHE_TTL = 300; // снимок обновляется раз в час, 5 минут кэша хватает

const EXPLORER = {
  base: "https://dexscreener.com/base/",
  robinhood: "https://robinhoodchain.blockscout.com/address/",
};
const CHAIN = { base: "Base", robinhood: "Robinhood Chain" };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Небольшая страница-визитка, если кто-то откроет воркер в браузере.
    if (request.method === "GET") {
      return new Response(
        `meme-scout public bot is running.\nStats: ${PAGE_URL}\nSource: ${REPO_URL}\n`,
        { headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }

    if (request.method !== "POST" || url.pathname !== "/webhook") {
      return new Response("Not found", { status: 404 });
    }

    // Telegram присылает этот заголовок, если вебхук зарегистрирован с
    // secret_token. Без проверки любой желающий мог бы слать боту фейковые
    // апдейты.
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.WEBHOOK_SECRET || secret !== env.WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad request", { status: 400 });
    }

    // Отвечаем Telegram сразу, а работу доделываем в фоне: иначе при медленном
    // ответе он начнёт ретраить тот же апдейт.
    ctx.waitUntil(handleUpdate(update, env));
    return new Response("ok");
  },
};

async function handleUpdate(update, env) {
  const message = update.message || update.edited_message;
  if (!message || !message.text) return;

  const chatId = message.chat && message.chat.id;
  if (!chatId) return;

  const command = message.text.trim().split(/\s+/)[0].split("@")[0].toLowerCase();

  try {
    const text = await buildReply(command);
    if (text) await sendMessage(env, chatId, text);
  } catch (err) {
    await sendMessage(
      env,
      chatId,
      "Данные сейчас недоступны. Попробуй через минуту — снимок обновляется раз в час."
    );
  }
}

async function buildReply(command) {
  switch (command) {
    case "/start":
    case "/help":
      return helpText();
    case "/stats":
      return renderStats(await getStats());
    case "/finds":
      return renderFinds(await getStats());
    case "/survivors":
      return renderSurvivors(await getStats());
    case "/drains":
      return renderDrains(await getStats());
    default:
      return null; // молчим на всё остальное, чтобы не шуметь в группах
  }
}

async function getStats() {
  const response = await fetch(STATS_URL, {
    cf: { cacheTtl: CACHE_TTL, cacheEverything: true },
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error("stats unavailable: " + response.status);
  return await response.json();
}

// --- форматирование ------------------------------------------------------

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])
  );
}

function num(value) {
  if (value == null) return "—";
  return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function usd(value) {
  return value == null ? "—" : "$" + num(value);
}

function pct(part, whole) {
  return whole ? ((part / whole) * 100).toFixed(1) + "%" : "—";
}

function ago(unix) {
  const seconds = Math.max(0, Date.now() / 1000 - unix);
  if (seconds < 5400) return Math.round(seconds / 60) + " мин назад";
  if (seconds < 172800) return Math.round(seconds / 3600) + " ч назад";
  return Math.round(seconds / 86400) + " дн назад";
}

function tokenLink(chain, address, symbol) {
  const base = EXPLORER[chain];
  const label = esc(symbol || "?");
  return base ? `<a href="${base}${esc(address)}">${label}</a>` : label;
}

function footer(stats) {
  return `\n<i>Снимок ${ago(stats.generated_at_unix)}.</i> <a href="${PAGE_URL}">Все данные</a>`;
}

function helpText() {
  return (
    "🔭 <b>Meme Scout</b>\n\n" +
    "Сканер смотрит каждый новый пул на Base и Robinhood Chain, проверяет токен " +
    "на honeypot, безлимитную эмиссию, незалоченную ликвидность и концентрацию " +
    "держателей — и следит, что с ним будет дальше.\n\n" +
    "<b>Команды</b>\n" +
    "/stats — сколько всего найдено и сколько прошло проверки\n" +
    "/finds — последние чистые находки\n" +
    "/survivors — кто пережил первые сутки\n" +
    "/drains — у кого только что слили ликвидность\n\n" +
    `Страница со всеми данными: ${PAGE_URL}\n` +
    `Исходники: ${REPO_URL}\n\n` +
    "<i>Это автоматические проверки, а не совет. Названия подделывают, " +
    "проверки ошибаются — решай сам.</i>"
  );
}

function renderStats(stats) {
  const t = stats.totals;
  return (
    `📊 <b>За ${stats.period.days} дней наблюдения</b>\n\n` +
    `<b>${num(t.seen)}</b> новых токенов замечено\n` +
    `<b>${num(t.scored)}</b> имели ликвидность от ${usd(stats.liquidity_threshold_usd)} — ${pct(t.scored, t.seen)}\n` +
    `<b>${num(t.green)}</b> прошли проверку чисто — ${pct(t.green, t.seen)}\n\n` +
    `<b>По сетям</b>\n` +
    `Robinhood Chain: ${num(t.robinhood)} (${pct(t.robinhood, t.seen)})\n` +
    `Base: ${num(t.base)} (${pct(t.base, t.seen)})\n\n` +
    `Обвалов ликвидности отмечено: ${num((stats.alerts || {}).rug || 0)}` +
    footer(stats)
  );
}

function renderFinds(stats) {
  const finds = (stats.recent_finds || []).slice(0, 8);
  if (!finds.length) return "Пока пусто." + footer(stats);
  const lines = finds.map(
    (f) =>
      `${f.verdict === "green" ? "🟢" : "🟡"} ${tokenLink(f.chain, f.address, f.symbol)} ` +
      `<b>${f.score}</b>/100 · ${usd(f.liquidity_usd)} · ${CHAIN[f.chain] || f.chain} · ${ago(f.first_seen)}`
  );
  return "🔎 <b>Последние находки</b>\n\n" + lines.join("\n") + "\n" + footer(stats);
}

function renderSurvivors(stats) {
  const survivors = (stats.survivors || []).slice(0, 8);
  if (!survivors.length)
    return "Пока никто не дотянул до суток с целой ликвидностью." + footer(stats);
  const lines = survivors.map(
    (s) =>
      `🏆 ${tokenLink(s.chain, s.address, s.symbol)} · ` +
      `${usd(s.liq_0)} → ${usd(s.liq_24h)} · объём ${usd(s.volume_24h)}`
  );
  return (
    "🏆 <b>Пережили первые сутки</b>\n" +
    "<i>Ликвидность не ниже стартовой и всё ещё торгуются. Таких единицы.</i>\n\n" +
    lines.join("\n") +
    "\n" +
    footer(stats)
  );
}

function renderDrains(stats) {
  const drains = (stats.recent_drains || []).slice(0, 8);
  if (!drains.length) return "Пока пусто." + footer(stats);
  const lines = drains.map(
    (d) =>
      `🚨 ${tokenLink(d.chain, d.address, d.symbol)} · ${CHAIN[d.chain] || d.chain} · ${ago(d.ts)}`
  );
  return (
    "🚨 <b>Ликвидность упала больше чем вдвое</b>\n" +
    "<i>Часть из этого — rug pull, часть — обычный отток. Сканер их не различает.</i>\n\n" +
    lines.join("\n") +
    "\n" +
    footer(stats)
  );
}

// --- Telegram ------------------------------------------------------------

async function sendMessage(env, chatId, text) {
  const response = await fetch(
    `https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    }
  );
  return response.ok;
}
