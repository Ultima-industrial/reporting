"""
Writes the cash flow report to a live Google Sheet across four tabs:
Register, Category Rollup, Upcoming, Forecast.

Dedup strategy: the sheet itself is the source of truth for which
transactions have already been recorded. Each run reads existing
transaction IDs from the Register tab and only appends new ones — no
separate local state file is needed, which matters for a stateless daily
GitHub Actions run.
"""

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REGISTER_HEADER = ["Transaction ID", "Date", "Counterparty", "Description", "Category", "Amount", "Running Balance"]
ROLLUP_HEADER = ["Category", "Total"]
UPCOMING_HEADER = ["Type", "Reference", "Counterparty", "Due Date", "Amount"]
FORECAST_HEADER = ["Metric", "Value"]


class SheetsWriter:
    def __init__(self, cfg):
        creds = Credentials.from_service_account_file(cfg.service_account_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(cfg.sheet_id)

    def _get_or_create_tab(self, title, header):
        try:
            ws = self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=len(header))
            ws.append_row(header)
        return ws

    def existing_transaction_ids(self):
        ws = self._get_or_create_tab("Register", REGISTER_HEADER)
        rows = ws.get_all_values()[1:]
        return {row[0] for row in rows if row}

    def last_register_row(self):
        """Returns the last data row in the Register tab as a list of strings,
        or None if the tab has no transactions recorded yet. Used to continue
        the running balance across runs without a separate state file."""
        ws = self._get_or_create_tab("Register", REGISTER_HEADER)
        rows = ws.get_all_values()[1:]
        rows = [row for row in rows if row]
        return rows[-1] if rows else None

    def append_register_rows(self, rows):
        """rows: list of [txn_id, date, counterparty, description, category, amount, running_balance]"""
        if not rows:
            return
        ws = self._get_or_create_tab("Register", REGISTER_HEADER)
        ws.append_rows(rows, value_input_option="USER_ENTERED")

    def write_category_rollup(self, totals_by_category):
        ws = self._get_or_create_tab("Category Rollup", ROLLUP_HEADER)
        ws.resize(rows=1)
        ws.update("A1", [ROLLUP_HEADER])
        rows = [[category, total] for category, total in sorted(totals_by_category.items())]
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

    def write_upcoming(self, upcoming_rows):
        """upcoming_rows: list of [type, reference, counterparty, due_date, amount]"""
        ws = self._get_or_create_tab("Upcoming", UPCOMING_HEADER)
        ws.resize(rows=1)
        ws.update("A1", [UPCOMING_HEADER])
        if upcoming_rows:
            ws.append_rows(upcoming_rows, value_input_option="USER_ENTERED")

    def write_forecast(self, latest_balance, net_upcoming, forecast_balance):
        ws = self._get_or_create_tab("Forecast", FORECAST_HEADER)
        ws.resize(rows=1)
        ws.update("A1", [FORECAST_HEADER])
        ws.append_rows(
            [
                ["Latest Balance", latest_balance],
                ["Net Upcoming (In - Out)", net_upcoming],
                ["End-of-Period Forecast", forecast_balance],
            ],
            value_input_option="USER_ENTERED",
        )
