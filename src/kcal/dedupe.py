"""Build a DayStats from raw Garmin responses.

Garmin's daily step total already includes steps taken during
step-based workouts (runs, walks, hikes). Any activity that reports a
`steps` count is treated as a workout, and its steps are subtracted
from the daily total so a step is never counted both as "background"
movement and as part of a workout.
"""

from __future__ import annotations

from kcal.models import DayStats, Workout


def _latest_weight_kg(weigh_in: dict) -> float | None:
    """Weight from the last weigh-in of the day (by timestamp), or None if
    the scale wasn't used that day. Garmin reports weight in grams.
    """
    entries = weigh_in.get("dateWeightList") or []
    if not entries:
        return None
    latest = max(entries, key=lambda e: e.get("date") or 0)
    grams = latest.get("weight")
    return round(grams / 1000, 1) if grams is not None else None


def build_day_stats(
    date_str: str, summary: dict, activities: list[dict], weigh_in: dict | None = None
) -> DayStats:
    workouts = [Workout.from_raw(a) for a in activities]
    return DayStats(
        date=date_str,
        weight_kg=_latest_weight_kg(weigh_in) if weigh_in else None,
        total_steps=summary.get("totalSteps") or 0,
        total_calories=summary.get("totalKilocalories") or 0.0,
        active_calories=summary.get("activeKilocalories") or 0.0,
        bmr_calories=summary.get("bmrKilocalories") or 0.0,
        workouts=workouts,
    )
