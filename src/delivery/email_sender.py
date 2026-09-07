"""Send the PDF report via Azure Communication Services (ACS) Email.

Auth (in order of preference):
  1. Connection string - env ACS_CONNECTION_STRING or config email.connection_string
     (the access key lives in Key Vault / .env, never in config.yaml).
  2. Passwordless - config email.endpoint (or env ACS_ENDPOINT) + Managed
     Identity / Workload Identity via DefaultAzureCredential.

Config (config.yaml `email`):
  enabled: true
  sender: "DoNotReply@<verified-domain>.azurecomm.net"
  recipients: ["finops@example.com", ...]   # receiver email(s)
  connection_string: "${ACS_CONNECTION_STRING}"   # optional
  endpoint: "${ACS_ENDPOINT}"                      # optional (passwordless)
  subject_template: "Monthly Cloud Infrastructure Cost - {month} {year}"
"""
from __future__ import annotations

import base64
import calendar
import logging
import os

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def email_enabled(cfg: dict) -> bool:
    """Whether email delivery runs.

    Precedence: the EMAIL_ENABLED env var (if set) wins, then the config
    `email.enabled` value, then defaults to True.
    """
    env_val = os.environ.get("EMAIL_ENABLED")
    if env_val not in (None, ""):
        return env_val.strip().lower() in _TRUE
    val = cfg.get("email", {}).get("enabled", True)
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return True


def _build_client(email_cfg: dict):
    """Create an ACS EmailClient from a connection string or endpoint+identity."""
    from azure.communication.email import EmailClient

    conn = os.environ.get("ACS_CONNECTION_STRING") or email_cfg.get("connection_string")
    if conn:
        return EmailClient.from_connection_string(conn)

    endpoint = os.environ.get("ACS_ENDPOINT") or email_cfg.get("endpoint")
    if endpoint:
        from azure.identity import DefaultAzureCredential

        return EmailClient(endpoint, DefaultAzureCredential())

    raise RuntimeError(
        "Azure Communication Services not configured: set ACS_CONNECTION_STRING "
        "(or email.connection_string), or email.endpoint for passwordless auth"
    )


def _recipients(email_cfg: dict) -> list[str]:
    raw = email_cfg.get("recipients", [])
    if isinstance(raw, str):
        raw = [raw]
    return [r.strip() for r in raw if r and str(r).strip()]


def _row_context(row: dict) -> str:
    """Cloud + environment + resource suffix for a change row, omitting blanks."""
    parts = [p for p in (row.get("cloud"), row.get("environment"), row.get("resource")) if p]
    return " · ".join(parts)


def _month_label(month_key: str | None) -> str:
    """'2026-05' -> 'May 2026'; passthrough for anything unexpected."""
    if not month_key or "-" not in str(month_key):
        return str(month_key or "")
    try:
        y, m = str(month_key).split("-")[:2]
        return f"{calendar.month_name[int(m)]} {y}"
    except (ValueError, IndexError):
        return str(month_key)


def _clouds_phrase(summaries: list[dict] | None) -> str:
    """Human list of clouds present in the report, e.g. 'AWS, Azure and GCP'."""
    seen: list[str] = []
    for s in summaries or []:
        name = s.get("cloud")
        if name and name not in seen:
            seen.append(name)
    if not seen:
        return "AWS, Azure and GCP"
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen[:-1]) + " and " + seen[-1]


def _cost_summary_text(mom: dict | None, curr_total: float | None, currency: str) -> str:
    if mom:
        up = mom.get("delta", 0) >= 0
        arrow, sign = ("\u2191", "+") if up else ("\u2193", "-")
        return "\n".join([
            "",
            "Cost summary",
            f"  {_month_label(mom.get('prev_month'))} cost:   {currency} {mom.get('prev', 0):,.2f}",
            f"  {_month_label(mom.get('curr_month'))} cost:   {currency} {mom.get('curr', 0):,.2f}",
            f"  Variance:   {sign} {currency} {abs(mom.get('delta', 0)):,.2f} "
            f"({arrow} {abs(mom.get('pct', 0)):.1f}%)",
        ])
    if curr_total is not None:
        return "\n".join(["", "Cost summary", f"  Current cost:   {currency} {curr_total:,.2f}"])
    return ""


