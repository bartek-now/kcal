"""The OAuth flow through the MCP SDK's real endpoints (design section 2)."""

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from kcal import mcp_server, oauth
from kcal.oauth import KcalOAuthProvider
from kcal.settings import HttpSettings

PUBLIC = "https://abc-def.trycloudflare.com"
RESOURCE = PUBLIC + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "mcp_auth.sqlite"


@pytest.fixture
def provider(db):
    p = KcalOAuthProvider(db, PUBLIC)
    yield p
    p.close()


@pytest.fixture
def client(provider):
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC), provider)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client):
    r = client.post("/register", json={
        "client_name": "Claude",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def start_authorize(client, client_id, challenge, **extra):
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "st4te", "resource": RESOURCE, **extra,
    }
    return client.get("/authorize", params=params, follow_redirects=False)


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def exchange_code(client, client_id, code, verifier):
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": RESOURCE,
    })


def connect(client, provider):
    """Register, authorize, 'log in' as the owner, exchange the code."""
    client_id = register(client)
    verifier, challenge = pkce()
    r = start_authorize(client, client_id, challenge)
    request_id = query(r.headers["location"])["req"]
    back = provider.complete_authorization(request_id, subject="owner")
    r = exchange_code(client, client_id, query(back)["code"], verifier)
    assert r.status_code == 200, r.text
    return client_id, r.json()


def mcp_call(client, token):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", content=json.dumps(INIT), headers=headers)


# --- discovery -----------------------------------------------------------------


