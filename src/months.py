"""Report month helpers — billing reports only cover completed calendar months."""
from __future__ import annotations

from datetime import date

from dateutil.relativedelta import relativedelta


def latest_allowed_month(today: date | None = None) -> date:
    """Last day of the most recent month that can be reported (previous calendar month)."""
    today = today or date.today()
    return today.replace(day=1) - relativedelta(months=1)


def latest_allowed_month_key(today: date | None = None) -> str:
    d = latest_allowed_month(today)
    return f"{d.year:04d}-{d.month:02d}"


def parse_month_key(month_key: str) -> tuple[int, int]:
    year, month = (int(x) for x in month_key.split("-"))
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid month in {month_key!r}")
    return year, month


def month_key_to_date(month_key: str) -> date:
    year, month = parse_month_key(month_key)
    return date(year, month, 1)


def is_future_month(month_key: str, today: date | None = None) -> bool:
    return month_key_to_date(month_key) > latest_allowed_month(today)


def validate_month_key(month_key: str, today: date | None = None) -> str:
    """Return normalized YYYY-MM or raise ValueError if month is current/future."""
    year, month = parse_month_key(month_key)
    normalized = f"{year:04d}-{month:02d}"
    if is_future_month(normalized, today):
        allowed = latest_allowed_month_key(today)
        raise ValueError(
            f"Cannot generate a report for {normalized}: only completed months up to "
            f"{allowed} are available."
        )
    return normalized
