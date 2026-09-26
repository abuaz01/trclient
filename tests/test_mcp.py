"""MCP server tools, called in-process against the local fake Trade Republic server."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fakes import ISIN, make_client, server  # noqa: F401 (server is a fixture)
from trclient.mcp_server import TradingService, create_server
from trclient.store import MemoryStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def build(port, tmp_path, *, trading=True, store=None, clock=None, **limits):
    service = TradingService(
        lambda: make_client(port, tmp_path, trading=trading, store=store, **limits),
        preview_ttl=60,
        clock=clock or Clock(),
    )
    return create_server(service), service


async def call(mcp, name, **args):
    result = await mcp.call_tool(name, args)
    assert not result.is_error, result.content
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def test_preview_then_place(server, tmp_path):
    fake, port = server
    mcp, service = build(port, tmp_path)
    preview = await call(mcp, "preview_order", isin=ISIN, side="buy", size=1, order_type="limit", limit_price=100.0)
    assert preview["order"]["limit_price"] == 100.0 and preview["trading_enabled"] is True
    assert not [s for s in fake.subs if s["type"] == "simpleCreateOrder"]  # preview sends nothing

    placed = await call(mcp, "place_order", confirmation_id=preview["confirmation_id"])
    assert placed["status"] == "accepted" and placed["order_id"].startswith("1111")
    sent = [s for s in fake.subs if s["type"] == "simpleCreateOrder"]
    assert len(sent) == 1 and sent[0]["clientProcessId"] == preview["confirmation_id"]

    # single use
    with pytest.raises(ToolError, match="already used"):
        await mcp.call_tool("place_order", {"confirmation_id": preview["confirmation_id"]})
    await service.reset()


async def test_preview_expires(server, tmp_path):
    _, port = server
    clock = Clock()
    mcp, service = build(port, tmp_path, clock=clock)
    preview = await call(mcp, "preview_order", isin=ISIN, side="buy", size=1)
    clock.now += 61
    with pytest.raises(ToolError, match="expired"):
        await mcp.call_tool("place_order", {"confirmation_id": preview["confirmation_id"]})
    await service.reset()


async def test_trading_disabled_blocks_place_and_cancel(server, tmp_path):
    fake, port = server
    mcp, service = build(port, tmp_path, trading=False)
    preview = await call(mcp, "preview_order", isin=ISIN, side="buy", size=1)
    assert preview["trading_enabled"] is False
    with pytest.raises(ToolError, match="Trading is disabled"):
        await mcp.call_tool("place_order", {"confirmation_id": preview["confirmation_id"]})
    with pytest.raises(ToolError, match="Trading is disabled"):
        await mcp.call_tool("cancel_order", {"order_id": "11111111-2222-3333-4444-555555555555"})
    assert not [s for s in fake.subs if s["type"] in ("simpleCreateOrder", "cancelOrder")]
    await service.reset()


async def test_guardrail_and_validation_errors_reach_the_agent(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path, max_order_value_eur=50)
    with pytest.raises(ToolError, match="GuardrailViolation"):
        await mcp.call_tool("preview_order", {"isin": ISIN, "side": "buy", "size": 1})
    with pytest.raises(ToolError, match="invalid ISIN"):
        await mcp.call_tool("preview_order", {"isin": "DE0000000000", "side": "buy", "size": 1})
    with pytest.raises(ToolError, match="limit"):
        await mcp.call_tool("preview_order", {"isin": ISIN, "side": "buy", "size": 1, "order_type": "limit"})
    await service.reset()


async def test_not_logged_in_message_and_public_tools(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path, store=MemoryStore())
    quote = await call(mcp, "get_quote", isin=ISIN)
    assert quote["priceAsk"] == 100.5
    with pytest.raises(ToolError, match="trclient login"):
        await mcp.call_tool("get_orders", {})
    await service.reset()


async def test_read_tools(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path)
    assert await call(mcp, "get_orders") == {"orders": []}
    status = await call(mcp, "get_account_status")
    assert status["securities_account_number"] == "0123456789"
    assert status["trading_enabled"] is True and status["orders_submitted_today"] == 0
    await service.reset()


async def test_cancel_via_mcp(server, tmp_path):
    fake, port = server
    mcp, service = build(port, tmp_path)
    oid = "11111111-2222-3333-4444-555555555555"
    await call(mcp, "cancel_order", order_id=oid)
    assert [s for s in fake.subs if s["type"] == "cancelOrder"] == [{"type": "cancelOrder", "orderId": oid}]
    await service.reset()


async def test_tool_annotations():
    mcp = create_server(TradingService(lambda: None))
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["place_order"].annotations.destructive_hint is True
    assert tools["cancel_order"].annotations.destructive_hint is True
    for name, tool in tools.items():
        if name not in ("place_order", "cancel_order", "download_documents"):
            assert tool.annotations.read_only_hint is True, name


async def test_preview_shows_fees_and_budget(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path, budget_eur=50.0, max_order_value_eur=50)
    p = await call(mcp, "preview_order", isin=ISIN, side="buy", size=0.4)  # 0.4 * 100.5 = 40.2
    assert p["fee_eur"] == 1.0 and p["total_cost_eur"] == 41.2
    assert p["budget_remaining_before_eur"] == 50.0
    await call(mcp, "place_order", confirmation_id=p["confirmation_id"])
    budget = await call(mcp, "get_budget_status")
    assert budget["remaining_eur"] == 8.8 and budget["units_bought_by_agent"] == {ISIN: 0.4}
    with pytest.raises(ToolError, match="budget exceeded"):
        await mcp.call_tool("preview_order", {"isin": ISIN, "side": "buy", "size": 0.35})
    with pytest.raises(ToolError, match="only sell units"):
        await mcp.call_tool("preview_order", {"isin": ISIN, "side": "sell", "size": 1})
    sell = await call(mcp, "preview_order", isin=ISIN, side="sell", size=0.4)
    assert sell["net_proceeds_eur"] == round(0.4 * 99.5 - 1, 2)
    await service.reset()


async def test_wait_for_price(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path)
    hit = await call(mcp, "wait_for_price", isin=ISIN, above=101.0, timeout_seconds=5)
    assert hit["triggered"] is True and hit["price"] == 101.5
    miss = await call(mcp, "wait_for_price", isin=ISIN, below=1.0, timeout_seconds=1)
    assert miss["triggered"] is False
    await service.reset()


async def test_budget_reconciles_expired_orders(server, tmp_path):
    fake, port = server
    mcp, service = build(port, tmp_path, budget_eur=50.0, max_order_value_eur=50)
    p = await call(mcp, "preview_order", isin=ISIN, side="buy", size=0.4, order_type="limit", limit_price=100.0,
                   expiry="gtd", expiry_date="2099-01-01")
    placed = await call(mcp, "place_order", confirmation_id=p["confirmation_id"])
    assert (await call(mcp, "get_budget_status"))["remaining_eur"] == 8.8  # 0.4 x 100.5 + 1 fee
    fake.terminated_orders = [{"id": placed["order_id"], "status": "expired", "executions": []}]
    status = await call(mcp, "get_budget_status")
    assert status["remaining_eur"] == 50.0 and status["reconciled"][0]["status"] == "expired"
    await service.reset()


async def test_market_status(server, tmp_path):
    _, port = server
    mcp, service = build(port, tmp_path)
    status = await call(mcp, "get_market_status", isin=ISIN)
    assert status["exchange"] == "LSX" and "likely_open" in status
    await service.reset()


def test_home_exchange():
    from trclient.orders import OrderRequest, home_exchange

    assert home_exchange("XF000BTC0017") == "BHS" and home_exchange("DE0007164600") == "LSX"
    assert OrderRequest(isin="XF000BTC0017", side="buy", size=0.0006).exchange == "BHS"
    assert OrderRequest(isin="DE0007164600", side="buy", size=1).exchange == "LSX"
