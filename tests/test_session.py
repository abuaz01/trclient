import base64
import json

import httpx
import pytest

from trclient.errors import APIError, LoginError, NotLoggedIn, SessionExpired
from trclient.session import TRSession, normalize_phone
from trclient.store import MemoryStore

PHONE = "+4917012345678"
COOKIES = [
    "tr_session=SESS1; Domain=.traderepublic.com; Path=/; Secure; HttpOnly",
    "tr_refresh=REFR1; Domain=.traderepublic.com; Path=/; Secure; HttpOnly",
]


class FakeTR:
    """Minimal stand-in for api.traderepublic.com."""

    def __init__(self, required_action=None, pending_polls=1, refresh_status=200, code_sets_cookies=True,
                 confirm_cookies=None):
        self.required_action = required_action
        self.code_sets_cookies = code_sets_cookies
        self.confirm_cookies = confirm_cookies if confirm_cookies is not None else COOKIES
        self.pending_polls = pending_polls
        self.refresh_status = refresh_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for h in ("X-TR-Device-Info", "X-TR-App-Version", "X-Tr-Platform"):
            if h not in request.headers:
                return httpx.Response(400, json={"errors": [{"errorCode": "MISSING_REQUIRED_HEADER"}]})
        if path == "/api/v2/auth/web/login":
            body = json.loads(request.content)
            if body["pin"] != "1234":
                return httpx.Response(401, json={"errors": [{"errorCode": "AUTHENTICATION_ERROR"}]})
            return httpx.Response(200, json={"processId": "proc-1", "countdownInSeconds": 60})
        if path == "/api/v2/auth/web/login/processes/proc-1":
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return httpx.Response(200, json={"status": "PENDING", "requiredAction": self.required_action})
            return httpx.Response(200, json={"status": "CONFIRMED"},
                                  headers=[("set-cookie", c) for c in self.confirm_cookies])
        if path == "/api/v2/auth/web/login/processes/proc-1/authenticator-verification":
            if json.loads(request.content)["code"] != "123456":
                return httpx.Response(400, json={"errors": [{"errorCode": "VALIDATION_CODE_INVALID"}]})
            if not self.code_sets_cookies:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={}, headers=[("set-cookie", c) for c in COOKIES])
        if path == "/api/v1/auth/web/session":
            if "tr_refresh=" not in request.headers.get("cookie", ""):
                return httpx.Response(401)
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status)
            return httpx.Response(
                200, headers=[("set-cookie", "tr_session=SESS2; Domain=.traderepublic.com; Path=/; Secure")]
            )
        if path == "/api/v1/auth/web/logout":
            return httpx.Response(200)
        if path == "/api/v2/auth/account":
            return httpx.Response(200, json={"securitiesAccountNumber": "123"})
        return httpx.Response(404)


def make(fake: FakeTR, store=None) -> TRSession:
    return TRSession(PHONE, store=store or MemoryStore(), transport=httpx.MockTransport(fake.handler))


def test_normalize_phone():
    assert normalize_phone("0049 170 1234-5678") == "+4917012345678"
    with pytest.raises(LoginError):
        normalize_phone("017012345678")


async def test_app_confirmation_login_persists_session_not_pin():
    fake, store = FakeTR(pending_polls=2), MemoryStore()
    s = make(fake, store)
    s_poll = 0.0
    ch = await s.start_login("1234")
    assert ch.method == "app" and ch.flow == "v2"
    await s.wait_for_app_confirmation(ch, poll_interval=s_poll)
    assert s.has_session
    raw = store.data[PHONE]
    assert "SESS1" in raw and "REFR1" in raw
    assert "1234" not in raw.replace(s.device_id, "")  # the PIN is never stored
    info = json.loads(base64.b64decode(fake.requests[0].headers["X-TR-Device-Info"]))
    assert info["stableDeviceId"] == s.device_id and len(s.device_id) == 128
    assert fake.requests[0].headers["X-Tr-Platform"] == "web-pro"
    await s.aclose()


async def test_authenticator_login():
    fake = FakeTR(required_action="AUTHENTICATOR_VERIFICATION")
    s = make(fake)
    ch = await s.start_login("1234")
    assert ch.method == "authenticator"
    with pytest.raises(LoginError) as exc:
        await s.complete_with_code(ch, "000000")
    assert exc.value.code == "VALIDATION_CODE_INVALID"
    assert await s.complete_with_code(ch, "123456") is True
    assert s.has_session
    await s.aclose()


