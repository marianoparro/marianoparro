"""Parse a Chase CSV export and store it in the transactions table."""

import csv
import io
import re
import sqlite3
from collections import Counter
from datetime import datetime

from app.categorize import categorize, normalize_merchant

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
                "description": raw["Description"].strip(),
                "amount": round(-float(raw["Amount"]), 2),
            }
        )
    return rows


def import_rows(
    conn: sqlite3.Connection, rows: list[dict], card_last4: str | None
) -> dict:
    merchant_map = {
        r["merchant_pattern"]: r["category"]
        for r in conn.execute("SELECT merchant_pattern, category FROM merchant_map")
    }

    # Dedup. A plain "skip if it already exists" rule would wrongly drop real
    # repeats — e.g. two identical $5.24 Uber rides on the same day. So we
    # count: if this file has 3 copies of a (date, description, amount) and
    # the DB already has 1, we insert 2. Re-importing a file inserts 0.
    in_file = Counter((r["date"], r["description"], r["amount"]) for r in rows)
    to_insert: Counter = Counter()
    for key, n in in_file.items():
        (already,) = conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE date = ? AND description = ? AND amount = ? AND card_last4 IS ?""",
            (*key, card_last4),
        ).fetchone()
        to_insert[key] = max(0, n - already)

    result = {"imported": 0, "duplicates": 0, "uncategorized": 0, "oneoffs": 0, "flagged": []}
    for r in rows:
        key = (r["date"], r["description"], r["amount"])
        if to_insert[key] == 0:
            result["duplicates"] += 1
            continue
        to_insert[key] -= 1

        match = categorize(r["description"], r["amount"], merchant_map)
        conn.execute(
            """INSERT INTO transactions
               (date, description, amount, category, merchant_normalized,
                card_last4, is_oneoff, is_fixed, month)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["date"], r["description"], r["amount"], match.category,
                normalize_merchant(r["description"]), card_last4,
                int(match.is_oneoff), int(match.is_fixed), r["month"],
            ),
        )
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
    return result