def _by_cloud_text(summaries: list[dict] | None, currency: str) -> str:
    if not summaries:
        return ""
    lines = ["", "Cost by cloud"]
    for s in summaries:
        lines.append(f"  {str(s.get('cloud', '')):<7} {currency} {s.get('total', 0):,.2f}")
    return "\n".join(lines)


def _highlights_text(
    increases: list[dict], decreases: list[dict], mom: dict | None, currency: str
) -> str:
    bullets: list[str] = []
    if mom:
        up = mom.get("delta", 0) >= 0
        bullets.append(
            f"Total spend {'rose' if up else 'fell'} {abs(mom.get('pct', 0)):.1f}% "
            f"({'+' if up else '-'} {currency} {abs(mom.get('delta', 0)):,.2f}) "
            f"vs {_month_label(mom.get('prev_month'))}."
        )
    if increases:
        r = increases[0]
        ctx = _row_context(r)
        bullets.append(
            f"Largest increase: {r.get('service', '')}"
            + (f" [{ctx}]" if ctx else "")
            + f"  + {currency} {r.get('amount', 0):,.2f}."
        )
    if decreases:
        r = decreases[0]
        ctx = _row_context(r)
        bullets.append(
            f"Largest decrease: {r.get('service', '')}"
            + (f" [{ctx}]" if ctx else "")
            + f"  - {currency} {r.get('amount', 0):,.2f}."
        )
    if not bullets:
        return ""
    return "\n".join(["", "Key highlights"] + [f"  \u2022 {b}" for b in bullets])


_CLOUD_COLORS = {"AWS": "#ff9900", "Azure": "#0078d4", "GCP": "#1a73e8"}
_UP_FG, _UP_BG = "#c0392b", "#fdecec"
_DOWN_FG, _DOWN_BG = "#0e8a44", "#e9f9f1"
_FONT = "Arial,Helvetica,sans-serif"


def _esc(v) -> str:
    return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge(cloud) -> str:
    c = _CLOUD_COLORS.get(str(cloud), "#6b7280")
    return (
        f'<span style="display:inline-block;padding:3px 9px;border-radius:6px;'
        f'background:{c};color:#ffffff;font:700 11px {_FONT};letter-spacing:.02em">'
        f"{_esc(cloud)}</span>"
    )


def _section_title(text: str) -> str:
    return (
        f'<div style="font:700 12px {_FONT};letter-spacing:.06em;text-transform:uppercase;'
        f'color:#8a94a6;margin:0 0 12px">{_esc(text)}</div>'
    )


