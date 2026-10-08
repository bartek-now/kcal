from dataclasses import replace

from kcal.cli import _render_csv
from kcal.models import DayStats, Workout


ONE_DAY = [
    DayStats(
        date="2026-07-06",
        weight_kg=70.5,
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


def test_render_csv_header_and_row():
    rows = _render_csv(ONE_DAY).splitlines()
    assert rows[0] == (
        "date,weight_kg,workout_active_calories,workout_calories,steps,"
        "non_workout_steps,active_calories,passive_calories"
    )
    assert rows[1] == "2026-07-06,70.5,300,350,10000,6000,600,1800"


def test_render_csv_blank_weight_when_no_weigh_in():
    day = replace(ONE_DAY[0], weight_kg=None)
    rows = _render_csv([day]).splitlines()
    assert rows[1] == "2026-07-06,,300,350,10000,6000,600,1800"
