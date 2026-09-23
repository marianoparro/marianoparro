"""Step 3: the budget assistant (Claude + tools).

How it works — the "agentic loop":
  1. We send Claude the question, a system prompt with a compact snapshot of
     the budget (targets, month-by-month totals, 3-month averages), and a
     list of TOOLS it may call.
  2. If Claude needs detail ("break down my Amazon charges"), it replies with
     a tool call instead of an answer. We run that tool against SQLite and
     send the result back.
  3. Repeat until Claude answers in plain text (stop_reason == "end_turn").

Claude never touches the database directly: it can only ask for the tools
below, and the only one that writes is set_merchant_category (the same
save_rule() the dashboard uses).

Settings (environment variables, never in git):
  ANTHROPIC_API_KEY   required for the assistant; without it the chat says so
  ANTHROPIC_MODEL     default "claude-sonnet-4-6"
  HOUSEHOLD_NOTES     optional free text about the family the assistant
                      should know (kids, moves, what a merchant really is)
"""

import json
import os
import sqlite3
from datetime import date

import anthropic

from app import settings
from app.importer import save_rule
from app.seed_data import FIXED, ONEOFF

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOOL_ROUNDS = 8       # safety stop: a question never needs more than a few lookups
HISTORY_MESSAGES = 20     # how much of the conversation Claude sees each time
UNCATEGORIZED = "Uncategorized"


class AssistantUnavailable(Exception):
    """No API key configured, or the API couldn't be reached."""


# ---------------------------------------------------------------- data views

def _variable_filter(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return f"{p}is_oneoff = 0 AND {p}is_fixed = 0"


def monthly_table(conn: sqlite3.Connection) -> list[dict]:
    """Per month: variable total, per-category totals, one-offs, uncategorized."""
    months = {}
    for r in conn.execute(
        f"""SELECT month, COALESCE(category, '{UNCATEGORIZED}') AS cat, ROUND(SUM(amount), 2) AS total
            FROM transactions WHERE {_variable_filter()} GROUP BY month, cat"""
    ):
        months.setdefault(r["month"], {"categories": {}})["categories"][r["cat"]] = r["total"]
    for r in conn.execute(
        """SELECT month, ROUND(SUM(CASE WHEN is_oneoff = 1 THEN amount ELSE 0 END), 2) AS oneoffs,
                  MIN(date) AS first_date, MAX(date) AS last_date
           FROM transactions GROUP BY month"""
    ):
        m = months.setdefault(r["month"], {"categories": {}})
        m.update(oneoffs=r["oneoffs"], first_date=r["first_date"], last_date=r["last_date"])
    out = []
    for month in sorted(months):
        m = months[month]
        out.append({
            "month": month,
            "variable_total": round(sum(m["categories"].values()), 2),
            "categories": m["categories"],
            "oneoffs": m.get("oneoffs", 0),
            "data_from": m.get("first_date"),
            "data_to": m.get("last_date"),
        })
    return out


def _is_complete(row: dict) -> bool:
    """A month counts for averages only if the data covers (nearly) all of it."""
    first, last = row["data_from"], row["data_to"]
    if not first or not last:
        return False
    y, mo = map(int, row["month"].split("-"))
    days = (date(y + mo // 12, mo % 12 + 1, 1) - date(y, mo, 1)).days
    return int(first[8:]) <= 3 and int(last[8:]) >= days - 3


def rolling_averages(table: list[dict], n: int = 3) -> dict:
    """Average of the last n COMPLETE months, per category (one-offs excluded)."""
    complete = [r for r in table if _is_complete(r)][-n:]
    if not complete:
        return {"months": [], "variable_total": 0, "categories": {}}
    cats = {c for r in complete for c in r["categories"]}
    return {
        "months": [r["month"] for r in complete],
        "variable_total": round(sum(r["variable_total"] for r in complete) / len(complete), 2),
        "categories": {
            c: round(sum(r["categories"].get(c, 0) for r in complete) / len(complete), 2)
            for c in sorted(cats)
        },
    }


# -------------------------------------------------------------------- tools
# Each tool = a JSON schema Claude sees + a Python function we run.

TOOLS = [
    {
        "name": "query_transactions",
        "description": (
            "List individual card transactions with filters. Use for breakdowns "
            "('break down my Amazon charges'), finding what drove a category, or "
            "checking specific charges. Amounts are USD; positive = spent, negative = refund. "
            "Returns matching rows (up to `limit`) plus the count and total of ALL matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_month": {"type": "string", "description": "First month, YYYY-MM (inclusive)."},
                "end_month": {"type": "string", "description": "Last month, YYYY-MM (inclusive). Same as start_month for one month."},
                "category": {
                    "type": "string",
                    "description": f"A budget category, '{UNCATEGORIZED}', '{ONEOFF}' or '{FIXED}'. Omit for all.",
                },
                "search": {"type": "string", "description": "Case-insensitive text to find in the description, e.g. 'amazon' or 'rappi'."},
                "min_amount": {"type": "number", "description": "Only charges at least this large."},
                "exclude_oneoffs": {"type": "boolean", "description": "Drop one-off charges (default false)."},
                "sort": {"type": "string", "enum": ["date", "amount"], "description": "Newest first, or largest first (default amount)."},
                "limit": {"type": "integer", "description": "Max rows to return, 1-200 (default 40)."},
            },
        },
    },
    {
        "name": "top_merchants",
        "description": (
            "Total spend per merchant (cleaned merchant name), largest first. Best first "
            "step for 'why is X so high' or 'where does our money go'. One-offs and fixed "
            "costs are excluded unless a category of 'One-off'/'Fixed' is given."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_month": {"type": "string", "description": "YYYY-MM (inclusive)."},
                "end_month": {"type": "string", "description": "YYYY-MM (inclusive)."},
                "category": {"type": "string", "description": "Limit to one category (or 'Uncategorized')."},
                "limit": {"type": "integer", "description": "How many merchants, 1-50 (default 15)."},
            },
            "required": ["start_month", "end_month"],
        },
    },
    {
        "name": "set_merchant_category",
        "description": (
            "Save a rule so every transaction whose description contains `pattern` gets "
            "`category`, in all past and future months. ONLY call this when the user "
            "explicitly tells you what a merchant is or asks you to recategorize it — "
            "never on your own guess. Pick the shortest pattern that identifies the "
            "merchant without catching others (e.g. 'CAFE NIDDO', not 'CAFE')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text found in the bank description."},
                "category": {"type": "string", "description": f"A budget category, '{ONEOFF}' or '{FIXED}'."},
            },
            "required": ["pattern", "category"],
        },
    },
]


