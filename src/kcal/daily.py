"""Which days to fetch, and fetching per-day stats; shared by the CLI and MCP server."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from kcal.auth import login
from kcal.client import fetch_activities, fetch_day_summary, fetch_weigh_ins
from kcal.dedupe import build_day_stats
from kcal.models import DayStats


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from None


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"End date {end} is before start date {start}.")
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def resolve_days(
    single: str | None,
    start: str | None,
    end: str | None,
    today: date | None = None,
) -> list[date]:
    """Work out which days to fetch.

    - single date alone: that day.
    - start alone: start through yesterday.
    - start and end: that range, inclusive.
    - none given: yesterday only.
    - end without start: an error, since there'd be no start to range from.
    """
    today = today or date.today()

    if single and (start or end):
        raise ValueError(
            "Give either a single date or a start/end range, not both."
        )
    if end and not start:
        raise ValueError("An end date needs a start date.")

    if single:
        return [_parse_date(single)]
    if start:
        last = _parse_date(end) if end else today - timedelta(days=1)
        return _date_range(_parse_date(start), last)
    return [today - timedelta(days=1)]


def fetch_days(days: list[date], login_fn=login) -> list[DayStats]:
    api = login_fn()
    results = []
    for day in days:
        summary = fetch_day_summary(api, day)
        activities = fetch_activities(api, day)
        weigh_in = fetch_weigh_ins(api, day)
        results.append(
            build_day_stats(day.isoformat(), summary, activities, weigh_in)
        )
    return results
