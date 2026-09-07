"""Azure resource-group catalog and workload / environment parsing."""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_PROD_TIERS = frozenset({"pd"})
_NONPROD_TIERS = frozenset({"st", "dv"})
_OTHER_NONPROD_TIERS = frozenset({"ut", "sb", "np"})

# rg-app-pd-eu-01, MC_rg-app-dv-eu-01_..., ME_cae-ecommerce-st-eu-01_...
_BIZ_ENV_RE = re.compile(
    r"[-_](?:rg-)?([a-z0-9]+(?:-[a-z0-9]+)*?)-(pd|st|dv|ut|sb|np)-",
    re.IGNORECASE,
)

# Canonical workload buckets (order used in reports and filters).
WORKLOAD_CATEGORIES = (
    "Ecommerce",
    "Shared",
    "VRE",
    "AI",
    "Bioinfo",
    "Others",
)

_CATEGORY_ORDER = {name: index for index, name in enumerate(WORKLOAD_CATEGORIES)}

# Environment tiers parsed from RG names (rg-<workload>-<tier>-<region>-<nn>).
FILTERABLE_ENV_TIERS = ("pd", "dv", "st", "ut")
ENV_TIER_LABELS = {
    "pd": "Production (pd)",
    "dv": "Development (dv)",
    "st": "Staging (st)",
    "ut": "UAT (ut)",
}
_ENV_TIER_ORDER = {code: index for index, code in enumerate(FILTERABLE_ENV_TIERS)}


@dataclass
class RgCatalogEntry:
    name: str
    subscription: str
    product: str
    env_tier: str


@dataclass
class ProductRgGroup:
    product: str
    production: float = 0.0
    non_production: float = 0.0
    production_rgs: list[tuple[str, float]] = field(default_factory=list)
    non_production_rgs: list[tuple[str, float]] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(self.production + self.non_production, 2)


def catalog_path(cfg: dict) -> Path | None:
    raw = cfg.get("azure", {}).get("resource_groups_csv", "")
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path if path.is_file() else None
    base = Path(os.environ.get("CONFIG_PATH", "config/config.yaml")).parent
    resolved = base / raw
    return resolved if resolved.is_file() else None


def env_tier(resource_group: str) -> str:
    m = _BIZ_ENV_RE.search(resource_group or "")
    return m.group(2).lower() if m else ""


def env_tier_label(tier: str) -> str:
    return ENV_TIER_LABELS.get(tier.lower(), tier)


def rg_matches_env_tier(resource_group: str, env_filter: str | None) -> bool:
    """True when the RG name contains the requested tier code (pd/dv/st/ut)."""
    if not env_filter:
        return True
    return env_tier(resource_group) == env_filter.lower().strip()


def workload_category(resource_group: str) -> str:
    """Map an Azure resource group name to a workload category.

    Handles standard ``rg-<workload>-<tier>-<region>-<nn>`` names plus
    Azure-managed groups (``MC_``, ``ME_``, ``MA_``) for AKS, Container Apps,
    and Managed Prometheus.
    """
    name = (resource_group or "").strip()
    if not name:
        return "Others"
    lower = name.lower()

    if "ecommerce" in lower:
        return "Ecommerce"

    if lower.startswith("rg-shared") or "_rg-shared" in lower:
        return "Shared"

    if (
        lower.startswith("rg-vre")
        or "rg-vre" in lower
        or "cae-vre" in lower
        or re.search(r"(?:^|[-_/])vre(?:[-_]|$)", lower)
    ):
        return "VRE"

    if "bioinfo" in lower:
        return "Bioinfo"

    # AI workloads incl. rg-ai, AKS (MC_rg-ai), and Managed Prometheus (MA_mw-vse).
    if (
        lower.startswith("rg-ai")
        or "rg-ai" in lower
        or lower.startswith("mc_rg-ai")
        or "mw-vse" in lower
        or "aks-vse" in lower
    ):
        return "AI"

    return "Others"


def product_key(resource_group: str) -> str:
    """Alias for :func:`workload_category` (used by filters and grouping)."""
    return workload_category(resource_group)


def load_rg_catalog(cfg: dict) -> dict[str, RgCatalogEntry]:
    """Resource-group catalog, read from the database (CSV is a seed fallback)."""
    try:
        from ..azure_catalog import load_rg_catalog_db

        db_catalog = load_rg_catalog_db(cfg)
        if db_catalog:
            return db_catalog
    except Exception:  # noqa: BLE001 - fall back to CSV if DB layer unavailable
        pass
    return _load_rg_catalog_csv(cfg)


def _load_rg_catalog_csv(cfg: dict) -> dict[str, RgCatalogEntry]:
    path = catalog_path(cfg)
    if not path:
        return {}
    out: dict[str, RgCatalogEntry] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("NAME") or "").strip()
            if not name:
                continue
            out[name.lower()] = RgCatalogEntry(
                name=name,
                subscription=(row.get("SUBSCRIPTION") or "").strip(),
                product=workload_category(name),
                env_tier=env_tier(name),
            )
    return out


def list_products(records_scope: list[str], catalog: dict[str, RgCatalogEntry] | None = None) -> list[str]:
    catalog = catalog or {}
    products: set[str] = set()
    for rg in records_scope:
        if not rg:
            continue
        entry = catalog.get(rg.lower())
        products.add(entry.product if entry else workload_category(rg))
    return sorted(products, key=lambda c: (_CATEGORY_ORDER.get(c, len(WORKLOAD_CATEGORIES)), c))


def rg_matches_product(resource_group: str, product: str) -> bool:
    if not product:
        return True
    return workload_category(resource_group).lower() == product.strip().lower()


def rg_matches_subscription_tier(resource_group: str, subscription: str | None) -> bool:
    """When a subscription is selected, keep only the env tiers that belong there."""
    if not subscription:
        return True
    tier = env_tier(resource_group)
    if not tier:
        return True
    sub = subscription.lower().replace(" ", "").replace("-", "")
    if sub == "production":
        return tier in _PROD_TIERS
    if sub in {"nonproduction", "nonprod"}:
        return tier in _NONPROD_TIERS
    if sub in {"connectivity", "management", "shared"}:
        return tier in _OTHER_NONPROD_TIERS or tier in _PROD_TIERS
    return True


def group_rgs_by_product(
    rg_costs: list[tuple[str, float]],
    *,
    is_production: bool,
) -> list[ProductRgGroup]:
    """Roll individual RG costs into workload buckets."""
    buckets: dict[str, ProductRgGroup] = {}
    for rg, cost in rg_costs:
        category = workload_category(rg)
        group = buckets.setdefault(category, ProductRgGroup(product=category))
        rounded = round(cost, 2)
        if is_production:
            group.production += rounded
            group.production_rgs.append((rg, rounded))
        else:
            group.non_production += rounded
            group.non_production_rgs.append((rg, rounded))

    for group in buckets.values():
        group.production = round(group.production, 2)
        group.non_production = round(group.non_production, 2)
        group.production_rgs.sort(key=lambda x: x[1], reverse=True)
        group.non_production_rgs.sort(key=lambda x: x[1], reverse=True)

    # Order product groups by spend, highest first (ties fall back to the
    # canonical workload order for a stable layout).
    return sorted(
        buckets.values(),
        key=lambda g: (-g.total, _CATEGORY_ORDER.get(g.product, len(WORKLOAD_CATEGORIES))),
    )
