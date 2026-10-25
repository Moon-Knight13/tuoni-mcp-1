from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from mcp.types import CallToolResult, TextContent

# These limits include the complete serialized CallToolResult, not only the JSON
# string carried by TextContent. The latter is escaped once more on the MCP wire.
LIST_RESULT_BUDGET = 4 * 1024
DETAIL_RESULT_BUDGET = 6 * 1024
MAX_RESULT_BUDGET = 8 * 1024
ERROR_RESULT_BUDGET = 1024

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"authorization|(?:^|[_-])auth(?:$|[_-])|password|passwd|passphrase|"
    r"secret|token|jwt|session[_-]?id|cookie|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|content[_-]?base64|body",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
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
_AUTH_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|auth|password|passwd|passphrase|secret|jwt|session[_-]?id|"
    r"(?:access|refresh|id)?[_-]?token|credential|signature|sig|cookie|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|content[_-]?base64|body)"
    # A Tuoni error often quotes the offending request back as JSON, so the key
    # may be quoted: {"password": "..."} as well as password=... . Without the
    # optional closing quote the JSON form slips through unredacted.
    r"[\"']?\s*[:=]\s*"
    # The already-redacted marker comes first so re-redacting text is a no-op;
    # otherwise the unquoted branch would stop at its closing bracket and leave
    # a stray one behind each time.
    r"(?:\[REDACTED\]"
    r"|\"(?:\\.|[^\"\\])*\""
    r"|'(?:\\.|[^'\\])*'"
    # The unquoted branch must not swallow a following key. `{` and `\"` end it,
    # so `body={\"password\": \"x\"}` fails to match at `body` and the engine
    # goes on to redact the inner password instead of consuming past it.
    r"|[^\s,;}\]\"{]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+@")


# Some clients expose an MCP tool under a name of their own: OpenCode prefixes
# every tool with its server key, so `tuoni_agents_list` is callable only as
# `tuoni_tuoni_agents_list`. A continuation naming the server-side name is then
# not callable, and the operating rule "copy next verbatim" becomes a trap. When
# the deployment declares that prefix, continuations are emitted in the client's
# vocabulary instead. It is request-scoped rather than a parameter so page
# packing and envelope construction measure the identical bytes.
_TOOL_PREFIX: ContextVar[str] = ContextVar("tuoni_mcp_tool_prefix", default="")
_DISPATCH_TARGETS: ContextVar[frozenset[str]] = ContextVar("tuoni_mcp_dispatch_targets", default=frozenset())


@contextmanager
def dispatch_continuations(targets):
    """Measure and render the same dispatch envelope before packing a page."""
    token = _DISPATCH_TARGETS.set(frozenset(targets))
    try:
        yield
    finally:
        _DISPATCH_TARGETS.reset(token)


def validation_schema_call(tool_name: str) -> dict[str, Any] | None:
    """Offer schema recovery only for a hidden, enabled dispatch target."""
    if tool_name in _DISPATCH_TARGETS.get():
        return {"name": "tuoni_tool_schema", "arguments": {"name": tool_name}}
    return None


@contextmanager
def exposed_tool_prefix(prefix: str):
    """Emit continuations under the name the connected client exposes."""
    token = _TOOL_PREFIX.set(prefix or "")
    try:
        yield
    finally:
        _TOOL_PREFIX.reset(token)


def exposed_name(name: str) -> str:
    """Rename one tool to what the client exposes.

    The prefix is applied unconditionally. A "does it already start with the
    prefix" guard would be wrong here, because the usual prefix is the server
    key `tuoni_` and every tool is already named `tuoni_*`. Idempotence comes
    from applying this exactly once, where the envelope is built.
    """
    prefix = _TOOL_PREFIX.get()
    if not prefix or not isinstance(name, str):
        return name
    return prefix + name


