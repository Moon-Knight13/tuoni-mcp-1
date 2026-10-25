"""Real loopback WebSockets exercise bounds before application callbacks run."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import socket as sockets
import threading
import time
from unittest.mock import MagicMock
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidMessage

from tuoni_mcp.http_client import (
    BoundedTuoniC2, ResponseTooLarge, TuoniHTTPError, VerifiedTuoniWebSocket, _EventSocket,
)


@contextmanager
def _http(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass


def _client(port, *, limit=1024):
    client = BoundedTuoniC2(request_timeout=0.5, response_max_bytes=limit)
    response = requests.Response()
    response._content = json.dumps({
        "tokenType": "Bearer", "accessToken": "synthetic-token", "refreshToken": "synthetic-refresh",
        "expiresIn": 3600, "refreshExpiresAt": "2099-01-01T00:00:00Z",
    }).encode()
    client._auth = client._parse_tokens(response, f"http://127.0.0.1:{port}", time.monotonic(), object())
    return client


def test_event_redirect_never_forwards_authorization_to_another_origin():
    reached = []

    class OtherOrigin(_QuietHandler):
        def do_GET(self):
            reached.append(self.headers.get("Authorization"))
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _http(OtherOrigin) as other:
        class Redirect(_QuietHandler):
            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer synthetic-token"
                self.send_response(302)
                self.send_header("Location", f"ws://127.0.0.1:{other.server_port}/other")
                self.send_header("Content-Length", "0")
                self.end_headers()

        with _http(Redirect) as origin:
            client = _client(origin.server_port)
            try:
                with pytest.raises(TuoniHTTPError) as error:
                    client.connect_websocket()
                assert error.value.status_code == 302
                assert reached == []
            finally:
                client.close()


@pytest.mark.parametrize("fragmented", [False, True])
def test_event_limit_rejects_frame_headers_before_reading_oversized_payload(fragmented):
    closed = threading.Event()
    close_codes = []

    class Oversized(_QuietHandler):
        def do_GET(self):
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept.decode())
            self.end_headers()
            if fragmented:
                self.wfile.write(b"\x01\x60" + b"x" * 96 + b"\x80\x60")
            else:
                self.wfile.write(b"\x81\x7f" + struct.pack("!Q", 32 * 1024 * 1024))
            # Deliberately send no oversized body. The client must reject based
            # on the declared length, including remaining fragmented-message budget.
            self.wfile.flush()
            self.connection.settimeout(2)
            header = self.rfile.read(2)
            if len(header) == 2 and header[0] & 15 == 8:
                mask = self.rfile.read(4)
                body = self.rfile.read(header[1] & 127)
                body = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
                close_codes.append(struct.unpack("!H", body[:2])[0])
                self.wfile.write(b"\x88\x02" + body[:2])
                self.wfile.flush()
            closed.set()

    with _http(Oversized) as origin:
        client = _client(origin.server_port, limit=128)
        ws = VerifiedTuoniWebSocket(client)
        try:
            assert ws.connect(timeout=0.5) is False
            assert closed.wait(2)
            assert close_codes == [1009]
            assert ws._reader is None or not ws._reader.is_alive()
        finally:
            ws.disconnect()
            client.close()


def test_event_handshake_error_body_is_rejected_before_reading_declared_megabytes():
    disconnected = threading.Event()

    class LargeError(_QuietHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Length", str(16 * 1024 * 1024))
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(2)
            if self.rfile.read(1) == b"":
                disconnected.set()

    with _http(LargeError) as origin:
        client = _client(origin.server_port)
        try:
            with pytest.raises(InvalidMessage):
                client.connect_websocket()
            assert disconnected.wait(2)
        finally:
            client.close()


def test_event_stomp_delivery_and_disconnect_leave_no_reader_thread():
    delivered = []
    received = threading.Event()
    peer_done = threading.Event()
    topic = "/topic/command/result"

    def peer(socket):
        try:
            socket.send("o")
            assert json.loads(socket.recv())[0].startswith("CONNECT\n")
            socket.send("a" + json.dumps(["CONNECTED\n\n\x00"]))
            assert topic in json.loads(socket.recv())[0]
            frame = 'MESSAGE\ndestination:' + topic + '\n\n{"fixture":true}\x00'
            socket.send("a" + json.dumps([frame]))
            with pytest.raises(ConnectionClosedOK):
                socket.recv()
        finally:
            peer_done.set()

    with serve(peer, "127.0.0.1", 0) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        client = _client(upstream.socket.getsockname()[1])
        ws = VerifiedTuoniWebSocket(client)

        def callback(value):
            delivered.append((value, threading.current_thread().name))
            received.set()

        ws.on_event(topic, callback)
        try:
            assert ws.connect(timeout=0.5)
            assert received.wait(2)
            assert delivered == [({"fixture": True}, "tuoni-event-reader")]
            ws.disconnect()
            assert peer_done.wait(2)
            assert not ws.is_connected
            assert ws._reader is not None and not ws._reader.is_alive()
        finally:
            ws.disconnect()
            client.close()
            upstream.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def test_silent_stomp_handshake_closes_reader_receiver_and_keepalive_threads():
    finished = threading.Event()

    def peer(socket):
        try:
            with pytest.raises(ConnectionClosedOK):
                socket.recv()
        finally:
            finished.set()

    with serve(peer, "127.0.0.1", 0) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        client = _client(upstream.socket.getsockname()[1])
        ws = VerifiedTuoniWebSocket(client)
        try:
            assert not ws.connect(timeout=0.2)
            assert finished.wait(2)
            ws._reader.join(timeout=1)
            assert not ws._reader.is_alive()
            assert ws._loop.is_closed()
        finally:
            ws.disconnect()
            client.close()
            upstream.shutdown()
            thread.join(timeout=2)


def test_disconnect_interrupts_send_when_peer_stops_reading():
    release = threading.Event()
    sending = threading.Event()
    stopped = threading.Event()

    def peer(socket):
        socket.send("o")
        socket.recv()
        socket.send('a["CONNECTED\\n\\n\\u0000"]')
        release.wait(5)

    with serve(peer, "127.0.0.1", 0, max_size=None, max_queue=1) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        client = _client(upstream.socket.getsockname()[1])
        ws = VerifiedTuoniWebSocket(client)
        sender = None
        try:
            assert ws.connect(timeout=1)
            ws._ws.connection.transport.get_extra_info("socket").setsockopt(
                sockets.SOL_SOCKET, sockets.SO_SNDBUF, 4096,
            )

            async def saturate():
                try:
                    # Exercise a send blocked by a saturated transport. The
                    # application handoff itself also has a separate queue cap.
                    sending.set()
                    for _ in range(256):
                        await ws._ws.connection.send(b"x" * 65536)
                except (ConnectionClosed, OSError):
                    pass
                finally:
                    stopped.set()

            sender = asyncio.run_coroutine_threadsafe(saturate(), ws._loop)
            assert sending.wait(1)
            assert not stopped.wait(0.1)
            start = time.monotonic()
            ws.disconnect()
            assert time.monotonic() - start < 2
            assert stopped.wait(1)
            assert not ws._reader.is_alive()
            assert ws._loop.is_closed()
        finally:
            release.set()
            ws.disconnect()
            client.close()
            if sender is not None:
                sender.result(timeout=2)
            upstream.shutdown()
            thread.join(timeout=2)


def test_control_handoff_bounds_frames_and_schedules_one_wakeup_per_batch():
    client = _client(1)
    ws = VerifiedTuoniWebSocket(client)
    ws._loop = MagicMock()
    ws._async_stop = MagicMock()
    transport = _EventSocket(ws, MagicMock())
    try:
        for _ in range(32):
            transport.send("fixture")
        assert transport.pending.qsize() == 32
        assert ws._loop.call_soon_threadsafe.call_count == 1
        with pytest.raises(ResponseTooLarge):
            transport.send("overflow")
        assert ws._stop_requested.is_set()
        assert transport.pending.qsize() == 32
        assert ws._loop.call_soon_threadsafe.call_count == 2
        for _ in range(100):
            ws.disconnect()
        assert ws._loop.call_soon_threadsafe.call_count == 2
    finally:
        client.close()