def test_authorization_server_metadata(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == PUBLIC
    assert meta["authorization_endpoint"] == PUBLIC + "/authorize"
    assert meta["token_endpoint"] == PUBLIC + "/token"
    assert meta["registration_endpoint"] == PUBLIC + "/register"
    assert meta["revocation_endpoint"] == PUBLIC + "/revoke"
    assert meta["scopes_supported"] == [oauth.SCOPE]
    assert "S256" in meta["code_challenge_methods_supported"]


def test_protected_resource_metadata(client):
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == RESOURCE
    assert [s.rstrip("/") for s in meta["authorization_servers"]] == [PUBLIC]


def test_mcp_needs_a_token(client):
    r = mcp_call(client, None)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


def test_mcp_rejects_unknown_token(client):
    assert mcp_call(client, "not-a-token").status_code == 401


# --- the full flow ---------------------------------------------------------------


def test_authorize_sends_browser_to_login(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(PUBLIC + "/login?req=")
    pending = provider.pending(query(location)["req"])
    assert pending.client.client_name == "Claude"
    assert str(pending.params.redirect_uri) == REDIRECT


def test_full_flow_then_mcp_works(client, provider):
    _, tokens = connect(client, provider)
    assert tokens["token_type"].lower() == "bearer"
    assert tokens["expires_in"] == oauth.ACCESS_TOKEN_SECONDS
    assert tokens["scope"] == oauth.SCOPE
    r = mcp_call(client, tokens["access_token"])
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "kcal"


def test_completion_redirects_back_with_state(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    back = provider.complete_authorization(query(r.headers["location"])["req"], "owner")
    assert back.startswith(REDIRECT + "?")
    assert query(back)["state"] == "st4te" and query(back)["code"]


def test_denied_authorization_redirects_with_error(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    back = provider.deny_authorization(query(r.headers["location"])["req"])
    assert query(back) == {"error": "access_denied", "state": "st4te"}


def test_authorization_request_is_single_use(client, provider):
    client_id = register(client)
    request_id = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    provider.complete_authorization(request_id, "owner")
    with pytest.raises(KeyError):
        provider.complete_authorization(request_id, "owner")
    assert provider.deny_authorization(request_id) is None


def test_authorization_request_expires(client, provider, monkeypatch):
    client_id = register(client)
    request_id = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    later = time.time() + oauth.PENDING_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    assert provider.pending(request_id) is None
    with pytest.raises(KeyError):
        provider.complete_authorization(request_id, "owner")


def test_authorize_rejects_other_resource(client):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1], resource="https://evil.example/mcp")
    assert r.status_code == 302
    assert query(r.headers["location"])["error"] == "invalid_request"
    assert r.headers["location"].startswith(REDIRECT)


# --- code exchange ---------------------------------------------------------------


def authorized_code(client, provider):
    client_id = register(client)
    verifier, challenge = pkce()
    request_id = query(start_authorize(client, client_id, challenge).headers["location"])["req"]
    code = query(provider.complete_authorization(request_id, "owner"))["code"]
    return client_id, code, verifier


def test_code_is_single_use(client, provider):
    client_id, code, verifier = authorized_code(client, provider)
    assert exchange_code(client, client_id, code, verifier).status_code == 200
    r = exchange_code(client, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_needs_the_right_verifier(client, provider):
    client_id, code, _ = authorized_code(client, provider)
    r = exchange_code(client, client_id, code, pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_belongs_to_its_client(client, provider):
    _, code, verifier = authorized_code(client, provider)
    other = register(client)
    r = exchange_code(client, other, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_expires(client, provider, monkeypatch):
    client_id, code, verifier = authorized_code(client, provider)
    later = time.time() + oauth.CODE_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    r = exchange_code(client, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


# --- refresh, expiry, revocation ---------------------------------------------------


def refresh(client, client_id, refresh_token):
    return client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "resource": RESOURCE,
    })


def test_refresh_rotates_both_tokens(client, provider):
    client_id, old = connect(client, provider)
    r = refresh(client, client_id, old["refresh_token"])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != old["access_token"]
    assert new["refresh_token"] != old["refresh_token"]
    assert mcp_call(client, new["access_token"]).status_code == 200
    # The old pair is dead.
    assert mcp_call(client, old["access_token"]).status_code == 401
    r = refresh(client, client_id, old["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_token_belongs_to_its_client(client, provider):
    _, tokens = connect(client, provider)
    other = register(client)
    r = refresh(client, other, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_access_token_expires(client, provider, monkeypatch):
    _, tokens = connect(client, provider)
    later = time.time() + oauth.ACCESS_TOKEN_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert mcp_call(client, tokens["access_token"]).status_code == 401


def test_refresh_token_expires(client, provider, monkeypatch):
    client_id, tokens = connect(client, provider)
    later = time.time() + oauth.REFRESH_TOKEN_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    r = refresh(client, client_id, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_revoking_either_token_revokes_both(client, provider, which):
    client_id, tokens = connect(client, provider)
    # The SDK's /revoke form requires the field even for public clients.
    r = client.post("/revoke", data={"token": tokens[which], "client_id": client_id, "client_secret": ""})
    assert r.status_code == 200
    assert mcp_call(client, tokens["access_token"]).status_code == 401
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_revoke_all_signs_everyone_out(client, provider):
    _, a = connect(client, provider)
    _, b = connect(client, provider)
    provider.revoke_all()
    assert mcp_call(client, a["access_token"]).status_code == 401
    assert mcp_call(client, b["access_token"]).status_code == 401


def test_token_for_another_resource_is_refused(client, provider, db):
    _, tokens = connect(client, provider)
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE tokens SET resource = 'https://other.example/mcp'")
    assert mcp_call(client, tokens["access_token"]).status_code == 401


# --- storage -----------------------------------------------------------------------


def test_clients_and_tokens_survive_a_restart(client, provider, db):
    client_id, tokens = connect(client, provider)
    provider.close()
    reopened = KcalOAuthProvider(db, PUBLIC)
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC), reopened)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        assert mcp_call(c, tokens["access_token"]).status_code == 200
        assert refresh(c, client_id, tokens["refresh_token"]).status_code == 200
    reopened.close()


def test_tokens_are_stored_hashed(client, provider, db):
    _, tokens = connect(client, provider)
    raw = db.read_bytes()
    assert tokens["access_token"].encode() not in raw
    assert tokens["refresh_token"].encode() not in raw


def test_tokens_carry_the_owner(client, provider):
    _, tokens = connect(client, provider)
    import asyncio
    loaded = asyncio.run(provider.load_access_token(tokens["access_token"]))
    assert loaded.subject == "owner" and loaded.resource == RESOURCE


# --- the login page placeholder (built in step 4) ------------------------------------


def test_login_page_placeholder(client):
    r = client.get("/login?req=x")
    assert r.status_code == 503
    assert r.headers["cache-control"] == "no-store"


def test_provider_hides_tokens_from_other_clients(client, provider):
    # The SDK checks this too; the provider shouldn't rely on it.
    import asyncio
    client_id, tokens = connect(client, provider)
    other = asyncio.run(provider.get_client(register(client)))
    mine = asyncio.run(provider.get_client(client_id))
    assert asyncio.run(provider.load_refresh_token(other, tokens["refresh_token"])) is None
    assert asyncio.run(provider.load_refresh_token(mine, tokens["refresh_token"])) is not None
