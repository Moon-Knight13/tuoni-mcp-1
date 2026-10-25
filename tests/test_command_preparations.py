from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import MagicMock

import pytest
import anyio

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.preparations import PreparationStore
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry


def _cfg() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )


def _template(*, with_file: bool = False) -> dict:
    schema: dict = {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1}},
        "required": ["command"],
        "additionalProperties": False,
    }
    if with_file:
        schema["files"] = {"input": {"type": "file", "required": True}}
    return {
        "id": "template-cmd",
        "name": "cmd",
        "pluginId": "shell.commands",
        "scope": "global",
        "qualifiedName": "shell.commands:global:cmd",
        "fullyQualifiedName": "shell.commands:global:cmd",
        "description": "Run Windows cmd.exe",
        "supportedExecUnitTypes": ["SHELLCODE_NATIVE", "DOTNET_EXE"],
        "configurationSchema": schema,
        "updateSchema": {
            "type": "object",
            "properties": {
                "stdin": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["stdin"],
            "additionalProperties": False,
        },
        "exampleConfigurations": [],
    }


def _agent() -> dict:
    return {
        "guid": "agent-1",
        "active": True,
        "status": "ACTIVE",
        "availableCommandTemplates": ["template-cmd"],
    }


def _queued_command(*, status: str = "QUEUED") -> dict:
    return {
        "id": 42,
        "commandTemplateId": "template-cmd",
        "commandTemplateName": "cmd",
        "status": status,
        "created": "2026-07-20T00:00:00Z",
        "sent": None,
        "result": None,
    }


def _invocation(fake_c2, template: dict | None = None):
    cfg = _cfg()
    template = template or _template()
    fake_c2.responses["/api/v1/agents/agent-1"] = _agent()
    fake_c2.responses["/api/v1/command-templates"] = [template]
    fake_c2.responses[("POST", "/api/v1/agents/agent-1/commands")] = (
        _queued_command()
    )
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


async def _prepare_create(
    registry,
    invocation,
    *,
    execution: dict | None = None,
    attachments: list[dict] | None = None,
):
    arguments: dict = {
        "agent_guid": "agent-1",
        "template": "template-cmd",
        "configuration": {"command": "whoami /all"},
    }
    if execution is not None:
        arguments["execution"] = execution
    if attachments is not None:
        arguments["attachments"] = attachments
    return await _call(registry, invocation, "tuoni_command_prepare", arguments)


def _mutation_calls(fake_c2):
    return [
        call
        for call in fake_c2.calls
        if call[0] in {"request_post", "request_put", "request_patch", "request_delete"}
    ]


async def test_prepare_validates_and_stores_without_remote_mutation(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    payload = _payload(prepared)
    serialized = prepared.model_dump_json(by_alias=True, exclude_none=True)

    assert prepared.isError is False
    assert payload["data"]["operation"] == "command.create"
    assert payload["data"]["expires_in_seconds"] == 300
    assert payload["data"]["summary"]["configuration_keys"] == ["command"]
    assert payload["data"]["summary"]["configuration_preview"] == {
        "command": "whoami /all"
    }
    assert payload["next"] == {
        "name": "tuoni_operation_execute",
        "arguments": {"preparation_id": payload["data"]["preparation_id"]},
    }
    assert "whoami /all" in serialized
    assert _mutation_calls(fake_c2) == []
    assert call_result_size(prepared) <= 6 * 1024


async def test_invalid_shell_arguments_are_rejected_without_preparation_or_mutation(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    rejected = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            "template": "cmd",
            "configuration": {"args": "whoami"},
        },
    )

    assert rejected.isError is True
    assert _payload(rejected)["code"] == "INVALID_COMMAND_CONFIGURATION"
    assert "shell_command" in _payload(rejected)["hint"]
    assert _mutation_calls(fake_c2) == []
    store = getattr(invocation.server, "preparation_store", None)
    assert store is None or await store.stats() == (0, 0)


@pytest.mark.parametrize(
    "execution",
    (
        None,
        {"execType": "SELF"},
        {"execType": "SELF", "execUnitType": "SHELLCODE_NATIVE"},
        {
            "execType": "NEW",
            "executable": "cmd.exe",
            "execUnitType": "DOTNET_EXE",
            "suspended": True,
            "username": "EXAMPLE\\operator",
            "password": "execution-secret",
            "ppid": 444,
        },
        {
            "execType": "EXISTING",
            "pid": 1234,
            "execUnitType": "SHELLCODE_NATIVE",
        },
    ),
)
async def test_all_execution_modes_prepare_and_execute_once(
    fake_c2, execution
) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation, execution=execution)
    assert prepared.isError is False
    assert "execution-secret" not in prepared.model_dump_json(
        by_alias=True, exclude_none=True
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    assert _mutation_calls(fake_c2) == []

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    assert executed.isError is False
    assert _payload(executed)["data"]["command_id"] == 42
    posts = [call for call in fake_c2.calls if call[0] == "request_post"]
    assert len(posts) == 1
    body = posts[0][1][1]
    if execution is None:
        assert "execConf" not in body
    else:
        assert body["execConf"] == execution


@pytest.mark.parametrize(
    "execution",
    (
        {"execType": "SELF", "executable": "must-not-be-accepted.exe"},
        {"execType": "NEW"},
        {"execType": "EXISTING", "pid": 0},
        {"execType": "EXISTING", "pid": True},
        {"execType": "UNKNOWN"},
        {"execType": "SELF", "execUnitType": "NOT_A_REAL_UNIT_TYPE"},
        {"execType": "NEW", "executable": "cmd.exe", "ppid": -1},
        {"execType": "NEW", "executable": "cmd.exe", "ppid": False},
        {
            "execType": "NEW",
            "executable": "cmd.exe",
            "username": "EXAMPLE\\operator",
        },
        {
            "execType": "NEW",
            "executable": "cmd.exe",
            "password": "execution-secret",
        },
    ),
)
async def test_malformed_execution_modes_are_rejected_before_any_c2_call(
    fake_c2, execution
) -> None:
    registry, invocation = _invocation(fake_c2)
    rejected = await _prepare_create(registry, invocation, execution=execution)

    assert rejected.isError is True
    assert _payload(rejected)["code"] == "INVALID_ARGUMENTS"
    assert fake_c2.calls == []


@pytest.mark.parametrize("command_id", [True, False, "42", 42.0])
async def test_command_ids_do_not_coerce_a_different_input_type_into_a_target(fake_c2, command_id):
    registry, invocation = _invocation(fake_c2)
    rejected = await registry.invoke_spec(
        registry.get("tuoni_command_stop_prepare"), {"command_id": command_id}, invocation
    )
    assert rejected.isError
    assert _payload(rejected)["code"] == "INVALID_ARGUMENTS"
    assert fake_c2.calls == []


async def test_invalid_backend_command_identity_is_ambiguous_and_never_replayed(fake_c2):
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    assert not prepared.isError
    fake_c2.responses[("POST", "/api/v1/agents/agent-1/commands")] = {"id": True}
    arguments = _payload(prepared)["next"]["arguments"]
    first = await registry.invoke_spec(registry.get("tuoni_operation_execute"), arguments, invocation)
    second = await registry.invoke_spec(registry.get("tuoni_operation_execute"), arguments, invocation)
    assert first.isError and second.isError
    assert _payload(first)["code"] == _payload(second)["code"] == "AMBIGUOUS_OUTCOME"
    assert len([call for call in fake_c2.calls if call[0] == "request_post"]) == 1


async def test_unknown_preparation_does_not_instruct_replaying_an_attempted_execution(fake_c2):
    registry, invocation = _invocation(fake_c2)
    response = await registry.invoke_spec(
        registry.get("tuoni_operation_execute"), {"preparation_id": "unknown-preparation"}, invocation
    )
    error = _payload(response)
    assert error["code"] == "PREPARATION_NOT_FOUND"
    assert error["retryable"] is False
    assert "do not resubmit" in error["hint"]
    assert fake_c2.calls == []


async def test_known_but_unsupported_exec_unit_type_is_rejected_without_mutation(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    rejected = await _prepare_create(
        registry,
        invocation,
        execution={"execType": "SELF", "execUnitType": "DOTNET_DLL"},
    )

    assert rejected.isError is True
    assert _payload(rejected)["code"] == "INVALID_EXECUTION"
    assert "SHELLCODE_NATIVE" in _payload(rejected)["hint"]
    assert "DOTNET_EXE" in _payload(rejected)["hint"]
    assert _mutation_calls(fake_c2) == []


@pytest.mark.parametrize(
    "exec_unit_type",
    ("SHELLCODE_NATIVE", "DOTNET_EXE", "DOTNET_DLL", "NATIVE_LIB"),
)
async def test_each_advertised_exec_unit_type_can_be_prepared(
    fake_c2, exec_unit_type: str
) -> None:
    template = _template()
    template["supportedExecUnitTypes"] = [exec_unit_type]
    registry, invocation = _invocation(fake_c2, template)

    prepared = await _prepare_create(
        registry,
        invocation,
        execution={"execType": "SELF", "execUnitType": exec_unit_type},
    )

    assert prepared.isError is False
    assert _payload(prepared)["data"]["summary"]["execution"] == {
        "execType": "SELF",
        "execUnitType": exec_unit_type,
    }
    assert _mutation_calls(fake_c2) == []


async def test_execute_receipt_is_replayed_without_duplicate_submission(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    preparation_id = _payload(prepared)["data"]["preparation_id"]

    first, second = await asyncio.gather(
        _call(
            registry,
            invocation,
            "tuoni_operation_execute",
            {"preparation_id": preparation_id},
        ),
        _call(
            registry,
            invocation,
            "tuoni_operation_execute",
            {"preparation_id": preparation_id},
        ),
    )

    assert first.isError is False
    assert second.isError is False
    assert _payload(first)["data"] == _payload(second)["data"]
    assert len([call for call in fake_c2.calls if call[0] == "request_post"]) == 1


async def test_ambiguous_submission_is_never_automatically_retried(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    preparation_id = _payload(prepared)["data"]["preparation_id"]

    def ambiguous_post(uri, json_data=None, files=None):
        fake_c2._record("request_post", uri, json_data, files)
        raise TimeoutError("connection dropped after request body was sent")

    fake_c2.request_post = ambiguous_post
    first = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    second = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )

    assert first.isError is True
    assert second.isError is True
    assert _payload(first)["code"] == _payload(second)["code"] == "AMBIGUOUS_OUTCOME"
    assert len([call for call in fake_c2.calls if call[0] == "request_post"]) == 1
    assert call_result_size(first) <= ERROR_RESULT_BUDGET


async def test_template_drift_blocks_execution_before_submission(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    changed = _template()
    changed["configurationSchema"]["properties"]["new_required"] = {
        "type": "string"
    }
    changed["configurationSchema"]["required"].append("new_required")
    fake_c2.responses["/api/v1/command-templates"] = [changed]

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )

    assert executed.isError is True
    assert _payload(executed)["code"] == "PREPARATION_DRIFT"
    assert _mutation_calls(fake_c2) == []


async def test_inline_attachment_limit_and_body_redaction(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))
    accepted = await _prepare_create(
        registry,
        invocation,
        attachments=[
            {
                "field": "input",
                "filename": "input.bin",
                "content_base64": base64.b64encode(b"S" * 4096).decode(),
            }
        ],
    )
    serialized = accepted.model_dump_json(by_alias=True, exclude_none=True)

    assert accepted.isError is False
    assert _payload(accepted)["data"]["summary"]["attachments"] == [
        {
            "field": "input",
            "source": "inline",
            "filename": "input.bin",
            "decoded_bytes": 4096,
        }
    ]
    assert base64.b64encode(b"S" * 32).decode() not in serialized
    assert _mutation_calls(fake_c2) == []

    too_large_registry, too_large_invocation = _invocation(
        fake_c2, _template(with_file=True)
    )
    rejected = await _prepare_create(
        too_large_registry,
        too_large_invocation,
        attachments=[
            {
                "field": "input",
                "filename": "input.bin",
                "content_base64": base64.b64encode(b"L" * 4097).decode(),
            }
        ],
    )
    assert rejected.isError is True
    assert _payload(rejected)["code"] == "INLINE_ATTACHMENT_TOO_LARGE"
    assert _mutation_calls(fake_c2) == []


async def test_oversized_inline_attachment_is_rejected_before_decode(
    fake_c2, monkeypatch
) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("oversized inline input must be rejected before decoding")

    monkeypatch.setattr("tuoni_mcp.tools.commands.base64.b64decode", unexpected_decode)
    rejected = await _prepare_create(
        registry,
        invocation,
        attachments=[
            {
                "field": "input",
                "filename": "input.bin",
                "content_base64": "A" * 6000,
            }
        ],
    )

    assert rejected.isError is True
    assert _payload(rejected)["code"] == "INLINE_ATTACHMENT_TOO_LARGE"
    assert _mutation_calls(fake_c2) == []


async def test_unreturnable_preparation_summary_is_rejected_before_storage(
    fake_c2,
) -> None:
    template = _template()
    fields = [f"field-{index}-" + "f" * 100 for index in range(32)]
    template["configurationSchema"]["files"] = {
        field: {"type": "file"} for field in fields
    }
    registry, invocation = _invocation(fake_c2, template)

    rejected = await _prepare_create(
        registry,
        invocation,
        attachments=[
            {
                "field": field,
                "filename": f"attachment-{index}-" + "n" * 220,
                "content_base64": "",
            }
            for index, field in enumerate(fields)
        ],
    )

    assert rejected.isError is True
    assert _payload(rejected)["code"] == "PREPARATION_SUMMARY_TOO_LARGE"
    store = getattr(invocation.server, "preparation_store", None)
    assert store is None or await store.stats() == (0, 0)
    assert _mutation_calls(fake_c2) == []


async def test_existing_file_attachment_is_revalidated_and_materialized(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))
    metadata = {
        "fileId": "file-1",
        "originalFileName": "artifact.bin",
        "size": 3,
        "downloadHref": "/download/file-1",
    }
    fake_c2.responses["/api/v1/file/file-1"] = metadata
    fake_c2.responses["/download/file-1"] = b"abc"
    prepared = await _prepare_create(
        registry,
        invocation,
        attachments=[{"field": "input", "file_id": "file-1"}],
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    assert _mutation_calls(fake_c2) == []

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    assert executed.isError is False
    post = next(call for call in fake_c2.calls if call[0] == "request_post")
    assert post[1][2] == {"input": ["artifact.bin", b"abc"]}


async def test_absolute_file_download_href_uses_only_path_and_query(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))
    metadata = {
        "fileId": "file-1",
        "originalFileName": "artifact.bin",
        "size": 3,
        "downloadHref": (
            "https://operator:url-password@example.test/download/file-1"
            "?signature=opaque-value#must-not-be-sent"
        ),
    }
    fake_c2.responses["/api/v1/file/file-1"] = metadata
    fake_c2.responses["/download/file-1?signature=opaque-value"] = b"abc"
    prepared = await _prepare_create(
        registry,
        invocation,
        attachments=[{"field": "input", "file_id": "file-1"}],
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    gets = [call[1][0] for call in fake_c2.calls if call[0] == "request_get"]

    assert executed.isError is False
    assert "/download/file-1?signature=opaque-value" in gets
    assert not any("operator" in path or "url-password" in path for path in gets)
    assert not any("must-not-be-sent" in path for path in gets)


async def test_existing_file_drift_prevents_submission(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))
    fake_c2.responses["/api/v1/file/file-1"] = {
        "fileId": "file-1",
        "originalFileName": "artifact.bin",
        "size": 3,
        "downloadHref": "/download/file-1",
    }
    fake_c2.responses["/download/file-1"] = b"abc"
    prepared = await _prepare_create(
        registry,
        invocation,
        attachments=[{"field": "input", "file_id": "file-1"}],
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    fake_c2.responses["/api/v1/file/file-1"]["size"] = 4

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    assert executed.isError is True
    assert _payload(executed)["code"] == "PREPARATION_DRIFT"
    assert _mutation_calls(fake_c2) == []


async def test_equal_size_existing_file_content_drift_prevents_submission(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2, _template(with_file=True))
    fake_c2.responses["/api/v1/file/file-1"] = {
        "fileId": "file-1",
        "originalFileName": "artifact.bin",
        "size": 3,
        "downloadHref": "/download/file-1",
    }
    fake_c2.responses["/download/file-1"] = b"abc"
    prepared = await _prepare_create(
        registry,
        invocation,
        attachments=[{"field": "input", "file_id": "file-1"}],
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    fake_c2.responses["/download/file-1"] = b"xyz"

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )

    assert executed.isError is True
    assert _payload(executed)["code"] == "PREPARATION_DRIFT"
    assert _mutation_calls(fake_c2) == []


async def test_expired_preparation_cannot_execute(fake_c2) -> None:
    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()
    registry, invocation = _invocation(fake_c2)
    invocation.server.preparation_store = PreparationStore(clock=clock)
    prepared = await _prepare_create(registry, invocation)
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    clock.now += 300

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    assert executed.isError is True
    assert _payload(executed)["code"] == "PREPARATION_NOT_FOUND"
    assert _mutation_calls(fake_c2) == []


async def test_preseeded_executing_state_becomes_ambiguous_without_mutation(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    store = invocation.server.preparation_store
    record = await store.get(preparation_id)
    assert record is not None
    await store.begin(record)

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )
    replay = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": preparation_id},
    )

    assert executed.isError is True
    assert _payload(executed)["code"] == "AMBIGUOUS_OUTCOME"
    assert _payload(replay)["code"] == "AMBIGUOUS_OUTCOME"
    assert _mutation_calls(fake_c2) == []


async def test_update_and_stop_are_two_phase_and_replay_safe(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    fake_c2.responses["/api/v1/commands/42"] = _queued_command(status="ONGOING")
    update = await _call(
        registry,
        invocation,
        "tuoni_command_update_prepare",
        {"command_id": 42, "configuration": {"stdin": ["next line"]}},
    )
    assert _payload(update)["data"]["summary"]["configuration_preview"] == {
        "stdin": ["next line"]
    }
    update_id = _payload(update)["data"]["preparation_id"]
    assert _mutation_calls(fake_c2) == []
    update_receipt = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": update_id},
    )
    update_replay = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": update_id},
    )
    assert update_receipt.isError is False
    assert _payload(update_receipt)["data"] == _payload(update_replay)["data"]
    update_posts = [
        call
        for call in fake_c2.calls
        if call[0] == "request_post" and call[1][0] == "/api/v1/commands/42/update"
    ]
    assert len(update_posts) == 1

    stop = await _call(
        registry,
        invocation,
        "tuoni_command_stop_prepare",
        {"command_id": 42},
    )
    stop_id = _payload(stop)["data"]["preparation_id"]
    mutation_count_before_stop = len(_mutation_calls(fake_c2))
    stopped = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": stop_id},
    )
    stop_replay = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": stop_id},
    )
    assert stopped.isError is False
    assert _payload(stopped)["data"] == _payload(stop_replay)["data"]
    stop_puts = [
        call
        for call in fake_c2.calls
        if call[0] == "request_put" and call[1][0] == "/api/v1/commands/42/stop"
    ]
    assert len(stop_puts) == 1
    assert len(_mutation_calls(fake_c2)) == mutation_count_before_stop + 1
    assert call_result_size(stopped) <= MAX_RESULT_BUDGET


