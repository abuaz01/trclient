"""High-level async client: account data, market data and trading."""

from __future__ import annotations

import asyncio
import math
import pathlib
import time
import uuid
from datetime import date
from typing import Any

from .documents import Document, list_documents
from .documents import download as download_document
from .errors import (
    ConnectionLost,
    GuardrailViolation,
    OrderRejected,
    OrderStateUnknown,
    OrderValidationError,
    SubscriptionError,
    TRError,
)
from .guard import Guardrails, OrderGuard
from .log import get_logger
from .orders import OrderRequest, OrderResult, home_exchange, isin_is_valid, parse_order_response
from .session import TRSession
from .ws import Subscription, TRWebSocket

log = get_logger(__name__)

HISTORY_RANGES = ("1d", "5d", "1m", "3m", "1y", "max")
ASSET_TYPES = ("stock", "fund", "derivative", "crypto", "bond")


def _isin(isin: str) -> str:
    isin = isin.strip().upper()
    if not isin_is_valid(isin):
        raise OrderValidationError(f"invalid ISIN {isin!r}")
    return isin


def _exchange(exchange: str) -> str:
    exchange = exchange.strip().upper()
    if not exchange.isalnum() or not 2 <= len(exchange) <= 8:
        raise OrderValidationError(f"invalid exchange {exchange!r}")
    return exchange


