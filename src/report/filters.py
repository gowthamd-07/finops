"""Filter records and build chart/trend datasets for the web UI."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterator

from ..billing import is_credit_line, is_tax_line, line_subtotal, line_total
from ..models import CostRecord
from .azure_rg import (
    FILTERABLE_ENV_TIERS,
    ENV_TIER_LABELS,
    list_products,
    load_rg_catalog,
    rg_matches_env_tier,
    rg_matches_product,
    rg_matches_subscription_tier,
)


def subscription_map(cfg: dict) -> dict[str, str]:
    return {s["id"]: s["name"] for s in cfg.get("azure", {}).get("subscriptions", [])}


def record_subscription(r: CostRecord, sub_map: dict[str, str]) -> str:
    if r.subscription:
        return r.subscription
    if r.cloud == "Azure":
        return sub_map.get(r.account, r.account or "Unknown")
    return r.cloud or "Unknown"


def record_matches_env_tier(r: CostRecord, env_filter: str | None) -> bool:
    """Match records by Azure RG tier code; AWS production maps to pd only."""
    if not env_filter:
        return True
    tier = env_filter.lower().strip()
    if tier not in FILTERABLE_ENV_TIERS:
        return True
    if r.cloud == "Azure":
        return rg_matches_env_tier(r.scope, tier)
    if r.cloud == "AWS":
        return r.environment == "production" if tier == "pd" else False
    return False


def filter_records(
    records: list[CostRecord],
    *,
    cloud: str | None = None,
    subscription: str | None = None,
    resource_group: str | None = None,
    product_group: str | None = None,
    env_tier: str | None = None,
    service: str | None = None,
    sub_map: dict[str, str] | None = None,
) -> list[CostRecord]:
    sub_map = sub_map or {}
    out: list[CostRecord] = []
    for r in records:
        if cloud and r.cloud.lower() != cloud.lower():
            continue
        if service and r.service.lower() != service.lower():
            continue
        rg = r.scope or ""
        if resource_group and rg.lower() != resource_group.lower():
            continue
        if env_tier and not record_matches_env_tier(r, env_tier):
            continue
        if product_group:
            if not rg_matches_product(rg, product_group):
                continue
            if not env_tier and not rg_matches_subscription_tier(rg, subscription):
                continue
        if subscription:
            sub = record_subscription(r, sub_map)
            if r.cloud == "AWS" and subscription.lower() != "aws":
                continue
            if r.cloud != "AWS" and sub.lower() != subscription.lower():
                continue
        out.append(r)
    return out


def dimension_key(r: CostRecord, dimension: str, sub_map: dict[str, str]) -> str:
    if dimension == "subscription":
        return record_subscription(r, sub_map)
    if dimension == "resource_group":
        return r.scope or "Unassigned"
    return r.service


def chart_by_dimension(
    records: list[CostRecord],
    dimension: str,
    sub_map: dict[str, str] | None = None,
    limit: int = 20,
) -> list[dict]:
    sub_map = sub_map or {}
    totals: dict[str, float] = defaultdict(float)
    for r in records:
        key = dimension_key(r, dimension, sub_map)
        totals[key] += line_total(r)
    rows = sorted(
        [{"label": k, "cost": round(v, 2)} for k, v in totals.items()],
        key=lambda x: x["cost"],
        reverse=True,
    )
    return rows[:limit]


def filter_options(
    records: list[CostRecord],
    sub_map: dict[str, str],
    cfg: dict | None = None,
) -> dict[str, list[str]]:
    clouds, subs, rgs, services = set(), set(), set(), set()
    service_clouds: dict[str, set[str]] = defaultdict(set)
    for r in records:
        clouds.add(r.cloud)
        services.add(r.service)
        service_clouds[r.service].add(r.cloud)
        if r.cloud == "Azure":
            subs.add(record_subscription(r, sub_map))
            if r.scope:
                rgs.add(r.scope)
        else:
            subs.add(record_subscription(r, sub_map))
    products = list_products(sorted(rgs), load_rg_catalog(cfg or {}))
    env_tiers = [{"code": code, "label": ENV_TIER_LABELS[code]} for code in FILTERABLE_ENV_TIERS]
    return {
        "clouds": sorted(clouds),
        "subscriptions": sorted(subs),
        "resource_groups": sorted(rgs),
        "product_groups": products,
        "env_tiers": env_tiers,
        "services": sorted(services),
        "service_clouds": {s: sorted(c) for s, c in service_clouds.items()},
    }


def monthly_trend(
    month_records: list[tuple[str, list[CostRecord]]],
    filters: dict,
    sub_map: dict[str, str],
) -> list[dict]:
    points = []
    for month_key, records in month_records:
        filtered = filter_records(
            records,
            cloud=filters.get("cloud"),
            subscription=filters.get("subscription"),
            resource_group=filters.get("resource_group"),
            product_group=filters.get("product_group"),
            env_tier=filters.get("env_tier"),
            service=filters.get("service"),
            sub_map=sub_map,
        )
        points.append({
            "month": month_key,
            "cost": round(sum(line_total(r) for r in filtered), 2),
        })
    return points
