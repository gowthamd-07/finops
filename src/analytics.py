"""Trend analytics: next-month projection, credits projection, period comparison.

All "spend" figures use the GROSS subtotal (sum of per-cloud subtotals, before
credits) because that is what burns down credits and what the source sheet's
"Overall Spend" totals reflect. Net "grand total" is projected separately.

`points` is a list of dicts ordered oldest -> newest, each:
    {"month": "YYYY-MM", "grand_total": float, "subtotal": float, "credits": float}
"""
from __future__ import annotations

from datetime import date
from statistics import mean


def _linreg(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n if n else 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _next_month_label(month_key: str) -> str:
    year, month = (int(x) for x in month_key.split("-"))
    nm = month + 1
    ny = year + (1 if nm > 12 else 0)
    nm = 1 if nm > 12 else nm
    return f"{ny:04d}-{nm:02d}"


def project_next_month(points: list[dict]) -> dict | None:
    """Forecast next month's spend from the trend (linear least squares)."""
    if not points:
        return None
    label = _next_month_label(points[-1]["month"])
    if len(points) < 2:
        return {
            "month": label,
            "grand_total": round(points[-1]["grand_total"], 2),
            "subtotal": round(points[-1]["subtotal"], 2),
            "method": "last-value",
            "trend_direction": "flat",
        }
    xs = list(range(len(points)))
    nxt = len(points)
    gt_s, gt_i = _linreg(xs, [p["grand_total"] for p in points])
    st_s, st_i = _linreg(xs, [p["subtotal"] for p in points])
    return {
        "month": label,
        "grand_total": round(max(0.0, gt_s * nxt + gt_i), 2),
        "subtotal": round(max(0.0, st_s * nxt + st_i), 2),
        "method": "linear-trend",
        "trend_direction": "up" if st_s >= 0 else "down",
    }


def month_over_month(points: list[dict]) -> dict | None:
    if len(points) < 2:
        return None
    prev, curr = points[-2], points[-1]
    delta = curr["grand_total"] - prev["grand_total"]
    pct = (delta / prev["grand_total"] * 100) if prev["grand_total"] else 0.0
    return {
        "prev_month": prev["month"],
        "curr_month": curr["month"],
        "prev": round(prev["grand_total"], 2),
        "curr": round(curr["grand_total"], 2),
        "delta": round(delta, 2),
        "pct": round(pct, 1),
        "direction": "up" if delta >= 0 else "down",
    }


def _window_summary(window: list[dict], label: str) -> dict | None:
    spends = [p["grand_total"] for p in window]
    if not spends:
        return None
    hi, lo = max(spends), min(spends)
    savings = hi - lo
    pct = (savings / hi * 100) if hi else 0.0
    return {
        "label": label,
        "months": len(window),
        "from": window[0]["month"],
        "to": window[-1]["month"],
        "highest": round(hi, 2),
        "lowest": round(lo, 2),
        "savings": round(savings, 2),
        "reduction_pct": round(pct, 1),
        "average": round(mean(spends), 2),
    }


def build_period_summaries(points: list[dict], baseline_month: str | None = None) -> list[dict]:
    """3/6/12-month + since-baseline summaries, deduped by covered range."""
    out: list[dict] = []
    seen_ranges: set[tuple[str, str]] = set()

    def _add(window: list[dict], label: str) -> None:
        if len(window) < 2:
            return
        rng = (window[0]["month"], window[-1]["month"])
        if rng in seen_ranges:
            return
        s = _window_summary(window, label)
        if s:
            seen_ranges.add(rng)
            out.append(s)

    for months, label in ((3, "3-month"), (6, "6-month"), (12, "1-year")):
        _add(points[-months:], label)
    if baseline_month:
        _add([p for p in points if p["month"] >= baseline_month], f"since {baseline_month}")
    return out


def credits_configured(cfg: dict) -> bool:
    """True when prepaid/EA credit tracking is enabled with a positive balance."""
    c = cfg.get("credits") or {}
    if not c.get("enabled"):
        return False
    return float(c.get("current_balance", 0) or 0) > 0


def credits_projection(cfg: dict, points: list[dict]) -> dict | None:
    """Compute prepaid-credit usage and how long the balance lasts.

    Invoice-only: each month the applied credits (the Azure Credit + other credits
    on the invoice) draw down the prepaid balance, so "credit usage" for a month
    equals exactly that month's invoiced credit. Months where the invoice applied
    no credit count as zero usage (gross spend is NOT treated as credit drawdown).
    """
    if not credits_configured(cfg):
        return None
    c = cfg.get("credits") or {}
    balance = float(c.get("current_balance", 0) or 0)
    window = int(c.get("burn_window_months", 6))
    window_points = points[-window:]

    def _usage(p: dict) -> float:
        return float(p.get("credits", 0) or 0)

    burns = [_usage(p) for p in window_points]
    if not burns:
        return None
    avg = mean(burns)
    this_month_usage = round(_usage(points[-1]), 2)
    used_window = round(sum(burns), 2)
    res: dict = {
        "current_balance": round(balance, 2),
        "this_month_usage": this_month_usage,
        "avg_monthly_burn": round(avg, 2),
        "window_months": len(burns),
        "credits_used_window": used_window,
        "months_remaining": round(balance / avg, 1) if avg > 0 else None,
        # Per-month usage (oldest -> newest) for the usage table/chart.
        "monthly_usage": [
            {"month": p["month"], "used": round(_usage(p), 2)} for p in window_points
        ],
    }
    original = float(c.get("original_amount", 0) or 0)
    if original > 0:
        res["original_amount"] = round(original, 2)
        res["credits_used_total"] = round(max(0.0, original - balance), 2)
        res["pct_used"] = round(100.0 * (original - balance) / original, 1)
    expiry = c.get("expiry_date")
    if expiry:
        expd = date.fromisoformat(expiry)
        months_to_expiry = max(0.0, (expd - date.today()).days / 30.44)
        res["expiry_date"] = expiry
        res["months_to_expiry"] = round(months_to_expiry, 1)
        projected_use = avg * months_to_expiry
        unused = balance - projected_use
        res["projected_usage_to_expiry"] = round(projected_use, 2)
        res["estimated_unused_at_expiry"] = round(max(0.0, unused), 2)
        res["estimated_overrun_at_expiry"] = round(max(0.0, -unused), 2)
        res["status"] = "under-utilizing" if unused > 0 else "fully-utilizing"
    return res


def anthropic_credits_configured(cfg: dict) -> bool:
    """True when prepaid Anthropic (Claude) credit tracking is enabled with a figure."""
    c = (cfg.get("anthropic") or {}).get("credits") or {}
    if not c.get("enabled"):
        return False
    return (
        float(c.get("current_balance", 0) or 0) > 0
        or float(c.get("original_amount", 0) or 0) > 0
    )


def anthropic_credits_projection(cfg: dict, points: list[dict]) -> dict | None:
    """Prepaid Anthropic (Claude) credit grants and how long the balance lasts.

    Anthropic exposes no balance API, so the grant total / remaining balance come
    from ``anthropic.credits`` in config. Unlike Azure EA credits (which are
    applied on the invoice), prepaid Anthropic credits are drawn down by Claude
    *usage*, so each month's Claude net spend is that month's credit drawdown.
    Requires per-cloud totals on each point (``point["clouds"]["Claude"]``).
    """
    if not anthropic_credits_configured(cfg):
        return None
    c = (cfg.get("anthropic") or {}).get("credits") or {}
    balance = float(c.get("current_balance", 0) or 0)
    window = int(c.get("burn_window_months", 6))
    window_points = points[-window:]

    def _usage(p: dict) -> float:
        return float((p.get("clouds") or {}).get("Claude", 0) or 0)

    burns = [_usage(p) for p in window_points]
    if not burns:
        return None
    avg = mean(burns)
    res: dict = {
        "current_balance": round(balance, 2),
        "this_month_usage": round(_usage(points[-1]), 2),
        "avg_monthly_burn": round(avg, 2),
        "window_months": len(burns),
        "credits_used_window": round(sum(burns), 2),
        "months_remaining": round(balance / avg, 1) if avg > 0 else None,
        "monthly_usage": [
            {"month": p["month"], "used": round(_usage(p), 2)} for p in window_points
        ],
    }
    original = float(c.get("original_amount", 0) or 0)
    if original > 0:
        res["original_amount"] = round(original, 2)
        res["credits_used_total"] = round(max(0.0, original - balance), 2)
        res["pct_used"] = round(100.0 * (original - balance) / original, 1)
    expiry = c.get("expiry_date")
    if expiry:
        expd = date.fromisoformat(expiry)
        months_to_expiry = max(0.0, (expd - date.today()).days / 30.44)
        res["expiry_date"] = expiry
        res["months_to_expiry"] = round(months_to_expiry, 1)
        projected_use = avg * months_to_expiry
        unused = balance - projected_use
        res["projected_usage_to_expiry"] = round(projected_use, 2)
        res["estimated_unused_at_expiry"] = round(max(0.0, unused), 2)
        res["estimated_overrun_at_expiry"] = round(max(0.0, -unused), 2)
        res["status"] = "under-utilizing" if unused > 0 else "fully-utilizing"
    return res


def build_analytics(cfg: dict, points: list[dict]) -> dict:
    baseline = (cfg.get("analytics") or {}).get("baseline_month")
    return {
        "projection": project_next_month(points),
        "mom": month_over_month(points),
        "period_summaries": build_period_summaries(points, baseline),
        "credits_projection": credits_projection(cfg, points),
        "anthropic_credits": anthropic_credits_projection(cfg, points),
    }
