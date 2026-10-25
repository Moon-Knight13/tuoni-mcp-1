from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, ToolFailure, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools import payloads


TEMPLATE = "fixture.payload"
LISTENER = 7
TEMPLATE_PATH = f"/api/v1/payloads/templates/{TEMPLATE}"
LISTENER_PATH = f"/api/v1/listeners/{LISTENER}"


def _context(fake_c2, mode=MCPMode.NATIVE, prefix=""):
    cfg = Settings(
        url="https://example.test", username="fixture", password="fixture", verify=True,
        profiles=frozenset({Profile.ACCESS, Profile.AGENT_OPS}), mode=mode, tool_prefix=prefix,
    )
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    return context, server, cfg


def _metadata(fake_c2, *, allowed=None, supported=None):
    fake_c2.responses[TEMPLATE_PATH] = {
        "id": "deprecated-template-id", "name": TEMPLATE,
        "availableListenerIds": [1, 2] if allowed is None else allowed,
    }
    fake_c2.responses[LISTENER_PATH] = {
        "id": LISTENER, "supportedPayloadTemplateIds": ["different.payload"] if supported is None else supported,
    }


def _tool_harness(fake_c2, mode, prefix):
    context, server, cfg = _context(fake_c2, mode, prefix)
    registry = build_registry(cfg)
    selected = registry.select(cfg.profiles, read_only=False)
    exposed = build_dispatch_specs(registry, selected, read_only=False) if mode is MCPMode.DISPATCH else selected
    specs = {spec.name: spec for spec in exposed}
    invocation = InvocationContext(context=context, server=server)

    def create_payload(*args):
        fake_c2._record("create_payload", *args)
        return 73

    fake_c2.create_payload = create_payload

    async def call(name, arguments):
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        if mode is MCPMode.DISPATCH and name not in specs:
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        assert call_result_size(result) <= (ERROR_RESULT_BUDGET if result.isError else MAX_RESULT_BUDGET)
        return result, json.loads(result.content[0].text)

    return context, call


async def test_template_compatible_listener_needs_no_second_metadata_read(fake_c2):
    _metadata(fake_c2, allowed=[LISTENER])
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check == {"status": "compatible"}
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [TEMPLATE_PATH]


async def test_both_live_metadata_lists_must_confirm_incompatibility(fake_c2):
    _metadata(fake_c2)
    context, _, _ = _context(fake_c2)

    with pytest.raises(ToolFailure) as error:
        await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert error.value.code == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [
        TEMPLATE_PATH, LISTENER_PATH,
    ]
    assert all(name == "request_get" for name, _, _ in fake_c2.calls)


@pytest.mark.parametrize("supported", [[TEMPLATE], [TEMPLATE.upper()], ["deprecated-template-id"]])
async def test_disagreeing_metadata_defers_to_backend_instead_of_blocking(fake_c2, supported):
    _metadata(fake_c2, supported=supported)
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check == {"status": "unknown", "reason": "compatibility_metadata_disagrees"}


@pytest.mark.parametrize("allowed", [None, [], "bad", [True], ["7"], [0], [1, "2"]])
async def test_missing_or_malformed_template_listener_metadata_is_not_a_denial(fake_c2, allowed):
    _metadata(fake_c2)
    fake_c2.responses[TEMPLATE_PATH]["availableListenerIds"] = allowed
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check["status"] == "unknown"
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [TEMPLATE_PATH]


@pytest.mark.parametrize("supported", [None, [], "bad", [None], [1], [""]])
async def test_missing_or_malformed_listener_template_metadata_is_not_a_denial(fake_c2, supported):
    _metadata(fake_c2)
    fake_c2.responses[LISTENER_PATH]["supportedPayloadTemplateIds"] = supported
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check["status"] == "unknown"


@pytest.mark.parametrize("field, value", [("name", "different"), ("id", True), ("id", 8)])
async def test_metadata_for_a_different_resource_is_not_used_to_deny(fake_c2, field, value):
    _metadata(fake_c2)
    if field == "name":
        fake_c2.responses[TEMPLATE_PATH][field] = value
    else:
        fake_c2.responses[LISTENER_PATH][field] = value
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check["status"] == "unknown"


@pytest.mark.parametrize("failing_path", [TEMPLATE_PATH, LISTENER_PATH])
async def test_unavailable_preflight_metadata_is_optional_and_does_not_leak_errors(fake_c2, failing_path):
    _metadata(fake_c2)
    original = fake_c2.request_get

    def get(path):
        if path == failing_path:
            raise ConnectionError("metadata unavailable password=must-not-leak")
        return original(path)

    fake_c2.request_get = get
    context, _, _ = _context(fake_c2)

    check = await payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER)

    assert check == {"status": "unknown", "reason": "compatibility_metadata_unavailable"}
    assert "must-not-leak" not in json.dumps(check)


