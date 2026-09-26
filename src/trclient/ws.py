"""Websocket client for wss://api.traderepublic.com.

Authenticated through the session cookies on the connection (subscriptions carry no
token - sending one makes the server reject some topics). Every subscription gets its
own queue, and one-shot requests always unsubscribe, so a stale update of an earlier
subscription can never end up as the answer to a later request.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .errors import ConnectionLost, SubscriptionClosed, SubscriptionError, TRConnectionError
from .log import get_logger
from .protocol import apply_delta, parse_frame
from .session import TRSession

log = get_logger(__name__)


class Subscription:
    def __init__(self, client: "TRWebSocket", sub_id: str, payload: dict) -> None:
        self.id = sub_id
        self.payload = payload
        self._client = client
        self._queue: asyncio.Queue = asyncio.Queue()
        self._last: str | None = None

    def _push(self, kind: str, value: Any) -> None:
        self._queue.put_nowait((kind, value))

    async def get(self, timeout: float | None = None) -> Any:
        if timeout is None:
            kind, value = await self._queue.get()
        else:
            kind, value = await asyncio.wait_for(self._queue.get(), timeout)
        if kind == "data":
            return value
        if kind == "closed":
            raise SubscriptionClosed(f"subscription {self.id} closed by the server")
        raise value  # SubscriptionError or ConnectionLost

    def __aiter__(self):
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        await self._client.unsubscribe(self.id)


class TRWebSocket:
    def __init__(
        self,
        session: TRSession,
        *,
        connect: Callable[..., Awaitable[Any]] = ws_connect,
    ) -> None:
        self.session = session
        self.settings = session.settings
        self._connect_fn = connect
        self._conn: Any = None
        self._reader: asyncio.Task | None = None
        self._subs: dict[str, Subscription] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self.connected_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._reader is not None and not self._reader.done()

    @property
    def age(self) -> float:
        return time.monotonic() - self.connected_at if self.connected_at is not None else float("inf")

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            headers = {}
            if self.session.has_session:
                await self.session.ensure_fresh()
                headers["Cookie"] = self.session.cookie_header()
            try:
                conn = await self._connect_fn(
                    self.settings.ws_url,
                    additional_headers=headers,
                    user_agent_header=self.settings.user_agent,
                    open_timeout=20,
                    max_size=2**24,
                )
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as exc:
                raise TRConnectionError(f"websocket connection failed: {exc}") from exc
            info = dict(self.settings.ws_client_info, locale=self.settings.locale)
            try:
                await conn.send(f"connect {self.settings.ws_protocol_version} {json.dumps(info)}")
                answer = await asyncio.wait_for(conn.recv(), 20)
            except (ConnectionClosed, asyncio.TimeoutError) as exc:
                await conn.close()
                raise TRConnectionError("websocket handshake failed") from exc
            if answer != "connected":
                await conn.close()
                raise TRConnectionError(
                    f"websocket handshake refused: {str(answer)[:200]!r} "
                    f"(protocol version {self.settings.ws_protocol_version}; override with TR_WS_PROTOCOL_VERSION)"
                )
            self._conn = conn
            self.connected_at = time.monotonic()
            self._reader = asyncio.create_task(self._read_loop(conn))
            log.info("Websocket connected.")

    async def close(self) -> None:
        conn, reader = self._conn, self._reader
        self._conn = None
        self.connected_at = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        if reader is not None:
            try:
                await asyncio.wait_for(reader, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                reader.cancel()
        self._reader = None

    async def reconnect(self) -> None:
        await self.close()
        await self.connect()

    async def _send(self, message: str) -> None:
        if self._conn is None:
            raise ConnectionLost("websocket is not connected")
        try:
            await self._conn.send(message)
        except ConnectionClosed as exc:
            raise ConnectionLost("websocket closed while sending") from exc

    async def subscribe(self, payload: dict) -> Subscription:
        await self.connect()
        sub_id = str(next(self._ids))
        sub = Subscription(self, sub_id, payload)
        self._subs[sub_id] = sub
        try:
            await self._send(f"sub {sub_id} {json.dumps(payload, separators=(',', ':'))}")
        except ConnectionLost:
            self._subs.pop(sub_id, None)
            raise
        log.debug("sub %s type=%s", sub_id, payload.get("type"))
        return sub

    async def unsubscribe(self, sub_id: str) -> None:
        if self._subs.pop(sub_id, None) is None:
            return
        if self.connected:
            try:
                await self._send(f"unsub {sub_id}")
            except ConnectionLost:
                pass

    async def request(self, payload: dict, timeout: float) -> Any:
        """Subscribe, return the first answer, unsubscribe."""
        sub = await self.subscribe(payload)
        try:
            return await sub.get(timeout)
        finally:
            await sub.close()

    async def _read_loop(self, conn) -> None:
        reason: Exception = ConnectionLost("websocket closed")
        try:
            async for raw in conn:
                if isinstance(raw, bytes):
                    continue  # protobuf frames are only sent for proto subscriptions, which we don't use
                self._dispatch(raw)
        except ConnectionClosed as exc:
            reason = ConnectionLost(f"websocket closed ({exc.rcvd.code if exc.rcvd else 'no close frame'})")
        except Exception as exc:  # never let a bad frame kill waiting callers silently
            log.error("websocket reader failed: %s", type(exc).__name__)
            reason = ConnectionLost(f"websocket reader failed: {type(exc).__name__}")
        finally:
            if self._conn is conn:
                self._conn = None
                self.connected_at = None
            subs, self._subs = self._subs, {}
            for sub in subs.values():
                sub._push("error", reason)

    def _dispatch(self, raw: str) -> None:
        if raw == "connected" or raw.startswith("echo"):
            return
        frame = parse_frame(raw)
        if frame is None:
            log.debug("ignoring unparsable frame")
            return
        sub = self._subs.get(frame.sub_id)
        if sub is None:
            return
        if frame.code == "A":
            sub._last = frame.body
            try:
                sub._push("data", json.loads(frame.body) if frame.body else {})
            except ValueError:
                sub._push("error", SubscriptionError(sub.id, sub.payload, "invalid JSON in answer"))
        elif frame.code == "D":
            if sub._last is None:
                log.debug("delta without a previous answer on %s, skipped", sub.id)
                return
            try:
                text = apply_delta(sub._last, frame.body)
                data = json.loads(text)
            except ValueError:
                sub._push("error", SubscriptionError(sub.id, sub.payload, "could not apply delta"))
                return
            sub._last = text
            sub._push("data", data)
        elif frame.code == "C":
            self._subs.pop(sub.id, None)
            sub._push("closed", None)
        elif frame.code == "E":
            try:
                error: Any = json.loads(frame.body) if frame.body else {}
            except ValueError:
                error = frame.body[:500]
            sub._push("error", SubscriptionError(sub.id, sub.payload, error))
            asyncio.get_running_loop().create_task(self.unsubscribe(sub.id))
