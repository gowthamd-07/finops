"""Database-backed Azure catalogs: resource-group inventory and invoice summaries.

These replace the on-disk ``Azureresourcegroups.csv`` and
``config/azure_invoices.csv`` files. The CSVs (when present) are only used to
*seed* these tables; at runtime the loaders read from Postgres.
"""
from __future__ import annotations

import logging

from . import db
from .report.azure_rg import RgCatalogEntry, env_tier, workload_category

log = logging.getLogger(__name__)

_INVOICE_FIELDS = (
    "invoice_number",
    "charges",
    "other_credits",
    "azure_credit",
    "subtotal",
    "tax",
    "total",
)


# --------------------------------------------------------------------------- #
# Resource-group catalog
# --------------------------------------------------------------------------- #
def load_rg_catalog_db(cfg: dict | None) -> dict[str, RgCatalogEntry]:
    """Return {name_lower: RgCatalogEntry} from the DB, or {} if unavailable."""
    try:
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, subscription, product, env_tier FROM azure_resource_groups"
            )
            rows = cur.fetchall()
    except db.DatabaseNotConfigured:
        return {}
    except Exception:  # noqa: BLE001 - never break reports on catalog read
        log.exception("azure_catalog: failed to read RG catalog from DB")
        return {}
    out: dict[str, RgCatalogEntry] = {}
    for name, subscription, product, tier in rows:
        out[name.lower()] = RgCatalogEntry(
            name=name,
            subscription=subscription or "",
            product=product or workload_category(name),
            env_tier=tier or env_tier(name),
        )
    return out


def upsert_rg_entries(cfg: dict | None, entries: list[dict]) -> int:
    """Insert/update resource-group rows. Each entry: name, subscription,
    optional location, resource_link. product/env_tier are derived if missing."""
    if not entries:
        return 0
    rows = []
    for e in entries:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        rows.append((
            name,
            (e.get("subscription") or "").strip(),
            (e.get("product") or workload_category(name)),
            (e.get("env_tier") or env_tier(name)),
            (e.get("location") or "").strip(),
            (e.get("resource_link") or "").strip(),
        ))
    if not rows:
        return 0
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO azure_resource_groups
                (name, subscription, product, env_tier, location, resource_link, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (name, subscription) DO UPDATE SET
                product=EXCLUDED.product, env_tier=EXCLUDED.env_tier,
                location=EXCLUDED.location, resource_link=EXCLUDED.resource_link,
                updated_at=now()
            """,
            rows,
        )
        conn.commit()
    log.info("azure_catalog: upserted %d resource groups", len(rows))
    return len(rows)


# --------------------------------------------------------------------------- #
# Invoice billing summaries
# --------------------------------------------------------------------------- #
def load_invoices_db(cfg: dict | None) -> dict[str, dict]:
    """Return {month_key: {charges, other_credits, azure_credit, subtotal, tax,
    total, invoice_number}} from the DB, or {} if unavailable."""
    try:
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT month_key, invoice_number, charges, other_credits, "
                "azure_credit, subtotal, tax, total FROM azure_invoices"
            )
            rows = cur.fetchall()
    except db.DatabaseNotConfigured:
        return {}
    except Exception:  # noqa: BLE001
        log.exception("azure_catalog: failed to read invoices from DB")
        return {}
    out: dict[str, dict] = {}
    for mk, inv_no, charges, other_credits, azure_credit, subtotal, tax, total in rows:
        out[mk] = {
            "invoice_number": inv_no or "",
            "charges": float(charges),
            "other_credits": float(other_credits),
            "azure_credit": float(azure_credit),
            "subtotal": float(subtotal),
            "tax": float(tax),
            "total": float(total),
        }
    return out


def upsert_invoice(cfg: dict | None, month_key: str, inv: dict, *, source: str = "api") -> None:
    """Insert/update one month's invoice billing summary."""
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO azure_invoices
                (month_key, invoice_number, charges, other_credits, azure_credit,
                 subtotal, tax, total, source, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (month_key) DO UPDATE SET
                invoice_number=EXCLUDED.invoice_number, charges=EXCLUDED.charges,
                other_credits=EXCLUDED.other_credits, azure_credit=EXCLUDED.azure_credit,
                subtotal=EXCLUDED.subtotal, tax=EXCLUDED.tax, total=EXCLUDED.total,
                source=EXCLUDED.source, updated_at=now()
            """,
            (
                month_key,
                inv.get("invoice_number", ""),
                float(inv.get("charges", 0) or 0),
                float(inv.get("other_credits", 0) or 0),
                float(inv.get("azure_credit", 0) or 0),
                float(inv.get("subtotal", 0) or 0),
                float(inv.get("tax", 0) or 0),
                float(inv.get("total", 0) or 0),
                source,
            ),
        )
        conn.commit()
    log.info("azure_catalog: upserted invoice %s (source=%s)", month_key, source)


def delete_invoice(cfg: dict | None, month_key: str) -> None:
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM azure_invoices WHERE month_key=%s", (month_key,))
        conn.commit()
    log.info("azure_catalog: deleted invoice %s", month_key)
