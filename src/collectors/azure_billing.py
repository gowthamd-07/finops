"""Fetch Azure invoice billing summaries from the Billing API.

The Cost Management *usage* API does not return the monthly "Azure Credit"
(prepaid commitment drawdown) or tax. Those live on the invoice, exposed by the
Microsoft.Billing ``invoices`` API. This module pulls the invoice for a billing
month and stores the normalized summary (Charges / Azure Credit / Tax / Total)
into the ``azure_invoices`` table.

Requires (config ``azure.billing_account_name`` or env ``AZURE_BILLING_ACCOUNT``):
  the Microsoft Customer Agreement billing account name, e.g.
  "abcd1234-...:efgh5678-..._2019-05-31".
Role: "Billing account reader" (or Invoice section reader) on the account.
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date

from ..azure_catalog import upsert_invoice

log = logging.getLogger(__name__)

# Numeric fields of a normalized invoice summary (everything except invoice_number).
_NUM_FIELDS = ("charges", "other_credits", "azure_credit", "subtotal", "tax", "total")


def _billing_account(cfg: dict) -> str:
    return (
        os.environ.get("AZURE_BILLING_ACCOUNT")
        or (cfg.get("azure") or {}).get("billing_account_name")
        or ""
    ).strip()


def _subscription_id(cfg: dict) -> str:
    """A subscription id for the BillingManagementClient constructor.

    Invoice calls are billing-account-scoped, but the SDK client still requires
    a subscription id. Prefer the explicit env var, otherwise the first
    configured Azure subscription.
    """
    env = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if env:
        return env.strip()
    subs = (cfg.get("azure") or {}).get("subscriptions") or []
    for sub in subs:
        sub_id = (sub.get("id") or "").strip()
        if sub_id:
            return sub_id
    return ""


def _amount(value) -> float:
    """Azure SDK returns Amount objects with a `.value`; tolerate plain numbers."""
    if value is None:
        return 0.0
    inner = getattr(value, "value", value)
    try:
        return round(float(inner), 2)
    except (TypeError, ValueError):
        return 0.0


def _invoice_month(inv) -> str | None:
    """Billing month (YYYY-MM) an invoice covers, from its invoice period."""
    start = getattr(inv, "invoice_period_start_date", None) or getattr(
        inv, "billing_period_start_date", None
    )
    if start:
        return f"{start.year:04d}-{start.month:02d}"
    # Fall back to the invoice date minus one month (invoices bill the prior month).
    inv_date = getattr(inv, "invoice_date", None)
    if inv_date:
        y, m = inv_date.year, inv_date.month - 1
        if m < 1:
            m, y = 12, y - 1
        return f"{y:04d}-{m:02d}"
    return None


def _normalize(inv) -> dict:
    """Map an Azure SDK Invoice to our billing-summary schema."""
    azure_credit = _amount(getattr(inv, "azure_prepayment_applied", None)) + _amount(
        getattr(inv, "free_azure_credit_applied", None)
    )
    # Azure reports ``credit_amount`` as a negative number (a credit reducing the
    # bill). Store it as a positive credit magnitude so the gross identity holds:
    #   gross billed = sub_total + azure_credit + other_credits
    # and the credit shows up (and is subtracted) as a credit line downstream.
    raw_credit = _amount(getattr(inv, "credit_amount", None))
    other_credits = -raw_credit if raw_credit < 0 else 0.0
    subtotal = _amount(getattr(inv, "sub_total", None))
    tax = _amount(getattr(inv, "tax_amount", None))
    total = _amount(getattr(inv, "total_amount", None)) or _amount(
        getattr(inv, "amount_due", None)
    )
    charges = round(subtotal + azure_credit + other_credits, 2)
    return {
        "invoice_number": getattr(inv, "name", "") or "",
        "charges": charges,
        "other_credits": round(other_credits, 2),
        "azure_credit": round(azure_credit, 2),
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


def _aggregate(summaries: list[dict]) -> dict:
    """Sum several invoice summaries into one month-level summary.

    A Microsoft Customer Agreement billing account issues multiple invoices for a
    month (the full-month invoice plus separate marketplace / one-off invoices),
    so the month's true charges/credit/tax are the sum across all of them. The
    invoice_number reported is the largest invoice (the monthly one)."""
    agg = {k: 0.0 for k in _NUM_FIELDS}
    for s in summaries:
        for k in _NUM_FIELDS:
            agg[k] = round(agg[k] + float(s.get(k, 0.0) or 0.0), 2)
    primary = max(summaries, key=lambda s: s.get("total", 0.0), default={})
    agg["invoice_number"] = primary.get("invoice_number", "")
    return agg


def fetch_invoice(cfg: dict, month_key: str) -> dict | None:
    """Return the normalized invoice summary for *month_key*, or None.

    Aggregates every invoice whose billing period falls in the month (a billing
    account can have several per month). Best-effort: returns None (and logs) if
    the billing account is unset, the SDK is missing, the caller lacks permission,
    or no matching invoice exists.
    """
    account = _billing_account(cfg)
    if not account:
        log.info("Azure billing: no billing_account_name configured; skipping API fetch")
        return None
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.billing import BillingManagementClient
    except ImportError:
        log.warning("Azure billing: azure-mgmt-billing not installed; skipping API fetch")
        return None

    year, month = (int(x) for x in month_key.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    # Invoices are issued the following month; widen the invoice-date window.
    win_start = date(year, month, 1)
    end_month, end_year = (month + 2, year)
    if end_month > 12:
        end_month -= 12
        end_year += 1
    win_end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])

    try:
        client = BillingManagementClient(DefaultAzureCredential(), _subscription_id(cfg))
        invoices = client.invoices.list_by_billing_account(
            billing_account_name=account,
            period_start_date=win_start.isoformat(),
            period_end_date=win_end.isoformat(),
        )
        matches = [inv for inv in invoices if _invoice_month(inv) == month_key]
        if not matches:
            log.info("Azure billing: no invoice covering %s found", month_key)
            return None
        summary = _aggregate([_normalize(inv) for inv in matches])
        log.info(
            "Azure billing: %s invoices=%d primary=%s charges=%.2f credit=%.2f tax=%.2f total=%.2f",
            month_key, len(matches), summary["invoice_number"], summary["charges"],
            summary["azure_credit"], summary["tax"], summary["total"],
        )
        return summary
    except Exception:  # noqa: BLE001 - never break report generation
        log.exception("Azure billing: invoice fetch failed for %s", month_key)
        return None


def fetch_and_store_invoice(cfg: dict, month_key: str) -> dict | None:
    """Fetch the invoice from the API and cache it in the DB. Returns it or None."""
    summary = fetch_invoice(cfg, month_key)
    if summary is None:
        return None
    try:
        upsert_invoice(cfg, month_key, summary, source="api")
    except Exception:  # noqa: BLE001
        log.exception("Azure billing: failed to store invoice %s", month_key)
    return summary
