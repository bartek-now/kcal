"""Data model for a day's step and calorie stats."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Workout:
    activity_id: int | None
    name: str
    activity_type: str
    start_time: str | None
    duration_seconds: float
    calories: float
    bmr_calories: float
    steps: int

    @classmethod
    def from_raw(cls, raw: dict) -> "Workout":
        activity_type = raw.get("activityType") or {}
        return cls(
            activity_id=raw.get("activityId"),
            name=raw.get("activityName") or "Unnamed activity",
            activity_type=activity_type.get("typeKey", "unknown"),
            start_time=raw.get("startTimeLocal"),
            duration_seconds=raw.get("duration") or 0.0,
            calories=raw.get("calories") or 0.0,
            bmr_calories=raw.get("bmrCalories") or 0.0,
            steps=raw.get("steps") or 0,
        )

    @property
    def active_calories(self) -> float:
        """Calories net of the basal metabolic cost already counted in the
        day's bmr_calories. Garmin's per-activity `calories` is gross (active
        + BMR for that duration), unlike the day summary's activeKilocalories.
        """
        return self.calories - self.bmr_calories


@dataclass
class DayStats:
    date: str
    total_steps: int
    total_calories: float
    active_calories: float
    bmr_calories: float
    workouts: list[Workout] = field(default_factory=list)

    @property
    def workout_steps(self) -> int:
        return sum(w.steps for w in self.workouts)

    @property
    def non_workout_steps(self) -> int:
        return max(self.total_steps - self.workout_steps, 0)

    @property
    def workout_calories(self) -> float:
        """Total calories Garmin attributes to workouts (gross, includes BMR
        for the workout's duration) - matches what the Garmin app shows per
        activity. Not directly comparable to active_calories; see
        workout_active_calories for that.
        """
        return sum(w.calories for w in self.workouts)

    @property
    def workout_active_calories(self) -> float:
        """Workout calories net of BMR - the portion actually comparable to
        (and subtractable from) active_calories.
        """
        return sum(w.active_calories for w in self.workouts)

    @property
    def non_workout_active_calories(self) -> float:
        return max(self.active_calories - self.workout_active_calories, 0.0)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "total_steps": self.total_steps,
            "workout_steps": self.workout_steps,
            "non_workout_steps": self.non_workout_steps,
            "total_calories": self.total_calories,
            "active_calories": self.active_calories,
            "bmr_calories": self.bmr_calories,
            "workout_calories": self.workout_calories,
            "workout_active_calories": self.workout_active_calories,
            "non_workout_active_calories": self.non_workout_active_calories,
            "workouts": [
                {
                    "activity_id": w.activity_id,
                    "name": w.name,
                    "activity_type": w.activity_type,
                    "start_time": w.start_time,
                    "duration_minutes": round(w.duration_seconds / 60, 1),
                    "calories": w.calories,
                    "bmr_calories": w.bmr_calories,
                    "active_calories": w.active_calories,
                    "steps": w.steps,
                }
                for w in self.workouts
            ],
        }
