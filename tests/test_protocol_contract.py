from __future__ import annotations

import json

from mcp.types import CallToolResult, TextContent

from tuoni_mcp.protocol import (
    DETAIL_RESULT_BUDGET,
    ERROR_RESULT_BUDGET,
    LIST_RESULT_BUDGET,
    MAX_RESULT_BUDGET,
    call_result_size,
    enforce_result_budget,
    error_result,
    success_result,
)


def _payload(result: CallToolResult) -> dict:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    return json.loads(content.text)


def test_success_contract_measures_complete_call_tool_result() -> None:
    result = success_result(
        {"items": ["emoji 🙂", "combining e\u0301", 'quote " slash \\\\']},
        next_cursor="opaque",
        next_call={"name": "next_tool", "arguments": {"cursor": "opaque"}},
        budget=LIST_RESULT_BUDGET,
    )

    assert result.isError is False
    assert _payload(result) == {
        "ok": True,
        "data": {"items": ["emoji 🙂", "combining e\u0301", 'quote " slash \\\\']},
        "next_cursor": "opaque",
        "next": {"name": "next_tool", "arguments": {"cursor": "opaque"}},
    }
    assert call_result_size(result) == len(
        result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    )
    assert call_result_size(result) <= LIST_RESULT_BUDGET


def test_success_redacts_credentials_and_attachment_bodies_recursively() -> None:
    secrets = {
        "password": "password-value",
        "nested": {
            "authorization": "Bearer authorization-value",
            "content_base64": "YXR0YWNobWVudC1ib2R5",
            "contentBase64": "Y2FtZWwtY2FzZS1ib2R5",
            "private_key": "private-key-value",
            "accessToken": "camel-access-token",
        },
        "message": (
            "token=message-token Authorization: Bearer header-token "
            "accessToken=raw-access-token contentBase64=raw-attachment-body "
            "body=raw-request-body"
        ),
        "url": "https://operator:url-password@example.test/path",
    }
    result = success_result(secrets, budget=DETAIL_RESULT_BUDGET)
    serialized = result.model_dump_json(by_alias=True, exclude_none=True)
    payload = _payload(result)

    assert result.isError is False
    assert payload["data"]["password"] == "[REDACTED]"
    assert payload["data"]["nested"]["authorization"] == "[REDACTED]"
    assert payload["data"]["nested"]["content_base64"] == "[REDACTED]"
    assert payload["data"]["nested"]["contentBase64"] == "[REDACTED]"
    assert payload["data"]["nested"]["private_key"] == "[REDACTED]"
    assert payload["data"]["nested"]["accessToken"] == "[REDACTED]"
    for secret in (
        "password-value",
        "authorization-value",
        "YXR0YWNobWVudC1ib2R5",
        "Y2FtZWwtY2FzZS1ib2R5",
        "private-key-value",
        "camel-access-token",
        "message-token",
        "header-token",
        "raw-access-token",
        "raw-attachment-body",
        "raw-request-body",
        "url-password",
    ):
        assert secret not in serialized
    assert call_result_size(result) <= DETAIL_RESULT_BUDGET


def test_credential_inventory_keeps_identity_but_redacts_secret_material() -> None:
    result = success_result(
        {
            "credentials": [
                {
                    "credential_id": "cred-1",
                    "credentialId": "cred-2",
                    "username": "bob",
                    "password": "hunter2",
                }
            ],
            "credential_count": 1,
            "credential": "raw-secret",
        },
        budget=DETAIL_RESULT_BUDGET,
    )
    data = _payload(result)["data"]

    assert data["credentials"][0] == {
        "credential_id": "cred-1",
        "credentialId": "cred-2",
        "username": "bob",
        "password": "[REDACTED]",
    }
    assert data["credential_count"] == 1
    assert data["credential"] == "[REDACTED]"
    assert "hunter2" not in result.content[0].text
    assert "raw-secret" not in result.content[0].text


def test_dynamic_setting_name_redacts_its_generic_value() -> None:
    result = success_result(
        {
            "settings": [
                {"key": "tuoni.agent.default-sleep", "value": "60"},
                {"key": "plugin.authorization-token", "value": "token-secret"},
                {
                    "name": "password",
                    "value": "password-secret",
                    "currentValue": "current-secret",
                    "defaultValue": "default-secret",
                },
            ]
        },
        budget=DETAIL_RESULT_BUDGET,
    )
    settings = _payload(result)["data"]["settings"]

    assert settings[0]["value"] == "60"
    assert settings[1]["value"] == "[REDACTED]"
    assert settings[2]["value"] == "[REDACTED]"
    assert settings[2]["currentValue"] == "[REDACTED]"
    assert settings[2]["defaultValue"] == "[REDACTED]"
    assert "token-secret" not in result.content[0].text
    assert "password-secret" not in result.content[0].text
    assert "current-secret" not in result.content[0].text
    assert "default-secret" not in result.content[0].text


def test_common_session_fields_and_signed_url_values_are_redacted() -> None:
    result = success_result(
        {
            "jwt": "jwt-secret",
            "auth": "auth-secret",
            "sessionId": "session-secret",
            "authHeader": "auth-header-secret",
            "basicAuth": "basic-auth-secret",
            "signature": "signature-secret",
            "oauthEnabled": True,
            "url": (
                "https://storage.test/object?X-Amz-Signature=signed-secret"
                "&X-Amz-Credential=credential-secret"
            ),
        },
        budget=DETAIL_RESULT_BUDGET,
    )
    data = _payload(result)["data"]

    assert data["jwt"] == "[REDACTED]"
    assert data["auth"] == "[REDACTED]"
    assert data["sessionId"] == "[REDACTED]"
    assert data["authHeader"] == "[REDACTED]"
    assert data["basicAuth"] == "[REDACTED]"
    assert data["signature"] == "[REDACTED]"
    assert data["oauthEnabled"] is True
    assert "signed-secret" not in data["url"]
    assert "credential-secret" not in data["url"]


def test_oversized_success_becomes_bounded_tool_error_without_sliced_json() -> None:
    result = success_result(
        {"value": ('🙂"\\e\u0301' * 10_000)},
        budget=MAX_RESULT_BUDGET,
    )
    payload = _payload(result)

    assert result.isError is True
    assert payload["code"] == "RESPONSE_TOO_LARGE"
    assert set(payload) == {"code", "message", "retryable", "hint"}
    assert call_result_size(result) <= ERROR_RESULT_BUDGET


def test_errors_are_valid_json_and_never_exceed_one_kibibyte() -> None:
    result = error_result(
        "X" * 10_000,
        ('failure 🙂 e\u0301 " \\\\ password=do-not-leak ' * 1_000),
        retryable=True,
        hint=("Bearer hint-secret " * 1_000),
    )
    payload = _payload(result)

    assert result.isError is True
    assert set(payload) == {"code", "message", "retryable", "hint"}
    assert payload["retryable"] is True
    assert "do-not-leak" not in result.content[0].text
    assert "hint-secret" not in result.content[0].text
    assert call_result_size(result) <= ERROR_RESULT_BUDGET


def test_prebuilt_results_are_guarded_by_complete_envelope_size() -> None:
    oversized = CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"value": "x" * 20_000}))],
        isError=False,
    )
    guarded = enforce_result_budget(oversized, budget=LIST_RESULT_BUDGET)

    assert guarded.isError is True
    assert _payload(guarded)["code"] == "RESPONSE_TOO_LARGE"
    assert call_result_size(guarded) <= ERROR_RESULT_BUDGET
