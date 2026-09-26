"""Order requests, validated before anything leaves the machine.

Payload of simpleCreateOrder, confirmed against a real account (TradeRepublicApi PR #42,
2026-08): numbers must be JSON numbers (strings -> "validation failed"), clientProcessId
must be a UUID, and warningsShown + acceptedWarnings are both required.

The answer never raises on the server side:
    accepted: {"status": "succeeded", "orderId": "<uuid>"}
    refused:  {"status": "failed", "message": "...", "error": {"code": ..., ...}?}
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from .errors import OrderRejected, OrderStateUnknown, OrderValidationError

Side = Literal["buy", "sell"]
Mode = Literal["market", "limit", "stopMarket"]
Expiry = Literal["gfd", "gtd", "gtc"]

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_EXCHANGE_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def home_exchange(isin: str) -> str:
    """Venue with the tightest spread at Trade Republic: crypto (XF... pseudo-ISINs) trades 24/7 on
    BHS (~0.8 % spread; B2C ~2 %), everything else on LS Exchange (LSX)."""
    return "BHS" if str(isin).strip().upper().startswith("XF") else "LSX"


def is_crypto(isin: str) -> bool:
    return str(isin).strip().upper().startswith("XF")


def isin_is_valid(isin: str) -> bool:
    """Format plus ISO 6166 check digit (Luhn over the letters converted to numbers)."""
    if not isinstance(isin, str) or not _ISIN_RE.match(isin):
        return False
    digits = "".join(str(int(ch, 36)) for ch in isin)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise OrderValidationError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise OrderValidationError(f"{name} must be a positive number, got {value!r}")
    return number


@dataclass(frozen=True)
class OrderRequest:
    isin: str
    side: Side
    size: float
    mode: Mode = "market"
    limit: float | None = None
    stop: float | None = None
    expiry: Expiry = "gfd"
    expiry_date: date | None = None
    exchange: str | None = None  # None = home_exchange(isin)
    sell_fractions: bool = False
    client_process_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        isin = self.isin.strip().upper() if isinstance(self.isin, str) else self.isin
        if not isin_is_valid(isin):
            raise OrderValidationError(f"invalid ISIN {self.isin!r}")
        object.__setattr__(self, "isin", isin)
        if self.side not in ("buy", "sell"):
            raise OrderValidationError("side must be 'buy' or 'sell'")
        if self.mode not in ("market", "limit", "stopMarket"):
            raise OrderValidationError("mode must be 'market', 'limit' or 'stopMarket'")
        object.__setattr__(self, "size", _number(self.size, "size"))

        if self.mode == "limit":
            object.__setattr__(self, "limit", _number(self.limit, "limit"))
            if self.stop is not None:
                raise OrderValidationError("a limit order takes no stop price")
        elif self.mode == "stopMarket":
            object.__setattr__(self, "stop", _number(self.stop, "stop"))
            if self.limit is not None:
                raise OrderValidationError("a stop-market order takes no limit price")
        else:
            if self.limit is not None or self.stop is not None:
                raise OrderValidationError("a market order takes neither limit nor stop")
            if self.expiry != "gfd":
                raise OrderValidationError("market orders only support expiry 'gfd'")

        if self.expiry not in ("gfd", "gtd", "gtc"):
            raise OrderValidationError("expiry must be 'gfd', 'gtd' or 'gtc'")
        if self.expiry == "gtd":
            if not isinstance(self.expiry_date, date):
                raise OrderValidationError("expiry 'gtd' needs an expiry_date")
            if self.expiry_date <= date.today():
                raise OrderValidationError("expiry_date must be in the future")
        elif self.expiry_date is not None:
            raise OrderValidationError("expiry_date is only allowed with expiry 'gtd'")

        if self.exchange is None:
            object.__setattr__(self, "exchange", home_exchange(isin))
        exchange = self.exchange.strip().upper() if isinstance(self.exchange, str) else ""
        if not _EXCHANGE_RE.match(exchange):
            raise OrderValidationError(f"invalid exchange {self.exchange!r}")
        object.__setattr__(self, "exchange", exchange)

        if self.sell_fractions and not (self.side == "sell" and self.mode == "market"):
            raise OrderValidationError("sell_fractions only applies to market sell orders")
        try:
            uuid.UUID(str(self.client_process_id))
        except ValueError:
            raise OrderValidationError("client_process_id must be a UUID") from None

    def to_payload(self) -> dict:
        # Never send "value": null in expiry - the server rejects the whole subscription.
        expiry: dict = {"type": self.expiry}
        if self.expiry == "gtd":
            expiry["value"] = self.expiry_date.isoformat()  # type: ignore[union-attr]
        parameters: dict = {
            "instrumentId": self.isin,
            "exchangeId": self.exchange,
            "expiry": expiry,
            "mode": self.mode,
            "size": self.size,
            "type": self.side,
        }
        if self.mode == "limit":
            parameters["limit"] = self.limit
        elif self.mode == "stopMarket":
            parameters["stop"] = self.stop
        else:
            parameters["sellFractions"] = self.sell_fractions
        return {
            "type": "simpleCreateOrder",
            "clientProcessId": self.client_process_id,
            "warningsShown": ["userExperience"],
            "acceptedWarnings": ["userExperience"],
            "parameters": parameters,
        }


@dataclass(frozen=True)
class OrderResult:
    status: Literal["accepted", "dry_run"]
    client_process_id: str
    order_id: str | None
    estimated_price: float | None
    estimated_value: float | None
    payload: dict
    response: Any = None
    fee_eur: float = 0.0
    budget_remaining_eur: float | None = None


def parse_order_response(response: Any, order: OrderRequest) -> str:
    """Return the orderId of an accepted order, raise otherwise.

    Note: "succeeded" means accepted, not executed or even resting - an accepted order
    can expire seconds later. Check orders()/timeline for the final state.
    """
    if not isinstance(response, dict):
        raise OrderStateUnknown("unexpected answer to the order", client_process_id=order.client_process_id)
    status = response.get("status")
    if status == "succeeded":
        order_id = response.get("orderId")
        if not order_id:
            raise OrderStateUnknown(
                "order accepted without an orderId", client_process_id=order.client_process_id
            )
        return str(order_id)
    if status == "failed":
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        message = response.get("message") or error.get("message") or "order refused"
        raise OrderRejected(str(message), code=error.get("code"), response=response)
    raise OrderStateUnknown(f"unexpected order status {status!r}", client_process_id=order.client_process_id)
