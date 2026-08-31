# Ultima Industrial — Cash Flow Pipeline

Daily automated cash flow report: pulls the BluBanca transaction feed and
open payables/receivables both from Odoo, categorizes transactions, and
writes a register + category roll-up + upcoming list + forecast to a
Google Sheet.

Bank data comes straight from Odoo's own bank feed (Accounting > Bank &
Cash), not a separate aggregator — Odoo already has a live connection to
the BluBanca account, so there's no second provider to integrate.

## One-time setup

### 1. Odoo
1. Confirm the Odoo subscription includes XML-RPC/JSON-RPC external API
   access (typically the Custom plan — Standard and One App Free exclude it).
2. Generate an API key: Odoo user avatar > My Profile > Account Security >
   API Keys.
3. Note the database name (`ODOO_DB`) and instance URL (`ODOO_URL`).
4. Set `ODOO_URL`/`ODOO_DB`/`ODOO_USERNAME`/`ODOO_API_KEY` in `.env`, then run:
   ```bash
   python scripts/list_odoo_bank_journals.py
   ```
   This lists every bank journal with its `sync_source`. Pick the one that's
   the BluBanca account and confirm `sync_source` is `online_sync` (a live
   feed) rather than `manual`/`file_import` (someone re-entering data by
   hand — if it says that, the pipeline would just be automating a stale
   feed, worth flagging before relying on it). Put its `id` in
   `ODOO_BANK_JOURNAL_ID`.

### 2. Google Sheets
1. Create a Google Cloud service account with Sheets API access, download
   its JSON key.
2. Create the target Google Sheet and share it (Editor access) with the
   service account's `client_email`.
3. Note the sheet ID from its URL.

### 3. Starting balance (first run only)
The Register's running balance continues from the last row already in the
sheet. On the very first run, with no prior rows, seed it with the actual
BluBanca balance as of a known date: set `STARTING_BALANCE_AMOUNT` and
`STARTING_BALANCE_DATE` in `.env`. `INITIAL_LOOKBACK_DAYS` (default 90)
caps how far back transactions are pulled on that first run.

### 4. Categorization
- Default keyword rules live in `config/categories.yaml`.
- Recurring counterparties that don't fit a keyword pattern go in
  `config/counterparty_map.yaml` (exact name → category), which always wins
  over the keyword rules.

## Local run

```bash
cp .env.example .env   # fill in real values
pip install -r requirements.txt
python main.py
```

## Scheduled run

`.github/workflows/daily_cashflow.yml` runs the pipeline daily at 06:00 UTC.
Add these as GitHub repo secrets (Settings > Secrets and variables > Actions):

- `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY`, `ODOO_BANK_JOURNAL_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the full service account JSON key content)
- `GOOGLE_SHEET_ID`
- `STARTING_BALANCE_AMOUNT`, `STARTING_BALANCE_DATE` (only needed until the
  first successful run populates the sheet; harmless to leave set after)

## How it works

- **Register**: every Odoo bank statement line (`account.bank.statement.line`)
  for the configured journal, categorized, with a running balance that
  continues from the last row already in the sheet (or the configured
  starting balance on the first run) — so it doesn't depend on Odoo exposing
  an authoritative "current balance" field.
- **Category Rollup**: sum of all in-scope transaction amounts per category
  (recomputed in full each run).
- **Upcoming**: open Odoo vendor bills (payables) and customer invoices
  (receivables) with due dates, from `account.move` where `state = posted`
  and `payment_state` is `not_paid` or `partial`.
- **Forecast**: latest running balance + net upcoming (receivables − payables).
