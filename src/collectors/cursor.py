"""Cursor collector — LLM/coding-assistant spend (read-only).

Source: the Cursor Admin API ``POST /teams/filtered-usage-events`` endpoint.
Summing the ``chargedCents`` field across events reconciles with the
``/teams/spend`` dashboard totals, and — unlike ``/teams/spend`` which only
reports the *current* billing cycle — the filtered-usage-events endpoint accepts
an explicit ``startDate`` / ``endDate`` window, so we can pull an exact window.

By default that window is the calendar month (to match the rest of the report).
Cursor, however, invoices on its own billing cycle (e.g. "cycle starting the
27th"), so setting ``cursor.billing_cycle_day`` pulls the spend on that cycle
instead — making the reported Cursor figure reconcile with Cursor's invoices.

Auth: HTTP Basic with the Cursor Team/Admin API key as the username and an empty
password (``-u YOUR_API_KEY:``). Create the key in Cursor > Dashboard > Settings
> Cursor Admin API keys. Provide it via the CURSOR_API_KEY env var (locally in
.env, in production from Key Vault). This is a *team admin* key, distinct from
the per-developer API keys used to actually call models.

Spend is grouped per model into one CostRecord each (``service`` = model name),
so the report shows which models drive the cost.

Billing mode: ``chargedCents`` is the *list value* of all usage and overshoots the
invoice, because Cursor only bills the overage beyond each seat's included
allotment. With ``cursor.billing_mode: billed`` the collector derives a billed/list
factor from the current cycle (``/teams/spend`` billed spend ÷ same-window list
value) and scales the per-model usage by it, so the reported total matches the
invoice while the model/user breakdown is preserved. ``billing_mode: list`` reports
the raw list value instead. The factor rises through a cycle (the fixed included
allotment is consumed early), so it is most accurate late in the cycle.

Seat subscription: when ``cursor.seats.enabled`` is set, an extra line is added
for the Cursor Teams seat subscription. The seat *count* is pulled live from the
Admin API ``GET /teams/members`` (active members only — ``removed`` users are
excluded); the per-seat price and tax rate are contract constants from config
(``seat_price`` / ``tax_rate``), since the API does not expose them. Bugbot is a
separate add-on the members API does not report, so its seat count comes from
``cursor.seats.bugbot_seats``. Each seat line carries its base in ``cost`` and the
sales tax in ``tax``. Model usage itself remains untaxed (the API returns pre-tax
``chargedCents``); only the seat lines carry tax.
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, timezone

import requests

from ..models import CostRecord

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.cursor.com/teams/filtered-usage-events"
_MEMBERS_ENDPOINT = "https://api.cursor.com/teams/members"
_SPEND_ENDPOINT = "https://api.cursor.com/teams/spend"
_PAGE_SIZE = 1000          # endpoint maximum
_MAX_PAGES = 1000          # safety cap (≤ 1M events / month)
_TIMEOUT = 60              # seconds per request
_INACTIVE_ROLES = frozenset({"removed"})   # not billable seats


def _api_key(cursor_cfg: dict) -> str | None:
    import os

    key = (os.environ.get("CURSOR_API_KEY") or cursor_cfg.get("api_key") or "").strip()
    return key or None


def _epoch_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _clamp_day(year: int, month: int, day: int) -> date:
    """Anchor day, clamped to the last valid day of the month (e.g. Feb)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _billing_window(cursor_cfg: dict, start: date, end: date) -> tuple[date, date]:
    """Resolve the collection window for the report month.

    By default this is the calendar month ``[start, end)`` passed in. When
    ``billing_cycle_day`` is configured, Cursor's spend is pulled on its own
    invoice cycle instead — the cycle *ending* in the report month, anchored on
    that day. Cursor labels its invoices "cycle starting <prev-month> <day>", so
    for a report month M the window is ``[(M-1)/day, M/day)`` (end exclusive),
    matching what Cursor bills during month M.
    """
    day = cursor_cfg.get("billing_cycle_day")
    if not day:
        return start, end
    day = int(day)
    cycle_end = _clamp_day(start.year, start.month, day)      # exclusive
    py, pm = (start.year, start.month - 1)
    if pm < 1:
        pm, py = 12, py - 1
    cycle_start = _clamp_day(py, pm, day)
    return cycle_start, cycle_end


