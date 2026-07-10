from datetime import date

import pytest

from kcal.cli import resolve_days

TODAY = date(2026, 7, 9)
YESTERDAY = date(2026, 7, 8)


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
