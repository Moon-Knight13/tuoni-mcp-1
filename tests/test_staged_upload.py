from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
import time

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, ERROR_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, ToolFailure, build_dispatch_specs
from tuoni_mcp.staging import load_upload, prepare_upload, prepared_upload
from tuoni_mcp.tools import build_registry


def _settings(root: Path | None = None, *, limit=4096, mode=MCPMode.NATIVE):
    return Settings(
        url="https://example.test", username="test", password="test", verify=True,
        upload_dir=str(root) if root is not None else None, upload_max_bytes=limit,
        profiles=frozenset({Profile.ARTIFACTS, Profile.AGENT_OPS}), mode=mode,
    )


def test_local_upload_requires_explicit_staging_directory():
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(), "tuoni_file_upload", None, "report.txt")
    assert error.value.code == "UPLOAD_DIRECTORY_NOT_CONFIGURED"


def test_binary_content_is_read_without_model_facing_base64(tmp_path):
    source = tmp_path / "artifact.bin"
    expected = bytes(range(256)) + "日本語 🚀".encode()
    source.write_bytes(expected)
    cfg = _settings(tmp_path)

    result = load_upload(cfg, "tuoni_file_upload", None, source.name)

    assert result.data == expected
    assert result.filename == source.name
    assert result.sha256 == hashlib.sha256(expected).hexdigest()
    assert load_upload(cfg, "tuoni_file_upload", None, str(source)).data == expected


@pytest.mark.parametrize("arguments", [{}, {"local_path": "a", "content_base64": ""}])
def test_upload_requires_exactly_one_source(tmp_path, arguments):
    with pytest.raises(ToolFailure) as error:
        prepare_upload(_settings(tmp_path), "tuoni_file_upload", arguments)
    assert error.value.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("local_path", ["../outside", "sub/../file", "sub/./file", "..\\outside", ""])
def test_traversal_is_rejected_before_opening_a_file(tmp_path, local_path):
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(tmp_path), "tuoni_file_upload", None, local_path)
    assert error.value.code == "INVALID_UPLOAD_PATH"


def test_outside_absolute_path_and_directories_are_not_uploadable(tmp_path):
    root = tmp_path / "stage"
    root.mkdir()
    outside = tmp_path / "private"
    outside.write_bytes(b"private content must not be read")
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(root), "tuoni_file_upload", None, str(outside))
    assert error.value.code == "INVALID_UPLOAD_PATH"
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(root), "tuoni_file_upload", None, str(root))
    assert error.value.code in {"INVALID_UPLOAD_PATH", "UNSAFE_UPLOAD_FILE"}
    assert "private content" not in str(error.value)


@pytest.mark.parametrize("directory_link", [False, True])
@pytest.mark.parametrize("portable_fallback", [False, True])
def test_symlink_files_and_parents_are_rejected(tmp_path, directory_link, portable_fallback, monkeypatch):
    if portable_fallback:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    root = tmp_path / "stage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"outside")
    link = root / "link"
    try:
        link.symlink_to(outside if directory_link else outside / "file", target_is_directory=directory_link)
    except OSError:
        pytest.skip("Creating symlinks requires platform support or Windows developer mode")
    path = "link/file" if directory_link else "link"
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(root), "tuoni_file_upload", None, path)
    assert error.value.code == "UNSAFE_UPLOAD_FILE"


def test_portable_file_open_preserves_regular_file_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    (tmp_path / "file").write_bytes(b"staged")

    actual = load_upload(_settings(tmp_path), "tuoni_file_upload", None, "file")

    assert actual.data == b"staged"


def test_path_and_handle_ctime_differences_do_not_reject_an_unchanged_file(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_bytes(b"staged")
    cfg = _settings(tmp_path)
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == source:
            fields = {name: getattr(info, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
            )}
            fields["st_ctime_ns"] -= 1_000_000
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)

    assert load_upload(cfg, "tuoni_file_upload", None, source.name).data == b"staged"


def test_windows_reparse_attributes_are_rejected_without_opening_file(tmp_path, monkeypatch):
    source = tmp_path / "reparse"
    source.write_bytes(b"must not be read")
    cfg = _settings(tmp_path)
    original = Path.lstat
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)

    def lstat(path):
        if path == source:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(ToolFailure) as error:
        load_upload(cfg, "tuoni_file_upload", None, source.name)
    assert error.value.code == "UNSAFE_UPLOAD_FILE"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
def test_non_regular_file_is_rejected_without_opening_it(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(tmp_path), "tuoni_file_upload", None, "pipe")
    assert error.value.code == "UNSAFE_UPLOAD_FILE"


def test_staged_and_inline_limits_are_enforced_before_upload(tmp_path):
    file = tmp_path / "large"
    file.write_bytes(b"12345")
    cfg = _settings(tmp_path, limit=4)
    for inline, path in ((None, "large"), (base64.b64encode(b"12345").decode(), None)):
        with pytest.raises(ToolFailure) as error:
            load_upload(cfg, "tuoni_file_upload", inline, path)
        assert error.value.code == "UPLOAD_TOO_LARGE"
    assert load_upload(cfg, "tuoni_file_upload", base64.b64encode(b"1234").decode(), None).data == b"1234"


