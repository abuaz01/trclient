"""End-to-end over a real local websocket server that speaks the Trade Republic protocol."""

import asyncio
import json
import os
import stat

import pytest

from fakes import ISIN, logged_in_store, make_client, server  # noqa: F401 (server is a fixture)
from trclient import OrderRequest
from trclient.errors import GuardrailViolation, OrderRejected, OrderStateUnknown, SubscriptionError
from trclient.store import MemoryStore


async def test_handshake_cookie_and_delta_stream(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path) as tr:
        ticks = []
        async with tr.stream_ticker(ISIN) as sub:
            async for t in sub:
                ticks.append(t["last"]["price"])
                if len(ticks) == 2:
                    break
    assert ticks == [100.0, 101.5]
    assert "tr_session=S2" in fake.cookies[0]  # refreshed before connecting
    hello = json.loads(fake.connects[0].split(" ", 2)[2])
    assert hello["clientId"] == "app.traderepublic.com" and hello["locale"] == "de"
    assert "token" not in fake.subs[0]  # auth is by cookie only
    await asyncio.sleep(0.05)
    assert fake.unsubs  # stream closed -> unsub sent


async def test_one_shot_unsubscribes(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path) as tr:
        assert await tr.orders() == {"orders": []}
        await asyncio.sleep(0.05)
    assert fake.unsubs == ["1"]


async def test_auth_error_is_retried_for_reads(server, tmp_path):
    fake, port = server
    fake.fail_auth_once = True
    async with make_client(port, tmp_path) as tr:
        assert await tr.orders() == {"orders": []}
    assert len(fake.connects) == 2


async def test_public_topic_without_login(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path, store=MemoryStore()) as tr:
        q = await tr.price_for_order(ISIN, "buy")
        assert q["priceAsk"] == 100.5
        with pytest.raises(SubscriptionError):
            await tr.ws.request({"type": "orders"}, 2)
    assert fake.cookies[0] == ""


async def test_dry_run_by_default(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path, trading=False) as tr:
        res = await tr.buy(ISIN, 1, execute=True)  # execute but trading disabled
    assert res.status == "dry_run" and res.estimated_price == 100.5
    assert not [s for s in fake.subs if s["type"] == "simpleCreateOrder"]


async def test_execute_order_and_audit(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path, trading=True) as tr:
        res = await tr.buy(ISIN, 1, execute=True, mode="limit", limit=100.0)
    assert res.status == "accepted" and res.order_id.startswith("1111")
    sent = [s for s in fake.subs if s["type"] == "simpleCreateOrder"][0]
    assert sent["parameters"]["limit"] == 100.0 and sent["clientProcessId"] == res.client_process_id
    audit = tmp_path / "audit.jsonl"
    events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
    assert events == ["submitted", "accepted"]
    assert stat.S_IMODE(os.stat(audit).st_mode) == 0o600


async def test_rejection(server, tmp_path):
    _, port = server
    async with make_client(port, tmp_path, trading=True) as tr:
        with pytest.raises(OrderRejected) as exc:
            await tr.buy(ISIN, 2, execute=True)
    assert exc.value.code == "cashMissing"


async def test_timeout_is_unknown_and_not_retried(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path, trading=True) as tr:
        with pytest.raises(OrderStateUnknown):
            await tr.buy(ISIN, 3, execute=True)
    assert len([s for s in fake.subs if s["type"] == "simpleCreateOrder"]) == 1


async def test_guardrails(server, tmp_path):
    fake, port = server
    async with make_client(port, tmp_path, trading=True, max_order_value_eur=150, max_orders_per_day=1) as tr:
        with pytest.raises(GuardrailViolation, match="exceeds"):
            await tr.buy(ISIN, 2, execute=True)  # 2 * 100.5 > 150
        with pytest.raises(GuardrailViolation, match="away from the quote"):
            await tr.buy(ISIN, 1, execute=True, mode="limit", limit=10.0)  # typo guard
        await tr.buy(ISIN, 1, execute=True)
        with pytest.raises(GuardrailViolation, match="daily order limit"):
            await tr.buy(ISIN, 1, execute=True)
    assert len([s for s in fake.subs if s["type"] == "simpleCreateOrder"]) == 1


async def test_allow_list(server, tmp_path):
    _, port = server
    async with make_client(port, tmp_path, trading=True, allowed_isins=frozenset({"US0378331005"})) as tr:
        with pytest.raises(GuardrailViolation, match="TR_ALLOWED_ISINS"):
            await tr.buy(ISIN, 1, execute=True)


async def test_cancel(server, tmp_path):
    fake, port = server
    oid = "11111111-2222-3333-4444-555555555555"
    async with make_client(port, tmp_path, trading=True) as tr:
        dry = await tr.cancel_order(oid)
        assert dry["status"] == "dry_run"
        assert await tr.cancel_order(oid, execute=True) == {"status": "succeeded"}
    assert [s for s in fake.subs if s["type"] == "cancelOrder"] == [{"type": "cancelOrder", "orderId": oid}]


async def test_order_placed_with_payload_shape(server, tmp_path):
    fake, port = server
    order = OrderRequest(isin=ISIN, side="sell", size=0.5, sell_fractions=True)
    async with make_client(port, tmp_path, trading=True) as tr:
        await tr.place_order(order, execute=True)
    sent = [s for s in fake.subs if s["type"] == "simpleCreateOrder"][0]
    assert sent == order.to_payload()


async def test_wrong_protocol_version(server, tmp_path, monkeypatch):
    _, port = server
    monkeypatch.setenv("TR_WS_PROTOCOL_VERSION", "21")
    from trclient.errors import TRConnectionError
    async with make_client(port, tmp_path, store=MemoryStore()) as tr:
        with pytest.raises(TRConnectionError, match="failed 34"):
            await tr.price_for_order(ISIN)
