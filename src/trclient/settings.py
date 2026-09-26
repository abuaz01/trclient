"""Protocol constants. Each value that Trade Republic can invalidate can be overridden from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Build version of app.traderepublic.com (read from its public bundle, 2026-09).
# A stale value makes the login answer 426 CLIENT_VERSION_OUTDATED -> set TR_APP_VERSION.
DEFAULT_APP_VERSION = "2.2639.14"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
# Websocket protocol version. The server accepts 26-34 (measured 2026-08); 31 is what
# pytr and the TradeRepublicApi fixes run against the live API.
DEFAULT_WS_PROTOCOL_VERSION = 31
DEFAULT_WS_CLIENT_INFO = {
    "platformId": "webtrading",
    "platformVersion": "chrome - 94.0.4606",
    "clientId": "app.traderepublic.com",
    "clientVersion": "5582",
}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    api_host: str = "https://api.traderepublic.com"
    ws_url: str = "wss://api.traderepublic.com"
    app_version: str = field(default_factory=lambda: _env("TR_APP_VERSION", DEFAULT_APP_VERSION))
    user_agent: str = field(default_factory=lambda: _env("TR_USER_AGENT", DEFAULT_USER_AGENT))
    # The web frontend's HTTP client sends "X-Tr-Platform: web-pro" on the login calls.
    platform: str = "web-pro"
    locale: str = field(default_factory=lambda: _env("TR_LOCALE", "de"))
    ws_protocol_version: int = field(
        default_factory=lambda: int(_env("TR_WS_PROTOCOL_VERSION", str(DEFAULT_WS_PROTOCOL_VERSION)))
    )
    ws_client_info: dict = field(default_factory=lambda: dict(DEFAULT_WS_CLIENT_INFO))
    # tr_session is short-lived (~5 min). Refresh well before that.
    session_refresh_after: float = 240.0
    http_timeout: float = 20.0
    request_timeout: float = 15.0
    order_timeout: float = 20.0
