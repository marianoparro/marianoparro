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

from app import assistant, settings
from app.auth import require_login
from app.db import get_conn, init_db
from app.importer import card_from_filename, import_rows, parse_chase_csv, recategorize, save_rule
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


@app.get("/config")
def config(request: Request):
    """Income and fixed costs come from environment variables, not code,
    so they never end up in the (public) git repo. Unset -> null, and the
    frontend simply hides the income/fixed/savings tiles."""
    with get_conn() as conn:
        categories = [r["category"] for r in conn.execute("SELECT category FROM budget_targets")]
        tax = conn.execute(
            "SELECT monthly_target FROM budget_targets WHERE category = 'Taxes'"
        ).fetchone()
    return {
        "user": request.state.user,  # who's logged in (None when login is off)
        # The chat is optional (it's the only part that costs money): the page
        # hides it entirely when no API key is set.
        "assistant_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "categories": categories + [ONEOFF, FIXED],
        **settings.plan(),  # income, fixed costs (itemized), savings goal
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
    """Teach the app a merchant. The assistant uses the same save_rule()."""
    with get_conn() as conn:
        try:
            return save_rule(conn, rule.pattern, rule.category)
        except ValueError as e:
            raise HTTPException(400, str(e))


# ---------- Step 3: the assistant ----------

class ChatQuestion(BaseModel):
    message: str


def _chat_user(request: Request) -> str:
    # Each person gets their own conversation. With login off (local dev),
    # everyone shares one called "family".
    return request.state.user or "family"


@app.get("/chat")
def chat_history(request: Request):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT role, content, created_at FROM chat_messages WHERE "user" = ? ORDER BY id',
            (_chat_user(request),),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/chat")
def chat(request: Request, question: ChatQuestion):
    text = question.message.strip()
    if not text:
        raise HTTPException(400, "message can't be empty")
    user = _chat_user(request)
    with get_conn() as conn:
        # Claude sees the recent conversation as plain text turns (not the old
        # tool calls), so follow-ups like "and last month?" work.
        history = [
            {"role": r["role"], "content": r["content"]}
            for r in conn.execute(
                """SELECT role, content FROM (
                     SELECT id, role, content FROM chat_messages WHERE "user" = ?
                     ORDER BY id DESC LIMIT ?) AS recent ORDER BY id""",
                (user, assistant.HISTORY_MESSAGES),
            )
        ]
        if history and history[0]["role"] == "assistant":
            history = history[1:]  # a conversation must start with the user
        try:
            result = assistant.ask(conn, history, text)
        except assistant.AssistantUnavailable as e:
            raise HTTPException(503, str(e))
        conn.executemany(
            'INSERT INTO chat_messages ("user", role, content) VALUES (?, ?, ?)',
            [(user, "user", text), (user, "assistant", result["reply"])],
        )
    return result


@app.delete("/chat")
def clear_chat(request: Request):
    with get_conn() as conn:
        conn.execute('DELETE FROM chat_messages WHERE "user" = ?', (_chat_user(request),))
    return {"cleared": True}


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
