import os
from datetime import date
from pathlib import Path

from src import exec_data
from src.config import OdooConfig, STARTING_BALANCE_AMOUNT, STARTING_BALANCE_DATE
from src.dashboard import render_html
from src.netlify_deploy import deploy
from src.odoo_client import OdooClient

OUTPUT_DIR = Path(__file__).resolve().parent / "site"


def run():
    odoo = OdooClient(OdooConfig())
    starting_balance_date = date.fromisoformat(STARTING_BALANCE_DATE)
    data = exec_data.build(odoo, odoo.cfg.bank_journal_id, float(STARTING_BALANCE_AMOUNT), starting_balance_date)

    html = render_html(data)

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")

    auth_token = os.environ.get("NETLIFY_AUTH_TOKEN")
    site_id = os.environ.get("NETLIFY_SITE_ID")
    if auth_token and site_id:
        result = deploy(OUTPUT_DIR, site_id, auth_token)
        print(f"Deployed: {result.get('deploy_ssl_url') or result.get('url')}")
    else:
        print(f"NETLIFY_AUTH_TOKEN/NETLIFY_SITE_ID not set — generated {OUTPUT_DIR / 'index.html'} locally, did not deploy.")


if __name__ == "__main__":
    run()
