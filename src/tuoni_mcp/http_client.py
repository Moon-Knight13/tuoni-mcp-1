"""Tuoni client with pooled connections, socket deadlines and streamed limits."""

from __future__ import annotations

import asyncio
import json
import math
import queue
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Any
from pathlib import Path

import requests
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import InvalidStatus
from urllib3.exceptions import HTTPError, ReadTimeoutError, SSLError
from tuoni import TuoniC2
from tuoni.TuoniExceptions import ExceptionTuoniRequestFailed
from tuoni.TuoniWebSocket import TuoniWebSocket

from tuoni_mcp.config import validate_base_url
from tuoni_mcp.connection import mutation_once_only_active
from tuoni_mcp.mutations import mutation_request


class TuoniHTTPError(ExceptionTuoniRequestFailed):
    def __init__(self, status_code: int, message: str):
        super().__init__(message or f"HTTP {status_code}", status_code=status_code)


class ResponseTooLarge(Exception):
    pass


class _NoRedirectConnect(connect_websocket):
    def process_redirect(self, exc: Exception) -> Exception:
        # The asyncio client normally follows redirects. A configured Tuoni
        # origin is a trust boundary, so reject every redirect explicitly.
        return exc


class _EventSocket:
    """Synchronous SDK facade; all network I/O stays on one asyncio loop."""

    def __init__(self, owner, connection):
        self.owner = owner
        self.connection = connection
        self.pending: queue.Queue[str | bytes] = queue.Queue(maxsize=32)
        self.ready = asyncio.Event()
        self._wake_lock = threading.Lock()
        self._wake_pending = False

    def send(self, message: str | bytes) -> None:
        # SDK calls can hold its subscription lock. Never wait for the owner
        # loop here, and bound the handoff before scheduling any loop callback.
        if self.owner._stop_requested.is_set():
            raise RuntimeError("Tuoni event connection is closing")
        size = len(message.encode("utf-8")) if isinstance(message, str) else len(message)
        if size > 128 * 1024:
            self.close()
            raise ResponseTooLarge("Tuoni event control frame exceeds its send limit")
        with self._wake_lock:
            try:
                self.pending.put_nowait(message)
            except queue.Full as exc:
                self.close()
                raise ResponseTooLarge("Tuoni event control queue is full") from exc
            if not self._wake_pending:
                self._wake_pending = True
                self.owner._loop.call_soon_threadsafe(self.ready.set)

    def close(self) -> None:
        self.owner._request_close()

    async def write_messages(self) -> None:
        while True:
            await self.ready.wait()
            self.ready.clear()
            while True:
                with self._wake_lock:
                    try:
                        message = self.pending.get_nowait()
                    except queue.Empty:
                        self._wake_pending = False
                        break
                await asyncio.wait_for(
                    self.connection.send(message), timeout=self.owner.c2.request_timeout,
                )


