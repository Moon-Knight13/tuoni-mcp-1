from __future__ import annotations

import asyncio
import io
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from urllib3.response import HTTPResponse

from tuoni_mcp.config import ALL_PROFILES, MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.diagnostics import (
    DIAGNOSTICS_BUDGET,
    IDENTITY_PATH,
    INFO_PATH,
    catalog_summary,
    collect_diagnostics,
)
from tuoni_mcp.http_client import BoundedTuoniC2
from tuoni_mcp.protocol import call_result_size, success_result
from tuoni_mcp.registry import Effect, InvocationContext, ToolInput, ToolRegistry, ToolSpec
from tuoni_mcp.tools.diagnostics import register_specs


def _settings(**overrides) -> Settings:
    return Settings(**{
        "url": "https://backend.example.test/private-base",
        "username": "configured-operator",
        "password": "configured-password-secret",
        "verify": True,
        **overrides,
    })


@pytest.fixture
def upstream(monkeypatch):
    """Use the real bounded client and HTTP decoder without opening sockets."""
    replies = {
        "/api/v2/auth/login": (200, {'tokenType': 'Bearer', 'accessToken': 'fixture-auth-token', 'refreshToken': 'fixture-refresh', 'expiresIn': 3600, 'refreshExpiresAt': '2099-01-01T00:00:00Z'}),
        INFO_PATH: (200, {"version": "0.15.0-SNAPSHOT"}),
        IDENTITY_PATH: (200, {"username": "operator", "enabled": True, "authorities": ["VIEW_RESOURCES"]}),
    }
    calls = []

    def request(_session, method, url, **kwargs):
        path = url.removeprefix("https://backend.example.test/private-base")
        calls.append((method, path, kwargs))
        reply = replies[path]
        if isinstance(reply, Exception):
            raise reply
        status, value = reply
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Length"] = str(len(body))
        response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False)
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = BoundedTuoniC2()
    yield ServerContext(c2=client, cfg=_settings()), replies, calls
    client.close()


async def test_offline_report_never_authenticates_or_probes_and_hides_configuration_secrets(upstream, tmp_path):
    ctx, _, calls = upstream
    ctx.cfg = replace(
        ctx.cfg, verify=str(tmp_path / "private-ca.pem"),
        download_dir=str(tmp_path / "private-downloads"),
    )
    report = await collect_diagnostics(ctx)
    assert report["ok"] is True
    assert report["backend"] == {"status": "not_checked"}
    assert report["transport"]["tls_verification"] == "custom_ca"
    assert report["mcp"]["profiles"] == ["agent-ops"]
    assert report["transport"]["response_max_bytes"] == ctx.cfg.response_max_bytes
    assert calls == []
    encoded = json.dumps(report)
    for hidden in (ctx.cfg.password, ctx.cfg.username, ctx.cfg.url, ctx.cfg.verify, ctx.cfg.download_dir):
        # Match the wire representation too: Windows paths escape backslashes.
        assert json.dumps(hidden)[1:-1] not in encoded


@pytest.mark.parametrize(("url", "verify", "expected"), [
    ("https://backend.example.test", True, "system_ca"),
    ("https://backend.example.test", False, "disabled"),
    ("http://backend.example.test", True, "not_used_http"),
])
async def test_effective_tls_and_disabled_reasons_are_explicit(url, verify, expected):
    cfg = _settings(url=url, verify=verify, read_only=True, mode=MCPMode.DISPATCH)
    report = await collect_diagnostics(ServerContext(c2=SimpleNamespace(), cfg=cfg))
    assert report["transport"]["tls_verification"] == expected
    assert report["unavailable"]["mutations"] == "read_only"
    assert report["unavailable"]["raw"] == "native_mode_required"
    assert report["unavailable"]["local_downloads"] == "download_directory_not_configured"


async def test_network_check_uses_documented_get_contracts_and_shared_authentication(upstream):
    ctx, _, calls = upstream
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is True
    assert report["backend"]["status"] == "ok"
    assert report["backend"]["server"] == {"status": "ok", "version": "0.15.0-SNAPSHOT"}
    assert report["backend"]["identity"]["username"] == "operator"
    assert report["backend"]["identity"]["authorities"] == ["VIEW_RESOURCES"]
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/api/v2/auth/login"), ("GET", INFO_PATH), ("GET", IDENTITY_PATH),
    ]
    assert all(call[2]["headers"]["Authorization"] == "Bearer fixture-auth-token" for call in calls[1:])
    assert all(call[2]["allow_redirects"] is False for call in calls)


@pytest.mark.parametrize(("status", "code"), [(401, "AUTHENTICATION_FAILED"), (403, "FORBIDDEN")])
async def test_failed_login_distinguishes_authentication_from_permission_without_disclosing_body(upstream, status, code):
    ctx, replies, calls = upstream
    replies["/api/v2/auth/login"] = (status, {"message": "password=do-not-print https://secret.example/path"})
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is False
    assert report["backend"]["session"]["code"] == code
    assert report["backend"]["identity"]["status"] == "skipped"
    assert len(calls) == 1
    assert "do-not-print" not in json.dumps(report)
    assert "secret.example" not in json.dumps(report)


