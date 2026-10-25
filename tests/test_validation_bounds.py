"""Numeric errors must let a client correct its next call without guessing."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import Field

from tuoni_mcp.config import MCPMode, Profile
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import ToolFailure, ToolInput, ToolRegistry, ToolSpec
from tuoni_mcp.server import build_server


async def _call(session, mode, name, arguments):
    if mode is MCPMode.DISPATCH and name != "tuoni_find_tool":
        return await session.call_tool("tuoni_call", {"name": name, "arguments": arguments})
    return await session.call_tool(name, arguments)


CASES = [
    (mode, name, arguments, maximum, default)
    for mode in MCPMode
    for name, arguments, maximum, default in [
        ("tuoni_agents_list", {}, 25, 10),
        ("tuoni_commands_search", {"agent_guid": "agent-1"}, 20, 8),
        ("tuoni_command_get", {"command_id": 42}, 50, 20),
    ]
] + [(MCPMode.DISPATCH, "tuoni_find_tool", {"query": "agents"}, 100, 8)]


@pytest.mark.parametrize("mode,name,arguments,maximum,default", CASES)
@pytest.mark.parametrize("bound", ["lower", "upper"])
async def test_real_mcp_limit_errors_include_range_and_default_without_reading_upstream(
    patched_server, cfg, fake_c2, mode, name, arguments, maximum, default, bound,
):
    invalid = 0 if bound == "lower" else maximum + 1
    server = build_server(replace(cfg, mode=mode))
    async with create_connected_server_and_client_session(server.protocol_server) as session:
        response = await _call(session, mode, name, {**arguments, "limit": invalid})

    payload = json.loads(response.content[0].text)
    assert response.isError and payload["code"] == "INVALID_ARGUMENTS"
    assert f"limit: must be between 1 and {maximum}; omit to use {default}" in payload["hint"]
    assert call_result_size(response) <= ERROR_RESULT_BUDGET
    assert fake_c2.calls == []


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_omitting_the_rejected_limit_succeeds_with_the_advertised_default(
    patched_server, cfg, fake_c2, mode,
):
    uri = "/api/v2/agents?page=0&pageSize=10&status=ACTIVE"
    fake_c2.responses[uri] = {"items": [], "page": 0, "totalCount": 0, "hasMorePages": False}
    server = build_server(replace(cfg, mode=mode))
    async with create_connected_server_and_client_session(server.protocol_server) as session:
        rejected = await _call(session, mode, "tuoni_agents_list", {"limit": 100})
        assert "omit to use 10" in json.loads(rejected.content[0].text)["hint"]
        assert fake_c2.calls == []
        corrected = await _call(session, mode, "tuoni_agents_list", {})

    assert not corrected.isError
    assert len(fake_c2.calls) == 1 and fake_c2.calls[0][1][0] == uri


async def _unused_handler(*_args):
    raise AssertionError("Invalid input must not reach a handler")


def _hint(model, arguments):
    spec = ToolSpec("fixture", "fixture", model, _unused_handler, frozenset({Profile.AGENT_OPS}))
    with pytest.raises(ToolFailure) as error:
        ToolRegistry.validate(spec, arguments)
    return error.value.hint


def test_default_factories_are_never_evaluated_to_suggest_an_omission():
    def factory():
        pytest.fail("Constructing an error must not evaluate a default factory")

    class Input(ToolInput):
        limit: int = Field(default_factory=factory, ge=1, le=25)

    hint = _hint(Input, {"limit": 100})
    assert "between 1 and 25" in hint
    assert "omit" not in hint


def test_required_exclusive_bounds_do_not_suggest_omitting_required_input():
    class Input(ToolInput):
        identifier: int = Field(gt=0)

    hint = _hint(Input, {"identifier": 0})
    assert "identifier: must be greater than 0" in hint
    assert "omit" not in hint


def test_numeric_secret_defaults_are_not_exposed_in_validation_hints():
    class Input(ToolInput):
        password: int = Field(default=987654321, ge=1, le=9)

    hint = _hint(Input, {"password": 100})
    assert "987654321" not in hint
    assert "100" not in hint
    assert "omit" not in hint
