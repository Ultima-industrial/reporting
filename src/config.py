import os
from pathlib import Path

from dotenv import load_dotenv

try:
    # On managed/corporate Windows machines, Python's bundled CA list often
    # doesn't include a TLS-inspecting proxy's root cert, even though the OS
    # trust store (which the proxy's root gets installed into) does. This
    # makes all outbound HTTPS (Odoo, Google Sheets) use the OS store instead.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"


def _require(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file (see .env.example)."
        )
    return value


class OdooConfig:
    def __init__(self):
        self.url = _require("ODOO_URL")
        self.db = _require("ODOO_DB")
        self.username = _require("ODOO_USERNAME")
        self.api_key = _require("ODOO_API_KEY")
        self.bank_journal_id = int(_require("ODOO_BANK_JOURNAL_ID"))


class SheetsConfig:
    def __init__(self):
        self.service_account_file = _require("GOOGLE_SERVICE_ACCOUNT_FILE")
        self.sheet_id = _require("GOOGLE_SHEET_ID")


CURRENCY = os.environ.get("CURRENCY", "EUR")
INITIAL_LOOKBACK_DAYS = int(os.environ.get("INITIAL_LOOKBACK_DAYS", "90"))

# Only used to seed the Register's running balance on the very first run,
# when the sheet has no prior rows to continue from.
STARTING_BALANCE_AMOUNT = os.environ.get("STARTING_BALANCE_AMOUNT")
STARTING_BALANCE_DATE = os.environ.get("STARTING_BALANCE_DATE")

CATEGORIES = [
    "Travel",
    "Consulting",
    "Food",
    "Tax & Duties",
    "Personnel",
    "Other",
    "Software/SaaS",
    "Utilities",
    "Banking",
    "Goods",
]