def _price(value: Any) -> float | None:
    """Prices arrive as numbers (priceForOrder) or numeric strings (ticker)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        price = float(value)
    except ValueError:
        return None
    return price if math.isfinite(price) and price > 0 else None


class _Stream:
    def __init__(self, ws: TRWebSocket, payload: dict) -> None:
        self._ws = ws
        self._payload = payload
        self._sub: Subscription | None = None

    async def __aenter__(self) -> Subscription:
        self._sub = await self._ws.subscribe(self._payload)
        return self._sub

    async def __aexit__(self, *exc) -> None:
        if self._sub is not None:
            await self._sub.close()


class TradeRepublic:
    def __init__(
        self,
        session: TRSession,
        *,
        guardrails: Guardrails | None = None,
        ws: TRWebSocket | None = None,
    ) -> None:
        self.session = session
        self.settings = session.settings
        self.ws = ws or TRWebSocket(session)
        self.guard = OrderGuard(guardrails or Guardrails.from_env())
        self._sec_acc_no: str | None = None

    async def __aenter__(self) -> "TradeRepublic":
        if self.session.has_session:
            self.session.start_keepalive()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.ws.close()
        await self.session.aclose()

    # ------------------------------------------------------------------ plumbing

    async def _query(self, payload: dict, timeout: float | None = None) -> Any:
        """One-shot read. Retried once after a session refresh - never used for orders."""
        timeout = timeout or self.settings.request_timeout
        try:
            return await self.ws.request(payload, timeout)
        except SubscriptionError as exc:
            if not (exc.is_auth_error and self.session.has_session):
                raise
        except ConnectionLost:
            pass
        if self.session.has_session:
            await self.session.ensure_fresh(force=True)
        await self.ws.reconnect()
        return await self.ws.request(payload, timeout)

    def stream(self, payload: dict) -> "_Stream":
        """Live subscription. Use as `async with tr.stream(...) as sub: async for item in sub:` -
        leaving the block always unsubscribes."""
        return _Stream(self.ws, payload)

    # ------------------------------------------------------------------ account

    async def account(self) -> dict:
        return (await self.session.request("GET", "/api/v2/auth/account")).json()

    async def sec_acc_no(self) -> str:
        if self._sec_acc_no is None:
            number = (await self.account()).get("securitiesAccountNumber")
            if not number:
                raise TRError("the account settings contain no securitiesAccountNumber")
            self._sec_acc_no = str(number)
        return self._sec_acc_no

    async def portfolio(self) -> Any:
        return await self._query({"type": "compactPortfolioByType", "secAccNo": await self.sec_acc_no()})

    async def cash(self) -> Any:
        return await self._query({"type": "cash"})

    async def available_cash(self) -> Any:
        return await self._query({"type": "availableCash"})

    async def available_size(self, isin: str, exchange: str | None = None) -> Any:
        return await self._query(
            {"type": "availableSize", "parameters": {"exchangeId": _exchange(exchange or home_exchange(isin)), "instrumentId": _isin(isin)}}
        )

    async def orders(self, terminated: bool = False) -> Any:
        payload: dict = {"type": "orders"}
        if terminated:
            payload["terminated"] = True
        return await self._query(payload)

    async def timeline_transactions(self, after: str | None = None) -> Any:
        return await self._query({"type": "timelineTransactions", "after": after})

    async def timeline_activity_log(self, after: str | None = None) -> Any:
        """Postbox / activity timeline (account statements, tax documents, notices)."""
        return await self._query({"type": "timelineActivityLog", "after": after})

    async def timeline_detail(self, item_id: str) -> Any:
        return await self._query({"type": "timelineDetailV2", "id": item_id})

    async def documents(self, *, since: date | None = None, sources: tuple[str, ...] = ("transactions", "postbox"),
                        title_contains: str | None = None) -> list[Document]:
        """Documents attached to timeline events on or after `since` (see documents.py)."""
        return await list_documents(self, since=since, sources=sources, title_contains=title_contains)

    async def download_document(self, doc: Document, directory: str | pathlib.Path, *, overwrite: bool = False) -> dict:
        return await download_document(self, doc, pathlib.Path(directory), overwrite=overwrite)

    # ------------------------------------------------------------------ market data

    async def instrument(self, isin: str) -> Any:
        return await self._query({"type": "instrument", "id": _isin(isin)})

    async def stock_details(self, isin: str) -> Any:
        return await self._query({"type": "stockDetails", "id": _isin(isin)})

    async def ticker(self, isin: str, exchange: str | None = None) -> Any:
        return await self._query({"type": "ticker", "id": f"{_isin(isin)}.{_exchange(exchange or home_exchange(isin))}"})

    def stream_ticker(self, isin: str, exchange: str | None = None) -> "_Stream":
        return self.stream({"type": "ticker", "id": f"{_isin(isin)}.{_exchange(exchange or home_exchange(isin))}"})

    async def performance(self, isin: str, exchange: str | None = None) -> Any:
        return await self._query({"type": "performance", "id": f"{_isin(isin)}.{_exchange(exchange or home_exchange(isin))}"})

    async def history(self, isin: str, range_: str = "1y", exchange: str | None = None) -> Any:
        # No "resolution": with it the server silently drops the subscription.
        if range_ not in HISTORY_RANGES:
            raise OrderValidationError(f"range must be one of {HISTORY_RANGES}")
        return await self._query(
            {"type": "aggregateHistoryLight", "range": range_, "id": f"{_isin(isin)}.{_exchange(exchange or home_exchange(isin))}"}
        )

    async def search(
        self, query: str, asset_type: str = "stock", page: int = 1, page_size: int = 20, jurisdiction: str | None = None
    ) -> Any:
        if asset_type not in ASSET_TYPES:
            raise OrderValidationError(f"asset_type must be one of {ASSET_TYPES}")
        filters = [{"key": "type", "value": asset_type}]
        if jurisdiction:
            filters.append({"key": "jurisdiction", "value": jurisdiction.upper()})
        return await self._query(
            {"type": "neonSearch", "data": {"q": query, "page": page, "pageSize": page_size, "filter": filters}}
        )

    async def news(self, isin: str) -> Any:
        return await self._query({"type": "neonNews", "isin": _isin(isin)})

    async def price_for_order(self, isin: str, side: str = "buy", exchange: str | None = None) -> dict:
        if side not in ("buy", "sell"):
            raise OrderValidationError("side must be 'buy' or 'sell'")
        return await self._query(
            {
                "type": "priceForOrder",
                "parameters": {"exchangeId": _exchange(exchange or home_exchange(isin)), "instrumentId": _isin(isin), "type": side},
            }
        )

    async def order_cost(self, order: OrderRequest) -> str:
        """Cost transparency document (fees) for an order, as Trade Republic shows it before ordering."""
        params: dict = {
            "instrumentId": order.isin,
            "exchangeId": order.exchange,
            "mode": order.mode,
            "type": order.side,
            "size": order.size,
            "sellFractions": str(order.sell_fractions).lower(),
        }
        if order.mode == "limit":
            params["limit"] = order.limit
        if order.mode == "stopMarket":
            params["stop"] = order.stop
        return (await self.session.request("GET", "/api/v1/user/costtransparency", params=params)).text

    async def market_status(self, isin: str, exchange: str | None = None) -> dict:
        """Is the instrument tradable right now? Uses Trade Republic's tradingStatus when logged in,
        plus a quote-freshness heuristic that also works without login."""
        tick = await self.ticker(isin, exchange)
        if not isinstance(tick, dict):
            tick = {}
        last = tick.get("last") or {}
        bid = _price((tick.get("bid") or {}).get("price"))
        ask = _price((tick.get("ask") or {}).get("price"))
        stamp = last.get("time")
        age = round(time.time() - stamp / 1000, 1) if isinstance(stamp, (int, float)) else None
        status: dict = {
            "isin": _isin(isin),
            "exchange": _exchange(exchange or home_exchange(isin)),
            "last_price": _price(last.get("price")),
            "bid": bid,
            "ask": ask,
            "spread_percent": round((ask - bid) / ask * 100, 3) if bid and ask else None,
            "quote_age_seconds": age,
            "quality": tick.get("qualityId"),
            "likely_open": age is not None and age < 300 and bool(bid and ask),
            "trading_status": None,
        }
        if self.session.has_session:
            try:
                status["trading_status"] = await self._query(
                    {"type": "tradingStatus", "isin": status["isin"], "exchangeId": status["exchange"],
                     "currencyCode": "EUR"},
                    timeout=8,
                )
            except (SubscriptionError, TimeoutError):
                pass
        status["note"] = ("likely_open is a heuristic (fresh two-sided quote in the last 5 minutes). "
                          "LSX quotes stocks/ETFs Mon-Fri 07:30-23:00 CET; crypto trades 24/7.")
        return status

    async def wait_for_market(self, isin: str, exchange: str | None = None, timeout_seconds: float = 55) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = await self.market_status(isin, exchange)
            if status["likely_open"] or time.monotonic() + 15 > deadline:
                return {"open": status["likely_open"], "status": status}
            await asyncio.sleep(15)

    async def wait_for_price(
        self,
        isin: str,
        *,
        above: float | None = None,
        below: float | None = None,
        field: str = "last",
        exchange: str | None = None,
        timeout_seconds: float = 55,
    ) -> dict:
        """Watch the live ticker until the price crosses a threshold or the timeout ends."""
        if above is None and below is None:
            raise OrderValidationError("give 'above' and/or 'below'")
        if field not in ("last", "bid", "ask"):
            raise OrderValidationError("field must be last, bid or ask")
        started = time.monotonic()
        latest = None
        try:
            async with self.stream_ticker(isin, exchange) as ticks:
                async with asyncio.timeout(timeout_seconds):
                    async for tick in ticks:
                        latest = _price((tick.get(field) or {}).get("price"))
                        if latest is None:
                            continue
                        if (above is not None and latest >= above) or (below is not None and latest <= below):
                            return {"triggered": True, "price": latest, "field": field,
                                    "waited_seconds": round(time.monotonic() - started, 1)}
        except TimeoutError:
            pass
        return {"triggered": False, "price": latest, "field": field,
                "waited_seconds": round(time.monotonic() - started, 1)}

    async def reconcile_budget(self) -> list[dict]:
        """Release the budget of the agent's orders that ended without (full) execution.

        Uses the terminated orders list: canceled/expired/rejected orders free their
        reservation (partly filled ones keep the filled part); executed ones are marked final.
        """
        open_ids = self.guard.open_order_ids()
        if not open_ids or not self.session.has_session:
            return []
        data = await self.orders(terminated=True)
        items = data.get("orders") if isinstance(data, dict) else data
        changes = []
        for order in items or []:
            if not isinstance(order, dict) or str(order.get("id")) not in open_ids:
                continue
            status = str(order.get("status") or "").lower()
            filled = 0.0
            for execution in order.get("executions") or []:
                try:
                    filled += float((execution or {}).get("size") or 0)
                except (TypeError, ValueError, AttributeError):
                    filled = -1.0
                    break
            if filled < 0:
                continue  # unknown execution format: keep the conservative reservation
            if status in ("canceled", "cancelled", "expired", "rejected"):
                self.guard.audit("order_released", order_id=order["id"], status=status, filled_size=filled)
                changes.append({"order_id": order["id"], "status": status, "filled_size": filled})
            elif status in ("executed", "filled"):
                self.guard.audit("order_filled", order_id=order["id"], status=status, filled_size=filled)
                changes.append({"order_id": order["id"], "status": status, "filled_size": filled})
        return changes

    # ------------------------------------------------------------------ trading

    async def quote(self, isin: str, side: str, exchange: str | None = None) -> float | None:
        """Best price for an order on this side (ask for buys, bid for sells), EUR only."""
        q = await self.price_for_order(isin, side, exchange)
        if not isinstance(q, dict):
            return None
        if q.get("currencyId") not in (None, "EUR"):
            raise GuardrailViolation(f"instrument is quoted in {q.get('currencyId')}, only EUR is supported")
        for key in ("priceAsk" if side == "buy" else "priceBid", "price"):
            price = _price(q.get(key))
            if price is not None:
                return price
        return None

    async def place_order(self, order: OrderRequest, *, execute: bool = False) -> OrderResult:
        """Validate, check guardrails and - only with execute=True and TR_TRADING_ENABLED=1 - send.

        Never retried automatically: on a timeout the state is unknown (OrderStateUnknown),
        check orders() before sending again.
        """
        try:
            quote = await self.quote(order.isin, order.side, order.exchange)
        except (SubscriptionError, TimeoutError) as exc:
            if order.mode == "market":
                raise GuardrailViolation("no quote available for a market order") from exc
            quote = None
        checked = self.guard.check(order, quote)
        value = checked.value_eur
        payload = order.to_payload()
        base = {
            "client_process_id": order.client_process_id,
            "isin": order.isin,
            "side": order.side,
            "mode": order.mode,
            "size": order.size,
            "limit": order.limit,
            "stop": order.stop,
            "exchange": order.exchange,
            "quote": quote,
            "estimated_value": value,
            "fee_eur": checked.fee_eur,
        }

        if not (execute and self.guard.limits.trading_enabled):
            self.guard.audit("dry_run", **base)
            return OrderResult("dry_run", order.client_process_id, None, quote, value, payload,
                               fee_eur=checked.fee_eur, budget_remaining_eur=checked.budget_remaining_eur)

        if not self.session.has_session:
            raise TRError("not logged in")
        # Fresh session and a connection opened with it, so the order is not refused for auth.
        await self.session.ensure_fresh()
        if self.ws.age > self.settings.session_refresh_after:
            await self.ws.reconnect()

        self.guard.audit("submitted", **base)
        try:
            response = await self.ws.request(payload, self.settings.order_timeout)
        except (TimeoutError, ConnectionLost) as exc:
            self.guard.audit("unknown", client_process_id=order.client_process_id, reason=type(exc).__name__)
            raise OrderStateUnknown(
                "no answer to the order - it may or may not have been placed; check orders()",
                client_process_id=order.client_process_id,
            ) from exc
        except SubscriptionError as exc:
            self.guard.audit("rejected", client_process_id=order.client_process_id, error=exc.error)
            raise OrderRejected(str(exc), code=(exc.codes or [None])[0], response=exc.error) from exc

        try:
            order_id = parse_order_response(response, order)
        except OrderRejected as exc:
            self.guard.audit("rejected", client_process_id=order.client_process_id, code=exc.code, message=str(exc))
            raise
        except OrderStateUnknown:
            self.guard.audit("unknown", client_process_id=order.client_process_id, response=response)
            raise
        self.guard.audit("accepted", client_process_id=order.client_process_id, order_id=order_id)
        return OrderResult("accepted", order.client_process_id, order_id, quote, value, payload, response,
                           fee_eur=checked.fee_eur, budget_remaining_eur=checked.budget_remaining_eur)

    async def buy(self, isin: str, size: float, *, execute: bool = False, **kwargs) -> OrderResult:
        return await self.place_order(OrderRequest(isin=isin, side="buy", size=size, **kwargs), execute=execute)

    async def sell(self, isin: str, size: float, *, execute: bool = False, **kwargs) -> OrderResult:
        return await self.place_order(OrderRequest(isin=isin, side="sell", size=size, **kwargs), execute=execute)

    async def cancel_order(self, order_id: str, *, execute: bool = False) -> Any:
        try:
            order_id = str(uuid.UUID(str(order_id)))
        except ValueError:
            raise OrderValidationError("order_id must be a UUID") from None
        payload = {"type": "cancelOrder", "orderId": order_id}
        if not (execute and self.guard.limits.trading_enabled):
            self.guard.audit("cancel_dry_run", order_id=order_id)
            return {"status": "dry_run", "payload": payload}
        await self.session.ensure_fresh()
        if self.ws.age > self.settings.session_refresh_after:
            await self.ws.reconnect()
        self.guard.audit("cancel_submitted", order_id=order_id)
        try:
            response = await self.ws.request(payload, self.settings.order_timeout)
        except SubscriptionError as exc:
            self.guard.audit("cancel_rejected", order_id=order_id, error=exc.error)
            raise OrderRejected(str(exc), code=(exc.codes or [None])[0], response=exc.error) from exc
        if isinstance(response, dict) and response.get("status") == "failed":
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            self.guard.audit("cancel_rejected", order_id=order_id, response=response)
            raise OrderRejected(str(response.get("message") or "cancel refused"), code=error.get("code"), response=response)
        self.guard.audit("cancel_answered", order_id=order_id, response=response)
        return response
