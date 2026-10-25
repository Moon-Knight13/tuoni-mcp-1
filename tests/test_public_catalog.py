from __future__ import annotations

import json
from unittest.mock import MagicMock

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ListToolsResult

from tuoni_mcp.config import ALL_EXPANSION, MCPMode, Profile, Settings
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import (
    Effect,
    InvocationContext,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    build_dispatch_specs,
)
from tuoni_mcp.server import build_server, instructions_for
from tuoni_mcp.tools import build_registry


NATIVE_AGENT_OPS_NAMES = (
    "tuoni_agents_list",
    "tuoni_agent_get",
    "tuoni_commands_search",
    "tuoni_command_describe",
    "tuoni_command_prepare",
    "tuoni_command_update_prepare",
    "tuoni_command_stop_prepare",
    "tuoni_operation_execute",
    "tuoni_command_get",
    "tuoni_commands_list",
    "tuoni_capabilities",
)
READ_ONLY_AGENT_OPS_NAMES = (
    "tuoni_agents_list",
    "tuoni_agent_get",
    "tuoni_commands_search",
    "tuoni_command_describe",
    "tuoni_command_get",
    "tuoni_commands_list",
    "tuoni_capabilities",
)
DISPATCH_NAMES = (
    "tuoni_find_tool",
    "tuoni_tool_schema",
    "tuoni_call",
    "tuoni_operation_execute",
)


def _cfg(
    *,
    profiles: frozenset[Profile] = frozenset({Profile.AGENT_OPS}),
    read_only: bool = False,
    mode: MCPMode = MCPMode.NATIVE,
) -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=profiles,
        read_only=read_only,
        mode=mode,
    )


async def _wire_catalog(cfg: Settings):
    server = build_server(cfg)
    tools = await server.list_tools()
    encoded = ListToolsResult(tools=tools).model_dump_json(
        by_alias=True, exclude_none=True
    ).encode("utf-8")
    return server, tools, encoded


async def test_real_native_default_is_exactly_eleven_small_model_tools() -> None:
    _, tools, encoded = await _wire_catalog(_cfg())
    names = tuple(tool.name for tool in tools)
    by_name = {tool.name: tool for tool in tools}

    assert names == NATIVE_AGENT_OPS_NAMES
    assert len(encoded) <= 11 * 1024
    assert b'"outputSchema"' not in encoded
    assert b'"include_configuration"' not in encoded
    assert b'"include_descriptions"' not in encoded
    assert by_name["tuoni_command_get"].inputSchema["properties"]["wait_seconds"]["maximum"] == 30
    assert "shell_command" in (by_name["tuoni_command_prepare"].description or "")
    assert "complete opaque preparation" in (
        by_name["tuoni_operation_execute"].description or ""
    )


async def test_real_read_only_default_is_exactly_seven_observational_tools() -> None:
    _, tools, encoded = await _wire_catalog(_cfg(read_only=True))

    assert tuple(tool.name for tool in tools) == READ_ONLY_AGENT_OPS_NAMES
    assert len(encoded) <= 10 * 1024
    assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)
    assert all(tool.annotations and not tool.annotations.destructiveHint for tool in tools)


async def test_real_dispatch_default_is_exactly_four_tools() -> None:
    _, tools, encoded = await _wire_catalog(_cfg(mode=MCPMode.DISPATCH))

    assert tuple(tool.name for tool in tools) == DISPATCH_NAMES
    assert len(encoded) <= 10 * 1024
    assert b'"outputSchema"' not in encoded


async def test_read_only_dispatch_instructions_name_its_visible_client_entrypoints():
    from dataclasses import replace

    cfg = replace(_cfg(mode=MCPMode.DISPATCH, read_only=True), tool_prefix="client_")
    server, tools, _ = await _wire_catalog(cfg)
    assert server.instructions == instructions_for(cfg)
    for tool in tools:
        assert cfg.tool_prefix + tool.name in server.instructions
    assert "tuoni_operation_execute" not in server.instructions
    assert "tuoni_command_prepare" not in server.instructions
    assert "Inner name values are unprefixed" in server.instructions