@pytest.mark.parametrize(("error", "code"), [
    (requests.exceptions.SSLError("sensitive certificate detail"), "TLS_ERROR"),
    (requests.exceptions.ConnectionError("sensitive address"), "UNREACHABLE"),
    (requests.exceptions.Timeout("sensitive address"), "TIMEOUT"),
])
async def test_transport_failure_codes_are_safe_and_do_not_hide_other_checks(upstream, error, code):
    ctx, replies, calls = upstream
    replies[INFO_PATH] = error
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is False
    assert report["backend"]["status"] == "degraded"
    assert report["backend"]["server"]["code"] == code
    assert report["backend"]["identity"]["status"] == "ok"
    assert calls[-1][1] == IDENTITY_PATH
    assert "sensitive" not in json.dumps(report)


@pytest.mark.parametrize(("status", "code"), [(401, "AUTHENTICATION_FAILED"), (403, "FORBIDDEN"), (404, "ENDPOINT_UNAVAILABLE")])
async def test_endpoint_failures_do_not_replay_requests_or_claim_healthy(upstream, status, code):
    ctx, replies, calls = upstream
    replies[INFO_PATH] = (status, {"message": "upstream-secret"})
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is False
    assert report["backend"]["server"]["code"] == code
    assert report["backend"]["identity"]["status"] == "ok"
    assert len(calls) == 3
    assert "upstream-secret" not in json.dumps(report)


@pytest.mark.parametrize(("path", "body"), [
    (INFO_PATH, {"version": None}),
    (INFO_PATH, b"not JSON"),
    (IDENTITY_PATH, {"username": "operator", "enabled": True}),
    (IDENTITY_PATH, {"username": "operator", "enabled": True, "authorities": [{}]}),
])
async def test_malformed_contract_is_degraded_instead_of_optimistic_success(upstream, path, body):
    ctx, replies, _ = upstream
    replies[path] = (200, body)
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is False
    check = "server" if path == INFO_PATH else "identity"
    assert report["backend"][check]["code"] == "INVALID_RESPONSE"


async def test_disabled_identity_is_reported_as_degraded(upstream):
    ctx, replies, _ = upstream
    replies[IDENTITY_PATH][1]["enabled"] = False
    report = await collect_diagnostics(ctx, check_connection=True)
    assert report["ok"] is False
    assert report["backend"]["identity"]["code"] == "ACCOUNT_DISABLED"


async def test_oversized_identity_is_bounded_redacted_and_excludes_unrelated_user_fields(upstream):
    ctx, replies, _ = upstream
    ctx.cfg = replace(ctx.cfg, profiles=ALL_PROFILES)
    hostile = '"\\' * 1000 + ctx.cfg.password
    replies[IDENTITY_PATH] = (200, {
        "username": "fixture-auth-token\n" + hostile,
        "enabled": True,
        "authorities": [hostile] * 100,
        "password": "not-for-model",
        "configuration": {"url": "https://user:secret@example.test"},
    })
    replies[INFO_PATH] = (200, {"version": hostile})
    registry = ToolRegistry()
    register_specs(registry, ctx.cfg)
    report = await collect_diagnostics(ctx, check_connection=True, catalog=catalog_summary(registry, ctx.cfg))
    result = success_result(report, budget=DIAGNOSTICS_BUDGET)
    assert not result.isError
    assert call_result_size(result) <= DIAGNOSTICS_BUDGET
    assert report["backend"]["identity"]["authorities_omitted"] >= 88
    assert len(report["backend"]["identity"]["authorities"]) <= 12
    encoded = json.dumps(report)
    for secret in (ctx.cfg.password, "fixture-auth-token", "not-for-model", "user:secret"):
        assert secret not in encoded


async def test_tool_registration_stays_offline_validates_bool_and_counts_later_registrations(upstream):
    ctx, _, calls = upstream
    registry = ToolRegistry()
    register_specs(registry, ctx.cfg)

    async def unused(*_args):
        raise AssertionError("Diagnostic must not invoke other tools")

    registry.add(ToolSpec(
        name="fixture_mutation", description="fixture", input_model=ToolInput,
        handler=unused, profiles=frozenset({Profile.AGENT_OPS}), effect=Effect.MUTATE,
    ))
    ctx.cfg = replace(ctx.cfg, read_only=True)
    invocation = InvocationContext(context=SimpleNamespace(), server=ctx)
    spec = registry.get("tuoni_capabilities")
    assert spec is not None
    result = await registry.invoke_spec(spec, {}, invocation)
    payload = json.loads(result.content[0].text)
    assert payload["data"]["catalog"]["enabled_operations"] == 1
    assert payload["data"]["catalog"]["withheld_read_only"] == 1
    assert payload["data"]["catalog"]["by_profile"] == {"agent-ops": 1}
    assert call_result_size(result) <= DIAGNOSTICS_BUDGET
    for value in ("true", 1):
        failed = await registry.invoke_spec(spec, {"check_connection": value}, invocation)
        assert failed.isError
    assert calls == []


async def test_cancellation_propagates_instead_of_becoming_a_degraded_report(upstream, monkeypatch):
    import tuoni_mcp.diagnostics as module

    ctx, _, _ = upstream
    ctx.last_login = time.monotonic()

    async def cancelled(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(module, "call_without_retry", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await collect_diagnostics(ctx, check_connection=True)
