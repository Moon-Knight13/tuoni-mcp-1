"""Errors must tell a small model what happened and whether to try again."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from tuoni.TuoniExceptions import (
    ExceptionTuoniAuthentication,
    ExceptionTuoniDeleted,
    ExceptionTuoniRequestFailed,
)

from tuoni_mcp.errors import classify
from tuoni_mcp.http_client import TuoniHTTPError
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, call_result_size, error_result


def _payload(result) -> dict:
    return json.loads(next(item.text for item in result.content if item.type == "text"))


# -- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("503 Service Unavailable", "TUONI_SERVER_ERROR"),
        ("500 Internal Server Error", "TUONI_SERVER_ERROR"),
        ("Tuoni request timed out after 30s", "TUONI_TIMEOUT"),
        ("Connection refused", "TUONI_UNREACHABLE"),
        ("Max retries exceeded with url", "TUONI_UNREACHABLE"),
    ],
)
def test_transient_failures_are_retryable_for_a_read(message: str, code: str) -> None:
    classified = classify(
        ExceptionTuoniRequestFailed(message), tool_name="tuoni_agents_list", safe_to_retry=True
    )

    assert classified.code == code
    assert classified.retryable is True
    assert "try" in classified.hint.casefold()


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("404 Not Found", "TUONI_NOT_FOUND"),
        ("403 Forbidden", "TUONI_FORBIDDEN"),
        ("Command template rejected the configuration", "TUONI_API_ERROR"),
    ],
)
def test_definite_rejections_are_never_retryable(message: str, code: str) -> None:
    classified = classify(
        ExceptionTuoniRequestFailed(message), tool_name="tuoni_agent_get", safe_to_retry=True
    )

    assert classified.code == code
    assert classified.retryable is False


def test_a_transient_failure_after_a_mutation_is_ambiguous_not_retryable() -> None:
    """Repeating a submission that may have landed could queue the command twice."""

    classified = classify(
        ExceptionTuoniRequestFailed("502 Bad Gateway"),
        tool_name="tuoni_operation_execute",
        safe_to_retry=False,
    )

    assert classified.code == "TUONI_SERVER_ERROR"
    assert classified.retryable is False
    assert "may already have reached" in classified.hint


@pytest.mark.parametrize(
    "message",
    [
        # A status code is far likelier to be part of an identifier than an
        # actual 5xx, because TuoniLib keeps only the server's message body.
        "command 1500 not found",
        "listener 503 not found",
        "job=2500 not found",
    ],
)
def test_an_id_that_looks_like_a_status_code_is_still_not_found(message: str) -> None:
    classified = classify(
        ExceptionTuoniRequestFailed(message), tool_name="tuoni_command_get", safe_to_retry=True
    )

    assert classified.code == "TUONI_NOT_FOUND"
    assert classified.retryable is False


def test_a_setting_named_timeout_is_not_a_timeout() -> None:
    classified = classify(
        ExceptionTuoniRequestFailed("setting with key=server.readTimeout does not exist"),
        tool_name="tuoni_setting_get",
        safe_to_retry=True,
    )

    assert classified.code != "TUONI_TIMEOUT"
    assert classified.retryable is False


def test_a_genuine_5xx_is_still_retryable() -> None:
    for message in ("503 Service Unavailable", "Internal Server Error", "502 Bad Gateway"):
        classified = classify(
            ExceptionTuoniRequestFailed(message), tool_name="tuoni_agents_list", safe_to_retry=True
        )
        assert classified.code == "TUONI_SERVER_ERROR", message
        assert classified.retryable is True, message


def test_authentication_and_deletion_are_reported_distinctly() -> None:
    auth = classify(ExceptionTuoniAuthentication("bad password"), tool_name="t", safe_to_retry=True)
    deleted = classify(ExceptionTuoniDeleted("gone"), tool_name="t", safe_to_retry=True)

    assert (auth.code, auth.retryable) == ("AUTHENTICATION_FAILED", False)
    assert (deleted.code, deleted.retryable) == ("RESOURCE_DELETED", False)


@pytest.mark.parametrize("status,code,retryable", [
    (403, "TUONI_FORBIDDEN", False), (404, "INCOMPATIBLE_TUONI_SERVER", False),
    (429, "TUONI_RATE_LIMITED", True), (503, "TUONI_SERVER_ERROR", True)
])
async def test_http_status_survives_helpers_and_takes_precedence_over_body(
    patched_server, fake_c2, status, code, retryable
):
    def rejected(*args, **kwargs):
        raise TuoniHTTPError(status, "command 503 not found")

    fake_c2.request_get = rejected
    async with create_connected_server_and_client_session(patched_server.protocol_server) as session:
        response = await session.call_tool("tuoni_agents_list", {})
    error = _payload(response)
    assert (error["code"], error["retryable"]) == (code, retryable)


def test_an_unexpected_exception_still_names_the_tool() -> None:
    classified = classify(ValueError("boom"), tool_name="tuoni_command_get", safe_to_retry=True)

    assert classified.code == "TOOL_EXECUTION_FAILED"
    assert "tuoni_command_get" in classified.message
    assert classified.retryable is False


def test_the_server_message_survives_into_the_bounded_envelope() -> None:
    classified = classify(
        ExceptionTuoniRequestFailed("agent refused: template plugin is not loaded"),
        tool_name="tuoni_command_prepare",
        safe_to_retry=True,
    )
    result = error_result(
        classified.code, classified.message, retryable=classified.retryable, hint=classified.hint
    )
    payload = _payload(result)

    assert "template plugin is not loaded" in payload["message"]
    assert call_result_size(result) <= ERROR_RESULT_BUDGET


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("rejected: password=hunter2", "hunter2"),
        # Tuoni quotes the offending request back as JSON, so the key is quoted.
        ('rejected: {"password": "hunter2"}', "hunter2"),
        ("rejected: {'password': 'hunter2'}", "hunter2"),
        ('body was {"apiKey":"AKIA123SECRET"}', "AKIA123SECRET"),
        ('config {"token": "tok_live_9999"}', "tok_live_9999"),
        ('{"content_base64": "QUJDREVGRw=="}', "QUJDREVGRw=="),
        ("upstream https://user:s3cr3t@c2.internal/api failed", "s3cr3t"),
        ("Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi.abc.def"),
        # `body` is itself a redacted key, so it must not match first and consume
        # past the inner password, which would leave the real secret in place.
        ('rejected body={"password": "hunter2"} tail', "hunter2"),
        ('body={"apiKey":"AKIA123","token":"tok_live_9"}', "AKIA123"),
        ('body={"apiKey":"AKIA123","token":"tok_live_9"}', "tok_live_9"),
        ('nested {"conf":{"password":"p1","other":1}}', "p1"),
    ],
)
def test_no_secret_shape_survives_into_a_surfaced_server_message(message: str, secret: str) -> None:
    """Surfacing the real Tuoni message is only safe if redaction covers its shapes."""

    classified = classify(
        ExceptionTuoniRequestFailed(message), tool_name="tuoni_command_prepare", safe_to_retry=True
    )
    payload = _payload(error_result(classified.code, classified.message))

    assert secret not in payload["message"]
    assert "[REDACTED]" in payload["message"]


@pytest.mark.parametrize(
    "message",
    [
        "password=hunter2",
        '{"password": "hunter2"}',
        'body={"password": "hunter2"} tail',
        "Authorization: Bearer eyJhbGciOi.abc.def",
        "https://user:s3cr3t@c2.internal/api",
    ],
)
def test_redaction_is_idempotent(message: str) -> None:
    """Several paths redact text that has already been redacted once."""

    from tuoni_mcp.protocol import redact_text

    once = redact_text(message)

    assert redact_text(once) == once


@pytest.mark.parametrize(
    "message",
    [
        "agent refused: template plugin is not loaded",
        "command_id: 42, status: COMPLETE",
        "404 Not Found",
    ],
)
def test_ordinary_diagnostics_are_not_over_redacted(message: str) -> None:
    """Over-redaction would destroy the very detail this change exists to keep."""

    classified = classify(
        ExceptionTuoniRequestFailed(message), tool_name="tuoni_agent_get", safe_to_retry=True
    )

    assert "[REDACTED]" not in _payload(error_result(classified.code, classified.message))["message"]


def test_credentials_in_a_server_message_are_redacted() -> None:
    classified = classify(
        ExceptionTuoniRequestFailed("rejected for password=hunter2 on the upstream call"),
        tool_name="tuoni_command_prepare",
        safe_to_retry=True,
    )
    payload = _payload(error_result(classified.code, classified.message))

    assert "hunter2" not in payload["message"]
    assert "[REDACTED]" in payload["message"]


# -- end to end through the tool boundary ----------------------------------


async def test_a_failing_read_reaches_the_model_as_a_retryable_tuoni_error(
    patched_server, fake_c2
) -> None:
    def unavailable(uri, result_as_json=True, result_as_bytes=False):
        raise ExceptionTuoniRequestFailed("503 Service Unavailable")

    fake_c2.request_get = unavailable

    async with create_connected_server_and_client_session(patched_server.protocol_server) as session:
        result = await session.call_tool("tuoni_command_get", {"command_id": 1})

    payload = _payload(result)
    assert result.isError is True
    assert payload["code"] == "TUONI_SERVER_ERROR"
    assert payload["retryable"] is True
    # Preserve the useful, sanitized status text at the public boundary.
    assert "503" in payload["message"]


async def test_configuration_hint_leads_with_the_correction(patched_server, fake_c2, wide_template) -> None:
    agent, template = wide_template
    fake_c2.responses[("GET", "/api/v1/agents/agent-1")] = agent
    fake_c2.responses[("GET", "/api/v1/command-templates")] = [template]

    async with create_connected_server_and_client_session(patched_server.protocol_server) as session:
        result = await session.call_tool(
            "tuoni_command_prepare",
            {
                "agent_guid": "agent-1",
                "template": "jump-service",
                "configuration": {name: 1 for name in ("copyType", "copyPath", "servicePath")},
            },
        )

    payload = _payload(result)
    assert payload["code"] == "INVALID_COMMAND_CONFIGURATION"
    # The allowed-field list must survive the 360-character hint cap, because it
    # is the only thing that lets the model build a valid configuration.
    assert payload["hint"].startswith("allowed configuration fields:")
    assert "target" in payload["hint"]