def test_native_workflow_uses_client_names_for_every_command_step():
    from dataclasses import replace

    cfg = replace(_cfg(), tool_prefix="mcp__tuoni__")
    text = instructions_for(cfg)
    for name in ("tuoni_agents_list", "tuoni_commands_search", "tuoni_command_describe",
                 "tuoni_command_prepare", "tuoni_operation_execute", "tuoni_command_get"):
        assert cfg.tool_prefix + name in text


def test_instructions_do_not_recommend_agent_tools_in_excluded_profiles():
    text = instructions_for(_cfg(profiles=frozenset({Profile.ACCESS}), read_only=True))
    assert "tuoni_command_get" not in text
    assert "tuoni_command_prepare" not in text


async def test_real_agent_ops_annotations_match_effect_contract() -> None:
    _, tools, _ = await _wire_catalog(_cfg())
    by_name = {tool.name: tool.annotations for tool in tools}

    for name in READ_ONLY_AGENT_OPS_NAMES:
        annotation = by_name[name]
        assert annotation and annotation.readOnlyHint is True
        assert annotation.destructiveHint is False
        assert annotation.idempotentHint is True
    for name in (
        "tuoni_command_prepare",
        "tuoni_command_update_prepare",
        "tuoni_command_stop_prepare",
    ):
        annotation = by_name[name]
        assert annotation and annotation.readOnlyHint is False
        assert annotation.destructiveHint is False
    execute = by_name["tuoni_operation_execute"]
    assert execute and execute.destructiveHint is True
    assert execute.idempotentHint is True


async def test_default_server_exposes_no_event_resources_or_event_tools() -> None:
    server = build_server(_cfg())
    async with create_connected_server_and_client_session(
        server.protocol_server
    ) as session:
        resources = await session.list_resources()
        templates = await session.list_resource_templates()
        tools = await session.list_tools()

    assert resources.resources == []
    assert templates.resourceTemplates == []
    assert all(tool.name != "tuoni_events_poll" for tool in tools.tools)


def test_profile_isolation_and_all_expansion_excludes_raw() -> None:
    registry = build_registry(_cfg())
    default = registry.select({Profile.AGENT_OPS}, read_only=False)
    events = registry.select({Profile.EVENTS}, read_only=False)
    expanded = registry.select(ALL_EXPANSION, read_only=False)

    assert tuple(spec.name for spec in default) == NATIVE_AGENT_OPS_NAMES
    assert tuple(spec.name for spec in events) == ("tuoni_events_poll", "tuoni_capabilities")
    assert all(Profile.EVENTS not in spec.profiles for spec in default if spec.name != "tuoni_capabilities")
    assert all(Profile.RAW not in spec.profiles for spec in expanded if spec.name != "tuoni_capabilities")
    assert all(spec.name != "tuoni_raw_request" for spec in expanded)


def test_public_native_schema_defaults_and_maxima_match_contract() -> None:
    registry = build_registry(_cfg())
    by_name = {spec.name: spec.input_schema for spec in registry.specs}

    agents_limit = by_name["tuoni_agents_list"]["properties"]["limit"]
    search_limit = by_name["tuoni_commands_search"]["properties"]["limit"]
    describe_limit = by_name["tuoni_command_describe"]["properties"]["limit"]
    command_get = by_name["tuoni_command_get"]["properties"]
    assert (agents_limit["default"], agents_limit["maximum"]) == (10, 25)
    assert (search_limit["default"], search_limit["maximum"]) == (8, 20)
    assert describe_limit["maximum"] == 20
    assert command_get["max_chars"]["default"] == 4096
    assert command_get["max_chars"]["maximum"] == 16_384

    prepare_schema = by_name["tuoni_command_prepare"]
    # The optional wrapper is collapsed away, so the discriminator sits inline.
    execution = prepare_schema["properties"]["execution"]
    assert execution["discriminator"] == {
        "mapping": {
            "EXISTING": "#/$defs/Existing",
            "NEW": "#/$defs/New",
            "SELF": "#/$defs/Self",
        },
        "propertyName": "execType",
    }
    unit_types = [
        "SHELLCODE_NATIVE",
        "DOTNET_EXE",
        "DOTNET_DLL",
        "NATIVE_LIB",
    ]
    definitions = prepare_schema["$defs"]
    for mode in ("Self", "New", "Existing"):
        definition = definitions[mode]
        assert definition["properties"]["execUnitType"]["enum"] == unit_types
    assert set(definitions["New"]["properties"]) >= {
        "execType",
        "execUnitType",
        "executable",
        "suspended",
        "username",
        "password",
        "ppid",
    }
    assert set(definitions["New"]["required"]) == {
        "execType",
        "executable",
    }
    assert set(definitions["Existing"]["required"]) == {
        "execType",
        "pid",
    }


