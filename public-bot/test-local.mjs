/**
 * Прогоняет воркер локально, без деплоя и без Cloudflare.
 *
 * Подменяет глобальный fetch: запросы за статистикой уходят на живой сайт
 * (данные настоящие), а обращения к Telegram перехватываются - вместо
 * отправки печатаем, что бот ответил бы.
 *
 * Запуск:  node test-local.mjs
 */
import worker from "./worker.js";

const SECRET = "test-secret";
const env = { BOT_TOKEN: "test-token", WEBHOOK_SECRET: SECRET };

const realFetch = globalThis.fetch;
let sent = [];

globalThis.fetch = async (url, options) => {
  const href = typeof url === "string" ? url : url.url;
  if (href.includes("api.telegram.org")) {
    sent.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }
  return realFetch(href, { headers: (options && options.headers) || {} });
};

function ctx() {
  const jobs = [];
  return { waitUntil: (p) => jobs.push(p), done: () => Promise.all(jobs) };
}

async function send(text, { secret = SECRET } = {}) {
  sent = [];
  const c = ctx();
  const response = await worker.fetch(
    new Request("https://bot.example/webhook", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret },
      body: JSON.stringify({ message: { chat: { id: 123 }, text } }),
    }),
    env,
    c
  );
  await c.done();
  return { status: response.status, replies: sent };
}

let failures = 0;
function check(label, condition, detail) {
  if (condition) {
    console.log("  OK   " + label + (detail ? " -> " + detail : ""));
  } else {
    failures++;
    console.log("  FAIL " + label + (detail ? " -> " + detail : ""));
  }
}

console.log("\n== защита вебхука ==");
{
  const bad = await send("/stats", { secret: "wrong" });
  check("чужой секрет отвергается", bad.status === 403 && bad.replies.length === 0, "status " + bad.status);

  const getPage = await worker.fetch(new Request("https://bot.example/"), env, ctx());
  check("GET отдаёт визитку, а не webhook", getPage.status === 200, "status " + getPage.status);

  const wrongPath = await worker.fetch(
    new Request("https://bot.example/other", { method: "POST" }), env, ctx());
  check("POST на чужой путь -> 404", wrongPath.status === 404, "status " + wrongPath.status);
}

console.log("\n== ответы на команды ==");
for (const cmd of ["/start", "/stats", "/finds", "/survivors", "/drains"]) {
  const r = await send(cmd);
  const text = r.replies[0] && r.replies[0].text;
  check(cmd, !!text && text.length > 40, text ? text.length + " символов" : "нет ответа");
  if (text && text.length > 4096) {
    failures++;
    console.log("  FAIL " + cmd + " -> длиннее лимита Telegram в 4096 символов");
  }
}

console.log("\n== молчание там, где надо ==");
{
  const r = await send("привет");
  check("на обычный текст не отвечает", r.replies.length === 0);
  const r2 = await send("/unknowncommand");
  check("на неизвестную команду не отвечает", r2.replies.length === 0);
}

console.log("\n== содержимое /stats ==");
{
  const r = await send("/stats");
  const text = r.replies[0].text;
  check("есть число найденных токенов", /\d[\d\s]{3,}/.test(text));
  check("есть обе сети", text.includes("Robinhood Chain") && text.includes("Base"));
  check("есть возраст снимка", text.includes("Снимок"));
  console.log("\n--- как это выглядит в Telegram ---");
  console.log(text.replace(/<[^>]+>/g, ""));
}

console.log("\n== пример /finds ==");
{
  const r = await send("/finds");
  console.log(r.replies[0].text.replace(/<a href="[^"]*">/g, "").replace(/<\/?[^>]+>/g, ""));
}

console.log("\n" + "=".repeat(60));
if (failures) {
  console.log("ПРОВАЛЕНО проверок: " + failures);
  process.exit(1);
}
console.log("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ");
