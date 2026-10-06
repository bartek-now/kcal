"""MCP server exposing Garmin daily steps/calories/weight as a tool.

Runs over stdio. Login is non-interactive: run `kcal fetch` once in a terminal
first so the session token (and any MFA code) is cached under ~/.kcal.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from mcp.server.fastmcp import FastMCP

from kcal.auth import login
from kcal.cli import _fetch_days, resolve_days
from kcal.models import DayStats

mcp = FastMCP("kcal")


def _no_mfa() -> str:
    raise RuntimeError(
        "Garmin login needs an MFA code or fresh credentials. Run `kcal fetch` "
        "in a terminal once to refresh the cached session, then retry."
    )


def _day_to_dict(s: DayStats) -> dict:
    return {
        "date": s.date,
        "weight_kg": s.weight_kg,
        "steps": s.total_steps,
        "non_workout_steps": s.non_workout_steps,
        "active_calories": round(s.active_calories),
        "passive_calories": round(s.bmr_calories),
        "workout_calories": round(s.workout_calories),
        "workout_active_calories": round(s.workout_active_calories),
        "workouts": [asdict(w) for w in s.workouts],
    }


@mcp.tool()
def get_garmin_daily_stats(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Daily Garmin Connect stats: weight, steps, calories and workouts.

    Dates are YYYY-MM-DD. Pass `date` for one day, or `from_date` (and
    optionally `to_date`, default yesterday) for an inclusive range. With no
    arguments, returns yesterday. Calories are kcal; weight is kg (null if no
    weigh-in that day).
    """
    days = resolve_days(date, from_date, to_date)
    return [_day_to_dict(s) for s in _fetch_days(days, lambda: login(prompt_mfa=_no_mfa))]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
