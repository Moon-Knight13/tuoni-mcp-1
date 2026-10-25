from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import jsonschema
from pydantic import ValidationError

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools.commands import CommandGetInput, CommandStopPrepareInput, CommandUpdatePrepareInput


def _cfg() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )


def _command(result, *, status: str | None = None) -> dict:
    return {
        "id": 42,
        "commandTemplateId": "template-1",
        "commandTemplateName": "enumerate",
        "status": status or ("COMPLETE" if result is not None else "SENT"),
        "created": "2026-07-20T00:00:00Z",
        "sent": "2026-07-20T00:00:01Z",
        "result": result,
    }


def _result(parts: list[dict], *, error: str | None = None) -> dict:
    return {
        "status": "SUCCESS" if error is None else "FAILED",
        "errorMessage": error,
        "received": "2026-07-20T00:00:02Z",
        "childResults": parts,
    }


def _invocation(fake_c2, command: dict):
    cfg = _cfg()
    fake_c2.responses["/api/v1/commands/42"] = command
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = MagicMock()
    context.request_context.lifespan_context = server
    return build_registry(cfg), InvocationContext(context=context, server=server)


async def _get(registry, invocation, arguments: dict):
    spec = registry.get("tuoni_command_get")
    assert spec is not None
    return await registry.invoke_spec(spec, arguments, invocation)


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def test_canonical_decimal_read_id_fetches_the_same_command_once(fake_c2):
    registry, invocation = _invocation(fake_c2, _command(None))
    arguments = {"command_id": "42"}
    jsonschema.validate(arguments, CommandGetInput.model_json_schema())
    result = await _get(registry, invocation, arguments)
    assert not result.isError
    assert _payload(result)["data"]["command_id"] == 42
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == ["/api/v1/commands/42"]
    assert all(name == "request_get" for name, _, _ in fake_c2.calls)


@pytest.mark.parametrize("value", [True, False, 1.0, -1, "-1", "01", "1.0", "1e2", "42\n", " 42", "４２", "9" * 19])
def test_read_id_compatibility_rejects_ambiguous_or_noncanonical_values(value):
    arguments = {"command_id": value}
    with pytest.raises(ValidationError):
        CommandGetInput.model_validate(arguments)
    if not isinstance(value, float):  # JSON Schema treats mathematically integral floats as integers.
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(arguments, CommandGetInput.model_json_schema())


@pytest.mark.parametrize("model", [CommandStopPrepareInput, CommandUpdatePrepareInput])
def test_decimal_id_compatibility_does_not_apply_to_mutations(model):
    with pytest.raises(ValidationError):
        model.model_validate({"command_id": "42"})


async def test_manifest_and_small_text_windows_fetch_completed_result_once(fake_c2):
    registry, invocation = _invocation(fake_c2, _command(_result([
        {"type": "text", "name": "stdout", "value": "safe password=hidden tail"}
    ])))
    manifest = _payload(await _get(registry, invocation, {"command_id": 42}))
    assert manifest["ok"]
    text = ""
    arguments = {"command_id": 42, "part_index": 0, "max_chars": 2}
    while True:
        response = _payload(await _get(registry, invocation, arguments))
        assert response["ok"]
        text += response["data"]["result"]["part"]["value"]
        if response["next"] is None:
            break
        arguments = response["next"]["arguments"]
    assert text == "safe password=[REDACTED] tail"
    reads = [args for name, args, _ in fake_c2.calls if name == "request_get"]
    assert len(reads) == 1


@pytest.mark.parametrize("prefix", ["", "tuoni_"])
async def test_dispatch_windows_fit_the_final_envelope_and_reconstruct_text(fake_c2, prefix):
    text = ('🙂\\"\n' * 8_000)
    registry, invocation = _invocation(fake_c2, _command(_result([
        {"type": "text", "name": "stdout", "value": text}
    ])))
    invocation.server.cfg = replace(invocation.server.cfg, mode=MCPMode.DISPATCH, tool_prefix=prefix)
    adapters = {spec.name: spec for spec in build_dispatch_specs(
        registry, registry.select({Profile.AGENT_OPS}, read_only=False), read_only=False
    )}
    arguments = {"name": "tuoni_command_get", "arguments": {
        "command_id": 42, "part_index": 0, "max_chars": 16384
    }}
    reconstructed = ""
    while True:
        result = await registry.invoke_spec(adapters["tuoni_call"], arguments, invocation)
        payload = _payload(result)
        assert not result.isError, payload
        assert call_result_size(result) <= MAX_RESULT_BUDGET
        part = payload["data"]["result"]["part"]
        assert part["returned_chars"] > 0
        reconstructed += part["value"]
        if payload["next"] is None:
            break
        assert payload["next"]["name"] == prefix + "tuoni_call"
        arguments = payload["next"]["arguments"]
    assert reconstructed == text


