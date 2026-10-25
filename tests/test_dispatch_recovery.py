from __future__ import annotations

import json
from types import SimpleNamespace

import jsonschema
import pytest
from pydantic import ValidationError

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import Effect, InvocationContext, ToolInput, ToolSpec, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools.agents import AgentsListInput
from tuoni_mcp.tools.commands import CommandDescribeInput, CommandPrepareInput


def _catalog(*, read_only=False, prefix="", password="test"):
    cfg = Settings(
        url="https://example.test", username="test", password=password, verify=True,
        profiles=frozenset({Profile.AGENT_OPS}), read_only=read_only,
        mode=MCPMode.DISPATCH, tool_prefix=prefix,
    )
    registry = build_registry(cfg)
    adapters = {spec.name: spec for spec in build_dispatch_specs(
        registry, registry.select(cfg.profiles, read_only=read_only), read_only=read_only,
    )}
    # No upstream client: schema inspection and error recovery must stay local.
    invocation = InvocationContext(context=None, server=SimpleNamespace(cfg=cfg))
    return registry, adapters, invocation


async def _invoke(catalog, name, arguments):
    registry, adapters, invocation = catalog
    result = await registry.invoke_spec(adapters[name], arguments, invocation)
    return result, json.loads(result.content[0].text)


@pytest.mark.parametrize("read_only", [False, True])
async def test_every_exposed_entrypoint_describes_its_exact_wire_schema(read_only):
    catalog = _catalog(read_only=read_only)
    for name, spec in catalog[1].items():
        result, payload = await _invoke(catalog, "tuoni_tool_schema", {"name": name})
        assert not result.isError
        assert payload["data"]["input_schema"] == spec.wire_tool().inputSchema
        assert payload["data"]["name"] == name
        assert payload["data"]["effect"] == spec.effect.value


@pytest.mark.parametrize("arguments", [
    {"name": "tuoni_call"}, {"tool_name": "tuoni_call"},
    {"name": "tuoni_call", "tool_name": "tuoni_call"},
    {"tool_name": "tuoni_command_prepare"},
])
async def test_schema_alias_is_advertised_and_validated(arguments):
    catalog = _catalog()
    schema = catalog[1]["tuoni_tool_schema"].input_schema
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(arguments, schema)
    result, payload = await _invoke(catalog, "tuoni_tool_schema", arguments)
    assert not result.isError
    assert payload["data"]["name"] == arguments.get("name", arguments.get("tool_name"))


@pytest.mark.parametrize("arguments", [
    {}, {"tool_name": ""}, {"tool_name": None}, {"tool_name": 123},
    {"tool_name": "x" * 201}, {"name": "tuoni_call", "unexpected": True},
])
async def test_invalid_schema_arguments_are_rejected(arguments):
    catalog = _catalog()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(arguments, catalog[1]["tuoni_tool_schema"].input_schema)
    result, payload = await _invoke(catalog, "tuoni_tool_schema", arguments)
    assert result.isError
    assert payload["code"] == "INVALID_ARGUMENTS"


async def test_conflicting_names_fail_without_echoing_values_or_guessing():
    result, payload = await _invoke(_catalog(), "tuoni_tool_schema", {
        "name": "private-value-one", "tool_name": "private-value-two",
    })
    assert result.isError
    assert payload["code"] == "INVALID_ARGUMENTS"
    assert "values must agree" in payload["hint"]
    assert "private-value" not in result.content[0].text


async def test_alias_does_not_change_dispatch_invocation_validation():
    result, payload = await _invoke(_catalog(), "tuoni_call", {"tool_name": "tuoni_agents_list"})
    assert result.isError
    assert payload["code"] == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("prefix", ["", "mcp__tuoni__", "tuoni_"])
@pytest.mark.parametrize("entrypoint", ["tuoni_call", "tuoni_tool_schema"])
async def test_unknown_name_has_callable_read_only_discovery_recovery(read_only, prefix, entrypoint):
    catalog = _catalog(read_only=read_only, prefix=prefix)
    result, payload = await _invoke(catalog, entrypoint, {"name": "search"})
    assert result.isError
    assert payload["code"] == "TOOL_NOT_FOUND"
    assert call_result_size(result) <= ERROR_RESULT_BUDGET
    assert payload["next"] == {"name": prefix + "tuoni_find_tool", "arguments": {"query": "search"}}
    recovery = payload["next"]
    result, payload = await _invoke(catalog, recovery["name"].removeprefix(prefix), recovery["arguments"])
    assert not result.isError
    assert "tuoni_commands_search" in {item["name"] for item in payload["data"]["tools"]}
    assert all(item["effect"] == "read" for item in payload["data"]["tools"])


