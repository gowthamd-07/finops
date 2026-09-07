"""GCP collector (read-only).

Two data sources, selected by ``gcp.source`` (auto | csv | bigquery):

* **csv** — parse "Cost table" CSVs exported from the Cloud Billing Console
  (Billing -> Cost table -> Download CSV). This is the only way to get GCP cost
  history for months *before* BigQuery billing export was enabled, since GCP
  never backfills the export.
* **bigquery** — query the Cloud Billing export dataset(s). This is the
  forward-looking, fully-automated source (data lands ~24h after each usage day,
  starting from when the export was enabled — no history before that).

``auto`` (default) uses the CSV for a month if a matching file exists, otherwise
falls back to BigQuery.

Charges are positive ``Cost`` amounts; credits (spending-based / committed-use
discounts, free-tier, etc.) arrive as negative amounts and are stored as a
positive ``credits`` value. GCP invoices carry no separate tax line here.
"""
from __future__ import annotations

import csv
import glob
import logging
import os
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from ..models import CostRecord

log = logging.getLogger(__name__)

# 0-based column indexes in the Console "Cost table" CSV export.
_COL_ACCOUNT_NAME = 0
_COL_ACCOUNT_ID = 1
_COL_PROJECT_NAME = 2
_COL_PROJECT_ID = 3
_COL_SERVICE = 5
_COL_UNROUNDED_COST = 16
_HEADER_MARKER = "Billing account name"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def collect_gcp(cfg: dict, start: date, end: date) -> list[CostRecord]:
    gcp_cfg = cfg.get("gcp", {})
    source = str(gcp_cfg.get("source", "auto")).strip().lower()
    ym = start.strftime("%Y-%m")

    if source == "csv":
        records = _collect_from_csv(gcp_cfg, start, end)
        log.info("GCP: collected %d records from CSV for %s", len(records), ym)
        return records

    if source == "bigquery":
        try:
            records = _collect_from_bigquery(gcp_cfg, start, end)
        except Exception:  # noqa: BLE001 - never let GCP abort the whole run
            log.exception("GCP: BigQuery collection failed")
            return []
        log.info("GCP: collected %d records from BigQuery for %s", len(records), ym)
        return records

    # ---- auto ----
    # Prefer BigQuery (SKU/model/token detail) but only when it actually covers
    # the month. BigQuery has no history before the export was enabled, and the
    # month the export was turned on is partial — so we prefer BigQuery only when
    # its total reconciles with the Console CSV (within `bigquery_reconcile_ratio`),
    # or when there's no CSV for the month at all. Otherwise the CSV is
    # authoritative (complete history, but service-level only — no AI detail).
    csv_records = _collect_from_csv(gcp_cfg, start, end)
    try:
        bq_records = _collect_from_bigquery(gcp_cfg, start, end)
    except Exception:  # noqa: BLE001
        log.exception("GCP: BigQuery collection failed; using CSV for %s", ym)
        return csv_records

    if not bq_records:
        log.info("GCP: no BigQuery data for %s; using CSV (%d records)", ym, len(csv_records))
        return csv_records
    if not csv_records:
        log.info("GCP: no CSV for %s; using BigQuery (%d records)", ym, len(bq_records))
        return bq_records

    csv_total = sum(r.cost for r in csv_records)
    bq_total = sum(r.cost for r in bq_records)
    ratio = float(gcp_cfg.get("bigquery_reconcile_ratio", 0.98))
    if csv_total > 0 and bq_total >= csv_total * ratio:
        log.info(
            "GCP: BigQuery reconciles for %s (BQ $%.2f vs CSV $%.2f); using BigQuery detail",
            ym, bq_total, csv_total,
        )
        return bq_records
    log.info(
        "GCP: BigQuery incomplete for %s (BQ $%.2f < CSV $%.2f); using CSV",
        ym, bq_total, csv_total,
    )
    return csv_records


# --------------------------------------------------------------------------- #
# Environment classification (by project name/id).
# --------------------------------------------------------------------------- #
def _classifier(gcp_cfg: dict):
    default_env = gcp_cfg.get("default_environment", "production")
    overrides = {k.lower(): v for k, v in gcp_cfg.get("project_overrides", {}).items()}
    patterns = [
        (re.compile(p["pattern"], re.IGNORECASE), p["environment"])
        for p in gcp_cfg.get("project_patterns", [])
    ]

    def classify(project_id: str, project_name: str) -> str:
        for key in (project_id or "", project_name or ""):
            if key.lower() in overrides:
                return overrides[key.lower()]
        haystack = f"{project_id} {project_name}"
        for rx, env in patterns:
            if rx.search(haystack):
                return env
        return default_env

    return classify


# --------------------------------------------------------------------------- #
# CSV source.
# --------------------------------------------------------------------------- #
def _csv_dirs(gcp_cfg: dict) -> list[Path]:
    configured = gcp_cfg.get("csv_dir", "gcp-billing")
    candidates = [
        Path(configured),
        Path.cwd() / configured,
        _REPO_ROOT / configured,
    ]
    seen: dict[Path, Path] = {}
    for c in candidates:
        if not c.is_dir():
            continue
        resolved = c.resolve()
        if resolved not in seen:  # dedup dirs that resolve to the same location
            seen[resolved] = resolved
    return list(seen.values())


