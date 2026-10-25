"""Lossless JSON windows for optional-profile details that exceed one response."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any
from urllib.parse import unquote

from pydantic import Field

from tuoni_mcp.pagination import CursorError, call_result_size, decode_cursor, encode_cursor, fit_text_window, success_payload
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, compact_json, redact, redact_text, should_redact_key
from tuoni_mcp.registry import ToolFailure, ToolSuccess


DetailCursor = Annotated[str | None, Field(strict=True, max_length=2048)]
DetailWindow = Annotated[int, Field(strict=True, ge=1, le=16_384)]


def exact_catalog_id(value: Any, *, max_chars: int = 512) -> str:
    """Validate opaque upstream identifiers without altering callable values."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise ToolFailure("INVALID_UPSTREAM_ID", "A catalog record has an invalid identifier.",
                          hint="Check the backend catalog metadata before retrying.")
    if redact_text(value) != value:
        raise ToolFailure("INVALID_UPSTREAM_ID", "A catalog identifier contains secret-like text.",
                          hint="Check the backend catalog metadata before retrying.")
    return value


def redact_schema(value: Any) -> Any:
    """Keep property names/types; callers must flag removed secret literals/constraints."""
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    literals = {"default", "examples", "const", "enum"}
    references = {"$ref", "$dynamicRef", "$recursiveRef"}
    sensitive_nodes: set[int] = set()
    visited: set[tuple[int, bool]] = set()
    anchors: dict[str, list[Any]] = {}
    indexed: set[int] = set()
    nested_resources = False
    unresolved_sensitive_ref = False

    def index(node: Any) -> None:
        nonlocal nested_resources
        if not isinstance(node, (dict, list)) or id(node) in indexed:
            return
        indexed.add(id(node))
        if isinstance(node, list):
            for child in node:
                index(child)
            return
        if node is not value and "$id" in node:
            nested_resources = True
        for key in ("$anchor", "$dynamicAnchor"):
            if isinstance(node.get(key), str):
                anchors.setdefault(node[key], []).append(node)
        for key, child in node.items():
            if key in maps and isinstance(child, dict):
                for schema in child.values():
                    index(schema)
            elif key not in literals:
                index(child)

    index(value)

    def referenced(ref: str) -> list[Any]:
        # Never fetch references. Nested $id resources need a full resolver;
        # conservatively omit literals rather than resolve against a wrong root.
        if not ref.startswith("#") or nested_resources:
            return []
        fragment = unquote(ref[1:])
        if not fragment:
            return [value]
        if not fragment.startswith("/"):
            # All matching dynamic anchors are possible targets. Treat duplicate
            # anchors conservatively instead of choosing a possibly wrong one.
            return anchors.get(fragment, [])
        node = value
        for part in fragment[1:].split("/"):
            if re.search(r"~(?![01])", part):
                return []
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and key in node:
                node = node[key]
            elif isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                if len(key) > len(str(len(node))) or int(key) >= len(node):
                    return []
                node = node[int(key)]
            else:
                return []
        return [node] if isinstance(node, (dict, list, bool)) else []

    def mark(node: Any, sensitive: bool = False) -> None:
        nonlocal unresolved_sensitive_ref
        if not isinstance(node, (dict, list)) or (id(node), sensitive) in visited:
            return
        visited.add((id(node), sensitive))
        if sensitive:
            sensitive_nodes.add(id(node))
        if isinstance(node, list):
            for child in node:
                mark(child, sensitive)
            return
        if sensitive:
            for key in references:
                if isinstance(node.get(key), str):
                    targets = referenced(node[key])
                    if not targets:
                        unresolved_sensitive_ref = True
                    for target in targets:
                        mark(target, True)
        for key, child in node.items():
            if key in maps and isinstance(child, dict):
                for name, schema in child.items():
                    mark(schema, sensitive or should_redact_key(name, ""))
            else:
                mark(child, sensitive)

    # Shared definitions must be redacted even if reached first through an
    # innocuous property; follow sensitive local references with cycle protection.
    mark(value)
    if unresolved_sensitive_ref:
        mark(value, True)

    def render(node: Any) -> Any:
        if isinstance(node, dict):
            output = {}
            for key, child in node.items():
                if key in maps and isinstance(child, dict):
                    # These keys name properties/definitions, even when named
                    # "default" or "enum"; never interpret them as annotations.
                    output[key] = {name: render(schema) for name, schema in child.items()}
                elif key == "required" and isinstance(child, list) and all(isinstance(name, str) for name in child):
                    output[key] = list(child)
                elif key == "dependentRequired" and isinstance(child, dict):
                    output[key] = {name: list(required) if isinstance(required, list) and
                                   all(isinstance(item, str) for item in required) else render(required)
                                   for name, required in child.items()}
                elif key in literals:
                    if id(node) not in sensitive_nodes:
                        output[key] = redact(child)
                else:
                    output[key] = render(child)
            return output
        if isinstance(node, list):
            return [render(child) for child in node]
        return redact_text(node) if isinstance(node, str) else node

    return render(value)


