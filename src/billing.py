"""Shared billing amount helpers so totals reconcile across views."""
from __future__ import annotations

import logging
from collections import defaultdict

from .models import CloudSummary, CostRecord

log = logging.getLogger(__name__)

_TAX_SERVICES = frozenset({"tax"})
_CREDIT_SERVICES = frozenset({"credits", "credit"})


def is_tax_line(r: CostRecord) -> bool:
    return r.service.lower() in _TAX_SERVICES and r.tax > 0


def is_credit_line(r: CostRecord) -> bool:
    return r.credits > 0 and r.cost == 0


def line_subtotal(r: CostRecord) -> float:
    """Pre-tax, pre-credit charges (summary subtotal column)."""
    if is_tax_line(r) or is_credit_line(r):
        return 0.0
    return round(r.cost, 2)


def line_credits(r: CostRecord) -> float:
    return round(r.credits, 2)


def line_tax(r: CostRecord) -> float:
    return round(r.tax, 2)


def line_total(r: CostRecord) -> float:
    """Net billed amount for a line (matches invoice total)."""
    return round(r.cost - r.credits + r.tax, 2)


def sum_subtotal(records: list[CostRecord]) -> float:
    return round(sum(line_subtotal(r) for r in records), 2)


def sum_credits(records: list[CostRecord]) -> float:
    return round(sum(line_credits(r) for r in records), 2)


def sum_tax(records: list[CostRecord]) -> float:
    return round(sum(line_tax(r) for r in records), 2)


def sum_total(records: list[CostRecord]) -> float:
    return round(sum(line_total(r) for r in records), 2)


def summarize_cloud(records: list[CostRecord], cloud: str) -> CloudSummary:
    rows = [r for r in records if r.cloud == cloud]
    return CloudSummary(
        cloud=cloud,
        subtotal=sum_subtotal(rows),
        credits=sum_credits(rows),
        tax=sum_tax(rows),
    )


def reconcile_cloud(records: list[CostRecord], cloud: str, *, context: str = "") -> None:
    """Log when breakdown totals do not match the cloud summary."""
    rows = [r for r in records if r.cloud == cloud]
    if not rows:
        return
    summary = summarize_cloud(rows, cloud)
    parts_total = sum_total(rows)
    if abs(summary.total - parts_total) > 0.02:
        log.warning(
            "%s %s total mismatch: summary=%.2f parts=%.2f (delta=%.2f)",
            cloud,
            context,
            summary.total,
            parts_total,
            summary.total - parts_total,
        )


def reconcile_breakdown(
    records: list[CostRecord],
    cloud: str,
    breakdown_total: float,
    *,
    label: str,
) -> None:
    expected = summarize_cloud(records, cloud).total
    if abs(expected - breakdown_total) > 0.02:
        log.warning(
            "%s breakdown %s mismatch: expected=%.2f got=%.2f (delta=%.2f)",
            cloud,
            label,
            expected,
            breakdown_total,
            expected - breakdown_total,
        )


def aggregate_rows(
    rows: list[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    """Merge duplicate (service, scope) rows from paginated API results."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for service, scope, cost in rows:
        key = (service or "Unassigned", scope or "")
        totals[key] += cost
    return [(svc, scope, round(cost, 2)) for (svc, scope), cost in totals.items()]


def aggregate_charge_credit_rows(
    rows: list[tuple[str, str, float]],
) -> list[tuple[str, str, float, float]]:
    """Split signed amounts into separate charge and credit buckets per service/scope."""
    charges: dict[tuple[str, str], float] = defaultdict(float)
    credits: dict[tuple[str, str], float] = defaultdict(float)
    for service, scope, amount in rows:
        key = (service or "Unassigned", scope or "")
        amount = float(amount)
        if amount < 0:
            credits[key] += abs(amount)
        elif amount > 0:
            charges[key] += amount
    keys = set(charges) | set(credits)
    return [
        (
            svc,
            scope,
            round(charges.get((svc, scope), 0.0), 2),
            round(credits.get((svc, scope), 0.0), 2),
        )
        for svc, scope in sorted(keys)
    ]


def split_signed_amount(amount: float) -> tuple[float, float]:
    """Return (charges, credits) from a signed billing amount."""
    amount = round(float(amount), 2)
    if amount < 0:
        return 0.0, abs(amount)
    return amount, 0.0
