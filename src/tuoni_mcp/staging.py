"""Bounded artifact inputs from one explicitly configured staging directory."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import stat
from typing import Any

from tuoni_mcp.config import Settings
from tuoni_mcp.connection import to_thread
from tuoni_mcp.registry import ToolFailure


_UPLOAD_TOOLS = frozenset({"tuoni_file_upload", "tuoni_file_replace"})


@dataclass(frozen=True)
class UploadContent:
    data: bytes = field(repr=False)
    filename: str | None
    sha256: str
    fingerprint: str = field(repr=False)


@dataclass(frozen=True)
class _BoundUpload:
    tool_name: str
    local_path: str
    upload_dir: str
    content: UploadContent


_prepared: ContextVar[_BoundUpload | None] = ContextVar("tuoni_prepared_upload", default=None)


def _too_large() -> ToolFailure:
    return ToolFailure(
        "UPLOAD_TOO_LARGE", "Upload exceeds the configured byte limit.",
        hint="Stage a smaller file or increase TUONI_MCP_UPLOAD_MAX_BYTES.",
    )


def _source_arguments(arguments: Mapping[str, Any]) -> tuple[str | None, str | None]:
    inline, local = arguments.get("content_base64"), arguments.get("local_path")
    if (inline is None) == (local is None):
        raise ToolFailure(
            "INVALID_ARGUMENTS", "Provide exactly one of content_base64 or local_path.",
            hint="local_path names a file under TUONI_MCP_UPLOAD_DIR on the MCP server.",
        )
    if inline is not None and not isinstance(inline, str):
        raise ToolFailure("INVALID_ARGUMENTS", "content_base64 must be a string.")
    if local is not None and not isinstance(local, str):
        raise ToolFailure("INVALID_ARGUMENTS", "local_path must be a string.")
    return inline, local


def _inline_content(value: str, maximum: int) -> UploadContent:
    encoded_limit = 4 * ((maximum + 2) // 3)
    # Allow MIME whitespace without allocating an unbounded normalized copy.
    if len(value) > encoded_limit * 2 + 1024:
        raise _too_large()
    normalized = "".join(value.split())
    if len(normalized) > encoded_limit:
        raise _too_large()
    try:
        data = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("'content_base64' is not valid base64") from exc
    if len(data) > maximum:
        raise _too_large()
    digest = hashlib.sha256(data).hexdigest()
    return UploadContent(data, None, digest, digest)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Windows path-based stat and fstat can expose different ctime semantics.
    # Compare the same portable fields across those two APIs; use handle-based
    # revisions below to detect metadata changes while the file is read.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_ctime_ns)


def _relative_path(root: Path, value: str) -> Path:
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ToolFailure("INVALID_UPLOAD_PATH", "local_path is not a valid staged-file path.")
    if any(part in {".", ".."} for part in value.replace("\\", "/").split("/")):
        raise ToolFailure("INVALID_UPLOAD_PATH", "Staged-file paths must not contain dot traversal.")
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise ToolFailure("INVALID_UPLOAD_PATH", "local_path is outside the configured staging directory.") from exc
    if not path.parts or path.anchor:
        raise ToolFailure("INVALID_UPLOAD_PATH", "local_path must name a staged file.")
    return path


def _check_path(root: Path, relative: Path) -> tuple[os.stat_result, list[tuple[Path, os.stat_result]]]:
    parents = []
    current = root
    root_info = current.lstat()
    if _is_link(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ToolFailure("UNSAFE_UPLOAD_FILE", "The staging directory is no longer a regular directory.")
    parents.append((current, root_info))
    for index, part in enumerate(relative.parts):
        current = current / part
        info = current.lstat()
        if _is_link(info):
            raise ToolFailure("UNSAFE_UPLOAD_FILE", "Staged-file symlinks and reparse points are not allowed.")
        if index < len(relative.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ToolFailure("UNSAFE_UPLOAD_FILE", "A staged-file parent is not a directory.")
            parents.append((current, info))
        elif not stat.S_ISREG(info.st_mode):
            raise ToolFailure("UNSAFE_UPLOAD_FILE", "Only regular staged files can be uploaded.")
    return info, parents


def _open_file(root: Path, relative: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow and os.open in os.supports_dir_fd:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
        directory = os.open(root, directory_flags)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
            return os.open(relative.parts[-1], flags | nofollow, dir_fd=directory)
        finally:
            os.close(directory)
    # Windows lacks dir_fd/O_NOFOLLOW. Pre/post lstat and file identity checks
    # below also reject reparse points and a path swapped during the read.
    return os.open(root / relative, flags | nofollow)


def _staged_content(cfg: Settings, value: str) -> UploadContent:
    if cfg.upload_dir is None:
        raise ToolFailure(
            "UPLOAD_DIRECTORY_NOT_CONFIGURED", "Local artifact uploads are not configured.",
            hint="Set TUONI_MCP_UPLOAD_DIR to an existing absolute staging directory on the MCP server.",
        )
    root = Path(cfg.upload_dir)
    relative = _relative_path(root, value)
    try:
        before, parents = _check_path(root, relative)
        if before.st_size > cfg.upload_max_bytes:
            raise _too_large()
        descriptor = _open_file(root, relative)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                raise ToolFailure("UPLOAD_FILE_CHANGED", "The staged file changed while it was opened.")
            data = stream.read(cfg.upload_max_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(data) > cfg.upload_max_bytes:
            raise _too_large()
        current, current_parents = _check_path(root, relative)
        if (_revision(opened) != _revision(after) or _identity(current) != _identity(after)
                or len(data) != after.st_size or len(parents) != len(current_parents)
                or any(a.st_dev != b.st_dev or a.st_ino != b.st_ino
                       for (_, a), (_, b) in zip(parents, current_parents))):
            raise ToolFailure("UPLOAD_FILE_CHANGED", "The staged file changed while it was read.")
    except FileNotFoundError as exc:
        raise ToolFailure("UPLOAD_FILE_NOT_FOUND", "The staged file or its directory does not exist.") from exc
    except OSError as exc:
        raise ToolFailure("UNSAFE_UPLOAD_FILE", "The staged file could not be opened safely.") from exc
    digest = hashlib.sha256(data).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(
        [str(relative), _revision(after), digest], separators=(",", ":"),
    ).encode()).hexdigest()
    return UploadContent(data, relative.name, digest, fingerprint)


def load_upload(
    cfg: Settings, tool_name: str, content_base64: str | None, local_path: str | None,
) -> UploadContent:
    inline, local = _source_arguments({"content_base64": content_base64, "local_path": local_path})
    if local is None:
        assert inline is not None
        return _inline_content(inline, cfg.upload_max_bytes)
    bound = _prepared.get()
    if (bound is not None and bound.tool_name == tool_name and bound.local_path == local
            and bound.upload_dir == cfg.upload_dir):
        return bound.content
    return _staged_content(cfg, local)


def prepare_upload(cfg: Settings, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    if tool_name not in _UPLOAD_TOOLS:
        return None
    inline, local = _source_arguments(arguments)
    if local is None:
        assert inline is not None
        _inline_content(inline, cfg.upload_max_bytes)
        return None
    content = _staged_content(cfg, local)
    return {"fingerprint": content.fingerprint, "filename": content.filename,
            "size_bytes": len(content.data), "sha256": content.sha256}


@asynccontextmanager
async def prepared_upload(
    cfg: Settings, tool_name: str, arguments: Mapping[str, Any], expected: Mapping[str, Any] | None,
) -> AsyncIterator[None]:
    if tool_name not in _UPLOAD_TOOLS or expected is None:
        yield
        return
    _, local = _source_arguments(arguments)
    if local is None:
        raise ToolFailure("PREPARATION_DRIFT", "Prepared upload no longer names a staged file.")
    try:
        content = await to_thread(_staged_content, cfg, local, _timeout=cfg.request_timeout)
    except ToolFailure as exc:
        raise ToolFailure(
            "PREPARATION_DRIFT", "The staged file is no longer available as prepared.",
            hint="Review the staged file and create a new preparation.",
        ) from exc
    if content.fingerprint != expected.get("fingerprint"):
        raise ToolFailure(
            "PREPARATION_DRIFT", "The staged file changed after preparation.",
            hint="Review the staged file and create a new preparation.",
        )
    token = _prepared.set(_BoundUpload(tool_name, local, cfg.upload_dir, content))
    try:
        yield
    finally:
        _prepared.reset(token)
