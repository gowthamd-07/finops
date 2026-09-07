"""Seed the store with realistic *synthetic* multi-cloud + AI demo data.

For screenshots/demo only. No real billing data is used or fetched — every
number here is made up. Builds several months through the real reporting
pipeline (build_report + analytics + MoM enrichment) so every dashboard view,
tab, executive summary, trend, and forecast renders exactly as in production.

Run (from the repo root), with a reachable DATABASE_URL:

    DATABASE_URL=postgresql://ccr@127.0.0.1:5433/costreporter \
    CONFIG_PATH=config/config.yaml \
    .venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import calendar
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.aggregator import build_report  # noqa: E402
from src.analytics import build_analytics, credits_configured  # noqa: E402
from src.config import load_config  # noqa: E402
from src.models import CostRecord  # noqa: E402
from src.report.changes import enrich_mom_changes, normalize_change_rows  # noqa: E402
from src.report.groups import azure_gross  # noqa: E402
from src.store import ReportStore  # noqa: E402

MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

# Per-month scale factors (oldest -> newest) to create a believable trend.
GEN_SCALE = [0.80, 0.86, 0.92, 0.96, 1.00]          # general infra growth
AI_GCP_SCALE = [0.20, 0.28, 0.35, 0.42, 1.00]        # Gemini spike in the last month
AI_TOOL_SCALE = [0.55, 0.68, 0.80, 0.88, 1.00]       # Cursor / Claude adoption ramp


def R(cloud, service, cost, **kw):
    return CostRecord(cloud=cloud, service=service, cost=round(float(cost), 2), **kw)


def _aws(gen):
    env = {"p": "production", "s": "shared", "n": "non-production"}
    rows = [
        ("Amazon EC2", 4200, "p"), ("Amazon RDS", 2600, "p"),
        ("Amazon S3", 900, "p"), ("Amazon OpenSearch Service", 1300, "p"),
        ("Amazon CloudFront", 480, "s"), ("AWS Data Transfer", 300, "s"),
        ("AWS Lambda", 220, "n"),
    ]
    recs = [R("AWS", s, c * gen, environment=env[e], account="Example AWS (prod)")
            for s, c, e in rows]
    recs.append(R("AWS", "Tax", 0, tax=round(120 * gen, 2), environment="shared",
                  account="Example AWS (prod)"))
    recs.append(R("AWS", "Solution Provider Program Discount", 0,
                  credits=round(150 * gen, 2), environment="shared",
                  account="Example AWS (prod)"))
    return recs


def _azure(gen):
    # (service, resource_group, subscription, environment, cost)
    rows = [
        ("Virtual Machines", "rg-app-pd-eu-01", "Production", "production", 9000),
        ("Azure Database for PostgreSQL", "rg-app-pd-eu-01", "Production", "production", 5200),
        ("Storage", "rg-app-pd-eu-01", "Production", "production", 2100),
        ("Azure Kubernetes Service", "rg-ai-pd-eu-01", "Production", "production", 7400),
        ("Virtual Machines", "rg-vre-pd-eu-01", "Production", "production", 6100),
        ("Azure Database for PostgreSQL", "rg-vre-pd-eu-01", "Production", "production", 4300),
        ("Storage", "rg-bioinfo-pd-eu-01", "Production", "production", 3800),
        ("Log Analytics", "rg-shared-pd-eu-01", "Production", "production", 900),
        ("Container Registry", "rg-shared-pd-eu-01", "Production", "production", 300),
        ("Virtual Machines", "rg-app-st-eu-01", "NonProduction", "non-production", 2600),
        ("Azure Database for PostgreSQL", "rg-vre-dv-eu-01", "NonProduction", "non-production", 3100),
        ("Storage", "rg-ai-dv-eu-01", "NonProduction", "non-production", 1400),
        ("Azure Kubernetes Service", "rg-ai-st-eu-01", "NonProduction", "non-production", 2200),
        ("Azure Firewall", "rg-network-connectivity-eu-01", "Connectivity", "shared", 2600),
        ("Application Gateway", "rg-network-connectivity-eu-01", "Connectivity", "shared", 1200),
        ("VPN Gateway", "rg-network-connectivity-eu-01", "Connectivity", "shared", 700),
        ("Log Analytics", "rg-management-eu-01", "Management", "shared", 1500),
        ("Backup", "rg-management-eu-01", "Management", "shared", 800),
    ]
    recs = [R("Azure", s, c * gen, environment=env, subscription=sub, scope=rg)
            for s, rg, sub, env, c in rows]
    # Invoice overlay: applied Azure credit + tax (as their own lines).
    recs.append(R("Azure", "Azure Credit", 0, credits=round(50000 * gen, 2),
                  environment="shared", subscription="Production"))
    recs.append(R("Azure", "Tax", 0, tax=round(450 * gen, 2),
                  environment="shared", subscription="Production"))
    return recs


def _gcp(gen, ai):
    # (service, project, environment, cost, model, tokens, unit)
    rows = [
        ("Compute Engine", "gcp-prod", "production", 3200 * gen, "", 0, ""),
        ("Cloud SQL", "gcp-prod", "production", 1800 * gen, "", 0, ""),
        ("Cloud Storage", "gcp-prod", "production", 900 * gen, "", 0, ""),
        ("Networking", "gcp-prod", "production", 600 * gen, "", 0, ""),
        ("Compute Engine", "gcp-dev", "non-production", 1100 * gen, "", 0, ""),
        ("BigQuery", "gcp-dev", "non-production", 700 * gen, "", 0, ""),
        ("Cloud Logging", "gcp-shared", "shared", 300 * gen, "", 0, ""),
        ("Vertex AI", "ml-dev", "non-production", 2400 * ai,
         "Gemini 1.5 Pro", 820_000_000 * ai, "tokens"),
        ("Generative Language API", "ml-dev", "non-production", 1500 * ai,
         "Gemini 1.5 Flash", 1_250_000_000 * ai, "tokens"),
    ]
    recs = []
    for s, proj, env, c, model, tok, unit in rows:
        recs.append(R("GCP", s, c, environment=env, scope=proj, account="0123AB-CDEF01",
                      model=model, tokens=round(tok, 0), usage_unit=unit))
    recs.append(R("GCP", "Committed Use Discount", 0, credits=round(1200 * gen, 2),
                  environment="production", scope="gcp-prod"))
    return recs


def _cursor(ai):
    users = [
        ("claude-3.5-sonnet", "alice@example.com", 210, 45_000_000),
        ("claude-3.5-sonnet", "bob@example.com", 180, 38_000_000),
        ("gpt-4o", "carol@example.com", 95, 12_000_000),
        ("auto", "dave@example.com", 60, 9_000_000),
        ("gemini-1.5-pro", "erin@example.com", 40, 6_000_000),
    ]
    recs = [R("Cursor", m, c * ai, environment="production", account="Cursor Team",
              model=m, tokens=round(t * ai, 0), usage_unit="tokens", user=u)
            for m, u, c, t in users]
    # Seat subscription lines (base + WA sales tax).
    for label, seats in (("Cursor Teams (42 seats)", 42), ("Bugbot (8 seats)", 8)):
        base = seats * 40.0
        recs.append(R("Cursor", label, base, tax=round(base * 0.103, 2),
                      environment="production", account="Cursor Team",
                      tokens=float(seats), usage_unit="seats"))
    return recs


def _claude(ai):
    rows = [
        ("claude-3-5-sonnet-20241022", "prod-agent-key", 520, 90_000_000),
        ("claude-3-opus-20240229", "research-key", 300, 20_000_000),
        ("claude-3-haiku-20240307", "batch-pipeline-key", 80, 40_000_000),
    ]
    recs = []
    for m, u, c, t in rows:
        cost = c * ai
        recs.append(R("Claude", m, cost, tax=round(cost * 0.103, 2),
                      environment="production", account="Anthropic Org",
                      model=m, tokens=round(t * ai, 0), usage_unit="tokens", user=u))
    return recs


def build_month(idx: int) -> list[CostRecord]:
    gen, ai_gcp, ai_tool = GEN_SCALE[idx], AI_GCP_SCALE[idx], AI_TOOL_SCALE[idx]
    return (_aws(gen) + _azure(gen) + _gcp(gen, ai_gcp)
            + _cursor(ai_tool) + _claude(ai_tool))


def main() -> None:
    cfg = load_config(os.environ.get("CONFIG_PATH", "config/config.yaml"))
    store = ReportStore(cfg)
    currency = cfg.get("report", {}).get("currency", "USD")

    for idx, month_key in enumerate(MONTHS):
        year, month = (int(x) for x in month_key.split("-"))
        month_name = calendar.month_name[month]
        records = build_month(idx)
        report = build_report(records, month=month_name, year=year,
                              currency=currency, history=None, month_key=month_key)

        # Trend points (stored months + this one) — mirrors service.generate.
        points = [p for p in store.trend() if p["month"] != month_key]
        points.append({
            "month": month_key,
            "grand_total": report.grand_total,
            "subtotal": report.gross_subtotal,
            "credits": round(sum(s.credits for s in report.summaries), 2),
            "clouds": {s.cloud: s.total for s in report.summaries},
        })
        points.sort(key=lambda p: p["month"])
        analytics = build_analytics(cfg, points)
        report.mom = analytics["mom"]
        report.period_summaries = analytics["period_summaries"]
        report.credits_projection = (
            analytics["credits_projection"] if credits_configured(cfg) else None
        )
        report.anthropic_credits = analytics["anthropic_credits"]
        report.azure_curr_gross = azure_gross(report)

        prev_key = MONTHS[idx - 1] if idx > 0 else None
        prev = store.load(prev_key) if prev_key else None
        if prev:
            report.azure_prev_gross = azure_gross(prev)
            enrich_mom_changes(report, prev.records)
        report.key_increases = normalize_change_rows(report.key_increases)
        report.savings = normalize_change_rows(report.savings)

        store.save(month_key, report, b"")  # empty PDF: screenshots use the web UI
        print(f"seeded {month_key}: grand_total=${report.grand_total:,.2f} "
              f"gross=${report.gross_subtotal:,.2f} records={len(records)}")

    print("months in store:", store.list_months())


if __name__ == "__main__":
    main()
