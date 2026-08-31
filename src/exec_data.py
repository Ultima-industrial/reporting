"""
Aggregates raw Odoo data (sale orders, bank transactions, open invoices/bills)
into the figures the executive report needs: today-vs-yesterday snapshots,
30-day trend series, order-status breakdown, and receivables/payables aging.

Every number here is derived directly from Odoo records fetched via
OdooClient (read-only) — nothing is invented. Where a comparison can only be
approximated from current data (see overdue receivables below), that's
called out explicitly in the returned dict under "caveats" rather than
silently presented as exact.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

TREND_DAYS = 30
AGING_BUCKETS = ["Not yet due", "1-30 days overdue", "31-60 days overdue", "61-90 days overdue", "90+ days overdue"]


def _date_part(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace(" ", "T")).date()


def _aging_bucket(due_date, as_of):
    if due_date is None or due_date >= as_of:
        return AGING_BUCKETS[0]
    days = (as_of - due_date).days
    if days <= 30:
        return AGING_BUCKETS[1]
    if days <= 60:
        return AGING_BUCKETS[2]
    if days <= 90:
        return AGING_BUCKETS[3]
    return AGING_BUCKETS[4]


def _order_status(order, as_of):
    state = order["state"]
    if state in ("draft", "sent"):
        return "Quotation"
    if state == "done" or order.get("delivery_status") == "full":
        return "Fulfilled"
    commitment = _date_part(order.get("commitment_date"))
    if commitment and commitment < as_of:
        return "Delayed"
    return "In Progress"


def _daily_series(dated_amounts, start, end):
    """Fills every date in [start, end] with 0.0 where there's no data, so
    charts don't silently skip days with genuinely zero activity."""
    totals = defaultdict(float)
    for d, amount in dated_amounts:
        if start <= d <= end:
            totals[d] += amount
    series = []
    d = start
    while d <= end:
        series.append((d, totals.get(d, 0.0)))
        d += timedelta(days=1)
    return series


