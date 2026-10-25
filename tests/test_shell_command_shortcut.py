from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.registry import InvocationContext
from tuoni_mcp.tools import build_registry


def _settings() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )


def _shell_template(name: str) -> dict:
    return {
        "id": f"template-{name}",
        "name": name,
        "pluginId": "shell.commands",
        "scope": "global",
        "qualifiedName": f"shell.commands:global:{name}",
        "fullyQualifiedName": f"shell.commands:global:{name}",
        "description": f"Run {name}",
        "supportedExecUnitTypes": ["SHELLCODE_NATIVE"],
        "configurationSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "minLength": 1}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "updateSchema": None,
        "exampleConfigurations": [],
    }


def _invocation(fake_c2, shell: str):
    cfg = _settings()
    template = _shell_template(shell)
    fake_c2.responses["/api/v1/agents/agent-1"] = {
        "guid": "agent-1",
        "active": True,
        "status": "ACTIVE",
        "availableCommandTemplates": [template["id"]],
    }
    fake_c2.responses["/api/v1/command-templates"] = [template]
    fake_c2.responses[("POST", "/api/v1/agents/agent-1/commands")] = {
        "id": 42,
        "commandTemplateId": template["id"],
        "commandTemplateName": shell,
        "status": "QUEUED",
        "created": "2026-07-21T00:00:00Z",
        "sent": None,
        "result": None,
    }
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


@pytest.mark.parametrize("shell", ("cmd", "powershell", "sh"))
async def test_flat_shell_command_is_normalized_and_preserves_embedded_quotes(
    fake_c2,
    shell: str,
) -> None:
    registry, invocation = _invocation(fake_c2, shell)
    command = 'for %F in ("C:\\Program Files\\*") do @echo "%~fF"'

    prepared = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            "template": shell,
            "shell_command": command,
        },
    )

    assert prepared.isError is False
    prepared_payload = _payload(prepared)
    assert prepared_payload["data"]["summary"]["configuration_keys"] == ["command"]
    assert prepared_payload["data"]["summary"]["configuration_preview"] == {
        "command": command
    }

    executed = await _call(
        registry,
        invocation,
        "tuoni_operation_execute",
        {"preparation_id": prepared_payload["data"]["preparation_id"]},
    )

    assert executed.isError is False
    posts = [call for call in fake_c2.calls if call[0] == "request_post"]
    assert len(posts) == 1
    assert posts[0][1][1]["configuration"] == {"command": command}


@pytest.mark.parametrize("shell", ("cmd", "powershell", "sh"))
async def test_shell_prepare_rejects_both_configuration_and_shell_command_before_c2(
    fake_c2,
    shell: str,
) -> None:
    registry, invocation = _invocation(fake_c2, shell)

    result = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {
            "agent_guid": "agent-1",
            "template": shell,
            "configuration": {"command": "whoami"},
            "shell_command": 'dir "C:\\Program Files"',
        },
    )

    assert result.isError is True
    error = _payload(result)
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["retryable"] is False
    assert fake_c2.calls == []


@pytest.mark.parametrize("shell", ("cmd", "powershell", "sh"))
async def test_shell_prepare_rejects_missing_configuration_and_shell_command_before_c2(
    fake_c2,
    shell: str,
) -> None:
    registry, invocation = _invocation(fake_c2, shell)

    result = await _call(
        registry,
        invocation,
        "tuoni_command_prepare",
        {"agent_guid": "agent-1", "template": shell},
    )

    assert result.isError is True
    error = _payload(result)
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["retryable"] is False
    assert fake_c2.calls == []


@pytest.mark.parametrize("shell", ("cmd", "powershell", "sh"))
async def test_shell_describe_example_uses_flat_shell_command(fake_c2, shell: str) -> None:
    registry, invocation = _invocation(fake_c2, shell)

    result = await _call(
        registry,
        invocation,
        "tuoni_command_describe",
        {"agent_guid": "agent-1", "template": shell},
    )

    assert result.isError is False
    example = _payload(result)["data"]["prepare_arguments_example"]
    assert example["agent_guid"] == "agent-1"
    assert example["template"] == shell
    assert isinstance(example["shell_command"], str)
    assert example["shell_command"]
    assert "configuration" not in example
