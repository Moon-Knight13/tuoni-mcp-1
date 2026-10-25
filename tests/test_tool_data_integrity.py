"""Malformed file inputs and sensitive schema literals never reach public outputs."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools._helpers import decode_b64


@pytest.mark.parametrize("encoded", ["!!!", "YWJj!", "YQ==junk", "YQ===", "YWJj_", "é"])
def test_invalid_base64_is_rejected_instead_of_silently_changing_bytes(encoded):
    with pytest.raises(ValueError, match="not valid base64"):
        decode_b64(encoded)


@pytest.mark.parametrize("encoded,expected", [("", b""), ("YWJj", b"abc"), (" YW\r\nJj\t", b"abc")])
def test_base64_accepts_empty_files_and_mime_whitespace(encoded, expected):
    assert decode_b64(encoded) == expected


def _invocation(fake_c2, profiles):
    cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                   profiles=frozenset(profiles))
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    return build_registry(cfg), InvocationContext(context=context, server=server)


@pytest.mark.parametrize("tool_name,arguments", [
    ("tuoni_file_upload", {"hosted_path": "/file.bin"}),
    ("tuoni_file_replace", {"file_id": "file-1"}),
])
async def test_invalid_base64_never_uploads_or_replaces_a_file(fake_c2, tool_name, arguments):
    registry, invocation = _invocation(fake_c2, {Profile.ARTIFACTS})
    result = await registry.invoke_spec(registry.get(tool_name), {**arguments, "content_base64": "!!!"}, invocation)
    assert result.isError is True
    assert fake_c2.calls == []


@pytest.mark.parametrize("shared", [False, True])
async def test_describe_redacts_sensitive_reference_literals_and_generated_examples(fake_c2, shared):
    properties = {"password": {"$ref": "#/$defs/value"}, "mode": {"type": "string", "enum": ["red", "blue"]}}
    if shared:
        properties["normal"] = {"$ref": "#/$defs/value"}
    schema = {
        "type": "object", "properties": properties,
        "required": ["normal" if shared else "password"],
        "$defs": {"value": {"type": "string", "minLength": 8, "default": "private-default",
                            "enum": ["private-enum"], "const": "private-const"}},
    }
    template = {"id": "template-1", "name": "example", "configurationSchema": schema}
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        "guid": "agent-1", "active": True, "availableCommandTemplates": ["template-1"],
    }
    fake_c2.responses["/api/v1/command-templates"] = [template]
    original = copy.deepcopy(template)
    registry, invocation = _invocation(fake_c2, {Profile.AGENT_OPS})
    arguments = {"agent_guid": "agent-1", "template": "example", "limit": 2}
    fields = []
    responses = []
    while True:
        result = await registry.invoke_spec(registry.get("tuoni_command_describe"), arguments, invocation)
        assert result.isError is False
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET
        responses.append(result.model_dump_json())
        payload = json.loads(result.content[0].text)
        fields.extend(payload["data"]["fields"])
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]
    serialized = "".join(responses)
    assert all(literal not in serialized for literal in ("private-default", "private-enum", "private-const"))
    assert any(field.get("reason") == "secret_literals_redacted" for field in fields)
    assert any(field.get("enum") == ["red", "blue"] for field in fields)
    assert any(field.get("minLength") == 8 for field in fields)
    assert template == original, "Privacy filtering must not alter the live validation schema"


@pytest.mark.parametrize("field", ["page", "item_offset"])
@pytest.mark.parametrize("position", [-1, "1", 0.5, True, None, {}, []])
async def test_agent_cursor_rejects_invalid_positions_before_request(fake_c2, field, position):
    registry, invocation = _invocation(fake_c2, {Profile.AGENT_OPS})
    cursor = registry.cursors.encode({"tool": "tuoni_agents_list", "active": True, "inactive": False,
                            "limit": 10, "page": 0, "item_offset": 0, field: position})
    result = await registry.invoke_spec(registry.get("tuoni_agents_list"), {"cursor": cursor}, invocation)
    assert result.isError is True
    assert json.loads(result.content[0].text)["code"] == "INVALID_CURSOR"
    assert fake_c2.calls == []


@pytest.mark.parametrize("name,arguments", [
    ("tuoni_commands_search", {"agent_guid": "agent-1", "limit": 1}),
    ("tuoni_command_describe", {"agent_guid": "agent-1", "template": "template-1", "limit": 1}),
    ("tuoni_command_get", {"command_id": 1, "limit": 1}),
    ("tuoni_setting_get", {"key": "example"}),
])
@pytest.mark.parametrize("position", [-1, "1", 0.5, True])
async def test_detail_and_search_cursors_reject_coerced_offsets(fake_c2, name, arguments, position):
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        "guid": "agent-1", "active": True, "availableCommandTemplates": ["template-1", "template-2"],
    }
    fake_c2.responses["/api/v1/command-templates"] = [
        {"id": f"template-{index}", "name": f"example-{index}", "configurationSchema": {
            "type": "object", "properties": {"first": {"type": "string"}, "second": {"type": "string"}},
        }}
        for index in (1, 2)
    ]
    fake_c2.responses["/api/v1/commands/1"] = {
        "id": 1, "status": "COMPLETE", "result": {"childResults": [
            {"type": "text", "value": "first"}, {"type": "text", "value": "second"},
        ]},
    }
    fake_c2.responses["/api/v1/setting/example"] = {"value": "large value " * 2000}
    registry, invocation = _invocation(fake_c2, {Profile.AGENT_OPS, Profile.ADMINISTRATION})
    first = await registry.invoke_spec(registry.get(name), arguments, invocation)
    assert first.isError is False
    payload = json.loads(first.content[0].text)
    state = registry.cursors.decode(payload["next_cursor"], tool=name)
    cursor = registry.cursors.encode({**state, "offset": position})
    result = await registry.invoke_spec(registry.get(name), {**arguments, "cursor": cursor}, invocation)
    assert result.isError is True
    assert json.loads(result.content[0].text)["code"] == "INVALID_CURSOR"


def test_event_cursor_rejects_boolean_sequence():
    from tuoni_mcp.registry import ToolFailure
    from tuoni_mcp.tools.events import _cursor, _decode_cursor

    with pytest.raises(ToolFailure, match="invalid"):
        _decode_cursor(_cursor(True, ()), (), 10)
