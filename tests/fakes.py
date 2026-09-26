"""Shared fakes: a local websocket server speaking the Trade Republic protocol."""

import json

import httpx
import pytest
from websockets.asyncio.server import serve

from trclient import Guardrails, Settings, TradeRepublic, TRSession
from trclient.store import MemoryStore

PHONE = "+4917012345678"
ISIN = "DE0007236101"


class FakeServer:
    def __init__(self):
        self.cookies: list[str] = []
        self.connects: list[str] = []
        self.subs: list[dict] = []
        self.unsubs: list[str] = []
        self.fail_auth_once = False
        self.terminated_orders: list[dict] = []

    async def handler(self, ws):
        self.cookies.append(ws.request.headers.get("Cookie", ""))
        hello = await ws.recv()
        self.connects.append(hello)
        if not hello.startswith("connect 31 "):
            await ws.send("failed 34")
            return
        await ws.send("connected")
        logged_in = "tr_session=" in self.cookies[-1]
        async for msg in ws:
            if msg.startswith("unsub "):
                self.unsubs.append(msg.split(" ")[1])
                continue
            _, sid, body = msg.split(" ", 2)
            payload = json.loads(body)
            self.subs.append(payload)
            t = payload["type"]
            if t == "ticker":
                prev = '{"last":{"price":100.0},"q":"x"}'
                await ws.send(f"{sid} A {prev}")
                await ws.send(f"{sid} D =17\t-5\t+101.5\t=10")
            elif t == "priceForOrder":
                await ws.send(f'{sid} A {{"currencyId":"EUR","price":100.0,"priceAsk":100.5,"priceBid":99.5}}')
            elif t in ("orders", "compactPortfolioByType") and (not logged_in or self.fail_auth_once):
                self.fail_auth_once = False
                await ws.send(f'{sid} E {{"errors":[{{"errorCode":"AUTHENTICATION_ERROR"}}]}}')
            elif t == "orders":
                items = self.terminated_orders if payload.get("terminated") else []
                await ws.send(f"{sid} A {json.dumps({'orders': items})}")
            elif t == "simpleCreateOrder":
                size = payload["parameters"]["size"]
                if size == 3:
                    continue  # never answers
                if size == 2:
                    await ws.send(f'{sid} A {{"status":"failed","message":"Insufficient funds for this operation.","error":{{"code":"cashMissing"}}}}')
                else:
                    await ws.send(f'{sid} A {{"status":"succeeded","orderId":"11111111-2222-3333-4444-555555555555"}}')
            elif t == "aggregateHistoryLight":
                aggs = [{"time": 1_700_000_000_000 + i * 86_400_000, "open": "100.0", "high": "101.0",
                         "low": "99.0", "close": "100.5", "volume": 0} for i in range(5)]
                await ws.send(f"{sid} A {json.dumps({'aggregates': aggs, 'resolution': 86_400_000})}")
            elif t == "timelineTransactions":
                page = TIMELINE_TX if payload.get("after") is None else {"items": [], "cursors": {}}
                await ws.send(f"{sid} A {json.dumps(page)}")
            elif t == "timelineActivityLog":
                await ws.send(f"{sid} A {json.dumps(TIMELINE_ACTIVITY)}")
            elif t == "timelineDetailV2":
                await ws.send(f"{sid} A {json.dumps(TIMELINE_DETAILS.get(payload['id'], {'sections': []}))}")
            elif t == "cancelOrder":
                await ws.send(f'{sid} A {{"status":"succeeded"}}')
            else:
                await ws.send(f'{sid} A {{"echo":{json.dumps(t)}}}')


def _event(id_, ts, title, subtitle=None, event_type=None):
    return {"id": id_, "timestamp": ts, "title": title, "subtitle": subtitle, "eventType": event_type,
            "action": {"type": "timelineDetail", "payload": id_}}


TIMELINE_TX = {"items": [
    _event("tx-1", "2026-09-20T10:00:00.000+0000", "SAP", "Kauforder", "TRADING_TRADE_EXECUTED"),
    _event("tx-old", "2020-01-01T10:00:00.000+0000", "Old", None, None),
], "cursors": {"after": "c1"}}
TIMELINE_ACTIVITY = {"items": [
    _event("act-1", "2026-09-01T08:00:00.000+0000", "Kontoauszug", "August 2026", "ACCOUNT_STATEMENT"),
    {"id": "act-2", "timestamp": "2026-09-02T08:00:00.000+0000", "title": "No details", "action": None},
], "cursors": {}}
TIMELINE_DETAILS = {
    "tx-1": {"sections": [{"type": "documents", "data": [
        {"id": "doc-trade-1", "title": "Abrechnung", "detail": "20.09.2026",
         "action": {"type": "browserModal", "payload": "https://storage.example/doc-trade-1.pdf?sig=x"}}]}]},
    "act-1": {"sections": [{"type": "header"}, {"type": "documents", "data": [
        {"id": "doc-statement-1", "title": "Kontoauszug", "detail": "01.09.2026",
         "action": {"type": "browserModal", "payload": {"path": "api/v1/documents/doc-statement-1"}}}]}]},
}
PDF = b"%PDF-1.4 fake"


def refresh_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.startswith("/api/v1/documents/"):
        return httpx.Response(200, content=PDF)
    if request.url.path == "/api/v2/auth/account":
        return httpx.Response(200, json={"securitiesAccountNumber": "0123456789"})
    return httpx.Response(200, headers=[("set-cookie", "tr_session=S2; Domain=.traderepublic.com; Path=/; Secure")])


@pytest.fixture
async def server():
    fake = FakeServer()
    async with serve(fake.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield fake, port


def logged_in_store() -> MemoryStore:
    store = MemoryStore()
    cookie = dict(domain=".traderepublic.com", path="/", expires=None, secure=True)
    store.save(PHONE, {"device_id": "d" * 128, "cookies": [
        dict(name="tr_session", value="S1", **cookie), dict(name="tr_refresh", value="R1", **cookie)]})
    return store


def make_client(port, tmp_path, *, store=None, trading=False, **limits) -> TradeRepublic:
    settings = Settings(ws_url=f"ws://127.0.0.1:{port}", order_timeout=0.5, request_timeout=2)
    session = TRSession(PHONE, store=store if store is not None else logged_in_store(), settings=settings,
                        transport=httpx.MockTransport(refresh_ok))
    guard = Guardrails(trading_enabled=trading, audit_path=tmp_path / "audit.jsonl", **limits)
    return TradeRepublic(session, guardrails=guard)
