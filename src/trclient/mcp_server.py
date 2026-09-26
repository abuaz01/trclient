"""Local MCP server (stdio) that exposes the Trade Republic client to an AI agent.

Security model:
* Login happens only in a terminal (`trclient login`). The PIN never passes through
  the agent or this server; the server only reuses the session from the OS keychain.
* Orders are a two-step process: `preview_order` validates, checks the guardrails and
  returns a single-use `confirmation_id`; only `place_order(confirmation_id)` sends it.
  The agent cannot change an order between preview and execution.
* Nothing is sent unless TR_TRADING_ENABLED=1. All guardrails of guard.py apply again
  at execution time (limits, daily count, price deviation, allow list, audit log).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import TradeRepublic
from .config import stored_phone
from .errors import AuthError, OrderStateUnknown, TRError
from .log import RedactingFilter
from .orders import OrderRequest, isin_is_valid
from .session import TRSession

NOT_LOGGED_IN = (
    "Not logged in to Trade Republic (or the session expired). Ask the user to run "
    "`trclient login` in a terminal, then retry. Never ask the user for their PIN."
)

INSTRUCTIONS = """\
Tools for the user's Trade Republic brokerage account (unofficial API, real money).

How the order tools work:
1. preview_order validates an order, checks the configured guardrails and returns a single-use
   confirmation_id. place_order sends exactly that previewed order. Previews expire.
2. "accepted" means Trade Republic received the order, not that it was executed.
   get_orders / get_transactions show the actual state.