def _active_seat_count(session: requests.Session) -> int | None:
    """Live count of billable Cursor Teams members (excludes 'removed')."""
    try:
        resp = session.get(_MEMBERS_ENDPOINT, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        log.exception("Cursor: /teams/members request failed")
        return None

    members = data.get("teamMembers") if isinstance(data, dict) else data
    if members is None and isinstance(data, dict):
        members = data.get("members")
    if not isinstance(members, list):
        log.warning("Cursor: unexpected /teams/members shape; skipping seat line")
        return None

    def _active(m: dict) -> bool:
        # Members carry an explicit `isRemoved` flag; also guard the `role` string.
        if m.get("isRemoved"):
            return False
        return (m.get("role") or "").strip().lower() not in _INACTIVE_ROLES

    return sum(1 for m in members if isinstance(m, dict) and _active(m))


def _seat_records(
    cursor_cfg: dict,
    session: requests.Session,
    environment: str,
    start: date,
    end: date,
) -> list[CostRecord]:
    """Seat subscription lines: (active members + Bugbot seats) x price + tax.

    The seat count is live from the Admin API; ``seat_price`` / ``tax_rate`` /
    ``bugbot_seats`` are config (invoice/contract constants the API can't give).
    """
    seats_cfg = cursor_cfg.get("seats") or {}
    if not seats_cfg.get("enabled"):
        return []

    seat_price = float(seats_cfg.get("seat_price") or 0)
    tax_rate = float(seats_cfg.get("tax_rate") or 0) / 100.0
    bugbot_seats = int(seats_cfg.get("bugbot_seats") or 0)
    if seat_price <= 0:
        return []

    active = _active_seat_count(session)
    if active is None:
        return []

    def _line(service: str, seats: int) -> CostRecord:
        base = round(seats * seat_price, 2)
        return CostRecord(
            cloud="Cursor",
            service=service,
            cost=base,
            tax=round(base * tax_rate, 2),
            environment=environment,
            account="Cursor Team",
            period_start=start,
            period_end=end,
            tokens=float(seats),
            usage_unit="seats",
        )

    records: list[CostRecord] = []
    if active > 0:
        records.append(_line(f"Cursor Teams ({active} seats)", active))
    if bugbot_seats > 0:
        records.append(_line(f"Bugbot ({bugbot_seats} seats)", bugbot_seats))

    log.info(
        "Cursor: seat lines active=%d bugbot=%d @ $%.2f + %.1f%% tax",
        active, bugbot_seats, seat_price, tax_rate * 100,
    )
    return records


def _sum_charged_cents(session: requests.Session, start_ms: int, end_ms: int) -> float | None:
    """Sum ``chargedCents`` (list value of usage) over an epoch-ms window."""
    total = 0.0
    page = 1
    while page <= _MAX_PAGES:
        try:
            resp = session.post(
                _ENDPOINT,
                json={"startDate": start_ms, "endDate": end_ms, "page": page, "pageSize": _PAGE_SIZE},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            log.exception("Cursor: usage window page %d failed", page)
            return None
        for ev in data.get("usageEvents", []) or []:
            c = ev.get("chargedCents")
            if c is not None:
                total += float(c)
        if not (data.get("pagination") or {}).get("hasNextPage"):
            break
        page += 1
    return total


def _current_cycle_billed(session: requests.Session) -> dict | None:
    """Billed spend, list value and members for the *current* billing cycle.

    Cursor bills the overage beyond each seat's included allotment. ``/teams/spend``
    exposes that billed spend (``spendCents``) for the current cycle only, and it
    resets when the cycle rolls — so this must be captured while the cycle is live.
    Returns ``{"cycle_start": ms, "billed_cents", "list_cents", "members"}`` or
    ``None`` if unavailable.
    """
    try:
        resp = session.post(_SPEND_ENDPOINT, json={"page": 1, "pageSize": 100}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        log.exception("Cursor: /teams/spend request failed")
        return None

    cycle_start = int(data.get("subscriptionCycleStart") or 0)
    if cycle_start <= 0:
        log.warning("Cursor: /teams/spend has no cycle start")
        return None

    def _page_spend(d: dict) -> tuple[float, int]:
        rows = d.get("teamMemberSpend") or []
        return sum(float(x.get("spendCents") or 0) for x in rows), len(rows)

    billed, members = _page_spend(data)
    for page in range(2, int(data.get("totalPages") or 1) + 1):
        try:
            dp = session.post(_SPEND_ENDPOINT, json={"page": page, "pageSize": 100}, timeout=_TIMEOUT)
            dp.raise_for_status()
            b, m = _page_spend(dp.json())
            billed += b
            members += m
        except (requests.RequestException, ValueError):
            log.exception("Cursor: /teams/spend page %d failed", page)
            return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    list_value = _sum_charged_cents(session, cycle_start, now_ms)
    if list_value is None:
        return None
    return {
        "cycle_start": cycle_start,
        "billed_cents": billed,
        "list_cents": list_value,
        "members": members,
    }


def snapshot_cursor_billed_spend(cfg: dict):
    """Capture the current cycle's billed spend into the store (run daily).

    The monthly report reads these snapshots so it can value usage at the billed
    (invoice) amount even after the cycle has rolled over. Returns the saved
    snapshot dict, or ``None`` if the API/DB is unavailable.
    """
    from ..store import CursorSpendStore

    cursor_cfg = cfg.get("cursor", {})
    key = _api_key(cursor_cfg)
    if not key:
        log.info("Cursor: no CURSOR_API_KEY; skipping billed-spend snapshot")
        return None

    session = requests.Session()
    session.auth = (key, "")
    cyc = _current_cycle_billed(session)
    if not cyc:
        return None

    cycle_start = datetime.fromtimestamp(cyc["cycle_start"] / 1000, timezone.utc).date()
    # Anchor the cycle end one month on (same day-of-month, clamped).
    ny, nm = (cycle_start.year, cycle_start.month + 1)
    if nm > 12:
        nm, ny = 1, ny + 1
    cycle_end = _clamp_day(ny, nm, cycle_start.day)

    try:
        CursorSpendStore(cfg).save_snapshot(
            cycle_start, cycle_end, cyc["billed_cents"], cyc["list_cents"], cyc["members"]
        )
    except Exception:  # noqa: BLE001 - DB may be unconfigured in some contexts
        log.exception("Cursor: failed to persist billed-spend snapshot")
        return None

    log.info(
        "Cursor: snapshot cycle %s billed=$%.2f list=$%.2f members=%d",
        cycle_start, cyc["billed_cents"] / 100, cyc["list_cents"] / 100, cyc["members"],
    )
    return {**cyc, "cycle_start_date": cycle_start, "cycle_end": cycle_end}


def _resolve_billed_factor(
    cfg: dict, session: requests.Session, cycle_start: date, list_cents: float
) -> float:
    """Billed/list factor for the report cycle: stored snapshot first, else live.

    Prefers the persisted snapshot for this cycle (captured near its end), scaling
    so the reported total equals the snapshot's billed spend. If there is no
    snapshot, only uses a live factor when the report cycle IS Cursor's current
    cycle (the only cycle /teams/spend describes); otherwise reports list value
    (factor 1.0) rather than mis-applying another cycle's factor.
    """
    if list_cents <= 0:
        return 1.0

    try:
        from ..store import CursorSpendStore

        snap = CursorSpendStore(cfg).get_snapshot(cycle_start)
    except Exception:  # noqa: BLE001 - DB may be unconfigured
        snap = None
    if snap and snap.get("billed_cents"):
        factor = snap["billed_cents"] / list_cents
        log.info(
            "Cursor: billed factor=%.4f from snapshot (billed=$%.2f / list=$%.2f)",
            factor, snap["billed_cents"] / 100, list_cents / 100,
        )
        return factor

    # No snapshot: only the live current cycle is reconstructable from /teams/spend.
    cyc = _current_cycle_billed(session)
    if cyc and cyc["list_cents"] > 0:
        live_start = datetime.fromtimestamp(cyc["cycle_start"] / 1000, timezone.utc).date()
        if live_start == cycle_start:
            factor = cyc["billed_cents"] / list_cents
            log.info(
                "Cursor: live billed factor=%.4f (billed=$%.2f / list=$%.2f, cycle-to-date)",
                factor, cyc["billed_cents"] / 100, list_cents / 100,
            )
            return factor
        log.warning(
            "Cursor: no snapshot for cycle %s (current cycle is %s); reporting list value",
            cycle_start, live_start,
        )
    else:
        log.warning("Cursor: no billed snapshot or live spend; reporting list value")
    return 1.0


def collect_cursor(cfg: dict, start: date, end: date) -> list[CostRecord]:
    """Return per-model Cursor spend for the report month.

    The window defaults to the calendar month ``[start, end)`` but follows
    Cursor's own invoice cycle when ``cursor.billing_cycle_day`` is set (see
    ``_billing_window``), so the reported spend lines up with Cursor's invoices.
    """
    cursor_cfg = cfg.get("cursor", {})
    key = _api_key(cursor_cfg)
    if not key:
        log.info("Cursor: no CURSOR_API_KEY configured; skipping")
        return []

    environment = cursor_cfg.get("environment", "production")
    # Resolve calendar-month vs billing-cycle window for this report month.
    start, end = _billing_window(cursor_cfg, start, end)
    # startDate / endDate are inclusive epoch-ms bounds. `end` is the exclusive
    # first-of-next-cycle, so the last billable ms of the window is end - 1ms.
    start_ms = _epoch_ms(datetime(start.year, start.month, start.day))
    end_ms = _epoch_ms(datetime(end.year, end.month, end.day)) - 1

    # Aggregate per (model, user): Cursor attributes each event to a user email
    # or a service account, so we can report per-model spend, tokens, and a
    # per-user (top spenders) breakdown.
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"cents": 0.0, "tokens": 0.0})
    total_events = 0
    session = requests.Session()
    session.auth = (key, "")

    page = 1
    while page <= _MAX_PAGES:
        payload = {
            "startDate": start_ms,
            "endDate": end_ms,
            "page": page,
            "pageSize": _PAGE_SIZE,
        }
        try:
            resp = session.post(_ENDPOINT, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException:
            log.exception("Cursor: request for page %d failed", page)
            break

        data = resp.json()
        events = data.get("usageEvents", []) or []
        for ev in events:
            total_events += 1
            charged = ev.get("chargedCents")
            if charged is None:
                continue
            model = (ev.get("model") or "Unknown model").strip() or "Unknown model"
            user = (
                ev.get("serviceAccountName")
                or ev.get("userEmail")
                or ev.get("serviceAccountId")
                or "unknown"
            ).strip() or "unknown"
            bucket = agg[(model, user)]
            bucket["cents"] += float(charged)
            tu = ev.get("tokenUsage") or {}
            bucket["tokens"] += float(
                (tu.get("inputTokens") or 0)
                + (tu.get("outputTokens") or 0)
                + (tu.get("cacheWriteTokens") or 0)
                + (tu.get("cacheReadTokens") or 0)
            )

        pagination = data.get("pagination", {}) or {}
        if not pagination.get("hasNextPage"):
            break
        page += 1

    # "billed" mode: scale the list-value usage down to what Cursor actually
    # invoices (overage beyond each seat's included allotment). Uses the stored
    # per-cycle snapshot for this window (captured near cycle-end), falling back
    # to a live factor. `start` is the resolved cycle start = snapshot key.
    factor = 1.0
    if str(cursor_cfg.get("billing_mode", "list")).lower() == "billed":
        total_list_cents = sum(b["cents"] for b in agg.values())
        factor = _resolve_billed_factor(cfg, session, start, total_list_cents)

    records: list[CostRecord] = []
    for (model, user), b in agg.items():
        cost = round(b["cents"] / 100.0 * factor, 2)
        tokens = round(b["tokens"], 0)
        if abs(cost) < 0.005 and tokens < 1:
            continue
        records.append(
            CostRecord(
                cloud="Cursor",
                service=model,
                cost=cost,
                environment=environment,
                account="Cursor Team",
                period_start=start,
                period_end=end,
                model=model,
                tokens=tokens,
                usage_unit="tokens",
                user=user,
            )
        )

    records.extend(_seat_records(cursor_cfg, session, environment, start, end))

    log.info(
        "Cursor: collected %d (model,user) lines from %d events, total=%.2f",
        len(records), total_events, round(sum(r.cost for r in records), 2),
    )
    return records
