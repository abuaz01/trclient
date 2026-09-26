"""Command line interface: trclient <command> ..."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import pathlib
import sys
from datetime import date

from .client import HISTORY_RANGES, TradeRepublic
from .config import save_phone, stored_phone
from .errors import GuardrailViolation, LoginError, OrderRejected, OrderStateUnknown, TRError
from .log import RedactingFilter
from .orders import OrderRequest
from .session import TRSession, normalize_phone
from .store import MemoryStore


PUBLIC_COMMANDS = ("quote", "search", "history", "instrument", "budget")
ANONYMOUS_PHONE = "+490000000000"


def _load_phone(arg: str | None) -> str:
    phone = arg or stored_phone()
    if not phone:
        phone = input("Phone number (international format, e.g. +4917012345678): ")
    return normalize_phone(phone)


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def _login(args) -> None:
    session = TRSession(_load_phone(args.phone))
    try:
        if not args.force and await session.resume():
            print("Existing session is still valid.")
            save_phone(session.phone)
            return
        pin = getpass.getpass("PIN / password (never stored): ")
        if args.code_flow:
            challenge = await session.start_login_code_flow(pin, waf_token=args.waf_token or os.environ.get("TR_AWS_WAF_TOKEN"))
        else:
            challenge = await session.start_login(pin)
        del pin

        if challenge.method == "app":
            print("Confirm the login in your Trade Republic app (waiting)...")
            if challenge.required_action:
                print(f"(Trade Republic requested: {challenge.required_action})")
            await session.wait_for_app_confirmation(challenge)
        else:
            if challenge.method == "authenticator":
                code = input("Code from your authenticator app: ").strip()
            else:
                code = input("Code from Trade Republic (empty = send again): ").strip()
                if not code:
                    await session.resend_code(challenge)
                    code = input("Code sent again. Enter it: ").strip()
            if not await session.complete_with_code(challenge, code):
                print("Code accepted. Now confirm the login in your Trade Republic app (waiting)...")
                await session.wait_for_app_confirmation(challenge)
        save_phone(session.phone)
        print("Logged in. The session is stored in your OS keychain.")
    finally:
        await session.aclose()


def _order_from_args(args, side: str) -> OrderRequest:
    mode = "limit" if args.limit is not None else "stopMarket" if args.stop is not None else "market"
    kwargs = dict(
        isin=args.isin,
        side=side,
        size=args.size,
        mode=mode,
        limit=args.limit,
        stop=args.stop,
        expiry=args.expiry,
        expiry_date=date.fromisoformat(args.expiry_date) if args.expiry_date else None,
        exchange=args.exchange,
    )
    if side == "sell":
        kwargs["sell_fractions"] = args.sell_fractions
    return OrderRequest(**kwargs)


async def _run(args) -> int:
    if args.command == "login":
        await _login(args)
        return 0

    public = args.command in PUBLIC_COMMANDS
    phone = args.phone or stored_phone()
    if public and not phone:
        # Public market data needs no account: use a throw-away in-memory session.
        session = TRSession(ANONYMOUS_PHONE, store=MemoryStore())
    else:
        session = TRSession(_load_phone(args.phone))
    async with TradeRepublic(session) as tr:
        if not public and not await session.resume():
            print("Not logged in (or session expired). Run: trclient login", file=sys.stderr)
            return 2
        cmd = args.command
        if cmd == "status":
            acc = await tr.account()
            _print({"logged_in": True, "securitiesAccountNumber": acc.get("securitiesAccountNumber")})
        elif cmd == "logout":
            await session.logout()
            print("Logged out, stored session removed.")
        elif cmd == "portfolio":
            _print(await tr.portfolio())
        elif cmd == "cash":
            _print(await tr.cash())
        elif cmd == "orders":
            _print(await tr.orders(terminated=args.terminated))
        elif cmd == "transactions":
            _print(await tr.timeline_transactions(after=args.after))
        elif cmd == "documents":
            from .documents import parse_since

            sources = {"all": ("transactions", "postbox"), "postbox": ("postbox",),
                       "transactions": ("transactions",)}[args.source]
            docs = await tr.documents(since=parse_since(args.since, None if args.since else args.days),
                                      sources=sources, title_contains=args.match)
            if args.list:
                _print([d.public() for d in docs])
                return 0
            for doc in docs:
                result = await tr.download_document(doc, args.out, overwrite=args.overwrite)
                print(f"{result['status']:>10}  {result['file']}")
            print(f"{len(docs)} document(s) in {pathlib.Path(args.out).expanduser()}")
        elif cmd == "quote":
            _print(await tr.price_for_order(args.isin, args.side, args.exchange))
        elif cmd == "instrument":
            _print(await tr.instrument(args.isin))
        elif cmd == "history":
            _print(await tr.history(args.isin, args.range, args.exchange))
        elif cmd == "search":
            _print(await tr.search(args.query, args.type))
        elif cmd == "budget":
            _print(tr.guard.budget_status())
        elif cmd in ("buy", "sell"):
            order = _order_from_args(args, cmd)
            preview = await tr.place_order(order, execute=False)
            _print({"preview": preview.payload, "quote": preview.estimated_price, "estimated_value_eur": preview.estimated_value})
            if not args.execute:
                print("Dry run only. Add --execute (and set TR_TRADING_ENABLED=1) to send the order.")
                return 0
            if not tr.guard.limits.trading_enabled:
                print("TR_TRADING_ENABLED is not set - refusing to send.", file=sys.stderr)
                return 3
            if not args.yes:
                typed = input(f"Type {cmd.upper()} to send this order: ").strip()
                if typed != cmd.upper():
                    print("Aborted.")
                    return 1
            # Same client_process_id as the preview.
            result = await tr.place_order(order, execute=True)
            _print({"status": result.status, "orderId": result.order_id, "clientProcessId": result.client_process_id})
            print("Note: 'accepted' is not 'executed'. Check: trclient orders")
        elif cmd == "cancel":
            if not args.execute:
                _print(await tr.cancel_order(args.order_id, execute=False))
                return 0
            if not args.yes and input("Type CANCEL to cancel this order: ").strip() != "CANCEL":
                print("Aborted.")
                return 1
            _print(await tr.cancel_order(args.order_id, execute=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trclient", description="Unofficial Trade Republic client")
    p.add_argument("--phone", help="phone number, default: TR_PHONE or the last login")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="log in (app confirmation or authenticator code)")
    login.add_argument("--force", action="store_true", help="log in again even if the stored session is valid")
    login.add_argument("--code-flow", action="store_true", help="v1 flow with a code sent by Trade Republic (needs a WAF token)")
    login.add_argument("--waf-token", help="aws-waf-token cookie from a browser session (v1 only)")

    for name in ("status", "logout", "portfolio", "cash"):
        sub.add_parser(name)
    orders = sub.add_parser("orders")
    orders.add_argument("--terminated", action="store_true")
    tx = sub.add_parser("transactions")
    tx.add_argument("--after")

    docs = sub.add_parser("documents", help="download statements, trade confirmations and postbox documents")
    docs.add_argument("--out", default=os.environ.get("TR_DOCUMENTS_DIR", "~/trclient-documents"),
                      help="target folder (default: TR_DOCUMENTS_DIR or ~/trclient-documents)")
    docs.add_argument("--since", help="only events on or after this date (YYYY-MM-DD)")
    docs.add_argument("--days", type=int, default=90, help="only the last N days when --since is not given (default 90)")
    docs.add_argument("--source", choices=["all", "postbox", "transactions"], default="all")
    docs.add_argument("--match", help="only documents whose title contains this text, e.g. Kontoauszug")
    docs.add_argument("--list", action="store_true", help="only list, do not download")
    docs.add_argument("--overwrite", action="store_true")
    quote = sub.add_parser("quote", help="current order price (no login needed)")
    quote.add_argument("isin")
    quote.add_argument("--side", choices=["buy", "sell"], default="buy")
    quote.add_argument("--exchange", default="LSX")
    inst = sub.add_parser("instrument")
    inst.add_argument("isin")
    hist = sub.add_parser("history")
    hist.add_argument("isin")
    hist.add_argument("--range", choices=HISTORY_RANGES, default="1y")
    hist.add_argument("--exchange", default="LSX")
    sub.add_parser("budget", help="budget ledger of the trading agent (TR_BUDGET_EUR)")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--type", default="stock")

    for side in ("buy", "sell"):
        o = sub.add_parser(side, help=f"{side} order (dry run unless --execute)")
        o.add_argument("isin")
        o.add_argument("--size", type=float, required=True, help="number of shares (fractions allowed)")
        g = o.add_mutually_exclusive_group()
        g.add_argument("--limit", type=float, help="limit price -> limit order")
        g.add_argument("--stop", type=float, help="stop price -> stop-market order")
        o.add_argument("--expiry", choices=["gfd", "gtd", "gtc"], default="gfd")
        o.add_argument("--expiry-date", help="YYYY-MM-DD, required for gtd")
        o.add_argument("--exchange", default="LSX")
        if side == "sell":
            o.add_argument("--sell-fractions", action="store_true")
        o.add_argument("--execute", action="store_true", help="actually send the order")
        o.add_argument("--yes", action="store_true", help="skip the interactive confirmation")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("order_id")
    cancel.add_argument("--execute", action="store_true")
    cancel.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, handlers=[handler])
    try:
        code = asyncio.run(_run(args))
    except (GuardrailViolation, OrderRejected, LoginError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    except OrderStateUnknown as exc:
        print(f"ORDER STATE UNKNOWN ({exc.client_process_id}): {exc}", file=sys.stderr)
        code = 4
    except TRError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
