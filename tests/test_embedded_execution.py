"""Embedded commands must not advertise or accept plugin execution settings."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode
from tuoni_mcp.server import build_server


def _catalog(fake_c2, *, plugin_id="shelldot.commands.native", name="ls"):
    template = {
        "id": "template-listing",
        "name": name,
        "pluginId": plugin_id,
        "scope": "global",
        "alias": name != "ls",
        "configurationSchema": {
            "type": "object",
            "properties": {"dir": {"type": "string", "minLength": 1}},
            "required": ["dir"],
            "additionalProperties": False,
        },
        "supportedExecUnitTypes": [],
        "exampleConfigurations": [],
    }
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        "guid": "agent-1",
        "active": True,
        "status": "ACTIVE",
        "availableCommandTemplates": [template["id"]],
    }
    fake_c2.responses["/api/v1/command-templates"] = [template]
    fake_c2.responses[("POST", "/api/v1/agents/agent-1/commands")] = {
        "id": 42,
        "commandTemplateId": template["id"],
        "commandTemplateName": name,
        "status": "CREATED",
    }


async def _call(session, mode, name, arguments):
    if mode is MCPMode.DISPATCH and name != "tuoni_operation_execute":
        arguments = {"name": name, "arguments": arguments}
        name = "tuoni_call"
    result = await session.call_tool(name, arguments)
    return result, json.loads(result.content[0].text)


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("name", ["ls", "custom-listing"])
async def test_embedded_commands_and_aliases_execute_without_execution_settings(
    patched_server, fake_c2, cfg, mode, name,
):
    _catalog(fake_c2, name=name)
    mcp = build_server(replace(cfg, mode=mode))
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        described, description = await _call(session, mode, "tuoni_command_describe", {
            "agent_guid": "agent-1", "template": name,
        })
        assert not described.isError
        assert description["data"]["template"]["execution_applicable"] is False
        assert description["data"]["template"]["execution_modes"] == []
        assert "execution_examples" not in description["data"]

        prepared, preparation = await _call(session, mode, "tuoni_command_prepare", {
            "agent_guid": "agent-1", "template": name, "configuration": {"dir": "."},
        })
        assert not prepared.isError
        executed, _ = await _call(session, mode, **preparation["next"])
        assert not executed.isError

    posts = [call for call in fake_c2.calls if call[0] == "request_post"]
    assert len(posts) == 1
    assert "execConf" not in posts[0][1][1]


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("name", ["ls", "custom-listing"])
@pytest.mark.parametrize("execution", [
    {"execType": "SELF"},
    {"execType": "NEW", "executable": "missing.exe"},
    {"execType": "EXISTING", "pid": 1234},
])
async def test_embedded_commands_reject_execution_before_storing_preparations(
    patched_server, fake_c2, cfg, mode, name, execution,
):
    _catalog(fake_c2, name=name)
    mcp = build_server(replace(cfg, mode=mode))
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        result, payload = await _call(session, mode, "tuoni_command_prepare", {
            "agent_guid": "agent-1", "template": name,
            "configuration": {"dir": "."}, "execution": execution,
        })
        assert result.isError
        assert payload["code"] == "EXECUTION_NOT_APPLICABLE"
        assert "omit execution" in payload["hint"].lower()
        assert "preparation_id" not in payload.get("data", {})
        assert payload.get("next") is None

    assert all(call[0] == "request_get" for call in fake_c2.calls)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_plugin_commands_with_empty_format_lists_keep_execution_settings(
    patched_server, fake_c2, cfg, mode,
):
    _catalog(fake_c2, plugin_id="example.commands")
    mcp = build_server(replace(cfg, mode=mode))
    execution = {"execType": "NEW", "executable": "example.exe"}
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        described, description = await _call(session, mode, "tuoni_command_describe", {
            "agent_guid": "agent-1", "template": "ls",
        })
        assert not described.isError
        assert description["data"]["template"]["execution_applicable"] is True
        assert description["data"]["template"]["execution_modes"] == ["SELF", "NEW", "EXISTING"]

        prepared, preparation = await _call(session, mode, "tuoni_command_prepare", {
            "agent_guid": "agent-1", "template": "ls",
            "configuration": {"dir": "."}, "execution": execution,
        })
        assert not prepared.isError
        executed, _ = await _call(session, mode, **preparation["next"])
        assert not executed.isError

    posts = [call for call in fake_c2.calls if call[0] == "request_post"]
    assert len(posts) == 1
    assert posts[0][1][1]["execConf"] == execution