def json_detail(
    value: Any,
    *,
    tool_name: str,
    identity: dict[str, Any],
    cursor: str | None,
    max_chars: int,
    already_redacted: bool = False,
) -> ToolSuccess:
    """Return a structured detail or reconstructable windows of its redacted JSON.

    Each continuation re-reads upstream. A changed record fails explicitly instead
    of concatenating windows from different versions; it is not a cached snapshot.
    """
    safe = value if already_redacted else redact(value)
    # This value may contain preserved schema properties with secret-like names.
    # Measure the exact envelope: would_fit_success would redact those a second
    # time and wrongly classify an oversized schema as a small structured detail.
    if cursor is None and call_result_size(success_payload(safe)) <= DETAIL_RESULT_BUDGET:
        return ToolSuccess(safe, redact_output=False)
    serialized = compact_json(safe)
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    scope = hashlib.sha256(compact_json(identity).encode("utf-8")).hexdigest()
    start = 0
    if cursor is not None:
        try:
            decoded = decode_cursor(cursor, tool=tool_name)
            # max_chars bounds this response; it does not identify the record.
            if decoded.get("scope") != scope:
                raise CursorError("CURSOR_SCOPE_CHANGED", "The detail identity changed while continuing a cursor.")
            start = decoded["offset"]
            if type(start) is not int or start < 0:
                raise ValueError("invalid detail offset")
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolFailure(getattr(exc, "code", "INVALID_CURSOR"),
                              str(exc) if isinstance(exc, CursorError) else "The detail cursor is invalid for these arguments.",
                              hint="Follow next exactly, or restart without cursor.") from exc
        if decoded.get("fingerprint") != fingerprint:
            raise ToolFailure(
                "DETAIL_CHANGED", "The record changed while its detail was being read.",
                retryable=False, hint="Restart without cursor; discard the previous JSON windows.",
                next_call={"name": tool_name, "arguments": {**identity, "max_chars": max_chars}},
            )

    def continuation(offset: int) -> tuple[str, dict[str, Any]]:
        following = encode_cursor({"tool": tool_name, "scope": scope, "fingerprint": fingerprint,
                                   "offset": offset})
        return following, {"name": tool_name, "arguments": {**identity, "cursor": following, "max_chars": max_chars}}

    def make_payload(window: str, end: int, has_more: bool) -> dict[str, Any]:
        following, next_call = continuation(end) if has_more else (None, None)
        return success_payload(
            {**identity, "value_format": "json", "value_json": window, "offset_chars": start,
             "returned_chars": end - start, "total_chars": len(serialized), "has_more": has_more},
            next_cursor=following, next_call=next_call,
        )

    try:
        _, end, has_more, payload = fit_text_window(
            serialized, offset_chars=start, requested_chars=max_chars,
            budget=DETAIL_RESULT_BUDGET, make_payload=make_payload,
        )
    except ValueError as exc:
        raise ToolFailure("INVALID_CURSOR", "The detail cursor is outside this record.",
                          hint="Restart without cursor.") from exc
    following, next_call = continuation(end) if has_more else (None, None)
    return ToolSuccess(payload["data"], next_cursor=following, next_call=next_call, redact_output=False)


def schema_detail(value: dict[str, Any], **window: Any) -> ToolSuccess:
    """Preserve schema structure and disclose any privacy-related omissions."""
    safe = redact(value)
    schema = value.get("configuration_schema")
    safe["configuration_schema"] = redact_schema(schema)
    safe["configuration_schema_redacted"] = safe["configuration_schema"] != schema
    if safe["configuration_schema_redacted"]:
        safe["configuration_schema_notice"] = (
            "Secret literals were removed; this schema may omit constraints. "
            "Do not treat it as complete validation."
        )
    return json_detail(safe, already_redacted=True, **window)
