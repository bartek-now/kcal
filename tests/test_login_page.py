"""The /login page (design sections 2 and 3), with a fake Garmin."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from kcal import cli, login_page, mcp_server
from kcal.oauth import KcalOAuthProvider
from kcal.settings import HttpSettings

PUBLIC = "https://abc-def.trycloudflare.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
OWNER = 12345
EMAIL, PASSWORD = "owner@example.com", "hunter2-secret"


class FakeGarmin:
    """Accounts: email -> (password, profile_id, mfa_code or None)."""

    accounts = {
        EMAIL: (PASSWORD, OWNER, None),
        "mfa@example.com": (PASSWORD, OWNER, "123456"),
        "other@example.com": (PASSWORD, 999, None),
    }

    def __init__(self):
        self.calls = []
        self.error = None  # raised by start() if set

    def start(self, email, password):
        self.calls.append(("start", email))
        if self.error:
            raise self.error
        account = self.accounts.get(email)
        if account is None or account[0] != password:
            raise GarminConnectAuthenticationError("401 Unauthorized")
        session = {"email": email, "profile": account[1], "mfa": account[2], "done": not account[2]}
        return session, account[2] is not None

    def finish(self, session, code):
        self.calls.append(("finish", code))
        if code != session["mfa"]:
            raise GarminConnectAuthenticationError("bad MFA code")
        session["done"] = True

    def profile_id(self, session):
        assert session["done"]
        return session["profile"]

    def save(self, session, token_store):
        token_store.mkdir(parents=True, exist_ok=True)
        (token_store / "saved.json").write_text(json.dumps(session))


@pytest.fixture
def garmin(monkeypatch):
    fake = FakeGarmin()
    monkeypatch.setattr(login_page, "GarminGateway", lambda: fake)
    return fake


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KCAL_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def provider(state):
    p = KcalOAuthProvider(state / "mcp_auth.sqlite", PUBLIC)
    yield p
    p.close()


@pytest.fixture
def server(garmin, provider):
    return mcp_server.build_server(HttpSettings(public_url=PUBLIC, garmin_owner=OWNER), provider)


@pytest.fixture
def client(server):
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def register(client, name="Claude"):
    r = client.post("/register", json={
        "client_name": name, "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    return r.json()["client_id"]


def start_authorization(client, name="Claude"):
    client_id = register(client, name)
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256", "state": "st4te",
    }, follow_redirects=False)
    return client_id, query(r.headers["location"])["req"]


def open_form(client, req=None):
    r = client.get("/login", params={"req": req} if req else {})
    return r, client.cookies.get(login_page.CSRF_COOKIE)


def sign_in(client, req=None, email=EMAIL, password=PASSWORD, csrf=None):
    if csrf is None:
        _, csrf = open_form(client, req)
    return client.post("/login", data={
        "csrf": csrf, "req": req or "", "email": email, "password": password, "action": "signin",
    }, follow_redirects=False)


def flow_id(body):
    return body.split('name="flow" value="')[1].split('"')[0]


# --- the form ---------------------------------------------------------------------


def test_form_shows_who_is_asking(client):
    _, req = start_authorization(client)
    r, csrf = open_form(client, req)
    assert r.status_code == 200
    assert "<b>Claude</b> wants read access" in r.text
    assert "<b>claude.ai</b>" in r.text  # where the owner will be sent back
    assert 'value="deny"' in r.text and csrf


def test_form_escapes_client_name(client):
    _, req = start_authorization(client, name="<script>alert(1)</script>")
    r, _ = open_form(client, req)
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_form_security_headers_and_cookie(client):
    _, req = start_authorization(client)
    r, _ = open_form(client, req)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "script-src" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_unknown_request_is_expired(client):
    r, _ = open_form(client, "bogus")
    assert r.status_code == 400 and "expired" in r.text


def test_direct_form_is_for_refreshing(client):
    r, _ = open_form(client)
    assert "refresh kcal's Garmin session" in r.text
    assert 'value="deny"' not in r.text


# --- signing in to connect a client ---------------------------------------------------


def test_owner_sign_in_completes_oauth(client, garmin, state):
    client_id, req = start_authorization(client)
    r = sign_in(client, req)
    assert r.status_code == 303
    back = r.headers["location"]
    assert back.startswith(REDIRECT) and query(back)["state"] == "st4te"
    assert (state / "garmin_tokens" / "saved.json").exists()
    # The code works, and the token belongs to the owner.
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query(back)["code"],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    }).json()
    r = client.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                    headers={"Authorization": f"Bearer {tokens['access_token']}",
                             "Content-Type": "application/json",
                             "Accept": "application/json, text/event-stream"})
    assert r.status_code == 200


def test_sign_in_resets_cached_session(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", object())
    _, req = start_authorization(client)
    sign_in(client, req)
    assert mcp_server._api is None  # next tool call loads the new tokens


def test_mfa_sign_in(client, garmin):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    assert r.status_code == 200 and "verification code" in r.text
    csrf = client.cookies.get(login_page.CSRF_COOKIE)
    r = client.post("/login", data={
        "csrf": csrf, "flow": flow_id(r.text), "code": " 123456 ", "action": "mfa",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(REDIRECT)


def test_wrong_mfa_code_starts_over(client, garmin):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    flow = flow_id(r.text)
    csrf = client.cookies.get(login_page.CSRF_COOKIE)
    r = client.post("/login", data={"csrf": csrf, "flow": flow, "code": "000000", "action": "mfa"})
    assert "didn't accept" in r.text
    r = client.post("/login", data={"csrf": csrf, "flow": flow, "code": "123456", "action": "mfa"})
    assert r.status_code == 400 and "expired" in r.text  # the flow was single use


def test_mfa_flow_expires(client, garmin, monkeypatch):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    later = time.time() + login_page.MFA_SECONDS + 1
    monkeypatch.setattr(login_page.time, "time", lambda: later)
    r = client.post("/login", data={
        "csrf": client.cookies.get(login_page.CSRF_COOKIE), "flow": flow,
        "code": "123456", "action": "mfa",
    })
    assert r.status_code == 400


def test_wrong_password(client, garmin, provider):
    _, req = start_authorization(client)
    r = sign_in(client, req, password="wrong-pass")
    assert r.status_code == 200 and "didn't accept" in r.text
    assert "wrong-pass" not in r.text  # never echoed
    assert f'value="{EMAIL}"' in r.text  # email kept for retrying
    assert provider.pending(req) is not None  # still waiting


def test_other_garmin_account_is_refused(client, garmin, state, provider):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="other@example.com")
    assert r.status_code == 200 and "isn't the one this server belongs to" in r.text
    assert not (state / "garmin_tokens").exists()
    assert provider.pending(req) is not None


def test_deny(client, provider):
    _, req = start_authorization(client)
    _, csrf = open_form(client, req)
    r = client.post("/login", data={"csrf": csrf, "req": req, "action": "deny"}, follow_redirects=False)
    assert r.status_code == 303
    assert query(r.headers["location"]) == {"error": "access_denied", "state": "st4te"}
    assert provider.pending(req) is None


def test_expired_request_at_sign_in(client, garmin, provider):
    _, req = start_authorization(client)
    _, csrf = open_form(client, req)
    provider.deny_authorization(req)  # gone
    r = sign_in(client, req, csrf=csrf)
    assert r.status_code == 400 and garmin.calls == []


# --- refreshing the Garmin session --------------------------------------------------


def test_direct_sign_in_refreshes_garmin_only(client, state, provider):
    r = sign_in(client)
    assert r.status_code == 200 and "Garmin session refreshed" in r.text
    assert (state / "garmin_tokens" / "saved.json").exists()
    assert provider._codes == {}  # no OAuth code issued


# --- CSRF ---------------------------------------------------------------------------


def test_post_without_csrf_cookie_is_refused(client, garmin):
    _, req = start_authorization(client)
    client.cookies.clear()
    r = client.post("/login", data={"csrf": "x", "req": req, "email": EMAIL,
                                    "password": PASSWORD, "action": "signin"})
    assert r.status_code == 400 and garmin.calls == []


def test_post_with_mismatched_csrf_is_refused(client, garmin):
    _, req = start_authorization(client)
    open_form(client, req)
    r = sign_in(client, req, csrf="not-the-cookie")
    assert r.status_code == 400 and garmin.calls == []


# --- rate limiting --------------------------------------------------------------------


def test_failures_are_capped_for_everyone(client, garmin):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, password="wrong")
    starts = len(garmin.calls)
    r = sign_in(client)  # even the right password
    assert r.status_code == 429 and "paused" in r.text
    assert len(garmin.calls) == starts  # Garmin never contacted


def test_wrong_account_counts_as_failure(client, garmin):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, email="other@example.com")
    assert sign_in(client).status_code == 429


def test_failures_expire_after_the_window(client, garmin, monkeypatch):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, password="wrong")
    later = time.time() + login_page.FAILURE_WINDOW_SECONDS + 1
    monkeypatch.setattr(login_page.time, "time", lambda: later)
    assert sign_in(client).status_code == 200


def test_unreachable_garmin_is_not_counted(client, garmin):
    garmin.error = GarminConnectConnectionError("timeout")
    for _ in range(login_page.MAX_FAILURES + 1):
        r = sign_in(client)
    assert "Couldn't reach Garmin" in r.text
    garmin.error = None
    assert "Garmin session refreshed" in sign_in(client).text


def test_garmin_rate_limit_is_counted(client, garmin):
    garmin.error = GarminConnectTooManyRequestsError("429")
    for _ in range(login_page.MAX_FAILURES):
        r = sign_in(client)
    assert "limiting sign-ins" in r.text
    garmin.error = None
    assert sign_in(client).status_code == 429


# --- the Garmin-expired tool error ------------------------------------------------------


@pytest.fixture
def failing_login(monkeypatch):
    def fail(**_):
        raise GarminConnectAuthenticationError("token expired")
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", fail)


def tool_error(server, name, args):
    with pytest.raises(ToolError) as err:
        asyncio.run(server.call_tool(name, args))
    return str(err.value)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_garmin_day", {"date": "2026-10-05"}),
        ("get_garmin_weight", {"date": "2026-10-05"}),
        ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "d"}}),
    ],
)
def test_expired_garmin_session_says_what_to_do(server, failing_login, tool, args):
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message  # what happened
    assert "Nothing is wrong with the connector" in message
    assert f"open {PUBLIC}/login" in message  # the full URL
    assert "sign in to Garmin" in message and "verification code" in message  # what to do there
    assert "ask again" in message and "no need to reconnect" in message  # what to do after
    assert "show this link to the user" in message  # don't just retry


def test_stdio_keeps_its_own_message(failing_login):
    message = tool_error(mcp_server.mcp, "get_garmin_day", {"date": "2026-10-05"})
    assert "token expired" in message and "/login" not in message


def test_session_rejected_mid_call(server, monkeypatch):
    class Expired:
        def get_sleep_data(self, cdate):
            raise GarminConnectAuthenticationError("401")

    monkeypatch.setattr(mcp_server, "_api", Expired())
    message = tool_error(server, "get_garmin_day", {"date": "d", "metrics": ["sleep_data", "hrv_data"]})
    assert f"open {PUBLIC}/login" in message
    assert mcp_server._api is None  # the dead session is dropped


# --- kcal whoami ----------------------------------------------------------------------


def test_whoami_prints_profile_id(monkeypatch, capsys):
    class Client:
        def connectapi(self, path):
            assert path == "/userprofile-service/socialProfile"
            return {"profileId": OWNER, "displayName": "owner-display"}

    class Api:
        client = Client()

    monkeypatch.setattr(cli, "login", lambda: Api())
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert f"profile ID:   {OWNER}" in out and "owner-display" in out
