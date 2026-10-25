from __future__ import annotations

import hashlib
import asyncio
import threading
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import TOKEN_REFRESH_AFTER_SECONDS, ServerContext
from tuoni_mcp.registry import InvocationContext
from tuoni_mcp.tools import build_registry


def _settings(
    *,
    download_dir: Path | None,
    download_max_bytes: int = 128 * 1024 * 1024,
    artifacts: bool = True,
) -> Settings:
    profiles = {Profile.AGENT_OPS}
    if artifacts:
        profiles.add(Profile.ARTIFACTS)
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset(profiles),
        download_dir=str(download_dir) if download_dir is not None else None,
        download_max_bytes=download_max_bytes,
    )


def _result(parts: list[dict]) -> dict:
    return {
        "status": "SUCCESS",
        "errorMessage": None,
        "received": "2026-07-21T00:00:02Z",
        "childResults": parts,
    }


def _command(parts: list[dict], *, status: str = "COMPLETE") -> dict:
    return {
        "id": 42,
        "commandTemplateId": "template-enumerate",
        "commandTemplateName": "enumerate",
        "status": status,
        "created": "2026-07-21T00:00:00Z",
        "sent": "2026-07-21T00:00:01Z",
        "result": _result(parts),
    }


def _file_part(
    *,
    filename: str = "../../operator report?.txt",
    href: str = (
        "https://href-user:href-password@foreign.test/download/result.bin"
        "?access_token=query-secret#fragment-secret"
    ),
) -> dict:
    return {"type": "file", "filename": filename, "href": href}


def _invocation(fake_c2, cfg: Settings, command: dict):
    fake_c2.responses["/api/v1/commands/42"] = command
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


async def test_same_size_modified_download_is_not_reused(fake_c2, tmp_path):
    cfg = _settings(download_dir=tmp_path)
    part = {"type": "file", "filename": "evidence.txt", "href": "/download/evidence"}
    registry, invocation = _invocation(fake_c2, cfg, _command([part]))
    fake_c2.responses["/download/evidence"] = b"original"
    first = _payload(await _call(registry, invocation, "tuoni_command_file_download", {
        "command_id": 42, "part_index": 0
    }))
    path = Path(first["data"]["local_path"])
    path.write_bytes(b"modified")
    response = await _call(registry, invocation, "tuoni_command_file_download", {
        "command_id": 42, "part_index": 0
    })
    assert response.isError
    assert _payload(response)["code"] == "DOWNLOAD_PATH_COLLISION"
    assert path.read_bytes() == b"modified"


async def test_concurrent_downloads_do_not_observe_partial_files(fake_c2, tmp_path, monkeypatch):
    import tuoni_mcp.tools.commands as module

    original = module._save_result_download
    active = 0
    guard = threading.Lock()

    def save(*args, **kwargs):
        nonlocal active
        with guard:
            active += 1
            assert active == 1, "overlapping writes can expose a partial file"
        try:
            time.sleep(0.01)
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(module, "_save_result_download", save)
    cfg = _settings(download_dir=tmp_path)
    registry, invocation = _invocation(fake_c2, cfg, _command([
        {"type": "file", "filename": "evidence.txt", "href": "/download/evidence"}
    ]))
    fake_c2.responses["/download/evidence"] = b"evidence" * 10_000
    responses = await asyncio.gather(*[
        _call(registry, invocation, "tuoni_command_file_download", {"command_id": 42, "part_index": 0})
        for _ in range(5)
    ])
    assert all(not response.isError for response in responses)
    data = [_payload(response)["data"] for response in responses]
    assert len({item["local_path"] for item in data}) == 1
    assert sum(not item["already_present"] for item in data) == 1
    assert not invocation.server.result_download_locks


