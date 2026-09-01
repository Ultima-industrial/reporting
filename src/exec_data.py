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
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    # Orders fetch window must cover both the trend chart and month-to-date
    # sums — whichever of the two starts earlier.
    fetch_start = min(trend_start, month_start)
    # Invoices need a wider window — Last Month and Year-to-Date revenue go
    # further back than the order/trend window does.
    revenue_fetch_start = min(fetch_start, last_month_start, year_start)

    caveats = []

    def mtd_sum(dated_amounts, as_of):
        """Sum of amounts dated in [month_start, as_of]. 0 if as_of predates
        month_start (e.g. computing "yesterday" on the 1st of the month)."""
        if as_of < month_start:
            return 0.0
        return sum(amount for d, amount in dated_amounts if month_start <= d <= as_of)

    def mtd_count(dates, as_of):
        if as_of < month_start:
            return 0
        return sum(1 for d in dates if month_start <= d <= as_of)

    # --- Sales ---
    orders_raw = odoo.sales_orders(from_date=fetch_start.isoformat())
    orders = []
    for o in orders_raw:
        order_date = _date_part(o["date_order"])
        orders.append({
            **o,
            "order_date": order_date,
            "commitment_date_parsed": _date_part(o.get("commitment_date")),
            "partner_name": o["partner_id"][1] if o.get("partner_id") else "(unknown)",
        })

    new_orders_today = mtd_count([o["order_date"] for o in orders], today)
    new_orders_yesterday = mtd_count([o["order_date"] for o in orders], yesterday)

    # Revenue is actual invoiced amounts (account.move), net of VAT — a sale
    # order being confirmed doesn't mean it's been invoiced/recognized yet,
    # and amount_total mixes VAT-inclusive and VAT-exempt invoices inconsistently.
    invoices_raw = odoo.posted_customer_invoices(from_date=revenue_fetch_start.isoformat())
    invoiced = [(_date_part(inv["invoice_date"]), float(inv["amount_untaxed"])) for inv in invoices_raw if inv.get("invoice_date")]
    revenue_today = mtd_sum(invoiced, today)
    revenue_yesterday = mtd_sum(invoiced, yesterday)
    revenue_last_month = sum(amount for d, amount in invoiced if last_month_start <= d <= last_month_end)
    revenue_ytd = sum(amount for d, amount in invoiced if year_start <= d <= today)
    revenue_trend = _daily_series(invoiced, trend_start, today)

    # Gross profit — only over invoice lines with real cost data (see
    # OdooClient.invoiced_lines_with_cost). A blended figure across
    # everything would be skewed by products with no cost ever recorded in
    # Odoo (which look like 0 cost / 100% margin, not genuinely free).
    margin_lines = odoo.invoiced_lines_with_cost(from_date=month_start.isoformat())
    gp_revenue_total = sum(l["revenue"] for l in margin_lines)
    costed_lines = [l for l in margin_lines if l["unit_cost"]]
    gp_revenue_costed = sum(l["revenue"] for l in costed_lines)
    gp_cost_costed = sum(l["unit_cost"] * l["quantity"] for l in costed_lines)
    gp_percent = ((gp_revenue_costed - gp_cost_costed) / gp_revenue_costed * 100) if gp_revenue_costed else None
    gp_coverage_percent = (gp_revenue_costed / gp_revenue_total * 100) if gp_revenue_total else 0.0

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

    # Same basis as Revenue (MTD invoiced) — not order value, and not a
    # trailing 30-day window — so this reconciles with the Revenue figure
    # instead of implying more revenue than was actually invoiced.
    customer_totals = defaultdict(float)
    for inv in invoices_raw:
        d = _date_part(inv.get("invoice_date"))
        if d and month_start <= d <= today:
            name = inv["partner_id"][1] if inv.get("partner_id") else "(unknown)"
            customer_totals[name] += float(inv["amount_untaxed"])
    top_customers = sorted(customer_totals.items(), key=lambda kv: -kv[1])[:5]

    # --- Finance: bank cash flow ---
    # starting_balance_amount is only guaranteed accurate as of
    # starting_balance_date (it's the fixed bootstrap anchor from .env, which
    # doesn't move as this job keeps running on later days) — so every other
    # day's balance must be derived by walking forward or backward from that
    # anchor, never by just summing "the last N days" on top of it. Adding a
    # trailing window on top of the anchor double-counts every transaction
    # that already happened between the anchor date and window_start.
    window_start = min(trend_start, month_start, starting_balance_date)
    window_end = max(today, starting_balance_date)
    txns_raw = odoo.bank_transactions(bank_journal_id, from_date=window_start.isoformat())
    daily_net = defaultdict(float)
    for t in txns_raw:
        d = _date_part(t["date"])
        if window_start <= d <= window_end:
            daily_net[d] += float(t["amount"])

    full_balance = {starting_balance_date: starting_balance_amount}
    running = starting_balance_amount
    d = starting_balance_date
    while d < window_end:
        d += timedelta(days=1)
        running += daily_net.get(d, 0.0)
        full_balance[d] = running

    running = starting_balance_amount
    d = starting_balance_date
    while d > window_start:
        removed = daily_net.get(d, 0.0)
        d -= timedelta(days=1)
        running -= removed
        full_balance[d] = running

    latest_balance = full_balance[today]
    balance_trend = [(d, full_balance[d]) for d in sorted(full_balance) if trend_start <= d <= today]
    net_cash_flow = [(d, daily_net.get(d, 0.0)) for d in sorted(full_balance) if trend_start <= d <= today]
    # Cash Flow KPI is month-to-date net movement, not just today's — consistent
    # with Revenue/New Orders now also being period-to-date rather than single-day.
    cash_flow_today = mtd_sum(list(daily_net.items()), today)
    cash_flow_yesterday = mtd_sum(list(daily_net.items()), yesterday)
    receipts_today = sum(float(t["amount"]) for t in txns_raw if _date_part(t["date"]) == today and float(t["amount"]) > 0)
    payments_today = sum(-float(t["amount"]) for t in txns_raw if _date_part(t["date"]) == today and float(t["amount"]) < 0)

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
    if gp_revenue_total and gp_coverage_percent < 99.95:
        caveats.append(
            f"Gross Profit % is computed only over the {gp_coverage_percent:.0f}% of this month's invoiced "
            f"revenue that has real product cost data in Odoo — the remaining "
            f"€{gp_revenue_total - gp_revenue_costed:,.2f} has no cost recorded (shows as 0 cost / 100% "
            f"margin, which is a data gap, not a genuinely free sale) and is excluded rather than included "
            f"at a misleadingly inflated margin."
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
            "revenue_today": revenue_today,
            "revenue_yesterday": revenue_yesterday,
            "revenue_last_month": revenue_last_month,
            "revenue_ytd": revenue_ytd,
            "new_orders_today": new_orders_today,
            "new_orders_yesterday": new_orders_yesterday,
            "delayed_orders": delayed_today,
            "delayed_orders_count_yesterday": delayed_yesterday_count,
            "in_progress_orders": in_progress_today,
            "status_breakdown": dict(status_breakdown),
            "revenue_trend": revenue_trend,
            "top_customers": top_customers,
            "gp_percent": gp_percent,
            "gp_coverage_percent": gp_coverage_percent,
            "gp_revenue_costed": gp_revenue_costed,
            "gp_revenue_total": gp_revenue_total,
        },
        "finance": {
            "latest_balance": latest_balance,
            "cash_flow_today": cash_flow_today,
            "cash_flow_yesterday": cash_flow_yesterday,
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
