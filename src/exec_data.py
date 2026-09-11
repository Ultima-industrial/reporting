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

import yaml

from .config import CONFIG_DIR

TREND_DAYS = 30
FORECAST_MAX_DAYS = 180
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


def _is_confirmed_order(order):
    """True for a confirmed sale order (state 'sale'/'done' — 'Ordine di
    vendita' in Odoo's Italian UI). False for draft/sent quotations
    ('Preventivo'), which aren't real orders yet."""
    return order["state"] not in ("draft", "sent")


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


def _add_month(d):
    """One calendar month later, clamped to the target month's last valid
    day if d's day-of-month doesn't exist there (e.g. 31 Jan -> 28/29 Feb)."""
    if d.month == 12:
        y, m = d.year + 1, 1
    else:
        y, m = d.year, d.month + 1
    last_day = _end_of_month(date(y, m, 1)).day
    return date(y, m, min(d.day, last_day))


def _expected_invoiced_by_month(odoo, today, months=6):
    """Expected invoiced revenue by month, forward-looking: Ultima invoices
    immediately on delivery ("fatture immediate"), so a confirmed sale
    order's expected invoice date is its promised delivery date
    (commitment_date, "Data Consegna") rather than its order date. Only
    confirmed orders count (drafts/quotations aren't real deliveries yet).
    Uses each order line's untaxed_amount_to_invoice — Odoo's own computed
    "remaining to invoice" value (net of VAT) — rather than the order's
    full amount_untaxed, so anything already invoiced, in full or in part,
    is excluded rather than double-counted against actual Revenue. Grouped
    into `months` full calendar-month buckets starting with the current
    month."""
    month_starts = []
    m = today.replace(day=1)
    for _ in range(months):
        month_starts.append(m)
        m = _add_month(m)
    range_start = month_starts[0]
    range_end = _add_month(month_starts[-1]) - timedelta(days=1)

    lines = odoo.confirmed_order_lines_to_invoice_by_delivery_date(range_start.isoformat(), range_end.isoformat())
    order_ids = {l["order_id"][0] for l in lines if l.get("order_id")}
    commitment_by_order = {
        oid: _date_part(v) for oid, v in odoo.order_commitment_dates(order_ids).items()
    }

    totals = defaultdict(float)
    for l in lines:
        order_id = l["order_id"][0] if l.get("order_id") else None
        d = commitment_by_order.get(order_id)
        if not d:
            continue
        totals[d.replace(day=1)] += float(l["untaxed_amount_to_invoice"])
    return [(ms, totals.get(ms, 0.0)) for ms in month_starts]


def _vat_settlement_date(paid_date):
    """Ultima files IVA quarterly ("trimestrale per opzione", with the 1%
    interest surcharge) rather than monthly, so VAT paid at customs isn't
    recovered "next month" — it's recovered at the next quarterly
    settlement after the payment date:
      Q1 (Jan-Mar) -> 16 May
      Q2 (Apr-Jun) -> 20 Aug (the mid-August deadline shifts to the 20th
                              under the standard summer deferral)
      Q3 (Jul-Sep) -> 16 Nov
      Q4 (Oct-Dec) -> no standalone quarterly payment; settled via the
                      annual return the following 16 March instead."""
    y = paid_date.year
    if paid_date <= date(y, 3, 31):
        return date(y, 5, 16)
    if paid_date <= date(y, 6, 30):
        return date(y, 8, 20)
    if paid_date <= date(y, 9, 30):
        return date(y, 11, 16)
    return date(y + 1, 3, 16)


def _load_import_country_rules():
    """Reads config/import_vat_rules.yaml — the "is this an import PO"
    signal (country + per-rule supplier exclusions) shared by
    _import_vat_events and _matches_import_rule (used to scope other
    import-specific business rules, e.g. the "paid before dispatch"
    adjustment for Immediate Payment terms on import POs)."""
    path = CONFIG_DIR / "import_vat_rules.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _matches_import_rule(country, partner_name, rules):
    """True if this supplier matches one of the country rules (and isn't
    on that rule's exclude list) — the same "is this an import supplier"
    signal used for import VAT self-accounting, reused elsewhere so a
    supplier excluded there (e.g. Bioscan) is treated consistently."""
    for rule in rules:
        if country not in rule.get("countries", []):
            continue
        if any(ex.lower() in partner_name.lower() for ex in rule.get("exclude", [])):
            continue
        return True
    return False


