from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from tuoni import TuoniWebSocket

from tuoni_mcp.connection import ServerContext, authentication_secrets, ensure_fresh_token, login, to_thread
from tuoni_mcp.http_client import TuoniHTTPError
from tuoni_mcp.pagination import bounded_text, redact


# Event data is duplicated into a topic deque and the all-events deque.  Bound one
# normalized event before either append so a single WebSocket frame cannot retain
# arbitrary bytes for the life of both ring buffers.
EVENT_BUFFER_ITEM_MAX_BYTES = 32 * 1024

# (resource_uri, stomp_topic)
TOPIC_MAP: list[tuple[str, str]] = [
    ("tuoni://events/agents/new", TuoniWebSocket.TOPIC_AGENT),
    ("tuoni://events/agents/heartbeat", TuoniWebSocket.TOPIC_AGENT_HEARTBEAT),
    ("tuoni://events/agents/active", TuoniWebSocket.TOPIC_AGENT_ACTIVE),
    ("tuoni://events/agents/metadata", TuoniWebSocket.TOPIC_AGENT_METADATA),
    ("tuoni://events/commands/sent", TuoniWebSocket.TOPIC_COMMAND_SENT),
    ("tuoni://events/commands/results", TuoniWebSocket.TOPIC_COMMAND_RESULT),
    ("tuoni://events/listeners/new", TuoniWebSocket.TOPIC_LISTENER_NEW),
    ("tuoni://events/listeners/modified", TuoniWebSocket.TOPIC_LISTENER_MODIFIED),
    ("tuoni://events/listeners/removed", TuoniWebSocket.TOPIC_LISTENER_REMOVED),
    ("tuoni://events/payloads/new", TuoniWebSocket.TOPIC_PAYLOAD_NEW),
    ("tuoni://events/payloads/archived", TuoniWebSocket.TOPIC_PAYLOAD_ARCHIVED),
    ("tuoni://events/users/new", TuoniWebSocket.TOPIC_USER_NEW),
    ("tuoni://events/users/modified", TuoniWebSocket.TOPIC_USER_MODIFIED),
    ("tuoni://events/command-templates/new", TuoniWebSocket.TOPIC_COMMAND_TEMPLATE_NEW),
    ("tuoni://events/command-templates/removed", TuoniWebSocket.TOPIC_COMMAND_TEMPLATE_REMOVED),
]

URI_TO_TOPIC: dict[str, str] = dict(TOPIC_MAP)
URI_ALL = "tuoni://events/all"
URI_STATUS = "tuoni://events/_status"
ALL_URIS = [uri for uri, _ in TOPIC_MAP] + [URI_ALL, URI_STATUS]


@dataclass
class _Status:
    connected: bool = False
    last_connect: float | None = None
    last_disconnect: float | None = None
    reconnect_attempts: int = 0
    last_error: str | None = None
    dropped_pending_events: int = 0


