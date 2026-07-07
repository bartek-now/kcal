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
            steps=raw.get("steps") or 0,
        )


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
        return sum(w.calories for w in self.workouts)

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
            "workouts": [
                {
                    "activity_id": w.activity_id,
                    "name": w.name,
                    "activity_type": w.activity_type,
                    "start_time": w.start_time,
                    "duration_minutes": round(w.duration_seconds / 60, 1),
                    "calories": w.calories,
                    "steps": w.steps,
                }
                for w in self.workouts
            ],
        }
