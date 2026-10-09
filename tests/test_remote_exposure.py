"""What the HTTP server's tools may reach (docs/http-server-design.md, section 5)."""

import asyncio
import json

import pytest
from garminconnect import Garmin
from mcp.server.fastmcp.exceptions import ToolError

from kcal import endpoints, mcp_server
from kcal.settings import HttpSettings

GETTERS = {name.removeprefix("get_") for name in endpoints._getters()}
DISALLOWED = sorted(GETTERS - endpoints.REMOTE_ALLOWED)

# Named in the design doc's "Not allowed" list.
SENSITIVE = [
    "activity_details", "activity_weather", "user_profile",
    "userprofile_settings", "full_name", "unit_system", "devices", "gear",
    "goals", "training_plans", "menstrual_data_for_date",
    "menstrual_calendar_data", "pregnancy_summary",
]

LOCATED = {
    "activityId": 1,
    "locationName": "Somewhere",
    "summaryDTO": {"calories": 500.0, "startLatitude": 52.1, "endLongitude": 21.0},
    "lapDTOs": [{"distance": 1000, "startLatitude": 52.1, "startLongitude": 21.0}],
    "geoPolylineDTO": {"polyline": [[52.1, 21.0]]},
    "hasPolyline": True,
}


class RecordingApi:
    """Records every endpoint requested and answers with location-laden data."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def endpoint(*args, **kwargs):
            self.calls.append(name)
            return LOCATED
        return endpoint


@pytest.fixture
def api(monkeypatch):
    fake = RecordingApi()
    monkeypatch.setattr(mcp_server, "_api", fake)
    return fake


@pytest.fixture(scope="module")
def remote():
    return mcp_server.build_server(HttpSettings(public_url="https://x.example"))


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# --- the allowlist itself ----------------------------------------------------


def test_allowlisted_names_exist_in_garminconnect():
    # A library rename must not silently drop an endpoint.
    assert endpoints.REMOTE_ALLOWED <= GETTERS


@pytest.mark.parametrize("name", SENSITIVE)
def test_sensitive_endpoints_not_allowlisted(name):
    assert name in GETTERS
    assert name not in endpoints.REMOTE_ALLOWED


def test_default_day_metrics_allowlisted():
    assert set(endpoints.DEFAULT_DAY_METRICS) <= endpoints.REMOTE_ALLOWED


# --- negative: disallowed endpoints, through every entry point ---------------


@pytest.mark.parametrize("name", DISALLOWED)
def test_call_endpoint_rejects_disallowed_remotely(api, remote, name):
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": f"get_{name}", "args": {}})
    assert api.calls == []  # rejected before any Garmin request


def test_disallowed_rejection_needs_no_login(monkeypatch, remote):
    # Not even a login: rejection happens before the API is touched.
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", lambda **_: pytest.fail("logged in"))
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": "get_user_profile"})


def test_list_endpoints_remotely_only_allowlisted(api, remote):
    names = {e["endpoint"].removeprefix("get_") for e in call(remote, "list_garmin_endpoints", {})}
    assert names == endpoints.REMOTE_ALLOWED
    assert not names & set(DISALLOWED)


@pytest.mark.parametrize("name", DISALLOWED)
def test_day_rejects_disallowed_metric_remotely(api, remote, name):
    out = call(remote, "get_garmin_day", {"date": "2026-10-05", "metrics": [name, "sleep_data"]})
    assert "not available remotely" in out[name]["error"].lower()
    assert name not in out[name]["error"].split("Available:")[1]
    assert api.calls == ["get_sleep_data"]  # the allowed one still works


def test_unknown_getter_blocked_remotely_by_default(api, remote, monkeypatch):
    # A getter garminconnect might add later: reachable locally, not remotely.
    monkeypatch.setattr(Garmin, "get_new_thing", lambda self: {}, raising=False)
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": "get_new_thing"})
    assert api.calls == []
    call(mcp_server.mcp, "call_garmin_endpoint", {"endpoint": "get_new_thing"})
    assert api.calls == ["get_new_thing"]


# --- positive ----------------------------------------------------------------


def test_allowed_endpoint_works_remotely(api, remote):
    call(remote, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert api.calls == ["get_activity"]


@pytest.mark.parametrize("name", ["user_profile", "devices", "activity_details"])
def test_disallowed_endpoints_still_work_over_stdio(api, name):
    args = {"activity_id": "1"} if name == "activity_details" else {}
    out = call(mcp_server.mcp, "call_garmin_endpoint", {"endpoint": f"get_{name}", "args": args})
    assert api.calls == [f"get_{name}"]
    assert out["summaryDTO"]["startLatitude"] == 52.1  # nothing stripped locally


def test_list_endpoints_over_stdio_lists_everything(api):
    names = {e["endpoint"].removeprefix("get_") for e in call(mcp_server.mcp, "list_garmin_endpoints", {})}
    assert names == GETTERS


# --- location stripping ------------------------------------------------------


def test_strip_location_nested():
    assert endpoints.strip_location(LOCATED) == {
        "activityId": 1,
        "summaryDTO": {"calories": 500.0},
        "lapDTOs": [{"distance": 1000}],
    }


def test_strip_location_leaves_other_data_alone():
    data = {"a": [1, {"b": None, "c": [{"d": "x"}]}], "e": 2.5, "f": "latitude"}
    assert endpoints.strip_location(data) == data
    assert endpoints.strip_location([LOCATED])[0] == endpoints.strip_location(LOCATED)


def test_remote_results_are_stripped(api, remote):
    out = call(remote, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert out == endpoints.strip_location(LOCATED)


def test_remote_day_results_are_stripped(api, remote):
    out = call(remote, "get_garmin_day", {"date": "d", "metrics": ["sleep_data"]})
    assert out["sleep_data"] == endpoints.strip_location(LOCATED)