def build(odoo, bank_journal_id, starting_balance_amount, starting_balance_date, today=None):
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    trend_start = today - timedelta(days=TREND_DAYS - 1)

    caveats = []

    # --- Sales ---
    orders_raw = odoo.sales_orders(from_date=trend_start.isoformat())
    orders = []
    for o in orders_raw:
        order_date = _date_part(o["date_order"])
        orders.append({
            **o,
            "order_date": order_date,
            "commitment_date_parsed": _date_part(o.get("commitment_date")),
            "partner_name": o["partner_id"][1] if o.get("partner_id") else "(unknown)",
        })

    def confirmed_revenue(d):
        return sum(o["amount_total"] for o in orders if o["order_date"] == d and o["state"] in ("sale", "done"))

    def new_orders_count(d):
        return sum(1 for o in orders if o["order_date"] == d)

    status_today = [(_order_status(o, today), o) for o in orders]
    delayed_today = [o for label, o in status_today if label == "Delayed"]
    in_progress_today = [o for label, o in status_today if label == "In Progress"]

    status_yesterday_counts = defaultdict(int)
    for o in orders:
        status_yesterday_counts[_order_status(o, yesterday)] += 1
    delayed_yesterday_count = status_yesterday_counts["Delayed"]

    status_breakdown = defaultdict(int)
    for label, _ in status_today:
        status_breakdown[label] += 1

    revenue_trend = _daily_series([(o["order_date"], o["amount_total"]) for o in orders if o["state"] in ("sale", "done")], trend_start, today)

    customer_totals = defaultdict(float)
    for o in orders:
        if o["state"] in ("sale", "done"):
            customer_totals[o["partner_name"]] += o["amount_total"]
    top_customers = sorted(customer_totals.items(), key=lambda kv: -kv[1])[:5]

    # --- Finance: bank cash flow ---
    anchor = starting_balance_date + timedelta(days=1)
    bank_from = min(anchor, trend_start)
    txns_raw = odoo.bank_transactions(bank_journal_id, from_date=bank_from.isoformat())
    txns = [(_date_part(t["date"]), float(t["amount"])) for t in txns_raw]
    txns.sort(key=lambda t: t[0])

    running = starting_balance_amount
    balance_by_date = {}
    for d, amount in txns:
        running += amount
        balance_by_date[d] = running
    latest_balance = running

    # forward-fill so days with no transactions still show the carried balance
    balance_trend = []
    last_known = starting_balance_amount
    d = trend_start
    while d <= today:
        if d in balance_by_date:
            last_known = balance_by_date[d]
        balance_trend.append((d, last_known))
        d += timedelta(days=1)

    net_cash_flow = _daily_series(txns, trend_start, today)
    net_today = dict(net_cash_flow).get(today, 0.0)
    net_yesterday = dict(net_cash_flow).get(yesterday, 0.0)
    receipts_today = sum(a for d, a in txns if d == today and a > 0)
    payments_today = sum(-a for d, a in txns if d == today and a < 0)

    # --- Finance: receivables / payables ---
    bills = odoo.open_vendor_bills()
    invoices = odoo.open_customer_invoices()

    def with_due_date(records):
        out = []
        for r in records:
            out.append({**r, "due_date": _date_part(r.get("invoice_date_due"))})
        return out

    bills = with_due_date(bills)
    invoices = with_due_date(invoices)

    overdue_receivables_today = [i for i in invoices if i["due_date"] and i["due_date"] < today]
    overdue_receivables_yesterday = [i for i in invoices if i["due_date"] and i["due_date"] < yesterday]
    if len(invoices) != len(overdue_receivables_today):
        pass  # some not yet due — expected, not a data issue
    caveats.append(
        "Overdue receivables 'yesterday' is approximated from today's open-invoice snapshot "
        "(due_date < yesterday) — it slightly undercounts anything paid between yesterday and today, "
        "since Odoo only exposes currently-open invoices, not a historical snapshot."
    )

    overdue_payables_today = [b for b in bills if b["due_date"] and b["due_date"] < today]

    receivables_aging = defaultdict(float)
    for i in invoices:
        receivables_aging[_aging_bucket(i["due_date"], today)] += float(i["amount_residual"])
    payables_aging = defaultdict(float)
    for b in bills:
        payables_aging[_aging_bucket(b["due_date"], today)] += float(b["amount_residual"])

    return {
        "today": today,
        "yesterday": yesterday,
        "caveats": caveats,
        "sales": {
            "orders": orders,
            "revenue_today": confirmed_revenue(today),
            "revenue_yesterday": confirmed_revenue(yesterday),
            "new_orders_today": new_orders_count(today),
            "new_orders_yesterday": new_orders_count(yesterday),
            "delayed_orders": delayed_today,
            "delayed_orders_count_yesterday": delayed_yesterday_count,
            "in_progress_orders": in_progress_today,
            "status_breakdown": dict(status_breakdown),
            "revenue_trend": revenue_trend,
            "top_customers": top_customers,
        },
        "finance": {
            "latest_balance": latest_balance,
            "cash_flow_today": net_today,
            "cash_flow_yesterday": net_yesterday,
            "receipts_today": receipts_today,
            "payments_today": payments_today,
            "balance_trend": balance_trend,
            "net_cash_flow_trend": net_cash_flow,
            "overdue_receivables_today": overdue_receivables_today,
            "overdue_receivables_yesterday_count": len(overdue_receivables_yesterday),
            "overdue_receivables_yesterday_total": sum(float(i["amount_residual"]) for i in overdue_receivables_yesterday),
            "overdue_payables_today": overdue_payables_today,
            "open_bills": bills,
            "open_invoices": invoices,
            "receivables_aging": dict(receivables_aging),
            "payables_aging": dict(payables_aging),
        },
    }