def _import_vat_events(purchase_orders, country_by_partner, rules):
    """Computes one forecast outflow (plus a paired recovery inflow — see
    _vat_settlement_date) per matching purchase order: suppliers based in
    a listed country (e.g. the UK, post-Brexit) don't charge Italian VAT
    on their own PO/bill — Ultima self-accounts the import VAT straight to
    customs instead, due some days before the order's expected arrival
    (date_planned), and recovers it as input VAT at the next quarterly
    settlement. This is a real cash outflow (and later inflow) with no
    corresponding vendor bill in Odoo, so it can't come from
    open_vendor_bills() and has to be modeled separately. Matching is via
    _matches_import_rule (config/import_vat_rules.yaml)."""
    if not rules:
        return []

    events = []
    for po in purchase_orders:
        planned = _date_part(po.get("date_planned"))
        if not planned:
            continue
        partner_id = po["partner_id"][0] if po.get("partner_id") else None
        partner_name = po["partner_id"][1] if po.get("partner_id") else ""
        country = country_by_partner.get(partner_id)
        for rule in rules:
            if country not in rule.get("countries", []):
                continue
            if any(ex.lower() in partner_name.lower() for ex in rule.get("exclude", [])):
                continue
            due = planned - timedelta(days=rule["days_before_arrival"])
            events.append({
                "po_name": po["name"],
                "supplier": partner_name,
                "due_date": due,
                "amount": float(po["amount_total"]) * rule["vat_percent"] / 100,
                "recovered_date": _vat_settlement_date(due),
                "rule": f"{rule['vat_percent']}% import VAT, {rule['days_before_arrival']}d before arrival",
            })
            break  # first matching rule wins
    return events


def _end_of_month(d):
    """Last calendar day of d's month."""
    next_month = d.replace(day=28) + timedelta(days=4)
    return next_month - timedelta(days=next_month.day)


_SUPPORTED_DELAY_TYPES = {"days_after", "days_end_of_month", "days_end_of_month_on_the"}


def _term_due_dates(anchor_date, term_lines):
    """Given one payment term's lines and an anchor date, returns a list of
    (due_date, fraction) tuples — fraction is a 0-1 share of the PO's total.
    Returns None (not a supported schedule) if any line uses a delay_type
    or value type this hasn't been built to handle yet (e.g. a fixed-amount
    line) — callers should flag that as a data issue rather than silently
    mis-price it.

    Note: this Odoo instance's actual delay_type values are 'days_after',
    'days_end_of_month', and 'days_end_of_month_on_the' — confirmed via
    the raw field dump surfaced by _po_payment_events' issue messages,
    which is how a naming mismatch here gets caught rather than silently
    mis-pricing something (an earlier version of this code guessed
    'days_after_end_of_month(_on_the)', which turned out not to match).

    'days_end_of_month_on_the' is Odoo's "N giorni fine mese il D" schedule
    (e.g. Ultima's "60 gg fine mese", whose actual stored fields are
    nb_days=60 + days_next_month=31, NOT necessarily a literal 60/31 for
    every such term — the effective ~60-day/end-of-month behavior comes
    out of this exact sequence): end of the anchor's own month, plus
    nb_days, rounded UP to end of THAT resulting month, then moved to day
    `days_next_month` (capped at that month's real length) of the
    FOLLOWING month. Verified against Odoo's own preview UI for two
    different anchor dates (10 Sept -> 30 Nov, 1 Jul -> 30 Sept) before
    shipping this — every date within the same anchor month collapses to
    the same due date, which is exactly the point of a "fine mese" term
    (one shared payment date per month of invoices, not one per invoice)."""
    results = []
    for line in term_lines:
        if line.get("value") != "percent" or line.get("delay_type") not in _SUPPORTED_DELAY_TYPES:
            return None
        if line["delay_type"] == "days_end_of_month_on_the":
            d = _end_of_month(anchor_date) + timedelta(days=int(line["nb_days"]))
            target_month_first = _add_month(d.replace(day=1))
            target_day = min(int(line["days_next_month"]), _end_of_month(target_month_first).day)
            d = target_month_first.replace(day=target_day)
        else:
            d = anchor_date + timedelta(days=int(line["nb_days"]))
            if line["delay_type"] == "days_end_of_month":
                d = _end_of_month(d)
        results.append((d, float(line["value_amount"]) / 100))
    if results and abs(sum(f for _, f in results) - 1.0) > 0.01:
        return None  # lines don't add up to 100% — schema mismatch, don't guess
    return results or None


