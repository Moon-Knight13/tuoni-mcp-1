from __future__ import annotations

import json
import time
from unittest.mock import MagicMock
from dataclasses import replace

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools.commands import _describe_items


def _cfg() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )


def _agent(template_ids: list[str]) -> dict:
    return {
        "guid": "agent-1",
        "active": True,
        "status": "ACTIVE",
        "availableCommandTemplates": template_ids,
    }


def _template(index: int, *, field_count: int = 1) -> dict:
    properties = {
        f"field_{field}": {
            "type": "string",
            "description": (f"Description {field} " + "x" * 600),
            "maxLength": 1000,
        }
        for field in range(field_count)
    }
    return {
        "id": f"template-{index}",
        "name": "cmd" if index == 0 else f"command-{index:03d}",
        "pluginId": "example.commands",
        "scope": "global",
        "qualifiedName": f"example.commands:global:command-{index:03d}",
        "fullyQualifiedName": f"example.commands:global:command-{index:03d}",
        "description": "Run a bounded test command " + "d" * 500,
        "supportedExecUnitTypes": ["SHELLCODE_NATIVE", "DOTNET_EXE"],
        "configurationSchema": {
            "type": "object",
            "properties": properties,
            "required": ["field_0"] if properties else [],
            "additionalProperties": False,
        },
        "updateSchema": {
            "type": "object",
            "properties": {
                "stdin": {"type": "array", "items": {"type": "string"}}
            },
        },
        "defaultConfiguration": None,
        "exampleConfigurations": [
            {
                "name": "secret example",
                "configuration": {
                    "field_0": "example",
                    "password": "must-not-leak",
                    "@files.input": ["body-must-not-leak"],
                },
            }
        ],
    }


def _invocation(fake_c2, templates: list[dict]):
    cfg = _cfg()
    fake_c2.responses["/api/v1/agents/agent-1"] = _agent(
        [item["id"] for item in templates]
    )
    fake_c2.responses["/api/v1/command-templates"] = templates
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = MagicMock()
    context.request_context.lifespan_context = server
    return build_registry(cfg), InvocationContext(context=context, server=server)


async def _call(registry, invocation, name: str, arguments: dict):
    spec = registry.get(name)
    assert spec is not None
    return await registry.invoke_spec(spec, arguments, invocation)


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["mcp__tuoni__", "tuoni_"])
async def test_describe_cursor_error_restarts_exact_template_read(fake_c2, mode, prefix):
    _, invocation = _invocation(fake_c2, [_template(0, field_count=20)])
    cfg = replace(invocation.server.cfg, mode=mode, tool_prefix=prefix)
    invocation.server.cfg = cfg
    registry = build_registry(cfg)
    selected = registry.select(cfg.profiles, read_only=False)
    adapters = build_dispatch_specs(registry, selected, read_only=False) if mode is MCPMode.DISPATCH else selected
    specs = {spec.name: spec for spec in adapters}
    arguments = {"agent_guid": "agent-1", "template": "cmd", "limit": 1}
    target = {"name": "tuoni_command_describe", "arguments": arguments}
    bad = {**target, "arguments": {**arguments, "cursor": "c_" + "0" * 24}}
    if mode is MCPMode.DISPATCH:
        target = {"name": "tuoni_call", "arguments": target}
        bad = {"name": "tuoni_call", "arguments": bad}
    result = await registry.invoke_spec(specs[bad["name"]], bad["arguments"], invocation)
    payload = _payload(result)
    assert payload["code"] == "INVALID_CURSOR"
    assert payload["next"] == {**target, "name": prefix + target["name"]}
    restarted = await registry.invoke_spec(specs[target["name"]], payload["next"]["arguments"], invocation)
    assert not restarted.isError
    assert _payload(restarted)["data"]["template"]["name"] == "cmd"
    assert len(_payload(restarted)["next_cursor"]) == 26


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "client_"])
async def test_search_provides_an_exact_callable_description_without_guessing(fake_c2, mode, prefix):
    registry, invocation = _invocation(fake_c2, [_template(0)])
    invocation.server.cfg = replace(invocation.server.cfg, mode=mode, tool_prefix=prefix)
    selected = registry.select(invocation.server.cfg.profiles, read_only=True)
    specs = {s.name: s for s in (selected if mode is MCPMode.NATIVE else
                              build_dispatch_specs(registry, selected, read_only=True))}
    name, arguments = "tuoni_commands_search", {"agent_guid": "agent-1", "query": "cmd"}
    if mode is MCPMode.DISPATCH:
        name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
    result = await registry.invoke_spec(specs[name], arguments, invocation)
    assert not result.isError
    nxt = _payload(result)["data"]["commands"][0]["describe"]
    name = nxt["name"].removeprefix(prefix)
    described = await registry.invoke_spec(specs[name], nxt["arguments"], invocation)
    assert not described.isError
    assert _payload(described)["data"]["template"]["preferred_reference"] == "cmd"
    assert all(call[0] == "request_get" for call in fake_c2.calls)