async def test_caller_cancellation_is_not_swallowed_as_unknown_metadata(fake_c2, monkeypatch):
    started = asyncio.Event()

    async def read(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(payloads, "call", read)
    context, _, _ = _context(fake_c2)
    task = asyncio.create_task(payloads.check_payload_listener_compatibility(context, TEMPLATE, LISTENER))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "client_"])
async def test_incompatible_payload_is_rejected_before_any_mutation(fake_c2, mode, prefix):
    _metadata(fake_c2)
    _, call = _tool_harness(fake_c2, mode, prefix)

    result, body = await call("tuoni_payload_create", {
        "template": TEMPLATE, "listener_id": LISTENER, "configuration": {},
    })

    assert result.isError, body
    assert body["code"] == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert all(name in {"login", "request_get"} for name, _, _ in fake_c2.calls)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_compatible_payload_creation_uses_existing_sdk_path_once(fake_c2, mode):
    _metadata(fake_c2, allowed=[LISTENER])
    _, call = _tool_harness(fake_c2, mode, "client_")

    result, body = await call("tuoni_payload_create", {
        "template": TEMPLATE, "listener_id": LISTENER, "configuration": {},
    })
    if mode is MCPMode.DISPATCH:
        assert not result.isError
        assert not any(name == "create_payload" for name, _, _ in fake_c2.calls)
        result, body = await call(body["next"]["name"], body["next"]["arguments"])

    assert not result.isError, body
    assert len([name for name, _, _ in fake_c2.calls if name == "create_payload"]) == 1
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == (
        [TEMPLATE_PATH, TEMPLATE_PATH] if mode is MCPMode.DISPATCH else [TEMPLATE_PATH]
    )


async def test_dispatch_rechecks_compatibility_before_execution_without_ambiguous_failure(fake_c2):
    _metadata(fake_c2, allowed=[LISTENER])
    _, call = _tool_harness(fake_c2, MCPMode.DISPATCH, "")
    prepared, body = await call("tuoni_payload_create", {
        "template": TEMPLATE, "listener_id": LISTENER, "configuration": {},
    })
    assert not prepared.isError
    _metadata(fake_c2)

    result, body = await call(body["next"]["name"], body["next"]["arguments"])

    assert result.isError
    assert body["code"] == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert not any(name == "create_payload" for name, _, _ in fake_c2.calls)


async def test_prechecked_context_is_scoped_and_bound_to_exact_arguments(fake_c2):
    _metadata(fake_c2)
    context, call = _tool_harness(fake_c2, MCPMode.NATIVE, "")
    arguments = {"template": TEMPLATE, "listener_id": LISTENER, "configuration": {}}

    with payloads.payload_compatibility_prechecked(context, TEMPLATE, LISTENER):
        accepted, _ = await call("tuoni_payload_create", arguments)
    refused, body = await call("tuoni_payload_create", arguments)

    assert not accepted.isError
    assert refused.isError and body["code"] == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert len([name for name, _, _ in fake_c2.calls if name == "create_payload"]) == 1
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [
        TEMPLATE_PATH, LISTENER_PATH,
    ]


async def test_prechecked_context_does_not_apply_to_another_listener(fake_c2):
    _metadata(fake_c2)
    fake_c2.responses["/api/v1/listeners/8"] = {"id": 8, "supportedPayloadTemplateIds": ["different.payload"]}
    context, call = _tool_harness(fake_c2, MCPMode.NATIVE, "")

    with payloads.payload_compatibility_prechecked(context, TEMPLATE, LISTENER):
        result, body = await call("tuoni_payload_create", {
            "template": TEMPLATE, "listener_id": 8, "configuration": {},
        })

    assert result.isError and body["code"] == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert not any(name == "create_payload" for name, _, _ in fake_c2.calls)


async def test_prechecked_context_does_not_apply_to_another_backend(fake_c2):
    context, _, _ = _context(fake_c2)
    other = type(fake_c2)()
    _metadata(other)
    _, call = _tool_harness(other, MCPMode.NATIVE, "")

    with payloads.payload_compatibility_prechecked(context, TEMPLATE, LISTENER):
        result, body = await call("tuoni_payload_create", {
            "template": TEMPLATE, "listener_id": LISTENER, "configuration": {},
        })

    assert result.isError and body["code"] == "PAYLOAD_LISTENER_INCOMPATIBLE"
    assert not any(name == "create_payload" for name, _, _ in other.calls)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_unknown_metadata_preserves_backend_create_validation(fake_c2, mode):
    _, call = _tool_harness(fake_c2, mode, "")

    result, body = await call("tuoni_payload_create", {
        "template": TEMPLATE, "listener_id": LISTENER, "configuration": {},
    })
    if mode is MCPMode.DISPATCH:
        assert not result.isError
        result, body = await call(body["next"]["name"], body["next"]["arguments"])

    assert not result.isError, body
    assert len([name for name, _, _ in fake_c2.calls if name == "create_payload"]) == 1
