"""kcal fetch: daily steps and workout calories from Garmin Connect."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from kcal.auth import login
from kcal.client import fetch_activities, fetch_day_summary
from kcal.dedupe import build_day_stats
from kcal.models import DayStats


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("--to must not be before --from")
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def resolve_days(
    date_str: str | None,
    from_str: str | None,
    to_str: str | None,
    today: date | None = None,
) -> list[date]:
    """Work out which days to fetch from --date/--from/--to.

    - --date alone: that single day.
    - --from alone: --from through yesterday.
    - --from and --to: that range, inclusive.
    - none given: yesterday only.
    - --to without --from: an error, since there'd be no start to range from.
    """
    today = today or date.today()

    if date_str and (from_str or to_str):
        raise ValueError("--date cannot be combined with --from/--to")
    if to_str and not from_str:
        raise ValueError("--to requires --from")

    if date_str:
        return [_parse_date(date_str)]
    if from_str:
        end = _parse_date(to_str) if to_str else today - timedelta(days=1)
        return _date_range(_parse_date(from_str), end)
    return [today - timedelta(days=1)]


def _fetch_days(days: list[date]) -> list[DayStats]:
    api = login()
    results = []
    for day in days:
        summary = fetch_day_summary(api, day)
        activities = fetch_activities(api, day)
        results.append(build_day_stats(day.isoformat(), summary, activities))
    return results


def _render_table(stats: list[DayStats]) -> str:
    lines = []
    for s in stats:
        lines.append(f"{s.date}")
        lines.append(
            f"  steps:    total={s.total_steps}  workout={s.workout_steps}"
            f"  non-workout={s.non_workout_steps}"
        )
        lines.append(
            f"  calories: total={s.total_calories:.0f}  bmr={s.bmr_calories:.0f}"
            f"  active={s.active_calories:.0f}"
            f" (workout={s.workout_active_calories:.0f}"
            f" non-workout={s.non_workout_active_calories:.0f})"
        )
        if s.workouts:
            lines.append("  workouts:")
            for w in s.workouts:
                lines.append(
                    f"    - {w.name} ({w.activity_type}) "
                    f"steps={w.steps} calories={w.calories:.0f}"
                    f" (active={w.active_calories:.0f} bmr={w.bmr_calories:.0f}) "
                    f"duration={w.duration_seconds / 60:.1f}min"
                )
        else:
            lines.append("  workouts: none")
    return "\n".join(lines)


def _render_json(stats: list[DayStats]) -> str:
    return json.dumps([s.to_dict() for s in stats], indent=2)


def _render_csv(stats: list[DayStats]) -> str:
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "date",
            "total_steps",
            "workout_steps",
            "non_workout_steps",
            "total_calories",
            "bmr_calories",
            "active_calories",
            "workout_active_calories",
            "non_workout_active_calories",
            "workout_calories",
            "workout_count",
        ]
    )
    for s in stats:
        writer.writerow(
            [
                s.date,
                s.total_steps,
                s.workout_steps,
                s.non_workout_steps,
                round(s.total_calories),
                round(s.bmr_calories),
                round(s.active_calories),
                round(s.workout_active_calories),
                round(s.non_workout_active_calories),
                round(s.workout_calories),
                len(s.workouts),
            ]
        )
    return buf.getvalue()


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        days = resolve_days(args.date, args.from_date, args.to_date)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    stats = _fetch_days(days)

    renderer = {"table": _render_table, "json": _render_json, "csv": _render_csv}[
        args.format
    ]
    output = renderer(stats)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kcal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch", help="Fetch steps and workout calories for a day or date range"
    )
    fetch.add_argument("--date", help="Single day, YYYY-MM-DD")
    fetch.add_argument(
        "--from",
        dest="from_date",
        metavar="DATE",
        help="Range start, YYYY-MM-DD",
    )
    fetch.add_argument(
        "--to",
        dest="to_date",
        metavar="DATE",
        help="Range end, YYYY-MM-DD (default: yesterday, requires --from)",
    )
    fetch.add_argument(
        "--format", choices=["table", "json", "csv"], default="table"
    )
    fetch.add_argument("--output", help="Write to this file instead of stdout")
    fetch.set_defaults(func=_cmd_fetch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
