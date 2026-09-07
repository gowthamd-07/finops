"""Postgres-backed store for generated reports (JSON data + PDF) per month."""
from __future__ import annotations

import logging
from dataclasses import asdict

from psycopg.types.json import Jsonb

from . import db
from .report.changes import normalize_change_rows
from .models import CloudSummary, CostRecord, ReportData

log = logging.getLogger(__name__)


class ReportStore:
    def __init__(self, cfg: dict | None = None):
        self._cfg = cfg

    def _conn(self):
        return db.connect(self._cfg)

    def save(self, month_key: str, report: ReportData, pdf_bytes: bytes) -> None:
        payload = _serialize(report)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reports (month_key, month, year, currency,
                    grand_total, gross_subtotal, data, pdf, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (month_key) DO UPDATE SET
                    month=EXCLUDED.month, year=EXCLUDED.year, currency=EXCLUDED.currency,
                    grand_total=EXCLUDED.grand_total, gross_subtotal=EXCLUDED.gross_subtotal,
                    data=EXCLUDED.data, pdf=EXCLUDED.pdf, updated_at=now()
                """,
                (month_key, report.month, report.year, report.currency,
                 report.grand_total, report.gross_subtotal, Jsonb(payload), pdf_bytes),
            )
            conn.commit()
        log.info("store: saved report %s", month_key)

    def has(self, month_key: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM reports WHERE month_key=%s", (month_key,))
            return cur.fetchone() is not None

    def pdf_bytes(self, month_key: str) -> bytes | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pdf FROM reports WHERE month_key=%s", (month_key,))
            row = cur.fetchone()
            return bytes(row[0]) if row else None

    def load(self, month_key: str) -> ReportData | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM reports WHERE month_key=%s", (month_key,))
            row = cur.fetchone()
            return _deserialize(row[0]) if row else None

    def load_cached(self, month_key: str) -> tuple[ReportData, bytes] | None:
        """Return stored report + PDF in one query, or None if missing."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT data, pdf FROM reports WHERE month_key=%s", (month_key,))
            row = cur.fetchone()
            if not row or not row[1]:
                return None
            return _deserialize(row[0]), bytes(row[1])

    def list_months(self) -> list[str]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT month_key FROM reports ORDER BY month_key DESC")
            return [r[0] for r in cur.fetchall()]

    def list_meta(self) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT month_key, month, year, grand_total, gross_subtotal "
                "FROM reports ORDER BY month_key DESC"
            )
            return [
                {
                    "month_key": r[0],
                    "month": r[1],
                    "year": r[2],
                    "grand_total": float(r[3]),
                    "gross_subtotal": float(r[4]),
                }
                for r in cur.fetchall()
            ]

    def load_all_records(self) -> list[tuple[str, list]]:
        """Return [(month_key, records), ...] oldest first for trend charts."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT month_key, data FROM reports ORDER BY month_key ASC")
            rows = cur.fetchall()
        out = []
        for month_key, data in rows:
            report = _deserialize(data)
            out.append((month_key, report.records))
        return out

    def trend(self) -> list[dict]:
        """Per-month totals (oldest first) for charts and analytics."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT month_key, grand_total, gross_subtotal, data "
                "FROM reports ORDER BY month_key ASC"
            )
            rows = cur.fetchall()
        out = []
        for month_key, grand_total, subtotal, data in rows:
            summaries = data.get("summaries", [])
            out.append({
                "month": month_key,
                "grand_total": float(grand_total),
                "subtotal": float(subtotal),
                "credits": round(sum(float(s.get("credits", 0) or 0) for s in summaries), 2),
                # CloudSummary.total is a computed property, so it is not present in
                # the serialized JSON — reconstruct the per-cloud net total here.
                "clouds": {
                    s["cloud"]: round(
                        float(s.get("subtotal", 0) or 0)
                        - float(s.get("credits", 0) or 0)
                        + float(s.get("tax", 0) or 0),
                        2,
                    )
                    for s in summaries
                },
            })
        return out


class CursorSpendStore:
    """Persists per-cycle Cursor billed-spend snapshots (see db._SCHEMA)."""

    def __init__(self, cfg: dict | None = None):
        self._cfg = cfg

    def _conn(self):
        return db.connect(self._cfg)

    def save_snapshot(
        self,
        cycle_start,
        cycle_end,
        billed_cents: float,
        list_cents: float,
        members: int,
    ) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cursor_billed_spend
                    (cycle_start, cycle_end, billed_cents, list_cents, members, captured_at)
                VALUES (%s,%s,%s,%s,%s, now())
                ON CONFLICT (cycle_start) DO UPDATE SET
                    cycle_end=EXCLUDED.cycle_end,
                    billed_cents=EXCLUDED.billed_cents,
                    list_cents=EXCLUDED.list_cents,
                    members=EXCLUDED.members,
                    captured_at=now()
                """,
                (cycle_start, cycle_end, billed_cents, list_cents, members),
            )
            conn.commit()
        log.info("store: saved Cursor billed-spend snapshot for cycle %s", cycle_start)

    def get_snapshot(self, cycle_start) -> dict | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cycle_start, cycle_end, billed_cents, list_cents, members "
                "FROM cursor_billed_spend WHERE cycle_start=%s",
                (cycle_start,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "cycle_start": row[0],
                "cycle_end": row[1],
                "billed_cents": float(row[2]),
                "list_cents": float(row[3]),
                "members": int(row[4]),
            }


def _serialize(report: ReportData) -> dict:
    data = asdict(report)
    for rec in data.get("records", []):
        for k in ("period_start", "period_end"):
            if rec.get(k) is not None:
                rec[k] = rec[k].isoformat()
    return data


def _deserialize(data: dict) -> ReportData:
    records = []
    for r in data.get("records", []):
        rec = {**r, "period_start": None, "period_end": None}
        rec.setdefault("subscription", "")
        records.append(CostRecord(**rec))
    summaries = [CloudSummary(**s) for s in data.get("summaries", [])]
    return ReportData(
        month=data["month"],
        year=data["year"],
        currency=data["currency"],
        title=data.get("title", "Monthly Cloud Infrastructure Cost"),
        summaries=summaries,
        records=records,
        prev_total=data.get("prev_total"),
        curr_total=data.get("curr_total"),
        azure_prev_gross=data.get("azure_prev_gross"),
        azure_curr_gross=data.get("azure_curr_gross"),
        key_increases=normalize_change_rows(data.get("key_increases", [])),
        savings=normalize_change_rows(data.get("savings", [])),
        projection=data.get("projection"),
        mom=data.get("mom"),
        period_summaries=data.get("period_summaries", []),
        credits_projection=data.get("credits_projection"),
        anthropic_credits=data.get("anthropic_credits"),
    )
