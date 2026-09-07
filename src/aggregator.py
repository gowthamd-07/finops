"""Aggregate normalized records into the report data structure."""
from __future__ import annotations

from collections import defaultdict

from .billing import (
    is_credit_line,
    is_tax_line,
    line_credits,
    line_subtotal,
    line_tax,
    reconcile_cloud,
)
from .history import HistoryStore
from .models import CloudSummary, CostRecord, ReportData


def build_report(
    records: list[CostRecord],
    month: str,
    year: int,
    currency: str,
    history: HistoryStore | None = None,
    month_key: str | None = None,
) -> ReportData:
    summaries = _summaries(records)
    report = ReportData(
        month=month,
        year=year,
        currency=currency,
        summaries=summaries,
        records=records,
    )

    # Per-service totals across all clouds for MoM comparison.
    services: dict[str, float] = defaultdict(float)
    for r in records:
        if is_tax_line(r) or is_credit_line(r):
            continue
        services[f"{r.cloud}:{r.service}"] += line_subtotal(r)
    services = {k: round(v, 2) for k, v in services.items()}

    report.curr_total = report.grand_total

    if history and month_key:
        history.save_month(month_key, report.grand_total, services)

    return report


def _summaries(records: list[CostRecord]) -> list[CloudSummary]:
    by_cloud: dict[str, CloudSummary] = {}
    for r in records:
        s = by_cloud.setdefault(r.cloud, CloudSummary(cloud=r.cloud))
        s.subtotal += line_subtotal(r)
        s.credits += line_credits(r)
        s.tax += line_tax(r)
    for s in by_cloud.values():
        s.subtotal = round(s.subtotal, 2)
        s.credits = round(s.credits, 2)
        s.tax = round(s.tax, 2)
    for cloud in by_cloud:
        reconcile_cloud(records, cloud, context="summary")
    # Stable order: AWS, Azure, GCP, then LLM providers.
    order = {"AWS": 0, "Azure": 1, "GCP": 2, "Cursor": 3, "Claude": 4}
    return sorted(by_cloud.values(), key=lambda s: order.get(s.cloud, 99))