async def _describe_all(registry, invocation, template: str, *, limit: int = 5):
    arguments = {
        "agent_guid": "agent-1",
        "template": template,
        "limit": limit,
    }
    fields: list[dict] = []
    serialized_pages: list[str] = []
    for _ in range(200):
        result = await _call(
            registry, invocation, "tuoni_command_describe", arguments
        )
        assert result.isError is False
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET
        payload = _payload(result)
        fields.extend(payload["data"]["fields"])
        serialized_pages.append(
            result.model_dump_json(by_alias=True, exclude_none=True)
        )
        if payload["next"] is None:
            return fields, "".join(serialized_pages)
        arguments = payload["next"]["arguments"]
    raise AssertionError("describe pagination did not terminate")


async def test_search_pages_compact_identities_without_bulk_schemas_or_n_plus_one(
    fake_c2,
) -> None:
    templates = [_template(index) for index in range(70)]
    registry, invocation = _invocation(fake_c2, templates)
    arguments = {"agent_guid": "agent-1", "limit": 8}
    seen: list[str] = []
    page_count = 0

    while True:
        result = await _call(
            registry, invocation, "tuoni_commands_search", arguments
        )
        payload = _payload(result)
        serialized = result.model_dump_json(by_alias=True, exclude_none=True)
        assert result.isError is False
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        assert "configurationSchema" not in serialized
        assert "configuration_schema" not in serialized
        assert "exampleConfigurations" not in serialized
        assert "field_0" not in serialized
        seen.extend(item["id"] for item in payload["data"]["commands"])
        page_count += 1
        if payload["next"] is None:
            break
        assert payload["next"]["name"] == "tuoni_commands_search"
        assert payload["next"]["arguments"]["cursor"] == payload["next_cursor"]
        arguments = payload["next"]["arguments"]

    assert page_count > 1
    assert len(seen) == len(set(seen)) == 70
    gets = [call[1][0] for call in fake_c2.calls if call[0] == "request_get"]
    assert gets.count("/api/v1/agents/agent-1") == 1
    assert gets.count("/api/v1/command-templates") == 1
    assert not any(
        path == "/api/v1/agents/agent-1/command-templates" for path in gets
    )


async def test_search_cursor_rejects_changed_query_or_limit(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, [_template(index) for index in range(20)])
    first = await _call(
        registry,
        invocation,
        "tuoni_commands_search",
        {"agent_guid": "agent-1", "query": "command", "limit": 2},
    )
    cursor = _payload(first)["next_cursor"]

    changed = await _call(
        registry,
        invocation,
        "tuoni_commands_search",
        {
            "agent_guid": "agent-1",
            "query": "different",
            "limit": 3,
            "cursor": cursor,
        },
    )
    assert changed.isError is True
    assert _payload(changed)["code"] == "CURSOR_SCOPE_CHANGED"