def _bill_totals_for_pos(odoo, purchase_orders):
    """Maps account.move id -> amount_total for every bill referenced by
    the given purchase orders' invoice_ids (posted only — see
    OdooClient.bills_by_id) — used by _po_payment_events to net off what's
    already been invoiced (e.g. a partial "fattura acconto") from a PO's
    total before estimating what's still owed."""
    bill_ids = {bid for po in purchase_orders for bid in (po.get("invoice_ids") or [])}
    return {b["id"]: float(b["amount_total"]) for b in odoo.bills_by_id(bill_ids)}


IMMEDIATE_PAYMENT_IMPORT_LEAD_DAYS = 7


def _is_immediate_term(lines):
    """True if every line is "due immediately" (0 days, plain days_after) —
    the shape of Odoo's "Immediate Payment" / "Pagamento Immediato" term."""
    return bool(lines) and all(
        l.get("delay_type") == "days_after" and int(l.get("nb_days") or 0) == 0
        for l in lines
    )


def _po_payment_events(purchase_orders, term_lines_by_term_id, bill_totals_by_id,
                        country_by_partner, import_rules):
    """Estimated future payments to suppliers for confirmed purchase orders,
    for whatever balance hasn't been invoiced yet. A PO already fully
    invoiced (its linked bills' amount_total sums to ~its own amount_total)
    is skipped — that's fully accounted for elsewhere (via
    open_vendor_bills, or already settled). A PO with NO bills yet is
    estimated on its full amount_total, same as before. A PARTIALLY
    invoiced PO (e.g. an acconto invoice already received) is estimated on
    just the remaining un-invoiced balance — the acconto invoice itself
    already has its own real due date via open_vendor_bills, so only the
    still-unbilled remainder needs an estimate, and using the full PO
    amount here would double-count the acconto portion.

    Anchor date is normally date_planned (expected arrival) — an
    approximation, since a supplier's actual invoice date may fall earlier
    or later; worth sanity-checking computed dates against a few real POs
    after this ships. The same caveat applies doubly to the
    remaining-balance case: the split-by-payment-term-line proportions are
    applied to the smaller remaining amount as a fresh approximation,
    which may not exactly match a real "saldo" invoice's actual terms if
    those differ from the acconto's.

    Exception: an import PO (see _matches_import_rule) on an Immediate
    Payment term anchors IMMEDIATE_PAYMENT_IMPORT_LEAD_DAYS (7) earlier
    than expected arrival instead — these suppliers require payment before
    dispatch, not on/after arrival, so anchoring on date_planned itself
    (or later) would be backwards. Scoped to import POs only per Annalisa
    Casavecchia 2026-09 — a domestic "Immediate Payment" PO still anchors
    on date_planned itself.

    Every confirmed PO is expected to carry a payment term ("termini di
    pagamento") — this is meant to become standard data-entry practice, so
    a PO (or its remaining balance) missing one (or missing date_planned,
    or using a payment-term schedule this function doesn't support) is
    excluded and flagged via the returned issues list rather than silently
    guessed at or skipped quietly."""
    events = []
    issues = []
    for po in purchase_orders:
        invoiced_total = sum(bill_totals_by_id.get(bid, 0.0) for bid in (po.get("invoice_ids") or []))
        remaining = float(po["amount_total"]) - invoiced_total
        if remaining <= 0.01:
            continue  # fully invoiced (within rounding) — nothing left to estimate
        partner_id = po["partner_id"][0] if po.get("partner_id") else None
        supplier = po["partner_id"][1] if po.get("partner_id") else "(unknown)"
        term = po.get("payment_term_id")
        if not term:
            issues.append(f"{po['name']} ({supplier}) has no payment term set")
            continue
        planned = _date_part(po.get("date_planned"))
        if not planned:
            issues.append(f"{po['name']} ({supplier}) has a payment term but no expected arrival date set")
            continue
        lines = term_lines_by_term_id.get(term[0])
        anchor = planned
        if lines and _is_immediate_term(lines):
            country = country_by_partner.get(partner_id)
            if _matches_import_rule(country, supplier, import_rules):
                anchor = planned - timedelta(days=IMMEDIATE_PAYMENT_IMPORT_LEAD_DAYS)
        splits = _term_due_dates(anchor, lines) if lines else None
        if not splits:
            if not lines:
                issues.append(f"{po['name']}'s payment term ('{term[1]}') has no lines returned by Odoo (id {term[0]})")
            else:
                raw = "; ".join(
                    f"value={l.get('value')!r} delay_type={l.get('delay_type')!r} "
                    f"nb_days={l.get('nb_days')!r} days_next_month={l.get('days_next_month')!r} "
                    f"value_amount={l.get('value_amount')!r}"
                    for l in lines
                )
                issues.append(
                    f"{po['name']}'s payment term ('{term[1]}') uses a schedule not yet supported here — "
                    f"raw line data: {raw}"
                )
            continue
        for due, fraction in splits:
            events.append({
                "po_name": po["name"],
                "supplier": supplier,
                "due_date": due,
                "amount": remaining * fraction,
                "term_name": term[1],
            })
    return events, issues


