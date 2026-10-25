"""SDK sessions retain bounded I/O and exactly-once MCP request boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest
import requests
from tuoni.TuoniExceptions import ExceptionTuoniAuthentication, ExceptionTuoniRequestFailed

from tuoni_mcp.config import Settings
from tuoni_mcp.connection import ServerContext, call_without_retry, ensure_fresh_token
from tuoni_mcp.diagnostics import collect_diagnostics
from tuoni_mcp.http_client import BoundedTuoniC2, VerifiedTuoniWebSocket
from tuoni_mcp.registry import Effect, InvocationContext, ToolInput, ToolRegistry, ToolSpec
from tuoni_mcp.config import Profile


def tokens(access="access-one", refresh="refresh-one", *, deadline="2099-01-01T00:00:00Z"):
    return {"accessToken": access, "refreshToken": refresh, "tokenType": "Bearer",
            "expiresIn": 3600, "refreshExpiresAt": deadline}


@contextmanager
def upstream(*, rejected=False, always_rejected=False, stalled_refresh=False,
             changed_deadline=False, echo_tokens=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            calls.append(("POST", self.path, self.headers.get("Authorization"), body))
            if self.path == "/api/v2/auth/login":
                self.reply(200, tokens())
            elif self.path == "/api/v2/auth/refresh":
                assert body == {"refreshToken": "refresh-one"}
                if stalled_refresh:
                    time.sleep(0.2)
                deadline = "2100-01-01T00:00:00Z" if changed_deadline else "2099-01-01T00:00:00Z"
                self.reply(200, tokens("access-two", "refresh-two", deadline=deadline))
            elif self.path == "/api/v2/auth/logout":
                self.reply(204, None)
            elif self.path == "/mutation":
                self.reply(401, {"message": "Access expired"})
            else:
                self.reply(404, {})

        def do_GET(self):
            authorization = self.headers.get("Authorization")
            calls.append(("GET", self.path, authorization, None))
            if self.path == "/api/v2/info":
                self.reply(200, {"version": "access-one refresh-one" if echo_tokens else "0.17.1"})
            elif self.path == "/api/v1/users/me":
                self.reply(200, {"username": "access-one refresh-one" if echo_tokens else "operator",
                                 "enabled": True, "authorities": ["VIEW_RESOURCES"]})
            elif always_rejected or rejected and authorization == "Bearer access-one":
                self.reply(401, {"message": "Access expired; echoed access-two refresh-two"})
            else:
                self.reply(200, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def context(client, url, *, last_login=None):
    return ServerContext(client, Settings(url=url, username="operator", password="password", verify=True),
                         last_login=time.monotonic() if last_login is None else last_login)


def test_concurrent_reads_rotate_an_expired_access_token_once():
    with upstream() as (url, calls):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            client._auth = replace(client._auth, refresh_at=time.monotonic() - 1)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: client.request_get("/read"), range(16)))
            assert results == [{"ok": True}] * 16
            assert [path for method, path, *_ in calls if method == "POST"] == [
                "/api/v2/auth/login", "/api/v2/auth/refresh",
            ]
            assert all(header == "Bearer access-two" for method, _, header, _ in calls if method == "GET")
        finally:
            client.close()


def test_rejected_read_refreshes_and_retries_only_that_read():
    with upstream(rejected=True) as (url, calls):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            assert client.request_get("/read") == {"ok": True}
            assert [(method, path) for method, path, *_ in calls] == [
                ("POST", "/api/v2/auth/login"), ("GET", "/read"),
                ("POST", "/api/v2/auth/refresh"), ("GET", "/read"),
            ]
        finally:
            client.close()


def test_rejected_mutation_is_never_replayed_by_the_sdk():
    with upstream() as (url, calls):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            with pytest.raises(ExceptionTuoniAuthentication):
                client.request_post("/mutation", {"operation": "once"})
            assert sum(path == "/mutation" for _, path, *_ in calls) == 1
            assert client._auth.access_token == "access-two"
        finally:
            client.close()


async def test_once_only_mcp_boundary_never_refreshes_or_replays_a_rejection():
    with upstream() as (url, calls):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            with pytest.raises(ExceptionTuoniAuthentication):
                await call_without_retry(context(client, url), client.request_post, "/mutation", {"operation": "once"})
            assert [(method, path) for method, path, *_ in calls] == [
                ("POST", "/api/v2/auth/login"), ("POST", "/mutation"),
            ]
        finally:
            client.close()


@pytest.mark.parametrize("failure", ["stalled_refresh", "changed_deadline"])
def test_failed_refresh_invalidates_the_consumed_token_without_retry(failure):
    with upstream(**{failure: True}) as (url, calls):
        client = BoundedTuoniC2(request_timeout=0.05 if failure == "stalled_refresh" else 1)
        try:
            client.login(url, "operator", "password")
            client._auth = replace(client._auth, refresh_at=time.monotonic() - 1)
            with pytest.raises((requests.exceptions.Timeout, ExceptionTuoniRequestFailed)):
                client.request_get("/read")
            assert client._auth is None
            count = len(calls)
            with pytest.raises(ExceptionTuoniAuthentication):
                client.request_get("/read")
            assert len(calls) == count
            assert sum(path == "/api/v2/auth/refresh" for _, path, *_ in calls) == 1
        finally:
            client.close()


async def test_preflight_uses_refresh_sessions_instead_of_a_password_login_timer():
    with upstream() as (url, calls):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            ctx = context(client, url, last_login=time.monotonic() - 1000)
            await ensure_fresh_token(ctx)
            assert len(calls) == 1
            client._auth = replace(client._auth, refresh_at=time.monotonic() - 1)
            await ensure_fresh_token(ctx)
            assert [path for _, path, *_ in calls] == ["/api/v2/auth/login", "/api/v2/auth/refresh"]
        finally:
            client.close()


async def test_backend_diagnostics_redact_both_current_session_tokens():
    with upstream(echo_tokens=True) as (url, _):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            result = await collect_diagnostics(context(client, url), check_connection=True)
            assert result["ok"]
            encoded = json.dumps(result)
            assert "access-one" not in encoded and "refresh-one" not in encoded
            assert "[REDACTED]" in encoded
        finally:
            client.close()


async def test_registry_error_scrubs_tokens_before_and_after_session_rotation():
    with upstream() as (url, _):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            registry = ToolRegistry()

            async def fail(invocation, arguments):
                client._auth = replace(client._auth, access_token="access-two", refresh_token="refresh-two")
                raise ExceptionTuoniRequestFailed(
                    "Backend echoed access-one refresh-one access-two refresh-two password", status_code=503,
                )

            spec = ToolSpec(name="fixture_read", description="Read a fixture", input_model=ToolInput,
                            handler=fail, profiles=frozenset({Profile.AGENT_OPS}), effect=Effect.READ)
            registry.add(spec)
            result = await registry.invoke_spec(spec, {}, InvocationContext(context=None, server=context(client, url)))
            encoded = result.model_dump_json()
            assert all(secret not in encoded for secret in (
                "access-one", "refresh-one", "access-two", "refresh-two", "password",
            ))
            payload = json.loads(result.content[0].text)
            assert payload["code"] == "TUONI_SERVER_ERROR" and payload["retryable"] is True
        finally:
            client.close()


async def test_registry_error_scrubs_rotated_tokens_after_the_sdk_invalidates_them():
    with upstream(always_rejected=True) as (url, _):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            registry = ToolRegistry()

            async def read(invocation, arguments):
                return client.request_get("/read")

            spec = ToolSpec(name="fixture_read", description="Read a fixture", input_model=ToolInput,
                            handler=read, profiles=frozenset({Profile.AGENT_OPS}), effect=Effect.READ)
            registry.add(spec)
            result = await registry.invoke_spec(spec, {}, InvocationContext(context=None, server=context(client, url)))
            assert result.isError and client._auth is None
            assert all(secret not in result.model_dump_json() for secret in (
                "access-one", "refresh-one", "access-two", "refresh-two",
            ))
        finally:
            client.close()


async def test_event_authentication_watch_never_waits_for_the_sdk_http_lock():
    import asyncio

    with upstream() as (url, _):
        client = BoundedTuoniC2()
        client.login(url, "operator", "password")
        socket = VerifiedTuoniWebSocket(client)
        socket._connection_auth = client._auth
        socket._identity = client._auth.identity
        held = threading.Event()
        release = threading.Event()

        def hold_lock():
            with client._auth_lock:
                held.set()
                release.wait(2)

        worker = threading.Thread(target=hold_lock, daemon=True)
        worker.start()
        assert held.wait(1)
        started = time.monotonic()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(socket._watch_authentication(), timeout=0.05)
            assert time.monotonic() - started < 0.3
        finally:
            release.set()
            worker.join(timeout=1)
            client.close()


async def test_quiet_event_watch_ends_at_the_access_refresh_deadline():
    import asyncio

    with upstream() as (url, _):
        client = BoundedTuoniC2()
        try:
            client.login(url, "operator", "password")
            client._auth = replace(client._auth, refresh_at=time.monotonic() + 0.02)
            socket = VerifiedTuoniWebSocket(client)
            socket._connection_auth = client._auth
            socket._identity = client._auth.identity
            await asyncio.wait_for(socket._watch_authentication(), timeout=0.2)
        finally:
            client.close()


@pytest.mark.parametrize("existing_socket,expected_attempt", [(True, 1), (False, 30)])
async def test_event_watchdog_renews_quiet_sockets_promptly_without_hammering_offline_servers(
    fake_c2, monkeypatch, existing_socket, expected_attempt,
):
    from types import SimpleNamespace
    import tuoni_mcp.events as module

    cfg = Settings(url="https://example.test", username="operator", password="password", verify=True)
    bridge = module.EventBridge(ServerContext(fake_c2, cfg))
    bridge.ws = SimpleNamespace(is_connected=False) if existing_socket else None
    bridge._retry_after = 30
    clock = [0]
    attempts = []

    async def tick(seconds):
        assert seconds == 1
        clock[0] += seconds

    async def reconnect():
        attempts.append(clock[0])
        bridge._stopping = True

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module.asyncio, "sleep", tick)
    monkeypatch.setattr(bridge, "_reconnect_if_needed", reconnect)
    await bridge._watchdog_loop()
    assert attempts == [expected_attempt]
