"""Render ReportData to a CSV of line items (spreadsheet-friendly export).

Mirrors pdf_builder.build_pdf, but emits a flat table of every cost line item
instead of the formatted report — handy for pivoting in a spreadsheet.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ..billing import line_total
from ..models import ReportData
from .filters import subscription_map

_CLOUD_ORDER = {"AWS": 0, "Azure": 1, "GCP": 2, "Cursor": 3, "Claude": 4}

_HEADER = [
    "Cloud",
    "Subscription",
    "Account",
    "Service",
    "Category",
    "Publisher",
    "Scope",
    "Environment",
    "Model",
    "User",
    "Tokens",
    "Usage Unit",
    "Subtotal",
    "Credits",
    "Tax",
    "Total",
    "Currency",
]


def build_csv_bytes(data: ReportData, cfg: dict | None = None) -> bytes:
    """Return the report's line items as UTF-8 CSV bytes."""
    cfg = cfg or {}
    sub_map = subscription_map(cfg)

    rows = sorted(
        data.records,
        key=lambda r: (_CLOUD_ORDER.get(r.cloud, 99), -line_total(r)),
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADER)
    for r in rows:
        subscription = r.subscription or sub_map.get(r.account, "")
        writer.writerow([
            r.cloud,
            subscription,
            r.account,
            r.service,
            r.category,
            r.publisher,
            r.scope,
            r.environment,
            r.model,
            r.user,
            round(r.tokens, 0) if r.tokens else "",
            r.usage_unit,
            round(r.cost, 2),
            round(r.credits, 2),
            round(r.tax, 2),
            r.total,
            data.currency,
        ])
    return buf.getvalue().encode("utf-8-sig")


def build_csv(data: ReportData, output_path: str, title: str, cfg: dict | None = None) -> str:
    """Write the report's line items to *output_path* as CSV, returning the path."""
    data.title = title
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_csv_bytes(data, cfg))
    return str(out)