def _month_range(args: dict) -> tuple[str, str]:
    """(start, end) inclusive; a lone start means that one month; none means all."""
    start, end = args.get("start_month"), args.get("end_month")
    return start or "0000-00", end or start or "9999-99"


def _category_clause(category: str | None, params: list) -> str:
    if not category:
        return ""
    if category == UNCATEGORIZED:
        return " AND category IS NULL"
    params.append(category)
    return " AND category = ?"


def tool_query_transactions(conn: sqlite3.Connection, args: dict) -> dict:
    start, end = _month_range(args)
    params: list = [start, end]
    where = "month BETWEEN ? AND ?" + _category_clause(args.get("category"), params)
    if args.get("search"):
        where += " AND UPPER(description) LIKE ?"
        params.append(f"%{args['search'].upper()}%")
    if args.get("min_amount") is not None:
        where += " AND amount >= ?"
        params.append(args["min_amount"])
    if args.get("exclude_oneoffs"):
        where += " AND is_oneoff = 0"
    (count, total) = conn.execute(
        f"SELECT COUNT(*), ROUND(COALESCE(SUM(amount), 0), 2) FROM transactions WHERE {where}", params
    ).fetchone()
    order = "date DESC, amount DESC" if args.get("sort") == "date" else "amount DESC"
    limit = max(1, min(int(args.get("limit") or 40), 200))
    rows = conn.execute(
        f"""SELECT date, description, amount, category, is_oneoff, is_fixed
            FROM transactions WHERE {where} ORDER BY {order} LIMIT ?""",
        params + [limit],
    ).fetchall()
    return {
        "match_count": count,
        "match_total": total,
        "rows_shown": len(rows),
        "rows": [
            {
                "date": r["date"], "description": r["description"], "amount": r["amount"],
                "category": r["category"] or UNCATEGORIZED,
                **({"one_off": True} if r["is_oneoff"] else {}),
                **({"fixed": True} if r["is_fixed"] else {}),
            }
            for r in rows
        ],
    }


def tool_top_merchants(conn: sqlite3.Connection, args: dict) -> dict:
    start, end = _month_range(args)
    params: list = [start, end]
    category = args.get("category")
    where = "month BETWEEN ? AND ?" + _category_clause(category, params)
    if category not in (ONEOFF, FIXED):
        where += " AND " + _variable_filter()
    limit = max(1, min(int(args.get("limit") or 15), 50))
    rows = conn.execute(
        f"""SELECT merchant_normalized AS merchant, COUNT(*) AS charges,
                   ROUND(SUM(amount), 2) AS total, COALESCE(category, '{UNCATEGORIZED}') AS category
            FROM transactions WHERE {where}
            GROUP BY merchant_normalized ORDER BY total DESC LIMIT ?""",
        params + [limit],
    ).fetchall()
    return {"merchants": [dict(r) for r in rows]}


def tool_set_merchant_category(conn: sqlite3.Connection, args: dict) -> dict:
    return save_rule(conn, args["pattern"], args["category"])