async def test_many_part_manifest_is_body_free_paginated_and_byte_bounded(fake_c2) -> None:
    secret_text = "TEXT_BODY_MUST_NOT_APPEAR_" + "x" * 500_000
    parts: list[dict] = []
    for index in range(80):
        kind = index % 4
        if kind == 0:
            parts.append(
                {
                    "type": "text",
                    "name": f"stdout-{index}-" + "n" * 2_000,
                    "value": secret_text,
                }
            )
        elif kind == 1:
            parts.append(
                {
                    "type": "file",
                    "filename": f"artifact-{index}-" + "f" * 2_000,
                    "href": (
                        "https://operator:url-secret@example.test/download/result.bin"
                        "?authorization=query-secret#fragment-secret"
                    ),
                }
            )
        elif kind == 2:
            parts.append(
                {
                    "type": "binary",
                    "name": f"binary-{index}",
                    "result": "BASE64_BODY_MUST_NOT_APPEAR" + "A" * 100_000,
                }
            )
        else:
            parts.append(
                {
                    "type": "omitted",
                    "name": f"omitted-{index}",
                    "realType": "native",
                    "size": "9" * 2_000,
                }
            )
    registry, invocation = _invocation(
        fake_c2,
        _command(_result(parts, error="password=result-secret " + "e" * 10_000)),
    )
    arguments = {"command_id": 42, "limit": 17}
    indexes: list[int] = []
    versions: dict[int, str] = {}
    combined = ""
    terminal_next: dict | None = None

    while True:
        response = await _get(registry, invocation, arguments)
        payload = _payload(response)
        serialized = response.model_dump_json(by_alias=True, exclude_none=True)
        combined += serialized
        assert response.isError is False
        assert call_result_size(response) <= MAX_RESULT_BUDGET
        assert payload["data"]["result"]["part_count"] == 80
        assert len(payload["data"]["result"]["error_message"].encode("utf-8")) <= 512
        indexes.extend(
            item["part_index"] for item in payload["data"]["result"]["parts"]
        )
        versions.update({item["part_index"]: item["result_version"]
                         for item in payload["data"]["result"]["parts"] if item["type"] == "text"})
        if payload["next_cursor"] is None:
            terminal_next = payload["next"]
            break
        assert payload["next"]["name"] == "tuoni_command_get"
        assert payload["next"]["arguments"]["cursor"] == payload["next_cursor"]
        arguments = payload["next"]["arguments"]

    assert indexes == list(range(80))
    assert "TEXT_BODY_MUST_NOT_APPEAR" not in combined
    assert "BASE64_BODY_MUST_NOT_APPEAR" not in combined
    assert "url-secret" not in combined
    assert "query-secret" not in combined
    assert "fragment-secret" not in combined
    assert "result-secret" not in combined
    assert terminal_next == {
        "name": "tuoni_command_get",
        "arguments": {
            "command_id": 42,
            "part_index": 0,
            "result_version": versions[0],
            "offset_chars": 0,
            "max_chars": 4096,
        },
    }


async def test_unicode_escaping_and_combining_windows_reconstruct_exact_text(fake_c2) -> None:
    value = ('start 🙂 e\u0301 "quoted" \\\\ 漢字 end\n' * 3_000) + "terminal"
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": value}])),
    )
    arguments = {
        "command_id": 42,
        "part_index": 0,
        "offset_chars": 0,
        "max_chars": 16_384,
    }
    reconstructed: list[str] = []
    offsets: list[int] = []

    while True:
        response = await _get(registry, invocation, arguments)
        payload = _payload(response)
        part = payload["data"]["result"]["part"]
        assert response.isError is False
        assert call_result_size(response) <= MAX_RESULT_BUDGET
        assert part["offset_chars"] == arguments["offset_chars"]
        assert part["returned_chars"] == len(part["value"])
        reconstructed.append(part["value"])
        offsets.append(part["offset_chars"])
        if payload["next"] is None:
            assert part["has_more"] is False
            assert part["next_offset_chars"] is None
            break
        assert part["has_more"] is True
        assert part["next_offset_chars"] > part["offset_chars"]
        assert payload["next"]["arguments"]["offset_chars"] == part["next_offset_chars"]
        arguments = payload["next"]["arguments"]

    assert offsets == sorted(offsets)
    assert "".join(reconstructed) == value


