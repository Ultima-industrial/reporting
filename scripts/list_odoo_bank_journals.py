"""
One-off helper: lists Odoo bank journals with their sync source, so you can
find ODOO_BANK_JOURNAL_ID for the BluBanca feed and confirm it's a live sync
(bank_statements_source == 'online_sync') rather than manual/file-import.

Usage: set ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY in .env, then:
    python scripts/list_odoo_bank_journals.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import OdooConfig  # noqa: E402
from src.odoo_client import OdooClient  # noqa: E402


class _PartialOdooConfig(OdooConfig):
    """ODOO_BANK_JOURNAL_ID isn't known yet — that's the point of this script."""

    def __init__(self):
        import os

        self.url = os.environ["ODOO_URL"]
        self.db = os.environ["ODOO_DB"]
        self.username = os.environ["ODOO_USERNAME"]
        self.api_key = os.environ["ODOO_API_KEY"]
        self.bank_journal_id = None


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    odoo = OdooClient(_PartialOdooConfig())
    for journal in odoo.bank_journals():
        print(
            f"id={journal['id']:<6} name={journal['name']!r:<30} "
            f"bank_account={journal.get('bank_account_id')!r:<20} "
            f"sync_source={journal.get('bank_statements_source')!r}"
        )
