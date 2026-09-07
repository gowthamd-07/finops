"""Anthropic (Claude) collector — LLM API spend (read-only).

Source: the Anthropic Admin ``GET /v1/organizations/cost_report`` endpoint, which
returns time-bucketed cost data (daily granularity) for the whole organization —
the same numbers shown on the Console Cost page. Grouping by ``description`` adds
a parsed ``model`` field so the report can break spend down per model.

To attribute **token usage per model and per API key** (Anthropic's cost report
cannot be grouped by API key), we additionally call the Usage API
(``/v1/organizations/usage_report/messages``) grouped by model + api_key_id, and
resolve key ids to names via ``/v1/organizations/api_keys``. These usage rows
carry tokens only (cost = 0) because Anthropic does not expose cost per key.

Auth: an **Admin API key** (``sk-ant-admin01-...``), which is different from the
standard ``sk-ant-api...`` keys used to call the models. Create it in the Claude
Console > Settings > Admin keys and provide it via ANTHROPIC_ADMIN_KEY (locally
in .env, in production from Key Vault).

Costs are returned in USD as decimal strings in the lowest unit (cents); we sum
them per model into one CostRecord each. Priority Tier spend uses a different
billing model and is not included by this endpoint.

The endpoint reports pre-tax consumption. Anthropic charges sales tax on the
prepaid-credit purchases that usage draws down (a $500 grant invoices at $551.50,
i.e. +10.3% in Washington), so when ``anthropic.tax_rate`` is set the collector
grosses up each cost line by that rate (carried on ``CostRecord.tax``) to reflect
what is actually paid. Set the rate to 0 to report usage pre-tax.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime

import requests

from ..models import CostRecord

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.anthropic.com/v1/organizations/cost_report"
_USAGE_ENDPOINT = "https://api.anthropic.com/v1/organizations/usage_report/messages"
_API_KEYS_ENDPOINT = "https://api.anthropic.com/v1/organizations/api_keys"
_API_VERSION = "2023-06-01"
_MAX_PAGES = 50            # safety cap on next_page follow-through
_TIMEOUT = 60             # seconds per request


def _admin_key(anthropic_cfg: dict) -> str | None:
    import os

    key = (
        os.environ.get("ANTHROPIC_ADMIN_KEY")
        or os.environ.get("ANTHROPIC_ADMIN_API_KEY")
        or anthropic_cfg.get("admin_key")
        or ""
    ).strip()
    return key or None


def _rfc3339(d: date) -> str:
    return datetime(d.year, d.month, d.day).strftime("%Y-%m-%dT00:00:00Z")


def collect_anthropic(cfg: dict, start: date, end: date) -> list[CostRecord]:
    """Return per-model Claude spend for [start, end) (end exclusive)."""
    anthropic_cfg = cfg.get("anthropic", {})
    key = _admin_key(anthropic_cfg)
    if not key:
        log.info("Anthropic: no ANTHROPIC_ADMIN_KEY configured; skipping")
        return []

    environment = anthropic_cfg.get("environment", "production")
    headers = {
        "anthropic-version": _API_VERSION,
        "x-api-key": key,
    }
    # `ending_at` is exclusive ("buckets that end before"), so the month's
    # exclusive end (first-of-next-month) is exactly what we want.
    params = {
        "starting_at": _rfc3339(start),
        "ending_at": _rfc3339(end),
        "bucket_width": "1d",
        "group_by[]": ["description"],
        "limit": 31,
    }

    by_model: dict[str, float] = defaultdict(float)
    session = requests.Session()
    page_token: str | None = None
    pages = 0
    while pages < _MAX_PAGES:
        pages += 1
        q = dict(params)
        if page_token:
            q["page"] = page_token
        try:
            resp = session.get(_ENDPOINT, headers=headers, params=q, timeout=_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException:
            log.exception("Anthropic: cost_report request failed")
            break

        body = resp.json()
        for bucket in body.get("data", []) or []:
            for result in bucket.get("results", []) or []:
                amount = result.get("amount", 0)
                try:
                    cents = float(amount)
                except (TypeError, ValueError):
                    cents = 0.0
                model = (result.get("model") or result.get("description") or "Claude API").strip()
                by_model[model or "Claude API"] += cents

        if not body.get("has_more"):
            break
        page_token = body.get("next_page")
        if not page_token:
            break

    # cost_report is pre-tax usage; Anthropic charges tax on the prepaid-credit
    # purchases that usage draws down, so gross up by the configured rate to match
    # what is actually paid (tax carried on CostRecord.tax, like Cursor seats).
    tax_rate = float(anthropic_cfg.get("tax_rate", 0) or 0) / 100.0

    records: list[CostRecord] = []
    for model, cents in by_model.items():
        cost = round(cents / 100.0, 2)
        if abs(cost) < 0.005:
            continue
        records.append(
            CostRecord(
                cloud="Claude",
                service=model,
                cost=cost,
                tax=round(cost * tax_rate, 2),
                environment=environment,
                account="Anthropic Org",
                period_start=start,
                period_end=end,
                model=model,
            )
        )

    # Token usage per (model, api_key) — cost is not available per key, so these
    # rows carry tokens only (cost=0) and drive the per-model token totals and
    # the top-users-by-API-key breakdown. Never let usage failures break cost.
    try:
        records.extend(
            _collect_usage(session, headers, anthropic_cfg, start, end, environment)
        )
    except Exception:  # noqa: BLE001
        log.exception("Anthropic: usage/token collection failed; cost still reported")

    log.info(
        "Anthropic: collected %d lines, cost total=%.2f",
        len(records), round(sum(r.cost for r in records), 2),
    )
    return records


def _api_key_names(session, headers) -> dict[str, str]:
    """Map api_key_id -> human name via the admin api_keys endpoint."""
    names: dict[str, str] = {}
    page_token: str | None = None
    pages = 0
    while pages < _MAX_PAGES:
        pages += 1
        q = {"limit": 100}
        if page_token:
            q["page"] = page_token
        resp = session.get(_API_KEYS_ENDPOINT, headers=headers, params=q, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        for k in body.get("data", []) or []:
            kid = k.get("id")
            if kid:
                names[kid] = k.get("name") or kid
        if not body.get("has_more"):
            break
        page_token = body.get("next_page")
        if not page_token:
            break
    return names


def _sum_tokens(result: dict) -> float:
    """Sum every integer field whose name mentions tokens (schema-robust)."""
    total = 0.0
    for k, v in result.items():
        if "token" in k.lower() and isinstance(v, (int, float)):
            total += float(v)
    return total


def _collect_usage(session, headers, anthropic_cfg, start, end, environment) -> list[CostRecord]:
    key_names = _api_key_names(session, headers)

    params = {
        "starting_at": _rfc3339(start),
        "ending_at": _rfc3339(end),
        "bucket_width": "1d",
        "group_by[]": ["model", "api_key_id"],
        "limit": 31,
    }
    # Aggregate per (model, api_key) -> tokens.
    agg: dict[tuple[str, str], float] = defaultdict(float)
    page_token: str | None = None
    pages = 0
    while pages < _MAX_PAGES:
        pages += 1
        q = dict(params)
        if page_token:
            q["page"] = page_token
        resp = session.get(_USAGE_ENDPOINT, headers=headers, params=q, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        for bucket in body.get("data", []) or []:
            for result in bucket.get("results", []) or []:
                model = (result.get("model") or "Claude API").strip() or "Claude API"
                kid = result.get("api_key_id") or ""
                user = key_names.get(kid, kid or "unknown")
                agg[(model, user)] += _sum_tokens(result)
        if not body.get("has_more"):
            break
        page_token = body.get("next_page")
        if not page_token:
            break

    records: list[CostRecord] = []
    for (model, user), tokens in agg.items():
        tokens = round(tokens, 0)
        if tokens < 1:
            continue
        records.append(
            CostRecord(
                cloud="Claude",
                service=model,
                cost=0.0,   # cost is not attributable per API key
                environment=environment,
                account="Anthropic Org",
                period_start=start,
                period_end=end,
                model=model,
                tokens=tokens,
                usage_unit="tokens",
                user=user,
            )
        )
    return records
