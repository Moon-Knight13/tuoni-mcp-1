"""Optional catalog summaries lead to exact, bounded configuration details."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, ToolFailure, build_dispatch_specs
from tuoni_mcp.tools import build_registry


def _catalog(fake_c2, mode, prefix):
    cfg = Settings(url="https://example.test", username="fixture", password="fixture", verify=True,
                   profiles=frozenset({Profile.ACCESS, Profile.AUTHORING}), read_only=True,
                   mode=mode, tool_prefix=prefix)
    registry = build_registry(cfg)
    selected = registry.select(cfg.profiles, read_only=True)
    exposed = build_dispatch_specs(registry, selected, read_only=True) if mode is MCPMode.DISPATCH else selected
    specs = {spec.name: spec for spec in exposed}
    server = ServerContext(c2=fake_c2, cfg=cfg)
    server.last_login = time.monotonic()
    context = MagicMock()
    context.request_context.lifespan_context = server
    invocation = InvocationContext(context=context, server=server)

    async def call(descriptor, budget=DETAIL_RESULT_BUDGET):
        name = descriptor["name"]
        if prefix and name.startswith(prefix + "tuoni_"):
            name = name[len(prefix):]
        result = await registry.invoke_spec(specs[name], descriptor["arguments"], invocation)
        assert call_result_size(result) <= budget
        return result, json.loads(result.content[0].text)

    def initial(name, arguments):
        if mode is MCPMode.DISPATCH:
            return {"name": "tuoni_call", "arguments": {"name": name, "arguments": arguments}}
        return {"name": name, "arguments": arguments}

    return call, initial


def _schema():
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "password": {"$ref": "#/$defs/Setting"},
            **{f"field-{i}": {"type": "string", "description": 'Unicode🙂 quote" slash\\\n' * 12,
                              "enum": ["alpha", "beta"]} for i in range(65)},
        },
        "required": [f"field-{i}" for i in range(65)],
        "$defs": {"Setting": {"type": "string", "minLength": 8, "default": "schema-secret"}},
    }


async def _read_detail(call, descriptor):
    windows = []
    for _ in range(100):
        result, payload = await call(descriptor)
        assert not result.isError, payload
        data = payload["data"]
        if data.get("value_format") != "json":
            assert not windows
            return data
        assert data["offset_chars"] == sum(map(len, windows))
        assert data["returned_chars"] == len(data["value_json"]) > 0
        windows.append(data["value_json"])
        if payload["next"] is None:
            assert sum(map(len, windows)) == data["total_chars"]
            return json.loads("".join(windows))
        descriptor = payload["next"]
    pytest.fail("Detail windows did not finish")


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "mcp__tuoni__"])
@pytest.mark.parametrize("kind", ["command", "payload"])
async def test_catalog_ids_and_wide_schema_details_remain_complete(fake_c2, mode, prefix, kind):
    plugin_id = "fixture." + "界" * 180
    template_id = "opaque / + : " + "界" * 180
    schema = _schema()
    examples = {"default": {"password": "example-secret", "field-0": "alpha"}}
    template = SimpleNamespace(
        id=template_id, name="Template", plugin_id=plugin_id, scope="global",
        qualifiedName=template_id, fullyQualifiedName=template_id, description="Fixture template",
        conf_schema=schema, conf_examples=examples,
    )
    plugin = SimpleNamespace(plugin_id=plugin_id, name="Plugin", vendor="Fixture", description="Fixture",
                             commands=[template], templates=[template])
    fake_c2.responses[f"load_{kind}_plugins"] = {plugin_id: plugin}
    if kind == "payload":
        fake_c2.responses[f"/api/v1/payloads/templates/{url_path_segment(template_id)}"] = {
            "id": "deprecated-id", "name": template_id, "pluginId": plugin_id,
            "configurationSchema": schema, "description": "Fixture template",
            "exampleConfigurations": [{"name": "example", "configuration": examples["default"]}],
            "availableListenerIds": [1, 2], "status": "ENABLED", "payloadType": "fixture",
        }
    call, initial = _catalog(fake_c2, mode, prefix)
    result, index = await call(initial(f"tuoni_{kind}_plugins_list", {}), LIST_RESULT_BUDGET)
    assert not result.isError
    summary = index["data"]["plugins"][0]
    assert summary["plugin_id"] == plugin_id
    result, templates = await call(summary["detail"], LIST_RESULT_BUDGET)
    assert not result.isError
    assert templates["data"]["plugin_id"] == plugin_id
    record = templates["data"]["commands" if kind == "command" else "templates"][0]
    assert record["id"] == template_id
    assert "configuration_schema" not in record
    detail = await _read_detail(call, record["detail"])
    expected_schema = copy.deepcopy(schema)
    del expected_schema["$defs"]["Setting"]["default"]
    assert detail["configuration_schema"] == expected_schema
    assert detail["configuration_schema_redacted"] is True
    assert detail["configuration_examples"]
    assert "schema-secret" not in json.dumps(detail)
    assert "example-secret" not in json.dumps(detail)
    assert all(name in {"load_command_plugins", "load_payload_plugins", "request_get"}
               for name, _, _ in fake_c2.calls)


@pytest.mark.parametrize("name, arguments", [
    ("tuoni_command_plugin_command_get", {"plugin_id": "missing", "template_id": "missing"}),
    ("tuoni_payload_plugin_templates_list", {"plugin_id": "missing"}),
    ("tuoni_payload_template_get", {"template_id": "missing"}),
])
async def test_unknown_catalog_identifiers_are_actionable(fake_c2, name, arguments):
    call, initial = _catalog(fake_c2, MCPMode.DISPATCH, "tuoni_")
    result, payload = await call(initial(name, arguments))
    assert result.isError
    assert payload["code"] in {"COMMAND_PLUGIN_NOT_FOUND", "PAYLOAD_PLUGIN_NOT_FOUND", "NOT_FOUND"}


async def test_payload_template_malformed_metadata_is_rejected(fake_c2):
    fake_c2.responses["/api/v1/payloads/templates/fixture"] = {"configurationSchema": "bad"}
    call, initial = _catalog(fake_c2, MCPMode.NATIVE, "")
    result, payload = await call(initial("tuoni_payload_template_get", {"template_id": "fixture"}))
    assert result.isError and payload["code"] == "INVALID_RESPONSE"


@pytest.mark.parametrize("name, field, extra", [
    ("tuoni_listener_get", "listener_id", {}),
    ("tuoni_listener_update", "listener_id", {}),
    ("tuoni_listener_start", "listener_id", {}),
    ("tuoni_listener_stop", "listener_id", {}),
    ("tuoni_listener_delete", "listener_id", {}),
    ("tuoni_job_get", "job_id", {}),
    ("tuoni_job_restart", "job_id", {}),
    ("tuoni_job_pause", "job_id", {}),
    ("tuoni_job_resume", "job_id", {}),
    ("tuoni_payload_get", "payload_id", {}),
    ("tuoni_payload_download", "payload_id", {}),
    ("tuoni_payload_rename", "payload_id", {"new_name": "fixture"}),
    ("tuoni_payload_delete", "payload_id", {}),
    ("tuoni_payload_create", "listener_id", {"template": "fixture", "configuration": {}}),
])
@pytest.mark.parametrize("invalid", [True, False, "1", 0, -1, 1.5])
def test_numeric_resource_ids_do_not_coerce_into_another_resource(name, field, extra, invalid):
    cfg = Settings(url="https://example.test", username="fixture", password="fixture", verify=True)
    registry = build_registry(cfg)
    spec = registry.get(name)
    with pytest.raises(ToolFailure) as exc:
        registry.validate(spec, {field: invalid, **extra})
    assert exc.value.code == "INVALID_ARGUMENTS"
    assert registry.validate(spec, {field: 1, **extra}).model_dump()[field] == 1
