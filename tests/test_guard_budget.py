"""Fee and budget guardrails with a 50 EUR budget and 1 EUR per order."""

import pytest

from trclient.errors import GuardrailViolation
from trclient.guard import Guardrails, OrderGuard
from trclient.orders import OrderRequest

ISIN = "DE0007164600"
OTHER = "US0378331005"


def guard(tmp_path, **kw) -> OrderGuard:
    defaults = dict(trading_enabled=True, max_order_value_eur=50, order_fee_eur=1.0, max_fee_pct=0.03,
                    budget_eur=50.0, audit_path=tmp_path / "audit.jsonl")
    defaults.update(kw)
    return OrderGuard(Guardrails(**defaults))


def accept(g: OrderGuard, order: OrderRequest, value: float, order_id: str):
    g.audit("submitted", client_process_id=order.client_process_id, isin=order.isin, side=order.side,
            size=order.size, estimated_value=value, fee_eur=g.limits.order_fee_eur)
    g.audit("accepted", client_process_id=order.client_process_id, order_id=order_id)


def test_fee_share_too_high_blocks_small_buys(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(GuardrailViolation, match="order fee"):
        g.check(OrderRequest(isin=ISIN, side="buy", size=0.1), quote=100.0)  # 10 EUR -> 10 % fee
    r = g.check(OrderRequest(isin=ISIN, side="buy", size=0.4), quote=100.0)  # 40 EUR -> 2.5 %
    assert r.value_eur == 40.0 and r.fee_eur == 1.0 and r.total_eur == 41.0


def test_budget_includes_fee_and_compounds_after_sells(tmp_path):
    g = guard(tmp_path)
    buy = OrderRequest(isin=ISIN, side="buy", size=0.49, mode="limit", limit=100.0)
    assert g.check(buy, quote=100.0).budget_remaining_eur == 50.0
    accept(g, buy, 49.0, "o1")
    assert g.budget_status()["remaining_eur"] == 0.0  # 49 + 1 fee
    with pytest.raises(GuardrailViolation, match="budget exceeded"):
        g.check(OrderRequest(isin=OTHER, side="buy", size=0.35), quote=100.0)

    sell = OrderRequest(isin=ISIN, side="sell", size=0.49)
    r = g.check(sell, quote=110.0)
    assert r.net_proceeds_eur == pytest.approx(0.49 * 110 - 1)
    accept(g, sell, 0.49 * 110, "o2")
    status = g.budget_status()
    assert status["remaining_eur"] == pytest.approx(50 - 50 + (53.9 - 1), abs=0.01)  # profit compounds
    assert status["units_bought_by_agent"] == {}


def test_agent_can_only_sell_what_it_bought(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(GuardrailViolation, match="only sell units it bought"):
        g.check(OrderRequest(isin=OTHER, side="sell", size=1), quote=100.0)
    buy = OrderRequest(isin=ISIN, side="buy", size=0.4)
    accept(g, buy, 40.0, "o1")
    g.check(OrderRequest(isin=ISIN, side="sell", size=0.4), quote=100.0)
    with pytest.raises(GuardrailViolation):
        g.check(OrderRequest(isin=ISIN, side="sell", size=0.5), quote=100.0)


def test_exit_is_never_blocked_by_value_or_fee(tmp_path):
    g = guard(tmp_path)
    buy = OrderRequest(isin=ISIN, side="buy", size=0.45)
    accept(g, buy, 45.0, "o1")
    # position grew to 72 EUR (> max order value) -> selling must still be possible
    g.check(OrderRequest(isin=ISIN, side="sell", size=0.45), quote=160.0)
    # position shrank to 9 EUR (fee > 10 %) -> selling must still be possible
    g.check(OrderRequest(isin=ISIN, side="sell", size=0.45), quote=20.0)


def test_cancel_releases_budget(tmp_path):
    g = guard(tmp_path)
    buy = OrderRequest(isin=ISIN, side="buy", size=0.4)
    accept(g, buy, 40.0, "o1")
    assert g.budget_status()["remaining_eur"] == 9.0
    g.audit("cancel_answered", order_id="o1", response={"status": "succeeded"})
    assert g.budget_status()["remaining_eur"] == 50.0


def test_budget_since_resets_ledger(tmp_path):
    from datetime import date, timedelta

    g = guard(tmp_path)
    accept(g, OrderRequest(isin=ISIN, side="buy", size=0.4), 40.0, "o1")
    g2 = guard(tmp_path, budget_since=date.today() + timedelta(days=1))
    assert g2.budget_status()["remaining_eur"] == 50.0


def test_without_budget_selling_other_positions_is_allowed(tmp_path):
    g = guard(tmp_path, budget_eur=None)
    r = g.check(OrderRequest(isin=OTHER, side="sell", size=1), quote=100.0)
    assert r.budget_remaining_eur is None


def test_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TR_BUDGET_EUR", "50")
    monkeypatch.setenv("TR_ORDER_FEE_EUR", "1")
    monkeypatch.setenv("TR_MAX_FEE_PCT", "0.025")
    monkeypatch.setenv("TR_BUDGET_SINCE", "2026-09-25")
    lim = Guardrails.from_env()
    assert lim.budget_eur == 50 and lim.order_fee_eur == 1 and lim.max_fee_pct == 0.025
    assert str(lim.budget_since) == "2026-09-25"


def test_resting_sell_is_credited_only_when_filled(tmp_path):
    g = guard(tmp_path)
    buy = OrderRequest(isin=ISIN, side="buy", size=0.45)
    accept(g, buy, 45.0, "b1")
    stop = OrderRequest(isin=ISIN, side="sell", size=0.45, mode="stopMarket", stop=90.0, expiry="gtc")
    g.audit("submitted", client_process_id=stop.client_process_id, isin=ISIN, side="sell", size=0.45,
            mode="stopMarket", estimated_value=40.5, fee_eur=1.0)
    g.audit("accepted", client_process_id=stop.client_process_id, order_id="s1")
    status = g.budget_status()
    assert status["remaining_eur"] == 4.0          # stop pending: no money yet
    assert status["units_bought_by_agent"] == {}   # but units reserved -> no double sell
    g.audit("order_filled", order_id="s1", status="executed", filled_size=0.45)
    assert g.budget_status()["remaining_eur"] == pytest.approx(4.0 + 39.5)


def test_expired_orders_release_budget_and_partial_fills_stay(tmp_path):
    g = guard(tmp_path)
    accept(g, OrderRequest(isin=ISIN, side="buy", size=0.4), 40.0, "b1")
    assert g.open_order_ids() == {"b1"}
    g.audit("order_released", order_id="b1", status="expired", filled_size=0.0)
    assert g.budget_status()["remaining_eur"] == 50.0 and g.open_order_ids() == set()
    accept(g, OrderRequest(isin=ISIN, side="buy", size=0.4), 40.0, "b2")
    g.audit("order_released", order_id="b2", status="canceled", filled_size=0.1)  # 1/4 filled
    status = g.budget_status()
    assert status["remaining_eur"] == pytest.approx(50 - 10 - 1)
    assert status["units_bought_by_agent"] == {ISIN: 0.1}
