"""Shared report-generation service used by both the CLI and the web app."""
from __future__ import annotations

import calendar
import logging
import os
from pathlib import Path
from dataclasses import dataclass
from datetime import date

from dateutil.relativedelta import relativedelta

from .months import latest_allowed_month, latest_allowed_month_key, validate_month_key
from .aggregator import build_report
from .analytics import build_analytics, credits_configured
from .collectors import (
    apply_azure_invoice_overlay,
    collect_anthropic,
    collect_aws,
    collect_azure,
    collect_cursor,
    collect_gcp,
)
from .history import HistoryStore
from .models import ReportData
from .store import ReportStore

log = logging.getLogger(__name__)

_COLLECTORS = {
    "aws": ("AWS", collect_aws),
    "azure": ("Azure", collect_azure),
    "gcp": ("GCP", collect_gcp),
    "cursor": ("Cursor", collect_cursor),
    "anthropic": ("Claude", collect_anthropic),
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}
_ENV_FLAG = {
    "aws": "AWS_ENABLED",
    "azure": "AZURE_ENABLED",
    "gcp": "GCP_ENABLED",
    "cursor": "CURSOR_ENABLED",
    "anthropic": "ANTHROPIC_ENABLED",
}
_DEFAULT_ENABLED = {
    "aws": True,
    "azure": True,
    "gcp": True,
    "cursor": True,
    "anthropic": True,
}


def _coerce_bool(val, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def _is_enabled(cfg: dict, key: str) -> bool:
    """Whether a cloud collector runs.

    Precedence: the AWS_ENABLED / AZURE_ENABLED env var (if set) wins, then the
    config `enabled` value, then a per-cloud default (enabled).
    """
    default = _DEFAULT_ENABLED.get(key, True)
    env_val = os.environ.get(_ENV_FLAG.get(key, ""))
    if env_val not in (None, ""):
        return _coerce_bool(env_val, default)
    return _coerce_bool(cfg.get(key, {}).get("enabled", default), default)


@dataclass
class ReportResult:
    month_key: str
    month_name: str
    year: int
    report: ReportData
    pdf_bytes: bytes
    csv_bytes: bytes
    cached: bool = False


def target_month(arg: str | None) -> tuple[date, date]:
    """Return (start, end-exclusive). Default = previous full month."""
    if arg:
        month_key = validate_month_key(arg.strip())
        year, month = (int(x) for x in month_key.split("-"))
        start = date(year, month, 1)
    else:
        start = latest_allowed_month()
    last_day = calendar.monthrange(start.year, start.month)[1]
    end = date(start.year, start.month, last_day) + relativedelta(days=1)
    return start, end


def collect_records(cfg: dict, start: date, end: date, clouds: set[str] | None):
    selected = clouds or set(_COLLECTORS)
    records = []
    for key in selected:
        if key not in _COLLECTORS:
            log.warning("Unknown cloud '%s' ignored", key)
            continue
        if not _is_enabled(cfg, key):
            log.info("%s is disabled via config (enabled=false); skipping", key)
            continue
        name, fn = _COLLECTORS[key]
        try:
            records.extend(fn(cfg, start, end))
        except Exception:  # noqa: BLE001 - one cloud failing shouldn't abort the rest
            log.exception("%s collection failed; continuing without it", name)
    return records


def generate(
    cfg: dict,
    month: str | None = None,
    clouds: set[str] | None = None,
    persist: bool = True,
    force: bool = False,
) -> ReportResult:
    """Collect -> aggregate -> render PDF. Optionally persist to the store.

    When *persist* is true and the month already exists in the database, returns
    the stored report without calling cloud APIs unless *force* is set.
    """
    start, end = target_month(month)
    month_name = calendar.month_name[start.month]
    month_key = f"{start.year:04d}-{start.month:02d}"

    store = ReportStore(cfg) if persist else None
    if store and not force:
        cached = store.load_cached(month_key)
        if cached:
            report, pdf_bytes = cached
            log.info("Using stored report for %s (skipped cloud API collection)", month_key)
            from .report import build_csv_bytes

            csv_bytes = build_csv_bytes(report, cfg)
            return ReportResult(
                month_key, month_name, start.year, report, pdf_bytes, csv_bytes, cached=True
            )

    log.info("Generating report for %s %s [%s, %s)", month_name, start.year, start, end)

    records = collect_records(cfg, start, end, clouds)
    if not records:
        raise RuntimeError("No cost records collected from any cloud")

    # Pull the authoritative invoice (Azure Credit + tax) from the Billing API
    # and cache it in the DB; the Cost Management usage API can't return it.
    if _is_enabled(cfg, "azure"):
        from .collectors.azure_billing import fetch_and_store_invoice

        fetch_and_store_invoice(cfg, month_key)

    # Overlay the invoice credit/tax onto the Azure usage so the Azure summary
    # matches the real bill (reads the DB-cached invoice, CSV as fallback).
    records = apply_azure_invoice_overlay(records, cfg, month_key)

    history = HistoryStore(cfg) if persist else None
    report = build_report(
        records,
        month=month_name,
        year=start.year,
        currency=cfg.get("report", {}).get("currency", "USD"),
        history=history,
        month_key=month_key,
    )

    # Trend analytics: combine stored months with the current (unsaved) one.
    points = [p for p in (store.trend() if store else []) if p["month"] != month_key]
    points.append({
        "month": month_key,
        "grand_total": report.grand_total,
        "subtotal": report.gross_subtotal,
        "credits": round(sum(s.credits for s in report.summaries), 2),
        # Per-cloud net totals so usage-driven credit tracking (Anthropic) can
        # read this month's Claude spend; historical points carry this already.
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

    from .report.changes import enrich_mom_changes, normalize_change_rows
    from .report.groups import azure_gross

    report.azure_curr_gross = azure_gross(report)
    if store:
        py, pm = (start.year, start.month - 1)
        if pm < 1:
            pm, py = 12, py - 1
        prev_key = f"{py:04d}-{pm:02d}"
        prev_report = store.load(prev_key)
        if prev_report:
            report.azure_prev_gross = azure_gross(prev_report)
            enrich_mom_changes(report, prev_report.records)

    report.key_increases = normalize_change_rows(report.key_increases)
    report.savings = normalize_change_rows(report.savings)

    title = cfg.get("report", {}).get("title", "Monthly Cloud Infrastructure Cost")
    import tempfile

    from .report import build_csv_bytes, build_pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        build_pdf(report, tmp.name, title, cfg)
        pdf_bytes = Path(tmp.name).read_bytes()
    Path(tmp.name).unlink(missing_ok=True)

    csv_bytes = build_csv_bytes(report, cfg)

    if store is not None:
        store.save(month_key, report, pdf_bytes)

    return ReportResult(
        month_key, month_name, start.year, report, pdf_bytes, csv_bytes, cached=False
    )
