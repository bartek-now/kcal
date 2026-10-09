"""Garmin Connect authentication with cached sessions."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from garminconnect import Garmin

from kcal.settings import garmin_token_store


class GarminLoginError(Exception):
    """Logging in to Garmin failed: no usable cached session, and no way to
    get one without the owner (credentials or an MFA code).
    """


def garmin_profile_id(api: Garmin) -> int:
    """The account's stable ID: what KCAL_GARMIN_OWNER holds."""
    return int(api.client.connectapi("/userprofile-service/socialProfile")["profileId"])


def _prompt_mfa() -> str:
    return input("Enter Garmin MFA code: ").strip()


def login(
    email: str | None = None,
    password: str | None = None,
    token_store: Path | None = None,
    prompt_mfa: Callable[[], str] = _prompt_mfa,
) -> Garmin:
    """Log in to Garmin Connect, reusing a cached session when available.

    Credentials are only needed the first time (or after the cached session
    expires) - after that, `login()` resumes from `token_store` (default
    KCAL_STATE_DIR/garmin_tokens, i.e. ~/.kcal/garmin_tokens).
    """
    token_store = token_store or garmin_token_store()
    token_store.parent.mkdir(parents=True, exist_ok=True)

    email = email or os.environ.get("GARMIN_EMAIL")
    password = password or os.environ.get("GARMIN_PASSWORD")

    api = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    api.login(str(token_store))
    return api