@pytest.mark.parametrize("read_only", [False, True])
async def test_routers_cannot_be_called_recursively_and_recover_with_schema(read_only):
    catalog = _catalog(read_only=read_only, prefix="client_")
    for name in catalog[1]:
        result, payload = await _invoke(catalog, "tuoni_call", {"name": name})
        assert result.isError
        assert payload["next"] == {"name": "client_tuoni_tool_schema", "arguments": {"name": name}}
        result, payload = await _invoke(catalog, "tuoni_tool_schema", payload["next"]["arguments"])
        assert not result.isError
        assert payload["data"]["name"] == name


@pytest.mark.parametrize(("read_only", "name"), [
    (True, "tuoni_operation_execute"), (True, "tuoni_command_prepare"),
    (False, "tuoni_listeners_list"), (True, "tuoni_listeners_list"),
])
async def test_schema_and_recovery_do_not_expose_excluded_operations(read_only, name):
    catalog = _catalog(read_only=read_only)
    result, payload = await _invoke(catalog, "tuoni_tool_schema", {"name": name})
    assert result.isError
    assert payload["code"] == "TOOL_NOT_FOUND"
    assert payload["next"]["name"] == "tuoni_find_tool"
    result, payload = await _invoke(catalog, "tuoni_find_tool", payload["next"]["arguments"])
    assert not result.isError
    assert name not in {item["name"] for item in payload["data"]["tools"]}
    assert all(item["effect"] == "read" for item in payload["data"]["tools"])


async def test_long_unicode_miss_keeps_a_complete_bounded_continuation():
    catalog = _catalog(prefix="mcp__tuoni__")
    result, payload = await _invoke(catalog, "tuoni_call", {"name": "探" * 200})
    assert result.isError
    assert call_result_size(result) <= ERROR_RESULT_BUDGET
    assert payload["next"]["name"] == "mcp__tuoni__tuoni_find_tool"
    result, _ = await _invoke(catalog, "tuoni_find_tool", payload["next"]["arguments"])
    assert not result.isError


@pytest.mark.parametrize("password", ["tuoni", "name", "arguments", "call"])
async def test_schema_recovery_preserves_known_router_names_when_credentials_overlap(password):
    catalog = _catalog(prefix="mcp__tuoni__", password=password)
    result, payload = await _invoke(catalog, "tuoni_call", {"name": "tuoni_call"})
    assert result.isError
    assert payload["next"] == {
        "name": "mcp__tuoni__tuoni_tool_schema", "arguments": {"name": "tuoni_call"},
    }
    result, payload = await _invoke(catalog, "tuoni_tool_schema", payload["next"]["arguments"])
    assert not result.isError
    assert payload["data"]["name"] == "tuoni_call"


@pytest.mark.parametrize(("model", "arguments", "expected"), [
    (AgentsListInput, {"search": "host"}, {"query": "host"}),
    (CommandDescribeInput, {"agent_guid": "agent-1", "command_id": "template-1"},
     {"agent_guid": "agent-1", "template": "template-1"}),
])
def test_read_aliases_are_advertised_and_canonicalized_without_mutating_input(model, arguments, expected):
    original = dict(arguments)
    jsonschema.validate(arguments, model.model_json_schema())
    result = model.model_validate(arguments).model_dump(exclude_unset=True)
    assert result == expected
    assert arguments == original


@pytest.mark.parametrize(("model", "arguments"), [
    (AgentsListInput, {"query": "one", "search": "two"}),
    (CommandDescribeInput, {"agent_guid": "a", "template": "one", "command_id": "two"}),
    (CommandPrepareInput, {"agent_guid": "a", "command_id": "cmd", "shell_command": "whoami"}),
])
def test_conflicting_read_aliases_and_aliased_mutations_are_rejected(model, arguments):
    with pytest.raises(ValidationError):
        model.model_validate(arguments)