async def test_dispatch_mutation_prepares_then_executes_and_replays_once(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    targets = registry.select({Profile.ADMINISTRATION}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }

    prepared = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {
            "name": "tuoni_agent_block",
            "arguments": {"agent_guid": "agent-1"},
        },
        invocation,
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]
    assert prepared.isError is False
    assert _mutation_calls(fake_c2) == []

    executed = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )
    replay = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )
    assert executed.isError is False
    assert _payload(executed)["data"] == _payload(replay)["data"]
    puts = [
        call
        for call in fake_c2.calls
        if call[0] == "request_put"
        and call[1][0] == "/api/v1/agents/agent-1/block"
    ]
    assert len(puts) == 1


async def test_dispatch_command_execute_and_replay_advertise_callable_poll(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    targets = registry.select({Profile.AGENT_OPS}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }
    prepared = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {
            "name": "tuoni_command_prepare",
            "arguments": {
                "agent_guid": "agent-1",
                "template": "template-cmd",
                "configuration": {"command": "whoami /all"},
            },
        },
        invocation,
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]

    executed = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )
    replay = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )

    expected_next = {
        "name": "tuoni_call",
        "arguments": {
            "name": "tuoni_command_get",
            "arguments": {"command_id": 42},
        },
    }
    assert _payload(executed)["next"] == expected_next
    assert _payload(replay)["next"] == expected_next
    fake_c2.responses["/api/v1/commands/42"] = _queued_command(status="SENT")
    followed = await registry.invoke_spec(
        dispatch[expected_next["name"]],
        expected_next["arguments"],
        invocation,
    )
    assert followed.isError is False
    assert _payload(followed)["data"]["command_id"] == 42
    posts = [call for call in fake_c2.calls if call[0] == "request_post"]
    assert len(posts) == 1


