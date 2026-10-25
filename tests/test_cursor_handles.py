"""Opaque cursor integrity, resource bounds, and exact read recovery."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tuoni_mcp.config import MCPMode
from tuoni_mcp.pagination import CursorStore, cursor_scope, decode_cursor, encode_cursor
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size

from test_settings import _cfg, _invocation, _registry


def test_handles_hide_state_and_are_bound_to_one_store_and_tool():
    store = CursorStore()
    state = {"tool": "read", "offset": 20, "fingerprint": "private-state"}
    token = store.encode(state)
    assert len(token) == 26 and "private" not in token and "read" not in token
    assert store.encode(state) == token
    assert store.decode(token, tool="read") == {"v": 1, **state}
    for other in (token[:-1], token + "x", "c_" + "0" * 24,
                  token[:-1] + ("0" if token[-1] != "0" else "1")):
        with pytest.raises(ValueError, match="invalid"):
            store.decode(other, tool="read")
    with pytest.raises(ValueError, match="belong"):
        store.decode(token, tool="different")
    with pytest.raises(ValueError, match="invalid"):
        CursorStore().decode(token, tool="read")
    # A caller cannot mutate the saved state through a decoded object.
    store.decode(token, tool="read")["offset"] = 999
    assert store.decode(token, tool="read")["offset"] == 20


def test_handles_expire_evict_and_bound_individual_state():
    now = [0.0]
    store = CursorStore(capacity=2, ttl=60, clock=lambda: now[0])
    first = store.encode({"tool": "read", "offset": 1})
    second = store.encode({"tool": "read", "offset": 2})
    for _ in range(100):
        assert store.encode({"tool": "read", "offset": 2}) == second
    assert store.decode(first, tool="read")["offset"] == 1
    store.encode({"tool": "read", "offset": 3})
    with pytest.raises(ValueError, match="invalid"):
        store.decode(second, tool="read")
    now[0] = 59
    assert store.decode(first, tool="read")["offset"] == 1
    now[0] = 120
    with pytest.raises(ValueError, match="expired"):
        store.decode(first, tool="read")
    with pytest.raises(ValueError, match="4096-byte"):
        store.encode({"tool": "read", "scope": "x" * 4096})


async def test_cursor_scope_survives_interleaved_tasks_without_leaking():
    import asyncio

    async def read(offset):
        with cursor_scope(CursorStore()):
            token = encode_cursor({"tool": "read", "offset": offset})
            await asyncio.sleep(0)
            assert decode_cursor(token, tool="read")["offset"] == offset
            return token

    tokens = await asyncio.gather(read(1), read(2))
    for token in tokens:
        with pytest.raises(ValueError, match="invalid"):
            decode_cursor(token, tool="read")


@pytest.mark.parametrize("mode", [MCPMode.NATIVE, MCPMode.DISPATCH])
@pytest.mark.parametrize("prefix", ["mcp__tuoni__", "tuoni_"])
@pytest.mark.parametrize("failure", ["corrupt", "scope", "restart", "expired", "changed"])
async def test_setting_cursor_failures_offer_callable_exact_restart(monkeypatch, mode, prefix, failure):
    from tuoni_mcp.registry import build_dispatch_specs
    from tuoni_mcp.tools import settings as settings_tools

    state = {"value": "x" * 20_000}

    async def fake_call(_ctx, _fn, *_args, **_kwargs):
        return state

    monkeypatch.setattr(settings_tools, "call", fake_call)
    cfg = replace(_cfg(mode=mode), tool_prefix=prefix)
    registry = _registry(cfg)
    invocation = _invocation(cfg)

    async def invoke(arguments, *, exact=False):
        if mode is MCPMode.DISPATCH:
            specs = {s.name: s for s in build_dispatch_specs(registry, (registry.get("tuoni_setting_get"),), read_only=True)}
            name = "tuoni_call"
            if not exact:
                arguments = {"name": "tuoni_setting_get", "arguments": arguments}
        else:
            specs = {"tuoni_setting_get": registry.get("tuoni_setting_get")}
            name = "tuoni_setting_get"
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        if result.isError:
            assert call_result_size(result) <= ERROR_RESULT_BUDGET
        return json.loads(result.content[0].text)

    now = [0.0]
    registry.cursors = CursorStore(clock=lambda: now[0])
    first = await invoke({"key": "large", "max_chars": 300})
    arguments = {"key": "large", "max_chars": 300, "cursor": first["next_cursor"]}
    if failure == "corrupt":
        token = arguments["cursor"]
        arguments["cursor"] = token[:-1] + ("0" if token[-1] != "0" else "1")
    elif failure == "scope":
        arguments["key"] = "another"
    elif failure == "restart":
        registry = _registry(cfg)
    elif failure == "expired":
        now[0] = 3601
    else:
        state["value"] = "changed" * 4000
    error = await invoke(arguments)
    assert error["code"] == {"changed": "SETTING_CHANGED", "scope": "CURSOR_SCOPE_CHANGED",
                             "expired": "CURSOR_EXPIRED"}.get(failure, "INVALID_CURSOR")
    target = {"name": "tuoni_setting_get", "arguments": {"key": arguments["key"], "max_chars": 300}}
    if mode is MCPMode.DISPATCH:
        target = {"name": "tuoni_call", "arguments": target}
    assert error["next"] == {**target, "name": prefix + target["name"]}
    restarted = await invoke(error["next"]["arguments"], exact=True)
    assert restarted["ok"] and restarted["data"]["offset_chars"] == 0