@pytest.mark.parametrize("prefix", ["", "tuoni_", "mcp__tuoni__"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_dispatch_accepts_one_exact_read_envelope_without_recursive_invocation(prefix, wrapped):
    catalog = _catalog(prefix=prefix)
    seen = []

    async def read(_, model):
        seen.append(model.model_dump())
        return {"value": "read"}

    probe = ToolSpec("tuoni_read_probe", "Read", ToolInput, read, frozenset({Profile.AGENT_OPS}))
    catalog[0].add(probe)
    adapters = {s.name: s for s in build_dispatch_specs(catalog[0], [probe], read_only=False)}
    catalog = catalog[0], adapters, catalog[2]
    args = {"name": prefix + probe.name, "arguments": {"name": probe.name}}
    if wrapped:
        args = {"name": prefix + "tuoni_call", "arguments": args}
    result, payload = await _invoke(catalog, "tuoni_call", args)
    assert not result.isError
    assert payload["data"] == {"value": "read"}
    assert seen == [{}]


@pytest.mark.parametrize("effect", [Effect.PREPARE, Effect.MUTATE, Effect.EXECUTE])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_dispatch_compatibility_never_normalizes_mutations(effect, wrapped):
    catalog = _catalog(prefix="client_")

    async def forbidden(*_):
        pytest.fail("Compatibility handling invoked a state-changing target")

    spec = ToolSpec("tuoni_write_probe", "Write", ToolInput, forbidden,
                    frozenset({Profile.AGENT_OPS}), effect=effect)
    catalog[0].add(spec)
    adapters = {s.name: s for s in build_dispatch_specs(catalog[0], [spec], read_only=False)}
    catalog = catalog[0], adapters, catalog[2]
    args = {"name": "client_" + spec.name, "arguments": {}}
    if wrapped:
        args = {"name": "client_tuoni_call", "arguments": args}
    result, payload = await _invoke(catalog, "tuoni_call", args)
    assert result.isError and payload["code"] == "TOOL_NOT_FOUND"


async def test_repeated_wrappers_and_conflicting_routing_metadata_still_fail():
    catalog = _catalog(prefix="client_")
    for arguments in [
        {"name": "client_tuoni_call", "arguments": {"name": "client_tuoni_call", "arguments": {}}},
        {"name": "tuoni_capabilities", "arguments": {"name": ["untrusted"]}},
    ]:
        result, payload = await _invoke(catalog, "tuoni_call", arguments)
        assert result.isError
        assert payload["code"] in {"TOOL_NOT_FOUND", "INVALID_ARGUMENTS"}


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("prefix", ["", "tuoni_", "mcp__tuoni__"])
async def test_missing_command_search_scope_returns_exact_agent_selection_without_backend_access(read_only, prefix):
    catalog = _catalog(read_only=read_only, prefix=prefix)
    result, payload = await _invoke(catalog, "tuoni_call", {
        "name": "tuoni_commands_search", "arguments": {"query": "shell command"},
    })
    assert not result.isError
    assert payload["data"]["agent_required"] is True
    assert payload["data"]["query"] == "shell command"
    assert payload["next"] == {
        "name": prefix + "tuoni_call",
        "arguments": {"name": "tuoni_agents_list", "arguments": {}},
    }


@pytest.mark.parametrize("prefix", ["", "tuoni_", "mcp__tuoni__"])
async def test_read_metadata_tools_accept_one_copied_continuation(prefix):
    catalog = _catalog(prefix=prefix)
    result, payload = await _invoke(catalog, "tuoni_find_tool", {
        "name": prefix + "tuoni_find_tool", "arguments": {"query": "command result", "limit": 50},
    })
    assert not result.isError
    assert "tuoni_command_get" in {s["name"] for s in payload["data"]["tools"]}
    result, payload = await _invoke(catalog, "tuoni_tool_schema", {
        "name": prefix + "tuoni_tool_schema", "arguments": {"name": "tuoni_command_get"},
    })
    assert not result.isError
    assert payload["data"]["name"] == "tuoni_command_get"


@pytest.mark.parametrize("effect", [Effect.PREPARE, Effect.MUTATE, Effect.EXECUTE])
async def test_transport_does_not_unwrap_state_changing_calls(effect):
    catalog = _catalog(prefix="client_")

    async def forbidden(*_):
        pytest.fail("A state-changing envelope reached a handler")

    spec = ToolSpec("tuoni_write", "Write", ToolInput, forbidden,
                    frozenset({Profile.AGENT_OPS}), effect=effect)
    result = await catalog[0].invoke_spec(spec, {
        "name": "client_tuoni_write", "arguments": {},
    }, catalog[2])
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "INVALID_ARGUMENTS"