async def test_describe_structurally_pages_normalized_fields_and_redacts_examples(
    fake_c2,
) -> None:
    template = _template(7, field_count=45)
    registry, invocation = _invocation(fake_c2, [template])
    arguments = {
        "agent_guid": "agent-1",
        "template": "template-7",
        "limit": 7,
    }
    field_names: list[str] = []
    page_count = 0
    all_serialized = ""

    while True:
        result = await _call(
            registry, invocation, "tuoni_command_describe", arguments
        )
        payload = _payload(result)
        serialized = result.model_dump_json(by_alias=True, exclude_none=True)
        all_serialized += serialized
        assert result.isError is False
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET
        assert "configurationSchema" not in serialized
        assert "updateSchema" not in serialized
        fields = payload["data"]["fields"]
        field_names.extend(
            item["name"]
            for item in fields
            if item["kind"] in {"configuration_field", "update_field"}
        )
        page_count += 1
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]

    assert page_count > 1
    assert {f"field_{index}" for index in range(45)} <= set(field_names)
    assert "stdin" in field_names
    assert "must-not-leak" not in all_serialized
    assert "body-must-not-leak" not in all_serialized
    assert "[REDACTED]" in all_serialized


async def test_describe_prepare_example_prefers_short_unambiguous_template_name(
    fake_c2,
) -> None:
    template = _template(0)
    template["configurationSchema"] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "stdin": {"type": "array", "items": {"type": "string"}},
        },
    }
    registry, invocation = _invocation(fake_c2, [template])
    result = await _call(
        registry,
        invocation,
        "tuoni_command_describe",
        {"agent_guid": "agent-1", "template": "cmd"},
    )
    payload = _payload(result)

    assert result.isError is False
    assert payload["data"]["prepare_arguments_example"] == {
        "agent_guid": "agent-1",
        "template": "cmd",
        "shell_command": "COMMAND_TEXT_GOES_HERE",
    }


async def test_describe_uses_complete_id_when_short_name_is_ambiguous(fake_c2) -> None:
    first = _template(0, field_count=12)
    first["id"] = "11111111-1111-1111-1111-111111111111"
    second = _template(1)
    second["id"] = "22222222-2222-2222-2222-222222222222"
    second["name"] = first["name"]
    registry, invocation = _invocation(fake_c2, [first, second])

    result = await _call(
        registry,
        invocation,
        "tuoni_command_describe",
        {"agent_guid": "agent-1", "template": first["id"], "limit": 2},
    )
    payload = _payload(result)

    assert result.isError is False
    assert payload["data"]["template"]["preferred_reference"] == first["id"]
    assert payload["data"]["prepare_arguments_example"]["template"] == first["id"]
    assert payload["next"]["arguments"]["template"] == first["id"]


async def test_truncated_template_uuid_error_points_to_short_name_and_stops_retry(
    fake_c2,
) -> None:
    template = _template(0)
    template["id"] = "12345678-1234-1234-1234-123456789abc"
    registry, invocation = _invocation(fake_c2, [template])

    result = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            # Mirrors the observed small-model corruption: one UUID segment was
            # dropped from an otherwise copied template identity.
            "template": "12345678-1234-1234-123456789abc",
            "configuration": {"field_0": "whoami"},
        },
    )
    error = _payload(result)
    compact_hint = "".join(error["hint"].split())

    assert result.isError is True
    assert error["code"] == "COMMAND_TEMPLATE_NOT_FOUND"
    assert error["retryable"] is False
    assert '"template":"cmd"' in compact_hint
    assert "do not repeat" in error["hint"].casefold()
    assert not any(name.startswith("request_post") for name, _, _ in fake_c2.calls)