def _vat_acconto_reference_liability(odoo, ref_year):
    """Approximates the reference figure the "metodo storico" acconto
    calculation needs — the VAT liability of Oct-Dec of ref_year (what a
    quarterly filer's annual return would show on rigo VH4 for that
    quarter): output VAT (posted customer invoices) minus input VAT
    (posted vendor bills), Oct 1 - Dec 31.

    Two things this approximation does NOT capture, both of which mean it
    likely UNDERSTATES the true reference liability for a company with
    material self-accounted import VAT like Ultima:
      1. Self-accounted import VAT actually paid to customs in that
         historical window — Odoo has no clean queryable record of past
         customs payments the way it does posted invoices/bills, so this
         input VAT credit isn't included here.
      2. Any other Dichiarazione IVA adjustment (credit notes, splafonamento,
         prior-period corrections, etc.) that a full VAT return computes
         but a plain transaction sum doesn't.
    Treat this as a starting estimate, not a substitute for the actual
    filed rigo VH4 — see config/vat_acconto.yaml for a manual override."""
    q4_start = date(ref_year, 10, 1)
    q4_end = date(ref_year, 12, 31)

    invoices = odoo.posted_customer_invoices(from_date=q4_start.isoformat())
    output_vat = sum(
        float(i["amount_tax"]) for i in invoices
        if i.get("invoice_date") and q4_start <= _date_part(i["invoice_date"]) <= q4_end
    )
    bills = odoo.posted_vendor_bills(from_date=q4_start.isoformat())
    input_vat = sum(
        float(b["amount_tax"]) for b in bills
        if b.get("invoice_date") and q4_start <= _date_part(b["invoice_date"]) <= q4_end
    )
    return output_vat - input_vat


def _vat_acconto_events(odoo, today, max_days=FORECAST_MAX_DAYS):
    """The December VAT advance payment (acconto IVA, due ~27 Dec) within
    the forecast horizon. Uses config/vat_acconto.yaml's figure for a year
    if one is set there (a definitive, manually-entered amount — e.g. the
    actual filed rigo VH4 — always wins); otherwise estimates it via the
    "metodo storico" safe-harbor calculation: 88% of the prior year's Q4
    VAT liability (see _vat_acconto_reference_liability). If that
    reference period was a net VAT credit (liability <= 0), no acconto is
    due and no event is added."""
    overrides = {}
    path = CONFIG_DIR / "vat_acconto.yaml"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            entries = yaml.safe_load(f) or []
        overrides = {e["year"]: float(e["amount"]) for e in entries if e.get("amount") is not None}

    events = []
    horizon_end = today + timedelta(days=max_days)
    y = today.year
    while date(y, 12, 27) <= horizon_end:
        due = date(y, 12, 27)
        if due >= today:
            if y in overrides:
                amount = overrides[y]
                estimated = False
            else:
                reference = _vat_acconto_reference_liability(odoo, y - 1)
                amount = max(reference, 0.0) * 0.88
                estimated = True
            if amount:
                events.append({"date": due, "amount": amount, "estimated": estimated})
        y += 1
    return events


