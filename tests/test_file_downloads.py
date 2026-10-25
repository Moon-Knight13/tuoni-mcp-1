from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext
from tuoni_mcp.tools import build_registry


def _settings() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.ACCESS, Profile.ARTIFACTS, Profile.AUTHORING}),
    )


def _payload(result) -> dict:
    assert result.isError is False
    return json.loads(result.content[0].text)


async def test_additive_download_tools_return_bounded_references_without_binary_fetch(
    fake_c2,
) -> None:
    cfg = _settings()
    fake_c2.responses["/api/v1/file/file-1"] = {
        "fileId": "file-1",
        "originalFileName": ("\u0000\"\\🙂" * 20_000),
        "size": 50_000_000,
        "filePaths": [
            "/hosted/" + ("\u0000\"\\🙂" * 2_000) for _ in range(100)
        ],
        "downloadHref": (
            "https://embedded:password@elsewhere.test/api/v1/file/file-1/download"
            "?access_token=never-return-this"
        ),
        "metadata": "m" * 100_000,
    }
    fake_c2.responses[("load_payload", 7)] = SimpleNamespace(
        name="payload-7",
        status="AVAILABLE",
        os="WINDOWS",
        architecture="AMD64",
    )
    server = ServerContext(c2=fake_c2, cfg=cfg)
    server.last_login = time.monotonic()
    context = MagicMock()
    context.request_context.lifespan_context = server
    invocation = InvocationContext(context=context, server=server)
    registry = build_registry(cfg)

    results = {
        "file": await registry.invoke_spec(
            registry.get("tuoni_file_download"),  # type: ignore[arg-type]
            {"file_id": "file-1"},
            invocation,
        ),
        "payload": await registry.invoke_spec(
            registry.get("tuoni_payload_download"),  # type: ignore[arg-type]
            {"payload_id": 7},
            invocation,
        ),
        "alias": await registry.invoke_spec(
            registry.get("tuoni_alias_download_file"),  # type: ignore[arg-type]
            {"alias_id": "alias-1", "file_id": "alias-file-1"},
            invocation,
        ),
    }

    for result in results.values():
        payload = _payload(result)
        serialized = result.model_dump_json(by_alias=True, exclude_none=True)
        download = payload["data"]["download"]
        assert download["method"] == "GET"
        assert download["requires_tuoni_authentication"] is True
        assert download["content_included"] is False
        assert download["url"].startswith("https://example.test:8443/api/v1/")
        assert "content_base64" not in serialized
        assert "never-return-this" not in serialized
        assert "url-secret" not in serialized
        assert call_result_size(result) <= DETAIL_RESULT_BUDGET

    file_data = _payload(results["file"])["data"]
    assert 1 <= len(file_data["file_paths"]) <= 12
    assert file_data["file_paths_remaining"] == 100 - len(file_data["file_paths"])
    assert len(file_data["original_filename"].encode()) <= 256

    # Only JSON metadata may be fetched. Binary endpoints are returned as links,
    # never called by the MCP server.
    assert [call[0] for call in fake_c2.calls] == ["request_get", "load_payload"]
    request_args = fake_c2.calls[0][1]
    assert request_args == ("/api/v1/file/file-1", True, False)


async def test_hosted_path_traversal_is_rejected_before_any_c2_call(fake_c2) -> None:
    cfg = _settings()
    server = ServerContext(c2=fake_c2, cfg=cfg)
    server.last_login = time.monotonic()
    context = MagicMock()
    context.request_context.lifespan_context = server
    invocation = InvocationContext(context=context, server=server)
    registry = build_registry(cfg)

    read = await registry.invoke_spec(
        registry.get("tuoni_file_get_by_path"),  # type: ignore[arg-type]
        {"path": "/payloads/../settings"},
        invocation,
    )
    delete = await registry.invoke_spec(
        registry.get("tuoni_file_delete_by_path"),  # type: ignore[arg-type]
        {"path": "/payloads/%252e%252e/settings"},
        invocation,
    )
    upload = await registry.invoke_spec(
        registry.get("tuoni_file_upload"),
        {"hosted_path": "/payloads/%2525252e%2525252e/settings", "content_base64": "eA=="},
        invocation,
    )

    assert read.isError is delete.isError is upload.isError is True
    assert json.loads(read.content[0].text)["code"] == "INVALID_ARGUMENTS"  # type: ignore[union-attr]
    assert json.loads(delete.content[0].text)["code"] == "INVALID_ARGUMENTS"  # type: ignore[union-attr]
    assert fake_c2.calls == []


def test_authenticated_download_reference_removes_embedded_url_credentials():
    from tuoni_mcp.tools._helpers import authenticated_download_reference

    reference = authenticated_download_reference(
        "https://operator:url-secret@example.test:8443", "/api/v1/file/file-1/download"
    )
    assert reference["url"] == "https://example.test:8443/api/v1/file/file-1/download"
    assert "url-secret" not in json.dumps(reference)


def test_additive_download_schemas_and_descriptions_are_body_free() -> None:
    registry = build_registry(_settings())
    by_name = {spec.name: spec for spec in registry.specs}

    for name in (
        "tuoni_file_download",
        "tuoni_payload_download",
        "tuoni_alias_download_file",
    ):
        spec = by_name[name]
        assert "never" in spec.description
        assert "content_base64" not in json.dumps(spec.input_schema)

    file_id = by_name["tuoni_file_download"].input_schema["properties"]["file_id"]
    assert file_id["maxLength"] == 256
    assert "hosted-file ID" in file_id["description"]
    payload_id = by_name["tuoni_payload_download"].input_schema["properties"]["payload_id"]
    assert payload_id["minimum"] == 1