async def test_describe_distinguishes_modes_from_supported_exec_unit_types(
    fake_c2,
) -> None:
    template = _template(0)
    registry, invocation = _invocation(fake_c2, [template])

    result = await _call(
        registry,
        invocation,
        "tuoni_command_describe",
        {"agent_guid": "agent-1", "template": "cmd"},
    )
    described = _payload(result)["data"]["template"]

    assert described["execution_modes"] == ["SELF", "NEW", "EXISTING"]
    assert described["supported_exec_unit_types"] == [
        "SHELLCODE_NATIVE",
        "DOTNET_EXE",
    ]
    assert "supported_execution_types" not in described


async def test_describe_emits_nested_required_object_and_array_item_paths(
    fake_c2,
) -> None:
    template = _template(12)
    template["configurationSchema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["pipeline"],
        "properties": {
            "pipeline": {
                "type": "object",
                "additionalProperties": False,
                "required": ["jobs"],
                "properties": {
                    "jobs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "required": ["host"],
                            "properties": {
                                "host": {
                                    "type": "string",
                                    "minLength": 1,
                                    "pattern": r"^[a-z0-9.-]+$",
                                },
                                "ports": {
                                    "type": "array",
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 65535,
                                    },
                                },
                            },
                        },
                    }
                },
            }
        },
    }
    template["updateSchema"] = None
    registry, invocation = _invocation(fake_c2, [template])

    fields, _ = await _describe_all(registry, invocation, "template-12", limit=3)

    root = next(item for item in fields if item["kind"] == "configuration_schema")
    assert root["path"] == ""
    assert root["additional_properties"] is False
    pipeline = next(item for item in fields if item.get("path") == "/pipeline")
    jobs = next(item for item in fields if item.get("path") == "/pipeline/jobs")
    job_item = next(
        item
        for item in fields
        if item["kind"] == "configuration_item"
        and item.get("path") == "/pipeline/jobs/*"
    )
    host = next(
        item for item in fields if item.get("path") == "/pipeline/jobs/*/host"
    )
    port_item = next(
        item
        for item in fields
        if item["kind"] == "configuration_item"
        and item.get("path") == "/pipeline/jobs/*/ports/*"
    )

    assert pipeline["required"] is True
    assert pipeline["type"] == "object"
    assert jobs["required"] is True
    assert jobs["type"] == "array"
    assert jobs["minItems"] == 1
    assert jobs["maxItems"] == 8
    assert job_item["type"] == "object"
    assert job_item["required_properties"] == ["host"]
    assert host["required"] is True
    assert host["pattern"] == r"^[a-z0-9.-]+$"
    assert port_item["type"] == "integer"
    assert port_item["minimum"] == 1
    assert port_item["maximum"] == 65535


async def test_describe_resolves_local_refs_and_pages_composition_branches_safely(
    fake_c2,
) -> None:
    template = _template(13)
    template["configurationSchema"] = {
        "$defs": {
            "credential": {
                "type": "object",
                "required": ["username", "password"],
                "properties": {
                    "username": {"type": "string", "minLength": 1},
                    "password": {
                        "type": "string",
                        "default": "schema-secret-must-not-leak",
                    },
                },
            },
            "node": {
                "type": "object",
                "properties": {
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/node"},
                    }
                },
            },
            "unused": {
                "description": "UNREFERENCED_BULK_SCHEMA_MUST_NOT_LEAK",
                "enum": ["x" * 10_000],
            },
        },
        "type": "object",
        "required": ["auth"],
        "properties": {
            "auth": {
                "oneOf": [
                    {"$ref": "#/$defs/credential"},
                    {
                        "type": "object",
                        "required": ["token"],
                        "properties": {"token": {"type": "string"}},
                    },
                ]
            },
            "node": {"$ref": "#/$defs/node"},
        },
    }
    template["updateSchema"] = None
    registry, invocation = _invocation(fake_c2, [template])

    fields, serialized = await _describe_all(
        registry, invocation, "template-13", limit=2
    )

    auth = next(item for item in fields if item.get("path") == "/auth")
    assert auth["required"] is True
    assert auth["type"] == "composed"
    assert auth["one_of_count"] == 2
    assert any(
        item.get("relation") == "oneOf"
        and item.get("branch_index") == 0
        and item.get("path") == "/auth"
        for item in fields
    )
    username = next(
        item for item in fields if item.get("path") == "/auth/username"
    )
    token = next(item for item in fields if item.get("path") == "/auth/token")
    assert "oneOf[0]" in username["schema_context"]
    assert "$ref:#/$defs/credential" in username["schema_context"]
    assert "oneOf[1]" in token["schema_context"]
    assert any(
        item.get("relation") == "$ref"
        and item.get("ref") == "#/$defs/credential"
        for item in fields
    )
    assert any(item.get("reason") == "ref_cycle" for item in fields)
    assert "schema-secret-must-not-leak" not in serialized
    assert "UNREFERENCED_BULK_SCHEMA_MUST_NOT_LEAK" not in serialized
    assert "[REDACTED]" in serialized


