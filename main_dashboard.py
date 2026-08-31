from datetime import date
from pathlib import Path

from src import exec_data
from src.config import OdooConfig, STARTING_BALANCE_AMOUNT, STARTING_BALANCE_DATE
from src.dashboard import render_html
from src.odoo_client import OdooClient

OUTPUT_DIR = Path(__file__).resolve().parent / "site"


def run():
    odoo = OdooClient(OdooConfig())
    starting_balance_date = date.fromisoformat(STARTING_BALANCE_DATE)
    data = exec_data.build(odoo, odoo.cfg.bank_journal_id, float(STARTING_BALANCE_AMOUNT), starting_balance_date)

    html = render_html(data)

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"Generated {OUTPUT_DIR / 'index.html'}")


if __name__ == "__main__":
    run()