def test_encoded_input_cap_is_checked_before_base64_decode(tmp_path, monkeypatch):
    from tuoni_mcp import staging

    def forbidden(*_):
        pytest.fail("Oversized base64 must be rejected before decoding")

    monkeypatch.setattr(staging.base64, "b64decode", forbidden)
    with pytest.raises(ToolFailure) as error:
        load_upload(_settings(tmp_path, limit=4), "tuoni_file_upload", "A" * 12, None)
    assert error.value.code == "UPLOAD_TOO_LARGE"


def test_inline_upload_preserves_empty_and_mime_wrapped_inputs():
    cfg = _settings()
    assert load_upload(cfg, "tuoni_file_upload", "", None).data == b""
    assert load_upload(cfg, "tuoni_file_upload", " YW\r\nJj\t", None).data == b"abc"


async def test_prepared_upload_rejects_changed_or_removed_files(tmp_path):
    file = tmp_path / "artifact.bin"
    file.write_bytes(b"first")
    cfg = _settings(tmp_path)
    arguments = {"local_path": file.name}
    prepared = prepare_upload(cfg, "tuoni_file_upload", arguments)
    assert prepared["size_bytes"] == 5
    assert "data" not in prepared
    file.write_bytes(b"second")
    with pytest.raises(ToolFailure) as error:
        async with prepared_upload(cfg, "tuoni_file_upload", arguments, prepared):
            pytest.fail("Changed files must not enter the mutation boundary")
    assert error.value.code == "PREPARATION_DRIFT"
    file.unlink()
    with pytest.raises(ToolFailure) as error:
        async with prepared_upload(cfg, "tuoni_file_upload", arguments, prepared):
            pytest.fail("Missing files must not enter the mutation boundary")
    assert error.value.code == "PREPARATION_DRIFT"


async def test_execute_uses_verified_snapshot_and_scoped_binding_is_cleared(tmp_path):
    file = tmp_path / "artifact.bin"
    file.write_bytes(b"prepared")
    cfg = _settings(tmp_path)
    arguments = {"local_path": file.name}
    prepared = prepare_upload(cfg, "tuoni_file_upload", arguments)

    async with prepared_upload(cfg, "tuoni_file_upload", arguments, prepared):
        file.write_bytes(b"changed after preflight")
        assert load_upload(cfg, "tuoni_file_upload", None, file.name).data == b"prepared"
    assert load_upload(cfg, "tuoni_file_upload", None, file.name).data == b"changed after preflight"


