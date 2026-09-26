"""Web login and session handling (the flow app.traderepublic.com uses).

Login v2 (default, needs no AWS WAF token):
    POST api/v2/auth/web/login                                  {phoneNumber, pin} -> {processId}
    GET  api/v2/auth/web/login/processes/{processId}            -> {status, requiredAction?, expiresAt?}
    POST api/v2/auth/web/login/processes/{processId}/authenticator-verification   {code}
  requiredAction == AUTHENTICATOR_VERIFICATION -> a code is needed, otherwise the
  login is approved in the Trade Republic app and the process is polled.

Login v1 (code flow; only passes the AWS WAF with a browser-issued aws-waf-token):
    POST api/v1/auth/web/login                     {phoneNumber, pin} -> {processId, countdownInSeconds}
    POST api/v1/auth/web/login/{processId}/{code}
    POST api/v1/auth/web/login/{processId}/resend

After the login the server sets tr_session (short-lived), tr_refresh, tr_claims,
tr_device and tr_external_id. GET api/v1/auth/web/session exchanges tr_refresh for a
new tr_session.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import Cookie
from typing import Literal

import httpx

from .device import build_device_info, new_device_id
from .errors import APIError, LoginError, LoginTimeout, NotLoggedIn, SessionExpired
from .log import get_logger
from .settings import Settings
from .store import SessionStore, default_store

log = get_logger(__name__)

SESSION_COOKIE = "tr_session"
REFRESH_COOKIE = "tr_refresh"

LOGIN_ERRORS = {
    "NUMBER_INVALID": "The phone number is not valid (use international format, e.g. +4917012345678).",
    "AUTHENTICATION_ERROR": "Phone number or PIN is wrong.",
    "INVALID_VALUE": "Phone number or PIN is wrong.",
    "WEBTRADING_NOT_AVAILABLE": "Web trading is not available for this account.",
    "LOGIN_NOT_ALLOWED": "Trade Republic does not allow this login.",
    "PROCESS_GONE": "The login request expired. Please start again.",
    "ALREADY_PROCESSED": "The login request was rejected or has already been used.",
    "NOT_FOUND": "Trade Republic does not know this login request.",
    "TOO_MANY_REQUESTS": "Too many attempts. Please wait before trying again.",
    "VALIDATION_CODE_INVALID": "That code is not correct.",
    "VALIDATION_CODE_ALREADY_USED": "That code was already used.",
    "MISSING_REQUIRED_HEADER": "Trade Republic rejected the client headers (X-TR-* headers).",
    "CLIENT_VERSION_OUTDATED": "Client version outdated - set TR_APP_VERSION to the current web app version.",
}

_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CODE_RE = re.compile(r"^\d{4,8}$")


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"[\s()/-]", "", phone or "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not _PHONE_RE.match(cleaned):
        raise LoginError("Phone number must be in international format, e.g. +4917012345678.")
    return cleaned


@dataclass
class LoginChallenge:
    process_id: str
    flow: Literal["v2", "v1"]
    # "app": approve in the Trade Republic app, "authenticator": code from an authenticator app,
    # "code": code sent by Trade Republic (v1 flow).
    method: Literal["app", "authenticator", "code"]
    required_action: str | None
    expires_at: float
    countdown: int | None = None


def _parse_deadline(expires_at, countdown, fallback: int = 120) -> float:
    try:
        if isinstance(expires_at, (int, float)):
            return expires_at / 1000 if expires_at > 1e11 else float(expires_at)
        if expires_at:
            return datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return time.time() + int(countdown)
    except (TypeError, ValueError):
        return time.time() + fallback


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return errors[0].get("errorCode")
        return body.get("errorCode")
    return None


class TRSession:
    """Owns the HTTP client, the cookies and their persistence for one account."""

    def __init__(
        self,
        phone: str,
        *,
        store: SessionStore | None = None,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        read_only: bool = False,
    ) -> None:
        """read_only=True: use the stored cookies as they are - never refresh, never write the
        store. For observers (dashboard) that run next to the bot, so only one process rotates
        the session tokens."""
        self.read_only = read_only
        self.settings = settings or Settings()
        self.phone = normalize_phone(phone)
        self._store = store if store is not None else default_store()
        self._http = httpx.AsyncClient(
            base_url=self.settings.api_host,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.http_timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._refresh_lock = asyncio.Lock()
        self._last_refresh: float | None = None
        self._keepalive_task: asyncio.Task | None = None

        state = self._store.load(self.phone) or {}
        self.device_id: str = state.get("device_id") or new_device_id()
        self._device_info = build_device_info(self.device_id, self.settings.user_agent, self.settings.locale)
        self._restore_cookies(state.get("cookies") or [])

    # ------------------------------------------------------------------ helpers

    def _headers(self) -> dict[str, str]:
        return {
            "X-TR-Device-Info": self._device_info,
            "X-TR-App-Version": self.settings.app_version,
            "X-Tr-Platform": self.settings.platform,
            "Accept-Language": self.settings.locale,
        }

    def _cookie(self, name: str) -> str | None:
        for c in self._http.cookies.jar:
            if c.name == name and c.domain.endswith("traderepublic.com"):
                return c.value
        return None

    @property
    def has_session(self) -> bool:
        return bool(self._cookie(SESSION_COOKIE) or self._cookie(REFRESH_COOKIE))

    def cookie_header(self) -> str:
        return "; ".join(
            f"{c.name}={c.value}" for c in self._http.cookies.jar if c.domain.endswith("traderepublic.com")
        )

    def _serialize_cookies(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
            for c in self._http.cookies.jar
            if c.domain.endswith("traderepublic.com")
        ]

    def _restore_cookies(self, cookies: list[dict]) -> None:
        now = time.time()
        for item in cookies:
            if item.get("expires") and item["expires"] < now:
                continue
            domain = item.get("domain") or ".traderepublic.com"
            self._http.cookies.jar.set_cookie(
                Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith("."),
                    path=item.get("path") or "/",
                    path_specified=True,
                    secure=bool(item.get("secure", True)),
                    expires=item.get("expires"),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": ""},
                )
            )

    def _clear_cookies(self) -> None:
        self._http.cookies.clear()

    def _persist(self) -> None:
        if self.read_only:
            return
        self._store.save(
            self.phone,
            {
                "version": 1,
                "device_id": self.device_id,
                "cookies": self._serialize_cookies(),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _raise_login_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 405 and "awselb" in response.headers.get("server", "").lower():
            raise LoginError(
                "Blocked by the AWS WAF (missing or rejected aws-waf-token). Use the v2 login "
                "or pass a token taken from a real browser session.",
                code="WAF_BLOCKED",
                status=405,
            )
        code = _error_code(response)
        if code is None and response.status_code == 426:
            code = "CLIENT_VERSION_OUTDATED"
        message = LOGIN_ERRORS.get(code or "", f"Login failed: {code or 'HTTP ' + str(response.status_code)}.")
        raise LoginError(message, code=code, status=response.status_code)

    @staticmethod
    def _check_pin(pin: str) -> None:
        # Trade Republic now also allows passwords instead of the 4-digit PIN.
        if not isinstance(pin, str) or not pin or len(pin) > 256:
            raise LoginError("PIN/password must not be empty.")

    @staticmethod
    def _check_process_id(process_id) -> str:
        if not isinstance(process_id, str) or not _PROCESS_ID_RE.match(process_id):
            raise LoginError("Trade Republic answered without a valid processId.")
        return process_id

    # ------------------------------------------------------------------ login v2

    async def start_login(self, pin: str) -> LoginChallenge:
        """Start the v2 login. Triggers the second factor on Trade Republic's side."""
        self._check_pin(pin)
        self._clear_cookies()
        r = await self._http.post(
            "/api/v2/auth/web/login",
            json={"phoneNumber": self.phone, "pin": pin},
            headers=self._headers(),
        )
        self._raise_login_error(r)
        data = r.json()
        process_id = self._check_process_id(data.get("processId"))
        process = await self._get_process(process_id, quiet=True)
        action = process.get("requiredAction")
        log.info("Login started, requiredAction=%s", action)
        return LoginChallenge(
            process_id=process_id,
            flow="v2",
            method="authenticator" if action == "AUTHENTICATOR_VERIFICATION" else "app",
            required_action=action,
            expires_at=_parse_deadline(process.get("expiresAt"), data.get("countdownInSeconds")),
            countdown=data.get("countdownInSeconds"),
        )

    async def _get_process(self, process_id: str, quiet: bool = False) -> dict:
        r = await self._http.get(f"/api/v2/auth/web/login/processes/{process_id}", headers=self._headers())
        if r.status_code >= 400:
            if quiet:
                return {}
            self._raise_login_error(r)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    async def wait_for_app_confirmation(self, challenge: LoginChallenge, poll_interval: float = 2.0) -> None:
        """Poll until the login is approved in the Trade Republic app."""
        if challenge.flow != "v2":
            raise LoginError("Only v2 logins are confirmed in the app.")
        while True:
            process = await self._get_process(challenge.process_id)
            status = process.get("status")
            if status in ("CONFIRMED", "COMPLETED") or self._cookie(SESSION_COOKIE):
                break
            if status not in ("PENDING", None):
                raise LoginError(f"Login ended with status {status!r}.", code=status)
            if time.time() >= challenge.expires_at:
                raise LoginTimeout("The login was not confirmed in time.")
            await asyncio.sleep(poll_interval)
        await self._finish_login(challenge)

    async def complete_with_code(self, challenge: LoginChallenge, code: str) -> bool:
        """Submit a code (authenticator app, or the code of the v1 flow).

        Returns True when the login is complete. Returns False when Trade Republic
        additionally wants the login approved in the app - the web app does exactly this
        after a correct authenticator code. `challenge` is then switched to method "app";
        call wait_for_app_confirmation(challenge) next.
        """
        code = (code or "").strip()
        if not _CODE_RE.match(code):
            raise LoginError("The code must consist of 4 to 8 digits.")
        if challenge.flow == "v1":
            r = await self._http.post(
                f"/api/v1/auth/web/login/{challenge.process_id}/{code}", headers=self._headers()
            )
            self._raise_login_error(r)
            await self._finish_login(challenge)
            return True

        r = await self._http.post(
            f"/api/v2/auth/web/login/processes/{challenge.process_id}/authenticator-verification",
            json={"code": code},
            headers=self._headers(),
        )
        self._raise_login_error(r)
        process = await self._get_process(challenge.process_id, quiet=True)
        if self._cookie(SESSION_COOKIE) or process.get("status") in ("CONFIRMED", "COMPLETED"):
            await self._finish_login(challenge)
            return True
        # Code accepted, now the app approval (same as the web app's "confirm in app" step).
        challenge.method = "app"
        challenge.required_action = process.get("requiredAction")
        challenge.expires_at = _parse_deadline(process.get("expiresAt"), challenge.countdown)
        return False

    async def _finish_login(self, challenge: LoginChallenge) -> None:
        # The session cookie may arrive with the confirmation or with the next process read.
        for _ in range(5):
            if self._cookie(SESSION_COOKIE):
                break
            if challenge.flow != "v2":
                break
            await self._get_process(challenge.process_id, quiet=True)
            if not self._cookie(SESSION_COOKIE):
                await asyncio.sleep(1.0)
        if not self._cookie(SESSION_COOKIE) and self._cookie(REFRESH_COOKIE):
            # Only the refresh token arrived: exchange it for a session, as the web app does on load.
            r = await self._http.get("/api/v1/auth/web/session", headers=self._headers())
            log.debug("session exchange after login: HTTP %s", r.status_code)
        if not self._cookie(SESSION_COOKIE):
            names = sorted({c.name for c in self._http.cookies.jar}) or ["none"]
            raise LoginError(
                "The login was confirmed, but Trade Republic set no session cookie "
                f"(cookies received: {', '.join(names)}). Please report this."
            )
        self._last_refresh = time.monotonic()
        self._persist()
        log.info("Logged in.")

    # ------------------------------------------------------------------ login v1 (code flow)

    async def start_login_code_flow(self, pin: str, waf_token: str | None = None) -> LoginChallenge:
        """v1 login with a code sent by Trade Republic. Needs a browser-issued aws-waf-token."""
        self._check_pin(pin)
        self._clear_cookies()
        headers = self._headers()
        if waf_token:
            self._http.cookies.set("aws-waf-token", waf_token, domain=".traderepublic.com", path="/")
            headers["X-Aws-Waf-Token"] = waf_token
        r = await self._http.post(
            "/api/v1/auth/web/login", json={"phoneNumber": self.phone, "pin": pin}, headers=headers
        )
        self._raise_login_error(r)
        data = r.json()
        countdown = data.get("countdownInSeconds")
        return LoginChallenge(
            process_id=self._check_process_id(data.get("processId")),
            flow="v1",
            method="code",
            required_action=None,
            expires_at=_parse_deadline(None, None, fallback=300),
            countdown=int(countdown) if isinstance(countdown, (int, float)) else None,
        )

    async def resend_code(self, challenge: LoginChallenge) -> None:
        if challenge.flow != "v1":
            raise LoginError("Resending a code only exists in the v1 flow.")
        r = await self._http.post(
            f"/api/v1/auth/web/login/{challenge.process_id}/resend", headers=self._headers()
        )
        self._raise_login_error(r)

    # ------------------------------------------------------------------ session

    async def ensure_fresh(self, force: bool = False) -> None:
        """Exchange tr_refresh for a new tr_session when it is getting old."""
        if self.read_only:
            if not self._cookie(SESSION_COOKIE):
                raise NotLoggedIn("No active session cookie (the bot refreshes it while it runs).")
            return
        async with self._refresh_lock:
            if (
                not force
                and self._last_refresh is not None
                and time.monotonic() - self._last_refresh < self.settings.session_refresh_after
            ):
                return
            if not self.has_session:
                raise NotLoggedIn("Not logged in - run the login first.")
            r = await self._http.get("/api/v1/auth/web/session", headers=self._headers())
            if r.status_code in (401, 403):
                self._clear_cookies()
                self._last_refresh = None
                self._persist()
                raise SessionExpired("The session expired - please log in again.")
            if r.status_code >= 400:
                raise APIError(f"Session refresh failed with HTTP {r.status_code}.", status=r.status_code)
            self._last_refresh = time.monotonic()
            self._persist()
            log.debug("Session refreshed.")

    async def resume(self) -> bool:
        """Try to continue a stored session. Returns False when a new login is needed."""
        if not self.has_session:
            return False
        try:
            await self.ensure_fresh(force=True)
        except SessionExpired:
            return False
        return True

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated REST call. Only idempotent (GET) calls are retried after a refresh."""
        await self.ensure_fresh()
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        r = await self._http.request(method, path, headers=headers, **kwargs)
        if r.status_code == 401 and method.upper() == "GET" and not self.read_only:
            await self.ensure_fresh(force=True)
            r = await self._http.request(method, path, headers=headers, **kwargs)
        if r.status_code == 401:
            raise SessionExpired("The session is no longer accepted - please log in again.")
        if r.status_code >= 400:
            raise APIError(f"{method} {path} failed with HTTP {r.status_code}.", status=r.status_code)
        return r

    def start_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive())

    async def _keepalive(self) -> None:
        interval = max(5.0, self.settings.session_refresh_after / 4)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.ensure_fresh()
            except (SessionExpired, NotLoggedIn):
                log.warning("Session ended - keepalive stopped.")
                return
            except (httpx.HTTPError, APIError) as exc:
                log.warning("Session refresh failed (%s), will retry.", type(exc).__name__)

    async def logout(self) -> None:
        try:
            if self.has_session:
                await self._http.post("/api/v1/auth/web/logout", headers=self._headers())
        except httpx.HTTPError:
            pass
        finally:
            self._clear_cookies()
            self._last_refresh = None
            self._persist()

    async def aclose(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        await self._http.aclose()
