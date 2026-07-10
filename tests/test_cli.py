import json
from datetime import date

import pytest

from kcal.cli import _render_csv, _render_json, _render_table, resolve_days
from kcal.models import DayStats, Workout

TODAY = date(2026, 7, 9)
YESTERDAY = date(2026, 7, 8)

ONE_DAY = [
    DayStats(
        date="2026-07-06",
        total_steps=10000,
        total_calories=2400,
        active_calories=600,
        bmr_calories=1800,
        workouts=[
            Workout(
                activity_id=1,
                name="Morning Run",
                activity_type="running",
                start_time="2026-07-06 07:00:00",
                duration_seconds=1800,
                calories=350,
                bmr_calories=50,
                steps=4000,
            )
        ],
    )
]


def test_neither_given_defaults_to_yesterday():
    assert resolve_days(None, None, None, today=TODAY) == [YESTERDAY]


def test_date_alone_returns_single_day():
    assert resolve_days("2026-07-01", None, None, today=TODAY) == [date(2026, 7, 1)]


def test_from_alone_ranges_through_yesterday():
    days = resolve_days(None, "2026-07-05", None, today=TODAY)
    assert days == [date(2026, 7, 5), date(2026, 7, 6), date(2026, 7, 7), YESTERDAY]


def test_from_and_to_given_uses_explicit_range():
    days = resolve_days(None, "2026-07-05", "2026-07-06", today=TODAY)
    assert days == [date(2026, 7, 5), date(2026, 7, 6)]


def test_to_without_from_errors():
    with pytest.raises(ValueError, match="--to requires --from"):
        resolve_days(None, None, "2026-07-06", today=TODAY)


def test_date_combined_with_from_errors():
    with pytest.raises(ValueError, match="--date cannot be combined"):
        resolve_days("2026-07-01", "2026-07-05", None, today=TODAY)


def test_date_combined_with_to_errors():
    with pytest.raises(ValueError, match="--date cannot be combined"):
        resolve_days("2026-07-01", None, "2026-07-06", today=TODAY)


def test_to_before_from_errors():
    with pytest.raises(ValueError, match="--to must not be before --from"):
        resolve_days(None, "2026-07-06", "2026-07-01", today=TODAY)


def test_render_table_shows_active_calorie_split():
    output = _render_table(ONE_DAY)
    assert "2026-07-06" in output
    assert "steps:    total=10000  workout=4000  non-workout=6000" in output
    assert (
        "calories: total=2400  bmr=1800  active=600 (workout=300 non-workout=300)"
        in output
    )
    assert (
        "Morning Run (running) steps=4000 calories=350 (active=300 bmr=50) "
        "duration=30.0min" in output
    )


def test_render_json_includes_active_calorie_fields():
    data = json.loads(_render_json(ONE_DAY))
    day = data[0]
    assert day["workout_active_calories"] == 300
    assert day["non_workout_active_calories"] == 300
    assert day["workouts"][0]["active_calories"] == 300
    assert day["workouts"][0]["bmr_calories"] == 50


def test_render_csv_header_and_row():
    rows = _render_csv(ONE_DAY).splitlines()
    assert rows[0] == (
        "date,total_steps,workout_steps,non_workout_steps,total_calories,"
        "bmr_calories,active_calories,workout_active_calories,"
        "non_workout_active_calories,workout_calories,workout_count"
    )
    assert rows[1] == "2026-07-06,10000,4000,6000,2400,1800,600,300,300,350,1"