async def test_text_window_fits_the_final_redacted_envelope(fake_c2) -> None:
    # Replacing a short sensitive assignment can expand the returned text. The
    # fitter must measure that final representation, rather than return an
    # oversized-page error after selecting a raw window.
    value = "body=x " * 3_000
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": value}])),
    )
    arguments = {
        "command_id": 42,
        "part_index": 0,
        "offset_chars": 0,
        "max_chars": 16_384,
    }

    while True:
        response = await _get(registry, invocation, arguments)
        payload = _payload(response)

        assert response.isError is False
        assert call_result_size(response) <= MAX_RESULT_BUDGET
        part = payload["data"]["result"]["part"]
        assert "body=x" not in part["value"]
        assert "[REDACTED]" in part["value"]
        if payload["next"] is None:
            assert part["has_more"] is False
            assert part["next_offset_chars"] is None
            break
        assert part["has_more"] is True
        assert part["next_offset_chars"] > arguments["offset_chars"]
        arguments = payload["next"]["arguments"]


async def test_tiny_windows_cannot_bypass_whole_part_redaction(fake_c2) -> None:
    value = "prefix password=hunter2 suffix"
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": value}])),
    )
    arguments = {
        "command_id": 42,
        "part_index": 0,
        "offset_chars": 0,
        "max_chars": 1,
    }
    reconstructed: list[str] = []

    while True:
        response = await _get(registry, invocation, arguments)
        assert response.isError is False
        payload = _payload(response)
        part = payload["data"]["result"]["part"]
        reconstructed.append(part["value"])
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]

    combined = "".join(reconstructed)
    assert combined == "prefix password=[REDACTED] suffix"
    assert "hunter2" not in combined

    manifest = await _get(registry, invocation, {"command_id": 42})
    manifest_part = _payload(manifest)["data"]["result"]["parts"][0]
    assert manifest_part["total_chars"] == len(combined)


async def test_terminal_offset_returns_an_empty_exact_window(fake_c2) -> None:
    value = "done 🙂"
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": value}])),
    )
    response = await _get(
        registry,
        invocation,
        {
            "command_id": 42,
            "part_index": 0,
            "offset_chars": len(value),
            "max_chars": 100,
        },
    )
    part = _payload(response)["data"]["result"]["part"]

    assert part["value"] == ""
    assert part["returned_chars"] == 0
    assert part["has_more"] is False
    assert part["next_offset_chars"] is None


async def test_invalid_part_offset_and_manifest_cursor_are_tool_errors(fake_c2) -> None:
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": "short"}])),
    )
    invalid_part = await _get(
        registry, invocation, {"command_id": 42, "part_index": 4}
    )
    invalid_offset = await _get(
        registry,
        invocation,
        {"command_id": 42, "part_index": 0, "offset_chars": 6},
    )
    invalid_cursor = await _get(
        registry,
        invocation,
        {"command_id": 42, "cursor": "not-a-cursor"},
    )

    assert _payload(invalid_part)["code"] == "INVALID_PART_INDEX"
    assert invalid_offset.isError is True
    assert _payload(invalid_offset)["code"] == "INVALID_OFFSET"
    assert _payload(invalid_cursor)["code"] == "INVALID_CURSOR"
    assert all(
        call_result_size(result) <= ERROR_RESULT_BUDGET
        for result in (invalid_part, invalid_offset, invalid_cursor)
    )


@pytest.mark.parametrize(
    "window_arguments",
    [
        {"offset_chars": 0},
        {"max_chars": 4096},
        {"offset_chars": 0, "max_chars": 4096},
    ],
    ids=("offset-only", "max-only", "offset-and-max"),
)
async def test_manifest_rejects_explicit_window_arguments_without_part_index(
    fake_c2,
    window_arguments: dict,
) -> None:
    """Do not silently reinterpret a malformed text-window call as a manifest call.

    OpenCode's small model repeatedly supplied ``offset_chars`` while omitting
    ``part_index``. Returning the successful manifest again made the malformed
    call look productive and caused an unbounded retry loop.
    """
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": "done"}])),
    )

    response = await _get(
        registry,
        invocation,
        {"command_id": 42, **window_arguments},
    )
    error = _payload(response)

    assert response.isError is True
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["retryable"] is False
    assert "part_index" in f"{error['message']} {error['hint']}"
    assert call_result_size(response) <= ERROR_RESULT_BUDGET


