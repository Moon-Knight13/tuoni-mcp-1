"""Alias edits preserve multipart state using the server's request/response contract."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry


FILE_IDS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")


class AliasServer:
    """Model the API's partial edit and multipart replacement semantics."""

    def __init__(self):
        self.alias = {
            "id": "alias-original", "name": "example", "description": "original description",
            "fixedConfiguration": {"mode": "original", "@files": {"input": [
                {"fileId": file_id, "fileName": "input", "originalFileName": f"part-{index}.dat"}
                for index, file_id in enumerate(FILE_IDS)
            ]}},
        }
        self.writes = []

    def request_get(self, uri):
        assert uri == "/api/v1/command-alias/alias-original"
        return copy.deepcopy(self.alias)

    def request_put(self, uri, body):
        assert uri == "/api/v1/command-alias/alias-original"
        self.writes.append(copy.deepcopy(body))
        if "fixedConfiguration" in body:
            configuration = copy.deepcopy(body["fixedConfiguration"])
            assert "@files" not in configuration, "Response metadata was sent as JSON configuration"
            previous_files = self.alias["fixedConfiguration"].get("@files", {})
            retained = {}
            for reference in body.get("configurationFiles", []):
                field = reference["fileName"]
                part = next(part for part in previous_files[field] if part["fileId"] == reference["existingFileId"])
                retained.setdefault(field, []).append(part)
            if retained:
                configuration["@files"] = retained
            if configuration != self.alias["fixedConfiguration"]:
                self.alias["id"] = "alias-recreated"
                self.alias["fixedConfiguration"] = configuration
        if "description" in body:
            self.alias["description"] = body["description"]
        return copy.deepcopy(self.alias)


@pytest.fixture(params=list(MCPMode))
def alias_context(request):
    cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                   profiles=frozenset({Profile.AUTHORING}), mode=request.param)
    backend = AliasServer()
    server = ServerContext(c2=backend, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    registry = build_registry(cfg)
    exposure = registry.select(cfg.profiles, read_only=False)
    if cfg.mode is MCPMode.DISPATCH:
        exposure = build_dispatch_specs(registry, exposure, read_only=False)
    exposed = {spec.name: spec for spec in exposure}

    async def update(**arguments):
        arguments = {"alias_id": "alias-original", **arguments}
        if cfg.mode is MCPMode.DISPATCH:
            result = await registry.invoke_spec(exposed["tuoni_call"], {
                "name": "tuoni_alias_update", "arguments": arguments,
            }, invocation)
            assert not result.isError
            prepared = json.loads(result.content[0].text)
            assert not backend.writes, "Dispatch preparation changed the alias"
            result = await registry.invoke_spec(exposed["tuoni_operation_execute"],
                                                prepared["next"]["arguments"], invocation)
        else:
            result = await registry.invoke_spec(exposed["tuoni_alias_update"], arguments, invocation)
        payload = json.loads(result.content[0].text)
        if cfg.mode is MCPMode.DISPATCH and not result.isError:
            payload["data"] = payload["data"]["result"]
        return result, payload

    return backend, update


async def test_description_edit_keeps_identity_configuration_and_all_files(alias_context):
    backend, update = alias_context
    before = copy.deepcopy(backend.alias)
    result, payload = await update(description="new description")
    assert not result.isError
    assert backend.alias == {**before, "description": "new description"}
    assert payload["data"]["alias_id"] == "alias-original"
    assert "previous_alias_id" not in payload["data"]
    assert backend.writes == [{"description": "new description"}]


@pytest.mark.parametrize("description", [None, ""])
async def test_configuration_edit_preserves_all_files_and_optional_description(alias_context, description):
    backend, update = alias_context
    previous_files = copy.deepcopy(backend.alias["fixedConfiguration"]["@files"])
    result, payload = await update(configuration={"mode": "changed"}, description=description)
    assert not result.isError
    assert backend.alias["fixedConfiguration"] == {"mode": "changed", "@files": previous_files}
    assert backend.alias["description"] == ("original description" if description is None else description)
    assert payload["data"]["previous_alias_id"] == "alias-original"
    assert backend.writes[0]["configurationFiles"] == [
        {"fileName": "input", "existingFileId": file_id} for file_id in FILE_IDS
    ]


async def test_omitted_fields_are_a_noop(alias_context):
    backend, update = alias_context
    before = copy.deepcopy(backend.alias)
    result, _ = await update()
    assert not result.isError
    assert backend.alias == before
    assert not backend.writes


async def test_configuration_edit_without_files_does_not_invent_files(alias_context):
    backend, update = alias_context
    del backend.alias["fixedConfiguration"]["@files"]
    result, _ = await update(configuration={})
    assert not result.isError
    assert backend.alias["fixedConfiguration"] == {}
    assert backend.writes[0]["configurationFiles"] == []


@pytest.mark.parametrize("metadata", [[], "invalid", {"input": {}}, {"input": [None]},
                                      {"input": [{"fileId": ""}]}, {"input": [{"fileId": 1}]},
                                      {"input": [{"fileId": "invalid-id"}]}])
async def test_invalid_attachment_metadata_never_causes_a_write(alias_context, metadata):
    backend, update = alias_context
    backend.alias["fixedConfiguration"]["@files"] = metadata
    result, _ = await update(configuration={"mode": "changed"})
    assert result.isError
    assert not backend.writes


async def test_response_metadata_is_not_accepted_as_configuration(alias_context):
    backend, update = alias_context
    result, _ = await update(configuration={"mode": "changed", "@files": {}})
    assert result.isError
    assert not backend.writes
