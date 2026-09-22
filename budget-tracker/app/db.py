"""SQLite setup: connection, schema, and seeding.

SQLite is a whole database in a single file — no server to run. Python ships
with the `sqlite3` module, so there's nothing extra to install.
"""

import os
import sqlite3

from app.seed_data import BUDGET_TARGETS, MERCHANT_MAP

DB_PATH = os.environ.get("DB_PATH", "budget.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,          -- ISO format: YYYY-MM-DD
    description         TEXT NOT NULL,          -- exactly as the bank wrote it
    amount              REAL NOT NULL,          -- positive = money out, negative = refund
    category            TEXT,                   -- NULL = uncategorized
    merchant_normalized TEXT,
    card_last4          TEXT,
    is_oneoff           INTEGER NOT NULL DEFAULT 0,
    is_fixed            INTEGER NOT NULL DEFAULT 0,
    month               TEXT NOT NULL,          -- YYYY-MM, for fast monthly queries
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tx_month ON transactions(month);
CREATE INDEX IF NOT EXISTS idx_tx_dedup ON transactions(date, description, amount);

CREATE TABLE IF NOT EXISTS merchant_map (
    merchant_pattern TEXT UNIQUE NOT NULL,
    category         TEXT NOT NULL,
    added_by         TEXT NOT NULL DEFAULT 'seed',   -- 'seed' or 'user'
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budget_targets (
    category       TEXT UNIQUE NOT NULL,
    monthly_target REAL NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user       TEXT NOT NULL,                   -- 'Mariano' or 'Maura'
    role       TEXT NOT NULL,                   -- 'user' or 'assistant'
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # Rows behave like dicts: row["amount"] instead of row[4].
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        seed(conn)


def seed(conn: sqlite3.Connection) -> None:
    # Targets: insert only if missing, so edits made later aren't reset.
    conn.executemany(
        "INSERT OR IGNORE INTO budget_targets (category, monthly_target) VALUES (?, ?)",
        BUDGET_TARGETS.items(),
    )
    # Merchant patterns: seed rows follow the code, but a pattern a user has
    # corrected (added_by='user') always wins.
    rows = [
        (pattern.upper(), category)
        for category, patterns in MERCHANT_MAP.items()
        for pattern in patterns
    ]
    conn.executemany(
        """
        INSERT INTO merchant_map (merchant_pattern, category, added_by)
        VALUES (?, ?, 'seed')
        ON CONFLICT(merchant_pattern) DO UPDATE SET category = excluded.category
        WHERE merchant_map.added_by = 'seed'
        """,
        rows,
    )
