from __future__ import annotations

import json
import time
from types import ModuleType
from unittest.mock import MagicMock
from pydantic import Field
from jsonschema import Draft202012Validator

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ListToolsResult

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import (
    Effect,
    InvocationContext,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    ToolSuccess,
    build_dispatch_specs,
    collect_decorated_module,
)
from tuoni_mcp.server import RegistryFastMCP, SERVER_INSTRUCTIONS, build_server


DEFAULT_NAMES = (
    "tuoni_agents_list",
    "tuoni_agent_get",
    "tuoni_commands_search",
    "tuoni_command_describe",
    "tuoni_command_prepare",
    "tuoni_command_update_prepare",
    "tuoni_command_stop_prepare",
    "tuoni_operation_execute",
    "tuoni_command_get",
)


class EmptyInput(ToolInput):
    pass


class PageInput(ToolInput):
    cursor: str | None = None


def test_compact_schema_is_generated_once_and_preserves_literal_data(monkeypatch):
    literal = {"title": "data", "default": None, "reference": "#/$defs/NewExecutionInput"}

    class Input(ToolInput):
        data: dict = Field(default=literal)

    generated = 0
    original = Input.model_json_schema

    def schema(*args, **kwargs):
        nonlocal generated
        generated += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(Input, "model_json_schema", schema)
    spec = ToolSpec(name="fixture", description="fixture", input_model=Input, handler=_ok,
                    profiles=frozenset({Profile.AGENT_OPS}))
    first = spec.input_schema
    assert first["properties"]["data"]["default"] == literal
    first["properties"]["data"]["default"]["title"] = "changed"
    assert spec.input_schema["properties"]["data"]["default"] == literal
    assert generated == 1
    Draft202012Validator.check_schema(spec.input_schema)


async def _ok(_: InvocationContext, __: EmptyInput):
    return {"value": "ok"}


async def _secret_failure(_: InvocationContext, __: EmptyInput):
    raise RuntimeError(
        "password=hunter2 https://operator:supersecret@example.test/?token=abc"
    )


async def _paged(_: InvocationContext, arguments: PageInput):
    if arguments.cursor is None:
        return ToolSuccess(
            {"items": [1]},
            next_cursor="page-2",
            next_call={
                "name": "tuoni_fixture_list",
                "arguments": {"cursor": "page-2"},
            },
        )
    return ToolSuccess({"items": [2]})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    effects = (
        Effect.READ,
        Effect.READ,
        Effect.READ,
        Effect.READ,
        Effect.PREPARE,
        Effect.PREPARE,
        Effect.PREPARE,
        Effect.EXECUTE,
        Effect.READ,
    )
    for name, effect in zip(DEFAULT_NAMES, effects, strict=True):
        registry.add(
            ToolSpec(
                name=name,
                description=f"Compact contract for {name}.",
                input_model=EmptyInput,
                handler=_ok,
                profiles=frozenset({Profile.AGENT_OPS}),
                effect=effect,
            )
        )
    return registry


def _cfg(*, read_only: bool = False, mode: MCPMode = MCPMode.NATIVE) -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        read_only=read_only,
        profiles=frozenset({Profile.AGENT_OPS}),
        mode=mode,
    )


async def test_exact_native_and_read_only_catalogs_and_byte_ceiling() -> None:
    native = build_server(_cfg(), registry=_registry())
    native_tools = await native.list_tools()
    assert tuple(tool.name for tool in native_tools) == DEFAULT_NAMES
    serialized = ListToolsResult(tools=native_tools).model_dump_json(
        by_alias=True, exclude_none=True
    )
    assert len(serialized.encode("utf-8")) <= 10 * 1024
    assert all(tool.outputSchema is None for tool in native_tools)

    read_only = build_server(_cfg(read_only=True), registry=_registry())
    assert tuple(tool.name for tool in await read_only.list_tools()) == (
        "tuoni_agents_list",
        "tuoni_agent_get",
        "tuoni_commands_search",
        "tuoni_command_describe",
        "tuoni_command_get",
    )


async def test_dispatch_catalog_is_exact_and_read_only_drops_execute() -> None:
    dispatch = build_server(
        _cfg(mode=MCPMode.DISPATCH),
        registry=_registry(),
    )
    assert tuple(tool.name for tool in await dispatch.list_tools()) == (
        "tuoni_find_tool",
        "tuoni_tool_schema",
        "tuoni_call",
        "tuoni_operation_execute",
    )

    dispatch_ro = build_server(
        _cfg(read_only=True, mode=MCPMode.DISPATCH),
        registry=_registry(),
    )
    assert tuple(tool.name for tool in await dispatch_ro.list_tools()) == (
        "tuoni_find_tool",
        "tuoni_tool_schema",
        "tuoni_call",
    )


