from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, ToolRegistry, collect_decorated_module
from tuoni_mcp.tools import plugins


def _cfg() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AUTHORING}),
    )


def _command(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"command-{index:03d}",
        name=f"command-{index:03d}",
        plugin_id="large.plugin",
        scope="agent",
        qualifiedName=f"large.command-{index:03d}",
        fullyQualifiedName=f"large.plugin.command-{index:03d}",
        description="d" * 300,
        conf_schema={
            "type": "object",
            "properties": {
                f"field_{field}": {
                    "type": "string",
                    "description": "configuration field " + "x" * 120,
                }
                for field in range(6)
            },
        },
        conf_examples={"default": {"field_0": "value"}},
    )


async def test_command_plugin_index_is_compact_and_details_are_pageable(monkeypatch) -> None:
    command_items = [_command(index) for index in range(80)]
    plugin = SimpleNamespace(
        plugin_id="large.plugin",
        name="Large plugin",
        vendor="Example vendor",
        description="plugin description " + "z" * 20_000,
        commands=command_items,
    )

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return {plugin.plugin_id: plugin}

    monkeypatch.setattr(plugins, "call", fake_call)
    registry = ToolRegistry()
    collect_decorated_module(
        registry,
        plugins,
        _cfg(),
        profiles={Profile.AUTHORING},
    )
    index_spec = registry.get("tuoni_command_plugins_list")
    detail_spec = registry.get("tuoni_command_plugin_commands_list")
    assert index_spec is not None
    assert detail_spec is not None
    assert {"cursor", "limit"} <= detail_spec.input_schema["properties"].keys()

    server = ServerContext(c2=MagicMock(), cfg=_cfg())
    server.last_login = time.monotonic()
    context = MagicMock()
    context.request_context.lifespan_context = server
    invocation = InvocationContext(context=context, server=server)

    index_result = await registry.invoke_spec(index_spec, {"limit": 50}, invocation)
    index_payload = json.loads(index_result.content[0].text)  # type: ignore[union-attr]
    assert index_result.isError is False
    assert call_result_size(index_result) <= LIST_RESULT_BUDGET
    assert index_payload["data"]["plugins"] == [
        {
            "plugin_id": "large.plugin",
            "name": "Large plugin",
            "vendor": "Example vendor",
            "description": "plugin description " + "z" * 234 + "…",
            "command_count": 80,
            "detail": {"name": "tuoni_command_plugin_commands_list", "arguments": {"plugin_id": "large.plugin"}},
        }
    ]
    assert "commands" not in index_payload["data"]["plugins"][0]

    arguments = {"plugin_id": "large.plugin", "limit": 50}
    seen: list[str] = []
    pages = 0
    while True:
        result = await registry.invoke_spec(detail_spec, arguments, invocation)
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert result.isError is False
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        assert payload["data"]["plugin_id"] == "large.plugin"
        seen.extend(command["id"] for command in payload["data"]["commands"])
        pages += 1
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]

    assert pages > 1
    assert seen == [command.id for command in command_items]


async def test_command_plugin_detail_rejects_unknown_exact_id(monkeypatch) -> None:
    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return {}

    monkeypatch.setattr(plugins, "call", fake_call)
    registry = ToolRegistry()
    collect_decorated_module(
        registry,
        plugins,
        _cfg(),
        profiles={Profile.AUTHORING},
    )
    spec = registry.get("tuoni_command_plugin_commands_list")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=MagicMock())

    result = await registry.invoke_spec(
        spec,
        {"plugin_id": "missing.plugin"},
        invocation,
    )
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert result.isError is True
    assert payload["code"] == "COMMAND_PLUGIN_NOT_FOUND"
    assert call_result_size(result) <= 1024
