"""MCP server exposing Garmin daily steps/calories/weight as a tool.

Runs over stdio by default, or over Streamable HTTP with `--http` (see
docs/http-server-design.md). Login is non-interactive: run `kcal fetch` once
in a terminal first so the session token (and any MFA code) is cached under
KCAL_STATE_DIR (default ~/.kcal).
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from kcal import endpoints
from kcal.auth import GarminLoginError, login
from kcal.daily import fetch_days, fetch_weights, resolve_days
from kcal.http_log import LogClientErrors
from kcal.models import DayStats
from kcal.oauth import KcalOAuthProvider, auth_settings
from kcal.settings import HttpSettings, state_dir

# Filled by @_tool; build_server() registers them on each FastMCP instance.
_TOOLS = []


@dataclass(frozen=True)
class _Exposure:
    """What a server's tools may reach and how their results are filtered."""

    allowed: frozenset[str] | None  # endpoint names without `get_`; None = all
    scrub: Callable[[Any], Any]


LOCAL = _Exposure(allowed=None, scrub=lambda result: result)
REMOTE = _Exposure(allowed=endpoints.REMOTE_ALLOWED, scrub=endpoints.strip_private)
# Fails closed: code running outside a tool wrapper gets the remote rules.
# stdio tools unlock LOCAL explicitly in _wrap().
_exposure: ContextVar[_Exposure] = ContextVar("kcal_exposure", default=REMOTE)

# Each day costs one Garmin request (the daily summary), made sequentially, and
# ~300 chars of output; 120 days stays well inside endpoints.MAX_RESULT_CHARS.
MAX_STATS_DAYS = 120
# Weigh-ins for any range are one request and ~80 chars per day.
MAX_WEIGHT_DAYS = 366

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
_api_lock = threading.Lock()


def _get_api():
    """Log in once per server process and reuse the session. Tools run in
    worker threads, so concurrent first calls must not both log in.
    """
    global _api
    with _api_lock:
        if _api is None:
            try:
                _api = login(prompt_mfa=_no_mfa)
            except Exception as err:
                raise GarminLoginError(str(err)) from err
    return _api


class _Garmin:
    """What tools use instead of the Garmin API: the one place the current
    exposure's allowlist is enforced, for every tool. Logs in on first use,
    so a rejected call makes no Garmin request, not even a login.
    """

    def __getattr__(self, name):
        allowed = _exposure.get().allowed
        if allowed is not None and not (
            name.startswith("get_") and name.removeprefix("get_") in allowed
        ):
            raise ValueError(f"Garmin {name!r} is not available remotely.")
        return getattr(_get_api(), name)


def _no_mfa() -> str:
    raise RuntimeError(
        "Garmin login needs an MFA code or fresh credentials. Run `kcal fetch` "
        "in a terminal once to refresh the cached session, then retry."
    )


def _tool(fn):
    """Mark `fn` as a tool; build_server() registers it via _wrap()."""
    _TOOLS.append(fn)
    return fn


def _wrap(fn, exposure: _Exposure):
    """The tool as registered: its result goes out as one compact JSON text
    block, replaced by a notice if it's too large. (FastMCP's default
    pretty-prints, sends each list item as its own block, and for typed returns
    sends everything a second time as structured content.) `fn` runs in a
    worker thread: FastMCP calls sync tools on its event loop, so a slow
    Garmin request would otherwise stall every other request. While it runs,
    `_exposure` holds the server's policy, and the result is scrubbed with it.
    """

    def run(**kwargs):
        token = _exposure.set(exposure)
        try:
            result = exposure.scrub(fn(**kwargs))
        finally:
            _exposure.reset(token)
        return endpoints.to_json(endpoints.cap_size(result))

    @functools.wraps(fn)
    async def wrapper(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(run, **kwargs))

    return wrapper


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


