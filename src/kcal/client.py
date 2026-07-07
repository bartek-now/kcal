"""Thin wrapper around the Garmin Connect API calls kcal needs."""

from __future__ import annotations

from datetime import date

from garminconnect import Garmin


def fetch_day_summary(api: Garmin, day: date) -> dict:
    return api.get_user_summary(day.isoformat())


def fetch_activities(api: Garmin, day: date) -> list[dict]:
    iso = day.isoformat()
    return api.get_activities_by_date(iso, iso)