@pytest.mark.parametrize(
    "manifest_arguments",
    [
        {"cursor": "manifest-page-cursor"},
        {"limit": 20},
        {"cursor": "manifest-page-cursor", "limit": 20},
    ],
    ids=("cursor", "limit", "cursor-and-limit"),
)
async def test_text_window_rejects_explicit_manifest_arguments(
    fake_c2,
    manifest_arguments: dict,
) -> None:
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": "done"}])),
    )

    response = await _get(
        registry,
        invocation,
        {"command_id": 42, "part_index": 0, **manifest_arguments},
    )
    error = _payload(response)

    assert response.isError is True
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["retryable"] is False
    assert call_result_size(response) <= ERROR_RESULT_BUDGET


async def test_text_window_tolerates_explicit_null_cursor(fake_c2) -> None:
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "text", "name": "stdout", "value": "done"}])),
    )

    response = await _get(
        registry,
        invocation,
        {"command_id": 42, "part_index": 0, "cursor": None},
    )

    assert response.isError is False
    assert _payload(response)["data"]["result"]["part"]["value"] == "done"


async def test_terminal_manifest_points_to_first_text_window_with_exact_arguments(
    fake_c2,
) -> None:
    registry, invocation = _invocation(
        fake_c2,
        _command(
            _result(
                [
                    {"type": "binary", "name": "capture", "result": "AAAA"},
                    {"type": "text", "name": "stdout", "value": "complete output"},
                ]
            )
        ),
    )

    response = await _get(registry, invocation, {"command_id": 42})
    payload = _payload(response)

    assert response.isError is False
    assert payload["data"]["status"] == "COMPLETE"
    assert payload["next_cursor"] is None
    assert payload["next"] == {
        "name": "tuoni_command_get",
        "arguments": {
            "command_id": 42,
            "part_index": 1,
            "result_version": payload["data"]["result"]["parts"][1]["result_version"],
            "offset_chars": 0,
            "max_chars": 4096,
        },
    }


async def test_non_text_window_returns_metadata_only(fake_c2) -> None:
    encoded = "DO_NOT_INLINE_THIS_BASE64" + "A" * 100_000
    registry, invocation = _invocation(
        fake_c2,
        _command(_result([{"type": "binary", "name": "blob", "result": encoded}])),
    )
    response = await _get(
        registry, invocation, {"command_id": 42, "part_index": 0}
    )
    serialized = response.model_dump_json(by_alias=True, exclude_none=True)
    part = _payload(response)["data"]["result"]["part"]

    assert response.isError is False
    assert part["chunkable"] is False
    assert part["encoded_chars"] == len(encoded)
    assert "DO_NOT_INLINE_THIS_BASE64" not in serialized
    assert call_result_size(response) <= MAX_RESULT_BUDGET


async def test_pending_command_returns_poll_continuation_without_result(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, _command(None, status="SENT"))
    response = await _get(registry, invocation, {"command_id": 42})
    payload = _payload(response)

    assert response.isError is False
    assert payload["data"]["status"] == "SENT"
    assert payload["data"]["result"] is None
    assert payload["next"] == {
        "name": "tuoni_command_get",
        "arguments": {"command_id": 42},
    }


async def test_ongoing_result_pages_end_with_a_poll_continuation(fake_c2) -> None:
    parts = [
        {"type": "text", "name": "stdout", "value": "partial output"},
        {"type": "binary", "name": "capture", "result": "AAAA"},
    ]
    registry, invocation = _invocation(
        fake_c2,
        _command(_result(parts), status="ONGOING"),
    )
    poll = {
        "name": "tuoni_command_get",
        "arguments": {"command_id": 42},
    }

    manifest = await _get(registry, invocation, {"command_id": 42})
    text_part = await _get(
        registry,
        invocation,
        {"command_id": 42, "part_index": 0, "max_chars": 100},
    )
    binary_part = await _get(
        registry,
        invocation,
        {"command_id": 42, "part_index": 1},
    )

    assert _payload(manifest)["next"] == poll
    assert _payload(text_part)["next"] == poll
    assert _payload(binary_part)["next"] == poll