class VerifiedTuoniWebSocket(TuoniWebSocket):
    """Bounded STOMP transport with one owner for TLS reads, writes and close."""

    def __init__(self, c2):
        super().__init__(c2)
        self._reader: threading.Thread | None = None
        self._close_timeout = min(1.0, c2.request_timeout)
        self._stop_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._connect_error: Exception | None = None

    def connect(self, timeout: float = 10) -> bool:
        auth = self.c2._register_websocket(self)
        self._identity = auth.identity
        self._connection_auth = auth
        url = self._build_ws_url(auth.url)
        tls = None
        if url.startswith("wss://"):
            if self.c2._verify is False:
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls.check_hostname = False
                tls.verify_mode = ssl.CERT_NONE
            else:
                tls = ssl.create_default_context(
                    cafile=self.c2._verify if isinstance(self.c2._verify, str) else None,
                )
        self._reader = threading.Thread(
            target=self._run, args=(url, tls, timeout), name="tuoni-event-reader", daemon=True,
        )
        self._reader.start()
        self._connect_event.wait(timeout=timeout)
        if self._connect_error is not None:
            self.disconnect()
            raise self._connect_error
        if not self._stomp_connected:
            self.disconnect()
            return False
        return True

    def _run(self, url, tls, timeout) -> None:
        try:
            asyncio.run(self._run_connection(url, tls, timeout))
        except Exception as exc:
            if self._ws is None:
                self._connect_error = exc
        finally:
            with self._lock:
                self._stomp_connected = False
            self._connect_event.set()

    async def _run_connection(self, url, tls, timeout) -> None:
        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        if self._stop_requested.is_set():
            return

        async def open_connection():
            options = {"ssl": tls} if tls is not None else {}
            return await _NoRedirectConnect(
                url, **options, additional_headers={"Authorization": f"Bearer {self._connection_auth.access_token}"},
                open_timeout=timeout, close_timeout=self._close_timeout,
                max_size=self.c2.response_max_bytes, max_queue=1, compression=None,
                ping_interval=20, ping_timeout=20,
            )

        opening = asyncio.create_task(open_connection())
        stopping = asyncio.create_task(self._async_stop.wait())
        tasks = [opening, stopping]
        connection = None
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                return
            connection = opening.result()
            self._ws = _EventSocket(self, connection)
            reader = asyncio.create_task(self._read_messages())
            writer = asyncio.create_task(self._ws.write_messages())
            renewal = asyncio.create_task(self._watch_authentication())
            tasks.extend((reader, writer, renewal))
            done, _ = await asyncio.wait((reader, writer, renewal, stopping), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # All TLS operations, including forced aborts, belong to this loop.
            # Closing the WebSocket also ends its SockJS/STOMP session; avoid an
            # optional STOMP DISCONNECT write before the bounded close handshake.
            if connection is None:
                # Stop can arrive on the same tick as a successful opening.
                # Retrieve and close that socket rather than losing its owner.
                if not opening.done():
                    opening.cancel()
                outcome = (await asyncio.gather(opening, return_exceptions=True))[0]
                if not isinstance(outcome, BaseException):
                    connection = outcome
            try:
                if connection is not None:
                    try:
                        await asyncio.wait_for(connection.close(), self._close_timeout + 0.1)
                    except (TimeoutError, asyncio.TimeoutError):
                        connection.transport.abort()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _read_messages(self) -> None:
        async for message in self._ws.connection:
            if not isinstance(message, str):
                raise ValueError("Tuoni events require text WebSocket messages")
            if self._on_message(self._ws, message):
                return

    async def _watch_authentication(self) -> None:
        # End the socket before its access token expires, including a quiet
        # stream. The bridge reconnects through the shared SDK refresh preflight.
        # SDK refresh/login hold their lock across HTTP. Never acquire that
        # lock on the TLS owner loop: close must remain independently bounded.
        while self.c2._auth is self._connection_auth:
            remaining = self._connection_auth.refresh_at - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1.0, remaining))

    def _request_close(self) -> None:
        if self._stop_requested.is_set():
            return
        self._stop_requested.set()
        self._stop.set()
        loop, stopping = self._loop, self._async_stop
        if loop is not None and stopping is not None:
            try:
                loop.call_soon_threadsafe(stopping.set)
            except RuntimeError:  # owner loop already closed
                pass

    def disconnect(self) -> None:
        self._request_close()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self._close_timeout + 0.5)

    def _on_message(self, ws, message):
        limit = self.c2.response_max_bytes
        if len(message) > limit or len(message.encode("utf-8")) > limit:
            self._connect_error = ResponseTooLarge("Tuoni event frame exceeds the response limit")
            self._connect_event.set()
            ws.close()
            return
        return super()._on_message(ws, message)

    def _handle_stomp_frame(self, frame_str):
        # The public client spawns a daemon thread per callback. Deliver messages
        # on the reader instead; the bridge provides a bounded handoff to asyncio.
        frame_type, headers, body = self._parse_stomp_frame(frame_str)
        if frame_type == "ERROR":
            raise ExceptionTuoniRequestFailed("Tuoni event subscription was rejected")
        if frame_type != "MESSAGE":
            return super()._handle_stomp_frame(frame_str)
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError:
            data = body
        with self._lock:
            callbacks = list(self._callbacks.get(headers.get("destination", ""), []))
        for callback in callbacks:
            try:
                callback(data)
            except Exception:
                # One subscriber must not terminate the transport or others.
                continue


