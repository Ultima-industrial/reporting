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
        open_customer_invoices() which only returns currently-unpaid ones."""
        domain = [["move_type", "=", "out_invoice"], ["state", "=", "posted"]]
        if from_date:
            domain.append(["invoice_date", ">=", from_date])
        return self._search_read(
            "account.move",
            domain,
            ["name", "partner_id", "invoice_date", "amount_total"],
        )

    def sales_orders(self, from_date=None):
        """Sale orders (quotations + confirmed), excluding cancelled, with the
        fields needed for revenue, fulfillment status, and delay detection.
        commitment_date is the promised delivery date; delivery_status is
        Odoo's own fulfillment tracking (pending/partial/full)."""
        domain = [["state", "!=", "cancel"]]
        if from_date:
            domain.append(["date_order", ">=", from_date])
        return self._search_read(
            "sale.order",
            domain,
            ["name", "partner_id", "date_order", "amount_total", "state",
             "invoice_status", "delivery_status", "commitment_date"],
        )
