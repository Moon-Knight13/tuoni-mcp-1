from __future__ import annotations

import gzip
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from tuoni.TuoniExceptions import ExceptionTuoniAuthentication, ExceptionTuoniRequestFailed

from tuoni_mcp.http_client import BoundedTuoniC2, ResponseTooLarge


@pytest.fixture
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization"), self.client_address))
            status, body = 200, b'{"ok":true}'
            headers = {}
            if self.path == "/api/v2/auth/login":
                if self.headers.get("Authorization") != "Basic dXNlcjpwYXNz":
                    status, body = 401, b"invalid credentials"
                else:
                    body = b'{"tokenType":"Bearer","accessToken":"test-token","refreshToken":"fixture-refresh","expiresIn":3600,"refreshExpiresAt":"2099-01-01T00:00:00Z"}'
            elif self.path == "/oversize":
                body = b"x" * 200
            elif self.path == "/gzip":
                body = gzip.compress(b"x" * 1000)
                headers["Content-Encoding"] = "gzip"
            elif self.path == "/slow":
                time.sleep(0.25)
            elif self.path == "/pause":
                time.sleep(0.02)
            elif self.path == "/trickle":
                body = b"x" * 100
            elif self.path == "/redirect":
                status, body = 307, b"redirect"
                headers["Location"] = "/must-not-follow"
            elif self.path == "/forbidden":
                status, body = 403, b'{"message":"command 503 not found"}'
            elif self.path == "/forbidden-trickle":
                status, body = 403, b"x" * 100
            elif self.path == "/unavailable":
                status, body = 503, b"service unavailable"
            elif self.path == "/request-timeout":
                status, body = 408, b"request timeout"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                if self.path in {"/trickle", "/forbidden-trickle"}:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.02)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    server.server_close()
    thread.join()


def test_client_reuses_connections_preserves_status_and_never_follows_redirects(upstream):
    url, calls = upstream
    client = BoundedTuoniC2()
    try:
        client.login(url, "user", "pass")
        assert client.request_get("/read") == {"ok": True}
        assert client.request_get("/read") == {"ok": True}
        assert len({address for _, _, address in calls}) == 1
        assert calls[1][1] == "Bearer test-token"
        with pytest.raises(ExceptionTuoniRequestFailed) as error:
            client.request_get("/forbidden")
        assert error.value.status_code == 403
        assert "command 503 not found" in error.value.message
        with pytest.raises(ExceptionTuoniRequestFailed) as error:
            client.request_post("/redirect", {"operation": "once"})
        assert error.value.status_code == 307
        assert not any(path == "/must-not-follow" for path, _, _ in calls)
    finally:
        client.close()
    assert not client._sessions


def test_stream_limits_cover_content_length_decompression_and_binary_downloads(upstream, tmp_path):
    url, _ = upstream
    client = BoundedTuoniC2(response_max_bytes=100, download_max_bytes=300)
    try:
        client.login(url, "user", "pass")
        for path in ("/oversize", "/gzip"):
            with pytest.raises(ResponseTooLarge):
                client.request_get(path)
        assert client.request_get("/oversize", False, True) == b"x" * 200
        destination = tmp_path / "download.bin"
        client.request_get_file("/oversize", str(destination))
        assert destination.read_bytes() == b"x" * 200
        with pytest.raises(ResponseTooLarge):
            client.request_get("/gzip", False, True)
        for path in ("https://other.test/", "//other.test/", "/read#fragment"):
            with pytest.raises(ValueError):
                client.request_get(path)
    finally:
        client.close()


def test_socket_timeout_and_login_failure_are_bounded(upstream):
    url, _ = upstream
    client = BoundedTuoniC2(request_timeout=0.1)
    try:
        with pytest.raises(ExceptionTuoniAuthentication):
            client.login(url, "user", "wrong")
        client.login(url, "user", "pass")
        start = time.monotonic()
        with pytest.raises(requests.exceptions.Timeout):
            client.request_get("/slow")
        assert time.monotonic() - start < 2
    finally:
        client.close()


def test_slow_trickle_does_not_hold_a_worker_until_a_full_chunk_arrives(upstream):
    url, _ = upstream
    client = BoundedTuoniC2(request_timeout=0.15)
    try:
        client.login(url, "user", "pass")
        start = time.monotonic()
        with pytest.raises(requests.exceptions.Timeout):
            client.request_get("/trickle", False, True)
        assert time.monotonic() - start < 0.8
    finally:
        client.close()


def test_connection_pool_is_bounded_and_reused_across_many_threads(upstream):
    url, calls = upstream
    client = BoundedTuoniC2(max_connections=2)
    try:
        client.login(url, "user", "pass")
        with ThreadPoolExecutor(max_workers=16) as workers:
            responses = list(workers.map(lambda _: client.request_get("/pause"), range(32)))
        assert responses == [{"ok": True}] * 32
        assert len({address for _, _, address in calls}) <= 2
    finally:
        client.close()


def test_event_callbacks_use_the_reader_thread_and_isolate_subscriber_errors(monkeypatch):
    import tuoni_mcp.http_client as module

    client = BoundedTuoniC2()
    ws = module.VerifiedTuoniWebSocket(client)
    delivered = []

    def broken(data):
        raise ValueError("subscriber failed")

    ws.on_event("/topic/command/result", broken)
    ws.on_event("/topic/command/result", lambda data: delivered.append((data, threading.get_ident())))
    worker = MagicMock()
    monkeypatch.setattr(module.threading, "Thread", worker)
    frame = 'MESSAGE\ndestination:/topic/command/result\n\n{"id":1}\x00'
    for _ in range(1000):
        ws._handle_stomp_frame(frame)
    assert delivered == [({"id": 1}, threading.get_ident())] * 1000
    worker.assert_not_called()
    client.close()


def test_event_frames_are_limited_before_sockjs_json_parsing():
    import tuoni_mcp.http_client as module

    client = BoundedTuoniC2(response_max_bytes=100)
    ws = module.VerifiedTuoniWebSocket(client)
    app = MagicMock()
    ws._on_message(app, "a" + "🙂" * 50)
    assert ws._connect_event.is_set()
    app.close.assert_called_once()
    client.close()