class BoundedTuoniC2(TuoniC2):
    observes_mutations = True

    def __init__(
        self,
        verify: str | bool = True,
        *,
        request_timeout: float = 30,
        response_max_bytes: int = 16 * 1024 * 1024,
        download_max_bytes: int = 128 * 1024 * 1024,
        max_connections: int = 8,
    ):
        super().__init__(verify=verify)
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be finite and positive")
        if min(response_max_bytes, download_max_bytes, max_connections) < 1:
            raise ValueError("HTTP limits must be positive")
        self.request_timeout = request_timeout
        self.response_max_bytes = response_max_bytes
        self.download_max_bytes = download_max_bytes
        self._max_connections = max_connections
        self._idle_sessions: queue.LifoQueue[requests.Session] = queue.LifoQueue(max_connections)
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._closed = False
        self._redaction_secrets: tuple[str, ...] = ()

    @contextmanager
    def _session(self):
        # Timed-out anyio calls can outlive the thread-pool limiter. Keep a fixed
        # number of sessions regardless of how many abandoned workers remain.
        with self._sessions_lock:
            if self._closed:
                raise RuntimeError("Tuoni HTTP client is closed")
            try:
                session = self._idle_sessions.get_nowait()
            except queue.Empty:
                session = None
                if len(self._sessions) < self._max_connections:
                    session = requests.Session()
                    self._sessions.append(session)
        if session is None:
            try:
                session = self._idle_sessions.get(timeout=self.request_timeout)
            except queue.Empty as exc:
                raise requests.exceptions.Timeout("Tuoni connection pool is busy; reduce concurrent calls") from exc
        try:
            with self._sessions_lock:
                if self._closed:
                    raise RuntimeError("Tuoni HTTP client is closed")
            yield session
        finally:
            with self._sessions_lock:
                if self._closed:
                    session.close()
                else:
                    self._idle_sessions.put_nowait(session)

    def close(self) -> None:
        with self._sessions_lock:
            self._closed = True
            for session in self._sessions:
                session.close()
            self._sessions.clear()
            while not self._idle_sessions.empty():
                self._idle_sessions.get_nowait()

    def _bounded_request(
        self, method: str, url: str, *, max_bytes: int, raise_status: bool = True, **kwargs: Any,
    ) -> requests.Response:
        deadline = time.monotonic() + self.request_timeout
        # Redirects can replay a POST or cross the configured trust boundary.
        # A Tuoni base URL must point directly at the API, without redirects.
        kwargs.pop("stream", None)
        # Observe each HTTP write, including writes hidden inside SDK composite
        # methods. Authentication traffic is outside the requested operation.
        attempt = None if url.endswith(("/api/v2/auth/login", "/api/v2/auth/refresh", "/api/v2/auth/logout")) else mutation_request(method)
        with self._session() as session, session.request(
            method, url, verify=self._verify, stream=True, allow_redirects=False,
            timeout=self._remaining_timeout(deadline), **kwargs,
        ) as response:
            if attempt is not None:
                attempt.respond(response.status_code)
            failed = not 200 <= response.status_code < 300
            limit = min(max_bytes, 64 * 1024) if failed else max_bytes
            length = response.headers.get("Content-Length")
            if not failed and length and length.isdecimal() and int(length) > limit:
                raise ResponseTooLarge(f"Tuoni response exceeds the {limit}-byte limit.")
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise requests.exceptions.Timeout("Tuoni response exceeded the request deadline")
                try:
                    # read1 returns available data rather than waiting to fill a
                    # 64 KiB chunk while a peer continuously trickles tiny writes.
                    chunk = response.raw.read1(min(64 * 1024, limit + 1), decode_content=True)
                except ReadTimeoutError as exc:
                    raise requests.exceptions.Timeout(str(exc)) from exc
                except SSLError as exc:
                    raise requests.exceptions.SSLError(str(exc)) from exc
                except HTTPError as exc:
                    raise requests.exceptions.ConnectionError(str(exc)) from exc
                if time.monotonic() > deadline:
                    raise requests.exceptions.Timeout("Tuoni response exceeded the request deadline")
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    if failed:
                        break
                    raise ResponseTooLarge(f"Tuoni response exceeds the {limit}-byte limit.")
                chunks.append(chunk)
            # TuoniLib's callers expect a normal, already-read Response. The
            # limit above is applied to decompressed chunks, not just headers.
            response._content = b"".join(chunks)
            response._content_consumed = True
            if failed and raise_status:
                message = response.text
                try:
                    body = json.loads(message)
                    if isinstance(body, dict):
                        message = str(body.get("message") or message)
                except ValueError:
                    pass
                raise TuoniHTTPError(response.status_code, message)
            return response

    @staticmethod
    def _remaining_timeout(deadline: float) -> tuple[float, float]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.exceptions.Timeout("Tuoni request expired while waiting for a connection")
        return min(10, remaining), remaining

    def _send_request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        auth = self._auth
        authorization = kwargs.get("headers", {}).get("Authorization", "")
        sent_token = authorization.partition(" ")[2] if isinstance(authorization, str) else None
        body = kwargs.get("json")
        recent = (getattr(auth, "access_token", None), getattr(auth, "refresh_token", None),
                  sent_token, body.get("refreshToken") if isinstance(body, dict) else None)
        # A rejected refresh/read can invalidate the SDK session before the
        # exception reaches MCP. Retain only a bounded redaction snapshot so
        # an upstream echo cannot expose a token that just stopped being current.
        self._redaction_secrets = tuple(dict.fromkeys(
            (*self._redaction_secrets, *(value for value in recent if isinstance(value, str) and value))
        ))[-8:]
        limit = kwargs.pop("max_bytes", self.response_max_bytes)
        if url.endswith(("/api/v2/auth/login", "/api/v2/auth/refresh", "/api/v2/auth/logout")):
            limit = 64 * 1024
        return self._bounded_request(method, url, max_bytes=limit, raise_status=False, **kwargs)

    def login(self, url: str, username: str, password: str) -> None:
        super().login(validate_base_url(url), username, password)

    def ensure_authenticated(self) -> None:
        """Refresh the SDK session before crossing a mutation boundary."""
        self._get_auth()

    def _make_request(self, method: str, uri: str, *, max_bytes: int | None = None, **kwargs: Any):
        if not uri.startswith("/") or uri.startswith("//") or "#" in uri:
            raise ValueError("Tuoni requests require an absolute API path on the configured server")
        if mutation_once_only_active():
            auth = self._get_auth()
            url = f"{auth.url}{uri}"
            response = self._send_request(
                method, url, headers={"Authorization": f"Bearer {auth.access_token}"},
                max_bytes=self.response_max_bytes if max_bytes is None else max_bytes, **kwargs,
            )
            self._raise_for_status(response, method, url)
            return response
        return super()._make_request(
            method, uri,
            max_bytes=self.response_max_bytes if max_bytes is None else max_bytes,
            **kwargs,
        )

    def request_get(self, uri: str, result_as_json: bool = True, result_as_bytes: bool = False):
        limit = self.download_max_bytes if result_as_bytes and not result_as_json else self.response_max_bytes
        response = self._make_request("GET", uri, max_bytes=limit)
        if not response.content:
            return None
        if result_as_json:
            return response.json()
        return response.content if result_as_bytes else response.text

    def request_get_bounded_bytes(self, uri: str, max_bytes: int) -> bytes:
        return self._make_request("GET", uri, max_bytes=min(max_bytes, self.download_max_bytes)).content

    def request_get_file(self, uri: str, file_name: str):
        # The inherited method reads Response.raw, but our bounded transport has
        # already consumed and closed it. Preserve the client's file API too.
        Path(file_name).write_bytes(self.request_get_bounded_bytes(uri, self.download_max_bytes))

    def connect_websocket(self, timeout: float = 10, on_error=None):
        self._get_auth()
        ws = VerifiedTuoniWebSocket(self)
        if on_error is not None:
            ws.on_error(on_error)
        try:
            connected = ws.connect(timeout=min(timeout, self.request_timeout))
        except InvalidStatus as exc:
            status = exc.response.status_code
            # Expose status for auth recovery without server-controlled headers
            # or handshake bodies in resource diagnostics.
            raise TuoniHTTPError(status, f"Tuoni WebSocket upgrade failed (HTTP {status})") from exc
        if not connected:
            raise ExceptionTuoniRequestFailed("Tuoni WebSocket handshake failed within the configured deadline")
        return ws
