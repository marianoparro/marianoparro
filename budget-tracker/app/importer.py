"""Parse a Chase CSV export and store it in the transactions table."""

import csv
import html
import io
import re
from collections import Counter
from datetime import datetime

from app.categorize import categorize, normalize_merchant
from app.seed_data import FIXED, ONEOFF

# Any single charge above this is surfaced after import (for the AI agent later).
LARGE_CHARGE = 200

_CARD_IN_FILENAME = re.compile(r"chase(\d{4})", re.IGNORECASE)


def card_from_filename(filename: str | None) -> str | None:
    """'Chase6297_Activity20260124_....CSV' -> '6297'."""
    m = _CARD_IN_FILENAME.search(filename or "")
    return m.group(1) if m else None


def parse_chase_csv(text: str) -> list[dict]:
    """Return one dict per purchase/refund. Card payments are skipped.

    Chase sign convention: purchases are negative, refunds positive.
    We flip it so that amount > 0 means money spent — easier to add up.
    """
    rows = []
    for raw in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        # Paying off the card isn't spending; counting it would double-count.
        if (raw.get("Type") or "").strip().lower() == "payment":
            continue
        date = datetime.strptime(raw["Transaction Date"].strip(), "%m/%d/%Y").date()
        rows.append(
            {
                "date": date.isoformat(),
                "month": date.strftime("%Y-%m"),
                # Chase escapes '&' as '&amp;' in some rows ("H&amp;M").
                "description": html.unescape(raw["Description"]).strip(),
                "amount": round(-float(raw["Amount"]), 2),
            }
        )
    return rows


def load_merchant_map(conn) -> dict[str, str]:
    return {
        r["merchant_pattern"]: r["category"]
        for r in conn.execute("SELECT merchant_pattern, category FROM merchant_map")
    }


def recategorize(conn) -> int:
    """Re-run matching on every stored transaction; return how many changed.

    Categories are derived from merchant_map, so whenever the map changes
    (new seed patterns, a user correction) we recompute instead of leaving
    old rows stuck with the old answer.
    """
    merchant_map = load_merchant_map(conn)
    updates = []
    for r in conn.execute(
        "SELECT id, description, amount, category, is_oneoff, is_fixed FROM transactions"
    ).fetchall():
        m = categorize(r["description"], r["amount"], merchant_map)
        new = (m.category, int(m.is_oneoff), int(m.is_fixed))
        if new != (r["category"], r["is_oneoff"], r["is_fixed"]):
            updates.append((*new, r["id"]))
    conn.executemany(
        "UPDATE transactions SET category = ?, is_oneoff = ?, is_fixed = ? WHERE id = ?", updates
    )
    return len(updates)


def save_rule(conn, pattern: str, category: str) -> dict:
    """Store a user's merchant -> category rule and re-apply the map.

    Shared by the dashboard (POST /merchant-map) and the assistant, so a
    correction made either way behaves identically. Raises ValueError on
    bad input.
    """
    pattern = pattern.strip().upper()
    if not pattern:
        raise ValueError("pattern can't be empty")
    valid = {r["category"] for r in conn.execute("SELECT category FROM budget_targets")}
    valid |= {ONEOFF, FIXED}
    if category not in valid:
        raise ValueError(f"category must be one of: {sorted(valid)}")
    conn.execute(
        """INSERT INTO merchant_map (merchant_pattern, category, added_by)
           VALUES (?, ?, 'user')
           ON CONFLICT(merchant_pattern) DO UPDATE
           SET category = excluded.category, added_by = 'user'""",
        (pattern, category),
    )
    return {"pattern": pattern, "category": category, "transactions_updated": recategorize(conn)}


def import_rows(
    conn, rows: list[dict], card_last4: str | None
) -> dict:
    merchant_map = load_merchant_map(conn)

    # Dedup. A plain "skip if it already exists" rule would wrongly drop real
    # repeats — e.g. two identical $5.24 Uber rides on the same day. So we
    # count: if this file has 3 copies of a (date, description, amount) and
    # the DB already has 1, we insert 2. Re-importing a file inserts 0.
    in_file = Counter((r["date"], r["description"], r["amount"]) for r in rows)
    # One query for what's already stored in this file's date range (not one
    # per row: with a hosted database each query is a network round trip).
    in_db: Counter = Counter()
    if rows:
        for r in conn.execute(
            """SELECT date, description, amount, COUNT(*) AS n FROM transactions
               WHERE date BETWEEN ? AND ? AND card_last4 IS NOT DISTINCT FROM ?
               GROUP BY date, description, amount""",
            (min(r["date"] for r in rows), max(r["date"] for r in rows), card_last4),
        ):
            in_db[(r["date"], r["description"], round(r["amount"], 2))] = r["n"]
    to_insert = Counter({key: max(0, n - in_db[key]) for key, n in in_file.items()})
    new_rows = []

    result = {"imported": 0, "duplicates": 0, "uncategorized": 0, "oneoffs": 0, "flagged": []}
    for r in rows:
        key = (r["date"], r["description"], r["amount"])
        if to_insert[key] == 0:
            result["duplicates"] += 1
            continue
        to_insert[key] -= 1

        match = categorize(r["description"], r["amount"], merchant_map)
        new_rows.append((
            r["date"], r["description"], r["amount"], match.category,
            normalize_merchant(r["description"]), card_last4,
            int(match.is_oneoff), int(match.is_fixed), r["month"],
        ))
        result["imported"] += 1
        result["uncategorized"] += match.category is None
        result["oneoffs"] += match.is_oneoff

        # Things a human (or the Step 3 agent) should look at right away.
        if match.is_oneoff or r["amount"] > LARGE_CHARGE:
            result["flagged"].append(
                {
                    "date": r["date"],
                    "description": r["description"],
                    "amount": r["amount"],
                    "category": match.category,
                    "reason": "one-off" if match.is_oneoff else f"over ${LARGE_CHARGE}",
                }
            )
    conn.executemany(
        """INSERT INTO transactions
           (date, description, amount, category, merchant_normalized,
            card_last4, is_oneoff, is_fixed, month)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        new_rows,
    )
    return result
