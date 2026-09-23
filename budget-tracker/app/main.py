"""FastAPI app. Run locally with:  uvicorn app.main:app --reload
Then open http://127.0.0.1:8000/docs for a clickable API explorer.
"""

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import require_login
from app.db import get_conn, init_db
from app.importer import card_from_filename, import_rows, parse_chase_csv, recategorize
from app.seed_data import FIXED, ONEOFF

# A category is flagged when actual spend is more than this % above target.
OVER_TARGET_PCT = 30

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create tables + load targets and merchant map
    with get_conn() as conn:
        recategorize(conn)  # apply any map changes to already-imported rows
    yield


app = FastAPI(title="Family Budget Tracker", lifespan=lifespan)
app.middleware("http")(require_login)  # login wall in front of everything

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


def _env_money(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


@app.get("/config")
def config(request: Request):
    """Income and fixed costs come from environment variables, not code,
    so they never end up in the (public) git repo. Unset -> null, and the
    frontend simply hides the savings tile."""
    with get_conn() as conn:
        categories = [r["category"] for r in conn.execute("SELECT category FROM budget_targets")]
        tax = conn.execute(
            "SELECT monthly_target FROM budget_targets WHERE category = 'Taxes'"
        ).fetchone()
    return {
        "user": request.state.user,  # who's logged in (None when login is off)
        "categories": categories + [ONEOFF, FIXED],
        "monthly_net_income": _env_money("MONTHLY_NET_INCOME"),
        "monthly_fixed_costs": _env_money("MONTHLY_FIXED_COSTS"),
        "yearly_savings_goal": _env_money("YEARLY_SAVINGS_GOAL"),
        "monthly_tax_setaside": tax["monthly_target"] if tax else 0,
    }


@app.get("/months")
def months():
    """One row per month with data, oldest first — feeds the trend chart."""
    with get_conn() as conn:
        (variable_target,) = conn.execute(
            "SELECT COALESCE(SUM(monthly_target), 0) FROM budget_targets"
        ).fetchone()
        rows = conn.execute(
            """SELECT month,
                      ROUND(SUM(CASE WHEN is_oneoff = 0 AND is_fixed = 0 THEN amount ELSE 0 END), 2)
                        AS variable_total,
                      ROUND(SUM(CASE WHEN is_oneoff = 1 THEN amount ELSE 0 END), 2) AS oneoff_total,
                      MIN(date) AS first_date,
                      MAX(date) AS last_date
               FROM transactions GROUP BY month ORDER BY month"""
        ).fetchall()
    return [{**dict(r), "variable_target": variable_target} for r in rows]


def _check_month(month: str) -> None:
    if not MONTH_RE.match(month):
        raise HTTPException(400, "month must look like YYYY-MM, e.g. 2026-04")


@app.post("/import")
async def import_csv(
    files: list[UploadFile] = File(...),
    card_last4: str | None = Form(None),  # optional override if the filename has no card
):
    totals = {"imported": 0, "duplicates": 0, "uncategorized": 0, "oneoffs": 0, "flagged": []}
    per_file = []
    with get_conn() as conn:  # one DB transaction: all files succeed or none do
        for f in files:
            text = (await f.read()).decode("utf-8-sig")
            try:
                rows = parse_chase_csv(text)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, f"{f.filename}: not a Chase CSV ({e})")
            card = card_last4 or card_from_filename(f.filename)
            result = import_rows(conn, rows, card)
            per_file.append({"file": f.filename, "card_last4": card, **result})
            for k in totals:
                totals[k] += result[k]
    return {**totals, "files": per_file}


class MerchantRule(BaseModel):
    pattern: str  # substring of the bank description, e.g. "CARULLA"
    category: str


@app.post("/merchant-map")
def add_merchant_rule(rule: MerchantRule):
    """Teach the app a merchant. Also what the Step 3 agent will call on corrections."""
    pattern = rule.pattern.strip().upper()
    if not pattern:
        raise HTTPException(400, "pattern can't be empty")
    with get_conn() as conn:
        valid = {r["category"] for r in conn.execute("SELECT category FROM budget_targets")}
        valid |= {ONEOFF, FIXED}
        if rule.category not in valid:
            raise HTTPException(400, f"category must be one of: {sorted(valid)}")
        conn.execute(
            """INSERT INTO merchant_map (merchant_pattern, category, added_by)
               VALUES (?, ?, 'user')
               ON CONFLICT(merchant_pattern) DO UPDATE
               SET category = excluded.category, added_by = 'user'""",
            (pattern, rule.category),
        )
        changed = recategorize(conn)
    return {"pattern": pattern, "category": rule.category, "transactions_updated": changed}


@app.get("/summary/{month}")
def summary(month: str):
    _check_month(month)
    with get_conn() as conn:
        targets = {
            r["category"]: r["monthly_target"]
            for r in conn.execute("SELECT category, monthly_target FROM budget_targets")
        }
        actuals = {
            r["category"]: r["total"]
            for r in conn.execute(
                """SELECT category, ROUND(SUM(amount), 2) AS total FROM transactions
                   WHERE month = ? AND is_oneoff = 0 AND is_fixed = 0
                     AND category IS NOT NULL
                   GROUP BY category""",
                (month,),
            )
        }
        extras = conn.execute(
            """SELECT
                 ROUND(COALESCE(SUM(CASE WHEN category IS NULL AND is_oneoff = 0
                                          AND is_fixed = 0 THEN amount END), 0), 2) AS uncategorized,
                 ROUND(COALESCE(SUM(CASE WHEN is_oneoff = 1 THEN amount END), 0), 2) AS oneoffs,
                 ROUND(COALESCE(SUM(CASE WHEN is_fixed = 1 THEN amount END), 0), 2) AS fixed
               FROM transactions WHERE month = ?""",
            (month,),
        ).fetchone()
        # One-offs stay out of the averages but are still listed, never hidden.
        oneoffs = [
            dict(r)
            for r in conn.execute(
                """SELECT date, description, amount, category FROM transactions
                   WHERE month = ? AND is_oneoff = 1 ORDER BY amount DESC""",
                (month,),
            )
        ]

    categories = []
    # Budgeted categories first, then any category that has spend but no target.
    for cat in list(targets) + [c for c in actuals if c not in targets]:
        target = targets.get(cat, 0)
        actual = actuals.get(cat, 0)
        variance = round((actual - target) / target * 100, 1) if target else None
        categories.append(
            {
                "category": cat,
                "target": target,
                "actual": actual,
                "variance_pct": variance,
                "over_target": variance is not None and variance > OVER_TARGET_PCT,
            }
        )

    variable_total = round(sum(actuals.values()) + extras["uncategorized"], 2)
    return {
        "month": month,
        "categories": categories,
        "variable_total": variable_total,  # includes uncategorized spend
        "variable_target": sum(targets.values()),
        "uncategorized_total": extras["uncategorized"],
        "oneoff_total": extras["oneoffs"],
        "oneoffs": oneoffs,
        "fixed_on_card_total": extras["fixed"],
    }


@app.get("/transactions")
def transactions(month: str):
    _check_month(month)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, date, description, merchant_normalized, amount, category,
                      card_last4, is_oneoff, is_fixed
               FROM transactions WHERE month = ? ORDER BY date, id""",
            (month,),
        ).fetchall()
    return [
        {**dict(r), "is_oneoff": bool(r["is_oneoff"]), "is_fixed": bool(r["is_fixed"])}
        for r in rows
    ]
