"""
Odoo external API client (XML-RPC), read-only.

Requires the Odoo Custom plan (or equivalent self-hosted Enterprise setup) —
XML-RPC/JSON-RPC access is not included in Standard or One App Free. If
authentication fails with an access-rights error, that's a plan/permissions
issue to resolve in Odoo, not a bug in this client.
"""

import xmlrpc.client


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

    def open_vendor_bills(self):
        """Posted, unpaid/partially-paid vendor bills (money going out)."""
        return self._search_read(
            "account.move",
            [
                ["move_type", "=", "in_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
            ],
            ["name", "partner_id", "amount_residual", "invoice_date_due", "currency_id"],
        )

    def open_customer_invoices(self):
        """Posted, unpaid/partially-paid customer invoices (money coming in)."""
        return self._search_read(
            "account.move",
            [
                ["move_type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
            ],
            ["name", "partner_id", "amount_residual", "invoice_date_due", "currency_id"],
        )

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
