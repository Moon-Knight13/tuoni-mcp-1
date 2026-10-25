from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from mcp.types import CallToolResult, TextContent

from tuoni_mcp.protocol import exposed_call, redact_text, should_redact_key


# Byte budgets live in tuoni_mcp.protocol. A second copy here drifted silently
# and was never read; callers pass their budget in explicitly.
_DYNAMIC_VALUE_KEYS = (
    "value",
    "currentValue",
    "current_value",
    "defaultValue",
    "default_value",
    "newValue",
    "new_value",
    "oldValue",
    "old_value",
)

def compact_json(value: Any) -> str:
    """Serialize model-facing JSON deterministically and without ASCII expansion."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def call_result_size(value: Any, *, is_error: bool = False) -> int:
    """Measure the complete serialized MCP CallToolResult, not only its inner JSON."""
    result = CallToolResult(
        content=[TextContent(type="text", text=compact_json(value))],
        isError=is_error,
    )
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def truncate_utf8(value: Any, max_bytes: int) -> tuple[str, bool, int]:
    """Bound a string by UTF-8 bytes without splitting a Unicode code point."""
    text = str(value or "")
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return text, False, original_bytes

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], True, original_bytes


def bounded_text(value: Any, max_bytes: int) -> str:
    text, truncated, _ = truncate_utf8(value, max_bytes)
    if not truncated or max_bytes < 3:
        return text
    while text and len((text + "…").encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text + "…"


def redact(value: Any, *, max_depth: int = 4, max_items: int = 32, max_string_bytes: int = 512) -> Any:
    """Recursively redact secrets and structurally bound arbitrary server metadata."""

    def walk(item: Any, depth: int, key: str | None = None) -> Any:
        if key is not None and should_redact_key(key, item):
            return "[REDACTED]"
        if depth >= max_depth:
            if isinstance(item, Mapping):
                return {"truncated": True, "kind": "object", "item_count": len(item)}
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                return {"truncated": True, "kind": "array", "item_count": len(item)}
        if isinstance(item, Mapping):
            out: dict[str, Any] = {}
            entries = list(item.items())
            dynamic_probe = next(
                (
                    item.get(value_key)
                    for value_key in _DYNAMIC_VALUE_KEYS
                    if value_key in item
                ),
                None,
            )
            dynamic_sensitive = any(
                isinstance(item.get(label), str)
                and should_redact_key(str(item[label]), dynamic_probe)
                for label in ("key", "name", "field")
            )
            for raw_key, child in entries[:max_items]:
                if item.get("type") == "binary" and raw_key == "result":
                    out["encoded_chars"] = len(child) if isinstance(child, str) else 0
                    out["result"] = "[REDACTED]"
                    continue
                child_key = bounded_text(raw_key, 96)
                out[child_key] = walk(child, depth + 1, str(raw_key))
            if len(entries) > max_items:
                out["_truncated_items"] = len(entries) - max_items
            if dynamic_sensitive:
                for value_key in _DYNAMIC_VALUE_KEYS:
                    if value_key in out:
                        out[value_key] = "[REDACTED]"
            return out
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            out = [walk(child, depth + 1) for child in item[:max_items]]
            if len(item) > max_items:
                out.append({"_truncated_items": len(item) - max_items})
            return out
        if isinstance(item, (bytes, bytearray)):
            return {"redacted_binary_bytes": len(item)}
        if isinstance(item, str):
            return bounded_text(redact_text(item), max_string_bytes)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return bounded_text(item, max_string_bytes)

    return walk(value, 0)


class CursorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CursorStore:
    """Bounded, expiring opaque handles owned by one server registry.

    Cursor state never travels through the model. Exact handle lookup detects
    corruption before a saved fingerprint can be mistaken for upstream drift.
    Identical state reuses a handle rather than allocating a fresh entry on
    every repeated page-size check. Handles are not authorization credentials.
    """

    def __init__(self, *, capacity: int = 4096, ttl: float = 3600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1 or ttl <= 0:
            raise ValueError("cursor capacity and lifetime must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._key = secrets.token_bytes(32)
        self._records: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def encode(self, payload: Mapping[str, Any]) -> str:
        raw = compact_json({"v": 1, **payload})
        if len(raw.encode("utf-8")) > 4096:
            raise ValueError("cursor state exceeds the 4096-byte limit")
        token = "c_" + hmac.new(self._key, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        now = self.clock()
        while self._records and next(iter(self._records.values()))[1] <= now:
            self._records.popitem(last=False)
        self._records[token] = (raw, now + self.ttl)
        self._records.move_to_end(token)
        while len(self._records) > self.capacity:
            self._records.popitem(last=False)
        return token

    def decode(self, cursor: str, *, tool: str) -> dict[str, Any]:
        record = self._records.get(cursor) if len(cursor) == 26 else None
        now = self.clock()
        if record is None:
            raise CursorError("INVALID_CURSOR", "cursor is invalid or no longer retained; restart without a cursor")
        if record[1] <= now:
            del self._records[cursor]
            raise CursorError("CURSOR_EXPIRED", "cursor expired; restart without a cursor")
        payload = json.loads(record[0])
        if type(payload.get("v")) is not int or payload["v"] != 1:
            raise CursorError("INVALID_CURSOR", "cursor version is invalid")
        if payload.get("tool") != tool:
            raise CursorError("CURSOR_SCOPE_CHANGED", "cursor does not belong to this tool")
        self._records[cursor] = (record[0], now + self.ttl)
        self._records.move_to_end(cursor)
        return payload


_CURSORS: ContextVar[CursorStore] = ContextVar("tuoni_mcp_cursors", default=CursorStore())


@contextmanager
def cursor_scope(store: CursorStore):
    token = _CURSORS.set(store)
    try:
        yield
    finally:
        _CURSORS.reset(token)


def encode_cursor(payload: Mapping[str, Any]) -> str:
    return _CURSORS.get().encode(payload)


def decode_cursor(cursor: str, *, tool: str) -> dict[str, Any]:
    return _CURSORS.get().decode(cursor, tool=tool)


def page_coverage(*, offset: int, returned: int, total: int | None, has_more: bool) -> dict[str, Any]:
    """Describe this response's coverage, never a client's accumulated reads.

    ``complete`` means this one response contains the full matching inventory.
    Unknown upstream totals stay null, including full numbered backend pages.
    """
    return {"offset": offset, "returned": returned, "total": total,
            "has_more": has_more, "complete": offset == 0 and not has_more}


def success_payload(
    data: Any,
    *,
    next_cursor: str | None = None,
    next_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the success envelope used to size a page before it is returned.

    This must stay byte-identical to what protocol.success_result finally emits,
    including the client-facing continuation name. Measuring the un-prefixed
    name would undercount, and a window sized right at the ceiling would then be
    rejected as RESPONSE_TOO_LARGE after the prefix pushed it over.
    """
    return {
        "ok": True,
        "data": data,
        "next_cursor": next_cursor,
        "next": exposed_call(next_call),
    }


