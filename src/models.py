"""Normalized data model shared by all cloud collectors."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class CostRecord:
    """A single normalized cost line item from any cloud."""

    cloud: str                      # "AWS" | "Azure"
    service: str                    # service name as billed
    cost: float                     # pre-tax / pre-credit subtotal in report currency
    category: str = ""              # e.g. Compute, Storage, Networking
    publisher: str = ""             # Azure publisher (Microsoft / Marketplace)
    tax: float = 0.0
    credits: float = 0.0
    environment: str = "non-production"   # production | non-production | shared
    subscription: str = ""            # Azure subscription name (Production, etc.)
    scope: str = ""                 # resource group (Azure) / account (AWS) / project (GCP)
    account: str = ""               # subscription id / billing account / aws account
    period_start: date | None = None
    period_end: date | None = None
    # --- LLM / AI attribution (populated only by the AI collectors) ---
    model: str = ""                 # LLM model name (falls back to `service`)
    tokens: float = 0.0             # token / usage quantity for the line
    usage_unit: str = ""            # unit for `tokens` (tokens | count | hour | ...)
    user: str = ""                  # owner: user email, service-account, or API key name

    @property
    def total(self) -> float:
        return round(self.cost - self.credits + self.tax, 2)


@dataclass
class CloudSummary:
    """Per-cloud rollup for the top summary table."""

    cloud: str
    subtotal: float = 0.0
    credits: float = 0.0
    tax: float = 0.0

    @property
    def total(self) -> float:
        return round(self.subtotal - self.credits + self.tax, 2)


@dataclass
class ReportData:
    """Everything the PDF template needs."""

    month: str
    year: int
    currency: str
    title: str = "Monthly Cloud Infrastructure Cost"
    summaries: list[CloudSummary] = field(default_factory=list)
    records: list[CostRecord] = field(default_factory=list)
    # Executive summary (Azure gross subtotal MoM — matches manual report).
    azure_prev_gross: float | None = None
    azure_curr_gross: float | None = None
    prev_total: float | None = None
    curr_total: float | None = None
    key_increases: list[dict] = field(default_factory=list)
    savings: list[dict] = field(default_factory=list)
    # Trend analytics (plain JSON-friendly structures, computed in service.py).
    projection: dict | None = None
    mom: dict | None = None
    period_summaries: list[dict] = field(default_factory=list)
    credits_projection: dict | None = None
    anthropic_credits: dict | None = None

    @property
    def grand_total(self) -> float:
        return round(sum(s.total for s in self.summaries), 2)

    @property
    def amount_due(self) -> float:
        """Net monthly bill after credits and tax (what you actually pay)."""
        return self.grand_total

    @property
    def gross_subtotal(self) -> float:
        return round(sum(s.subtotal for s in self.summaries), 2)

    @property
    def total_credits(self) -> float:
        return round(sum(s.credits for s in self.summaries), 2)

    @property
    def total_tax(self) -> float:
        return round(sum(s.tax for s in self.summaries), 2)

    @property
    def show_credits(self) -> bool:
        return self.total_credits > 0.005