@pytest.mark.parametrize("clock_now", [20.0, 10_000.0])
async def test_command_file_download_fetches_authenticated_bytes_into_unique_safe_paths(
    fake_c2,
    tmp_path: Path,
    monkeypatch,
    clock_now: float,
) -> None:
    # Control only authentication's clock; freezing time.monotonic globally
    # would also freeze asyncio/AnyIO deadlines. Zero is not an expired-token
    # sentinel on runners whose monotonic clock is below the refresh interval.
    monkeypatch.setattr(
        "tuoni_mcp.connection.time", SimpleNamespace(monotonic=lambda: clock_now)
    )
    download_dir = tmp_path / "downloads"
    cfg = _settings(download_dir=download_dir)
    first_part = _file_part()
    second_part = _file_part(
        href=(
            "https://second-user:second-password@foreign.test/download/result-2.bin"
            "?access_token=second-query-secret#second-fragment-secret"
        )
    )
    registry, invocation = _invocation(fake_c2, cfg, _command([first_part, second_part]))
    invocation.server.last_login = clock_now - TOKEN_REFRESH_AFTER_SECONDS - 1
    first_body = b"DOWNLOAD_BODY_SECRET\x00\xff"
    second_body = b"SECOND_DOWNLOAD_BODY_SECRET"
    first_request_path = "/download/result.bin?access_token=query-secret"
    second_request_path = "/download/result-2.bin?access_token=second-query-secret"
    fake_c2.responses[first_request_path] = first_body
    fake_c2.responses[second_request_path] = second_body

    first = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": 0},
    )
    second = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": 1},
    )
    repeated = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": 0},
    )

    assert first.isError is second.isError is repeated.isError is False
    first_data = _payload(first)["data"]
    second_data = _payload(second)["data"]
    repeated_data = _payload(repeated)["data"]
    for expected_index, expected_body, data in (
        (0, first_body, first_data),
        (1, second_body, second_data),
    ):
        local_path = Path(data["local_path"])
        assert local_path.is_absolute()
        assert local_path.parent.resolve() == download_dir.resolve()
        assert local_path.read_bytes() == expected_body
        assert ".." not in local_path.name
        assert "/" not in local_path.name
        assert "\\" not in local_path.name
        assert data["command_id"] == 42
        assert data["part_index"] == expected_index
        assert data["size_bytes"] == len(expected_body)
        assert data["sha256"] == hashlib.sha256(expected_body).hexdigest()

    assert first_data["local_path"] != second_data["local_path"]
    assert repeated_data["local_path"] == first_data["local_path"]
    assert repeated_data["already_present"] is True
    assert len(list(download_dir.iterdir())) == 2

    binary_gets = [
        call
        for call in fake_c2.calls
        if call[0] == "request_get" and call[1][2] is True
    ]
    assert [call[1] for call in binary_gets] == [
        (first_request_path, False, True),
        (second_request_path, False, True),
        (first_request_path, False, True),
    ]
    assert [call[0] for call in fake_c2.calls].count("login") == 1

    serialized = first.model_dump_json(by_alias=True, exclude_none=True)
    assert "DOWNLOAD_BODY_SECRET" not in serialized
    assert "href-user" not in serialized
    assert "href-password" not in serialized
    assert "access_token" not in serialized
    assert "query-secret" not in serialized
    assert "fragment-secret" not in serialized


@pytest.mark.parametrize(
    ("case", "part_index", "status", "parts", "expected_code"),
    (
        (
            "invalid-index",
            7,
            "COMPLETE",
            [_file_part()],
            "INVALID_PART_INDEX",
        ),
        (
            "text-part",
            0,
            "COMPLETE",
            [{"type": "text", "name": "stdout", "value": "not a file"}],
            "RESULT_PART_NOT_FILE",
        ),
        (
            "nonterminal-command",
            0,
            "ONGOING",
            [_file_part()],
            "COMMAND_NOT_COMPLETE",
        ),
    ),
)
async def test_command_file_download_rejects_invalid_source_without_writing(
    fake_c2,
    tmp_path: Path,
    case: str,
    part_index: int,
    status: str,
    parts: list[dict],
    expected_code: str,
) -> None:
    download_dir = tmp_path / case
    cfg = _settings(download_dir=download_dir)
    registry, invocation = _invocation(
        fake_c2,
        cfg,
        _command(parts, status=status),
    )
    fake_c2.responses["/download/result.bin?access_token=query-secret"] = b"must not fetch"

    result = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": part_index},
    )

    assert result.isError is True
    error = _payload(result)
    assert error["code"] == expected_code
    assert error["retryable"] is (case == "nonterminal-command")
    assert not download_dir.exists() or list(download_dir.iterdir()) == []
    assert not any(
        call[0] == "request_get" and call[1][2] is True for call in fake_c2.calls
    )


