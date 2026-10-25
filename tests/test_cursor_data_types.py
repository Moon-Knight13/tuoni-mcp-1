"""Cursor positions and versions preserve JSON integer types before any reads."""

from __future__ import annotations

import base64
import hashlib
import json
from types import ModuleType, SimpleNamespace

import pytest

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.pagination import decode_cursor, encode_cursor
from tuoni_mcp.registry import InvocationContext, ToolFailure, ToolRegistry, _DispatchCursors, collect_decorated_module


@pytest.mark.parametrize("version", [True, False, "1", 1.0, None])
def test_cursor_versions_require_the_exact_json_integer(version):
    cursor = encode_cursor({"v": version, "tool": "list"})
    with pytest.raises(ValueError, match="version is invalid"):
        decode_cursor(cursor, tool="list")


@pytest.mark.parametrize("offset", [True, False, "1", 1.0, -1, None, 0, 1])
def test_dispatch_rejects_forged_json_positions_instead_of_trusting_client_offsets(offset):
    cursor = base64.urlsafe_b64encode(json.dumps({"offset": offset, "query": "file"}).encode()).decode()
    with pytest.raises(ToolFailure, match="invalid"):
        _DispatchCursors().decode(cursor, "file")


@pytest.mark.parametrize("field,value", [
    ("offset", True), ("offset", False), ("offset", "1"), ("offset", 1.0), ("offset", -1),
    ("limit", True), ("limit", "1"), ("limit", 1.0),
    ("v", True), ("v", 1.0),
])
async def test_profile_lists_reject_coerced_cursor_fields_before_upstream_reads(field, value):
    reads = []
    module = ModuleType("synthetic_profile")

    def register(mcp, cfg):
        @mcp.tool(name="tuoni_files_list")
        async def files_list():
            reads.append(True)
            return {"files": [{"id": "file-0"}, {"id": "file-1"}]}

    module.register = register
    cfg = Settings(url="https://example.test", username="operator", password="private", verify=True)
    registry = ToolRegistry()
    collect_decorated_module(registry, module, cfg, profiles={Profile.ARTIFACTS})
    spec = registry.get("tuoni_files_list")
    cursor = registry.cursors.encode({
        "tool": "profile-list:tuoni_files_list", "offset": 0,
        "scope": hashlib.sha256(b"{}").hexdigest(), "limit": 1, field: value,
    })
    invocation = InvocationContext(context=None, server=SimpleNamespace(cfg=cfg))
    response = await registry.invoke_spec(spec, {"cursor": cursor, "limit": 1}, invocation)
    assert response.isError
    assert json.loads(response.content[0].text)["code"] == "INVALID_CURSOR"
    assert reads == []


def test_integer_cursor_values_still_decode():
    cursor = encode_cursor({"tool": "list", "offset": 0})
    assert decode_cursor(cursor, tool="list")["offset"] == 0
    store = _DispatchCursors()
    cursor = store.encode(0, "file")
    assert store.decode(cursor, "file") == 0