def _forecast_series(latest_balance, today, bills, invoices, import_vat_events,
                      po_payment_events, vat_acconto_events, max_days=FORECAST_MAX_DAYS):
    """Draft forward cash flow projection: current balance, walked forward
    day by day as open bills (out), invoices (in), configured import VAT
    prepayments and their quarterly recovery (see _import_vat_events),
    estimated payments on unbilled confirmed purchase orders (see
    _po_payment_events), and the December VAT acconto (see
    _vat_acconto_events) hit their dates. Still nothing beyond that is
    modeled — no new sales, no recurring costs, no fully realistic
    payment-timing behavior. An already-overdue bill/PO-payment/VAT event
    is assumed to land "today" rather than on its original (past) date,
    since projecting a date before today doesn't make sense for a forward
    chart — still expected, just timing unknown. An overdue RECEIVABLE
    (invoice sent to a client, already past due) instead lands 1 month
    from today — a rolling assumption recomputed against "today" on every
    run, not a fixed date — reflecting that a client already paying late
    is a different collection-risk case than something merely due today.

    The horizon always runs the full max_days (6 months) ahead of today,
    not just out to the last known event — the balance simply stays flat
    once every known event has landed, so the chart always shows a fixed
    forward window rather than stopping wherever data happens to run out."""
    events = defaultdict(float)
    for b in bills:
        if b["due_date"]:
            events[max(b["due_date"], today)] -= float(b["amount_residual"])
    # Overdue receivables (invoices sent to clients, past their due date) are
    # assumed to land 1 month from today — not immediately today like
    # everything else that's overdue/due — since a client already paying
    # late is a materially different collection-risk case than a bill just
    # due today. This is a rolling assumption, not a fixed date: it's
    # recomputed against "today" on every run, so it shifts forward a day
    # each time this report runs rather than converging on one calendar date.
    for i in invoices:
        if not i["due_date"]:
            continue
        landing = i["due_date"] if i["due_date"] >= today else _add_month(today)
        events[landing] += float(i["amount_residual"])
    for v in import_vat_events:
        events[max(v["due_date"], today)] -= v["amount"]
        events[max(v["recovered_date"], today)] += v["amount"]
    for p in po_payment_events:
        events[max(p["due_date"], today)] -= p["amount"]
    for a in vat_acconto_events:
        events[max(a["date"], today)] -= a["amount"]

    max_date = today + timedelta(days=max_days)
    series = []
    running = latest_balance
    d = today
    while d <= max_date:
        running += events.get(d, 0.0)
        series.append((d, running))
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
    # Fetched from year_start (not just fetch_start) so Sales Won (YTD) has a
    # full year of data available — but `orders` below is then filtered back
    # down to the narrower fetch_start window, so Order status/New Orders/
    # Delayed Orders etc. keep reflecting only the recent window they always
    # have, rather than every order confirmed since January.
    orders_fetch_start = min(fetch_start, year_start)
    orders_all_raw = odoo.sales_orders(from_date=orders_fetch_start.isoformat())
    orders_all = []
    for o in orders_all_raw:
        order_date = _date_part(o["date_order"])
        orders_all.append({
            **o,
            "order_date": order_date,
            "commitment_date_parsed": _date_part(o.get("commitment_date")),
            "partner_name": o["partner_id"][1] if o.get("partner_id") else "(unknown)",
        })
    orders = [o for o in orders_all if o["order_date"] and o["order_date"] >= fetch_start]

    # New Orders counts only confirmed sale orders ("Ordine di vendita") —
    # draft/sent quotations ("Preventivo") aren't new orders yet.
    confirmed_order_dates = [o["order_date"] for o in orders if _is_confirmed_order(o)]
    new_orders_today = mtd_count(confirmed_order_dates, today)
    new_orders_yesterday = mtd_count(confirmed_order_dates, yesterday)

    # Quotes = sale orders not yet confirmed (state draft/sent — see
    # _order_status), by value rather than count, raised this month so far.
    def quotes_value(as_of):
        if as_of < month_start:
            return 0.0
        return sum(
            o["amount_total"] for o in orders
            if o["state"] in ("draft", "sent") and month_start <= o["order_date"] <= as_of
        )

    quotes_raised_mtd = quotes_value(today)
    quotes_raised_mtd_yesterday = quotes_value(yesterday)

    # Sales Won = net (VAT-excluded) value of confirmed sale orders ("Ordine
    # di vendita"), by order date, raised this month so far. This is order
    # value, not invoiced revenue — a confirmed order isn't necessarily
    # invoiced yet, so this can (and often will) differ from Revenue (MTD).
    def sales_won_value(as_of):
        if as_of < month_start:
            return 0.0
        return sum(
            o["amount_untaxed"] for o in orders
            if _is_confirmed_order(o) and month_start <= o["order_date"] <= as_of
        )

    sales_won_mtd = sales_won_value(today)
    sales_won_mtd_yesterday = sales_won_value(yesterday)

    # Same measure, but Jan 1 through today — needs the wider orders_all
    # fetch (see above), not the MTD-window-limited `orders` list.
    sales_won_ytd = sum(
        o["amount_untaxed"] for o in orders_all
        if _is_confirmed_order(o) and year_start <= o["order_date"] <= today
    )

    expected_invoiced_by_month = _expected_invoiced_by_month(odoo, today)

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

    # Gross profit — year-to-date, only over invoice lines with real cost
    # data (see OdooClient.invoiced_lines_with_cost). A blended figure
    # across everything would be skewed by products with no cost ever
    # recorded in Odoo (which look like 0 cost / 100% margin, not
    # genuinely free).
    margin_lines = odoo.invoiced_lines_with_cost(from_date=year_start.isoformat())
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
            f"Gross Profit % is computed only over the {gp_coverage_percent:.0f}% of this year's invoiced "
            f"revenue that has real product cost data in Odoo — the remaining "
            f"€{gp_revenue_total - gp_revenue_costed:,.2f} has no cost recorded (shows as 0 cost / 100% "
            f"margin, which is a data gap, not a genuinely free sale) and is excluded rather than included "
            f"at a misleadingly inflated margin."
        )

    overdue_payables_today = [b for b in bills if b["due_date"] and b["due_date"] < today]

    # --- Finance: forward-looking events not yet reflected in a bill/invoice ---
    import_rules = _load_import_country_rules()
    not_yet_arrived_pos = odoo.open_purchase_orders()
    confirmed_pos = odoo.confirmed_purchase_orders()
    # One combined country lookup covers both _import_vat_events (VAT
    # self-accounting, not-yet-arrived POs only) and _po_payment_events
    # (the Immediate-Payment-import "-7 days" rule, all confirmed POs).
    all_po_partner_ids = {
        po["partner_id"][0] for po in (not_yet_arrived_pos + confirmed_pos) if po.get("partner_id")
    }
    country_by_partner = odoo.partner_countries(all_po_partner_ids)
    import_vat_events = _import_vat_events(not_yet_arrived_pos, country_by_partner, import_rules)

    term_ids = {po["payment_term_id"][0] for po in confirmed_pos if po.get("payment_term_id")}
    term_lines_raw = odoo.payment_term_lines(term_ids)
    term_lines_by_term_id = defaultdict(list)
    for line in term_lines_raw:
        term_lines_by_term_id[line["payment_id"][0]].append(line)
    bill_totals_by_id = _bill_totals_for_pos(odoo, confirmed_pos)
    po_payment_events, po_payment_issues = _po_payment_events(
        confirmed_pos, term_lines_by_term_id, bill_totals_by_id, country_by_partner, import_rules
    )

    vat_acconto_events = _vat_acconto_events(odoo, today)

    forecast_trend = _forecast_series(
        latest_balance, today, bills, invoices, import_vat_events, po_payment_events, vat_acconto_events
    )
    caveats.append(
        "Cash Flow Forecast is a draft, 6 months ahead: it projects known open vendor bill and customer "
        "invoice due dates, configured import VAT prepayment rules (config/import_vat_rules.yaml) and their "
        "quarterly recovery, estimated payment dates for confirmed purchase orders with no vendor bill yet "
        "(from their payment term + expected arrival date), and the configured December VAT acconto "
        "(config/vat_acconto.yaml) — against the current balance. No new sales, recurring costs, or fully "
        "realistic payment-timing behavior are modeled yet. An already overdue/due bill, PO payment, or VAT "
        "event is assumed to land today rather than on its original date. An overdue RECEIVABLE (invoice "
        "sent to a client, already past due) instead lands 1 month from today — a rolling assumption "
        "recomputed daily, not a fixed date."
    )
    caveats.append(
        "Import VAT self-accounted at customs (suppliers based in a listed country, see "
        "config/import_vat_rules.yaml) is modeled as recovered at Ultima's next quarterly IVA settlement "
        "(16 May / 20 Aug / 16 Nov, or via the annual return the following 16 March for Q4) rather than the "
        "following month, since Ultima files quarterly, not monthly."
    )
    if any(e.get("estimated") for e in vat_acconto_events):
        caveats.append(
            "The December acconto IVA is auto-estimated (metodo storico: 88% of the prior year's Oct-Dec "
            "output VAT minus input VAT, from posted invoices/bills) unless overridden in "
            "config/vat_acconto.yaml. This estimate does NOT include self-accounted import VAT actually paid "
            "in that historical quarter (Odoo has no queryable record of past customs payments) or other VAT "
            "return adjustments, so it likely UNDERSTATES the true reference liability — set the actual filed "
            "rigo VH4 figure in config/vat_acconto.yaml once known, which always takes precedence."
        )
    caveats.append(
        "Confirmed orders awaiting invoice estimates only the REMAINING un-invoiced balance of a purchase "
        "order — if it's been partially invoiced (e.g. a 'fattura acconto' already received), that invoice's "
        "own due date already shows via Open Vendor Bills, and only the still-unbilled remainder is "
        "estimated here, split across the same payment-term proportions applied to that smaller balance."
    )
    caveats.append(
        f"An import PO (supplier based in a listed country, see config/import_vat_rules.yaml) on an "
        f"Immediate Payment term is anchored {IMMEDIATE_PAYMENT_IMPORT_LEAD_DAYS} days before its expected "
        f"arrival rather than on the arrival date itself, since these suppliers require payment before "
        f"dispatch, not on/after arrival. A domestic Immediate Payment PO still anchors on the expected "
        f"arrival date."
    )
    if po_payment_issues:
        caveats.append(
            "Confirmed purchase orders missing what's needed to estimate their remaining payment date (no "
            "payment term set, no expected arrival date, or a payment-term schedule not yet supported here) "
            "are excluded from the forecast rather than guessed at: " + "; ".join(po_payment_issues)
        )
    caveats.append(
        "Quotes Raised 'yesterday' uses each order's CURRENT state, not its state as of yesterday — a "
        "quote raised yesterday but already confirmed into a sale order by today would drop out of both "
        "figures rather than staying counted in yesterday's."
    )
    caveats.append(
        "New Orders (MTD) and Sales Won (MTD) count only confirmed sale orders (state 'sale'/'done' — "
        "'Ordine di vendita' in Odoo's Italian UI); draft/sent quotations ('Preventivo') are excluded. "
        "Sales Won is order value (net of VAT), by order date — not invoiced revenue, so it can differ "
        "from Revenue (MTD, invoiced)."
    )
    caveats.append(
        "Expected Invoiced by Month assumes deliveries land on their promised date (commitment_date, "
        "'Data Consegna') and are invoiced immediately ('fatture immediate') — a delayed delivery shows in "
        "the month it was originally promised, not when it actually ships, and this doesn't model deliveries "
        "on orders not yet confirmed. Uses each line's remaining amount to invoice (net of VAT), not the "
        "order's full value, so a partially-invoiced order only contributes what's left to invoice."
    )

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
            "sales_won_mtd": sales_won_mtd,
            "sales_won_mtd_yesterday": sales_won_mtd_yesterday,
            "sales_won_ytd": sales_won_ytd,
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
            "quotes_raised_mtd": quotes_raised_mtd,
            "quotes_raised_mtd_yesterday": quotes_raised_mtd_yesterday,
            "expected_invoiced_by_month": expected_invoiced_by_month,
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
            "forecast_trend": forecast_trend,
            "import_vat_events": sorted(import_vat_events, key=lambda e: e["due_date"]),
            "po_payment_events": sorted(po_payment_events, key=lambda e: e["due_date"]),
            "po_payment_issues": po_payment_issues,
            "vat_acconto_events": vat_acconto_events,
        },
    }