def _html_email(
    month: str,
    year: int,
    mom: dict | None,
    curr_total: float | None,
    summaries: list[dict] | None,
    increases: list[dict],
    decreases: list[dict],
    currency: str,
) -> str:
    # Header band.
    header = (
        '<tr><td style="padding:26px 32px;background:#12a150;'
        'background:linear-gradient(135deg,#16a34a 0%,#0e8a44 100%)">'
        f'<div style="font:700 12px {_FONT};letter-spacing:.18em;text-transform:uppercase;'
        'color:rgba(255,255,255,.85)">FinOps &middot; Example</div>'
        f'<div style="font:800 22px {_FONT};color:#ffffff;margin-top:6px">'
        f"{_esc(month)} {year} Cloud Cost Report</div>"
        f'<div style="font:400 13px {_FONT};color:rgba(255,255,255,.92);margin-top:3px">'
        f"Monthly Cloud Infrastructure Cost &middot; {_esc(_clouds_phrase(summaries))}</div>"
        "</td></tr>"
    )

    # Hero: current total + variance pill.
    if mom:
        up = mom.get("delta", 0) >= 0
        arrow = "&#9650;" if up else "&#9660;"
        sign = "+" if up else "&minus;"
        pill_fg, pill_bg = (_UP_FG, _UP_BG) if up else (_DOWN_FG, _DOWN_BG)
        curr_val = mom.get("curr", curr_total or 0)
        pill = (
            f'<span style="display:inline-block;padding:5px 13px;border-radius:999px;'
            f'background:{pill_bg};color:{pill_fg};font:700 13px {_FONT};white-space:nowrap">'
            f"{sign} {currency} {abs(mom.get('delta', 0)):,.2f} "
            f"({arrow} {abs(mom.get('pct', 0)):.1f}%)</span>"
        )
        prev_line = (
            f'<div style="font:400 13px {_FONT};color:#8a94a6;margin-top:12px">'
            f"{_esc(_month_label(mom.get('prev_month')))}: "
            f'<span style="color:#4b5563;font-weight:600">{currency} '
            f"{mom.get('prev', 0):,.2f}</span></div>"
        )
    else:
        curr_val = curr_total or 0
        pill = prev_line = ""
    hero = (
        '<tr><td style="padding:26px 32px 4px">'
        f'<div style="font:600 11px {_FONT};letter-spacing:.08em;text-transform:uppercase;'
        f'color:#9ca3af">{_esc(month)} {year} total cost</div>'
        f'<div style="font:800 34px {_FONT};color:#0f172a;margin:8px 0 12px">'
        f"{currency} {curr_val:,.2f}</div>{pill}{prev_line}</td></tr>"
    )

    # Cost by cloud with proportional bars.
    by_cloud = ""
    if summaries:
        max_total = max((s.get("total", 0) for s in summaries), default=0) or 1
        rows = ""
        for s in summaries:
            col = _CLOUD_COLORS.get(str(s.get("cloud")), "#6b7280")
            width = max(4, int(round((s.get("total", 0) / max_total) * 100)))
            rows += (
                "<tr>"
                f'<td style="padding:9px 0;width:64px;vertical-align:middle">'
                f"{_badge(s.get('cloud'))}</td>"
                '<td style="padding:9px 12px;vertical-align:middle">'
                '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
                'style="border-collapse:collapse"><tr>'
                '<td style="background:#eef1f4;border-radius:6px;height:8px;padding:0;font-size:0;'
                'line-height:0">'
                '<table cellpadding="0" cellspacing="0" role="presentation" '
                f'style="border-collapse:collapse;width:{width}%"><tr>'
                f'<td style="background:{col};border-radius:6px;height:8px;font-size:0;'
                'line-height:0">&nbsp;</td></tr></table></td></tr></table></td>'
                f'<td style="padding:9px 0;text-align:right;font:700 14px {_FONT};color:#0f172a;'
                f'white-space:nowrap">{currency} {s.get("total", 0):,.2f}</td>'
                "</tr>"
            )
        by_cloud = (
            '<tr><td style="padding:22px 32px 6px">'
            + _section_title("Cost by cloud")
            + '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            f'style="border-collapse:collapse">{rows}</table></td></tr>'
        )

    # Key highlights.
    hi: list[str] = []
    if mom:
        up = mom.get("delta", 0) >= 0
        verb, sign, color = ("rose", "+", _UP_FG) if up else ("fell", "&minus;", _DOWN_FG)
        hi.append(
            f"Total spend <b>{verb} {abs(mom.get('pct', 0)):.1f}%</b> "
            f'(<span style="color:{color};font-weight:700">{sign} {currency} '
            f"{abs(mom.get('delta', 0)):,.2f}</span>) vs "
            f"{_esc(_month_label(mom.get('prev_month')))}."
        )
    if increases:
        r = increases[0]
        ctx = _row_context(r)
        hi.append(
            f"Largest increase: <b>{_esc(r.get('service'))}</b>"
            + (f' <span style="color:#9ca3af">[{_esc(ctx)}]</span>' if ctx else "")
            + f' <span style="color:{_UP_FG};font-weight:700">+ {currency} '
            f"{r.get('amount', 0):,.2f}</span>"
        )
    if decreases:
        r = decreases[0]
        ctx = _row_context(r)
        hi.append(
            f"Largest decrease: <b>{_esc(r.get('service'))}</b>"
            + (f' <span style="color:#9ca3af">[{_esc(ctx)}]</span>' if ctx else "")
            + f' <span style="color:{_DOWN_FG};font-weight:700">&minus; {currency} '
            f"{r.get('amount', 0):,.2f}</span>"
        )
    highlights = ""
    if hi:
        li = "".join(
            f'<tr><td style="padding:5px 0;vertical-align:top;width:16px;color:#12a150;'
            f'font:700 14px {_FONT}">&bull;</td>'
            f'<td style="padding:5px 0;font:400 13px {_FONT};color:#374151;line-height:1.5">'
            f"{it}</td></tr>"
            for it in hi
        )
        highlights = (
            '<tr><td style="padding:22px 32px 6px">'
            + _section_title("Key highlights")
            + '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-collapse:collapse;background:#f7fafc;border:1px solid #eef0f2;'
            'border-left:3px solid #12a150;border-radius:8px">'
            '<tr><td style="padding:8px 16px">'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            f'style="border-collapse:collapse">{li}</table></td></tr></table></td></tr>'
        )

    # Change tables.
    def change_table(title: str, rows: list[dict], positive: bool) -> str:
        if not rows:
            return ""
        color = _UP_FG if positive else _DOWN_FG
        sign = "+" if positive else "&minus;"
        th = (
            f"font:700 11px {_FONT};letter-spacing:.04em;text-transform:uppercase;color:#8a94a6;"
            "padding:9px 12px;border-bottom:2px solid #e9ecf1;text-align:left"
        )
        body = ""
        for i, r in enumerate(rows):
            bg = "#ffffff" if i % 2 == 0 else "#fafbfc"
            body += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:9px 12px;border-bottom:1px solid #f0f1f3;font:600 13px {_FONT};'
                f'color:#111827">{_esc(r.get("service"))}</td>'
                f'<td style="padding:9px 12px;border-bottom:1px solid #f0f1f3;font:400 12px {_FONT};'
                f'color:#6b7280">{_esc(r.get("environment"))}</td>'
                f'<td style="padding:9px 12px;border-bottom:1px solid #f0f1f3">'
                f"{_badge(r.get('cloud'))}</td>"
                f'<td style="padding:9px 12px;border-bottom:1px solid #f0f1f3;font:400 12px {_FONT};'
                f'color:#9ca3af">{_esc(r.get("resource"))}</td>'
                f'<td style="padding:9px 12px;border-bottom:1px solid #f0f1f3;text-align:right;'
                f'white-space:nowrap;font:700 13px {_FONT};color:{color}">'
                f"{sign} {currency} {r.get('amount', 0):,.2f}</td></tr>"
            )
        return (
            f'<div style="font:700 12px {_FONT};color:{color};margin:16px 0 8px">{title}</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-collapse:collapse;border:1px solid #eef0f2;border-radius:8px">'
            f'<tr><th style="{th}">Service</th><th style="{th}">Environment</th>'
            f'<th style="{th}">Cloud</th><th style="{th}">Resource</th>'
            f'<th style="{th};text-align:right">Change</th></tr>{body}</table>'
        )

    changes = ""
    if increases or decreases:
        changes = (
            '<tr><td style="padding:22px 32px 6px">'
            + _section_title("Cost changes vs the previous month")
            + change_table("Increases", increases, True)
            + change_table("Decreases", decreases, False)
            + "</td></tr>"
        )

    footer = (
        '<tr><td style="padding:22px 32px 28px;border-top:1px solid #eef0f2">'
        f'<div style="font:400 12px {_FONT};color:#9ca3af">The full breakdown is attached as a '
        "PDF. Generated automatically by FinOps.</div></td></tr>"
    )

    return (
        f'<div style="margin:0;padding:24px 12px;background:#eef1f4;font-family:{_FONT}">'
        '<table align="center" width="640" cellpadding="0" cellspacing="0" role="presentation" '
        'style="border-collapse:collapse;max-width:640px;width:100%;margin:0 auto;'
        'background:#ffffff;border-radius:14px;overflow:hidden;'
        'box-shadow:0 1px 3px rgba(15,23,42,.10)">'
        f"{header}{hero}{by_cloud}{highlights}{changes}{footer}"
        "</table></div>"
    )


