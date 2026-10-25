"""Live-schema examples are usable through native and dispatch MCP transports."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size
from tuoni_mcp.server import build_server


def _install_template(fake_c2, schema):
    template = {"id": "template-fixture", "name": "fixture-operation", "pluginId": "custom.fixture",
                "description": "A custom plugin command", "configurationSchema": schema,
                "exampleConfigurations": [], "supportedExecUnitTypes": []}
    fake_c2.responses["/api/v1/agents/agent-fixture"] = {
        "guid": "agent-fixture", "status": "ACTIVE", "active": True,
        "availableCommandTemplates": [template["id"]], "metadata": {},
    }
    fake_c2.responses["/api/v1/command-templates"] = [template]


def _payload(result):
    assert not result.isError, result
    assert call_result_size(result) <= DETAIL_RESULT_BUDGET
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "client_"])
async def test_complex_describe_example_can_be_filled_and_prepared_without_missing_required_fields(
    patched_server, cfg, fake_c2, mode, prefix,
):
    names = [f"field_{index}" for index in range(7)]
    nested = [f"nested_{index}" for index in range(5)]
    files = [f"input_{index}" for index in range(5)]
    schema = {"type": "object", "required": [*names, "nested"],
              "properties": {**{name: {"type": "string"} for name in names},
                             "nested": {"type": "object", "required": nested,
                                        "properties": {name: {"type": "integer"} for name in nested}}},
              "additionalProperties": False, "files": {name: {"required": True} for name in files}}
    _install_template(fake_c2, schema)
    server = build_server(replace(cfg, mode=mode, tool_prefix=prefix))

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        async def call(name, arguments):
            if mode is MCPMode.DISPATCH:
                name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
            return await session.call_tool(name, arguments)

        described = _payload(await call("tuoni_command_describe", {
            "agent_guid": "agent-fixture", "template": "fixture-operation", "limit": 1,
        }))
        arguments = copy.deepcopy(described["data"]["prepare_arguments_example"])
        assert set(arguments["configuration"]) == {*names, "nested"}
        assert set(arguments["configuration"]["nested"]) == set(nested)
        assert {item["field"] for item in arguments["attachments"]} == set(files)
        assert not described["data"].get("prepare_example_incomplete", False)
        arguments["configuration"].update({name: "filled-value" for name in names})
        for attachment in arguments["attachments"]:
            attachment["content_base64"] = "eA=="
        prepared = _payload(await call("tuoni_command_prepare", arguments))
        assert prepared["data"]["operation"] == "command.create"
        assert prepared["data"]["preparation_id"]
        following = described["next"]
        assert following is not None
        more = _payload(await session.call_tool(following["name"].removeprefix(prefix), following["arguments"]))
        assert more["data"]["fields"]

    assert all(name == "request_get" for name, *_ in fake_c2.calls)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_incomplete_schema_example_explains_omissions_outside_callable_arguments(
    patched_server, cfg, fake_c2, mode,
):
    names = [f"required_{index}" for index in range(100)]
    _install_template(fake_c2, {"type": "object", "required": names,
                               "properties": {name: {"type": "string"} for name in names}})
    server = build_server(replace(cfg, mode=mode))

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        arguments = {"agent_guid": "agent-fixture", "template": "fixture-operation", "limit": 1}
        if mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": "tuoni_command_describe", "arguments": arguments}
        else:
            name = "tuoni_command_describe"
        result = _payload(await session.call_tool(name, arguments))
        data = result["data"]
        assert data["prepare_example_incomplete"] is True
        assert data["prepare_example_notes"]
        example = data.get("prepare_arguments_example")
        if example is not None:
            assert not {"prepare_example_incomplete", "prepare_example_notes"}.intersection(example)
            assert set(example["configuration"]) <= set(names)
        following = result["next"]
        assert following is not None
        next_page = _payload(await session.call_tool(following["name"], following["arguments"]))
        assert next_page["data"]["fields"]

    assert all(name == "request_get" for name, *_ in fake_c2.calls)