def _month_csv_files(gcp_cfg: dict, start: date) -> list[Path]:
    """Cost-table CSVs whose usage window starts in the target month."""
    tag = f"{start.year:04d}-{start.month:02d}-01"  # e.g. "2026-01-01"
    files: list[Path] = []
    for d in _csv_dirs(gcp_cfg):
        for path in sorted(glob.glob(str(d / "*.csv"))):
            if tag in os.path.basename(path):
                files.append(Path(path))
    return files


def _parse_float(value: str) -> float:
    value = (value or "").strip().strip('"').replace(",", "")
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _collect_from_csv(gcp_cfg: dict, start: date, end: date) -> list[CostRecord]:
    classify = _classifier(gcp_cfg)
    files = _month_csv_files(gcp_cfg, start)
    if not files:
        return []

    # Aggregate (service, project_id) -> [charge, credit], keep names/account.
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"charge": 0.0, "credit": 0.0, "project_name": "", "account": "", "account_name": ""}
    )
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            in_body = False
            for row in reader:
                if not row:
                    continue
                if not in_body:
                    if row[0].strip() == _HEADER_MARKER:
                        in_body = True
                    continue
                # Body rows have a billing-account name and a service; footer
                # rows (Total / Rounding error) have an empty first column.
                if len(row) <= _COL_UNROUNDED_COST:
                    continue
                if not row[_COL_ACCOUNT_NAME].strip() or not row[_COL_SERVICE].strip():
                    continue
                service = row[_COL_SERVICE].strip()
                project_id = row[_COL_PROJECT_ID].strip()
                project_name = row[_COL_PROJECT_NAME].strip()
                amount = _parse_float(row[_COL_UNROUNDED_COST])
                key = (service, project_id or project_name or "unassigned")
                bucket = agg[key]
                bucket["project_name"] = project_name or project_id
                bucket["account"] = row[_COL_ACCOUNT_ID].strip()
                bucket["account_name"] = row[_COL_ACCOUNT_NAME].strip()
                if amount >= 0:
                    bucket["charge"] += amount
                else:
                    bucket["credit"] += -amount

    records: list[CostRecord] = []
    for (service, project_id), b in agg.items():
        charge = round(b["charge"], 2)
        credit = round(b["credit"], 2)
        if abs(charge) < 0.005 and credit < 0.005:
            continue
        records.append(
            CostRecord(
                cloud="GCP",
                service=service,
                cost=charge,
                credits=credit,
                environment=classify(project_id, b["project_name"]),
                subscription=b["account_name"],
                scope=b["project_name"] or project_id,
                account=b["account"],
                period_start=start,
                period_end=end,
            )
        )
    return records


# --------------------------------------------------------------------------- #
# BigQuery source (forward-looking; requires the export to have run).
# --------------------------------------------------------------------------- #
def _collect_from_bigquery(gcp_cfg: dict, start: date, end: date) -> list[CostRecord]:
    from google.cloud import bigquery  # lazy: only needed for the BQ path

    sources = gcp_cfg.get("bigquery", [])
    if not sources:
        log.info("GCP: no gcp.bigquery sources configured; skipping BigQuery")
        return []

    classify = _classifier(gcp_cfg)
    invoice_month = f"{start.year:04d}{start.month:02d}"  # export invoice.month is "YYYYMM"
    records: list[CostRecord] = []

    for src in sources:
        project = src["project"]
        dataset = src["dataset"]
        client = bigquery.Client(project=project)
        for table_ref in client.list_tables(f"{project}.{dataset}"):
            # Only the STANDARD export (gcp_billing_export_v1_*). A dataset often
            # also holds the RESOURCE export (gcp_billing_export_resource_v1_*)
            # which repeats the same costs at resource granularity — summing both
            # would double-count every GCP charge.
            if not table_ref.table_id.startswith("gcp_billing_export_v1"):
                continue
            fq = f"`{project}.{dataset}.{table_ref.table_id}`"
            # SKU-level granularity so AI services (Vertex AI / Gemini) expose the
            # model (sku.description) and token/usage amount. Non-AI services roll
            # back up by service in the existing tables, so totals are unchanged.
            query = f"""
                SELECT
                  service.description AS service,
                  sku.description AS sku,
                  project.id AS project_id,
                  project.name AS project_name,
                  billing_account_id AS account,
                  usage.pricing_unit AS usage_unit,
                  SUM(cost) AS charge,
                  SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credit,
                  SUM(IFNULL(usage.amount_in_pricing_units, 0)) AS usage_amount
                FROM {fq}
                WHERE invoice.month = @month
                GROUP BY service, sku, project_id, project_name, account, usage_unit
            """
            job = client.query(
                query,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("month", "STRING", invoice_month)]
                ),
            )
            for r in job.result():
                charge = round(float(r["charge"] or 0.0), 2)
                credit = round(-float(r["credit"] or 0.0), 2)  # credit amounts are negative
                if abs(charge) < 0.005 and abs(credit) < 0.005:
                    continue
                records.append(
                    CostRecord(
                        cloud="GCP",
                        service=r["service"] or "Unassigned",
                        cost=charge,
                        credits=max(credit, 0.0),
                        environment=classify(r["project_id"] or "", r["project_name"] or ""),
                        subscription=r["account"] or "",
                        scope=r["project_name"] or r["project_id"] or "",
                        account=r["account"] or "",
                        period_start=start,
                        period_end=end,
                        model=r.get("sku") or "",
                        tokens=round(float(r.get("usage_amount") or 0.0), 0),
                        usage_unit=r.get("usage_unit") or "",
                    )
                )
    return records
