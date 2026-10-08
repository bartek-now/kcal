"""Generic, read-only access to the Garmin Connect API for the MCP server.

Every public `get_*` method on `garminconnect.Garmin` is exposed by name. Only
getters are allowed, so nothing that downloads, writes or deletes is reachable.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from garminconnect import Garmin

# Compact per-day summaries; the intraday series (heart_rates, stress_data,
# steps_data, ...) are large and must be requested explicitly.
DEFAULT_DAY_METRICS = [
    "sleep_data",
    "hrv_data",
    "rhr_day",
    "training_readiness",
    "training_status",
    "max_metrics",
    "hydration_data",
    "intensity_minutes_data",
]

MAX_RESULT_CHARS = 200_000
# Lists longer than this are intraday series (per-minute movement, HRV
# readings, ...); the default day summary drops them and keeps scalars.
SUMMARY_MAX_LIST = 20


def _getters() -> dict[str, Any]:
    return {
        name: fn
        for name, fn in inspect.getmembers(Garmin, inspect.isfunction)
        if name.startswith("get_")
    }


def list_endpoints() -> list[dict]:
    out = []
    for name, fn in sorted(_getters().items()):
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        params = []
        for p in list(inspect.signature(fn).parameters.values())[1:]:
            entry = {"name": p.name, "required": p.default is p.empty}
            if p.default is not p.empty:
                entry["default"] = p.default
            params.append(entry)
        out.append({"endpoint": name, "params": params, "doc": doc[0] if doc else ""})
    return out


def call_endpoint(api: Garmin, endpoint: str, args: dict | None = None) -> Any:
    getters = _getters()
    if endpoint not in getters:
        raise ValueError(
            f"Unknown or non-readable endpoint {endpoint!r}. "
            "Use list_garmin_endpoints to see what is available."
        )
    try:
        inspect.signature(getters[endpoint]).bind(api, **(args or {}))
    except TypeError as err:
        raise ValueError(f"Bad arguments for {endpoint}: {err}") from err
    return getattr(api, endpoint)(**(args or {}))


def cap_size(result: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """Return `result`, or a notice if its JSON is too large to hand back."""
    size = len(json.dumps(result, default=str))
    if size <= limit:
        return result
    notice = {
        "error": f"Result is {size:,} chars (limit {limit:,}); request a narrower "
        "date range or different endpoint.",
        "top_level_keys": list(result) if isinstance(result, dict) else None,
    }
    if len(json.dumps(notice, default=str)) > limit:
        del notice["top_level_keys"]
    return notice


def summarize(value: Any, max_list: int = SUMMARY_MAX_LIST) -> Any:
    """Recursively replace long lists (intraday series) with an omission marker."""
    if isinstance(value, dict):
        return {k: summarize(v, max_list) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > max_list:
            return f"<{len(value)} items omitted>"
        return [summarize(v, max_list) for v in value]
    return value


def single_day_metrics() -> list[str]:
    """Endpoints whose only parameter is a single date (`cdate`)."""
    return sorted(
        name.removeprefix("get_")
        for name, fn in _getters().items()
        if list(inspect.signature(fn).parameters)[1:] == ["cdate"]
    )


def day_metrics(api: Garmin, day: str, metrics: list[str] | None = None) -> dict:
    """Fetch several single-date metrics. Defaults are summarized; explicit ones raw."""
    wanted = DEFAULT_DAY_METRICS if metrics is None else list(dict.fromkeys(metrics))
    slim = metrics is None
    available = set(single_day_metrics())
    out: dict[str, Any] = {"date": day}
    for m in wanted:
        if m not in available:
            out[m] = {"error": f"Unknown metric. Available: {sorted(available)}"}
            continue
        try:
            result = getattr(api, f"get_{m}")(day)
            out[m] = summarize(result) if slim else result
        except Exception as err:  # one failing metric shouldn't sink the rest
            out[m] = {"error": f"{type(err).__name__}: {err}"}
    return _fit_total(out)


def _fit_total(out: dict, limit: int = MAX_RESULT_CHARS) -> dict:
    """Replace the largest metrics with notices until the whole response fits."""
    sizes = {k: len(json.dumps(v, default=str)) for k, v in out.items()}
    for k in sorted(sizes, key=sizes.get, reverse=True):
        if len(json.dumps(out, default=str)) <= limit:
            return out
        out[k] = {
            "error": f"Omitted: {sizes[k]:,} chars would exceed the {limit:,} "
            "char response limit; request this metric on its own."
        }
    if len(json.dumps(out, default=str)) <= limit:
        return out
    return {"error": f"Response exceeds the {limit:,} char limit; request fewer metrics."}
