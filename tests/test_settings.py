from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size, redact
from tuoni_mcp.registry import (
    InvocationContext,
    ToolRegistry,
    build_dispatch_specs,
    collect_decorated_module,
)
from tuoni_mcp.tools import settings as settings_tools


def _cfg(*, mode: MCPMode = MCPMode.NATIVE) -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.ADMINISTRATION}),
        mode=mode,
    )


def _registry(cfg: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    collect_decorated_module(
        registry,
        settings_tools,
        cfg,
        profiles={Profile.ADMINISTRATION},
    )
    return registry


def _invocation(cfg: Settings) -> InvocationContext:
    server = ServerContext(c2=MagicMock(), cfg=cfg)
    context = MagicMock()
    context.request_context.lifespan_context = server
    return InvocationContext(context=context, server=server)


async def test_setting_get_preserves_small_native_response(monkeypatch) -> None:
    expected = {"key": "tuoni.example", "value": {"enabled": True, "count": 3}}

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return expected

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = _cfg()
    registry = _registry(cfg)
    spec = registry.get("tuoni_setting_get")
    assert spec is not None

    result = await registry.invoke_spec(
        spec,
        {"key": "tuoni.example"},
        _invocation(cfg),
    )
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert result.isError is False
    assert payload["data"] == expected
    assert payload["next"] is None


async def test_setting_key_is_encoded_as_one_path_segment(monkeypatch) -> None:
    seen: list[tuple] = []

    async def fake_call(_ctx, _fn, *args, **_kwargs):
        seen.append(args)
        return {"key": "../settings?admin=true", "currentValue": 1}

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = _cfg()
    registry = _registry(cfg)
    spec = registry.get("tuoni_setting_get")
    assert spec is not None

    result = await registry.invoke_spec(
        spec,
        {"key": "../settings?admin=true"},
        _invocation(cfg),
    )

    assert result.isError is False
    assert seen == [("/api/v1/setting/..%2Fsettings%3Fadmin%3Dtrue",)]


@pytest.mark.parametrize("resize", [False, True])
async def test_large_setting_windows_reconstruct_unicode_and_escaping(monkeypatch, resize) -> None:
    value = {
        "key": "tuoni.large",
        "value": ('emoji🙂 combining e\u0301 quote" slash\\\n' * 900),
        "password": "must-not-appear",
    }

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return value

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = _cfg()
    registry = _registry(cfg)
    spec = registry.get("tuoni_setting_get")
    assert spec is not None
    invocation = _invocation(cfg)
    arguments = {"key": "tuoni.large", "max_chars": 257}
    chunks: list[str] = []
    offsets: list[int] = []

    while True:
        result = await registry.invoke_spec(spec, arguments, invocation)
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert result.isError is False
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET
        data = payload["data"]
        offsets.append(data["offset_chars"])
        chunks.append(data["value_json"])
        assert data["returned_chars"] == len(data["value_json"])
        if payload["next"] is None:
            assert data["has_more"] is False
            break
        assert payload["next"]["name"] == "tuoni_setting_get"
        assert data["next_offset_chars"] == data["offset_chars"] + data["returned_chars"]
        arguments = payload["next"]["arguments"]
        if resize:
            arguments["max_chars"] = 16_384 if len(chunks) % 2 else 257

    assert offsets[0] == 0
    assert offsets[1:] == [
        offsets[index] + len(chunks[index])
        for index in range(len(chunks) - 1)
    ]
    reconstructed = json.loads("".join(chunks))
    assert reconstructed == redact(value)
    assert "must-not-appear" not in "".join(chunks)


@pytest.mark.parametrize("resize", [False, True])
async def test_dispatch_setting_continuation_is_directly_callable(monkeypatch, resize) -> None:
    value = {"value": "🙂\\\"" * 4000}

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return value

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = _cfg(mode=MCPMode.DISPATCH)
    registry = _registry(cfg)
    target = registry.get("tuoni_setting_get")
    assert target is not None
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, (target,), read_only=True)
    }
    invocation = _invocation(cfg)
    exact_call = {
        "name": "tuoni_call",
        "arguments": {
            "name": "tuoni_setting_get",
            "arguments": {"key": "large", "max_chars": 300},
        },
    }
    chunks: list[str] = []

    while True:
        result = await registry.invoke_spec(
            dispatch[exact_call["name"]],
            exact_call["arguments"],
            invocation,
        )
        payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert result.isError is False
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET
        chunks.append(payload["data"]["value_json"])
        if payload["next"] is None:
            break
        assert payload["next"]["name"] == "tuoni_call"
        exact_call = payload["next"]
        if resize:
            exact_call["arguments"]["arguments"]["max_chars"] = 16_384 if len(chunks) % 2 else 300

    assert json.loads("".join(chunks)) == value


async def test_setting_get_rejects_invalid_offset_cursor_and_drift(monkeypatch) -> None:
    state = {"value": {"value": "x" * 20_000}}

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return state["value"]

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = _cfg()
    registry = _registry(cfg)
    spec = registry.get("tuoni_setting_get")
    assert spec is not None
    invocation = _invocation(cfg)

    invalid_offset = await registry.invoke_spec(
        spec,
        {"key": "large", "offset_chars": 99_999},
        invocation,
    )
    invalid_cursor = await registry.invoke_spec(
        spec,
        {"key": "large", "cursor": "not-a-cursor"},
        invocation,
    )
    first = await registry.invoke_spec(
        spec,
        {"key": "large", "max_chars": 300},
        invocation,
    )
    first_payload = json.loads(first.content[0].text)  # type: ignore[union-attr]
    state["value"] = {"value": "changed" * 4000}
    drift = await registry.invoke_spec(
        spec,
        first_payload["next"]["arguments"],
        invocation,
    )

    assert json.loads(invalid_offset.content[0].text)["code"] == "INVALID_OFFSET"  # type: ignore[union-attr]
    assert json.loads(invalid_cursor.content[0].text)["code"] == "INVALID_CURSOR"  # type: ignore[union-attr]
    assert json.loads(drift.content[0].text)["code"] == "SETTING_CHANGED"  # type: ignore[union-attr]
    assert invalid_offset.isError is invalid_cursor.isError is drift.isError is True