async def test_dispatch_mutation_auth_failure_after_boundary_is_not_replayed(
    fake_c2,
) -> None:
    from tuoni.TuoniExceptions import ExceptionTuoniAuthentication

    registry, invocation = _invocation(fake_c2)
    targets = registry.select({Profile.ADMINISTRATION}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }
    prepared = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {
            "name": "tuoni_agent_block",
            "arguments": {"agent_guid": "agent-1"},
        },
        invocation,
    )
    preparation_id = _payload(prepared)["data"]["preparation_id"]

    def ambiguous_put(uri, json_data=None):
        fake_c2._record("request_put", uri, json_data)
        raise ExceptionTuoniAuthentication("response lost after possible acceptance")

    fake_c2.request_put = ambiguous_put
    failed = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )
    replay = await registry.invoke_spec(
        dispatch["tuoni_operation_execute"],
        {"preparation_id": preparation_id},
        invocation,
    )

    assert failed.isError is True
    assert _payload(failed)["code"] == "AMBIGUOUS_OUTCOME"
    assert _payload(replay)["code"] == "AMBIGUOUS_OUTCOME"
    assert len([call for call in fake_c2.calls if call[0] == "request_put"]) == 1
    assert not any(call[0] == "login" for call in fake_c2.calls)


async def test_dispatch_preparation_redacts_sensitive_name_value_pairs(fake_c2) -> None:
    registry, invocation = _invocation(fake_c2)
    targets = registry.select({Profile.ADMINISTRATION}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }

    prepared = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {
            "name": "tuoni_agent_set_custom_property",
            "arguments": {
                "agent_guid": "agent-1",
                "name": "accessToken",
                "value": "generic-summary-secret",
            },
        },
        invocation,
    )
    serialized = prepared.model_dump_json(by_alias=True, exclude_none=True)

    assert prepared.isError is False
    assert "generic-summary-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert _mutation_calls(fake_c2) == []


