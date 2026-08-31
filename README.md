# Ultima Industrial — Reporting Pipelines

Three independent daily jobs, all reading the same Odoo instance (read-only):

1. **Cash flow pipeline** (`main.py`) — a detailed, append-only transaction
   register with running balance, category roll-up, upcoming payables/
   receivables, and a simple forecast. Writes to the Google Sheet
   "Ultimate reporting".
2. **Executive report** (`main_exec.py`) — a daily snapshot overview for
   leadership: a narrative summary, today-vs-yesterday key figures, sales
   pipeline health (new orders, delayed orders, revenue trend), and finance
   health (cash trend, receivables/payables aging), each with charts. Also
   writes to "Ultimate reporting" (separate tabs).
3. **Dashboard website** (`main_dashboard.py`) — the same executive snapshot
   as a standalone public-facing website ("Ultima Pulse"), for sharing with
   people who shouldn't have Google Sheet access — e.g. external
   shareholders. Deployed to Netlify.

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

### 5. Netlify (dashboard website only)
1. Create a free account at netlify.com (or reuse an existing one).
2. Create a new empty site — "Add new site" → any option that doesn't
   require connecting a git repo yet is fine, since deployment happens via
   Netlify's Deploy API directly from Python (`src/netlify_deploy.py`), not
   Netlify's own git integration or the Node-based CLI.
   Note the **Site ID** (Site settings → General → Site details).
3. Generate a Personal Access Token: User settings → Applications →
   Personal access tokens → New access token.
4. Set `NETLIFY_AUTH_TOKEN` (the token) and `NETLIFY_SITE_ID` in `.env` for
   a local test deploy, and as GitHub repo secrets for the scheduled job.

## Local run

```bash
cp .env.example .env   # fill in real values
pip install -r requirements.txt
python main.py             # cash flow register
python main_exec.py        # executive report
python main_dashboard.py   # generates site/index.html — deploy separately, see below
```

## Scheduled run

Both workflows use the same GitHub repo secrets (Settings > Secrets and
variables > Actions) — no extra setup needed for the second one:

- `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY`, `ODOO_BANK_JOURNAL_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the full service account JSON key content)
- `GOOGLE_SHEET_ID`
- `STARTING_BALANCE_AMOUNT`, `STARTING_BALANCE_DATE` (only needed until the
  cash flow pipeline's Register has its first rows; harmless to leave set —
  the executive report also uses this as its running-balance anchor, since
  it recomputes the cash trend independently rather than reading the
  Register tab, so it works correctly regardless of which job runs first)

- `.github/workflows/daily_cashflow.yml` — 06:00 UTC
- `.github/workflows/daily_executive_report.yml` — 06:15 UTC
- `.github/workflows/daily_dashboard.yml` — 06:30 UTC, additionally needs
  `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` as repo secrets (this job
  doesn't touch Google Sheets at all, so none of the `GOOGLE_*` secrets are
  needed for it specifically)

## How it works

### Cash flow pipeline (`main.py` → Register / Category Rollup / Upcoming / Forecast tabs)
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

### Executive report (`main_exec.py` → Summary / Sales / Finance tabs)
Each run fully rewrites these three tabs (a point-in-time snapshot, not an
append-only ledger) and recreates their charts, sourced from `sale.order`
(Sales) and the same bank feed + open invoices/bills (Finance) as above.

- **Summary**: a data-driven narrative (built from the same figures below,
  not free-form text) plus a key-figures table — revenue, new orders,
  delayed orders, cash flow, overdue receivables — each vs. yesterday.
- **Sales**: order detail, a 30-day revenue trend (line chart), an order
  status breakdown of Quotation/In Progress/Delayed/Fulfilled (pie chart —
  "Delayed" means confirmed but past `commitment_date` and not fully
  delivered), and top customers by order value.
- **Finance**: open payables/receivables with an aging bucket, a 30-day
  cash trend (line chart: net daily flow + running balance), and a
  receivables-vs-payables aging bar chart.
- **Known data caveat** (also written into the Summary tab itself): the
  "Overdue Receivables — Yesterday" figure is approximated from today's
  open-invoice snapshot, since Odoo only exposes currently-open invoices,
  not a historical one — it slightly undercounts anything paid since
  yesterday. Margins/COGS are intentionally not included — not reliably
  derivable from current data without deeper product-cost analysis.

### Dashboard website (`main_dashboard.py` → `site/index.html`, deployed to Netlify)
Same underlying data and figures as the executive report, rendered as a
standalone static page (`templates/dashboard_template.html` + `src/dashboard.py`)
instead of Google Sheet tabs — no live connector pulls data into the page
itself, since there's no Odoo/Sheets connector available to a hosted page;
each deploy is a fresh snapshot baked in at generation time. This is the
one meant for sharing outside the company (e.g. shareholders), since it
doesn't require any Odoo or Google account access to view — whoever holds
the Netlify URL can see it.

### Reporting correction: `config/paid_overrides.yaml`
Vendor bills/customer invoices confirmed paid via the real bank statement
but not yet reconciled as paid in Odoo get excluded from every report via
this file (checked by `src/odoo_client.py`). This is a reporting-side
workaround — reconcile the actual payment in Odoo when convenient, and
remove the entry here once Odoo agrees.