@dataclass
class EventBridge:
    ctx: ServerContext
    ws: TuoniWebSocket | None = None
    buffers: dict[str, deque] = field(default_factory=dict)
    status: _Status = field(default_factory=_Status)
    loop: asyncio.AbstractEventLoop | None = None
    _watchdog: asyncio.Task | None = None
    startup_task: asyncio.Task | None = field(default=None, repr=False)
    _connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _stopping: bool = False
    _next_sequence: int = 1
    _pending: deque = field(default_factory=deque, repr=False)
    _pending_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _drain_scheduled: bool = False
    _retry_after: float = 0.0

    def __post_init__(self) -> None:
        size = self.ctx.cfg.event_buffer_size
        for uri, _ in TOPIC_MAP:
            self.buffers[uri] = deque(maxlen=size)
        self.buffers[URI_ALL] = deque(maxlen=size)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopping = False
        # Install recovery before the first network attempt.  Startup login or
        # WebSocket failures must not permanently disable the events profile.
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())
        try:
            await self._connect()
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            self._record_connect_failure(exc)

    async def _connect(self) -> None:
        async with self._connect_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        # WebSockets share the TuoniC2 JWT with ordinary tools.  Use the same
        # double-checked login lock before every initial or reconnect attempt so
        # event recovery cannot race a tool-triggered token refresh.
        if self.ws is not None:
            previous, self.ws = self.ws, None
            await to_thread(previous.disconnect)
        await ensure_fresh_token(self.ctx)
        previous_login = self.ctx.last_login
        def connect():
            ws = self.ctx.c2.connect_websocket(10)
            if self._stopping:
                ws.disconnect()
                return None
            return ws

        try:
            ws = await to_thread(connect)
        except TuoniHTTPError as exc:
            if exc.status_code != 401:
                raise
            # A server restart/revocation can invalidate a recent token. Retry
            # this read-only handshake once, sharing ordinary tools' login lock.
            async with self.ctx.login_lock:
                if self.ctx.last_login == previous_login:
                    await login(self.ctx)
            ws = await to_thread(connect)
        if ws is None:
            return
        if self._stopping:
            await to_thread(ws.disconnect)
            return
        for uri, topic in TOPIC_MAP:
            ws.on_event(topic, self._make_handler(uri))
        self.ws = ws
        self.status.connected = True
        self.status.last_connect = time.time()
        self.status.last_error = None
        self._retry_after = 0.0

    def _record_connect_failure(self, exc: Exception) -> None:
        self.status.connected = False
        self.status.last_disconnect = time.time()
        message = f"{type(exc).__name__}: {exc}"
        for secret in authentication_secrets(self.ctx):
            message = message.replace(secret, "[REDACTED]")
        self.status.last_error = bounded_text(message, 512)
        self._retry_after = time.monotonic() + 30

    async def _reconnect_if_needed(self) -> None:
        if self._stopping:
            return
        if self.ws is not None and self.ws.is_connected:
            self.status.connected = True
            return
        # Re-check after acquiring the bridge lock: a slow initial login/connect
        # can overlap the watchdog's first tick and must not create two sockets.
        async with self._connect_lock:
            if self._stopping:
                return
            if self.ws is not None and self.ws.is_connected:
                self.status.connected = True
                return
            self.status.connected = False
            self.status.last_disconnect = time.time()
            self.status.reconnect_attempts += 1
            try:
                await self._connect_locked()
            except Exception as exc:
                self._record_connect_failure(exc)

    def _make_handler(self, uri: str):
        def handler(data: Any) -> None:
            event = {"received_at": time.time(), "topic": URI_TO_TOPIC[uri], "data": data}
            self._submit_event(uri, event)

        return handler

    def _normalize_event(self, event: dict) -> dict:
        data = redact(
            event.get("data"),
            max_depth=3,
            max_items=8,
            max_string_bytes=256,
        )
        received_at = event.get("received_at")
        if not isinstance(received_at, (int, float)):
            received_at = bounded_text(received_at, 64)
        event = {
            "received_at": received_at,
            "topic": bounded_text(event.get("topic"), 128),
            "data": data,
        }
        buffered_size = len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
        # Reserve space for the sequence and bounded source URI added on drain.
        if buffered_size > EVENT_BUFFER_ITEM_MAX_BYTES - 256:
            event["data"] = {
                "omitted": True,
                "reason": "event data exceeded the buffer byte limit",
                "bounded_size_bytes": buffered_size,
            }
        return event

    def _submit_event(self, uri: str, event: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed() or self._stopping:
            return
        # Normalize on the reader thread. Never retain raw command bodies in
        # the event loop's unbounded ready queue, even during a traffic burst.
        bounded = self._normalize_event(event)
        with self._pending_lock:
            if self._stopping:
                return
            if len(self._pending) >= self.ctx.cfg.event_buffer_size:
                self._pending.popleft()
                self.status.dropped_pending_events += 1
            self._pending.append((uri, bounded))
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
            try:
                loop.call_soon_threadsafe(self._drain_pending)
            except RuntimeError:
                self._pending.clear()
                self._drain_scheduled = False

    def _drain_pending(self) -> None:
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
            self._drain_scheduled = False
        if self._stopping:
            return
        for uri, event in pending:
            self._append_normalized(uri, event)

    def _enqueue(self, uri: str, event: dict) -> None:
        self._append_normalized(uri, self._normalize_event(event))

    def _append_normalized(self, uri: str, event: dict) -> None:
        event = {**event, "sequence": self._next_sequence}
        self._next_sequence += 1
        buf = self.buffers.get(uri)
        if buf is not None:
            buf.append(event)
        self.buffers[URI_ALL].append(
            {**event, "source_uri": bounded_text(uri, 128)}
        )

    def events_after(self, sequence: int) -> tuple[list[dict[str, Any]], int | None]:
        """Return a stable snapshot after ``sequence`` and the oldest retained sequence."""
        events = list(self.buffers[URI_ALL])
        oldest = int(events[0]["sequence"]) if events else None
        return [event for event in events if int(event["sequence"]) > sequence], oldest

    def snapshot(self, uri: str) -> dict[str, Any]:
        if uri == URI_STATUS:
            return {
                "connected": self.status.connected,
                "last_connect": self.status.last_connect,
                "last_disconnect": self.status.last_disconnect,
                "reconnect_attempts": self.status.reconnect_attempts,
                "last_error": self.status.last_error,
                "buffer_size": self.ctx.cfg.event_buffer_size,
                "buffer_item_max_bytes": EVENT_BUFFER_ITEM_MAX_BYTES,
                "dropped_pending_events": self.status.dropped_pending_events,
            }
        buf = self.buffers.get(uri)
        if buf is None:
            return {"error": f"Unknown event URI: {uri}", "events": []}
        return {"uri": uri, "events": list(buf), "count": len(buf)}

    async def _watchdog_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            # Quiet sockets close when their access token needs renewal.
            # Detect that promptly, while retaining the offline retry backoff.
            if self.ws is None and time.monotonic() < self._retry_after:
                continue
            await self._reconnect_if_needed()

    async def stop(self) -> None:
        self._stopping = True
        with self._pending_lock:
            self._pending.clear()
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None
        if self.ws is not None:
            try:
                await to_thread(self.ws.disconnect)
            except Exception:
                pass
            self.ws = None
        self.status.connected = False
        self.status.last_disconnect = time.time()