async def test_dispatch_preparation_shows_bounded_ordinary_values_and_boolean(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    targets = registry.select({Profile.ACCESS}, read_only=False)
    dispatch = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=False)
    }

    prepared = await registry.invoke_spec(
        dispatch["tuoni_call"],
        {
            "name": "tuoni_payload_create",
            "arguments": {
                "template": "payload.windows-x64",
                "listener_id": 17,
                "configuration": {
                    "architecture": "x64",
                    "debug": False,
                    "api_token": "must-not-leak",
                },
                "encrypted": False,
                "name": "reviewable-payload",
            },
        },
        invocation,
    )
    payload = _payload(prepared)
    serialized = prepared.model_dump_json(by_alias=True, exclude_none=True)

    assert prepared.isError is False
    assert payload["data"]["summary"]["arguments"] == {
        "template": "payload.windows-x64",
        "listener_id": 17,
        "configuration": {
            "architecture": "x64",
            "debug": False,
            "api_token": "[REDACTED]",
        },
        "encrypted": False,
        "name": "reviewable-payload",
    }
    assert "must-not-leak" not in serialized
    assert call_result_size(prepared) <= MAX_RESULT_BUDGET
    assert _mutation_calls(fake_c2) == []


async def test_native_preparation_redacts_credentials_embedded_in_review_text(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    prepared = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            "template": "template-cmd",
            "configuration": {
                "command": "tool --password=command-secret --mode inventory"
            },
        },
    )
    payload = _payload(prepared)
    serialized = prepared.model_dump_json(by_alias=True, exclude_none=True)

    assert prepared.isError is False
    assert "command-secret" not in serialized
    assert "--mode inventory" in payload["data"]["summary"]["configuration_preview"]["command"]
    assert "[REDACTED]" in serialized
    assert call_result_size(prepared) <= 6 * 1024


