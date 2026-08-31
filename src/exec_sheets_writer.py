"""
Writes the executive overview (Summary/Sales/Finance tabs + charts) to the
same Google Sheet the cash flow pipeline uses. Each tab is fully rewritten
on every run (unlike the cash-flow Register, this report is a point-in-time
snapshot, not an append-only ledger), and charts are deleted and recreated
each time so they don't pile up across daily runs.
"""

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class ExecSheetsWriter:
    def __init__(self, cfg):
        creds = Credentials.from_service_account_file(cfg.service_account_file, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(cfg.sheet_id)

    def _get_or_create_tab(self, title, cols=20, rows=500):
        try:
            ws = self.sheet.worksheet(title)
            ws.clear()
            if ws.col_count < cols:
                ws.add_cols(cols - ws.col_count)
        except gspread.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=title, rows=rows, cols=cols)
        return ws

    def _delete_charts(self, worksheet):
        meta = self.sheet.fetch_sheet_metadata()
        requests = []
        for sheet_meta in meta.get("sheets", []):
            if sheet_meta["properties"]["sheetId"] == worksheet.id:
                for chart in sheet_meta.get("charts", []):
                    requests.append({"deleteEmbeddedObject": {"objectId": chart["chartId"]}})
        if requests:
            self.sheet.batch_update({"requests": requests})

    def _write_block(self, worksheet, top_left_row, top_left_col, rows):
        """rows: list of lists (first row treated as header, just formatting-wise).
        Uses RAW input so pre-formatted display text (e.g. leading '+' or a
        thousands-separator comma) is never misread as a broken formula by
        Sheets' value parser; plain numeric/int/float values are still stored
        as real numbers regardless of this setting."""
        if not rows:
            return
        end_row = top_left_row + len(rows) - 1
        end_col = top_left_col + len(rows[0]) - 1
        a1 = gspread.utils.rowcol_to_a1(top_left_row, top_left_col) + ":" + gspread.utils.rowcol_to_a1(end_row, end_col)
        worksheet.update(a1, rows, value_input_option="RAW")

    def _range(self, worksheet, start_row, end_row, col):
        """0-indexed GridRange source for one column, rows [start_row, end_row) as 1-indexed-input helper.
        start_row/end_row here are 1-indexed sheet rows (as used by _write_block); converted to the
        0-indexed, end-exclusive convention the Sheets API expects."""
        return {
            "sheetId": worksheet.id,
            "startRowIndex": start_row - 1,
            "endRowIndex": end_row,
            "startColumnIndex": col - 1,
            "endColumnIndex": col,
        }

    def add_line_chart(self, worksheet, title, data_top_row, data_row_count, domain_col, series_cols, anchor_row, anchor_col):
        end_row = data_top_row + data_row_count
        requests = [{
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "basicChart": {
                            "chartType": "LINE",
                            "legendPosition": "BOTTOM_LEGEND",
                            "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
                            "domains": [{"domain": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, domain_col)]}}}],
                            "series": [
                                {"series": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, col)]}}, "targetAxis": "LEFT_AXIS"}
                                for col in series_cols
                            ],
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {"sheetId": worksheet.id, "rowIndex": anchor_row - 1, "columnIndex": anchor_col - 1},
                            "widthPixels": 640, "heightPixels": 360,
                        }
                    },
                }
            }
        }]
        self.sheet.batch_update({"requests": requests})

    def add_column_chart(self, worksheet, title, data_top_row, data_row_count, domain_col, series_cols, anchor_row, anchor_col, stacked=False):
        end_row = data_top_row + data_row_count
        requests = [{
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "basicChart": {
                            "chartType": "COLUMN",
                            "stackedType": "STACKED" if stacked else "NOT_STACKED",
                            "legendPosition": "BOTTOM_LEGEND",
                            "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
                            "domains": [{"domain": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, domain_col)]}}}],
                            "series": [
                                {"series": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, col)]}}, "targetAxis": "LEFT_AXIS"}
                                for col in series_cols
                            ],
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {"sheetId": worksheet.id, "rowIndex": anchor_row - 1, "columnIndex": anchor_col - 1},
                            "widthPixels": 640, "heightPixels": 360,
                        }
                    },
                }
            }
        }]
        self.sheet.batch_update({"requests": requests})

    def add_pie_chart(self, worksheet, title, data_top_row, data_row_count, label_col, value_col, anchor_row, anchor_col):
        end_row = data_top_row + data_row_count
        requests = [{
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "pieChart": {
                            "legendPosition": "RIGHT_LEGEND",
                            "domain": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, label_col)]}},
                            "series": {"sourceRange": {"sources": [self._range(worksheet, data_top_row, end_row, value_col)]}},
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {"sheetId": worksheet.id, "rowIndex": anchor_row - 1, "columnIndex": anchor_col - 1},
                            "widthPixels": 480, "heightPixels": 360,
                        }
                    },
                }
            }
        }]
        self.sheet.batch_update({"requests": requests})

    def write_summary(self, narrative, key_figures_rows, caveats):
        ws = self._get_or_create_tab("Summary", cols=6, rows=40)
        self._delete_charts(ws)
        self._write_block(ws, 1, 1, [[narrative]])
        ws.merge_cells("A1:F4")
        ws.format("A1:F4", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"})

        header = ["Metric", "Today", "Yesterday", "Change", "Notes"]
        self._write_block(ws, 6, 1, [header] + key_figures_rows)
        # Revenue/Cash Flow/Overdue Receivables are money (2dp); New/Delayed Orders are counts (integer).
        ws.format("B7:D7", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}})
        ws.format("B8:D9", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
        ws.format(f"B10:D{6 + len(key_figures_rows)}", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}})

        if caveats:
            caveat_rows = [["Data caveats"]] + [[c] for c in caveats]
            start = 6 + len(key_figures_rows) + 3
            self._write_block(ws, start, 1, caveat_rows)
            ws.merge_cells(f"A{start}:F{start}")

    def write_sales(self, orders_rows, revenue_trend_rows, status_rows, top_customer_rows):
        ws = self._get_or_create_tab("Sales", cols=20, rows=max(60, len(orders_rows) + 10))
        self._delete_charts(ws)

        header = ["Order", "Customer", "Order Date", "Amount", "Odoo State", "Delivery Status", "Commitment Date", "Status"]
        self._write_block(ws, 1, 1, [header] + orders_rows)

        trend_header = ["Date", "Revenue"]
        self._write_block(ws, 1, 10, [trend_header] + revenue_trend_rows)

        status_header = ["Status", "Count"]
        self._write_block(ws, 1, 13, [status_header] + status_rows)

        cust_header = ["Customer", "Total (30d)"]
        self._write_block(ws, 1, 16, [cust_header] + top_customer_rows)

        chart_row = max(len(orders_rows), len(revenue_trend_rows), len(status_rows), len(top_customer_rows)) + 4
        self.add_line_chart(ws, "Revenue Trend (30 days)", 2, len(revenue_trend_rows), domain_col=10, series_cols=[11], anchor_row=chart_row, anchor_col=1)
        self.add_pie_chart(ws, "Order Status Breakdown", 2, len(status_rows), label_col=13, value_col=14, anchor_row=chart_row, anchor_col=10)

    def write_finance(self, open_items_rows, cash_trend_rows, aging_rows):
        ws = self._get_or_create_tab("Finance", cols=20, rows=max(60, len(open_items_rows) + 10))
        self._delete_charts(ws)

        header = ["Type", "Reference", "Counterparty", "Due Date", "Amount", "Aging Bucket"]
        self._write_block(ws, 1, 1, [header] + open_items_rows)

        trend_header = ["Date", "Net Cash Flow", "Running Balance"]
        self._write_block(ws, 1, 9, [trend_header] + cash_trend_rows)

        aging_header = ["Aging Bucket", "Receivables", "Payables"]
        self._write_block(ws, 1, 13, [aging_header] + aging_rows)

        chart_row = max(len(open_items_rows), len(cash_trend_rows), len(aging_rows)) + 4
        self.add_line_chart(ws, "Cash Flow / Balance Trend (30 days)", 2, len(cash_trend_rows), domain_col=9, series_cols=[11], anchor_row=chart_row, anchor_col=1)
        self.add_column_chart(ws, "Receivables / Payables Aging", 2, len(aging_rows), domain_col=13, series_cols=[14, 15], anchor_row=chart_row, anchor_col=10)