async def test_dispatch_wraps_global_executor_without_agent_ops_profile() -> None:
    registry = _registry()
    registry.add(
        ToolSpec(
            name="tuoni_listeners_list",
            description="List listeners.",
            input_model=EmptyInput,
            handler=_ok,
            profiles=frozenset({Profile.ACCESS}),
        )
    )
    cfg = Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.ACCESS}),
        mode=MCPMode.DISPATCH,
    )
    dispatch = build_server(cfg, registry=registry)
    assert tuple(tool.name for tool in await dispatch.list_tools()) == (
        "tuoni_find_tool",
        "tuoni_tool_schema",
        "tuoni_call",
        "tuoni_operation_execute",
    )
    native_execute = registry.get("tuoni_operation_execute")
    dispatch_execute = dispatch.exposure[-1]
    assert native_execute is not None
    assert dispatch_execute.handler is not native_execute.handler
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    context = MagicMock()
    context.request_context.lifespan_context = server_context
    result = await registry.invoke_spec(
        dispatch_execute,
        {},
        InvocationContext(context=context, server=server_context),
    )
    assert result.isError is False
    assert json.loads(result.content[0].text)["data"] == {"value": "ok"}  # type: ignore[union-attr]


async def test_registry_success_error_and_redaction_contracts() -> None:
    registry = _registry()
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    result = await registry.invoke_spec(
        registry.get("tuoni_agents_list"),  # type: ignore[arg-type]
        {},
        invocation,
    )
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert result.isError is False
    assert payload == {
        "ok": True,
        "data": {"value": "ok"},
        "next_cursor": None,
        "next": None,
    }

    invalid = await registry.invoke_spec(
        registry.get("tuoni_agents_list"),  # type: ignore[arg-type]
        {"password": "must-not-leak"},
        invocation,
    )
    error = json.loads(invalid.content[0].text)  # type: ignore[union-attr]
    assert invalid.isError is True
    assert set(error) == {"code", "message", "retryable", "hint"}
    assert "must-not-leak" not in invalid.content[0].text  # type: ignore[union-attr]
    assert call_result_size(invalid) <= 1024


def test_annotations_and_instructions_are_small_and_explicit() -> None:
    registry = _registry()
    read = registry.get("tuoni_agent_get")
    prepare = registry.get("tuoni_command_prepare")
    execute = registry.get("tuoni_operation_execute")
    assert read and read.tool_annotations.readOnlyHint is True
    assert prepare and prepare.tool_annotations.destructiveHint is False
    assert execute and execute.tool_annotations.destructiveHint is True
    assert execute.tool_annotations.idempotentHint is True
    assert "Additive tools marked destructive change remote state immediately" in SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS.encode("utf-8")) < 1024


async def test_public_registry_protocol_boundary_returns_json_tool_errors() -> None:
    registry = _registry()
    exposure = registry.select({Profile.AGENT_OPS}, read_only=False)
    server = RegistryFastMCP("test", registry=registry, exposure=exposure)
    async with create_connected_server_and_client_session(
        server.protocol_server
    ) as session:
        response = await session.call_tool(
            "tuoni_agents_list", {"unexpected": "not echoed"}
        )

    assert response.isError is True
    payload = json.loads(response.content[0].text)  # type: ignore[union-attr]
    assert payload["code"] == "INVALID_ARGUMENTS"
    assert "not echoed" not in response.content[0].text  # type: ignore[union-attr]


async def test_unknown_exception_details_cannot_leak_secrets() -> None:
    registry = ToolRegistry()
    spec = registry.add(
        ToolSpec(
            name="secret_failure",
            description="Test failure boundary.",
            input_model=EmptyInput,
            handler=_secret_failure,
            profiles=frozenset({Profile.AGENT_OPS}),
        )
    )
    result = await registry.invoke_spec(
        spec,
        {},
        InvocationContext(context=MagicMock(), server=MagicMock()),
    )
    text = result.content[0].text  # type: ignore[union-attr]
    assert result.isError is True
    assert "hunter2" not in text
    assert "supersecret" not in text
    assert "token=abc" not in text


async def test_profile_lists_are_structurally_paginated_and_bounded() -> None:
    module = ModuleType("bounded_list_fixture")

    def register(mcp, cfg) -> None:
        del cfg

        @mcp.tool(name="tuoni_fixture_list", description="List fixture records.")
        async def fixture_list(prefix: str = "item"):
            return {
                "records": [
                    {"id": index, "name": f"{prefix}-{index}", "note": "🙂" * 400}
                    for index in range(80)
                ]
            }

    module.register = register  # type: ignore[attr-defined]
    registry = ToolRegistry()
    collect_decorated_module(
        registry,
        module,
        _cfg(),
        profiles={Profile.ARTIFACTS},
    )
    spec = registry.get("tuoni_fixture_list")
    assert spec is not None
    assert {"cursor", "limit"} <= spec.input_schema["properties"].keys()
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    arguments = {"prefix": "entry", "limit": 50}
    ids: list[int] = []

    while True:
        result = await registry.invoke_spec(spec, arguments, invocation)
        assert result.isError is False
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        ids.extend(item["id"] for item in payload["data"]["records"])
        if payload["next"] is None:
            break
        assert payload["next"]["name"] == "tuoni_fixture_list"
        assert payload["next"]["arguments"]["cursor"] == payload["next_cursor"]
        arguments = payload["next"]["arguments"]

    assert ids == list(range(80))


