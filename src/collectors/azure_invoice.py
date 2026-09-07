"""Overlay authoritative Azure invoice figures onto collected usage.

The Azure Cost Management API returns gross usage only. The large monthly
"Azure Credit" (prepaid commitment drawdown), any other credits, and tax appear
only on the Microsoft invoice. This module reads the invoice billing summary
(``config/azure_invoices.csv``) and reconciles the Azure records so the report's
Azure summary matches the real bill: per-service charges are normalized to the
invoiced charge total, and the credit / tax lines are appended explicitly.
"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

from ..models import CostRecord

log = logging.getLogger(__name__)

_DEFAULT_PATH = "config/azure_invoices.csv"
_NUM_FIELDS = ("charges", "other_credits", "azure_credit", "subtotal", "tax", "total")


def _resolve_invoices_path(cfg: dict) -> Path | None:
    """Resolve the invoices CSV, trying cwd then the config-file directory."""
    raw = (cfg.get("azure") or {}).get("invoices_csv") or _DEFAULT_PATH
    candidates = [Path(raw)]
    if not Path(raw).is_absolute():
        config_dir = Path(os.environ.get("CONFIG_PATH", "config/config.yaml")).parent
        candidates.append(config_dir / Path(raw).name)
    for c in candidates:
        if c.exists():
            return c
    return None


def load_azure_invoices(cfg: dict) -> dict[str, dict]:
    """Invoice billing summaries, read from the DB (CSV is a seed fallback)."""
    try:
        from ..azure_catalog import load_invoices_db

        rows = load_invoices_db(cfg)
        if rows:
            return rows
    except Exception:  # noqa: BLE001 - fall back to CSV if DB layer unavailable
        pass
    return _load_invoices_csv(cfg)


def _load_invoices_csv(cfg: dict) -> dict[str, dict]:
    """Return {month_key: {charges, other_credits, azure_credit, subtotal, tax, total}}."""
    path = _resolve_invoices_path(cfg)
    if path is None:
        log.info("Azure invoices file not found; skipping credit overlay")
        return {}
    out: dict[str, dict] = {}
    with path.open(newline="") as f:
        reader = csv.DictReader(row for row in f if not row.lstrip().startswith("#"))
        for row in reader:
            month = (row.get("month") or "").strip()
            if not month:
                continue
            try:
                summary = {k: float(row.get(k) or 0) for k in _NUM_FIELDS}
            except ValueError:
                log.warning("Azure invoices: unparseable row for %s; skipped", month)
                continue
            summary["invoice_number"] = (row.get("invoice_number") or "").strip()
            out[month] = summary
    return out


def _azure_charge_rows(records: list[CostRecord]) -> list[CostRecord]:
    return [
        r
        for r in records
        if r.cloud == "Azure" and r.cost > 0 and r.credits == 0 and r.tax == 0
    ]


def apply_azure_invoice_overlay(
    records: list[CostRecord], cfg: dict, month_key: str
) -> list[CostRecord]:
    """Reconcile Azure records to the invoice billing summary for *month_key*.

    No-ops when no invoice row exists for the month or there is no Azure usage.
    Disabled entirely when ``azure.invoice_overlay`` is false in config — in that
    case the raw Cost Management gross usage is reported as-is (no per-service
    rescaling and no invoice-derived credit/tax lines).
    """
    if (cfg.get("azure") or {}).get("invoice_overlay", True) is False:
        log.info("Azure invoice overlay disabled (azure.invoice_overlay=false); using raw usage")
        return records

    inv = load_azure_invoices(cfg).get(month_key)
    if not inv:
        return records

    # Drop any previously-applied Azure credit/tax overlay lines so re-runs are idempotent.
    base = [
        r for r in records if not (r.cloud == "Azure" and (r.credits > 0 or r.tax > 0))
    ]
    charge_rows = _azure_charge_rows(base)
    api_gross = round(sum(r.cost for r in charge_rows), 2)
    if api_gross <= 0:
        return records

    # Normalize per-service charges to the invoiced charge total (keeps proportions).
    invoiced_charges = round(inv["charges"], 2)
    factor = invoiced_charges / api_gross if api_gross else 1.0
    if abs(factor - 1.0) > 1e-9:
        for r in charge_rows:
            r.cost = round(r.cost * factor, 2)
    # Absorb per-row rounding drift into the largest line so the subtotal equals
    # the invoiced charges exactly (and thus amount due equals the invoice total).
    drift = round(invoiced_charges - sum(r.cost for r in charge_rows), 2)
    if drift and charge_rows:
        biggest = max(charge_rows, key=lambda r: r.cost)
        biggest.cost = round(biggest.cost + drift, 2)

    period_start = charge_rows[0].period_start
    period_end = charge_rows[0].period_end

    def _line(service: str, *, credits: float = 0.0, tax: float = 0.0) -> CostRecord:
        return CostRecord(
            cloud="Azure",
            service=service,
            cost=0.0,
            credits=round(credits, 2),
            tax=round(tax, 2),
            publisher="Microsoft",
            environment="production",
            period_start=period_start,
            period_end=period_end,
        )

    if inv.get("azure_credit", 0) > 0:
        base.append(_line("Azure Credit", credits=inv["azure_credit"]))
    if inv.get("other_credits", 0) > 0:
        base.append(_line("Credits", credits=inv["other_credits"]))
    if inv.get("tax", 0) > 0:
        base.append(_line("Tax", tax=inv["tax"]))

    log.info(
        "Azure invoice overlay %s: charges=%.2f credit=%.2f tax=%.2f amount_due=%.2f",
        month_key,
        invoiced_charges,
        inv.get("azure_credit", 0) + inv.get("other_credits", 0),
        inv.get("tax", 0),
        inv.get("total", 0),
    )
    return base
