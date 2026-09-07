"""Postgres connection + schema management (targets Azure Database for PostgreSQL).

Auth modes (config `database.auth` or $DB_AUTH):
  - "password" (default): full DSN in $DATABASE_URL / config `database.url`, e.g.
      postgresql://user:pass@srv.postgres.database.azure.com/db?sslmode=require
  - "entra": passwordless via Azure AD. DSN carries host/db/user (the managed
      identity or AAD principal name) and NO password; an Entra access token is
      fetched at connect time using Workload Identity (DefaultAzureCredential).
"""
from __future__ import annotations

import logging
import os
import threading
import time

import psycopg

log = logging.getLogger(__name__)

# Token scope for Azure Database for PostgreSQL Flexible Server (AAD auth).
_PG_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    month_key      text PRIMARY KEY,
    month          text NOT NULL,
    year           int  NOT NULL,
    currency       text NOT NULL,
    grand_total    numeric NOT NULL,
    gross_subtotal numeric NOT NULL,
    data           jsonb   NOT NULL,
    pdf            bytea   NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cost_history (
    month_key   text PRIMARY KEY,
    grand_total numeric NOT NULL,
    services    jsonb   NOT NULL
);

-- Web UI users with roles (admin | viewer). The bootstrap admin from
-- BASIC_AUTH_USER/PASSWORD still works when this table is empty.
CREATE TABLE IF NOT EXISTS app_users (
    username      text PRIMARY KEY,
    password_hash text NOT NULL,
    role          text NOT NULL DEFAULT 'viewer',
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Azure resource-group inventory (workload/environment catalog). Replaces the
-- Azureresourcegroups.csv file; categories are derived but can be overridden.
CREATE TABLE IF NOT EXISTS azure_resource_groups (
    name          text NOT NULL,
    subscription  text NOT NULL DEFAULT '',
    product       text NOT NULL DEFAULT 'Others',
    env_tier      text NOT NULL DEFAULT '',
    location      text NOT NULL DEFAULT '',
    resource_link text NOT NULL DEFAULT '',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, subscription)
);

-- Cursor billed-spend snapshots per billing cycle. Cursor only exposes billed
-- spend (the overage beyond each seat's included allotment) for the CURRENT
-- cycle via /teams/spend, and it resets when the cycle rolls. A daily job
-- snapshots it (keyed by the cycle start date) so the monthly report can read
-- the completed cycle's billed total long after the cycle has rolled over.
CREATE TABLE IF NOT EXISTS cursor_billed_spend (
    cycle_start  date PRIMARY KEY,
    cycle_end    date,
    billed_cents numeric NOT NULL DEFAULT 0,
    list_cents   numeric NOT NULL DEFAULT 0,
    members      int     NOT NULL DEFAULT 0,
    captured_at  timestamptz NOT NULL DEFAULT now()
);

-- Azure invoice billing summary per month (Charges / Azure Credit / Tax /
-- Total). Populated from the Billing API; the Cost Management usage API does
-- not return the Azure Credit. Replaces config/azure_invoices.csv.
CREATE TABLE IF NOT EXISTS azure_invoices (
    month_key      text PRIMARY KEY,
    invoice_number text    NOT NULL DEFAULT '',
    charges        numeric NOT NULL DEFAULT 0,
    other_credits  numeric NOT NULL DEFAULT 0,
    azure_credit   numeric NOT NULL DEFAULT 0,
    subtotal       numeric NOT NULL DEFAULT 0,
    tax            numeric NOT NULL DEFAULT 0,
    total          numeric NOT NULL DEFAULT 0,
    source         text    NOT NULL DEFAULT 'api',
    updated_at     timestamptz NOT NULL DEFAULT now()
);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


class DatabaseNotConfigured(RuntimeError):
    pass


def get_dsn(cfg: dict | None = None) -> str:
    dsn = os.environ.get("DATABASE_URL") or (cfg or {}).get("database", {}).get("url")
    if not dsn:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set (and database.url is empty in config)"
        )
    return dsn


def _auth_mode(cfg: dict | None) -> str:
    return (os.environ.get("DB_AUTH") or (cfg or {}).get("database", {}).get("auth")
            or "password").lower()


def _entra_token() -> str:
    """Fetch (and cache) an Azure AD access token for Postgres."""
    now = time.time()
    cached = _token_cache.get(_PG_AAD_SCOPE)
    if cached and cached[1] - 120 > now:
        return cached[0]
    with _token_lock:
        cached = _token_cache.get(_PG_AAD_SCOPE)
        if cached and cached[1] - 120 > now:
            return cached[0]
        from azure.identity import DefaultAzureCredential
        token = DefaultAzureCredential().get_token(_PG_AAD_SCOPE)
        _token_cache[_PG_AAD_SCOPE] = (token.token, token.expires_on)
        return token.token


def connect(cfg: dict | None = None) -> psycopg.Connection:
    dsn = get_dsn(cfg)
    if _auth_mode(cfg) == "entra":
        # Token is used as the password; user/host/db come from the DSN.
        conn = psycopg.connect(dsn, password=_entra_token())
    else:
        conn = psycopg.connect(dsn)
    _ensure_schema(conn, dsn)
    return conn


def _ensure_schema(conn: psycopg.Connection, dsn: str) -> None:
    if dsn in _initialized:
        return
    with _init_lock:
        if dsn in _initialized:
            return
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
        _initialized.add(dsn)
        log.info("db: schema ensured")
