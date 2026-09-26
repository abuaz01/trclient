"""Guardrails for automated trading, flat-fee awareness, a budget ledger and an audit log.

Nothing is sent to the exchange unless TR_TRADING_ENABLED=1 *and* the caller passes
execute=True. Everything else is a dry run that returns the exact payload.

Budget ledger (TR_BUDGET_EUR): the agent may only commit this much money.
* Accepted buys reserve value + fee immediately.
* Market sells give back value - fee when accepted; resting sells (limit / stop) only
  once they are reported as executed, so a pending stop-loss is never spent twice.
* Orders that are cancelled, expire or get rejected release their reservation
  (TradeRepublic.reconcile_budget reads the terminated orders; partly filled orders keep
  the filled part). Estimates use the quote at order time.
With a budget set, the agent may only sell units it bought itself.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Mapping

from .errors import GuardrailViolation
from .orders import OrderRequest


@dataclass(frozen=True)
class Guardrails:
    trading_enabled: bool = False
    max_order_value_eur: float = 500.0
    max_orders_per_day: int = 10
    # A limit/stop further than this from the current quote is treated as a typo.
    max_price_deviation: float = 0.20
    allowed_isins: frozenset[str] | None = None
    # Trade Republic charges a flat external cost per order (buy and sell).
    order_fee_eur: float = 1.0
    # Optional: refuse buys where the fee is a larger share of the order than this (1.0 = off).
    max_fee_pct: float = 1.0
    budget_eur: float | None = None
    budget_since: date | None = None
    audit_path: pathlib.Path = pathlib.Path("~/.local/state/trclient/audit.jsonl").expanduser()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Guardrails":
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return str(env.get(name, "") or "").strip() or default

        allowed = get("TR_ALLOWED_ISINS")
        since = get("TR_BUDGET_SINCE")
        budget = get("TR_BUDGET_EUR")
        return cls(
            trading_enabled=get("TR_TRADING_ENABLED").lower() in ("1", "true", "yes", "on"),
            max_order_value_eur=float(get("TR_MAX_ORDER_EUR", "500")),
            max_orders_per_day=int(get("TR_MAX_ORDERS_PER_DAY", "10")),
            max_price_deviation=float(get("TR_MAX_PRICE_DEVIATION", "0.20")),
            allowed_isins=frozenset(i.strip().upper() for i in allowed.split(",") if i.strip()) or None,
            order_fee_eur=float(get("TR_ORDER_FEE_EUR", "1.0")),
            max_fee_pct=float(get("TR_MAX_FEE_PCT", "1.0")),
            budget_eur=float(budget) if budget else None,
            budget_since=date.fromisoformat(since) if since else None,
            audit_path=pathlib.Path(get("TR_AUDIT_PATH", "~/.local/state/trclient/audit.jsonl")).expanduser(),
        )


@dataclass(frozen=True)
class GuardResult:
    value_eur: float
    fee_eur: float
    fee_pct: float
    budget_remaining_eur: float | None
    owned_units: float | None

    @property
    def total_eur(self) -> float:
        return self.value_eur + self.fee_eur

    @property
    def net_proceeds_eur(self) -> float:
        return self.value_eur - self.fee_eur


@dataclass
class Ledger:
    committed_eur: float = 0.0
    units: dict[str, float] | None = None
    open_orders: int = 0
    cost_eur: dict[str, float] | None = None  # cost basis incl. buy fees of the units still held

    def owned(self, isin: str) -> float:
        return (self.units or {}).get(isin, 0.0)


class OrderGuard:
    def __init__(self, limits: Guardrails) -> None:
        self.limits = limits

    # ------------------------------------------------------------------ audit + ledger

    def _entries(self) -> list[dict]:
        path = self.limits.audit_path
        if not path.exists():
            return []
        out = []
        with path.open() as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def submitted_today(self) -> int:
        today = date.today().isoformat()
        return sum(1 for e in self._entries() if e.get("event") == "submitted" and e.get("local_date") == today)

    def ledger(self) -> Ledger:
        since = self.limits.budget_since.isoformat() if self.limits.budget_since else None
        submitted: dict[str, dict] = {}
        accepted: dict[str, dict] = {}  # client_process_id -> submitted entry
        by_order_id: dict[str, str] = {}
        released: dict[str, float] = {}  # client_process_id -> filled size that stays committed
        filled_ids: set[str] = set()
        for e in self._entries():
            if since and str(e.get("local_date", "")) < since:
                continue
            event, cpid = e.get("event"), e.get("client_process_id")
            if event == "submitted" and cpid:
                submitted[cpid] = e
            elif event == "accepted" and cpid in submitted:
                accepted[cpid] = submitted[cpid]
                if e.get("order_id"):
                    by_order_id[str(e["order_id"])] = cpid
            elif event == "cancel_answered" and str(e.get("order_id")) in by_order_id:
                released[by_order_id[str(e["order_id"])]] = 0.0
            elif event == "order_released" and str(e.get("order_id")) in by_order_id:
                released[by_order_id[str(e["order_id"])]] = float(e.get("filled_size") or 0.0)
            elif event == "order_filled" and str(e.get("order_id")) in by_order_id:
                filled_ids.add(by_order_id[str(e["order_id"])])
        ledger = Ledger(units={}, cost_eur={})
        for cpid, e in accepted.items():
            value, fee = float(e.get("estimated_value") or 0), float(e.get("fee_eur") or 0)
            size, isin = float(e.get("size") or 0), str(e.get("isin"))
            if cpid in released:
                filled = min(released[cpid], size)
                if filled <= 0 or size <= 0:
                    continue
                value, size = value * filled / size, filled  # partly filled: keep the filled part
            if e.get("side") == "buy":
                ledger.committed_eur += value + fee
                ledger.units[isin] = ledger.units.get(isin, 0.0) + size
                ledger.cost_eur[isin] = ledger.cost_eur.get(isin, 0.0) + value + fee
            else:
                # Units are reserved at once (no double selling); money only for market sells
                # or resting sells that were executed (fully or partly).
                held = ledger.units.get(isin, 0.0)
                if held > 0:
                    ledger.cost_eur[isin] = ledger.cost_eur.get(isin, 0.0) * max(held - size, 0.0) / held
                ledger.units[isin] = held - size
                if e.get("mode", "market") == "market" or cpid in filled_ids or cpid in released:
                    ledger.committed_eur -= value - fee
        ledger.units = {k: round(v, 9) for k, v in ledger.units.items() if abs(v) > 1e-9}
        ledger.cost_eur = {k: round(v, 2) for k, v in ledger.cost_eur.items() if k in ledger.units}
        return ledger

    def open_order_ids(self) -> set[str]:
        """Order ids of accepted orders that the ledger still counts in full."""
        since = self.limits.budget_since.isoformat() if self.limits.budget_since else None
        accepted, done = set(), set()
        for e in self._entries():
            if since and str(e.get("local_date", "")) < since:
                continue
            if e.get("event") == "accepted" and e.get("order_id"):
                accepted.add(str(e["order_id"]))
            elif e.get("event") in ("cancel_answered", "order_released", "order_filled") and e.get("order_id"):
                done.add(str(e["order_id"]))
        return accepted - done

    def budget_status(self) -> dict:
        lim = self.limits
        led = self.ledger()
        return {
            "budget_eur": lim.budget_eur,
            "committed_eur": round(led.committed_eur, 2),
            "remaining_eur": round(lim.budget_eur - led.committed_eur, 2) if lim.budget_eur is not None else None,
            "units_bought_by_agent": led.units,
            "cost_basis_eur": led.cost_eur,
            "since": lim.budget_since.isoformat() if lim.budget_since else "all time",
            "order_fee_eur": lim.order_fee_eur,
            "note": "Estimates from quotes at order time; realised P/L shows in get_transactions.",
        }

    def audit(self, event: str, **fields) -> None:
        path = self.limits.audit_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "local_date": date.today().isoformat(),
            "event": event,
            **fields,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    # ------------------------------------------------------------------ checks

    def check(self, order: OrderRequest, quote: float | None) -> GuardResult:
        """Raise GuardrailViolation, otherwise return value, fee and budget figures."""
        lim = self.limits
        if lim.allowed_isins is not None and order.isin not in lim.allowed_isins:
            raise GuardrailViolation(f"{order.isin} is not in TR_ALLOWED_ISINS")

        own_price = order.limit if order.mode == "limit" else order.stop if order.mode == "stopMarket" else None
        if own_price is not None and quote:
            deviation = abs(own_price - quote) / quote
            if deviation > lim.max_price_deviation:
                raise GuardrailViolation(
                    f"price {own_price} is {deviation:.0%} away from the quote {quote} "
                    f"(max {lim.max_price_deviation:.0%})"
                )
        prices = [p for p in (own_price, quote) if p]
        if not prices:
            raise GuardrailViolation("no price available to estimate the order value")
        # Buys are checked at the higher price, sells at the lower one (conservative).
        price = max(prices) if order.side == "buy" else min(prices)
        value = order.size * price
        fee = lim.order_fee_eur
        fee_pct = fee / value if value else 1.0

        # Sells are not capped by value, so an exit is never blocked because the position grew.
        if order.side == "buy" and value > lim.max_order_value_eur:
            raise GuardrailViolation(
                f"estimated order value {value:.2f} EUR exceeds TR_MAX_ORDER_EUR={lim.max_order_value_eur:.2f}"
            )
        if order.side == "buy" and fee_pct > lim.max_fee_pct:
            raise GuardrailViolation(
                f"the {fee:.2f} EUR order fee would be {fee_pct:.1%} of this {value:.2f} EUR buy "
                f"(max TR_MAX_FEE_PCT={lim.max_fee_pct:.1%}); buy at least {fee / lim.max_fee_pct:.2f} EUR"
            )

        remaining = owned = None
        if lim.budget_eur is not None:
            led = self.ledger()
            remaining = lim.budget_eur - led.committed_eur
            owned = led.owned(order.isin)
            if order.side == "buy" and value + fee > remaining + 1e-9:
                raise GuardrailViolation(
                    f"budget exceeded: this buy needs {value + fee:.2f} EUR incl. fee, "
                    f"{max(remaining, 0):.2f} EUR of TR_BUDGET_EUR={lim.budget_eur:.2f} left"
                )
            if order.side == "sell" and order.size > owned + 1e-9:
                raise GuardrailViolation(
                    f"with a budget set the agent may only sell units it bought itself "
                    f"({owned:g} of {order.isin} available)"
                )
        if self.submitted_today() >= lim.max_orders_per_day:
            raise GuardrailViolation(f"daily order limit reached (TR_MAX_ORDERS_PER_DAY={lim.max_orders_per_day})")
        return GuardResult(value, fee, fee_pct, remaining, owned)
