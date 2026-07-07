"""Garmin Connect authentication with cached sessions."""

from __future__ import annotations

import os
from pathlib import Path

from garminconnect import Garmin

DEFAULT_TOKEN_STORE = Path.home() / ".kcal" / "garmin_tokens"


def _prompt_mfa() -> str:
    return input("Enter Garmin MFA code: ").strip()


def login(
    email: str | None = None,
    password: str | None = None,
    token_store: Path = DEFAULT_TOKEN_STORE,
) -> Garmin:
    """Log in to Garmin Connect, reusing a cached session when available.

    Credentials are only needed the first time (or after the cached session
    expires) - after that, `login()` resumes from `token_store`.
    """
    token_store.parent.mkdir(parents=True, exist_ok=True)

    email = email or os.environ.get("GARMIN_EMAIL")
    password = password or os.environ.get("GARMIN_PASSWORD")

    api = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    api.login(str(token_store))
    return api
