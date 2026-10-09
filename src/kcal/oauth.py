"""OAuth 2.1 authorization server for the HTTP MCP server (design section 2).

The MCP SDK provides the endpoints (metadata, /register, /authorize, /token,
/revoke) and checks PKCE, redirect URIs, client IDs and expiry. This module
is the provider behind them: it stores registered clients and tokens, and
hands the owner's browser to the /login page, which calls
`complete_authorization()` once the owner has proved who they are.

Clients and tokens live in one SQLite file, so they survive restarts and a
move to another host is copying that file. Tokens are stored as SHA-256
hashes only. Pending authorizations and authorization codes are short-lived
and kept in memory; a restart just means logging in again.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "garmin:read"
ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
PENDING_SECONDS = 10 * 60


class KcalAccessToken(AccessToken):
    family: str  # shared by tokens issued together; revoking one revokes all


class KcalRefreshToken(RefreshToken):
    family: str


@dataclass
class PendingAuthorization:
    """An /authorize request waiting for the owner to log in."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float


def auth_settings(public_url: str) -> AuthSettings:
    return AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        # Refuse tokens issued for any other resource.
        validate_token_resource=True,
    )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class KcalOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, db_path: Path, public_url: str):
        self.public_url = public_url
        self.resource = f"{public_url}/mcp"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    info TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
                    family TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT,
                    subject TEXT
                );
                CREATE INDEX IF NOT EXISTS tokens_family ON tokens (family);
                """
            )
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # --- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            row = self._db.execute(
                "SELECT info FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO clients (client_id, info) VALUES (?, ?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    # --- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the owner's browser to /login."""
        if params.resource is not None and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError(
                "invalid_request", f"This server only issues tokens for {self.resource}"
            )
        now = time.time()
        self._pending = {k: p for k, p in self._pending.items() if p.expires_at > now}
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingAuthorization(
            client=client, params=params, expires_at=now + PENDING_SECONDS
        )
        return f"{self.public_url}/login?req={request_id}"

    def pending(self, request_id: str) -> PendingAuthorization | None:
        """The waiting request, for the login page to show and complete."""
        p = self._pending.get(request_id)
        return p if p is not None and p.expires_at > time.time() else None

    def complete_authorization(self, request_id: str, subject: str) -> str:
        """The owner logged in as `subject`: issue a code and return the URL
        to send their browser back to the client. Single use.
        """
        p = self._pending.pop(request_id, None)
        if p is None or p.expires_at <= time.time():
            raise KeyError("Unknown or expired authorization request")
        now = time.time()
        self._codes = {k: c for k, c in self._codes.items() if c.expires_at > now}
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=p.params.scopes or [SCOPE],
            expires_at=time.time() + CODE_SECONDS,
            client_id=p.client.client_id,
            code_challenge=p.params.code_challenge,
            redirect_uri=p.params.redirect_uri,
            redirect_uri_provided_explicitly=p.params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=subject,
        )
        return construct_redirect_uri(str(p.params.redirect_uri), code=code, state=p.params.state)

    def deny_authorization(self, request_id: str) -> str | None:
        """Send the browser back to the client with access_denied, if the
        request is still known.
        """
        p = self._pending.pop(request_id, None)
        if p is None:
            return None
        return construct_redirect_uri(
            str(p.params.redirect_uri), error="access_denied", state=p.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        return code if code is not None and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a replayed code finds nothing.
        self._codes.pop(authorization_code.code, None)
        return self._issue(
            client.client_id, authorization_code.scopes, authorization_code.subject
        )

    # --- tokens --------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> KcalRefreshToken | None:
        row = self._load(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return KcalRefreshToken(token=refresh_token, **row)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: KcalRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the old refresh token and its access token stop working.
        self._delete_family(refresh_token.family)
        return self._issue(client.client_id, scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> KcalAccessToken | None:
        row = self._load(token, "access")
        return KcalAccessToken(token=token, **row) if row is not None else None

    async def revoke_token(self, token: KcalAccessToken | KcalRefreshToken) -> None:
        self._delete_family(token.family)

    def revoke_all(self) -> None:
        """Sign every client out (clients stay registered)."""
        with self._lock:
            self._db.execute("DELETE FROM tokens")

    # --- storage -------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        now = int(time.time())
        family = secrets.token_urlsafe(16)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        rows = [
            (_hash(access), "access", now + ACCESS_TOKEN_SECONDS),
            (_hash(refresh), "refresh", now + REFRESH_TOKEN_SECONDS),
        ]
        with self._lock:
            self._db.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))
            self._db.executemany(
                "INSERT INTO tokens (token_hash, kind, family, client_id, scopes, "
                "expires_at, resource, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (h, kind, family, client_id, json.dumps(scopes), exp, self.resource, subject)
                    for h, kind, exp in rows
                ],
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _load(self, token: str, kind: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT family, client_id, scopes, expires_at, resource, subject "
                "FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, int(time.time())),
            ).fetchone()
        if row is None:
            return None
        family, client_id, scopes, expires_at, resource, subject = row
        return dict(
            family=family, client_id=client_id, scopes=json.loads(scopes),
            expires_at=expires_at, resource=resource, subject=subject,
        )

    def _delete_family(self, family: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tokens WHERE family = ?", (family,))

    def close(self) -> None:
        self._db.close()