@pytest.mark.parametrize("code", ["12", "12a456", "../../x", "123456789"])
async def test_code_is_validated_before_it_reaches_a_url(code):
    s = make(FakeTR(required_action="AUTHENTICATOR_VERIFICATION"))
    ch = await s.start_login("1234")
    with pytest.raises(LoginError):
        await s.complete_with_code(ch, code)
    await s.aclose()


async def test_wrong_pin_maps_error():
    s = make(FakeTR())
    with pytest.raises(LoginError) as exc:
        await s.start_login("9999")
    assert exc.value.code == "AUTHENTICATION_ERROR"
    await s.aclose()


async def test_waf_block_is_explained():
    s = TRSession(
        PHONE,
        store=MemoryStore(),
        transport=httpx.MockTransport(lambda r: httpx.Response(405, headers={"server": "awselb/2.0"})),
    )
    with pytest.raises(LoginError) as exc:
        await s.start_login("1234")
    assert exc.value.code == "WAF_BLOCKED"
    await s.aclose()


async def test_resume_from_store_and_refresh_updates_cookie():
    fake, store = FakeTR(pending_polls=0), MemoryStore()
    s = make(fake, store)
    await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    device = s.device_id
    await s.aclose()

    s2 = make(fake, store)
    assert s2.device_id == device  # stable device id across runs
    assert await s2.resume()
    assert "SESS2" in store.data[PHONE]
    r = await s2.request("GET", "/api/v2/auth/account")
    assert r.json()["securitiesAccountNumber"] == "123"
    await s2.aclose()


async def test_expired_refresh_wipes_cookies():
    fake, store = FakeTR(pending_polls=0), MemoryStore()
    s = make(fake, store)
    await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    fake.refresh_status = 401
    with pytest.raises(SessionExpired):
        await s.ensure_fresh(force=True)
    assert not s.has_session
    assert "SESS" not in store.data[PHONE]
    assert not await s.resume()
    await s.aclose()


async def test_refresh_server_error_is_not_expiry():
    fake = FakeTR(pending_polls=0)
    s = make(fake)
    await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    fake.refresh_status = 503
    with pytest.raises(APIError):
        await s.ensure_fresh(force=True)
    assert s.has_session
    await s.aclose()


async def test_not_logged_in():
    s = make(FakeTR())
    with pytest.raises(NotLoggedIn):
        await s.ensure_fresh()
    assert not await s.resume()
    await s.aclose()


async def test_logout_clears_store():
    fake, store = FakeTR(pending_polls=0), MemoryStore()
    s = make(fake, store)
    await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    await s.logout()
    assert not s.has_session and "SESS" not in store.data[PHONE]
    assert any(r.url.path == "/api/v1/auth/web/logout" for r in fake.requests)
    await s.aclose()


async def test_authenticator_code_then_app_approval():
    """Real behaviour (web app LoginFlow): a correct authenticator code is followed by the
    'confirm in app' step - the session only appears after the app approval."""
    fake = FakeTR(required_action="AUTHENTICATOR_VERIFICATION", pending_polls=3, code_sets_cookies=False)
    store = MemoryStore()
    s = make(fake, store)
    ch = await s.start_login("1234")
    assert ch.method == "authenticator"
    assert await s.complete_with_code(ch, "123456") is False
    assert ch.method == "app" and not s.has_session
    await s.wait_for_app_confirmation(ch, poll_interval=0)
    assert s.has_session and "SESS1" in store.data[PHONE]
    await s.aclose()


async def test_refresh_only_cookie_is_exchanged_for_session():
    refresh_only = ["tr_refresh=REFR1; Domain=.traderepublic.com; Path=/; Secure; HttpOnly"]
    fake = FakeTR(pending_polls=0, confirm_cookies=refresh_only)
    s = make(fake)
    await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    assert s._cookie("tr_session") == "SESS2"  # minted via /api/v1/auth/web/session
    await s.aclose()


async def test_missing_cookie_error_names_received_cookies():
    fake = FakeTR(pending_polls=0, confirm_cookies=["tr_claims=X; Domain=.traderepublic.com; Path=/"])
    s = make(fake)
    with pytest.raises(LoginError, match="cookies received: tr_claims"):
        await s.wait_for_app_confirmation(await s.start_login("1234"), poll_interval=0)
    await s.aclose()