TOOL_FUNCTIONS = {
    "query_transactions": tool_query_transactions,
    "top_merchants": tool_top_merchants,
    "set_merchant_category": tool_set_merchant_category,
}


def run_tool(conn: sqlite3.Connection, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool; return (JSON text, is_error). Errors go back to Claude, not the user."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"Unknown tool {name}", True
    try:
        return json.dumps(fn(conn, args)), False
    except (ValueError, KeyError, TypeError, sqlite3.Error) as e:
        return f"Error: {e}", True


# ------------------------------------------------------------- the prompt

INSTRUCTIONS = """You are the family budget assistant for a household that tracks its credit-card spending in this app. You answer questions from the family members about their spending.

How the numbers work:
- Card transactions come from Chase CSV exports. Amounts are USD; positive = spent, negative = refund.
- "Variable spend" = card spending excluding one-offs and fixed costs. It is compared against monthly targets per category.
- One-offs (moves, immigration fees, large medical bills over $100, one-time purchases) are recorded but excluded from averages and targets. Mention them separately when relevant.
- Fixed costs (rent, school, insurance, etc.) are paid outside the card or tagged Fixed; they are not variable spend.
- "Taxes" is a monthly set-aside for the year-end tax bill, never a card charge. Its actual is always $0; don't call it underspent.
- Months marked partial (data starts mid-month, or the month is still in progress) are not comparable to full months. Say so when they come up.
- Averages below use the last 3 complete months.

How to answer:
- Lead with the answer and the key numbers, then the 2-4 drivers behind it. Keep it short: this is read on a phone.
- Use tools to look up specifics rather than guessing; the snapshot below only has monthly totals. Quote real merchants and amounts.
- When a category is over target, say by how much and name the merchants that drove it.
- Round to whole dollars. Use plain text with short "- " bullet lists; **bold** is fine; no tables or headings.
- If the data can't answer the question, say what's missing.
- Only change a merchant's category (set_merchant_category) when the user explicitly corrects one or asks you to. Tell them what you changed and how many transactions moved."""


def build_system(conn: sqlite3.Connection) -> list[dict]:
    table = monthly_table(conn)
    targets = {r["category"]: r["monthly_target"] for r in conn.execute(
        "SELECT category, monthly_target FROM budget_targets")}
    snapshot = {
        "today": date.today().isoformat(),
        "monthly_targets": targets,
        "monthly_variable_target_total": sum(targets.values()),
        "plan": settings.plan(),
        "months": [{**r, "complete": _is_complete(r)} for r in table],
        "average_last_3_complete_months": rolling_averages(table),
        "categories": sorted(targets) + [ONEOFF, FIXED, UNCATEGORIZED],
    }
    notes = os.environ.get("HOUSEHOLD_NOTES", "").strip()
    text = INSTRUCTIONS
    if notes:
        text += "\n\nAbout this household (from the family):\n" + notes
    # ~1.5k tokens per question in total: small enough that prompt caching
    # wouldn't kick in, so we keep this simple and send it fresh each time.
    return [
        {"type": "text", "text": text},
        {"type": "text", "text": "Budget snapshot (JSON):\n" + json.dumps(snapshot)},
    ]


# --------------------------------------------------------------- the loop

def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AssistantUnavailable("The assistant isn't set up yet: add ANTHROPIC_API_KEY on the server.")
    return anthropic.Anthropic()


def ask(conn: sqlite3.Connection, history: list[dict], question: str, client=None) -> dict:
    """Answer one question. `history` is prior [{role, content}] text turns."""
    client = client or _client()
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    system = build_system(conn)
    messages = [*history, {"role": "user", "content": question}]
    tools_used: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},  # chat: good answers without overthinking
                messages=messages,
            )
        except anthropic.AuthenticationError:
            raise AssistantUnavailable("The API key was rejected. Check ANTHROPIC_API_KEY.")
        except anthropic.RateLimitError:
            raise AssistantUnavailable("Too many requests right now. Try again in a minute.")
        except anthropic.APIStatusError as e:
            raise AssistantUnavailable(f"The assistant hit an error ({e.status_code}). Try again.")
        except anthropic.APIConnectionError:
            raise AssistantUnavailable("Couldn't reach the assistant. Check the connection and retry.")

        if response.stop_reason == "tool_use":
            # Keep Claude's whole turn (incl. thinking + tool_use blocks), then
            # answer every tool call in ONE user message.
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    tools_used.append(block.name)
                    content, is_error = run_tool(conn, block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "refusal":
            return {"reply": "I can't help with that one.", "tools_used": tools_used}
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n(Answer cut short — ask me to continue.)"
        return {"reply": text or "I don't have an answer for that.", "tools_used": tools_used}

    return {"reply": "That took too many lookups. Try a narrower question (a month or a category).",
            "tools_used": tools_used}