@_tool
def get_garmin_daily_stats(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Daily Garmin Connect stats: weight, steps, calories and workouts.

    Dates are YYYY-MM-DD. Pass `date` for one day, or `from_date` (and
    optionally `to_date`, default yesterday) for an inclusive range. With no
    arguments, returns yesterday. Calories are kcal; weight is kg (null if no
    weigh-in that day). Ranges are limited to 120 days; split longer
    ones into several calls.
    """
    days = resolve_days(date, from_date, to_date, max_days=MAX_STATS_DAYS)
    return [_day_to_dict(s) for s in fetch_days(days, _Garmin)]


@_tool
def get_garmin_weight(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Weigh-ins from a Garmin scale: one row per day that had one, oldest first.

    Same date arguments as `get_garmin_daily_stats`; ranges are limited to
    366 days and take one Garmin request regardless of length. Each row has
    `date`, `weight_kg`, and `body_fat_pct` / `muscle_mass_kg` when the scale
    reports them, from the day's last weigh-in; `count` appears when there
    were several that day. Prefer this over `get_weigh_ins` for weight trends.
    """
    days = resolve_days(date, from_date, to_date, max_days=MAX_WEIGHT_DAYS)
    return fetch_weights(days, _Garmin)


@_tool
def list_garmin_endpoints() -> list[dict]:
    """List every read-only Garmin Connect endpoint with its parameters.

    Use the names with `call_garmin_endpoint`. Dates are YYYY-MM-DD strings.
    Over a remote connection, only health and fitness endpoints are listed.
    """
    return endpoints.list_endpoints(_exposure.get().allowed)


@_tool
def call_garmin_endpoint(endpoint: str, args: dict | None = None) -> object:
    """Call any read-only Garmin Connect endpoint, e.g. `get_sleep_data`.

    `args` maps parameter names to values, e.g. {"cdate": "2026-10-05"} (see
    `list_garmin_endpoints`). Covers sleep, heart rate, HRV, stress, body
    battery, SpO2, training status, activities and splits, devices, goals,
    badges and more (over a remote connection, only health and fitness
    endpoints, without location data). Oversized results are replaced by a
    notice; narrow the request if that happens.
    """
    return endpoints.call_endpoint(_Garmin(), endpoint, args, _exposure.get().allowed)


@_tool
def get_garmin_day(date: str, metrics: list[str] | None = None) -> dict:
    """Several Garmin metrics for one day (YYYY-MM-DD) in a single call.

    `metrics` are single-date endpoint names without the `get_` prefix, e.g.
    ["sleep_data", "heart_rates", "stress_data"]. Default is a compact set:
    sleep, HRV, resting HR, training readiness/status, max metrics, hydration
    and intensity minutes. Intraday series (heart_rates, stress_data,
    steps_data, ...) must be requested explicitly. Defaults have long lists
    replaced by "<N items omitted>"; metrics you name in `metrics` are returned
    in full, e.g. metrics=["sleep_data"] for the per-minute sleep lists. A
    metric that fails reports its own error without failing the others.
    """
    return endpoints.day_metrics(_Garmin(), date, metrics, _exposure.get().allowed)


@_tool
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


def build_server(
    http: HttpSettings | None = None, oauth: KcalOAuthProvider | None = None
) -> FastMCP:
    """A FastMCP server with every tool. With `http`, it's set up to be
    served over Streamable HTTP on 127.0.0.1, reached through `http.public_url`,
    and its tools only reach REMOTE_ALLOWED endpoints, with location data
    stripped from every result. With `oauth` too, /mcp needs a bearer token
    from its authorization server (docs/http-server-design.md, section 2).
    """
    kwargs = {}
    if http is not None:
        kwargs = dict(
            host="127.0.0.1",
            port=http.port,
            # No MCP sessions: each request stands alone, so server restarts
            # don't strand clients. Replies are plain JSON, not SSE streams,
            # since tools send no progress or log messages.
            stateless_http=True,
            json_response=True,
            # FastMCP only allows localhost Host/Origin headers on 127.0.0.1,
            # which would reject requests arriving through the tunnel.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    http.public_host, "127.0.0.1:*", "localhost:*", "[::1]:*",
                ],
                allowed_origins=[
                    http.public_url,
                    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                ],
            ),
        )
    if oauth is not None:
        if http is None:
            raise ValueError("OAuth needs HTTP settings (the public URL)")
        kwargs.update(auth_server_provider=oauth, auth=auth_settings(http.public_url))
    server = FastMCP("kcal", **kwargs)
    if oauth is not None:
        _add_login_routes(server, oauth)
    exposure = LOCAL if http is None else REMOTE
    for fn in _TOOLS:
        server.tool(structured_output=False)(_wrap(fn, exposure))
    return server


def _add_login_routes(server: FastMCP, oauth: KcalOAuthProvider) -> None:
    @server.custom_route("/login", methods=["GET", "POST"])
    async def login_page(request: Request):
        # Placeholder until the Garmin login page (build step 4).
        return PlainTextResponse(
            "The kcal login page isn't built yet, so connecting a client with "
            "authentication isn't possible yet.",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )


mcp = build_server()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcal-mcp",
        description="Garmin MCP server. Runs over stdio unless --http is given.",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Serve Streamable HTTP at <public URL>/mcp instead of stdio",
    )
    parser.add_argument(
        "--public-url", metavar="URL",
        help="Public base URL clients use, e.g. https://abc.trycloudflare.com "
        "(default: $KCAL_PUBLIC_URL)",
    )
    parser.add_argument(
        "--port", metavar="N",
        help="Local port to listen on, on 127.0.0.1 (default: $KCAL_PORT or 8000)",
    )
    parser.add_argument(
        "--no-auth", action="store_true",
        help="Serve --http without OAuth, so anyone with the URL can read your "
        "Garmin data. Only for short tests until the login page exists",
    )
    parser.add_argument(
        "--revoke-all", action="store_true",
        help="Sign every connected client out (they log in again), then exit. "
        "Works while the server is running",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.revoke_all:
        return _revoke_all()
    if not args.http:
        mcp.run()
        return 0
    try:
        http = HttpSettings.from_env(public_url=args.public_url, port=args.port)
    except ValueError as e:
        print(f"kcal-mcp: {e}", file=sys.stderr)
        return 2
    oauth = None
    if not args.no_auth:
        oauth = KcalOAuthProvider(state_dir() / "mcp_auth.sqlite", http.public_url)
    print(
        f"kcal-mcp: serving {http.public_url}/mcp from 127.0.0.1:{http.port} "
        + ("with OAuth" if oauth else "WITHOUT authentication"),
        file=sys.stderr,
    )
    if oauth:
        print(
            "kcal-mcp: note: the login page isn't built yet, so clients can't "
            "finish connecting; use --no-auth for short tests until it is.",
            file=sys.stderr,
        )
    serve_http(build_server(http, oauth))
    return 0


def _revoke_all() -> int:
    db = state_dir() / "mcp_auth.sqlite"
    if not db.exists():
        print(f"kcal-mcp: no OAuth database at {db}; nothing to revoke.", file=sys.stderr)
        return 0
    provider = KcalOAuthProvider(db, public_url="")
    try:
        ended = provider.revoke_all()
    finally:
        provider.close()
    print(f"kcal-mcp: signed out {ended} client authorization(s).", file=sys.stderr)
    return 0


def serve_http(server: FastMCP) -> None:
    """Like `server.run(transport="streamable-http")`, but logs why requests
    were rejected (see kcal.http_log).
    """
    uvicorn.run(
        LogClientErrors(server.streamable_http_app()),
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
