import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kcal import mcp_server
from kcal.settings import HttpSettings, garmin_token_store, state_dir

PUBLIC = "https://abc-def.trycloudflare.com"
HEADERS = {
    "Host": "abc-def.trycloudflare.com",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


# --- settings ---------------------------------------------------------------


def test_settings_from_env():
    s = HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC + "/", "KCAL_PORT": "9000"})
    assert s == HttpSettings(public_url=PUBLIC, port=9000)
    assert s.public_host == "abc-def.trycloudflare.com"


def test_settings_flags_override_env():
    s = HttpSettings.from_env(
        {"KCAL_PUBLIC_URL": "https://old.example", "KCAL_PORT": "9000"},
        public_url=PUBLIC, port="9100",
    )
    assert s == HttpSettings(public_url=PUBLIC, port=9100)


def test_settings_default_port():
    assert HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC}).port == 8000


def test_settings_require_public_url():
    with pytest.raises(ValueError, match="KCAL_PUBLIC_URL"):
        HttpSettings.from_env({})


@pytest.mark.parametrize(
    "url",
    [
        "http://abc.trycloudflare.com",  # plain http off localhost
        "abc.trycloudflare.com",  # no scheme
        "https://abc.trycloudflare.com/mcp",  # path
        "https://abc.trycloudflare.com?x=1",  # query
        "https://",  # no host
        "https://abc.trycloudflare.com:notaport",  # malformed port
        "https://abc.trycloudflare.com:70000",  # port out of range
    ],
)
def test_settings_reject_bad_urls(url):
    with pytest.raises(ValueError, match="KCAL_PUBLIC_URL"):
        HttpSettings.from_env({"KCAL_PUBLIC_URL": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@abc.trycloudflare.com",
        "https://user@abc.trycloudflare.com",
        # Would fail other checks too, whose messages echo the URL.
        "http://user:secret@abc.trycloudflare.com",
        "https://user:secret@abc.trycloudflare.com:notaport/mcp",
    ],
)
def test_settings_reject_credentials_without_echoing_them(url):
    with pytest.raises(ValueError, match="user name or password") as err:
        HttpSettings.from_env({"KCAL_PUBLIC_URL": url})
    assert "secret" not in str(err.value) and "user@" not in str(err.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
def test_settings_allow_http_on_localhost(url):
    assert HttpSettings.from_env({"KCAL_PUBLIC_URL": url}).public_url == url


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_settings_reject_bad_ports(port):
    with pytest.raises(ValueError, match="KCAL_PORT"):
        HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC, "KCAL_PORT": port})


def test_state_dir(tmp_path):
    assert state_dir({}) == Path.home() / ".kcal"
    assert garmin_token_store({"KCAL_STATE_DIR": str(tmp_path)}) == tmp_path / "garmin_tokens"


# --- HTTP transport ---------------------------------------------------------


@pytest.fixture
def client():
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC))
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def post(client, body, **headers):
    return client.post("/mcp", content=json.dumps(body), headers={**HEADERS, **headers})


def test_initialize_returns_plain_json_without_session(client):
    r = post(client, INIT)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in r.headers
    assert r.json()["result"]["serverInfo"]["name"] == "kcal"


def test_requests_stand_alone_without_initialize(client):
    # No MCP sessions: a fresh request works without an initialize first,
    # e.g. right after a server restart.
    r = post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "get_garmin_daily_stats" in names and len(names) == 6


def test_tool_call_over_http(client):
    r = post(client, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_kcal_server_info", "arguments": {}},
    })
    content = r.json()["result"]["content"]
    assert len(content) == 1
    assert json.loads(content[0]["text"])["stale"] is False


def test_localhost_host_allowed(client):
    assert post(client, INIT, Host="127.0.0.1:8000").status_code == 200


def test_foreign_host_rejected(client):
    # DNS rebinding protection stays on for anything but the public host.
    assert post(client, INIT, Host="evil.example").status_code == 421


def test_public_origin_allowed(client):
    assert post(client, INIT, Origin=PUBLIC).status_code == 200


def test_foreign_origin_rejected(client):
    assert post(client, INIT, Origin="https://evil.example").status_code == 403


def test_stdio_server_has_no_http_settings():
    assert mcp_server.mcp.settings.stateless_http is False


# --- entry point ------------------------------------------------------------


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server.FastMCP, "run",
        lambda self, transport="stdio", **_: calls.append((self, transport)),
    )
    monkeypatch.delenv("KCAL_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KCAL_PORT", raising=False)
    return calls


def test_main_defaults_to_stdio(runs):
    assert mcp_server.main([]) == 0
    assert runs == [(mcp_server.mcp, "stdio")]


def test_main_http_refuses_without_no_auth(runs, capsys):
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert runs == []
    assert "--no-auth" in capsys.readouterr().err


def test_main_http_reports_bad_settings(runs, capsys):
    assert mcp_server.main(["--http", "--no-auth"]) == 2
    assert runs == []
    assert "KCAL_PUBLIC_URL" in capsys.readouterr().err


def test_main_http_runs_streamable_http(runs, monkeypatch):
    monkeypatch.setenv("KCAL_PUBLIC_URL", PUBLIC)
    assert mcp_server.main(["--http", "--no-auth", "--port", "8123"]) == 0
    [(server, transport)] = runs
    assert transport == "streamable-http"
    assert server.settings.port == 8123
    assert server.settings.host == "127.0.0.1"
    assert server.settings.stateless_http and server.settings.json_response