def _summary_text(increases: list[dict], decreases: list[dict], currency: str) -> str:
    if not increases and not decreases:
        return ""

    def block(title: str, rows: list[dict], sign: str) -> list[str]:
        lines = [f"{title}:"]
        if not rows:
            lines.append("  (none)")
            return lines
        for r in rows:
            ctx = _row_context(r)
            ctx = f"  [{ctx}]" if ctx else ""
            lines.append(
                f"  {sign} {currency} {r.get('amount', 0):,.2f}  "
                f"{r.get('service', '')}{ctx}"
            )
        return lines

    out = ["", "Cost changes vs the previous month", ""]
    out += block("Increases", increases, "+")
    out.append("")
    out += block("Decreases", decreases, "-")
    return "\n".join(out)


def send_email(
    cfg: dict,
    pdf_bytes: bytes,
    filename: str,
    month: str,
    year: int,
    *,
    increases: list[dict] | None = None,
    decreases: list[dict] | None = None,
    currency: str = "USD",
    mom: dict | None = None,
    summaries: list[dict] | None = None,
    curr_total: float | None = None,
) -> None:
    if not email_enabled(cfg):
        log.info("email: disabled via EMAIL_ENABLED/config; skipping send")
        return

    email_cfg = cfg.get("email", {})
    recipients = _recipients(email_cfg)
    if not recipients:
        log.warning("email: no recipients configured, skipping send")
        return

    sender = email_cfg.get("sender")
    if not sender:
        log.warning("email: no sender configured, skipping send")
        return

    subject = email_cfg.get(
        "subject_template", "Monthly Cloud Infrastructure Cost - {month} {year}"
    ).format(month=month, year=year)

    increases = increases or []
    decreases = decreases or []

    intro = (
        f"Attached is the {month} {year} cloud infrastructure cost report "
        f"covering {_clouds_phrase(summaries)}."
    )

    # Plain-text body: intro -> cost summary -> by-cloud -> highlights -> change tables.
    cost_summary_text = _cost_summary_text(mom, curr_total, currency)
    by_cloud_text = _by_cloud_text(summaries, currency)
    highlights_text = _highlights_text(increases, decreases, mom, currency)
    changes_text = _summary_text(increases, decreases, currency)

    body = intro
    for section in (cost_summary_text, by_cloud_text, highlights_text, changes_text):
        if section:
            body += "\n" + section
    body += "\n\nGenerated automatically."

    # Branded HTML body (falls back to plainText in clients that block HTML).
    html = _html_email(month, year, mom, curr_total, summaries, increases, decreases, currency)

    content = {"subject": subject, "plainText": body, "html": html}

    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": addr} for addr in recipients]},
        "content": content,
        "attachments": [
            {
                "name": filename,
                "contentType": "application/pdf",
                "contentInBase64": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        ],
    }

    client = _build_client(email_cfg)
    poller = client.begin_send(message)
    result = poller.result()
    message_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
    log.info("email: sent report via ACS (messageId=%s) to %s", message_id, recipients)
