"""Translate Tuoni and transport failures into the model-facing error contract.

Classify failures with a stable code, retry guidance, and an actionable hint
while preserving useful upstream diagnostics and redacting sensitive text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from requests.exceptions import SSLError, Timeout

from tuoni.TuoniExceptions import (
    ExceptionTuoniAuthentication,
    ExceptionTuoniDeleted,
    ExceptionTuoniRequestFailed,
)
from tuoni_mcp.http_client import ResponseTooLarge

TUONI_ERRORS = (
    ExceptionTuoniAuthentication,
    ExceptionTuoniRequestFailed,
    ExceptionTuoniDeleted,
)

# Markers found in TuoniLib's message when the request never produced a definite
# server answer. Retrying these is the correct move for a read.
# Status codes are matched on word boundaries. TuoniLib keeps only the server's
# message body, so a bare "500" in it is far more likely to be part of an
# identifier ("command 1500 not found") than a status code.
_SERVER_ERROR_CODES = re.compile(r"\b(?:500|502|503|504)\b")
_SERVER_ERROR_MARKERS = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)
_TIMEOUT_MARKERS = ("timed out", "timeout")
# Likewise, "timeout" inside a setting name such as server.readTimeout is not a
# timeout. Require it to stand alone as a word.
_TIMEOUT_WORDS = re.compile(r"\b(?:timed out|timeout|timedout)\b")
_UNREACHABLE_MARKERS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",
    "connectionerror",
    "max retries exceeded",
    "temporarily unavailable",
    "name or service not known",
    "failed to establish",
    "broken pipe",
)
_NOT_FOUND_MARKERS = ("404", "not found")
_FORBIDDEN_MARKERS = ("401", "403", "unauthorized", "forbidden")

_RETRY_HINT = "Wait briefly, then try the same call again. Stop after a few attempts."


@dataclass(frozen=True)
class ToolError:
    """One classified failure, ready for the MCP error envelope."""

    code: str
    message: str
    retryable: bool
    hint: str


def format_tuoni_error(exc: BaseException) -> str:
    """Return a readable one-line description of a Tuoni failure."""

    if isinstance(exc, ExceptionTuoniAuthentication):
        return f"Tuoni authentication failed: {getattr(exc, 'message', None) or exc}"
    if isinstance(exc, ExceptionTuoniDeleted):
        return "Tuoni resource has been deleted or no longer exists."
    if isinstance(exc, ExceptionTuoniRequestFailed):
        return f"Tuoni request failed: {getattr(exc, 'message', None) or exc}"
    return f"{type(exc).__name__}: {exc}"


def _message_of(exc: BaseException) -> str:
    return str(getattr(exc, "message", None) or exc or "").strip()


def classify(exc: BaseException, *, tool_name: str, safe_to_retry: bool) -> ToolError:
    """Classify one exception raised inside a tool handler.

    ``safe_to_retry`` must be false for anything that may already have changed
    remote state. A transient failure after a command was submitted is ambiguous,
    not retryable, and repeating it could queue the operation twice.
    """

    lowered = _message_of(exc).casefold()

    def build(code: str, retryable: bool, hint: str, message: str | None = None) -> ToolError:
        allowed = retryable and safe_to_retry
        return ToolError(
            code=code,
            message=message or format_tuoni_error(exc),
            retryable=allowed,
            hint=hint
            if allowed
            else (
                "The operation may already have reached Tuoni. Inspect server state "
                "before deciding what to do next."
                if retryable
                else hint
            ),
        )

    if isinstance(exc, ResponseTooLarge):
        return build("UPSTREAM_RESPONSE_TOO_LARGE", False,
                     "Narrow the upstream query or raise TUONI_MCP_RESPONSE_MAX_BYTES / TUONI_MCP_DOWNLOAD_MAX_BYTES.")
    if isinstance(exc, SSLError):
        return build("TUONI_TLS_ERROR", False, "Set TUONI_VERIFY_TLS to the trusted Tuoni CA bundle path.")
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code in {401, 403}:
            return build("TUONI_FORBIDDEN", False, "This account is not permitted to perform that operation.")
        if status_code == 404:
            return build("TUONI_NOT_FOUND", False, "Re-read the inventory and select an existing resource.")
        if status_code == 429:
            return build("TUONI_RATE_LIMITED", True, "Wait before retrying; reduce concurrent calls.")
        if status_code >= 500:
            return build("TUONI_SERVER_ERROR", True, _RETRY_HINT)
        return build("TUONI_API_ERROR", False, "Correct the request or configured API URL before retrying.")

    if isinstance(exc, ExceptionTuoniAuthentication):
        return build(
            "AUTHENTICATION_FAILED",
            False,
            "Check the configured Tuoni credentials; re-login already failed once.",
        )
    if isinstance(exc, ExceptionTuoniDeleted):
        return build(
            "RESOURCE_DELETED",
            False,
            "Re-read the current inventory and select an item that still exists.",
        )

    # A definite answer from Tuoni is classified before any transient scan, so a
    # message like "command 1500 not found" is not read as a 500.
    if isinstance(exc, ExceptionTuoniRequestFailed):
        if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
            return build(
                "TUONI_NOT_FOUND",
                False,
                "The referenced resource does not exist. Re-read the inventory instead of retrying.",
            )
        if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
            return build(
                "TUONI_FORBIDDEN",
                False,
                "This account is not permitted to perform that operation.",
            )

    if isinstance(exc, (TimeoutError, Timeout)):
        return build("TUONI_TIMEOUT", True, _RETRY_HINT)
    if isinstance(exc, ConnectionError) or any(
        marker in lowered for marker in _UNREACHABLE_MARKERS
    ):
        return build("TUONI_UNREACHABLE", True, _RETRY_HINT)
    if _TIMEOUT_WORDS.search(lowered):
        return build("TUONI_TIMEOUT", True, _RETRY_HINT)
    if _SERVER_ERROR_CODES.search(lowered) or any(
        marker in lowered for marker in _SERVER_ERROR_MARKERS
    ):
        return build("TUONI_SERVER_ERROR", True, _RETRY_HINT)

    if isinstance(exc, ExceptionTuoniRequestFailed):
        return build(
            "TUONI_API_ERROR",
            False,
            "Correct the request from the message above before trying anything else.",
        )

    if isinstance(exc, OSError):
        return build("TUONI_UNREACHABLE", True, _RETRY_HINT)

    return ToolError(
        code="TOOL_EXECUTION_FAILED",
        message=f"{type(exc).__name__} while executing {tool_name}.",
        retryable=False,
        hint="Inspect the error and server state before deciding whether to retry.",
    )


def classify_rejection(exc: BaseException, *, tool_name: str, outcome: dict) -> ToolError:
    """A recorded rejection status survives a failed/truncated error body read."""
    status = outcome.get("http_status")
    if type(status) is int and getattr(exc, "status_code", None) != status:
        exc = ExceptionTuoniRequestFailed("The upstream write was rejected.", status_code=status)
    return classify(exc, tool_name=tool_name, safe_to_retry=False)
