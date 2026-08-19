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
    # timeout: watcher now checks tokens concurrently, so writes can queue up
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, coldef: str):
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA_V2)
        # watchlist gained health/bookkeeping columns after the first release
        _add_column_if_missing(conn, "watchlist", "manual", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "watchlist", "last_checked", "REAL")
        _add_column_if_missing(conn, "watchlist", "dead_checks", "INTEGER NOT NULL DEFAULT 0")


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


SCHEMA_V2 = """
-- Токены, которые пользователь скрыл кнопкой «Игнор» - по ним больше не алертим.
CREATE TABLE IF NOT EXISTS ignored (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (chain, address)
);

-- Append-only журнал алертов (alerts_sent хранит только последний по типу,
-- поэтому по нему нельзя посчитать историю для дайджеста).
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    symbol TEXT,
    pushed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events (ts);

-- Тихая очередь: всё, что не дотянуло до пуша, уходит сюда и попадает
-- в утренний дайджест одним сообщением.
CREATE TABLE IF NOT EXISTS digest_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chain TEXT,
    address TEXT,
    alert_type TEXT,
    symbol TEXT,
    summary TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_digest_queue_open ON digest_queue (consumed, ts);

-- Судьба каждого токена, про который был алерт: снимок на момент алерта
-- плюс контрольные точки 24ч и 7д. Основа «табло точности».
CREATE TABLE IF NOT EXISTS outcomes (
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    alerted_at REAL NOT NULL,
    score INTEGER,
    verdict TEXT,
    price_0 REAL,
    liq_0 REAL,
    price_24h REAL,
    liq_24h REAL,
    volume_24h REAL,
    checked_24h INTEGER NOT NULL DEFAULT 0,
    price_7d REAL,
    liq_7d REAL,
    checked_7d INTEGER NOT NULL DEFAULT 0,
    survivor_alerted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_alerted ON outcomes (alerted_at);
"""


# --- ignore list ---------------------------------------------------------

def ignore_token(chain: str, address: str):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ignored (chain, address, ts) VALUES (?,?,?)",
            (chain, address.lower(), time.time()),
        )
        conn.execute("UPDATE watchlist SET active=0 WHERE chain=? AND address=?", (chain, address.lower()))


def unignore_token(chain: str, address: str):
    with _conn() as conn:
        conn.execute("DELETE FROM ignored WHERE chain=? AND address=?", (chain, address.lower()))


def is_ignored(chain: str, address: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM ignored WHERE chain=? AND address=?", (chain, address.lower())
        ).fetchone()
        return row is not None


def get_ignored():
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT chain, address, ts FROM ignored ORDER BY ts DESC")]


# --- alert journal -------------------------------------------------------

def log_alert_event(chain: str, address: str, alert_type: str, symbol: str, pushed: bool):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO alert_events (ts, chain, address, alert_type, symbol, pushed)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), chain, (address or "").lower(), alert_type, symbol, 1 if pushed else 0),
        )


def count_alert_events(since_ts: float):
    """-> {(alert_type, pushed): count} за период."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT alert_type, pushed, COUNT(*) AS n FROM alert_events
               WHERE ts >= ? GROUP BY alert_type, pushed""",
            (since_ts,),
        ).fetchall()
        return {(r["alert_type"], bool(r["pushed"])): r["n"] for r in rows}


# --- digest queue --------------------------------------------------------

def enqueue_digest(chain: str, address: str, alert_type: str, symbol: str, summary: str):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO digest_queue (ts, chain, address, alert_type, symbol, summary, consumed)
               VALUES (?,?,?,?,?,?,0)""",
            (time.time(), chain, (address or "").lower(), alert_type, symbol, summary),
        )


def get_pending_digest(limit: int = 500):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM digest_queue WHERE consumed=0 ORDER BY ts LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_digest_consumed(max_id: int):
    with _conn() as conn:
        conn.execute("UPDATE digest_queue SET consumed=1 WHERE consumed=0 AND id<=?", (max_id,))


def prune_digest_queue(max_age_seconds: float = 14 * 24 * 3600):
    with _conn() as conn:
        conn.execute("DELETE FROM digest_queue WHERE consumed=1 AND ts < ?", (time.time() - max_age_seconds,))


# --- outcomes ------------------------------------------------------------

def record_outcome_start(chain: str, address: str, symbol: str, score, verdict, price_0, liq_0):
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO outcomes
               (chain, address, symbol, alerted_at, score, verdict, price_0, liq_0)
               VALUES (?,?,?,?,?,?,?,?)""",
            (chain, address.lower(), symbol, time.time(), score, verdict, price_0, liq_0),
        )


