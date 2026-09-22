# Family Budget Tracker

Personal budget tracker for Mariano + Maura (CDMX). FastAPI + SQLite, with an
HTML/JS frontend and a Claude-powered assistant coming in later steps.

## Step 1: CSV importer + database

```
app/
  seed_data.py   budget targets + merchant→category map (edit this to re-categorize)
  db.py          SQLite schema, connection, seeding on startup
  categorize.py  merchant name cleanup + pattern matching
  importer.py    Chase CSV parsing, dedup, insert
  main.py        API routes
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

pytest   # run the tests
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
- **Auto-surfaced on import:** every one-off plus any single charge over $200
  (the `flagged` list in the `/import` response).

Set `DB_PATH` to choose where the SQLite file lives (default `./budget.db`).
Bank CSVs and `.db` files are gitignored. Don't commit real financial data.
