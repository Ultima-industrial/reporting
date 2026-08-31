from datetime import date

from . import exec_data
from .config import STARTING_BALANCE_AMOUNT, STARTING_BALANCE_DATE, OdooConfig, SheetsConfig
from .exec_sheets_writer import ExecSheetsWriter
from .odoo_client import OdooClient


def _fmt_money(x):
    return f"{x:,.2f}"


def _narrative(data):
    s = data["sales"]
    f = data["finance"]
    parts = []

    if s["delayed_orders"]:
        parts.append(
            f"{len(s['delayed_orders'])} order(s) are past their promised delivery date and need attention."
        )
    if f["overdue_receivables_today"]:
        total = sum(float(i["amount_residual"]) for i in f["overdue_receivables_today"])
        parts.append(f"{len(f['overdue_receivables_today'])} customer invoice(s) totaling €{_fmt_money(total)} are overdue.")

    parts.append(
        f"Today: {s['new_orders_today']} new order(s) (€{_fmt_money(s['revenue_today'])} confirmed revenue), "
        f"vs {s['new_orders_yesterday']} yesterday (€{_fmt_money(s['revenue_yesterday'])})."
    )
    parts.append(
        f"Cash position is €{_fmt_money(f['latest_balance'])}, with a net movement of €{_fmt_money(f['cash_flow_today'])} today "
        f"(€{_fmt_money(f['receipts_today'])} in, €{_fmt_money(f['payments_today'])} out)."
    )
    open_payables_total = sum(float(b["amount_residual"]) for b in f["open_bills"])
    parts.append(f"Open payables to suppliers total €{_fmt_money(open_payables_total)} across {len(f['open_bills'])} bill(s).")

    return " ".join(parts)


def _key_figures_rows(data):
    """Returns raw numeric values (not pre-formatted strings) so Sheets stores
    them as real numbers — formatting is applied afterward via cell number
    format, not by embedding "+"/comma-formatted text (which Sheets'
    USER_ENTERED parser can misread as a broken formula)."""
    s = data["sales"]
    f = data["finance"]
    overdue_today_total = sum(float(i["amount_residual"]) for i in f["overdue_receivables_today"])

    return [
        ["Revenue (confirmed orders)", s["revenue_today"], s["revenue_yesterday"],
         s["revenue_today"] - s["revenue_yesterday"], ""],
        ["New Orders", s["new_orders_today"], s["new_orders_yesterday"],
         s["new_orders_today"] - s["new_orders_yesterday"], ""],
        ["Delayed Orders", len(s["delayed_orders"]), s["delayed_orders_count_yesterday"],
         len(s["delayed_orders"]) - s["delayed_orders_count_yesterday"], "Past commitment date, not yet fulfilled"],
        ["Cash Flow (net)", f["cash_flow_today"], f["cash_flow_yesterday"],
         f["cash_flow_today"] - f["cash_flow_yesterday"], ""],
        ["Overdue Receivables", overdue_today_total, f["overdue_receivables_yesterday_total"],
         overdue_today_total - f["overdue_receivables_yesterday_total"], "Yesterday figure is approximate (see caveats)"],
    ]


def _orders_rows(data):
    rows = []
    for o in sorted(data["sales"]["orders"], key=lambda o: o["order_date"], reverse=True):
        rows.append([
            o["name"], o["partner_name"], o["order_date"].isoformat(), o["amount_total"],
            o["state"], o.get("delivery_status") or "", o["commitment_date_parsed"].isoformat() if o["commitment_date_parsed"] else "",
            exec_data._order_status(o, data["today"]),
        ])
    return rows


def _revenue_trend_rows(data):
    return [[d.isoformat(), amount] for d, amount in data["sales"]["revenue_trend"]]


def _status_rows(data):
    return [[label, count] for label, count in sorted(data["sales"]["status_breakdown"].items())]


def _top_customer_rows(data):
    return [[name, total] for name, total in data["sales"]["top_customers"]]


def _open_items_rows(data):
    rows = []
    for b in data["finance"]["open_bills"]:
        rows.append([
            "Payable", b["name"], b["partner_id"][1] if b["partner_id"] else "",
            b["due_date"].isoformat() if b["due_date"] else "", -float(b["amount_residual"]),
            exec_data._aging_bucket(b["due_date"], data["today"]),
        ])
    for i in data["finance"]["open_invoices"]:
        rows.append([
            "Receivable", i["name"], i["partner_id"][1] if i["partner_id"] else "",
            i["due_date"].isoformat() if i["due_date"] else "", float(i["amount_residual"]),
            exec_data._aging_bucket(i["due_date"], data["today"]),
        ])
    rows.sort(key=lambda r: r[3])
    return rows


def _cash_trend_rows(data):
    net_by_date = dict(data["finance"]["net_cash_flow_trend"])
    return [[d.isoformat(), net_by_date.get(d, 0.0), balance] for d, balance in data["finance"]["balance_trend"]]


def _aging_rows(data):
    f = data["finance"]
    return [
        [bucket, f["receivables_aging"].get(bucket, 0.0), f["payables_aging"].get(bucket, 0.0)]
        for bucket in exec_data.AGING_BUCKETS
    ]


def run():
    odoo = OdooClient(OdooConfig())
    sheets = ExecSheetsWriter(SheetsConfig())

    starting_balance_date = date.fromisoformat(STARTING_BALANCE_DATE)
    data = exec_data.build(
        odoo,
        odoo.cfg.bank_journal_id,
        float(STARTING_BALANCE_AMOUNT),
        starting_balance_date,
    )

    sheets.write_summary(_narrative(data), _key_figures_rows(data), data["caveats"])
    sheets.write_sales(_orders_rows(data), _revenue_trend_rows(data), _status_rows(data), _top_customer_rows(data))
    sheets.write_finance(_open_items_rows(data), _cash_trend_rows(data), _aging_rows(data))
