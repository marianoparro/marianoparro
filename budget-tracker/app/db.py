"""Database: SQLite locally, Postgres when deployed.

- No DATABASE_URL (your laptop, the tests): a SQLite file, DB_PATH
  (default ./budget.db). Nothing to install.
- DATABASE_URL set (on Render): a hosted Postgres, e.g. Neon's free tier.
  Render's free plan wipes local files on every restart, so the data has
  to live somewhere else.

The rest of the app writes one kind of SQL (with `?` placeholders) and gets
rows that work both as dicts (row["amount"]) and tuples ((a, b) = row);
the small adapter below smooths over the differences.
"""

import os
import sqlite3

from app.seed_data import BUDGET_TARGETS, MERCHANT_MAP

DB_PATH = os.environ.get("DB_PATH", "budget.db")


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


# Tables are the same in both; only a few type names differ.
SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                  {id_pk},
    date                TEXT NOT NULL,          -- ISO format: YYYY-MM-DD
    description         TEXT NOT NULL,          -- exactly as the bank wrote it
    amount              {money} NOT NULL,       -- positive = money out, negative = refund
    category            TEXT,                   -- NULL = uncategorized
    merchant_normalized TEXT,
    card_last4          TEXT,
    is_oneoff           INTEGER NOT NULL DEFAULT 0,
    is_fixed            INTEGER NOT NULL DEFAULT 0,
    month               TEXT NOT NULL,          -- YYYY-MM, for fast monthly queries
    created_at          TEXT NOT NULL DEFAULT {now}
);
CREATE INDEX IF NOT EXISTS idx_tx_month ON transactions(month);
CREATE INDEX IF NOT EXISTS idx_tx_dedup ON transactions(date, description, amount);

CREATE TABLE IF NOT EXISTS merchant_map (
    merchant_pattern TEXT UNIQUE NOT NULL,
    category         TEXT NOT NULL,
    added_by         TEXT NOT NULL DEFAULT 'seed',   -- 'seed' or 'user'
    created_at       TEXT NOT NULL DEFAULT {now}
);

CREATE TABLE IF NOT EXISTS budget_targets (
    category       TEXT UNIQUE NOT NULL,
    monthly_target {money} NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT {now}
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         {id_pk},
    "user"     TEXT NOT NULL,                   -- 'Mariano' or 'Maura' ("user" is a reserved word in Postgres)
    role       TEXT NOT NULL,                   -- 'user' or 'assistant'
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT {now}
);
"""

SQLITE_TYPES = {"id_pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "money": "REAL", "now": "CURRENT_TIMESTAMP"}
POSTGRES_TYPES = {"id_pk": "BIGSERIAL PRIMARY KEY", "money": "DOUBLE PRECISION", "now": "(CURRENT_TIMESTAMP::text)"}

# SQLite's ROUND(x, 2) works on floats; Postgres only rounds exact numerics.
# Adding this overload lets the same ROUND(SUM(amount), 2) run on both.
POSTGRES_EXTRAS = """
CREATE OR REPLACE FUNCTION round(double precision, integer) RETURNS double precision
    AS $$ SELECT round($1::numeric, $2)::double precision $$ LANGUAGE sql IMMUTABLE;
"""


class Row(tuple):
    """A Postgres row that also answers row["column"], like sqlite3.Row."""

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._index = {n: i for i, n in enumerate(names)}
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._index)


def _row_factory(cursor):
    names = [c.name for c in cursor.description or []]
    return lambda values: Row(names, values)


class PostgresConnection:
    """Wraps psycopg so callers use the same calls as sqlite3."""

    def __init__(self, url: str):
        import psycopg  # only needed when deployed

        self._conn = psycopg.connect(url, row_factory=_row_factory)

    @staticmethod
    def _sql(query: str) -> str:
        # `?` placeholders -> `%s`; a literal % must be doubled for psycopg.
        return query.replace("%", "%%").replace("?", "%s")

    def execute(self, query, params=()):
        return self._conn.execute(self._sql(query), params)

    def executemany(self, query, rows):
        with self._conn.cursor() as cur:
            cur.executemany(self._sql(query), list(rows))

    def executescript(self, script):
        self._conn.execute(script)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


class SqliteConnection(sqlite3.Connection):
    """sqlite3 commits on `with` but leaves the file open; close it too."""

    def __exit__(self, *exc):
        result = super().__exit__(*exc)
        self.close()
        return result


def get_conn():
    url = _database_url()
    if url:
        return PostgresConnection(url)
    conn = sqlite3.connect(DB_PATH, factory=SqliteConnection)
    # Rows behave like dicts: row["amount"] instead of row[4].
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        if isinstance(conn, PostgresConnection):
            conn.executescript(SCHEMA.format(**POSTGRES_TYPES))
            conn.executescript(POSTGRES_EXTRAS)
        else:
            conn.executescript(SCHEMA.format(**SQLITE_TYPES))
        seed(conn)


def seed(conn) -> None:
    # Targets: insert only if missing, so edits made later aren't reset.
    conn.executemany(
        "INSERT INTO budget_targets (category, monthly_target) VALUES (?, ?) ON CONFLICT (category) DO NOTHING",
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
    # A pattern deleted from seed_data.py must leave the DB too, or a stale
    # longer pattern (e.g. "TARGET T-") would keep beating its replacement.
    seeded = [pattern for pattern, _ in rows]
    conn.execute(
        f"DELETE FROM merchant_map WHERE added_by = 'seed' "
        f"AND merchant_pattern NOT IN ({','.join('?' * len(seeded))})",
        seeded,
    )
