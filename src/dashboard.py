"""
Renders the "Ultima Pulse" executive dashboard as a static HTML file, using
the same data aggregation as the Google Sheet executive report (src/exec_data.py).

The template (templates/dashboard_template.html) contains a __DATA_JSON__
placeholder that gets replaced with the real snapshot data as a JSON blob —
the page itself is fully static (no live API calls from the browser), since
there's no Odoo/Sheets connector available to a hosted page in this setup.
"""

import json

from .config import ROOT_DIR
from . import exec_data

TEMPLATE_PATH = ROOT_DIR / "templates" / "dashboard_template.html"


def _build_payload(data):
    s = data["sales"]
    f = data["finance"]

    overdue_today_total = sum(float(i["amount_residual"]) for i in f["overdue_receivables_today"])
    open_payables_total = sum(float(b["amount_residual"]) for b in f["open_bills"])

    from . import exec_report  # local import avoids a circular import at module load time
    narrative = exec_report._narrative(data)

    return {
        "generated_at": data["today"].isoformat(),
        "today": data["today"].isoformat(),
        "yesterday": data["yesterday"].isoformat(),
        "narrative": narrative,
        "kpis": {
            "revenue_today": s["revenue_today"], "revenue_yesterday": s["revenue_yesterday"],
            "revenue_last_month": s["revenue_last_month"], "revenue_ytd": s["revenue_ytd"],
            "new_orders_today": s["new_orders_today"], "new_orders_yesterday": s["new_orders_yesterday"],
            "delayed_orders": len(s["delayed_orders"]), "delayed_orders_yesterday": s["delayed_orders_count_yesterday"],
            "cash_flow_today": f["cash_flow_today"], "cash_flow_yesterday": f["cash_flow_yesterday"],
            "overdue_receivables_today": overdue_today_total, "overdue_receivables_yesterday": f["overdue_receivables_yesterday_total"],
            "latest_balance": f["latest_balance"],
            "open_payables_total": open_payables_total, "open_payables_count": len(f["open_bills"]),
            "open_receivables_total": sum(float(i["amount_residual"]) for i in f["open_invoices"]), "open_receivables_count": len(f["open_invoices"]),
            "gp_percent": s["gp_percent"], "gp_coverage_percent": s["gp_coverage_percent"],
            "quotes_raised_mtd": s["quotes_raised_mtd"], "quotes_raised_mtd_yesterday": s["quotes_raised_mtd_yesterday"],
        },
        "revenue_trend": [[d.isoformat(), amt] for d, amt in s["revenue_trend"]],
        "balance_trend": [[d.isoformat(), bal] for d, bal in f["balance_trend"]],
        "forecast_trend": [[d.isoformat(), bal] for d, bal in f["forecast_trend"]],
        "status_breakdown": s["status_breakdown"],
        "top_customers": s["top_customers"],
        "receivables_aging": [f["receivables_aging"].get(b, 0.0) for b in exec_data.AGING_BUCKETS],
        "payables_aging": [f["payables_aging"].get(b, 0.0) for b in exec_data.AGING_BUCKETS],
        "aging_buckets": exec_data.AGING_BUCKETS,
        "top_overdue_receivables": sorted([
            {"name": i["name"], "customer": i["partner_id"][1] if i["partner_id"] else "",
             "due_date": i["due_date"].isoformat() if i["due_date"] else None, "amount": float(i["amount_residual"])}
            for i in f["overdue_receivables_today"]
        ], key=lambda r: -r["amount"])[:8],
        "open_bills_list": sorted([
            {"name": b["name"], "customer": b["partner_id"][1] if b["partner_id"] else "",
             "due_date": b["due_date"].isoformat() if b["due_date"] else None, "amount": float(b["amount_residual"])}
            for b in f["open_bills"]
        ], key=lambda r: -r["amount"])[:8],
    }


def render_html(data):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()
    payload = _build_payload(data)
    return template.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))
