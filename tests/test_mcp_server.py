import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kcal import mcp_server


class FakeApi:
    def get_sleep_data(self, cdate):
        return {"date": cdate, "movement": list(range(100))}


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", FakeApi())


def call(name, args):
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_tools_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "get_garmin_daily_stats",
        "get_garmin_day",
        "list_garmin_endpoints",
        "call_garmin_endpoint",
        "get_kcal_server_info",
    }


def test_get_garmin_day_through_mcp():
    out = call("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data"]})
    assert out["sleep_data"]["date"] == "2026-10-05"


def test_call_garmin_endpoint_through_mcp():
    out = call("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "d"}})
    assert out["date"] == "d"


def test_server_info_through_mcp():
    out = call("get_kcal_server_info", {})
    assert out["stale"] is False


def test_daily_stats_errors_reach_caller():
    with pytest.raises(ToolError, match="end date needs a start date"):
        call("get_garmin_daily_stats", {"to_date": "2026-10-05"})


def test_daily_stats_range_limited():
    with pytest.raises(ToolError, match="at most 366"):
        call("get_garmin_daily_stats", {"from_date": "2024-01-01", "to_date": "2025-12-31"})
