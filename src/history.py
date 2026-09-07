"""Postgres-backed per-month history for month-over-month comparisons."""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from . import db

log = logging.getLogger(__name__)


class HistoryStore:
    def __init__(self, cfg: dict | None = None):
        self._cfg = cfg

    def _conn(self):
        return db.connect(self._cfg)

    def get_month(self, month_key: str) -> dict | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT grand_total, services FROM cost_history WHERE month_key=%s",
                (month_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"grand_total": float(row[0]), "services": row[1]}

    def save_month(self, month_key: str, grand_total: float, services: dict[str, float]) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cost_history (month_key, grand_total, services)
                VALUES (%s,%s,%s)
                ON CONFLICT (month_key) DO UPDATE SET
                    grand_total=EXCLUDED.grand_total, services=EXCLUDED.services
                """,
                (month_key, grand_total, Jsonb(services)),
            )
            conn.commit()
        log.info("history: saved %s (total=%.2f)", month_key, grand_total)
