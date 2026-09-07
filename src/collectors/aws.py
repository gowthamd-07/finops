"""AWS collector using the Cost Explorer API (read-only).

Required IAM permissions (read-only):
    ce:GetCostAndUsage
    ce:GetDimensionValues

Auth: standard boto3 credential chain. Locally this resolves the named profile
    from `aws.profile` (default "", override via AWS_PROFILE). In-cluster,
    leave the profile empty to use the IRSA/instance role, or set static keys via
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (+ optional AWS_SESSION_TOKEN).
"""
from __future__ import annotations

import logging
import os
from datetime import date

import boto3
from botocore.exceptions import ProfileNotFound

from ..models import CostRecord

log = logging.getLogger(__name__)

# Route 53 Registrar, support fees, RI charges, etc. use non-Usage record types.
# Match the AWS billing console: charges and credits are separate from tax.
_SERVICE_RECORD_TYPES_EXCLUDE = ("Tax", "Credit")


def _aws_session(cfg: dict) -> boto3.Session:
    """Build a boto3 session for Cost Explorer.

  Precedence:
    1. Static keys in env (AWS_ACCESS_KEY_ID) — no named profile.
    2. AWS_PROFILE env, then aws.profile from config — only if the profile exists.
    3. Default credential chain (instance role, etc.).
    """
    aws_cfg = cfg.get("aws", {})
    region = aws_cfg.get("region", "us-east-1")

    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return boto3.Session(region_name=region)

    profile = (os.environ.get("AWS_PROFILE") or aws_cfg.get("profile") or "").strip() or None
    if profile:
        try:
            return boto3.Session(profile_name=profile, region_name=region)
        except ProfileNotFound:
            log.warning(
                "AWS profile %r not found (no ~/.aws in this environment); "
                "falling back to env/instance credentials",
                profile,
            )

    # Avoid inheriting a broken AWS_PROFILE when no credentials file is present.
    saved = os.environ.pop("AWS_PROFILE", None)
    try:
        return boto3.Session(region_name=region)
    finally:
        if saved is not None:
            os.environ["AWS_PROFILE"] = saved


def collect_aws(cfg: dict, start: date, end: date) -> list[CostRecord]:
    """Return per-service AWS cost for [start, end) (end exclusive).

    Service lines include Usage, Fee (e.g. Route 53 Registrar), RI fees, refunds,
    etc. Tax is retrieved separately so the summary table can show a tax line.
    """
    aws_cfg = cfg.get("aws", {})
    region = aws_cfg.get("region", "us-east-1")
    session = _aws_session(cfg)
    client = session.client("ce", region_name=region)
    period = {"Start": start.isoformat(), "End": end.isoformat()}

    records: list[CostRecord] = []

    # Per-service unblended cost (Usage + Fee + …; excludes Tax).
    service_resp = _paginated_cost(
        client,
        period,
        group_key="SERVICE",
        exclude_record_types=list(_SERVICE_RECORD_TYPES_EXCLUDE),
    )
    for name, amount in service_resp.items():
        if abs(amount) < 0.005:
            continue
        records.append(
            CostRecord(
                cloud="AWS",
                service=name,
                cost=round(amount, 2),
                environment="production",
                period_start=start,
                period_end=end,
            )
        )

    # Credits by service (Cost Explorer returns negative amounts).
    credit_resp = _paginated_cost(
        client, period, group_key="SERVICE", include_record_types=["Credit"]
    )
    for name, amount in credit_resp.items():
        credit = round(abs(amount), 2)
        if credit < 0.005:
            continue
        records.append(
            CostRecord(
                cloud="AWS",
                service=name,
                cost=0.0,
                credits=credit,
                environment="production",
                period_start=start,
                period_end=end,
            )
        )

    # Tax as a single line for the AWS summary.
    tax_resp = _paginated_cost(
        client, period, group_key="SERVICE", include_record_types=["Tax"]
    )
    tax_total = round(sum(tax_resp.values()), 2)
    if tax_total:
        records.append(
            CostRecord(
                cloud="AWS",
                service="Tax",
                cost=0.0,
                tax=tax_total,
                environment="production",
                period_start=start,
                period_end=end,
            )
        )
    credit_total = round(sum(abs(v) for v in credit_resp.values()), 2)
    charge_total = round(sum(service_resp.values()), 2)
    if tax_total or credit_total:
        log.info(
            "AWS: %d charge lines, credits=%.2f, tax=%.2f, charges=%.2f",
            len([r for r in records if r.cost and not r.credits]),
            credit_total,
            tax_total,
            charge_total,
        )
    log.info("AWS: collected %d records", len(records))
    return records


def _record_type_filter(
    *,
    include_record_types: list[str] | None = None,
    exclude_record_types: list[str] | None = None,
) -> dict | None:
    if include_record_types:
        return {"Dimensions": {"Key": "RECORD_TYPE", "Values": include_record_types}}
    if exclude_record_types:
        return {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": exclude_record_types}}}
    return None


def _paginated_cost(
    client,
    period: dict,
    group_key: str,
    *,
    include_record_types: list[str] | None = None,
    exclude_record_types: list[str] | None = None,
) -> dict[str, float]:
    """Aggregate UnblendedCost grouped by `group_key`, filtered by RECORD_TYPE."""
    totals: dict[str, float] = {}
    next_token: str | None = None
    cost_filter = _record_type_filter(
        include_record_types=include_record_types,
        exclude_record_types=exclude_record_types,
    )
    while True:
        kwargs = dict(
            TimePeriod=period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": group_key}],
        )
        if cost_filter:
            kwargs["Filter"] = cost_filter
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = client.get_cost_and_usage(**kwargs)
        for result in resp.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                name = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                totals[name] = totals.get(name, 0.0) + amount
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return totals
