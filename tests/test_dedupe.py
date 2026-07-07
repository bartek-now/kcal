from kcal.dedupe import build_day_stats

SUMMARY = {
    "totalSteps": 10000,
    "totalKilocalories": 2400,
    "activeKilocalories": 600,
    "bmrKilocalories": 1800,
}

RUN_ACTIVITY = {
    "activityId": 1,
    "activityName": "Morning Run",
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2026-07-06 07:00:00",
    "duration": 1800,
    "calories": 350,
    "steps": 4000,
}

BIKE_ACTIVITY = {
    "activityId": 2,
    "activityName": "Evening Ride",
    "activityType": {"typeKey": "cycling"},
    "startTimeLocal": "2026-07-06 18:00:00",
    "duration": 3600,
    "calories": 500,
    "steps": None,
}


def test_no_workouts_leaves_steps_untouched():
    stats = build_day_stats("2026-07-06", SUMMARY, [])
    assert stats.total_steps == 10000
    assert stats.workout_steps == 0
    assert stats.non_workout_steps == 10000
    assert stats.workout_calories == 0


def test_step_based_workout_is_deducted_from_total():
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY])
    assert stats.workout_steps == 4000
    assert stats.non_workout_steps == 6000
    assert stats.workout_calories == 350


def test_non_step_workout_contributes_zero_steps():
    stats = build_day_stats("2026-07-06", SUMMARY, [BIKE_ACTIVITY])
    assert stats.workout_steps == 0
    assert stats.non_workout_steps == 10000
    assert stats.workout_calories == 500


def test_multiple_workouts_are_summed():
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY, BIKE_ACTIVITY])
    assert stats.workout_steps == 4000
    assert stats.non_workout_steps == 6000
    assert stats.workout_calories == 850
    assert len(stats.workouts) == 2


def test_non_workout_steps_never_negative():
    summary = {**SUMMARY, "totalSteps": 1000}
    stats = build_day_stats("2026-07-06", summary, [RUN_ACTIVITY])
    assert stats.non_workout_steps == 0
