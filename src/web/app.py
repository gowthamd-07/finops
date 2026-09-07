"""FastAPI web app for FinOps."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..billing import line_total, sum_credits, sum_subtotal, sum_tax, sum_total
from ..config import load_config
from ..delivery import send_email
from ..report.filters import (
    chart_by_dimension,
    filter_records,
    monthly_trend,
    subscription_map,
)
from ..analytics import build_analytics
from ..months import latest_allowed_month_key
from ..service import generate
from ..store import ReportStore
from .auth import (
    RequireLoginMiddleware,
    current_role,
    current_user,
    is_admin,
    login_user,
    logout_user,
    session_secret,
    verify_login,
)
from .report_view import build_report_context
from . import users as users_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cloud-cost-reporter.web")

# Load secrets/config from Key Vault (if configured) before anything reads env
# such as the session secret below.
from ..keyvault import load_secrets_into_env

load_secrets_into_env()

app = FastAPI(title="FinOps")

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_PUBLIC = {"/login", "/healthz", "/readyz"}
_PROD = os.environ.get("APP_ENV", "").strip().lower() in {"prod", "production"}


def _cfg() -> dict:
    return load_config(os.environ.get("CONFIG_PATH"))


def _store() -> ReportStore:
    return ReportStore(_cfg())


def _filter_kwargs(
    cloud: str | None,
    subscription: str | None,
    resource_group: str | None,
    product_group: str | None,
    service: str | None,
    env_tier: str | None = None,
) -> dict:
    return {
        k: v for k, v in {
            "cloud": cloud or None,
            "subscription": subscription or None,
            "resource_group": resource_group or None,
            "product_group": product_group or None,
            "env_tier": env_tier or None,
            "service": service or None,
        }.items() if v
    }


def _cloud_summary(records: list) -> dict:
    """Per-cloud + overall charge/credit/tax/total rollup for filtered records.

    Mirrors the server-rendered "Summary — All Clouds" table so the figures can
    be refreshed client-side whenever filters change.
    """
    order = {"AWS": 0, "Azure": 1, "GCP": 2}
    clouds = sorted({r.cloud for r in records}, key=lambda c: order.get(c, 99))
    rows = []
    for cloud in clouds:
        cloud_rows = [r for r in records if r.cloud == cloud]
        rows.append({
            "cloud": cloud,
            "subtotal": sum_subtotal(cloud_rows),
            "credits": sum_credits(cloud_rows),
            "tax": sum_tax(cloud_rows),
            "total": sum_total(cloud_rows),
        })
    return {
        "rows": rows,
        "gross_subtotal": sum_subtotal(records),
        "total_credits": sum_credits(records),
        "total_tax": sum_tax(records),
        "amount_due": sum_total(records),
    }


# Display order for clouds/providers in the historical trend view.
_CLOUD_ORDER = {"AWS": 0, "Azure": 1, "GCP": 2, "Cursor": 3, "Claude": 4, "Google": 5}


def _trend_payload(store: ReportStore) -> dict:
    """Per-month totals plus the ordered set of clouds seen across all months."""
    points = store.trend()
    seen: set[str] = set()
    for p in points:
        seen.update((p.get("clouds") or {}).keys())
    clouds = sorted(seen, key=lambda c: _CLOUD_ORDER.get(c, 99))
    return {"trend": points, "clouds": clouds}


# SessionMiddleware must be registered last so it runs first on each request.
app.add_middleware(RequireLoginMiddleware, public_paths=frozenset(_PUBLIC))
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    https_only=_PROD,        # secure cookies behind TLS in production
    same_site="lax",
)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: process is up (no external dependencies checked)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Response:
    """Readiness: verify the database is reachable before taking traffic."""
    try:
        from .. import db

        with db.connect(_cfg()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        import json

        return Response(content=json.dumps({"status": "ready"}), media_type="application/json")
    except Exception as exc:  # noqa: BLE001
        import json

        log.warning("readyz failed: %s", exc)
        payload = {"status": "unavailable"}
        if not _PROD:
            payload["detail"] = str(exc)
        return Response(
            content=json.dumps(payload),
            media_type="application/json",
            status_code=503,
        )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _TEMPLATES.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    role = verify_login(username, password)
    if role:
        login_user(request, username, role)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _TEMPLATES.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def _sidebar_context(active_nav: str = "new") -> dict:
    store = _store()
    months = store.list_months()
    meta_list = store.list_meta()
    return {
        "months": months,
        "report_meta": {m["month_key"]: m for m in meta_list},
        "active_nav": active_nav,
    }


def _require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admins only")


@app.get("/generate", response_class=HTMLResponse)
def generate_page(request: Request):
    default_month = latest_allowed_month_key()
    return _TEMPLATES.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user(request),
            "role": current_role(request),
            "default_month": default_month,
            "max_month": default_month,
            "db_error": None,
            **_sidebar_context("new"),
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request, month: str | None = None):
    store = _store()
    months = store.list_months()
    if month and month in months:
        return RedirectResponse(f"/reports/{month}", status_code=status.HTTP_303_SEE_OTHER)
    if months:
        return RedirectResponse(f"/reports/{months[0]}", status_code=status.HTTP_303_SEE_OTHER)

    default_month = latest_allowed_month_key()
    return _TEMPLATES.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user(request),
            "role": current_role(request),
            "default_month": default_month,
            "max_month": default_month,
            "db_error": None,
            **_sidebar_context("new"),
        },
    )


@app.get("/reports/{month_key}", response_class=HTMLResponse)
def report_detail(request: Request, month_key: str):
    store = _store()
    report = store.load(month_key)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    ctx = build_report_context(report, _cfg(), store=store, month_key=month_key)
    return _TEMPLATES.TemplateResponse(
        "report.html",
        {
            "request": request,
            "user": current_user(request),
            "role": current_role(request),
            "month_key": month_key,
            "report_meta": {m["month_key"]: m for m in store.list_meta()},
            "from_cache": request.query_params.get("cached") == "1",
            **_sidebar_context(month_key),
            **ctx,
        },
    )


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request):
    cfg = _cfg()
    store = _store()
    payload = _trend_payload(store)
    points = payload["trend"]
    analytics = build_analytics(cfg, points) if points else {}
    return _TEMPLATES.TemplateResponse(
        "trends.html",
        {
            "request": request,
            "user": current_user(request),
            "role": current_role(request),
            "points": points,
            "clouds": payload["clouds"],
            "projection": analytics.get("projection"),
            "mom": analytics.get("mom"),
            "period_summaries": analytics.get("period_summaries", []),
            "show_credits": any(p.get("credits") for p in points),
            **_sidebar_context("trends"),
        },
    )


@app.post("/generate")
async def generate_report(request: Request, month: str = Form(default=""), email: bool = Form(default=False)):
    _require_admin(request)
    cfg = _cfg()
    form = await request.form()
    clouds_raw = [c.strip().lower() for c in form.getlist("clouds") if c.strip()]
    selected = set(clouds_raw) if clouds_raw else None
    force = str(form.get("force", "")).lower() in {"1", "true", "on", "yes"}
    try:
        result = generate(
            cfg,
            month=month.strip() or None,
            clouds=selected,
            persist=True,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if email and not result.cached:
        try:
            send_email(
                cfg,
                result.pdf_bytes,
                f"cloud-cost-{result.month_key}.pdf",
                result.month_name,
                result.year,
                increases=result.report.key_increases,
                decreases=result.report.savings,
                currency=result.report.currency,
                mom=result.report.mom,
                summaries=[
                    {"cloud": s.cloud, "total": s.total, "subtotal": s.subtotal,
                     "credits": s.credits, "tax": s.tax}
                    for s in result.report.summaries
                ],
                curr_total=result.report.grand_total,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("email send failed: %s", exc)
    url = f"/reports/{result.month_key}"
    if result.cached:
        url += "?cached=1"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/reports/{month_key}/chart")
def api_chart(
    month_key: str,
    dimension: str = Query("service", pattern="^(service|subscription|resource_group)$"),
    cloud: str | None = None,
    subscription: str | None = None,
    resource_group: str | None = None,
    product_group: str | None = None,
    service: str | None = None,
    env_tier: str | None = Query(None, pattern="^(pd|dv|st|ut)$"),
):
    cfg = _cfg()
    report = _store().load(month_key)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    sub_map = subscription_map(cfg)
    filters = _filter_kwargs(cloud, subscription, resource_group, product_group, service, env_tier)
    records = filter_records(report.records, sub_map=sub_map, **filters)
    return {
        "month": month_key,
        "dimension": dimension,
        "filters": filters,
        "total": round(sum(line_total(r) for r in records), 2),
        "series": chart_by_dimension(records, dimension, sub_map),
        "summary": _cloud_summary(records),
    }


@app.get("/api/trend/filtered")
def api_trend_filtered(
    cloud: str | None = None,
    subscription: str | None = None,
    resource_group: str | None = None,
    product_group: str | None = None,
    service: str | None = None,
    env_tier: str | None = Query(None, pattern="^(pd|dv|st|ut)$"),
):
    cfg = _cfg()
    store = _store()
    sub_map = subscription_map(cfg)
    filters = _filter_kwargs(cloud, subscription, resource_group, product_group, service, env_tier)
    trend = monthly_trend(store.load_all_records(), filters, sub_map)
    return {"filters": filters, "trend": trend}


@app.get("/api/trend")
def api_trend() -> dict:
    """Per-month per-cloud totals (oldest first) for the historical trend view."""
    return _trend_payload(_store())


@app.get("/api/reports")
def api_reports() -> dict:
    return {"months": _store().list_months(), "meta": _store().list_meta()}


@app.get("/reports/{month_key}/pdf")
def download_pdf(month_key: str):
    pdf = _store().pdf_bytes(month_key)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF not found")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="cloud-cost-{month_key}.pdf"'},
    )


@app.get("/reports/{month_key}/csv")
def download_csv(month_key: str):
    from ..report import build_csv_bytes

    report = _store().load(month_key)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    csv_bytes = build_csv_bytes(report, _cfg())
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="cloud-cost-{month_key}.csv"'
        },
    )


# --------------------------------------------------------------------------- #
# Admin (user management + billing/invoice management) — admins only
# --------------------------------------------------------------------------- #
def _admin_context(request: Request, cfg: dict, *, error: str | None = None,
                   notice: str | None = None) -> dict:
    from ..azure_catalog import load_invoices_db

    azure_cfg = cfg.get("azure", {}) or {}
    credits_cfg = cfg.get("credits", {}) or {}
    invoices = load_invoices_db(cfg)
    invoice_rows = [{"month": m, **v} for m, v in sorted(invoices.items(), reverse=True)]
    return {
        "request": request,
        "user": current_user(request),
        "role": current_role(request),
        "error": error,
        "notice": notice,
        "users": users_mod.list_users(cfg),
        "roles": list(users_mod.ROLES),
        "invoices": invoice_rows,
        "billing_account": azure_cfg.get("billing_account_name") or "",
        "credits": {
            "enabled": bool(credits_cfg.get("enabled")),
            "current_balance": credits_cfg.get("current_balance"),
            "original_amount": credits_cfg.get("original_amount"),
            "expiry_date": credits_cfg.get("expiry_date"),
        },
        "default_month": latest_allowed_month_key(),
        **_sidebar_context("admin"),
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    _require_admin(request)
    return _TEMPLATES.TemplateResponse("admin.html", _admin_context(request, _cfg()))


@app.post("/admin/users")
def admin_add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
):
    _require_admin(request)
    cfg = _cfg()
    try:
        users_mod.add_user(cfg, username, password, role)
    except ValueError as exc:
        return _TEMPLATES.TemplateResponse(
            "admin.html", _admin_context(request, cfg, error=str(exc))
        )
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users/delete")
def admin_delete_user(request: Request, username: str = Form(...)):
    _require_admin(request)
    cfg = _cfg()
    if username == current_user(request):
        return _TEMPLATES.TemplateResponse(
            "admin.html",
            _admin_context(request, cfg, error="You cannot delete your own account."),
        )
    users_mod.delete_user(cfg, username)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/billing/invoice")
def admin_save_invoice(
    request: Request,
    month: str = Form(...),
    charges: float = Form(0.0),
    other_credits: float = Form(0.0),
    azure_credit: float = Form(0.0),
    tax: float = Form(0.0),
    invoice_number: str = Form(""),
):
    _require_admin(request)
    from ..azure_catalog import upsert_invoice

    cfg = _cfg()
    month = month.strip()
    subtotal = round(charges - other_credits - azure_credit, 2)
    total = round(subtotal + tax, 2)
    inv = {
        "invoice_number": invoice_number.strip(),
        "charges": charges,
        "other_credits": other_credits,
        "azure_credit": azure_credit,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }
    upsert_invoice(cfg, month, inv, source="manual")
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/billing/invoice/delete")
def admin_delete_invoice(request: Request, month: str = Form(...)):
    _require_admin(request)
    from ..azure_catalog import delete_invoice

    delete_invoice(_cfg(), month.strip())
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/billing/fetch")
def admin_fetch_invoice(request: Request, month: str = Form(...)):
    _require_admin(request)
    from ..collectors.azure_billing import fetch_and_store_invoice

    cfg = _cfg()
    summary = fetch_and_store_invoice(cfg, month.strip())
    if summary is None:
        return _TEMPLATES.TemplateResponse(
            "admin.html",
            _admin_context(
                request, cfg,
                error=f"Could not fetch invoice for {month} from the Billing API "
                      "(check billing_account_name, role, and that azure-mgmt-billing "
                      "is installed).",
            ),
        )
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
