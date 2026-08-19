import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
ROBINHOOD_RPC_URL = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

GECKOTERMINAL_BASE_NETWORK = os.environ.get("GECKOTERMINAL_BASE_NETWORK", "base")
GECKOTERMINAL_ROBINHOOD_NETWORK = os.environ.get("GECKOTERMINAL_ROBINHOOD_NETWORK", "robinhood")

ROBINHOOD_V2_FACTORY = os.environ.get("ROBINHOOD_V2_FACTORY", "")
ROBINHOOD_V3_FACTORY = os.environ.get("ROBINHOOD_V3_FACTORY", "")

# Comma-separated addresses of known quote assets (WETH, USDC, ...) on Robinhood
# Chain, so discovery can tell which side of a new pair is "the meme token".
# Fill in once confirmed via robinhoodchain.blockscout.com - safe to leave empty.
ROBINHOOD_KNOWN_QUOTE_TOKENS = {
    a.strip().lower() for a in os.environ.get("ROBINHOOD_KNOWN_QUOTE_TOKENS", "").split(",") if a.strip()
}

BASE_CHAIN_ID = 8453
ROBINHOOD_CHAIN_ID = 4663

MIN_LIQUIDITY_USD = _float("MIN_LIQUIDITY_USD", 50000)
DISCOVERY_POLL_SECONDS = _int("DISCOVERY_POLL_SECONDS", 45)
WATCHLIST_POLL_SECONDS = _int("WATCHLIST_POLL_SECONDS", 120)
PUMP_THRESHOLD_PCT = _float("PUMP_THRESHOLD_PCT", 50)
DUMP_THRESHOLD_PCT = _float("DUMP_THRESHOLD_PCT", 30)
LIQUIDITY_DROP_THRESHOLD_PCT = _float("LIQUIDITY_DROP_THRESHOLD_PCT", 50)
ALERT_COOLDOWN_SECONDS = _int("ALERT_COOLDOWN_SECONDS", 3600)

DB_PATH = str(BASE_DIR / os.environ.get("DB_PATH", "meme_scout.sqlite3"))

# --- Тихий режим / приоритет алертов -------------------------------------
# Новый токен пушится сразу, только если он не 🔴 и ликвидность не ниже этого
# порога. Всё остальное копится в дайджест-очереди.
PUSH_MIN_LIQUIDITY_USD = _float("PUSH_MIN_LIQUIDITY_USD", 100000)

# --- Дневной дайджест ----------------------------------------------------
DIGEST_ENABLED = _int("DIGEST_ENABLED", 1) == 1
DIGEST_HOUR = _int("DIGEST_HOUR", 9)          # локальный час отправки
DIGEST_MINUTE = _int("DIGEST_MINUTE", 0)
DIGEST_POLL_SECONDS = _int("DIGEST_POLL_SECONDS", 300)

# --- «Выживший» + отслеживание исходов -----------------------------------
OUTCOME_POLL_SECONDS = _int("OUTCOME_POLL_SECONDS", 600)
SURVIVOR_MIN_AGE_HOURS = _float("SURVIVOR_MIN_AGE_HOURS", 24)
# Ликвидность на отметке 24ч должна быть не ниже стартовой * этот коэффициент.
SURVIVOR_MIN_LIQ_RATIO = _float("SURVIVOR_MIN_LIQ_RATIO", 1.0)
SURVIVOR_MIN_VOLUME_USD = _float("SURVIVOR_MIN_VOLUME_USD", 10000)

# --- Здоровье watchlist (лечит 429 от GeckoTerminal) ---------------------
# За один цикл опрашиваем не больше стольких токенов (самые давно не
# проверявшиеся - первыми), иначе 1600+ записей не укладываются в интервал.
WATCHLIST_BATCH = _int("WATCHLIST_BATCH", 120)
WATCHLIST_CONCURRENCY = _int("WATCHLIST_CONCURRENCY", 4)
# Токен вылетает из watchlist после стольких подряд проверок с мёртвой
# ликвидностью, либо по возрасту (ручные /watch не трогаем).
WATCHLIST_DEAD_LIQUIDITY_USD = _float("WATCHLIST_DEAD_LIQUIDITY_USD", 1000)
WATCHLIST_DEAD_CHECKS = _int("WATCHLIST_DEAD_CHECKS", 3)
WATCHLIST_MAX_AGE_DAYS = _float("WATCHLIST_MAX_AGE_DAYS", 14)

# Минимальный интервал между запросами к GeckoTerminal (free tier ~30/мин).
GECKOTERMINAL_MIN_INTERVAL = _float("GECKOTERMINAL_MIN_INTERVAL", 3.0)
# После 429 GeckoTerminal банит IP на заметное время - уходим в паузу с
# удваивающимся интервалом, вместо того чтобы долбиться и продлевать бан.
GECKOTERMINAL_COOLDOWN_START = _float("GECKOTERMINAL_COOLDOWN_START", 60)
GECKOTERMINAL_COOLDOWN_MAX = _float("GECKOTERMINAL_COOLDOWN_MAX", 1800)

# --- Публикация статистики на сайт ---------------------------------------
# Бот остаётся личным, но накопленные им цифры выкладываются на deftools.xyz
# в виде статического JSON - страница живёт даже когда бот выключен.
PUBLISH_ENABLED = _int("PUBLISH_ENABLED", 1) == 1
PUBLISH_INTERVAL_SECONDS = _int("PUBLISH_INTERVAL_SECONDS", 3600)
SITE_REPO_PATH = os.environ.get("SITE_REPO_PATH", str(BASE_DIR.parent / "base-tools"))

# --- Логи ----------------------------------------------------------------
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 5_000_000)
LOG_BACKUP_COUNT = _int("LOG_BACKUP_COUNT", 3)
