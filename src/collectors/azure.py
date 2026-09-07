"""Azure collector using the Cost Management Query API (read-only).

Required role: "Cost Management Reader" at each subscription scope.

Auth: Workload Identity in AKS (DefaultAzureCredential picks up the federated
token automatically). No secret needed in-cluster.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time

from azure.identity import DefaultAzureCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryAggregation,
    QueryDataset,
    QueryDefinition,
    QueryGrouping,
    QueryTimePeriod,
    TimeframeType,
)
from azure.core.pipeline import PipelineResponse
from azure.core.rest import HttpRequest

from ..billing import aggregate_charge_credit_rows
from ..models import CostRecord

log = logging.getLogger(__name__)


def collect_azure(cfg: dict, start: date, end: date) -> list[CostRecord]:
    azure_cfg = cfg.get("azure", {})
    credential = DefaultAzureCredential()
    client = CostManagementClient(credential)

    rg_overrides = {
        k.lower(): v for k, v in azure_cfg.get("resource_group_overrides", {}).items()
    }
    # Optional regex refinements, checked (in order) before the subscription
    # default. Each item: {"pattern": "<regex>", "environment": "<env>"}.
    rg_patterns = [
        (re.compile(p["pattern"], re.IGNORECASE), p["environment"])
        for p in azure_cfg.get("resource_group_patterns", [])
    ]
    default_env = azure_cfg.get("default_environment", "non-production")

    def classify(resource_group: str, sub_env: str) -> str:
        name = (resource_group or "").lower()
        if name in rg_overrides:           # 1) exact-name override
            return rg_overrides[name]
        for rx, env in rg_patterns:        # 2) regex refinement
            if rx.search(name):
                return env
        return sub_env                     # 3) subscription default (authoritative)

    time_period = QueryTimePeriod(
        from_property=datetime.combine(start, time.min),
        to=datetime.combine(end, time.min),
    )

    records: list[CostRecord] = []
    for sub in azure_cfg.get("subscriptions", []):
        scope = f"/subscriptions/{sub['id']}"
        sub_env = sub.get("environment", default_env)
        try:
            rows = _query_subscription(client, scope, time_period)
        except Exception as exc:  # noqa: BLE001 - keep other subs flowing
            log.error("Azure: query failed for %s (%s): %s", sub["name"], sub["id"], exc)
            continue

        for service, resource_group, charge, credit in rows:
            if charge == 0 and credit == 0:
                continue
            env = classify(resource_group, sub_env)
            records.append(
                CostRecord(
                    cloud="Azure",
                    service=service or "Unassigned",
                    cost=charge,
                    credits=credit,
                    publisher="Microsoft",
                    environment=env,
                    subscription=sub["name"],
                    scope=resource_group or "",
                    account=sub["id"],
                    period_start=start,
                    period_end=end,
                )
            )
        log.info("Azure: %s -> %d rows", sub["name"], len(rows))

    return records


def _query_subscription(client, scope, time_period):
    """Return list of (serviceName, resourceGroup, cost), with pagination."""
    definition = QueryDefinition(
        type="ActualCost",
        timeframe=TimeframeType.CUSTOM,
        time_period=time_period,
        dataset=QueryDataset(
            granularity=None,
            aggregation={
                "totalCost": QueryAggregation(name="Cost", function="Sum")
            },
            grouping=[
                QueryGrouping(type="Dimension", name="ServiceName"),
                QueryGrouping(type="Dimension", name="ResourceGroupName"),
            ],
        ),
    )
    raw_rows: list[tuple[str, str, float]] = []
    result = client.query.usage(scope=scope, parameters=definition)
    while result:
        cols = [c.name for c in result.columns]
        idx_cost = cols.index("Cost")
        idx_service = cols.index("ServiceName")
        idx_rg = cols.index("ResourceGroupName") if "ResourceGroupName" in cols else None
        for row in result.rows or []:
            service = row[idx_service]
            rg = row[idx_rg] if idx_rg is not None else ""
            cost = float(row[idx_cost])
            raw_rows.append((service, rg, cost))
        if not result.next_link:
            break
        result = _query_next_page(client, result.next_link)
    return aggregate_charge_credit_rows(raw_rows)


def _query_next_page(client: CostManagementClient, next_link: str):
    request = HttpRequest("GET", next_link)
    response = client._send_request(request)
    if response.status_code != 200:
        raise RuntimeError(f"Azure cost query pagination failed: HTTP {response.status_code}")
    pipeline_response = PipelineResponse(request, response)
    return client.query._deserialize("QueryResult", pipeline_response)
