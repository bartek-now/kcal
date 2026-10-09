"""The /login page: the owner proves who they are by logging in to Garmin
(docs/http-server-design.md, sections 2 and 3).

Two ways in, one form:

- From /authorize (`/login?req=<id>`): after the Garmin login it completes
  the OAuth flow and sends the browser back to the client.
- Opened directly (the link in the "Garmin session expired" tool error): it
  only refreshes the stored Garmin tokens.

Either way, only the configured owner's Garmin account gets through. The
password lives only in the request handler's memory; it is never logged,
stored or echoed back. Failed attempts reach Garmin, which can lock the
account, so they are capped for the whole server.
"""

from __future__ import annotations

import html
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from kcal.oauth import KcalOAuthProvider

# Failed attempts (wrong password or MFA code, wrong account, Garmin
# refusing) allowed per window, for everyone together: through the tunnel
# every request comes from 127.0.0.1, so per-IP limits mean nothing.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60 * 60
# How long the MFA step may take, and how many can be in progress.
MFA_SECONDS = 5 * 60
MAX_MFA_FLOWS = 20

CSRF_COOKIE = "kcal_csrf"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # No scripts at all. (No form-action: after sign-in the browser is
    # redirected to the client, which form-action would block.)
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


class GarminGateway:
    """The Garmin calls the page makes; tests swap in a fake."""

    def start(self, email: str, password: str) -> tuple[Any, bool]:
        """Log in with credentials. Returns (session, needs_mfa)."""
        api = Garmin(email=email, password=password, return_on_mfa=True)
        status, _ = api.login()
        return api, status == "needs_mfa"

    def finish(self, api: Any, code: str) -> None:
        api.resume_login(None, code)

    def profile_id(self, api: Any) -> int:
        return int(api.client.connectapi("/userprofile-service/socialProfile")["profileId"])

    def save(self, api: Any, token_store: Path) -> None:
        token_store.mkdir(parents=True, exist_ok=True)
        api.client.dump(str(token_store))


@dataclass
class _MfaFlow:
    api: Any
    request_id: str | None
    expires_at: float