def get_outcomes_due(field: str, min_age_seconds: float, limit: int = 40):
    """Записи, у которых контрольная точка (24h или 7d) уже созрела."""
    assert field in ("24h", "7d")
    cutoff = time.time() - min_age_seconds
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM outcomes WHERE checked_" + field + "=0 AND alerted_at <= ? "
            "ORDER BY alerted_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def fill_outcome_24h(chain: str, address: str, price, liq, volume):
    with _conn() as conn:
        conn.execute(
            """UPDATE outcomes SET price_24h=?, liq_24h=?, volume_24h=?, checked_24h=1
               WHERE chain=? AND address=?""",
            (price, liq, volume, chain, address.lower()),
        )


def fill_outcome_7d(chain: str, address: str, price, liq):
    with _conn() as conn:
        conn.execute(
            "UPDATE outcomes SET price_7d=?, liq_7d=?, checked_7d=1 WHERE chain=? AND address=?",
            (price, liq, chain, address.lower()),
        )


def mark_survivor_alerted(chain: str, address: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE outcomes SET survivor_alerted=1 WHERE chain=? AND address=?",
            (chain, address.lower()),
        )


def get_outcome(chain: str, address: str):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE chain=? AND address=?", (chain, address.lower())
        ).fetchone()
        return dict(row) if row else None


def get_outcomes_since(since_ts: float, field: str = "24h"):
    assert field in ("24h", "7d")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM outcomes WHERE checked_" + field + "=1 AND alerted_at >= ? ORDER BY alerted_at",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_tracked(chain: str, address: str) -> bool:
    """«Наш» ли токен: мы им заинтересовались (не красный) либо его добавили руками.

    Нужно, чтобы отличать памп/дамп/rug по интересному токену от шума по
    тысячам мусорных, попавших в watchlist автоматом.
    """
    addr = address.lower()
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM outcomes WHERE chain=? AND address=? AND verdict IN ('green','yellow')",
            (chain, addr),
        ).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT 1 FROM watchlist WHERE chain=? AND address=? AND manual=1", (chain, addr)
        ).fetchone()
        return row is not None


# --- watchlist health ----------------------------------------------------

def add_to_watchlist_manual(chain: str, address: str, baseline_price=None):
    add_to_watchlist(chain, address, baseline_price)
    with _conn() as conn:
        conn.execute(
            "UPDATE watchlist SET manual=1, active=1, dead_checks=0 WHERE chain=? AND address=?",
            (chain, address.lower()),
        )


def get_watchlist_batch(limit: int):
    """Порция watchlist, начиная с тех, кого дольше всех не проверяли."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT chain, address, added_at, baseline_price, manual, last_checked, dead_checks
               FROM watchlist WHERE active=1
               ORDER BY COALESCE(last_checked, 0) ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_watchlist_checked(chain: str, address: str, dead: bool):
    with _conn() as conn:
        if dead:
            conn.execute(
                """UPDATE watchlist SET last_checked=?, dead_checks=dead_checks+1
                   WHERE chain=? AND address=?""",
                (time.time(), chain, address.lower()),
            )
        else:
            conn.execute(
                "UPDATE watchlist SET last_checked=?, dead_checks=0 WHERE chain=? AND address=?",
                (time.time(), chain, address.lower()),
            )


def sweep_watchlist(dead_checks: int, max_age_seconds: float) -> int:
    """Выкидывает мёртвые и просроченные записи (ручные /watch не трогает)."""
    cutoff = time.time() - max_age_seconds
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE watchlist SET active=0
               WHERE active=1 AND manual=0 AND (dead_checks >= ? OR added_at < ?)""",
            (dead_checks, cutoff),
        )
        return cur.rowcount


def watchlist_size() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0]


# --- статистика для дайджеста -------------------------------------------

def count_tokens_since(since_ts: float):
    """-> {(chain, verdict): count} по first_seen."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT chain, verdict, COUNT(*) AS n FROM tokens
               WHERE first_seen >= ? GROUP BY chain, verdict""",
            (since_ts,),
        ).fetchall()
        return {(r["chain"], r["verdict"]): r["n"] for r in rows}


def get_snapshots(chain: str, address: str, since_ts: float):
    with _conn() as conn:
        rows = conn.execute(
            """SELECT ts, price, liquidity_usd, volume_24h FROM price_snapshots
               WHERE chain=? AND address=? AND ts >= ? ORDER BY ts""",
            (chain, address.lower(), since_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def get_token(chain: str, address: str):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM tokens WHERE chain=? AND address=?", (chain, address.lower())
        ).fetchone()
        return dict(row) if row else None
