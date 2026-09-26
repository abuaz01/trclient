from datetime import date, timedelta

import pytest

from trclient.errors import OrderRejected, OrderStateUnknown, OrderValidationError
from trclient.orders import OrderRequest, isin_is_valid, parse_order_response

SIEMENS = "DE0007236101"
APPLE = "US0378331005"


@pytest.mark.parametrize("isin", [SIEMENS, APPLE, "DE0007164600", "IE00B4L5Y983"])
def test_valid_isins(isin):
    assert isin_is_valid(isin)


@pytest.mark.parametrize("isin", ["DE0007236102", "US037833100", "de0007236101x", "", "XX0000000000"])
def test_invalid_isins(isin):
    assert not isin_is_valid(isin)


def test_limit_payload_matches_verified_shape():
    order = OrderRequest(isin=SIEMENS, side="buy", size=2, mode="limit", limit=100.5)
    p = order.to_payload()
    assert p["type"] == "simpleCreateOrder"
    assert p["warningsShown"] == ["userExperience"]
    assert p["acceptedWarnings"] == ["userExperience"]
    assert p["clientProcessId"] == order.client_process_id
    assert p["parameters"] == {
        "instrumentId": SIEMENS,
        "exchangeId": "LSX",
        "expiry": {"type": "gfd"},
        "mode": "limit",
        "size": 2.0,
        "type": "buy",
        "limit": 100.5,
    }
    # numbers must go out as JSON numbers, never strings
    assert isinstance(p["parameters"]["size"], float)


def test_market_sell_fractions_payload():
    p = OrderRequest(isin=APPLE, side="sell", size=0.25, sell_fractions=True).to_payload()["parameters"]
    assert p["mode"] == "market" and p["sellFractions"] is True
    assert "limit" not in p and "stop" not in p


def test_stop_market_payload():
    p = OrderRequest(isin=APPLE, side="sell", size=1, mode="stopMarket", stop=150).to_payload()["parameters"]
    assert p["stop"] == 150.0 and "limit" not in p and "sellFractions" not in p


def test_gtd_carries_date_and_other_expiries_no_value():
    d = date.today() + timedelta(days=10)
    p = OrderRequest(isin=APPLE, side="buy", size=1, mode="limit", limit=1, expiry="gtd", expiry_date=d).to_payload()
    assert p["parameters"]["expiry"] == {"type": "gtd", "value": d.isoformat()}
    p2 = OrderRequest(isin=APPLE, side="buy", size=1, mode="limit", limit=1, expiry="gtc").to_payload()
    assert p2["parameters"]["expiry"] == {"type": "gtc"}


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(size="1"),  # strings are refused by the server
        dict(size=0),
        dict(size=-1),
        dict(size=float("nan")),
        dict(size=True),
        dict(side="hold"),
        dict(mode="limit"),  # missing limit
        dict(mode="limit", limit=10, stop=5),
        dict(limit=10),  # market with limit
        dict(expiry="gtc"),  # market must be gfd
        dict(mode="limit", limit=1, expiry="gtd"),  # missing date
        dict(mode="limit", limit=1, expiry="gtd", expiry_date=date.today()),
        dict(mode="limit", limit=1, expiry_date=date.today() + timedelta(days=1)),
        dict(exchange="L S X"),
        dict(sell_fractions=True),  # buy
        dict(client_process_id="not-a-uuid"),
        dict(isin="DE0007236102"),
    ],
)
def test_validation_errors(kwargs):
    base = dict(isin=SIEMENS, side="buy", size=1)
    base.update(kwargs)
    with pytest.raises(OrderValidationError):
        OrderRequest(**base)


def test_isin_and_exchange_are_normalized():
    o = OrderRequest(isin=" de0007236101 ", side="buy", size=1, exchange="lsx")
    assert o.isin == SIEMENS and o.exchange == "LSX"


def test_each_order_gets_a_fresh_process_id():
    a = OrderRequest(isin=SIEMENS, side="buy", size=1)
    b = OrderRequest(isin=SIEMENS, side="buy", size=1)
    assert a.client_process_id != b.client_process_id


def test_parse_success():
    o = OrderRequest(isin=SIEMENS, side="buy", size=1)
    assert parse_order_response({"status": "succeeded", "orderId": "abc"}, o) == "abc"


@pytest.mark.parametrize(
    "response,code",
    [
        ({"status": "failed", "message": "Insufficient funds for this operation.",
          "error": {"code": "cashMissing", "details": {"exchangeId": "LSXCS"}}}, "cashMissing"),
        ({"status": "failed", "message": "limit orders do not support gtc expiry at lsx"}, None),
    ],
)
def test_parse_rejections(response, code):
    o = OrderRequest(isin=SIEMENS, side="buy", size=1)
    with pytest.raises(OrderRejected) as exc:
        parse_order_response(response, o)
    assert exc.value.code == code
    assert str(exc.value) == response["message"]


@pytest.mark.parametrize("response", [None, [], {"status": "weird"}, {"status": "succeeded"}])
def test_parse_unknown(response):
    o = OrderRequest(isin=SIEMENS, side="buy", size=1)
    with pytest.raises(OrderStateUnknown):
        parse_order_response(response, o)


def test_price_parsing():
    from trclient.client import _price

    assert _price(184.16) == 184.16
    assert _price("184.02") == 184.02
    for bad in (None, True, "abc", "nan", "-1", 0, {"price": 1}):
        assert _price(bad) is None
