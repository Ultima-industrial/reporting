"""
Odoo external API client (XML-RPC), read-only.

Requires the Odoo Custom plan (or equivalent self-hosted Enterprise setup) —
XML-RPC/JSON-RPC access is not included in Standard or One App Free. If
authentication fails with an access-rights error, that's a plan/permissions
issue to resolve in Odoo, not a bug in this client.
"""

import xmlrpc.client

import yaml

from .config import CONFIG_DIR


def _paid_override_names():
    """Bills/invoices confirmed paid via the real bank statement but not yet
    reconciled in Odoo (see config/paid_overrides.yaml) — excluded from
    every open/overdue query so reports don't count them as outstanding."""
    path = CONFIG_DIR / "paid_overrides.yaml"
    if not path.exists():
        return set()
    with open(path, "r", encoding="utf-8") as f:
        entries = yaml.safe_load(f) or []
    return {e["name"] for e in entries}


class OdooClient:
    def __init__(self, cfg):
        self.cfg = cfg
        common = xmlrpc.client.ServerProxy(f"{cfg.url}/xmlrpc/2/common")
        self.uid = common.authenticate(cfg.db, cfg.username, cfg.api_key, {})
        if not self.uid:
            raise RuntimeError(
                "Odoo authentication failed. Check ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY, "
                "and confirm this Odoo instance is on a plan with external API access (Custom)."
            )
        self.models = xmlrpc.client.ServerProxy(f"{cfg.url}/xmlrpc/2/object")

    def _search_read(self, model, domain, fields):
        return self.models.execute_kw(
            self.cfg.db, self.uid, self.cfg.api_key,
            model, "search_read",
            [domain, fields],
        )

    def _open_moves(self, move_type, fields):
        records = self._search_read(
            "account.move",
            [
                ["move_type", "=", move_type],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
            ],
            fields,
        )
        excluded = _paid_override_names()
        return [r for r in records if r["name"] not in excluded]

    def open_vendor_bills(self):
        """Posted, unpaid/partially-paid vendor bills (money going out),
        minus any confirmed-paid-but-unreconciled overrides."""
        return self._open_moves("in_invoice", ["name", "partner_id", "amount_residual", "invoice_date_due", "currency_id"])

    def open_customer_invoices(self):
        """Posted, unpaid/partially-paid customer invoices (money coming in),
        minus any confirmed-paid-but-unreconciled overrides."""
        return self._open_moves("out_invoice", ["name", "partner_id", "amount_residual", "invoice_date_due", "currency_id"])

    def bank_journals(self):
        """Lists bank journals, including their sync source, to identify which
        one is BluBanca and confirm it's actually a live sync (bank_statements_source
        == 'online_sync') rather than a manual/file-import feed."""
        return self._search_read(
            "account.journal",
            [["type", "=", "bank"]],
            ["id", "name", "bank_account_id", "bank_statements_source"],
        )

    def bank_transactions(self, journal_id, from_date=None):
        """Bank statement lines for the given journal, oldest details needed
        to build a register: date, description, counterparty, signed amount."""
        domain = [["journal_id", "=", journal_id]]
        if from_date:
            domain.append(["date", ">=", from_date])
        return self._search_read(
            "account.bank.statement.line",
            domain,
            ["id", "date", "payment_ref", "partner_id", "amount"],
        )

    def posted_customer_invoices(self, from_date=None):
        """All posted customer invoices (paid or not) with an invoice_date on
        or after from_date — used for actual invoiced revenue, unlike
        open_customer_invoices() which only returns currently-unpaid ones.
        amount_untaxed (net of VAT) is the revenue figure — amount_total
        mixes VAT-inclusive and VAT-exempt invoices inconsistently.
        amount_tax is used separately for the December VAT acconto estimate
        (see exec_data._vat_acconto_reference_liability)."""
        domain = [["move_type", "=", "out_invoice"], ["state", "=", "posted"]]
        if from_date:
            domain.append(["invoice_date", ">=", from_date])
        return self._search_read(
            "account.move",
            domain,
            ["name", "partner_id", "invoice_date", "amount_untaxed", "amount_tax"],
        )

    def posted_vendor_bills(self, from_date=None):
        """All posted vendor bills (paid or not), with invoice_date and
        amount_tax — unlike open_vendor_bills() (currently-unpaid only,
        no invoice_date/amount_tax), used to approximate VAT liability for
        a past period (the December acconto IVA's storico-method reference
        quarter, see exec_data._vat_acconto_reference_liability)."""
        domain = [["move_type", "=", "in_invoice"], ["state", "=", "posted"]]
        if from_date:
            domain.append(["invoice_date", ">=", from_date])
        return self._search_read(
            "account.move",
            domain,
            ["name", "partner_id", "invoice_date", "amount_tax"],
        )

    def invoiced_lines_with_cost(self, from_date=None):
        """Posted customer invoice product lines with their originating sale
        order line's per-unit cost (sale.order.line.purchase_price, from the
        sale_margin module), for computing gross profit. purchase_price is
        0.0 both for genuinely free items and for products with no cost ever
        recorded in Odoo — those two cases are indistinguishable from this
        field alone, so callers should treat 0.0 as "no reliable cost data"
        rather than "zero cost", per how Ultima Industrial's data actually
        looks (real products with real costs sometimes show 0.0 here)."""
        domain = [
            ["move_id.move_type", "=", "out_invoice"],
            ["move_id.state", "=", "posted"],
            ["display_type", "=", "product"],
        ]
        if from_date:
            domain.append(["move_id.invoice_date", ">=", from_date])
        move_lines = self._search_read(
            "account.move.line", domain,
            ["move_id", "product_id", "quantity", "price_subtotal", "sale_line_ids"],
        )

        sale_line_ids = sorted({sid for ml in move_lines for sid in ml["sale_line_ids"]})
        unit_cost_by_sale_line = {}
        if sale_line_ids:
            sale_lines = self._search_read("sale.order.line", [["id", "in", sale_line_ids]], ["purchase_price"])
            unit_cost_by_sale_line = {sl["id"]: sl["purchase_price"] for sl in sale_lines}

        result = []
        for ml in move_lines:
            unit_cost = unit_cost_by_sale_line.get(ml["sale_line_ids"][0]) if ml["sale_line_ids"] else None
            result.append({
                "move_id": ml["move_id"],
                "product_id": ml["product_id"],
                "quantity": float(ml["quantity"]),
                "revenue": float(ml["price_subtotal"]),
                "unit_cost": unit_cost,
            })
        return result

    def open_purchase_orders(self):
        """Confirmed purchase orders not yet arrived (state='purchase',
        effective_date not set) — the ones a forward cash flow forecast
        cares about for import VAT self-accounting. date_planned is Odoo's
        "Expected Arrival" field (labeled "Arrivo Previsto" in the Italian
        UI)."""
        return self._search_read(
            "purchase.order",
            [["state", "=", "purchase"], ["effective_date", "=", False]],
            ["name", "partner_id", "amount_total", "date_planned"],
        )

    def confirmed_purchase_orders(self):
        """All confirmed purchase orders (state 'purchase'/'done'), arrived
        or not — used to estimate a payment date for orders with no vendor
        bill yet (see exec_data._po_payment_events). Unlike
        open_purchase_orders(), not restricted to "not yet arrived" — the
        payment owed to the supplier is a separate concern from customs
        import VAT, and can fall due before or after arrival. invoice_ids
        tells us whether a real bill already exists for this PO."""
        return self._search_read(
            "purchase.order",
            [["state", "in", ["purchase", "done"]]],
            ["name", "partner_id", "date_order", "date_planned", "amount_total",
             "payment_term_id", "invoice_ids"],
        )

    def payment_term_lines(self, term_ids):
        """Lines of the given account.payment.term records, used to compute
        an estimated due-date split for a PO before any vendor bill exists.
        payment_id is the line's parent term (the M2O back-reference).
        days_next_month is only meaningful for delay_type
        'days_after_end_of_month_on_the' (e.g. "60 gg fine mese") — see
        exec_data._term_due_dates."""
        term_ids = sorted(set(term_ids))
        if not term_ids:
            return []
        return self._search_read(
            "account.payment.term.line",
            [["payment_id", "in", term_ids]],
            ["payment_id", "value", "value_amount", "nb_days", "delay_type", "days_next_month"],
        )

    def bills_by_id(self, bill_ids):
        """Posted vendor bills by id (amount_total) — used to net off what's
        already been invoiced against a purchase order's total (e.g. a
        partial 'fattura acconto') so only the true remaining balance gets
        estimated, not the PO's full value. Restricted to state='posted'
        since a draft/cancelled bill isn't a real invoiced amount yet."""
        bill_ids = sorted(set(bill_ids))
        if not bill_ids:
            return []
        return self._search_read(
            "account.move",
            [["id", "in", bill_ids], ["state", "=", "posted"]],
            ["id", "amount_total"],
        )

    def partner_countries(self, partner_ids):
        """Maps partner id -> ISO country code (e.g. 'GB'), for matching the
        import VAT country rule (config/import_vat_rules.yaml). Two-hop
        lookup since search_read can't follow a many2one's own fields
        directly: res.partner.country_id (id only) -> res.country.code."""
        partner_ids = sorted(set(partner_ids))
        if not partner_ids:
            return {}
        partners = self._search_read("res.partner", [["id", "in", partner_ids]], ["country_id"])
        country_ids = {p["country_id"][0] for p in partners if p.get("country_id")}
        countries = self._search_read("res.country", [["id", "in", list(country_ids)]], ["code"])
        code_by_country_id = {c["id"]: c["code"] for c in countries}
        return {
            p["id"]: (code_by_country_id.get(p["country_id"][0]) if p.get("country_id") else None)
            for p in partners
        }

    def sales_orders(self, from_date=None):
        """Sale orders (quotations + confirmed), excluding cancelled, with the
        fields needed for revenue, fulfillment status, and delay detection.
        commitment_date is the promised delivery date; delivery_status is
        Odoo's own fulfillment tracking (pending/partial/full). amount_untaxed
        is the net (VAT-excluded) order value, used for Sales Won (MTD)."""
        domain = [["state", "!=", "cancel"]]
        if from_date:
            domain.append(["date_order", ">=", from_date])
        return self._search_read(
            "sale.order",
            domain,
            ["name", "partner_id", "date_order", "amount_total", "amount_untaxed", "state",
             "invoice_status", "delivery_status", "commitment_date"],
        )

    def confirmed_order_lines_to_invoice_by_delivery_date(self, start_date, end_date):
        """Product lines of confirmed sale orders (state 'sale'/'done') with
        a promised delivery date (commitment_date, "Data Consegna" in the
        Italian UI) in [start_date, end_date] — used to forecast expected
        invoiced revenue by month, since Ultima invoices immediately on
        delivery ("fatture immediate"). untaxed_amount_to_invoice is Odoo's
        own computed "remaining to invoice" value per line (net of VAT) —
        it already nets out whatever's been invoiced so far, so a
        partially-invoiced order only contributes its true remaining
        value rather than double-counting against actual Revenue.
        display_type=False excludes section/note pseudo-lines, which carry
        no monetary value. order_id is returned (not the order's own
        commitment_date, which search_read can't follow through a
        relation) — see order_commitment_dates() for the matching lookup."""
        domain = [
            ["order_id.state", "in", ["sale", "done"]],
            ["order_id.commitment_date", ">=", start_date],
            ["order_id.commitment_date", "<=", end_date],
            ["display_type", "=", False],
        ]
        return self._search_read(
            "sale.order.line",
            domain,
            ["order_id", "untaxed_amount_to_invoice"],
        )

    def order_commitment_dates(self, order_ids):
        """Maps sale.order id -> raw commitment_date string, for lines
        fetched via confirmed_order_lines_to_invoice_by_delivery_date
        (which queries sale.order.line and so can't return the parent
        order's own field directly)."""
        order_ids = sorted(set(order_ids))
        if not order_ids:
            return {}
        orders = self._search_read("sale.order", [["id", "in", order_ids]], ["commitment_date"])
        return {o["id"]: o["commitment_date"] for o in orders}
