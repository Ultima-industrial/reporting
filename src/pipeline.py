from datetime import date, timedelta

from . import exec_data
from .categorize import Categorizer
from .config import INITIAL_LOOKBACK_DAYS, STARTING_BALANCE_AMOUNT, STARTING_BALANCE_DATE, OdooConfig, SheetsConfig
from .odoo_client import OdooClient
from .sheets_writer import SheetsWriter


def _starting_point(sheets):
    """Returns (from_date, starting_balance) to anchor the running balance.
    Continues from the last recorded row if the Register already has data;
    otherwise falls back to the configured starting balance for a first run.

    STARTING_BALANCE_AMOUNT is treated as the live balance as of
    STARTING_BALANCE_DATE (i.e. already reflecting that day's activity), so
    the first pull starts the day *after* it — otherwise that day's
    transactions would be double-counted on top of a balance that already
    includes them."""
    last_row = sheets.last_register_row()
    if last_row:
        return last_row[1], float(last_row[6])

    if not STARTING_BALANCE_AMOUNT or not STARTING_BALANCE_DATE:
        raise RuntimeError(
            "Register tab is empty and no starting balance is configured. "
            "Set STARTING_BALANCE_AMOUNT and STARTING_BALANCE_DATE in .env to seed the first run "
            "(the actual BluBanca balance as of that date)."
        )
    anchor_date = date.fromisoformat(STARTING_BALANCE_DATE)
    from_date = (anchor_date + timedelta(days=1)).isoformat()
    return from_date, float(STARTING_BALANCE_AMOUNT)


def run():
    categorizer = Categorizer()
    odoo = OdooClient(OdooConfig())
    sheets = SheetsWriter(SheetsConfig())

    from_date, running_balance = _starting_point(sheets)
    lookback_floor = (date.today() - timedelta(days=INITIAL_LOOKBACK_DAYS)).isoformat()
    from_date = max(from_date, lookback_floor)

    already_recorded = sheets.existing_transaction_ids()
    transactions = odoo.bank_transactions(odoo.cfg.bank_journal_id, from_date=from_date)
    transactions.sort(key=lambda t: (t["date"], t["id"]))

    new_rows = []
    category_totals = {}
    for txn in transactions:
        counterparty = txn["partner_id"][1] if txn.get("partner_id") else ""
        category = categorizer.categorize(txn.get("payment_ref"), counterparty)
        amount = float(txn["amount"])
        category_totals[category] = category_totals.get(category, 0.0) + amount

        if str(txn["id"]) in already_recorded:
            continue
        running_balance += amount
        new_rows.append([
            txn["id"],
            txn["date"],
            counterparty,
            txn.get("payment_ref") or "",
            category,
            amount,
            running_balance,
        ])

    sheets.append_register_rows(new_rows)
    sheets.write_category_rollup(category_totals)

    bills = odoo.open_vendor_bills()
    invoices = odoo.open_customer_invoices()

    upcoming_rows = []
    total_out = 0.0
    total_in = 0.0
    for bill in bills:
        amount = float(bill["amount_residual"])
        total_out += amount
        upcoming_rows.append([
            "Payable", bill["name"], bill["partner_id"][1] if bill["partner_id"] else "",
            bill.get("invoice_date_due") or "", -amount,
        ])
    for invoice in invoices:
        amount = float(invoice["amount_residual"])
        total_in += amount
        upcoming_rows.append([
            "Receivable", invoice["name"], invoice["partner_id"][1] if invoice["partner_id"] else "",
            invoice.get("invoice_date_due") or "", amount,
        ])

    # Import VAT self-accounting (UK-based suppliers, see
    # config/import_vat_rules.yaml) and its quarterly recovery, plus
    # estimated payments on confirmed purchase orders with no vendor bill
    # yet (see exec_data.py — same logic that feeds the executive
    # dashboard/Sheet forecast, reused here so both cash-flow views agree).
    not_yet_arrived_pos = odoo.open_purchase_orders()
    arriving_partner_ids = {po["partner_id"][0] for po in not_yet_arrived_pos if po.get("partner_id")}
    country_by_partner = odoo.partner_countries(arriving_partner_ids)
    import_vat_events = exec_data._import_vat_events(not_yet_arrived_pos, country_by_partner)

    confirmed_pos = odoo.confirmed_purchase_orders()
    term_ids = {po["payment_term_id"][0] for po in confirmed_pos if po.get("payment_term_id")}
    term_lines_by_term_id = {}
    for line in odoo.payment_term_lines(term_ids):
        term_lines_by_term_id.setdefault(line["payment_id"][0], []).append(line)
    bill_totals_by_id = exec_data._bill_totals_for_pos(odoo, confirmed_pos)
    po_payment_events, _po_payment_issues = exec_data._po_payment_events(confirmed_pos, term_lines_by_term_id, bill_totals_by_id)

    vat_acconto_events = exec_data._vat_acconto_events(odoo, date.today())

    for e in import_vat_events:
        total_out += e["amount"]
        upcoming_rows.append(["Import VAT (outflow)", e["po_name"], e["supplier"], e["due_date"].isoformat(), -e["amount"]])
        total_in += e["amount"]
        upcoming_rows.append(["Import VAT (recovery)", e["po_name"], e["supplier"], e["recovered_date"].isoformat(), e["amount"]])
    for e in po_payment_events:
        total_out += e["amount"]
        upcoming_rows.append(["PO payment (est.)", e["po_name"], e["supplier"], e["due_date"].isoformat(), -e["amount"]])
    for e in vat_acconto_events:
        total_out += e["amount"]
        upcoming_rows.append(["VAT acconto (est.)", "VAT acconto", "-", e["date"].isoformat(), -e["amount"]])

    upcoming_rows.sort(key=lambda r: r[3])

    sheets.write_upcoming(upcoming_rows)

    net_upcoming = total_in - total_out
    forecast_balance = running_balance + net_upcoming
    sheets.write_forecast(running_balance, net_upcoming, forecast_balance)
