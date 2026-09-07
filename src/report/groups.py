"""Shared report grouping helpers for PDF and web views."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..billing import (
    is_credit_line,
    is_tax_line,
    line_subtotal,
    line_total,
    reconcile_breakdown,
)
from ..models import CostRecord, ReportData
from .azure_rg import ProductRgGroup, group_rgs_by_product

_CLOUD_ORDER = {"AWS": 0, "Azure": 1, "GCP": 2, "Cursor": 3, "Claude": 4}

# Dedicated LLM / AI-provider "clouds" grouped into the report's AI section.
_AI_CLOUDS = ("Cursor", "Claude")

# Google has no dedicated AI cloud — Gemini / Vertex AI is billed through GCP, so
# we surface those GCP service lines in the AI section under a "Google" provider
# (a cross-cutting view; the spend is still counted once under GCP for the grand
# total). Match is a case-insensitive substring on the GCP service name.
_GCP_AI_DEFAULT_PATTERNS = ("vertex ai", "generative language", "gemini")


@dataclass
class ServiceRow:
    service: str
    production: float = 0.0
    non_production: float = 0.0
    shared: float = 0.0

    @property
    def total(self) -> float:
        return round(self.production + self.non_production + self.shared, 2)


@dataclass
class SubscriptionServiceRow:
    service: str
    by_subscription: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(self.by_subscription.values()), 2)


@dataclass
class ProjectServiceRow:
    service: str
    by_project: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return round(sum(self.by_project.values()), 2)


@dataclass
class AzureBreakdownRow:
    service: str
    production: float
    non_production: float
    total: float
    production_rgs: list[tuple[str, float]]
    non_production_rgs: list[tuple[str, float]]
    production_groups: list  # list[ProductRgGroup] — filled by azure_cost_breakdown
    non_production_groups: list


def _sub_name(r: CostRecord, sub_map: dict[str, str]) -> str:
    return r.subscription or sub_map.get(r.account, "Unknown")


def subscription_names(cfg: dict) -> list[str]:
    return [s["name"] for s in cfg.get("azure", {}).get("subscriptions", [])]


def group_by_cloud(data: ReportData) -> dict[str, dict]:
    """Per-cloud service tables with prod / non-prod / shared columns."""
    clouds: dict[str, dict[str, ServiceRow]] = defaultdict(dict)
    for r in data.records:
        if is_tax_line(r) or is_credit_line(r):
            continue
        row = clouds[r.cloud].setdefault(r.service, ServiceRow(service=r.service))
        amount = line_total(r)
        if r.environment == "production":
            row.production += amount
        elif r.environment == "shared":
            row.shared += amount
        else:
            row.non_production += amount

    out: dict[str, dict] = {}
    for cloud in sorted(clouds, key=lambda c: _CLOUD_ORDER.get(c, 99)):
        rows = sorted(clouds[cloud].values(), key=lambda x: x.total, reverse=True)
        breakdown_total = round(sum(x.total for x in rows), 2)
        summary = next((s for s in data.summaries if s.cloud == cloud), None)
        if summary and abs(round(summary.subtotal, 2) - breakdown_total) > 0.02:
            reconcile_breakdown(data.records, cloud, breakdown_total, label="by-cloud")
        out[cloud] = {
            "rows": rows,
            "production": round(sum(x.production for x in rows), 2),
            "non_production": round(sum(x.non_production for x in rows), 2),
            "shared": round(sum(x.shared for x in rows), 2),
            "total": breakdown_total,
            "tax": round(summary.tax, 2) if summary else 0.0,
            "credits": round(summary.credits, 2) if summary else 0.0,
            "grand_total": round(summary.total, 2) if summary else breakdown_total,
        }
    return out


def group_azure_by_subscription(
    records: list[CostRecord], sub_map: dict[str, str], names: list[str]
) -> dict:
    """Azure services with one column per subscription (replaces Shared lump)."""
    rows_map: dict[str, SubscriptionServiceRow] = {}
    for r in records:
        if r.cloud != "Azure" or is_tax_line(r) or is_credit_line(r):
            continue
        sub = _sub_name(r, sub_map)
        row = rows_map.setdefault(r.service, SubscriptionServiceRow(service=r.service))
        row.by_subscription[sub] = row.by_subscription.get(sub, 0.0) + line_total(r)

    rows = sorted(rows_map.values(), key=lambda x: x.total, reverse=True)
    totals = {n: 0.0 for n in names}
    for row in rows:
        for n in names:
            totals[n] += row.by_subscription.get(n, 0.0)
    totals = {k: round(v, 2) for k, v in totals.items()}
    total = round(sum(totals.values()), 2)
    return {
        "subscription_names": names,
        "rows": rows,
        "totals": totals,
        "total": total,
    }


def azure_cost_breakdown(
    records: list[CostRecord], sub_map: dict[str, str]
) -> list[AzureBreakdownRow]:
    """Prod / non-prod split with per-RG cost breakdown (matches manual PDF)."""
    by_service: dict[str, dict] = defaultdict(
        lambda: {
            "production": 0.0,
            "non_production": 0.0,
            "prod_rgs": defaultdict(float),
            "nonprod_rgs": defaultdict(float),
        }
    )
    for r in records:
        if r.cloud != "Azure" or is_tax_line(r) or is_credit_line(r):
            continue
        bucket = by_service[r.service]
        rg = r.scope or "Unassigned"
        amount = line_total(r)
        if r.environment == "production":
            bucket["production"] += amount
            bucket["prod_rgs"][rg] += amount
        elif r.environment == "shared":
            bucket["production"] += amount
            bucket["prod_rgs"][rg] += amount
        else:
            bucket["non_production"] += amount
            bucket["nonprod_rgs"][rg] += amount

    out: list[AzureBreakdownRow] = []
    for service, b in sorted(by_service.items(), key=lambda x: -(x[1]["production"] + x[1]["non_production"])):
        prod_rgs = sorted(b["prod_rgs"].items(), key=lambda x: x[1], reverse=True)
        nonprod_rgs = sorted(b["nonprod_rgs"].items(), key=lambda x: x[1], reverse=True)
        total = round(b["production"] + b["non_production"], 2)
        out.append(AzureBreakdownRow(
            service=service,
            production=round(b["production"], 2),
            non_production=round(b["non_production"], 2),
            total=total,
            production_rgs=[(k, round(v, 2)) for k, v in prod_rgs],
            non_production_rgs=[(k, round(v, 2)) for k, v in nonprod_rgs],
            production_groups=group_rgs_by_product(prod_rgs, is_production=True),
            non_production_groups=group_rgs_by_product(nonprod_rgs, is_production=False),
        ))
    return out


def azure_rg_by_service(records: list[CostRecord]) -> dict[str, list[tuple[str, float]]]:
    by_service: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        if r.cloud != "Azure" or is_tax_line(r) or is_credit_line(r):
            continue
        rg = r.scope or "Unassigned"
        by_service[r.service][rg] += r.cost
    return {
        svc: sorted(rgs.items(), key=lambda x: x[1], reverse=True)
        for svc, rgs in by_service.items()
    }


def aws_breakdown(records: list[CostRecord]) -> dict:
    services: list[dict] = []
    credits_by_service: list[dict] = []
    tax = 0.0
    credits = 0.0
    for r in records:
        if r.cloud != "AWS":
            continue
        if is_tax_line(r):
            tax += r.tax
        elif is_credit_line(r):
            credits += r.credits
            credits_by_service.append({"service": r.service, "credits": r.credits})
        else:
            services.append({"service": r.service, "cost": line_subtotal(r)})
    services.sort(key=lambda x: x["cost"], reverse=True)
    credits_by_service.sort(key=lambda x: x["credits"], reverse=True)
    subtotal = round(sum(s["cost"] for s in services), 2)
    tax = round(tax, 2)
    credits = round(credits, 2)
    total = round(subtotal - credits + tax, 2)
    reconcile_breakdown(records, "AWS", total, label="service-table")
    return {
        "services": services,
        "credits_by_service": credits_by_service,
        "subtotal": subtotal,
        "credits": credits,
        "tax": tax,
        "total": total,
    }


def gcp_project_names(records: list[CostRecord]) -> list[str]:
    names: set[str] = set()
    for r in records:
        if r.cloud != "GCP" or is_tax_line(r) or is_credit_line(r):
            continue
        names.add(r.scope or "Unassigned")
    return sorted(names)


def gcp_by_project(records: list[CostRecord]) -> dict:
    """GCP services with one column per project (net cost after credits)."""
    names = gcp_project_names(records)
    rows_map: dict[str, ProjectServiceRow] = {}
    for r in records:
        if r.cloud != "GCP" or is_tax_line(r) or is_credit_line(r):
            continue
        project = r.scope or "Unassigned"
        row = rows_map.setdefault(r.service, ProjectServiceRow(service=r.service))
        row.by_project[project] = row.by_project.get(project, 0.0) + line_total(r)

    rows = sorted(rows_map.values(), key=lambda x: x.total, reverse=True)
    totals = {n: 0.0 for n in names}
    for row in rows:
        for n in names:
            totals[n] += row.by_project.get(n, 0.0)
    totals = {k: round(v, 2) for k, v in totals.items()}
    total = round(sum(totals.values()), 2)
    return {
        "project_names": names,
        "rows": rows,
        "totals": totals,
        "total": total,
    }


def gcp_breakdown(records: list[CostRecord]) -> dict:
    """GCP services with a prod / non-prod / shared split and per-project detail.

    Charges come from positive cost lines; GCP credits (spending-based / CUD /
    free-tier discounts) are summed separately. GCP carries no tax line here.
    """
    by_service: dict[str, dict] = defaultdict(
        lambda: {
            "production": 0.0,
            "non_production": 0.0,
            "shared": 0.0,
            "projects": defaultdict(float),
        }
    )
    credits = 0.0
    credits_by_service: list[dict] = []
    for r in records:
        if r.cloud != "GCP":
            continue
        if is_credit_line(r):
            credits += r.credits
            credits_by_service.append({"service": r.service, "credits": r.credits})
            continue
        # A charge line may still carry an embedded credit amount.
        credits += r.credits
        bucket = by_service[r.service]
        amount = line_subtotal(r)
        project = r.scope or "Unassigned"
        bucket["projects"][project] += amount
        if r.environment == "production":
            bucket["production"] += amount
        elif r.environment == "shared":
            bucket["shared"] += amount
        else:
            bucket["non_production"] += amount

    rows: list[dict] = []
    for service, b in by_service.items():
        total = round(b["production"] + b["non_production"] + b["shared"], 2)
        if abs(total) < 0.005:
            continue
        rows.append({
            "service": service,
            "production": round(b["production"], 2),
            "non_production": round(b["non_production"], 2),
            "shared": round(b["shared"], 2),
            "total": total,
            "projects": sorted(
                [(p, round(v, 2)) for p, v in b["projects"].items()],
                key=lambda x: x[1], reverse=True,
            ),
        })
    rows.sort(key=lambda x: x["total"], reverse=True)
    credits_by_service.sort(key=lambda x: x["credits"], reverse=True)
    subtotal = round(sum(r["total"] for r in rows), 2)
    credits = round(credits, 2)
    total = round(subtotal - credits, 2)
    reconcile_breakdown(records, "GCP", total, label="service-table")
    return {
        "services": rows,
        "credits_by_service": credits_by_service,
        "subtotal": subtotal,
        "credits": credits,
        "tax": 0.0,
        "total": total,
    }


def _gcp_ai_patterns(cfg: dict | None) -> tuple[str, ...]:
    cfg = cfg or {}
    patterns = (cfg.get("ai", {}) or {}).get("gcp_service_patterns")
    if patterns:
        return tuple(str(p).lower() for p in patterns)
    return _GCP_AI_DEFAULT_PATTERNS


def ai_breakdown(records: list[CostRecord], cfg: dict | None = None) -> dict:
    """LLM / AI provider spend as a dedicated section (like AWS / GCP).

    Groups the AI-provider clouds (Cursor, Claude) into a per-provider summary
    plus a per-model table with a production / non-production / shared split.

    Google/Gemini has no dedicated AI cloud — its spend is billed through GCP —
    so matching GCP service lines (Vertex AI, Generative Language API, Gemini)
    are surfaced here under a "Google" provider. This is a cross-cutting view:
    the same spend is still counted once under GCP for the grand total, so the
    AI-section total may overlap with the GCP total by design.
    """
    def _model_bucket() -> dict:
        return {"production": 0.0, "non_production": 0.0, "shared": 0.0,
                "tokens": 0.0, "units": set()}

    # provider -> {credits, models: {(model, project): bucket}, users: {user: {cost, tokens}}}
    provider_agg: dict[str, dict] = defaultdict(
        lambda: {"credits": 0.0, "models": defaultdict(_model_bucket),
                 "users": defaultdict(lambda: {"cost": 0.0, "tokens": 0.0})}
    )

    def _add(provider: str, r: CostRecord, amount: float, project: str) -> None:
        model = (r.model or r.service or "Unknown").strip() or "Unknown"
        bucket = provider_agg[provider]["models"][(model, project)]
        if r.environment == "production":
            bucket["production"] += amount
        elif r.environment == "shared":
            bucket["shared"] += amount
        else:
            bucket["non_production"] += amount
        tokens = float(r.tokens or 0.0)
        bucket["tokens"] += tokens
        if tokens:
            bucket["units"].add(r.usage_unit or "")
        if r.user:
            u = provider_agg[provider]["users"][r.user]
            u["cost"] += amount
            u["tokens"] += tokens

    gcp_ai_patterns = _gcp_ai_patterns(cfg)

    for r in records:
        if r.cloud in _AI_CLOUDS:
            if is_tax_line(r):
                continue
            if is_credit_line(r):
                provider_agg[r.cloud]["credits"] += r.credits
                continue
            provider_agg[r.cloud]["credits"] += r.credits
            _add(r.cloud, r, line_subtotal(r), project="")
        elif r.cloud == "GCP" and not is_tax_line(r):
            svc = (r.service or "").lower()
            if not any(p in svc for p in gcp_ai_patterns):
                continue
            if is_credit_line(r):
                provider_agg["Google"]["credits"] += r.credits
                continue
            provider_agg["Google"]["credits"] += r.credits
            # For Google the "model" is the SKU; keep the project for attribution.
            _add("Google", r, line_subtotal(r), project=r.scope or "")

    providers: list[dict] = []
    rows: list[dict] = []
    top_users: dict[str, list[dict]] = {}
    subtotal = 0.0
    credits = 0.0
    def _unit_label(units: set) -> str:
        clean = {u for u in units if u}
        if len(clean) == 1:
            return next(iter(clean))
        if len(clean) > 1:
            return "mixed"
        return ""

    for provider, agg in provider_agg.items():
        models: list[dict] = []
        p_subtotal = 0.0
        p_tokens = 0.0
        p_units: set = set()
        for (model, project), env in agg["models"].items():
            total = round(env["production"] + env["non_production"] + env["shared"], 2)
            tokens = round(env["tokens"], 0)
            if abs(total) < 0.005 and tokens < 1:
                continue
            row = {
                "provider": provider,
                "service": model,
                "model": model,
                "project": project,
                "production": round(env["production"], 2),
                "non_production": round(env["non_production"], 2),
                "shared": round(env["shared"], 2),
                "total": total,
                "tokens": tokens,
                "unit": _unit_label(env["units"]),
            }
            models.append(row)
            rows.append(row)
            p_subtotal += total
            p_tokens += tokens
            p_units |= env["units"]
        if not models:
            continue
        models.sort(key=lambda x: (x["total"], x["tokens"]), reverse=True)
        p_subtotal = round(p_subtotal, 2)
        p_credits = round(agg["credits"], 2)
        subtotal += p_subtotal
        credits += p_credits

        # Top-10 users: rank by cost when available, else by tokens (Anthropic
        # cannot attribute cost per API key, so those rank by token volume).
        users = [
            {"user": u, "cost": round(v["cost"], 2), "tokens": round(v["tokens"], 0)}
            for u, v in agg["users"].items()
        ]
        rank_by_cost = any(u["cost"] > 0.005 for u in users)
        users.sort(key=lambda u: u["cost"] if rank_by_cost else u["tokens"], reverse=True)
        if users:
            top_users[provider] = {"rows": users[:10], "by": "cost" if rank_by_cost else "tokens"}

        # A provider-level token total only makes sense when every SKU shares one
        # unit (e.g. Cursor/Claude = tokens). Google mixes token-hours + counts.
        p_unit = _unit_label(p_units)
        providers.append({
            "provider": provider,
            "models": models,
            "subtotal": p_subtotal,
            "credits": p_credits,
            "total": round(p_subtotal - p_credits, 2),
            "tokens": round(p_tokens, 0) if p_unit and p_unit != "mixed" else 0.0,
            "unit": p_unit,
        })

    providers.sort(key=lambda p: p["total"], reverse=True)
    rows.sort(key=lambda r: (r["total"], r["tokens"]), reverse=True)

    # By Project: aggregate the model rows by project. AI providers with no
    # project concept (Cursor / Claude) fall back to the provider name so every
    # dollar is attributable.
    proj_agg: dict[str, dict] = {}
    for r in rows:
        key = r["project"] or r["provider"]
        p = proj_agg.setdefault(key, {
            "project": key, "providers": set(),
            "production": 0.0, "non_production": 0.0, "shared": 0.0,
            "total": 0.0, "tokens": 0.0, "units": set(),
        })
        p["providers"].add(r["provider"])
        p["production"] += r["production"]
        p["non_production"] += r["non_production"]
        p["shared"] += r["shared"]
        p["total"] += r["total"]
        p["tokens"] += r["tokens"]
        if r["unit"]:
            p["units"].add(r["unit"])
    project_rows: list[dict] = []
    for p in proj_agg.values():
        unit = _unit_label(p["units"])
        project_rows.append({
            "project": p["project"],
            "providers": sorted(p["providers"]),
            "production": round(p["production"], 2),
            "non_production": round(p["non_production"], 2),
            "shared": round(p["shared"], 2),
            "total": round(p["total"], 2),
            "tokens": round(p["tokens"], 0) if unit and unit != "mixed" else 0.0,
            "unit": unit,
        })
    project_rows.sort(key=lambda r: r["total"], reverse=True)

    subtotal = round(subtotal, 2)
    credits = round(credits, 2)
    return {
        "providers": providers,
        "provider_names": [p["provider"] for p in providers],
        "rows": rows,
        "projects": project_rows,
        "top_users": top_users,
        "subtotal": subtotal,
        "credits": credits,
        "total": round(subtotal - credits, 2),
        "has_env_split": any(r["non_production"] or r["shared"] for r in rows),
        "has_tokens": any(r["tokens"] for r in rows),
        "has_projects": any(r["project"] for r in rows),
    }


def azure_gross(report: ReportData) -> float:
    s = next((x for x in report.summaries if x.cloud == "Azure"), None)
    return round(s.subtotal, 2) if s else 0.0