async def test_dispatch_wraps_target_continuations_but_native_keeps_direct_call() -> None:
    registry = ToolRegistry()
    spec = registry.add(
        ToolSpec(
            name="tuoni_fixture_list",
            description="List a paginated fixture.",
            input_model=PageInput,
            handler=_paged,
            profiles=frozenset({Profile.ARTIFACTS}),
            effect=Effect.READ,
            budget=LIST_RESULT_BUDGET,
        )
    )
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())

    native = await registry.invoke_spec(spec, {}, invocation)
    native_payload = json.loads(native.content[0].text)  # type: ignore[union-attr]
    assert native_payload["next"] == {
        "name": "tuoni_fixture_list",
        "arguments": {"cursor": "page-2"},
    }

    dispatch = {
        item.name: item
        for item in build_dispatch_specs(registry, (spec,), read_only=True)
    }
    first = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {"name": "tuoni_fixture_list", "arguments": {}},
        invocation,
    )
    first_payload = json.loads(first.content[0].text)  # type: ignore[union-attr]
    assert first.isError is False
    assert call_result_size(first) <= LIST_RESULT_BUDGET
    assert first_payload["next"] == {
        "name": "tuoni_call",
        "arguments": {
            "name": "tuoni_fixture_list",
            "arguments": {"cursor": "page-2"},
        },
    }

    second = await registry.invoke_spec(
        dispatch[first_payload["next"]["name"]],
        first_payload["next"]["arguments"],
        invocation,
    )
    second_payload = json.loads(second.content[0].text)  # type: ignore[union-attr]
    assert second.isError is False
    assert second_payload["data"] == {"items": [2]}
    assert second_payload["next"] is None


async def test_bulk_settings_are_flattened_into_bounded_pages() -> None:
    module = ModuleType("bounded_settings_fixture")

    def register(mcp, cfg) -> None:
        del cfg

        @mcp.tool(name="tuoni_settings_get_all", description="List all settings.")
        async def settings_get_all():
            return {
                "server": {f"server.key.{index}": "🙂" * 500 for index in range(50)},
                "plugin": {f"plugin.key.{index}": index for index in range(50)},
            }

    module.register = register  # type: ignore[attr-defined]
    registry = ToolRegistry()
    collect_decorated_module(
        registry,
        module,
        _cfg(),
        profiles={Profile.ADMINISTRATION},
    )
    spec = registry.get("tuoni_settings_get_all")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())
    arguments = {"limit": 50}
    keys: list[str] = []

    while True:
        result = await registry.invoke_spec(spec, arguments, invocation)
        assert result.isError is False
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        keys.extend(item["key"] for item in payload["data"]["settings"])
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]

    assert keys == [f"server.key.{index}" for index in range(50)] + [
        f"plugin.key.{index}" for index in range(50)
    ]


async def test_adapter_mutation_calls_are_preflighted_once_without_auth_replay(
    monkeypatch,
) -> None:
    from tuoni_mcp import registry as registry_module
    from tuoni_mcp.tools import _helpers

    calls = {"preflight": 0, "retry": 0, "once": 0}

    async def preflight(_server):
        calls["preflight"] += 1

    async def with_retry(_server, _fn, *_args, **_kwargs):
        calls["retry"] += 1
        return "retry"

    async def without_retry(_server, _fn, *_args, **_kwargs):
        calls["once"] += 1
        return "once"

    monkeypatch.setattr(registry_module, "ensure_fresh_token", preflight)
    monkeypatch.setattr(_helpers, "call_with_relogin", with_retry)
    monkeypatch.setattr(_helpers, "call_without_retry", without_retry)

    cfg = _cfg()
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    server_context.last_login = time.monotonic()
    context = MagicMock()
    context.request_context.lifespan_context = server_context
    invocation = InvocationContext(context=context, server=server_context)

    async def mutation(inv: InvocationContext, _: EmptyInput):
        return {"path": await _helpers.call(inv.context, lambda: None)}

    registry = ToolRegistry()
    spec = registry.add(
        ToolSpec(
            name="adapter_mutation",
            description="Test once-only boundary.",
            input_model=EmptyInput,
            handler=mutation,
            profiles=frozenset({Profile.ADMINISTRATION}),
            effect=Effect.MUTATE,
        )
    )
    result = await registry.invoke_spec(spec, {}, invocation)
    assert result.isError is False
    assert calls == {"preflight": 1, "retry": 0, "once": 1}

    # The ContextVar must be reset after the handler so subsequent reads retain
    # their safe authentication-refresh behavior.
    await _helpers.call(context, lambda: None)
    assert calls == {"preflight": 1, "retry": 1, "once": 1}