async def test_native_preparation_review_preview_is_structurally_bounded(
    fake_c2,
) -> None:
    registry, invocation = _invocation(fake_c2)
    command = '\\\"' * 10_000
    prepared = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            "template": "template-cmd",
            "configuration": {"command": command},
        },
    )
    payload = _payload(prepared)
    preview = payload["data"]["summary"]["configuration_preview"]["command"]

    assert prepared.isError is False
    assert preview != command
    assert preview.endswith("…")
    assert call_result_size(prepared) <= 6 * 1024
    assert _mutation_calls(fake_c2) == []


@pytest.mark.parametrize("dispatch", [False, True])
async def test_preparation_expiring_while_waiting_for_execution_lock_has_recovery_hint(
    fake_c2, dispatch,
) -> None:
    registry, invocation = _invocation(fake_c2)
    now = [100.0]
    store = PreparationStore(clock=lambda: now[0])
    invocation.server.preparation_store = store
    prepared = await _prepare_create(registry, invocation)
    arguments = _payload(prepared)["next"]["arguments"]
    record = await store.get(arguments["preparation_id"])
    assert record is not None
    spec = registry.get("tuoni_operation_execute")
    if dispatch:
        spec = next(
            item for item in build_dispatch_specs(registry, registry.specs, read_only=False)
            if item.name == "tuoni_operation_execute"
        )
    # Hold the real operation lock until the request has looked up this live
    # record. Expiration after lookup must still have the public recovery code.
    looked_up = asyncio.Event()
    original_get = store.get

    async def observed_get(preparation_id):
        value = await original_get(preparation_id)
        looked_up.set()
        return value

    store.get = observed_get
    async with record.lock:
        task = asyncio.create_task(registry.invoke_spec(spec, arguments, invocation))
        await asyncio.wait_for(looked_up.wait(), 2)
        now[0] += store.ttl_seconds
    result = await asyncio.wait_for(task, 2)

    error = _payload(result)
    assert result.isError
    assert error["code"] == "PREPARATION_NOT_FOUND"
    assert error["retryable"] is False
    assert "do not resubmit" in error["hint"]
    assert _mutation_calls(fake_c2) == []
    assert await store.stats() == (0, 0)