async def test_describe_bounds_oversized_schema_items_and_reports_depth_limit(
    fake_c2,
) -> None:
    nested: dict = {"type": "string"}
    for index in reversed(range(15)):
        nested = {
            "type": "object",
            "required": [f"level_{index}"],
            "properties": {f"level_{index}": nested},
        }
    nested["description"] = "🙂\\\"" * 10_000
    nested["enum"] = [("🙂\\\"" * 1_000) for _ in range(100)]
    nested["default"] = {
        f"default_{index}": {f"nested_{child}": "x" * 1_000 for child in range(20)}
        for index in range(20)
    }
    template = _template(14)
    template["configurationSchema"] = nested
    template["updateSchema"] = None
    registry, invocation = _invocation(fake_c2, [template])

    fields, _ = await _describe_all(registry, invocation, "template-14", limit=20)

    assert any(item.get("reason") == "max_depth" for item in fields)
    assert all(len(json.dumps(item, ensure_ascii=False).encode()) < 4096 for item in fields)


def test_describe_normalization_has_an_aggregate_item_limit() -> None:
    template = _template(15)
    template["configurationSchema"] = {
        "type": "object",
        "allOf": [
            {
                "type": "object",
                "properties": {
                    f"branch_{branch}_field_{index}": {"type": "string"}
                    for index in range(256)
                },
            }
            for branch in range(6)
        ],
    }
    template["updateSchema"] = None

    items = _describe_items(template)

    assert len(items) <= 1032
    assert any(item.get("reason") == "item_limit" for item in items)


async def test_agent_get_bounds_metadata_and_never_dumps_template_catalog(fake_c2) -> None:
    templates = [_template(index) for index in range(70)]
    registry, invocation = _invocation(fake_c2, templates)
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        **_agent([item["id"] for item in templates]),
        "firstRegistrationTime": "2026-07-20T00:00:00Z",
        "lastCallbackTime": "2026-07-20T00:00:01Z",
        "recentListeners": list(range(100)),
        "metadata": {
            "hostname": "workstation-1",
            "accessToken": "agent-metadata-secret",
            **{f"field-{index}": "🙂" * 1_000 for index in range(100)},
        },
    }

    result = await _call(
        registry,
        invocation,
        "tuoni_agent_get",
        {"agent_guid": "agent-1"},
    )
    payload = _payload(result)
    serialized = result.model_dump_json(by_alias=True, exclude_none=True)

    assert result.isError is False
    assert payload["data"]["capability_count"] == 70
    assert payload["data"]["capabilities_note"].startswith("Use command search")
    assert "template-0" not in serialized
    assert "configurationSchema" not in serialized
    assert "agent-metadata-secret" not in serialized
    assert call_result_size(result) <= DETAIL_RESULT_BUDGET
