"""Build template context for the in-app report detail page."""
from __future__ import annotations

from ..analytics import credits_configured, credits_projection
from ..models import ReportData
from ..report.changes import enrich_mom_changes, normalize_change_rows
from ..report.filters import filter_options, subscription_map
from ..report.groups import (
    ai_breakdown,
    aws_breakdown,
    azure_cost_breakdown,
    gcp_breakdown,
    gcp_by_project,
    group_azure_by_subscription,
    group_by_cloud,
    subscription_names,
)
from ..store import ReportStore


def _prev_month_key(month_key: str) -> str | None:
    year, month = (int(x) for x in month_key.split("-"))
    pm = month - 1 or 12
    py = year if month > 1 else year - 1
    return f"{py:04d}-{pm:02d}"


_MOM_CLOUDS = ("AWS", "Azure", "GCP", "Cursor", "Claude")


def _delta(now: float, prev: float) -> dict:
    change = round(now - prev, 2)
    pct = round(change / prev * 100, 1) if prev else None
    return {"prev": round(prev, 2), "change": change, "pct": pct}


def _mom_context(trend: list[dict], month_key: str | None) -> dict:
    """Month-over-month deltas, per-cloud sparklines and an anomaly callout.

    Uses store.trend() rows: {month, grand_total, subtotal, credits, clouds}.
    """
    empty = {
        "has_prev": False,
        "spark_overall": [],
        "spark_clouds": {c: [] for c in _MOM_CLOUDS},
        "clouds": {},
        "amount_due": None,
        "gross": None,
        "anomaly": None,
    }
    if not trend or not month_key:
        return empty
    hist = [t for t in trend if t["month"] <= month_key]
    if not hist:
        return empty
    cur = hist[-1]
    prev = hist[-2] if len(hist) >= 2 else None
    recent = hist[-12:]

    spark_overall = [round(t["grand_total"], 2) for t in recent]
    spark_clouds = {
        c: [round((t.get("clouds") or {}).get(c, 0) or 0, 2) for t in recent]
        for c in _MOM_CLOUDS
    }

    per_cloud: dict[str, dict] = {}
    anomaly = None
    amount_due = gross = None
    if prev:
        for c in _MOM_CLOUDS:
            now = (cur.get("clouds") or {}).get(c, 0) or 0
            was = (prev.get("clouds") or {}).get(c, 0) or 0
            if now == 0 and was == 0:
                continue
            d = _delta(now, was)
            per_cloud[c] = d
            if (
                d["pct"] is not None
                and abs(d["pct"]) >= 10
                and abs(d["change"]) >= 50
                and (anomaly is None or abs(d["pct"]) > abs(anomaly["pct"]))
            ):
                anomaly = {
                    "cloud": c,
                    "pct": d["pct"],
                    "change": d["change"],
                    "direction": "up" if d["change"] >= 0 else "down",
                }
        amount_due = _delta(cur["grand_total"], prev["grand_total"])
        gross = _delta(cur["subtotal"], prev["subtotal"])

    return {
        "has_prev": prev is not None,
        "spark_overall": spark_overall,
        "spark_clouds": spark_clouds,
        "clouds": per_cloud,
        "amount_due": amount_due,
        "gross": gross,
        "anomaly": anomaly,
    }


def build_report_context(
    report: ReportData,
    cfg: dict | None = None,
    *,
    store: ReportStore | None = None,
    month_key: str | None = None,
) -> dict:
    cfg = cfg or {}
    if store and month_key:
        prev_report = store.load(_prev_month_key(month_key) or "")
        if prev_report:
            enrich_mom_changes(report, prev_report.records)

    key_increase_rows = normalize_change_rows(report.key_increases)
    savings_rows = normalize_change_rows(report.savings)
    report.key_increases = key_increase_rows
    report.savings = savings_rows

    sub_map = subscription_map(cfg)
    names = subscription_names(cfg)

    # Recompute prepaid-credit projection from current config (not stale stored data).
    trend = store.trend() if store else []
    if store and credits_configured(cfg):
        report.credits_projection = credits_projection(cfg, trend)
    else:
        report.credits_projection = None

    return {
        "report": report,
        "key_increase_rows": key_increase_rows,
        "savings_rows": savings_rows,
        "mom": _mom_context(trend, month_key),
        "clouds": group_by_cloud(report),
        "azure_sub": group_azure_by_subscription(report.records, sub_map, names),
        "azure_breakdown": azure_cost_breakdown(report.records, sub_map),
        "azure_rgs": {},
        "aws": aws_breakdown(report.records),
        "gcp": gcp_breakdown(report.records),
        "gcp_project": gcp_by_project(report.records),
        "ai": ai_breakdown(report.records, cfg),
        "filter_options": filter_options(report.records, sub_map, cfg),
        "subscription_names": names,
    }
