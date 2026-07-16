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
