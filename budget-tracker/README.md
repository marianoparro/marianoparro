# Family Budget Tracker

Personal budget tracker for Mariano + Maura (CDMX). FastAPI + SQLite, with an
HTML/JS frontend and a Claude-powered assistant coming in later steps.

## Step 2: Web dashboard

Start the server (below) and open http://127.0.0.1:8000. One page, plain
HTML/CSS/JS in `app/static/` (no build step):

- **Tiles, in money-flow order:** income − fixed − variable − one-offs =
  saved, each with its yearly figure underneath
- **Where the money goes:** the same flow line by line, fixed costs itemized,
  month and per-year columns. Per year = month × 12, except one-offs, which
  use what was actually paid this year (one movers bill isn't twelve).
- **Trend chart:** monthly variable spend vs target; hover for detail, click
  a bar to open that month. Striped bars with `*` are partial months.
- **By category:** spent (bar) vs target (tick); click a row to filter
- **Transactions:** change a category → it saves a *rule* for that merchant,
  so every month (past and future) is fixed at once
- **Import:** drag and drop Chase CSVs

The savings tile needs income and fixed costs. These are read from
environment variables so they never land in git:

```bash
export MONTHLY_NET_INCOME=...  FIXED_COSTS="Rent:2500,..."  YEARLY_SAVINGS_GOAL=...
```

Saved = income − fixed costs − variable spend − tax set-aside − one-offs.

## Step 3: Budget assistant (Claude)

The "Ask about your spending" card sends questions to Claude
(`claude-sonnet-4-6`; change with `ANTHROPIC_MODEL`). Code: `app/assistant.py`.

- **Context each question:** targets, income/fixed plan, every month's totals
  by category, and 3-month averages over *complete* months (partial months
  are flagged, not averaged). About 1.5k tokens.
- **Tools Claude can call** (run by our server against SQLite):
  `query_transactions` (filters: months, category, text, min amount),
  `top_merchants` (spend per merchant), and `set_merchant_category`, which
  saves a rule exactly like the dashboard does, *only* when you correct it.
- **Conversations** are saved per person in `chat_messages`; Claude sees the
  last 20 messages so follow-ups work. "New conversation" clears yours.
- **After an import**, "Ask the assistant about these" sends the flagged
  charges (one-offs and anything over $200) for a review.
- **Cost:** roughly 1-2 cents per question.
- **Optional.** It's the only part that costs money. Without
  `ANTHROPIC_API_KEY` the chat is simply hidden and everything else works.

`HOUSEHOLD_NOTES` (optional) is free text the assistant always knows, e.g.
who Mateo is or that Acuatic is swim lessons. Kept out of git like income.

## Deploy for free (Render + Neon) and share with the family

Two free services: **Render** runs the app, **Neon** stores the data
(Render's free plan wipes files on restart, so the data can't live there).
Free-tier terms are the providers' to change; check them when you sign up.
Trade-off of free: the app sleeps when unused, so the first open after a
while takes ~30-60 seconds.

1. **Neon** (neon.tech): sign up → create a project → copy the connection
   string (`postgresql://...`).
2. **Render** (render.com): sign up with GitHub → **New → Blueprint** → this
   repo and branch. It reads `render.yaml` and asks for:

| Variable | Example | Purpose |
|---|---|---|
| `DATABASE_URL` | the Neon connection string | where the data lives |
| `APP_USERS` | `Mariano:long-pass-1,Maura:long-pass-2` | who can log in |
| `MONTHLY_NET_INCOME` | `15293` | income tile |
| `FIXED_COSTS` | `Rent:2500,Domestic help:1561,Montessori:883,...` | fixed costs, itemized |
| `YEARLY_SAVINGS_GOAL` | `50000` | savings vs goal |
| `ANTHROPIC_API_KEY` | leave empty | **optional, paid** (~1-2¢/question): turns on the chat assistant |
| `HOUSEHOLD_NOTES` | leave empty | optional, only used by the assistant |

3. **Apply** → you get an `https://….onrender.com` link. Open it, log in,
   drag in your Chase CSVs.
4. Send Maura the link + her password (separately). On her phone: open it,
   log in once, then Share → **Add to Home Screen**.

Add a person or change a password: edit `APP_USERS` in Render (redeploys in
about a minute). No code change.

(`MONTHLY_FIXED_COSTS=5645` still works if you'd rather give one total with
no breakdown.) Locally, with no `DATABASE_URL`, the app uses a SQLite file
(`DB_PATH`, default `./budget.db`).

## Step 1: CSV importer + database

```
app/
  seed_data.py   budget targets + merchant→category map (edit this to re-categorize)
  db.py          schema + connection: SQLite locally, Postgres (DATABASE_URL) when deployed
  categorize.py  merchant name cleanup + pattern matching
  importer.py    Chase CSV parsing, dedup, insert
  main.py        API routes + serves the dashboard
  auth.py        family login (APP_USERS env var)
  settings.py    income / fixed costs / goal from env vars
  assistant.py   Step 3: Claude + tools over your data
  static/        the dashboard: index.html, style.css, app.js
tests/           pytest, synthetic data only
```

### Run it

```bash
cd budget-tracker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs to try each endpoint in the browser.

```bash
# import one or more Chase exports (card number comes from the filename)
curl -F "files=@Chase6297_Activity20260324_20260423.CSV" http://127.0.0.1:8000/import

curl http://127.0.0.1:8000/summary/2026-04
curl "http://127.0.0.1:8000/transactions?month=2026-04"

# teach it a merchant; every stored transaction is re-categorized
curl -X POST http://127.0.0.1:8000/merchant-map -H 'Content-Type: application/json' \
     -d '{"pattern": "CAFE NIDDO", "category": "Food & Dining"}'

pytest   # run the tests (SQLite)
TEST_DATABASE_URL=postgresql://... pytest   # same tests on Postgres (wipes that DB)
```

### Rules the importer follows

- **Sign flip:** Chase shows purchases as negative. Here `amount > 0` is money
  spent and `amount < 0` is a refund, so a category total is just `SUM(amount)`.
- **Card payments are skipped** (`Type = Payment`). They move money to the card;
  they aren't spending.
- **Dedup counts copies.** Two $5.24 Uber rides on the same day are both real.
  If a file has N copies of (date, description, amount) and the DB already has
  M, the importer inserts N − M. Re-importing a file, or importing overlapping
  date ranges, adds nothing twice.
- **Matching:** case-insensitive substring, spacing around `*` ignored; if
  several patterns match, the longest wins (`AMZN DIGITAL` over `AMZN`).
- **Flags:** `One-off` patterns set `is_oneoff`; `Fixed` patterns (Auna,
  Totalplay) set `is_fixed`. Both are left out of variable totals in `/summary`.
  A category is `over_target` when it is more than 30% above target.
- **Categories follow the map.** On startup and after `POST /merchant-map`,
  every stored transaction is re-matched, so a map change fixes history too.
  Patterns you add (`added_by='user'`) are never overwritten by the seed list.
- **Auto-surfaced on import:** every one-off plus any single charge over $200
  (the `flagged` list in the `/import` response).

Set `DB_PATH` to choose where the SQLite file lives (default `./budget.db`).
Bank CSVs and `.db` files are gitignored. Don't commit real financial data.
