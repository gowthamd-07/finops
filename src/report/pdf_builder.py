"""Render ReportData to a PDF using Jinja2 + WeasyPrint."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from ..models import ReportData
from .changes import normalize_change_rows
from .filters import subscription_map
from .groups import (
    ai_breakdown,
    aws_breakdown,
    azure_cost_breakdown,
    gcp_breakdown,
    gcp_by_project,
    group_azure_by_subscription,
    group_by_cloud,
    subscription_names,
)

_TEMPLATE_DIR = Path(__file__).parent


def build_pdf(data: ReportData, output_path: str, title: str, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    sub_map = subscription_map(cfg)
    names = subscription_names(cfg)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    data.title = title
    data.key_increases = normalize_change_rows(data.key_increases)
    data.savings = normalize_change_rows(data.savings)
    html = template.render(
        data=data,
        clouds=group_by_cloud(data),
        azure_sub=group_azure_by_subscription(data.records, sub_map, names),
        azure_breakdown=azure_cost_breakdown(data.records, sub_map),
        aws=aws_breakdown(data.records),
        gcp=gcp_breakdown(data.records),
        gcp_project=gcp_by_project(data.records),
        ai=ai_breakdown(data.records, cfg),
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(out))
    return str(out)