class LoginPage:
    def __init__(
        self,
        oauth: KcalOAuthProvider,
        owner_id: int,
        token_store: Path,
        on_login: Callable[[], None],
        garmin: GarminGateway | None = None,
    ):
        self.oauth = oauth
        self.owner_id = owner_id
        self.token_store = token_store
        self.on_login = on_login  # new Garmin tokens saved: drop any cached session
        self.garmin = garmin or GarminGateway()
        self._failures: deque[float] = deque()
        self._mfa: dict[str, _MfaFlow] = {}

    # --- entry point -----------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        if request.method == "GET":
            return self._form(request, request.query_params.get("req") or None)
        form = await request.form()
        token = request.cookies.get(CSRF_COOKIE)
        if not token or not secrets.compare_digest(token, str(form.get("csrf", ""))):
            return self._page("Session expired", "<p>This form expired. Please start again.</p>", 400)
        request_id = str(form.get("req", "")) or None
        action = form.get("action")
        if action == "deny":
            return self._deny(request_id)
        if action == "mfa":
            return await self._mfa_step(request, str(form.get("flow", "")), str(form.get("code", "")))
        return await self._signin(
            request, request_id, str(form.get("email", "")), str(form.get("password", ""))
        )

    # --- steps -------------------------------------------------------------------

    async def _signin(self, request, request_id, email, password) -> Response:
        if request_id is not None and self.oauth.pending(request_id) is None:
            return self._expired_request()
        if locked := self._locked():
            return locked
        if not email or not password:
            return self._form(request, request_id, error="Enter your Garmin email and password.", email=email)
        try:
            api, needs_mfa = await anyio.to_thread.run_sync(self.garmin.start, email, password)
        except Exception as err:
            return self._failed(request, request_id, err, email)
        if needs_mfa:
            now = time.time()
            self._mfa = {k: f for k, f in self._mfa.items() if f.expires_at > now}
            while len(self._mfa) >= MAX_MFA_FLOWS:
                del self._mfa[min(self._mfa, key=lambda k: self._mfa[k].expires_at)]
            flow = secrets.token_urlsafe(32)
            self._mfa[flow] = _MfaFlow(api, request_id, now + MFA_SECONDS)
            return self._mfa_form(request, flow)
        return await self._finish(request, api, request_id)

    async def _mfa_step(self, request, flow_id, code) -> Response:
        flow = self._mfa.pop(flow_id, None)
        if flow is None or flow.expires_at <= time.time():
            return self._page(
                "Sign-in expired",
                "<p>The sign-in took too long. Please start again.</p>", 400,
            )
        if locked := self._locked():
            return locked
        try:
            await anyio.to_thread.run_sync(self.garmin.finish, flow.api, code.strip())
        except Exception as err:
            # Garmin's MFA state is single use: a wrong code means starting over.
            return self._failed(request, flow.request_id, err, "")
        return await self._finish(request, flow.api, flow.request_id)

    async def _finish(self, request, api, request_id) -> Response:
        try:
            profile_id = await anyio.to_thread.run_sync(self.garmin.profile_id, api)
        except Exception as err:
            return self._failed(request, request_id, err, "")
        if profile_id != self.owner_id:
            self._failures.append(time.time())
            return self._form(
                request, request_id,
                error="That Garmin account isn't the one this server belongs to.",
            )
        await anyio.to_thread.run_sync(self.garmin.save, api, self.token_store)
        self.on_login()
        if request_id is None:
            return self._page(
                "Garmin session refreshed",
                "<p>You're signed in to Garmin again. Go back to your chat and ask "
                "again; there's no need to reconnect the connector.</p>",
            )
        try:
            back = self.oauth.complete_authorization(request_id, subject=str(profile_id))
        except KeyError:
            return self._expired_request()
        return self._secure(RedirectResponse(back, status_code=303))

    def _deny(self, request_id) -> Response:
        back = self.oauth.deny_authorization(request_id) if request_id else None
        if back is None:
            return self._page("Cancelled", "<p>Nothing was connected. You can close this page.</p>")
        return self._secure(RedirectResponse(back, status_code=303))

    # --- failures ------------------------------------------------------------------

    def _locked(self) -> Response | None:
        now = time.time()
        while self._failures and self._failures[0] <= now - FAILURE_WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) < MAX_FAILURES:
            return None
        minutes = int((self._failures[0] + FAILURE_WINDOW_SECONDS - now) // 60) + 1
        return self._page(
            "Too many attempts",
            f"<p>There were too many failed sign-ins. To protect your Garmin account "
            f"from being locked, sign-in is paused for about {minutes} minute(s).</p>",
            429,
        )

    def _failed(self, request, request_id, err: Exception, email: str) -> Response:
        if isinstance(err, GarminConnectConnectionError):
            # Garmin unreachable: not the owner's fault, not counted.
            message = "Couldn't reach Garmin. Please try again in a moment."
        else:
            self._failures.append(time.time())
            if isinstance(err, GarminConnectTooManyRequestsError):
                message = "Garmin is limiting sign-ins right now. Please wait a few minutes."
            else:
                message = "Garmin didn't accept that. Check your email, password or code and try again."
        return self._form(request, request_id, error=message, email=email)

    def _expired_request(self) -> Response:
        return self._page(
            "Request expired",
            "<p>This connection request has expired or was already used. Start "
            "connecting again from Claude or ChatGPT.</p>",
            400,
        )

    # --- rendering ---------------------------------------------------------------------

    def _form(self, request, request_id, error: str = "", email: str = "") -> Response:
        if request_id is not None:
            pending = self.oauth.pending(request_id)
            if pending is None:
                return self._expired_request()
            name = pending.client.client_name or "An app"
            back_to = urlsplit(str(pending.params.redirect_uri)).netloc
            intro = (
                f"<p><b>{html.escape(name)}</b> wants read access to your Garmin "
                f"health data. Afterwards you'll be sent back to "
                f"<b>{html.escape(back_to)}</b>. If you didn't start this, deny it.</p>"
            )
        else:
            intro = "<p>Sign in to Garmin to refresh kcal's Garmin session.</p>"
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        hidden = self._hidden(csrf, req=request_id or "")
        body = (
            f"{intro}{self._error(error)}"
            f'<form method="post">{hidden}'
            f'<label>Garmin email<input name="email" type="email" autocomplete="username" '
            f'value="{html.escape(email)}" required></label>'
            f'<label>Password<input name="password" type="password" '
            f'autocomplete="current-password" required></label>'
            f'<button name="action" value="signin">Sign in</button></form>'
        )
        if request_id is not None:
            body += (
                f'<form method="post">{hidden}'
                f'<button class="secondary" name="action" value="deny">Deny</button></form>'
            )
        return self._with_csrf(self._page("Sign in to Garmin", body), csrf)

    def _mfa_form(self, request, flow: str) -> Response:
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        body = (
            "<p>Garmin sent you a verification code. Enter it to finish signing in.</p>"
            f'<form method="post">{self._hidden(csrf, flow=flow)}'
            f'<label>Code<input name="code" inputmode="numeric" autocomplete="one-time-code" '
            f'required autofocus></label>'
            f'<button name="action" value="mfa">Verify</button></form>'
        )
        return self._with_csrf(self._page("Verification code", body), csrf)

    @staticmethod
    def _hidden(csrf: str, **fields: str) -> str:
        items = {"csrf": csrf, **fields}
        return "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in items.items()
        )

    @staticmethod
    def _error(message: str) -> str:
        return f'<p class="error">{html.escape(message, quote=False)}</p>' if message else ""

    def _page(self, title: str, body: str, status: int = 200) -> Response:
        page = (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>kcal: {html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
        )
        return self._secure(HTMLResponse(page, status_code=status))

    @staticmethod
    def _secure(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    @staticmethod
    def _with_csrf(response: Response, csrf: str) -> Response:
        response.set_cookie(
            CSRF_COOKIE, csrf, httponly=True, secure=True, samesite="strict", path="/login",
            max_age=60 * 60,
        )
        return response


_CSS = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
main{max-width:26rem;margin:3rem auto;padding:1.5rem;background:#fff;border-radius:12px;
box-shadow:0 1px 4px #0002}
h1{font-size:1.3rem;margin-top:0}
label{display:block;margin:.8rem 0;font-weight:600}
input{display:block;width:100%;box-sizing:border-box;margin-top:.3rem;padding:.55rem;
font:inherit;border:1px solid #c5cad3;border-radius:8px}
button{width:100%;padding:.65rem;margin-top:.6rem;font:inherit;font-weight:600;border:0;
border-radius:8px;background:#1d4ed8;color:#fff;cursor:pointer}
button.secondary{background:#e5e7eb;color:#1d2330}
.error{color:#b91c1c;font-weight:600}
@media (prefers-color-scheme:dark){body{background:#111318;color:#e6e8ec}
main{background:#1b1e25;box-shadow:none}input{background:#111318;color:inherit;border-color:#3a3f4b}
button.secondary{background:#2a2f3a;color:#e6e8ec}.error{color:#f87171}}
"""
