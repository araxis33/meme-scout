import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    first_seen REAL NOT NULL,
    score INTEGER,
    verdict TEXT,
    liquidity_usd REAL,
    market_cap REAL,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS watchlist (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    added_at REAL NOT NULL,
    baseline_price REAL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    ts REAL NOT NULL,
    price REAL,
    liquidity_usd REAL,
    volume_24h REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_lookup ON price_snapshots (chain, address, ts);

CREATE TABLE IF NOT EXISTS alerts_sent (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (chain, address, alert_type)
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)


def is_token_seen(chain: str, address: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tokens WHERE chain=? AND address=?", (chain, address.lower())
        ).fetchone()
        return row is not None


def mark_token_seen(chain: str, address: str, symbol: str, name: str,
                     score, verdict, liquidity_usd, market_cap):
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tokens
               (chain, address, symbol, name, first_seen, score, verdict, liquidity_usd, market_cap)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (chain, address.lower(), symbol, name, time.time(), score, verdict, liquidity_usd, market_cap),
        )


def add_to_watchlist(chain: str, address: str, baseline_price):
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO watchlist (chain, address, added_at, baseline_price, active)
               VALUES (?,?,?,?,1)""",
            (chain, address.lower(), time.time(), baseline_price),
        )


def get_watchlist():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT chain, address, added_at, baseline_price FROM watchlist WHERE active=1"
        ).fetchall()
        return [dict(r) for r in rows]


def deactivate_watchlist_entry(chain: str, address: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE watchlist SET active=0 WHERE chain=? AND address=?", (chain, address.lower())
        )


def record_price_snapshot(chain: str, address: str, price, liquidity_usd, volume_24h):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO price_snapshots (chain, address, ts, price, liquidity_usd, volume_24h)
               VALUES (?,?,?,?,?,?)""",
            (chain, address.lower(), time.time(), price, liquidity_usd, volume_24h),
        )


def get_snapshot_before(chain: str, address: str, seconds_ago: float):
    """Most recent snapshot at least `seconds_ago` old (closest match)."""
    cutoff = time.time() - seconds_ago
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM price_snapshots WHERE chain=? AND address=? AND ts<=?
               ORDER BY ts DESC LIMIT 1""",
            (chain, address.lower(), cutoff),
        ).fetchone()
        return dict(row) if row else None


def prune_old_snapshots(max_age_seconds: float = 7 * 24 * 3600):
    cutoff = time.time() - max_age_seconds
    with _conn() as conn:
        conn.execute("DELETE FROM price_snapshots WHERE ts < ?", (cutoff,))


def alert_recently_sent(chain: str, address: str, alert_type: str, cooldown_seconds: float) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT ts FROM alerts_sent WHERE chain=? AND address=? AND alert_type=?",
            (chain, address.lower(), alert_type),
        ).fetchone()
        if row is None:
            return False
        return (time.time() - row["ts"]) < cooldown_seconds


def mark_alert_sent(chain: str, address: str, alert_type: str):
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO alerts_sent (chain, address, alert_type, ts)
               VALUES (?,?,?,?)""",
            (chain, address.lower(), alert_type, time.time()),
        )


def get_state(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?,?)", (key, value)
        )
