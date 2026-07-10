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
    "bmrCalories": 50,
    "steps": 4000,
}

BIKE_ACTIVITY = {
    "activityId": 2,
    "activityName": "Evening Ride",
    "activityType": {"typeKey": "cycling"},
    "startTimeLocal": "2026-07-06 18:00:00",
    "duration": 3600,
    "calories": 500,
    "bmrCalories": 80,
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
    assert stats.workout_active_calories == 300  # 350 - 50 bmrCalories
    assert stats.non_workout_active_calories == 300  # 600 - 300


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


def test_workout_active_calories_nets_out_bmr():
    """Garmin's per-activity `calories` is gross - it includes the BMR
    calories for the activity's duration (`bmrCalories`), which Garmin
    separately counts in the day's bmr_calories, not active_calories. So a
    workout's gross calories can exceed the day's active_calories even
    though its *net* active contribution can't.
    """
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY])
    workout = stats.workouts[0]
    assert workout.active_calories == 300  # 350 - 50


def test_non_workout_active_calories_never_negative():
    summary = {**SUMMARY, "activeKilocalories": 100}
    stats = build_day_stats("2026-07-06", summary, [RUN_ACTIVITY])
    assert stats.workout_active_calories == 300
    assert stats.non_workout_active_calories == 0


def test_workout_active_calories_never_negative():
    """bmrCalories can exceed calories in malformed/incomplete source data
    (e.g. calories missing but bmrCalories present) - active_calories must
    still floor at 0 rather than go negative.
    """
    activity = {**RUN_ACTIVITY, "calories": None, "bmrCalories": 50}
    stats = build_day_stats("2026-07-06", SUMMARY, [activity])
    assert stats.workouts[0].active_calories == 0
    assert stats.workout_active_calories == 0