async def test_concurrent_prepared_uploads_do_not_share_snapshot_bindings(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root, value in zip(roots, (b"first", b"second")):
        root.mkdir()
        (root / "same-name.bin").write_bytes(value)
    ready = [asyncio.Event(), asyncio.Event()]

    async def run(index):
        cfg = _settings(roots[index])
        args = {"local_path": "same-name.bin"}
        expected = prepare_upload(cfg, "tuoni_file_upload", args)
        async with prepared_upload(cfg, "tuoni_file_upload", args, expected):
            ready[index].set()
            await ready[1 - index].wait()
            (roots[index] / "same-name.bin").write_bytes(b"changed")
            return load_upload(cfg, "tuoni_file_upload", None, "same-name.bin").data

    assert await asyncio.gather(run(0), run(1)) == [b"first", b"second"]


async def test_slow_staged_read_times_out_before_entering_mutation_scope(tmp_path, monkeypatch):
    from tuoni_mcp import staging

    source = tmp_path / "file"
    source.write_bytes(b"prepared")
    cfg = replace(_settings(tmp_path), request_timeout=0.03)
    arguments = {"local_path": source.name}
    expected = prepare_upload(cfg, "tuoni_file_upload", arguments)
    release = threading.Event()
    original = staging._staged_content

    def blocked(settings, path):
        release.wait(timeout=2)
        return original(settings, path)

    monkeypatch.setattr(staging, "_staged_content", blocked)
    try:
        with pytest.raises(TimeoutError):
            async with prepared_upload(cfg, "tuoni_file_upload", arguments, expected):
                pytest.fail("A timed-out staging read must not enter the mutation scope")
    finally:
        release.set()
        monkeypatch.setattr(staging, "_staged_content", original)
    source.write_bytes(b"changed")
    assert load_upload(cfg, "tuoni_file_upload", None, source.name).data == b"changed"


def _harness(fake_c2, cfg):
    registry = build_registry(cfg)
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(cfg.profiles, read_only=False)
    specs = {spec.name: spec for spec in (selected if cfg.mode is MCPMode.NATIVE else
             build_dispatch_specs(registry, selected, read_only=False))}

    async def call(name, arguments):
        if cfg.mode is MCPMode.DISPATCH and name != "tuoni_operation_execute":
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        assert call_result_size(result) <= (ERROR_RESULT_BUDGET if result.isError else DETAIL_RESULT_BUDGET)
        return result, json.loads(result.content[0].text)

    return call


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_staged_upload_roundtrip_keeps_content_out_of_tool_arguments(tmp_path, fake_c2, mode):
    file = tmp_path / "report.bin"
    expected = bytes(range(256))
    file.write_bytes(expected)

    def upload(path, data, filename):
        fake_c2._record("add_hosted", path, data, filename)
        return {"fileId": "created-file"}

    fake_c2.add_hosted = upload
    call = _harness(fake_c2, _settings(tmp_path, mode=mode))
    result, payload = await call("tuoni_file_upload", {"hosted_path": "/reports/report.bin", "local_path": file.name})
    assert not result.isError
    if mode is MCPMode.DISPATCH:
        assert fake_c2.calls == []
        assert payload["data"]["summary"]["arguments"]["local_path"] == file.name
        result, payload = await call("tuoni_operation_execute", payload["next"]["arguments"])
        assert not result.isError
        payload = {"data": payload["data"]["result"]}
    assert payload["data"]["size"] == len(expected)
    assert payload["data"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert fake_c2.calls == [("add_hosted", ("/reports/report.bin", expected, file.name), {})]


async def test_dispatch_detects_staged_file_drift_before_remote_mutation(tmp_path, fake_c2):
    file = tmp_path / "report.bin"
    file.write_bytes(b"first")
    call = _harness(fake_c2, _settings(tmp_path, mode=MCPMode.DISPATCH))
    result, prepared = await call("tuoni_file_upload", {"hosted_path": "/report.bin", "local_path": file.name})
    assert not result.isError
    file.write_bytes(b"second")

    result, error = await call("tuoni_operation_execute", prepared["next"]["arguments"])

    assert result.isError
    assert error["code"] == "PREPARATION_DRIFT"
    assert fake_c2.calls == []


async def test_dispatch_handler_uploads_verified_bytes_even_if_file_changes_during_authentication(
    tmp_path, fake_c2, monkeypatch,
):
    from tuoni_mcp.tools import commands

    file = tmp_path / "report.bin"
    file.write_bytes(b"prepared bytes")

    def upload(path, data, filename):
        fake_c2._record("add_hosted", path, data, filename)
        return {"fileId": "created-file"}

    fake_c2.add_hosted = upload
    call = _harness(fake_c2, _settings(tmp_path, mode=MCPMode.DISPATCH))
    result, prepared = await call("tuoni_file_upload", {"hosted_path": "/report.bin", "local_path": file.name})
    assert not result.isError

    async def authenticate(_):
        file.write_bytes(b"changed during authentication")

    monkeypatch.setattr(commands, "ensure_fresh_token", authenticate)
    result, payload = await call("tuoni_operation_execute", prepared["next"]["arguments"])

    assert not result.isError
    assert fake_c2.calls == [("add_hosted", ("/report.bin", b"prepared bytes", file.name), {})]
    assert payload["data"]["result"]["sha256"] == hashlib.sha256(b"prepared bytes").hexdigest()
    result, replay = await call("tuoni_operation_execute", prepared["next"]["arguments"])
    assert not result.isError
    assert replay == payload
    assert len(fake_c2.calls) == 1


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_bad_upload_sources_and_oversized_inputs_do_not_reach_c2(tmp_path, fake_c2, mode):
    call = _harness(fake_c2, _settings(tmp_path, limit=4, mode=mode))
    for arguments, expected in [
        ({}, "INVALID_ARGUMENTS"),
        ({"local_path": "file", "content_base64": ""}, "INVALID_ARGUMENTS"),
        ({"content_base64": "MTIzNDU="}, "UPLOAD_TOO_LARGE"),
    ]:
        result, error = await call("tuoni_file_upload", {"hosted_path": "/file", **arguments})
        assert result.isError
        assert error["code"] == expected
    assert fake_c2.calls == []


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_replace_uses_staged_content_and_retains_hosted_filename(tmp_path, fake_c2, mode):
    file = tmp_path / "replacement.bin"
    file.write_bytes(b"replacement")
    fake_c2.responses["/api/v1/file/file-id"] = {"originalFileName": "hosted.bin"}

    def replace_file(path, data):
        fake_c2._record("request_put_multipart", path, data)

    fake_c2.request_put_multipart = replace_file
    call = _harness(fake_c2, _settings(tmp_path, mode=mode))
    result, payload = await call("tuoni_file_replace", {"file_id": "file-id", "local_path": file.name})
    assert not result.isError
    if mode is MCPMode.DISPATCH:
        result, payload = await call("tuoni_operation_execute", payload["next"]["arguments"])
        assert not result.isError
    assert fake_c2.calls[-1] == (
        "request_put_multipart", ("/api/v1/file/file-id", {"content": ["hosted.bin", b"replacement"]}), {},
    )
