"""Settings shared by the CLI and the MCP server, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PORT = 8000
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where kcal keeps Garmin tokens (and, in HTTP mode, auth state)."""
    value = env.get("KCAL_STATE_DIR")
    return Path(value).expanduser() if value else Path.home() / ".kcal"


def garmin_token_store(env: Mapping[str, str] = os.environ) -> Path:
    return state_dir(env) / "garmin_tokens"


@dataclass(frozen=True)
class HttpSettings:
    """How the HTTP MCP server is reached. It always listens on 127.0.0.1;
    the public URL is where clients reach it, e.g. through a tunnel.
    """

    public_url: str
    port: int = DEFAULT_PORT

    @property
    def public_host(self) -> str:
        """The Host header clients send, e.g. `abc.trycloudflare.com`."""
        return urlsplit(self.public_url).netloc

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        public_url: str | None = None,
        port: int | str | None = None,
    ) -> HttpSettings:
        """Explicit arguments (from CLI flags) win over KCAL_PUBLIC_URL and
        KCAL_PORT. Raises ValueError naming the setting that's wrong.
        """
        url = public_url or env.get("KCAL_PUBLIC_URL")
        if not url:
            raise ValueError(
                "HTTP mode needs the server's public URL: set KCAL_PUBLIC_URL "
                "or pass --public-url, e.g. https://abc.trycloudflare.com"
            )
        return cls(
            public_url=_check_url(url),
            port=_check_port(port if port is not None else env.get("KCAL_PORT")),
        )


def _check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # First, so no message below echoes credentials into the log.
    if "@" in parts.netloc:
        raise ValueError("KCAL_PUBLIC_URL must not contain a user name or password")
    local = parts.hostname in _LOCAL_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError(
            f"KCAL_PUBLIC_URL must be https:// (http:// only for localhost): {url!r}"
        )
    try:
        parts.port  # raises for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(f"KCAL_PUBLIC_URL has an invalid port: {url!r}") from None
    if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            f"KCAL_PUBLIC_URL must be just scheme and host, e.g. "
            f"https://abc.trycloudflare.com: {url!r}"
        )
    # Clients leave a default port out of the Host and Origin headers, which
    # must match exactly, so leave it out here too.
    netloc = parts.netloc
    if parts.port == {"https": 443, "http": 80}[parts.scheme]:
        netloc = netloc.rsplit(":", 1)[0]
    return f"{parts.scheme}://{netloc}"


def _check_port(value: int | str | None) -> int:
    if value is None or value == "":
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"KCAL_PORT must be a number: {value!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"KCAL_PORT must be between 1 and 65535: {port}")
    return port