@pytest.mark.parametrize("dispatch", [False, True])
@pytest.mark.parametrize("cancel_kind", ["task", "scope"])
@pytest.mark.parametrize("boundary", ["preflight", "submission", "receipt"])
async def test_cancellation_preserves_execution_boundary_and_never_replays_mutation(
    fake_c2, monkeypatch, dispatch, cancel_kind, boundary,
) -> None:
    import tuoni_mcp.tools.commands as commands_module

    registry, invocation = _invocation(fake_c2)
    prepared = await _prepare_create(registry, invocation)
    arguments = _payload(prepared)["next"]["arguments"]
    store = invocation.server.preparation_store
    record = await store.get(arguments["preparation_id"])
    assert record is not None
    spec = registry.get("tuoni_operation_execute")
    if dispatch:
        spec = next(
            item for item in build_dispatch_specs(registry, registry.specs, read_only=False)
            if item.name == "tuoni_operation_execute"
        )

    entered = asyncio.Event()
    release_receipt = asyncio.Event()
    mutation_count = 0
    original_read = commands_module._read

    async def read(*args):
        if boundary == "preflight":
            entered.set()
            await anyio.sleep_forever()
        return await original_read(*args)

    async def submit(*args):
        nonlocal mutation_count
        mutation_count += 1
        if boundary == "submission":
            entered.set()
            await anyio.sleep_forever()
        return _queued_command()

    original_complete = store.complete

    async def complete(*args):
        if boundary == "receipt":
            entered.set()
            await release_receipt.wait()
        await original_complete(*args)

    monkeypatch.setattr(commands_module, "_read", read)
    monkeypatch.setattr(commands_module, "call_without_retry", submit)
    monkeypatch.setattr(store, "complete", complete)
    scopes = []

    async def execute():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            return await registry.invoke_spec(spec, arguments, invocation)

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel_kind == "task":
            task.cancel()
        else:
            scopes[0].cancel()
        release_receipt.set()
        if cancel_kind == "task":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            assert await asyncio.wait_for(task, 2) is None
    finally:
        release_receipt.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    expected_state = {"preflight": "prepared", "submission": "ambiguous", "receipt": "executed"}
    assert record.state == expected_state[boundary]
    assert mutation_count == (0 if boundary == "preflight" else 1)
    if boundary == "preflight":
        # A canceled preflight never crossed the mutation boundary. The same
        # preparation can still execute, provided live preflight succeeds.
        monkeypatch.setattr(commands_module, "_read", original_read)
    replay = await registry.invoke_spec(spec, arguments, invocation)
    if boundary == "submission":
        assert replay.isError
        assert _payload(replay)["code"] == "AMBIGUOUS_OUTCOME"
    else:
        assert not replay.isError
        assert _payload(replay)["data"]["command_id"] == 42
    assert mutation_count == 1