def native_name(name: str) -> str:
    """Undo :func:`exposed_name`, for looking a continuation back up internally.

    Strips one prefix occurrence. Callers that may hold either form should fall
    back to the original name, since `tuoni_command_get` is a legitimate native
    name that also starts with the `tuoni_` prefix.
    """
    prefix = _TOOL_PREFIX.get()
    if prefix and isinstance(name, str) and name.startswith(prefix):
        return name[len(prefix):]
    return name


def exposed_call(next_call: Any) -> Any:
    """Rename a continuation's target to what the client exposes.

    Every place that builds or *measures* a success envelope must apply this, or
    a page sized against the un-prefixed bytes would overflow its budget once the
    prefix is added.
    """
    if not isinstance(next_call, Mapping):
        return next_call
    name = next_call.get("name")
    if not isinstance(name, str):
        return next_call
    if name in _DISPATCH_TARGETS.get():
        next_call = {
            "name": "tuoni_call",
            "arguments": {"name": name, "arguments": next_call.get("arguments", {})},
        }
        name = "tuoni_call"
    return {**next_call, "name": exposed_name(name)}



def compact_json(value: Any) -> str:
    """Serialize JSON deterministically without wasting model context."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def call_result_size(result: CallToolResult) -> int:
    """Return the complete serialized MCP result size in UTF-8 bytes."""
    return len(
        result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    )


def _bounded_text(value: Any, max_chars: int) -> str:
    text = redact_text(str(value))
    if len(text) <= max_chars:
        return text
    # Python slices at Unicode code-point boundaries, so the resulting JSON is
    # always valid UTF-8. The marker makes the structural omission explicit.
    return text[: max(0, max_chars - 1)] + "…"


def redact_text(value: str) -> str:
    """Redact credentials embedded in free-form text and signed URLs."""
    value = _AUTH_VALUE.sub("Bearer [REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)


def should_redact_key(key: str, value: Any) -> bool:
    """Classify secret-bearing fields without hiding credential identities.

    Tuoni returns useful inventory under ``credentials`` and references selected
    records with fields such as ``credentialId``.  Treating every key containing
    "credential" as secret made those APIs unusable.  Containers are traversed,
    identifiers/counts remain visible, and an actual scalar credential is still
    redacted conservatively.
    """
    normalized = _CAMEL_BOUNDARY.sub("_", key).replace("-", "_").lower()
    if _SENSITIVE_KEY.search(normalized):
        return True
    if normalized in {"signature", "sig"} or normalized.endswith(
        ("_signature", "_sig")
    ):
        return not (
            isinstance(value, Mapping)
            or isinstance(value, (list, tuple))
        )
    if "credential" not in normalized:
        return False
    if normalized in {"credential", "credentials"}:
        return not (
            isinstance(value, Mapping)
            or isinstance(value, (list, tuple))
        )
    return not normalized.endswith(("_id", "_ids", "_count"))


def redact(value: Any, *, _key: str = "") -> Any:
    """Remove common credential and attachment-body fields from model output."""
    if _key and should_redact_key(_key, value):
        return _REDACTED
    if isinstance(value, Mapping):
        result = {
            str(key): redact(item, _key=str(key)) for key, item in value.items()
            if not (value.get("type") == "binary" and key == "result")
        }
        if value.get("type") == "binary" and "result" in value:
            body = value["result"]
            result["encoded_chars"] = len(body) if isinstance(body, str) else 0
            result["result"] = _REDACTED
        # Several Tuoni APIs represent settings/custom fields as a dynamic
        # ``key``/``name`` plus a generic ``value``. The generic field name alone
        # does not reveal that the paired value contains a password or token.
        dynamic_probe = next(
            (value.get(value_key) for value_key in _DYNAMIC_VALUE_KEYS if value_key in value),
            None,
        )
        dynamic_sensitive = any(
            isinstance(value.get(label), str)
            and should_redact_key(str(value[label]), dynamic_probe)
            for label in ("key", "name", "field")
        )
        if dynamic_sensitive:
            for value_key in _DYNAMIC_VALUE_KEYS:
                if value_key in result:
                    result[value_key] = _REDACTED
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _text_result(payload: Any, *, is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=compact_json(payload))],
        isError=is_error,
    )


def error_result(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    hint: str = "",
    next_call: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    budget: int = ERROR_RESULT_BUDGET,
) -> CallToolResult:
    """Build a bounded MCP tool error with the public JSON error contract."""
    payload = {
        "code": _bounded_text(code, 80),
        "message": _bounded_text(message, 320),
        "retryable": bool(retryable),
        "hint": _bounded_text(hint, 360),
    }
    if next_call is not None:
        payload["next"] = redact(exposed_call(next_call))
    if outcome is not None:
        payload["outcome"] = redact(outcome)
    result = _text_result(payload, is_error=True)
    if call_result_size(result) <= budget:
        return result

    # Multibyte text and JSON escaping can make character-based estimates too
    # large. Shrink whole string fields and reserialize until the final envelope
    # fits; never slice serialized JSON.
    message_text = payload["message"]
    hint_text = payload["hint"]
    while call_result_size(result) > budget and (message_text or hint_text):
        if len(hint_text) >= len(message_text) and hint_text:
            hint_text = hint_text[: len(hint_text) // 2]
        elif message_text:
            message_text = message_text[: len(message_text) // 2]
        payload["message"] = message_text
        payload["hint"] = hint_text
        result = _text_result(payload, is_error=True)

    # The fixed envelope is far below 1 KiB, but retain a deterministic fallback
    # in case a caller supplies an unrealistically tiny private-test budget.
    if call_result_size(result) > budget:
        result = _text_result(
            {
                "code": "ERROR",
                "message": "Tool failed.",
                "retryable": False,
                "hint": "",
            },
            is_error=True,
        )
    return result


def success_result(
    data: Any,
    *,
    next_cursor: str | None = None,
    next_call: dict[str, Any] | None = None,
    budget: int = DETAIL_RESULT_BUDGET,
    redact_output: bool = True,
) -> CallToolResult:
    """Build the canonical success envelope or a bounded size error.

    Pagination belongs to the handler: this function deliberately never slices a
    JSON value to make it fit. A handler that exceeds its advertised budget gets a
    tool error and must add structural pagination.
    """
    exposed = exposed_call(next_call)
    payload = {
        "ok": True,
        "data": redact(data) if redact_output else data,
        "next_cursor": next_cursor,
        "next": redact(exposed) if redact_output else exposed,
    }
    result = _text_result(payload, is_error=False)
    if call_result_size(result) <= min(budget, MAX_RESULT_BUDGET):
        return result
    return error_result(
        "RESPONSE_TOO_LARGE",
        "The tool response exceeded its serialized byte limit.",
        retryable=False,
        hint="Narrow the query or follow the tool's structural pagination fields.",
    )


def enforce_result_budget(
    result: CallToolResult,
    *,
    budget: int,
) -> CallToolResult:
    """Guard a prebuilt CallToolResult without modifying or slicing its content."""
    if call_result_size(result) <= min(budget, MAX_RESULT_BUDGET):
        return result
    return error_result(
        "RESPONSE_TOO_LARGE",
        "The tool response exceeded its serialized byte limit.",
        retryable=False,
        hint="Narrow the request; the oversized response was not returned.",
    )


def would_fit_success(
    data: Any,
    *,
    next_cursor: str | None = None,
    next_call: dict[str, Any] | None = None,
    budget: int,
    redact_output: bool = True,
) -> bool:
    """Measure a candidate page using the complete success envelope."""
    payload = {
        "ok": True,
        "data": redact(data) if redact_output else data,
        "next_cursor": next_cursor,
        "next": redact(exposed_call(next_call)) if redact_output else exposed_call(next_call),
    }
    return call_result_size(_text_result(payload, is_error=False)) <= min(
        budget, MAX_RESULT_BUDGET
    )