async def test_command_file_download_requires_configured_destination(
    fake_c2,
    tmp_path: Path,
) -> None:
    cfg = _settings(download_dir=None)
    registry, invocation = _invocation(fake_c2, cfg, _command([_file_part()]))
    fake_c2.responses["/download/result.bin?access_token=query-secret"] = b"must not fetch"

    result = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": 0},
    )

    assert result.isError is True
    assert _payload(result)["code"] == "DOWNLOAD_DIRECTORY_NOT_CONFIGURED"
    assert list(tmp_path.iterdir()) == []
    assert not any(
        call[0] == "request_get" and call[1][2] is True for call in fake_c2.calls
    )


async def test_command_file_download_rejects_oversize_body_without_partial_file(
    fake_c2,
    tmp_path: Path,
) -> None:
    download_dir = tmp_path / "downloads"
    cfg = _settings(download_dir=download_dir, download_max_bytes=4)
    registry, invocation = _invocation(fake_c2, cfg, _command([_file_part()]))
    fake_c2.responses["/download/result.bin?access_token=query-secret"] = b"12345"

    result = await _call(
        registry,
        invocation,
        "tuoni_command_file_download",
        {"command_id": 42, "part_index": 0},
    )

    assert result.isError is True
    error = _payload(result)
    assert error["code"] == "FILE_TOO_LARGE"
    assert error["retryable"] is False
    assert not download_dir.exists() or list(download_dir.iterdir()) == []


async def test_file_manifest_marks_non_resource_and_offers_download_only_with_artifacts(
    fake_c2,
    tmp_path: Path,
) -> None:
    command = _command([_file_part(filename="report.txt")])

    without_cfg = _settings(download_dir=None, artifacts=False)
    without_registry, without_invocation = _invocation(fake_c2, without_cfg, command)
    without = await _call(
        without_registry,
        without_invocation,
        "tuoni_command_get",
        {"command_id": 42},
    )
    without_payload = _payload(without)

    assert without.isError is False
    assert without_payload["data"]["result"]["parts"][0]["not_mcp_resource"] is True
    assert without_payload["next"] is None
    assert "tuoni_command_file_download" not in without.model_dump_json(
        by_alias=True,
        exclude_none=True,
    )
    without_selected = {
        spec.name
        for spec in without_registry.select(without_cfg.profiles, read_only=False)
    }
    assert "tuoni_command_file_download" not in without_selected

    with_cfg = _settings(download_dir=tmp_path / "downloads", artifacts=True)
    with_registry, with_invocation = _invocation(fake_c2, with_cfg, command)
    with_artifacts = await _call(
        with_registry,
        with_invocation,
        "tuoni_command_get",
        {"command_id": 42},
    )
    with_payload = _payload(with_artifacts)

    assert with_artifacts.isError is False
    assert with_payload["data"]["result"]["parts"][0]["not_mcp_resource"] is True
    assert with_payload["next"] == {
        "name": "tuoni_command_file_download",
        "arguments": {"command_id": 42, "part_index": 0},
    }
    with_selected = {
        spec.name for spec in with_registry.select(with_cfg.profiles, read_only=False)
    }
    assert "tuoni_command_file_download" in with_selected
