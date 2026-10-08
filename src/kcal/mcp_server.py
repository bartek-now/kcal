"""MCP server exposing Garmin daily steps/calories/weight as a tool.

Runs over stdio. Login is non-interactive: run `kcal fetch` once in a terminal
first so the session token (and any MFA code) is cached under ~/.kcal.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from kcal import endpoints
from kcal.auth import login
from kcal.cli import _fetch_days, resolve_days
from kcal.models import DayStats

mcp = FastMCP("kcal")

_SRC = Path(__file__).resolve().parent


def _code_hash() -> str:
    """Short hash of the package's .py files as they are on disk right now."""
    h = hashlib.sha256()
    for f in sorted(_SRC.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=_SRC, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


_LOADED_HASH = _code_hash()
_LOADED_COMMIT = _git_commit()
_STARTED = datetime.now().astimezone().isoformat(timespec="seconds")

_api = None


def _get_api():
    """Log in once per server process and reuse the session."""
    global _api
    if _api is None:
        _api = login(prompt_mfa=_no_mfa)
    return _api


def _no_mfa() -> str:
    raise RuntimeError(
        "Garmin login needs an MFA code or fresh credentials. Run `kcal fetch` "
        "in a terminal once to refresh the cached session, then retry."
    )


def _day_to_dict(s: DayStats) -> dict:
    return {
        "date": s.date,
        "weight_kg": s.weight_kg,
        "steps": s.total_steps,
        "non_workout_steps": s.non_workout_steps,
        "active_calories": round(s.active_calories),
        "passive_calories": round(s.bmr_calories),
        "workout_calories": round(s.workout_calories),
        "workout_active_calories": round(s.workout_active_calories),
        "workouts": [asdict(w) for w in s.workouts],
    }


@mcp.tool()
def get_garmin_daily_stats(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Daily Garmin Connect stats: weight, steps, calories and workouts.

    Dates are YYYY-MM-DD. Pass `date` for one day, or `from_date` (and
    optionally `to_date`, default yesterday) for an inclusive range. With no
    arguments, returns yesterday. Calories are kcal; weight is kg (null if no
    weigh-in that day).
    """
    try:
        days = resolve_days(date, from_date, to_date)
    except ValueError as err:
        raise ValueError(_CLI_FLAG_RE.sub(lambda m: _CLI_FLAGS[m[0]], str(err))) from None
    return [_day_to_dict(s) for s in _fetch_days(days, _get_api)]


# resolve_days reports errors with CLI flag names; MCP callers know the params.
_CLI_FLAGS = {"--date": "date", "--from": "from_date", "--to": "to_date"}
_CLI_FLAG_RE = re.compile("(?:" + "|".join(_CLI_FLAGS) + r")\b")


@mcp.tool()
def list_garmin_endpoints() -> list[dict]:
    """List every read-only Garmin Connect endpoint with its parameters.

    Use the names with `call_garmin_endpoint`. Dates are YYYY-MM-DD strings.
    """
    return endpoints.list_endpoints()


@mcp.tool()
def call_garmin_endpoint(endpoint: str, args: dict | None = None) -> object:
    """Call any read-only Garmin Connect endpoint, e.g. `get_sleep_data`.

    `args` maps parameter names to values, e.g. {"cdate": "2026-10-05"} (see
    `list_garmin_endpoints`). Covers sleep, heart rate, HRV, stress, body
    battery, SpO2, training status, activities and splits, devices, goals,
    badges and more. Oversized results are replaced by a notice; narrow the
    request if that happens.
    """
    return endpoints.cap_size(endpoints.call_endpoint(_get_api(), endpoint, args))


@mcp.tool()
def get_garmin_day(date: str, metrics: list[str] | None = None) -> dict:
    """Several Garmin metrics for one day (YYYY-MM-DD) in a single call.

    `metrics` are single-date endpoint names without the `get_` prefix, e.g.
    ["sleep_data", "heart_rates", "stress_data"]. Default is a compact set:
    sleep, HRV, resting HR, training readiness/status, max metrics, hydration
    and intensity minutes. Intraday series (heart_rates, stress_data,
    steps_data, ...) must be requested explicitly. A metric that fails reports
    its own error without failing the others.
    """
    return endpoints.day_metrics(_get_api(), date, metrics)


@mcp.tool()
def get_kcal_server_info() -> dict:
    """Which kcal code this server is running, and whether it is out of date.

    `stale: true` means the code on disk changed after this server started;
    fully quit and reopen the client to load it.
    """
    on_disk = _code_hash()
    return {
        "loaded_code": _LOADED_HASH,
        "loaded_git": _LOADED_COMMIT,
        "on_disk_code": on_disk,
        "stale": on_disk != _LOADED_HASH,
        "started": _STARTED,
        "pid": os.getpid(),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
