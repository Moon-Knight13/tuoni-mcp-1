"""Credential scrubbing must preserve trusted recovery routing identities."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import (
    InvocationContext, ToolFailure, ToolInput, ToolSpec, build_dispatch_specs,
)
from tuoni_mcp.server import build_server
from tuoni_mcp.tools import build_registry


def _harness(fake_c2, mode, prefix, password):
    cfg = Settings(url="https://example.test", username="tuoni", password=password, verify=True,
                   mode=mode, tool_prefix=prefix, read_only=True)
    fake_c2._token = "current-private-token"
    fake_c2._redaction_secrets = ("rotated-private-token",)
    registry = build_registry(cfg)
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    invocation = InvocationContext(
        context=SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server)), server=server,
    )
    selected = registry.select(cfg.profiles, read_only=True)
    specs = {spec.name: spec for spec in (selected if mode is MCPMode.NATIVE else
                                        build_dispatch_specs(registry, selected, read_only=True))}

    async def call(arguments=None, *, following=None):
        if following is not None:
            name, arguments = following["name"].removeprefix(prefix), following["arguments"]
        elif mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": "tuoni_command_get", "arguments": arguments}
        else:
            name = "tuoni_command_get"
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        if result.isError:
            assert call_result_size(result) <= ERROR_RESULT_BUDGET
        return result, json.loads(result.content[0].text)

    return call, registry, invocation


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "tuoni_"])
@pytest.mark.parametrize("password", ["tuoni", "name", "next", "arguments", "command_id"])
async def test_result_change_recovery_remains_callable_when_credentials_overlap_protocol_identifiers(
    fake_c2, mode, prefix, password,
):
    call, _, _ = _harness(fake_c2, mode, prefix, password)
    fake_c2.responses["/api/v1/commands/5565"] = {
        "id": 5565, "status": "COMPLETE",
        "result": {"status": "success", "childResults": [{"type": "text", "name": "STDOUT", "value": "visible"}]},
    }

    result, changed = await call({"command_id": 5565, "part_index": 0, "result_version": "0" * 64})

    assert result.isError
    assert changed["code"] == "RESULT_CHANGED"
    expected = {"name": prefix + "tuoni_command_get", "arguments": {"command_id": 5565}}
    if mode is MCPMode.DISPATCH:
        expected = {"name": prefix + "tuoni_call", "arguments": {"name": "tuoni_command_get", "arguments": {"command_id": 5565}}}
    assert changed["next"] == expected
    restarted, manifest = await call(following=changed["next"])
    assert not restarted.isError
    assert manifest["data"]["command_id"] == 5565


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "tuoni_"])
async def test_recovery_keeps_only_registered_names_and_scrubs_untrusted_argument_values(fake_c2, mode, prefix):
    _, registry, invocation = _harness(fake_c2, mode, prefix, "tuoni")

    async def fail(_, __):
        fake_c2._token = "refreshed-private-token"
        raise ToolFailure(
            "FIXTURE_FAILURE", "tuoni current-private-token rotated-private-token refreshed-private-token",
            hint="current-private-token rotated-private-token refreshed-private-token",
            next_call={"name": "tuoni_commands_search", "arguments": {
                "agent_guid": "agent-1", "query": "tuoni current-private-token rotated-private-token refreshed-private-token",
                "configuration": {"current-private-token": "rotated-private-token"},
            }},
        )

    spec = ToolSpec("fixture_failure", "Fixture", ToolInput, fail, frozenset({Profile.AGENT_OPS}))
    result = await registry.invoke_spec(spec, {}, invocation)
    payload = json.loads(result.content[0].text)

    assert result.isError
    assert payload["next"]["name"] == prefix + "tuoni_commands_search"
    assert payload["next"]["arguments"]["agent_guid"] == "agent-1"
    for secret in ("current-private-token", "rotated-private-token", "refreshed-private-token"):
        assert secret not in result.content[0].text
    assert "tuoni" not in payload["message"]
    assert "tuoni" not in payload["next"]["arguments"]["query"]


async def test_unknown_recovery_target_does_not_receive_a_secret_scrubbing_exemption(fake_c2):
    _, registry, invocation = _harness(fake_c2, MCPMode.NATIVE, "", "tuoni")

    async def fail(_, __):
        raise ToolFailure("FIXTURE_FAILURE", "failure", next_call={
            "name": "current-private-token", "arguments": {"query": "rotated-private-token"},
        })

    spec = ToolSpec("fixture_failure", "Fixture", ToolInput, fail, frozenset({Profile.AGENT_OPS}))
    result = await registry.invoke_spec(spec, {}, invocation)

    assert "current-private-token" not in result.content[0].text
    assert "rotated-private-token" not in result.content[0].text


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "tuoni_"])
async def test_real_mcp_transport_follows_result_changed_recovery_with_overlapping_credentials(
    patched_server, cfg, fake_c2, mode, prefix,
):
    fake_c2.responses["/api/v1/commands/5565"] = {
        "id": 5565, "status": "COMPLETE",
        "result": {"status": "success", "childResults": [{"type": "text", "name": "STDOUT", "value": "visible"}]},
    }
    server = build_server(replace(cfg, mode=mode, tool_prefix=prefix, username="tuoni", password="tuoni"))

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        name = "tuoni_command_get"
        arguments = {"command_id": 5565, "part_index": 0, "result_version": "0" * 64}
        if mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
        failed = await session.call_tool(name, arguments)
        assert failed.isError
        assert call_result_size(failed) <= ERROR_RESULT_BUDGET
        recovery = json.loads(failed.content[0].text)["next"]
        expected = prefix + ("tuoni_call" if mode is MCPMode.DISPATCH else "tuoni_command_get")
        assert recovery["name"] == expected
        manifest = await session.call_tool(recovery["name"].removeprefix(prefix), recovery["arguments"])
        assert not manifest.isError
        assert json.loads(manifest.content[0].text)["data"]["command_id"] == 5565
