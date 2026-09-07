"""Month-over-month cost change helpers for executive summary tables."""
from __future__ import annotations

from collections import defaultdict

from ..billing import is_credit_line, is_tax_line, line_subtotal
from ..models import CostRecord
from .azure_rg import ENV_TIER_LABELS, env_tier

_TOP_N = 8

_ENV_LABELS = {
    "production": "Production",
    "non-production": "Non-Production",
    "shared": "Shared",
}


def _resource_label(r: CostRecord) -> str:
    if r.cloud == "Azure":
        return r.scope or "Unassigned"
    return r.account or "AWS account"


def _environment_label(r: CostRecord) -> str:
    """Human-readable environment for a record (Azure RG tier or AWS env)."""
    if r.cloud == "Azure":
        tier = env_tier(r.scope)
        if tier:
            return ENV_TIER_LABELS.get(tier, tier)
    env = (r.environment or "").strip().lower()
    return _ENV_LABELS.get(env, env.title() or "Unknown")


def _aggregate(
    records: list[CostRecord],
) -> tuple[dict[tuple[str, str, str], float], dict[tuple[str, str, str], str]]:
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    env_totals: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for r in records:
        if is_tax_line(r) or is_credit_line(r):
            continue
        key = (r.cloud, _resource_label(r), r.service)
        sub = line_subtotal(r)
        totals[key] += sub
        env_totals[key][_environment_label(r)] += sub
    rounded = {k: round(v, 2) for k, v in totals.items()}
    # Pick the environment that accounts for the most spend on each key.
    env_by_key = {
        k: max(envs.items(), key=lambda kv: kv[1])[0] for k, envs in env_totals.items()
    }
    return rounded, env_by_key


def resource_cost_changes(
    prev_records: list[CostRecord],
    curr_records: list[CostRecord],
    *,
    top_n: int = _TOP_N,
) -> tuple[list[dict], list[dict]]:
    """Return top increases and decreases by cloud + resource + service."""
    prev, prev_env = _aggregate(prev_records)
    curr, curr_env = _aggregate(curr_records)
    increases: list[dict] = []
    decreases: list[dict] = []

    for key in set(prev) | set(curr):
        cloud, resource, service = key
        delta = round(curr.get(key, 0.0) - prev.get(key, 0.0), 2)
        row = {
            "cloud": cloud,
            "resource": resource,
            "service": service,
            "environment": curr_env.get(key) or prev_env.get(key) or "Unknown",
            "amount": abs(delta),
        }
        if delta > 0:
            increases.append(row)
        elif delta < 0:
            decreases.append(row)

    increases.sort(key=lambda x: x["amount"], reverse=True)
    decreases.sort(key=lambda x: x["amount"], reverse=True)
    return increases[:top_n], decreases[:top_n]


def enrich_mom_changes(report, prev_records: list[CostRecord] | None) -> None:
    if prev_records:
        report.key_increases, report.savings = resource_cost_changes(
            prev_records, report.records
        )


def _coerce_amount(val) -> float:
    try:
        return round(abs(float(val or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


def normalize_change_rows(rows: list) -> list[dict]:
    """Normalize MoM change rows for templates (legacy tuple/list/dict formats)."""
    out: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append({
                "cloud": str(row.get("cloud") or ""),
                "resource": str(row.get("resource") or ""),
                "service": str(row.get("service") or row.get("name") or ""),
                "environment": str(row.get("environment") or ""),
                "amount": _coerce_amount(
                    row.get("amount", row.get("delta", row.get("value")))
                ),
            })
            continue
        if isinstance(row, (list, tuple)):
            if len(row) >= 4:
                out.append({
                    "cloud": str(row[0]),
                    "resource": str(row[1]),
                    "service": str(row[2]),
                    "environment": "",
                    "amount": _coerce_amount(row[3]),
                })
            elif len(row) == 3:
                out.append({
                    "cloud": str(row[0]),
                    "resource": str(row[1]),
                    "service": str(row[2]),
                    "environment": "",
                    "amount": 0.0,
                })
            elif len(row) == 2:
                out.append({
                    "cloud": "",
                    "resource": "",
                    "service": str(row[0]),
                    "environment": "",
                    "amount": _coerce_amount(row[1]),
                })
            elif len(row) == 1:
                out.append({
                    "cloud": "",
                    "resource": "",
                    "service": str(row[0]),
                    "environment": "",
                    "amount": 0.0,
                })
            continue
        if isinstance(row, str):
            out.append({
                "cloud": "",
                "resource": "",
                "service": row,
                "environment": "",
                "amount": 0.0,
            })
    return out
