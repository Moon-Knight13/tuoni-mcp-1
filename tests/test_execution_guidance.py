"""Execution guidance must prevent guesses and offer a callable recovery."""
from __future__ import annotations

import json
from dataclasses import replace

import jsonschema
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size
from tuoni_mcp.server import build_server


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("execution,expected", [
    ("NEW", "required key execType: EXISTING, NEW, SELF"),
    ({"type": "NEW", "executable": "x"}, "required key execType"),
    ({"execType": "secret-invalid-mode"}, "required key execType"),
    ({"execType": "NEW"}, "execution.executable: field is required"),
    ({"execType": "EXISTING", "pid": "secret-not-a-number"}, "execution.pid: use an integer"),
])
async def test_execution_errors_are_precise_and_schema_recovery_is_read_only(
    patched_server, cfg, fake_c2, mode, execution, expected,
):
    server = build_server(replace(cfg, mode=mode, tool_prefix="client_"))
    arguments = {"agent_guid": "agent-1", "template": "procinfo", "configuration": {},
                 "execution": execution}
    async with create_connected_server_and_client_session(server.protocol_server) as session:
        if mode is MCPMode.DISPATCH:
            response = await session.call_tool("tuoni_call", {
                "name": "tuoni_command_prepare", "arguments": arguments,
            })
        else:
            response = await session.call_tool("tuoni_command_prepare", arguments)
        payload = json.loads(response.content[0].text)
        assert response.isError and payload["code"] == "INVALID_ARGUMENTS"
        assert expected in payload["hint"]
        assert "secret-" not in payload["hint"]
        if "required key" in expected:
            assert "NEW requires executable" in payload["hint"]
            assert "EXISTING requires pid" in payload["hint"]
        assert call_result_size(response) <= ERROR_RESULT_BUDGET
        assert fake_c2.calls == []
        if mode is MCPMode.DISPATCH:
            assert payload["next"] == {
                "name": "client_tuoni_tool_schema", "arguments": {"name": "tuoni_command_prepare"},
            }
            schema = await session.call_tool("tuoni_tool_schema", payload["next"]["arguments"])
            assert not schema.isError
            assert json.loads(schema.content[0].text)["data"]["input_schema"]
        else:
            assert payload.get("next") is None
    assert fake_c2.calls == []


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_plugin_examples_validate_against_the_advertised_prepare_schema(
    patched_server, cfg, fake_c2, mode,
):
    template = {
        "id": "template-procinfo", "name": "procinfo", "pluginId": "shelldot.commands.os",
        "configurationSchema": {"type": "object", "properties": {}},
        "supportedExecUnitTypes": [],
    }
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        "guid": "agent-1", "active": True, "status": "ACTIVE",
        "availableCommandTemplates": [template["id"]],
    }
    fake_c2.responses["/api/v1/command-templates"] = [template]
    server = build_server(replace(cfg, mode=mode))
    async with create_connected_server_and_client_session(server.protocol_server) as session:
        args = {"agent_guid": "agent-1", "template": "procinfo"}
        result = await session.call_tool(
            "tuoni_call" if mode is MCPMode.DISPATCH else "tuoni_command_describe",
            {"name": "tuoni_command_describe", "arguments": args} if mode is MCPMode.DISPATCH else args,
        )
        assert not result.isError
        data = json.loads(result.content[0].text)["data"]
        assert [e["execType"] for e in data["execution_examples"]] == ["SELF", "NEW", "EXISTING"]
        schema = server.registry.get("tuoni_command_prepare").input_schema
        for example in data["execution_examples"]:
            arguments = {**data["prepare_arguments_example"], "execution": example}
            jsonschema.validate(arguments, schema)
            result = await session.call_tool(
                "tuoni_call" if mode is MCPMode.DISPATCH else "tuoni_command_prepare",
                {"name": "tuoni_command_prepare", "arguments": arguments} if mode is MCPMode.DISPATCH else arguments,
            )
            assert not result.isError
    assert all(call[0] == "request_get" for call in fake_c2.calls)