class _Empty(ToolInput):
    pass


async def test_native_and_dispatch_reads_return_the_same_json_envelope() -> None:
    async def handler(_: InvocationContext, __: _Empty):
        return {
            "value": "emoji 🙂 e\u0301",
            "password": "never-visible",
        }

    registry = ToolRegistry()
    spec = registry.add(
        ToolSpec(
            name="tuoni_equivalence_probe",
            description="Read equivalence probe.",
            input_model=_Empty,
            handler=handler,
            profiles=frozenset({Profile.AGENT_OPS}),
            effect=Effect.READ,
        )
    )
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    native = await registry.invoke_spec(spec, {}, invocation)
    dispatch_specs = build_dispatch_specs(registry, (spec,), read_only=False)
    dispatch_call = next(item for item in dispatch_specs if item.name == "tuoni_call")
    dispatched = await registry.invoke_spec(
        dispatch_call,
        {"name": spec.name, "arguments": {}},
        invocation,
    )

    assert native.isError is False
    assert dispatched.isError is False
    assert native.model_dump_json(exclude_none=True) == dispatched.model_dump_json(
        exclude_none=True
    )
    assert "never-visible" not in json.dumps(
        json.loads(native.content[0].text), ensure_ascii=False
    )


async def test_dispatch_find_is_structurally_paginated_below_two_kibibytes() -> None:
    registry = ToolRegistry()
    for index in range(50):
        registry.add(
            ToolSpec(
                name=f"tuoni_probe_{index:02d}",
                description="Probe capability " + "d" * 500,
                input_model=_Empty,
                handler=_ok_probe,
                profiles=frozenset({Profile.AGENT_OPS}),
                effect=Effect.READ,
                keywords=("probe",),
            )
        )
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, registry.specs, read_only=False)
    }
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    arguments = {"query": "probe", "limit": 20}
    seen: list[str] = []

    while True:
        result = await registry.invoke_spec(
            dispatch["tuoni_find_tool"], arguments, invocation
        )
        payload = json.loads(result.content[0].text)
        assert result.isError is False
        assert call_result_size(result) <= 2 * 1024
        seen.extend(item["name"] for item in payload["data"]["tools"])
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]

    assert len(seen) == len(set(seen)) == 50


async def _ok_probe(_: InvocationContext, __: _Empty):
    return {"ok": "probe"}


async def test_dispatch_schema_returns_one_exact_profile_enabled_schema_under_six_kib() -> None:
    registry = build_registry(_cfg())
    targets = registry.select({Profile.AGENT_OPS}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    result = await registry.invoke_spec(
        dispatch["tuoni_tool_schema"],
        {"name": "tuoni_command_prepare"},
        invocation,
    )
    payload = json.loads(result.content[0].text)

    assert result.isError is False
    assert payload["data"]["name"] == "tuoni_command_prepare"
    assert payload["data"]["input_schema"] == registry.get(
        "tuoni_command_prepare"
    ).input_schema
    assert call_result_size(result) <= DETAIL_RESULT_BUDGET

    isolated = await registry.invoke_spec(
        dispatch["tuoni_find_tool"], {"query": "listener"}, invocation
    )
    assert json.loads(isolated.content[0].text)["data"]["tools"] == []