3. After ORDER STATE UNKNOWN the order may or may not exist - get_orders shows which.
4. Login happens only in a terminal (`trclient login`); the tools never need the PIN.
Instruments are identified by ISIN. Ticker prices are strings.
"""


@dataclass
class _Preview:
    order: OrderRequest
    expires_at: float


def _default_factory() -> TradeRepublic:
    phone = stored_phone()
    if not phone:
        raise ToolError("No Trade Republic account configured. Ask the user to run `trclient login` once.")
    return TradeRepublic(TRSession(phone))


class TradingService:
    """Holds one TradeRepublic client for the lifetime of the server."""

    def __init__(
        self,
        factory: Callable[[], TradeRepublic] = _default_factory,
        *,
        preview_ttl: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._tr: TradeRepublic | None = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._previews: dict[str, _Preview] = {}
        self.preview_ttl = preview_ttl if preview_ttl is not None else float(os.environ.get("TR_MCP_PREVIEW_TTL", "120"))

    async def client(self, *, require_login: bool = True) -> TradeRepublic:
        async with self._lock:
            if self._tr is None:
                self._tr = self._factory()
                self._logged_in = False
            if require_login and not self._logged_in:
                if not await self._tr.session.resume():
                    # Drop the client so a login done in the meantime is picked up next time.
                    await self._close_locked()
                    raise ToolError(NOT_LOGGED_IN)
                self._logged_in = True
                self._tr.session.start_keepalive()
            return self._tr

    async def reset(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._tr is not None:
            try:
                await self._tr.aclose()
            except Exception:
                pass
        self._tr = None
        self._logged_in = False

    # --------------------------------------------------------------- previews

    def remember(self, order: OrderRequest) -> None:
        now = self._clock()
        self._previews = {k: v for k, v in self._previews.items() if v.expires_at > now}
        self._previews[order.client_process_id] = _Preview(order, now + self.preview_ttl)

    def take(self, confirmation_id: str) -> OrderRequest:
        preview = self._previews.pop(confirmation_id, None)
        if preview is None:
            raise ToolError("Unknown or already used confirmation_id. Call preview_order again.")
        if preview.expires_at <= self._clock():
            raise ToolError("This preview expired. Call preview_order again to get a fresh quote.")
        return preview.order


async def _reconcile(tr: TradeRepublic) -> list[dict]:
    """Best effort: never let a failed reconciliation block reads or previews."""
    if tr.guard.limits.budget_eur is None or not tr.session.has_session:
        return []
    try:
        return await tr.reconcile_budget()
    except (TRError, TimeoutError):
        return []


def _trace_path() -> pathlib.Path | None:
    """Tool-call tracing is opt-in: only when TR_TRACE_PATH is set (e.g. for a local dashboard)."""
    raw = os.environ.get("TR_TRACE_PATH", "").strip()
    return pathlib.Path(raw).expanduser() if raw else None


def _summary(tool: str, result: Any) -> Any:
    """A compact digest of order-related results for the optional trace file."""
    if isinstance(result, dict) and tool in ("preview_order", "place_order"):
        return {k: result.get(k) for k in ("status", "order_id", "order", "quote_eur", "estimated_value_eur",
                                            "fee_eur", "total_cost_eur", "net_proceeds_eur")}
    return None


def _trace(tool: str, args: dict, started: float, ok: bool, result: Any = None, error: str | None = None) -> None:
    """Append one line per tool call to a local 0600 file - only if TR_TRACE_PATH is set."""
    path = _trace_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": os.environ.get("TR_TRACE_SOURCE", "claude"),
            "cycle": os.environ.get("TR_TRACE_CYCLE"),
            "tool": tool,
            "args": args,
            "ms": round((time.monotonic() - started) * 1000),
            "ok": ok,
            "summary": _summary(tool, result) if ok else None,
            "error": error,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # tracing must never break a tool


def _valid_isin(isin: str) -> str:
    isin = (isin or "").strip().upper()
    if not isin_is_valid(isin):
        raise ToolError(f"invalid ISIN {isin!r}")
    return isin


def _bounded(value: int | float, low: int | float, high: int | float):
    return max(low, min(high, value))


def _order_view(order: OrderRequest) -> dict:
    return {
        "isin": order.isin,
        "side": order.side,
        "size": order.size,
        "order_type": order.mode,
        "limit_price": order.limit,
        "stop_price": order.stop,
        "expiry": order.expiry,
        "expiry_date": order.expiry_date.isoformat() if order.expiry_date else None,
        "exchange": order.exchange,
        "sell_fractions": order.sell_fractions,
    }


def create_server(service: TradingService | None = None) -> MCPServer:
    service = service or TradingService()
    mcp = MCPServer("trade-republic", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

    def errors(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                _trace(fn.__name__, kwargs, started, False, error=str(exc)[:300])
                raise
            _trace(fn.__name__, kwargs, started, True, result)
            return result

        return _map_errors(wrapper)

    def _map_errors(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except OrderStateUnknown as exc:
                raise ToolError(
                    f"ORDER STATE UNKNOWN (clientProcessId {exc.client_process_id}): {exc}. "
                    "Call get_orders before doing anything else. Do NOT resend the order."
                ) from exc
            except AuthError as exc:
                await service.reset()
                raise ToolError(NOT_LOGGED_IN) from exc
            except TRError as exc:
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            except TimeoutError as exc:
                raise ToolError("Trade Republic did not answer in time. Try again.") from exc
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

        return wrapper

    # ------------------------------------------------------------------ account

    @mcp.tool(annotations=read_only)
    @errors
    async def get_account_status() -> dict[str, Any]:
        """Login state, securities account number and the active trading guardrails."""
        tr = await service.client()
        account = await tr.account()
        limits = tr.guard.limits
        return {
            "logged_in": True,
            "securities_account_number": account.get("securitiesAccountNumber"),
            "trading_enabled": limits.trading_enabled,
            "max_order_value_eur": limits.max_order_value_eur,
            "max_orders_per_day": limits.max_orders_per_day,
            "orders_submitted_today": tr.guard.submitted_today(),
            "max_price_deviation": limits.max_price_deviation,
            "allowed_isins": sorted(limits.allowed_isins) if limits.allowed_isins else "all",
            "order_fee_eur": limits.order_fee_eur,
            "max_fee_pct": limits.max_fee_pct,
            "budget": tr.guard.budget_status() if limits.budget_eur is not None else None,
        }

    @mcp.tool(annotations=read_only)
    @errors
    async def get_portfolio() -> Any:
        """All positions of the securities account, grouped by instrument type."""
        return await (await service.client()).portfolio()

    @mcp.tool(annotations=read_only)
    @errors
    async def get_cash() -> Any:
        """Cash balance of the account."""
        return await (await service.client()).cash()

    @mcp.tool(annotations=read_only)
    @errors
    async def get_orders(include_terminated: bool = False) -> Any:
        """Open orders. With include_terminated=true: finished, cancelled and expired orders instead."""
        return await (await service.client()).orders(terminated=include_terminated)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_transactions(after: str | None = None) -> Any:
        """Latest account transactions (timeline). Pass the cursor from a previous answer as `after` for older ones."""
        return await (await service.client()).timeline_transactions(after=after)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_available_size(isin: str, exchange: str | None = None) -> Any:
        """How many units of an instrument can currently be sold."""
        return await (await service.client()).available_size(isin, exchange)

    # ------------------------------------------------------------------ documents

    async def _documents(since_days: int, source: str, title_contains: str | None):
        sources = {"all": ("transactions", "postbox"), "postbox": ("postbox",), "transactions": ("transactions",)}
        if source not in sources:
            raise ToolError("source must be all, postbox or transactions")
        since = date.fromordinal(date.today().toordinal() - _bounded(since_days, 1, 3660))
        return await (await service.client()).documents(since=since, sources=sources[source],
                                                         title_contains=title_contains)

    @mcp.tool(annotations=read_only)
    @errors
    async def list_documents(since_days: int = 90, source: str = "all", title_contains: str | None = None) -> Any:
        """Documents of the last `since_days` days: account statements, trade confirmations, cost
        information, tax documents and postbox items. source: all | postbox | transactions.
        title_contains filters by title, e.g. "Kontoauszug"."""
        docs = await _documents(since_days, source, title_contains)
        return {"count": len(docs), "documents": [d.public() for d in docs]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    @errors
    async def download_documents(document_ids: list[str] | None = None, since_days: int = 90, source: str = "all",
                                 title_contains: str | None = None) -> Any:
        """Save documents as PDF files in the local folder TR_DOCUMENTS_DIR (default ~/trclient-documents).
        Without document_ids every document matching the filters is saved. Existing files are kept."""
        docs = await _documents(since_days, source, title_contains)
        if document_ids is not None:
            wanted = set(document_ids)
            docs = [d for d in docs if d.id in wanted]
            missing = wanted - {d.id for d in docs}
            if missing:
                raise ToolError(f"unknown document ids in this period: {sorted(missing)}")
        folder = pathlib.Path(os.environ.get("TR_DOCUMENTS_DIR", "~/trclient-documents")).expanduser()
        tr = await service.client()
        files = [await tr.download_document(d, folder) for d in docs]
        return {"folder": str(folder), "count": len(files), "files": files}

    # ------------------------------------------------------------------ market data

    @mcp.tool(annotations=read_only)
    @errors
    async def get_quote(isin: str, side: Literal["buy", "sell"] = "buy", exchange: str | None = None) -> Any:
        """Current order price for an instrument (priceAsk / priceBid in EUR)."""
        return await (await service.client(require_login=False)).price_for_order(isin, side, exchange)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_instrument(isin: str) -> Any:
        """Master data of an instrument: name, type, exchanges, tags."""
        return await (await service.client(require_login=False)).instrument(isin)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_price_history(
        isin: str, range: Literal["1d", "5d", "1m", "3m", "1y", "max"] = "1y", exchange: str | None = None
    ) -> Any:
        """Price history (OHLC aggregates) of an instrument."""
        return await (await service.client(require_login=False)).history(isin, range, exchange)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_performance(isin: str, exchange: str | None = None) -> Any:
        """Price changes of an instrument over the usual reference periods."""
        return await (await service.client(require_login=False)).performance(isin, exchange)

    @mcp.tool(annotations=read_only)
    @errors
    async def search_instruments(
        query: str, asset_type: Literal["stock", "fund", "derivative", "crypto", "bond"] = "stock"
    ) -> Any:
        """Search tradable instruments by name, ticker or ISIN."""
        return await (await service.client(require_login=False)).search(query, asset_type)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_news(isin: str, limit: int = 10) -> Any:
        """Recent news about an instrument from Trade Republic (in the app language, TR_LOCALE)."""
        items = await (await service.client(require_login=False)).news(isin)
        return items[: _bounded(limit, 1, 50)] if isinstance(items, list) else items

    @mcp.tool(annotations=read_only)
    @errors
    async def get_stock_details(isin: str) -> Any:
        """Trade Republic's own company details for a stock (description, key figures, analyst ratings)."""
        return await (await service.client(require_login=False)).stock_details(isin)

    @mcp.tool(annotations=read_only)
    @errors
    async def get_market_status(isin: str, exchange: str | None = None) -> dict[str, Any]:
        """Whether an instrument is tradable right now (quote freshness, spread, Trade Republic trading status)."""
        return await (await service.client(require_login=False)).market_status(isin, exchange)

    @mcp.tool(annotations=read_only)
    @errors
    async def wait_for_market(isin: str, exchange: str | None = None, timeout_seconds: int = 55) -> dict[str, Any]:
        """Wait (max 300 s) until the instrument has a fresh two-sided quote."""
        return await (await service.client(require_login=False)).wait_for_market(
            isin, exchange, _bounded(timeout_seconds, 1, 300)
        )

    @mcp.tool(annotations=read_only)
    @errors
    async def wait_for_price(
        isin: str,
        above: float | None = None,
        below: float | None = None,
        field: Literal["last", "bid", "ask"] = "last",
        exchange: str | None = None,
        timeout_seconds: int = 55,
    ) -> dict[str, Any]:
        """Watch the live price (max 300 s) until it reaches `above` or falls to `below`.
        Returns when the level is reached or the timeout expires."""
        return await (await service.client(require_login=False)).wait_for_price(
            isin, above=above, below=below, field=field, exchange=exchange,
            timeout_seconds=_bounded(timeout_seconds, 1, 300),
        )

    @mcp.tool(annotations=read_only)
    @errors
    async def get_budget_status() -> dict[str, Any]:
        """Budget ledger of this agent (TR_BUDGET_EUR): committed, remaining, units it bought, order fee.
        Orders that expired or were cancelled release their reservation automatically."""
        tr = await service.client(require_login=False)
        released = await _reconcile(tr)
        return {**tr.guard.budget_status(), "reconciled": released, "orders_submitted_today": tr.guard.submitted_today(),
                "max_orders_per_day": tr.guard.limits.max_orders_per_day}

    # ------------------------------------------------------------------ trading

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True))
    @errors
    async def preview_order(
        isin: str,
        side: Literal["buy", "sell"],
        size: float,
        order_type: Literal["market", "limit", "stopMarket"] = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        expiry: Literal["gfd", "gtd", "gtc"] = "gfd",
        expiry_date: str | None = None,
        exchange: str | None = None,
        sell_fractions: bool = False,
    ) -> dict[str, Any]:
        """Validate an order and check it against the guardrails WITHOUT sending it.

        size is the number of units (fractions allowed). limit_price is required for
        order_type=limit, stop_price for stopMarket. expiry gtd needs expiry_date
        (YYYY-MM-DD). Market orders only support expiry gfd. Returns a single-use
        confirmation_id for place_order.
        """
        try:
            parsed_date = date.fromisoformat(expiry_date) if expiry_date else None
        except ValueError:
            raise ToolError("expiry_date must be YYYY-MM-DD") from None
        order = OrderRequest(
            isin=isin,
            side=side,
            size=size,
            mode=order_type,
            limit=limit_price,
            stop=stop_price,
            expiry=expiry,
            expiry_date=parsed_date,
            exchange=exchange,
            sell_fractions=sell_fractions,
        )
        tr = await service.client(require_login=False)
        await _reconcile(tr)
        result = await tr.place_order(order, execute=False)
        service.remember(order)
        value = result.estimated_value or 0.0
        fee = result.fee_eur
        return {
            "confirmation_id": order.client_process_id,
            "expires_in_seconds": service.preview_ttl,
            "order": _order_view(order),
            "quote_eur": result.estimated_price,
            "estimated_value_eur": round(value, 2),
            "fee_eur": fee,
            "total_cost_eur": round(value + fee, 2) if side == "buy" else None,
            "net_proceeds_eur": round(value - fee, 2) if side == "sell" else None,
            "budget_remaining_before_eur": round(result.budget_remaining_eur, 2)
            if result.budget_remaining_eur is not None else None,
            "trading_enabled": tr.guard.limits.trading_enabled,
            "next_step": "Show this to the user. Call place_order with the confirmation_id to send it."
            if tr.guard.limits.trading_enabled
            else "Trading is disabled (TR_TRADING_ENABLED is not set) - this order cannot be sent.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        )
    )
    @errors
    async def place_order(confirmation_id: str) -> dict[str, Any]:
        """Send a previously previewed order to Trade Republic. REAL MONEY.

        Only works with a confirmation_id from preview_order (single use, expires).
        The guardrails are checked again right before sending.
        """
        tr = await service.client()
        if not tr.guard.limits.trading_enabled:
            raise ToolError("Trading is disabled. The user has to start the server with TR_TRADING_ENABLED=1.")
        order = service.take(confirmation_id)
        result = await tr.place_order(order, execute=True)
        return {
            "status": result.status,
            "order_id": result.order_id,
            "client_process_id": result.client_process_id,
            "order": _order_view(order),
            "estimated_value_eur": round(result.estimated_value, 2) if result.estimated_value else None,
            "fee_eur": result.fee_eur,
            "note": "Accepted is not executed. Check get_orders / get_transactions for the final state.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        )
    )
    @errors
    async def cancel_order(order_id: str) -> Any:
        """Cancel an open order by its order_id (from get_orders or place_order)."""
        tr = await service.client()
        if not tr.guard.limits.trading_enabled:
            raise ToolError("Trading is disabled. The user has to start the server with TR_TRADING_ENABLED=1.")
        return await tr.cancel_order(order_id, execute=True)

    return mcp


def main() -> None:
    # stdout belongs to the MCP protocol - logs go to stderr only.
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    level = logging.DEBUG if os.environ.get("TR_MCP_DEBUG") else logging.WARNING
    logging.basicConfig(level=level, handlers=[handler])
    create_server().run("stdio")


if __name__ == "__main__":
    main()