def paginate_items(
    items: Sequence[Any],
    *,
    start: int,
    limit: int,
    budget: int,
    make_data: Callable[[list[Any]], dict[str, Any]],
    make_cursor: Callable[[int], str],
    make_next_call: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Add whole structural items until either the item limit or final MCP byte budget."""
    if start < 0 or start > len(items):
        raise ValueError("cursor offset is outside the current result set")

    selected: list[Any] = []
    stop = min(start + limit, len(items))
    for index in range(start, stop):
        candidate = [*selected, items[index]]
        next_index = index + 1
        cursor = make_cursor(next_index) if next_index < len(items) else None
        next_call = make_next_call(cursor) if cursor is not None else None
        payload = success_payload(make_data(candidate), next_cursor=cursor, next_call=next_call)
        if call_result_size(payload) > budget:
            break
        selected = candidate

    next_index = start + len(selected)
    cursor = make_cursor(next_index) if next_index < len(items) else None
    next_call = make_next_call(cursor) if cursor is not None else None
    payload = success_payload(make_data(selected), next_cursor=cursor, next_call=next_call)
    if call_result_size(payload) > budget:
        raise ValueError("fixed response metadata exceeds the tool byte budget")
    if not selected and start < len(items):
        raise ValueError("one response item exceeds the tool byte budget")
    return payload


def fit_text_window(
    value: str,
    *,
    offset_chars: int,
    requested_chars: int,
    budget: int,
    make_payload: Callable[[str, int, bool], dict[str, Any]],
) -> tuple[str, int, bool, dict[str, Any]]:
    """Binary-search a code-point window that fits the serialized MCP result budget."""
    total = len(value)
    if offset_chars < 0 or offset_chars > total:
        raise ValueError(f"offset_chars {offset_chars} exceeds text length {total}")
    maximum = min(total, offset_chars + requested_chars)
    lo, hi = offset_chars, maximum
    best: tuple[str, int, bool, dict[str, Any]] | None = None
    while lo <= hi:
        end = (lo + hi) // 2
        text = value[offset_chars:end]
        has_more = end < total
        payload = make_payload(text, end, has_more)
        if call_result_size(payload) <= budget:
            best = (text, end, has_more, payload)
            lo = end + 1
        else:
            hi = end - 1
    if best is None:
        raise ValueError("fixed result metadata exceeds the command-result byte budget")
    return best
