"""Continuations must be callable, and shell text must stay flat.

Both target the same weakness: a small model has no reliable multi-step
machinery, so every hop it takes has to work from what the last response said.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode, Profile, Settings, parse_tool_prefix
from tuoni_mcp.protocol import (
    call_result_size,
    exposed_tool_prefix,
    native_name,
    success_result,
)
from tuoni_mcp.server import build_server
from tuoni_mcp.tools.commands import _shell_command_field


def _payload(result) -> dict:
    return json.loads(next(item.text for item in result.content if item.type == "text"))


def _schema(properties: dict, *, required: list[str] | None = None, files: dict | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    if files:
        schema["files"] = files
    return schema


def _template(name: str, schema: dict) -> dict:
    return {
        "id": f"template-{name}",
        "name": name,
        "pluginId": "shelldot.commands.native",
        "qualifiedName": f"shelldot.commands.native:global:{name}",
        "fullyQualifiedName": f"shelldot.commands.native:global:{name}",
        "description": f"Run {name}",
        "configurationSchema": schema,
        "exampleConfigurations": [],
    }


# -- the flat shell path ---------------------------------------------------


def test_command_only_templates_accept_the_flat_form() -> None:
    """Shell templates with one command field accept the flat text form."""

    string = {"type": "string"}
    stdin = {"type": "array", "items": {"type": "string"}}

    cmd = _template("cmd", _schema({"command": string, "stdin": stdin}))
    powerpick = _template("powerpick", _schema({"command": string}))
    run = _template("run", _schema({"cmdline": string, "output": {"type": "boolean"}}, required=["cmdline"]))

    assert _shell_command_field(cmd) == "command"
    assert _shell_command_field(powerpick) == "command"
    assert _shell_command_field(run) == "cmdline"


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        # Also requires credentials.
        ("run-as", _schema({"cmdline": {"type": "string"}, "username": {"type": "string"}, "password": {"type": "string"}}, required=["cmdline", "username", "password"])),
        # Also requires a target host.
        ("remote-exec-winrm", _schema({"target": {"type": "string"}, "command": {"type": "string"}}, required=["target", "command"])),
        # Also requires a file attachment.
        ("execute-assembly", _schema({"parameters": {"type": "array", "items": {"type": "string"}}}, files={"executable": {"required": True}})),
        # No command string at all.
        ("ls", _schema({"dir": {"type": "string"}}, required=["dir"])),
        # An in-memory loader: nothing is marked required in the schema, and the
        # executable file is optional there, but the plugin refuses the command
        # without one. Offering the flat form would prepare a call that can only
        # fail after submission, which burns the preparation as AMBIGUOUS.
        (
            "load-pe",
            _schema(
                {"commandline": {"type": "string"}, "localExecutable": {"type": "string"}},
                files={"executable": {"required": False}},
            ),
        ),
        (
            "load-dll",
            _schema({"methodName": {"type": "string"}}, files={"dll": {"required": False}}),
        ),
    ],
)
def test_templates_needing_more_than_a_command_are_excluded(name: str, schema: dict) -> None:
    assert _shell_command_field(_template(name, schema)) is None


async def test_a_command_only_plugin_template_prepares_from_flat_text(patched_server, fake_c2) -> None:
    template = _template("powerpick", _schema({"command": {"type": "string"}}))
    agent = {
        "guid": "agent-1",
        "metadata": {"hostname": "workstation-1"},
        "active": True,
        "status": "ACTIVE",
        "recentListeners": [],
        "availableCommandTemplates": [template["id"]],
    }
    fake_c2.responses[("GET", "/api/v1/agents/agent-1")] = agent
    fake_c2.responses[("GET", "/api/v1/command-templates")] = [template]

    async with create_connected_server_and_client_session(patched_server.protocol_server) as session:
        described = await session.call_tool(
            "tuoni_command_describe", {"agent_guid": "agent-1", "template": "powerpick"}
        )
        prepared = await session.call_tool(
            "tuoni_command_prepare",
            {
                "agent_guid": "agent-1",
                "template": "powerpick",
                # Embedded quotes are exactly what nesting into JSON corrupts.
                "shell_command": 'Get-Process | Where-Object { $_.Name -eq "explorer" }',
            },
        )

    assert "shell_command" in _payload(described)["data"]["prepare_arguments_example"]
    assert prepared.isError is False
    summary = _payload(prepared)["data"]["summary"]
    # The flat text landed in the template's own command field, quotes intact.
    assert summary["configuration_keys"] == ["command"]
    assert (
        summary["configuration_preview"]["command"]
        == 'Get-Process | Where-Object { $_.Name -eq "explorer" }'
    )
    # Preparation must not have queued anything.
    assert not [call for call in fake_c2.calls if call[0] != "request_get"]


async def test_a_template_needing_more_than_a_command_says_so(patched_server, fake_c2, wide_template) -> None:
    agent, template = wide_template
    fake_c2.responses[("GET", "/api/v1/agents/agent-1")] = agent
    fake_c2.responses[("GET", "/api/v1/command-templates")] = [template]

    async with create_connected_server_and_client_session(patched_server.protocol_server) as session:
        result = await session.call_tool(
            "tuoni_command_prepare",
            {"agent_guid": "agent-1", "template": "jump-service", "shell_command": "whoami"},
        )

    payload = _payload(result)
    assert payload["code"] == "INVALID_ARGUMENTS"
    assert "describe" in payload["hint"].casefold()


# -- client-prefixed continuations -----------------------------------------


def test_prefix_parsing_accepts_a_server_key_and_rejects_junk() -> None:
    assert parse_tool_prefix("tuoni_") == "tuoni_"
    assert parse_tool_prefix(None) == ""
    assert parse_tool_prefix("   ") == ""
    with pytest.raises(ValueError):
        parse_tool_prefix("bad prefix!")


def test_continuations_are_emitted_in_the_clients_vocabulary() -> None:
    with exposed_tool_prefix("tuoni_"):
        result = success_result({"ok": 1}, next_call={"name": "tuoni_command_get", "arguments": {"command_id": 7}})

    assert _payload(result)["next"]["name"] == "tuoni_tuoni_command_get"


def test_the_prefix_round_trips_for_internal_lookup() -> None:
    with exposed_tool_prefix("tuoni_"):
        assert native_name("tuoni_tuoni_command_get") == "tuoni_command_get"
    # Outside a prefixed request the name is untouched.
    assert native_name("tuoni_command_get") == "tuoni_command_get"


def test_without_a_prefix_nothing_changes() -> None:
    result = success_result({"ok": 1}, next_call={"name": "tuoni_command_get", "arguments": {}})

    assert _payload(result)["next"]["name"] == "tuoni_command_get"


@pytest.mark.parametrize("prefix", ["", "tuoni_", "a-very-long-server-key_"])
def test_page_sizing_measures_exactly_what_gets_emitted(prefix: str) -> None:
    """Text windows are sized with pagination.success_payload but returned through
    protocol.success_result. If those disagree by even one byte, a window landing
    on the ceiling is rejected as RESPONSE_TOO_LARGE with no way to recover."""

    from tuoni_mcp.pagination import call_result_size as sizing_measure, success_payload

    next_call = {
        "name": "tuoni_command_get",
        "arguments": {"command_id": 7, "part_index": 0, "offset_chars": 4096, "max_chars": 4096},
    }

    with exposed_tool_prefix(prefix):
        sized = sizing_measure(success_payload({"window": "text"}, next_call=next_call))
        emitted = call_result_size(success_result({"window": "text"}, next_call=next_call))

    assert sized == emitted


async def _prefixed_server(monkeypatch, fake_c2, cfg, mode: MCPMode) -> Settings:
    import time

    from tuoni_mcp import connection, server as server_mod

    async def _login(context):
        context.last_login = time.monotonic()

    monkeypatch.setattr(connection, "login", _login)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda *args, **kwargs: fake_c2)
    monkeypatch.setattr(server_mod, "login", _login)
    return build_server(replace(cfg, tool_prefix="tuoni_", mode=mode))


async def test_a_continuation_names_a_tool_the_client_can_actually_call(
    monkeypatch, fake_c2, cfg
) -> None:
    """The whole point: `next` must be callable after the client's renaming."""

    fake_c2.responses[("GET", "/api/v1/commands/7")] = {
        "id": 7,
        "commandTemplateId": "template-cmd",
        "commandTemplateName": "cmd",
        "status": "SENT",
        "created": "2026-08-01T00:00:00Z",
        "sent": "2026-08-01T00:00:01Z",
        "result": None,
    }
    server = await _prefixed_server(monkeypatch, fake_c2, cfg, MCPMode.NATIVE)

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        exposed = {tool.name for tool in (await session.list_tools()).tools}
        polled = await session.call_tool("tuoni_command_get", {"command_id": 7})

    following = _payload(polled)["next"]["name"]
    assert following == "tuoni_tuoni_command_get"
    # The server still advertises its own names; the client is what renames them.
    assert "tuoni_command_get" in exposed
    assert following == "tuoni_" + "tuoni_command_get"


@pytest.mark.parametrize("mode", [MCPMode.NATIVE, MCPMode.DISPATCH])
async def test_a_windowed_additive_read_applies_the_prefix_exactly_once(
    monkeypatch, fake_c2, cfg, mode: MCPMode
) -> None:
    """tuoni_setting_get measures with the prefixed name and returns its own
    continuation. Reusing the measured copy prefixed it twice, producing a name
    no client exposes and pushing the window past its byte budget."""

    import time

    from tuoni_mcp import connection, server as server_mod

    async def _login(context):
        context.last_login = time.monotonic()

    monkeypatch.setattr(connection, "login", _login)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda *args, **kwargs: fake_c2)
    monkeypatch.setattr(server_mod, "login", _login)

    fake_c2.responses[("GET", "/api/v1/setting/server.big")] = {
        "key": "server.big",
        "value": "v" * 40_000,
    }
    server = build_server(
        replace(cfg, tool_prefix="tuoni_", mode=mode, profiles=frozenset({Profile.ADMINISTRATION}))
    )

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        if mode is MCPMode.NATIVE:
            result = await session.call_tool("tuoni_setting_get", {"key": "server.big"})
        else:
            result = await session.call_tool(
                "tuoni_call", {"name": "tuoni_setting_get", "arguments": {"key": "server.big"}}
            )

    assert result.isError is False, _payload(result)
    following = _payload(result)["next"]
    assert following is not None, "a truncated setting must offer a continuation"
    assert not following["name"].startswith("tuoni_tuoni_tuoni_"), following["name"]
    expected = "tuoni_tuoni_call" if mode is MCPMode.DISPATCH else "tuoni_tuoni_setting_get"
    assert following["name"] == expected


@pytest.mark.parametrize("mode", [MCPMode.NATIVE, MCPMode.DISPATCH])
@pytest.mark.parametrize("prefix", ["", "tuoni_"])
async def test_a_file_parts_download_link_is_callable_in_every_exposure(
    monkeypatch, tmp_path, mode: MCPMode, prefix: str
) -> None:
    """The download call sits inside `data`, so neither the envelope's prefix
    rewriting nor the dispatch wrapper reaches it. The skill tells the model to
    follow it verbatim, so it has to name a tool the client actually exposes."""

    import time

    from evaluation.portable.world import MULTIPART_COMMAND_ID, PortableC2
    from tuoni_mcp import connection, server as server_mod

    world = PortableC2()

    async def _login(context):
        context.last_login = time.monotonic()

    monkeypatch.setattr(connection, "login", _login)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda *args, **kwargs: world)
    monkeypatch.setattr(server_mod, "login", _login)

    server = build_server(
        Settings(
            url="https://example.test:8443",
            username="test",
            password="test",
            verify=False,
            profiles=frozenset({Profile.AGENT_OPS, Profile.ARTIFACTS}),
            mode=mode,
            tool_prefix=prefix,
            download_dir=str(tmp_path),
        )
    )

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        exposed = {tool.name for tool in (await session.list_tools()).tools}
        if mode is MCPMode.NATIVE:
            result = await session.call_tool(
                "tuoni_command_get", {"command_id": MULTIPART_COMMAND_ID}
            )
        else:
            result = await session.call_tool(
                "tuoni_call",
                {"name": "tuoni_command_get", "arguments": {"command_id": MULTIPART_COMMAND_ID}},
            )

    payload = _payload(result)
    parts = payload["data"]["result"]["parts"]
    file_parts = [part for part in parts if part.get("type") == "file"]
    assert file_parts, "the fixture command has a file result part"

    def assert_callable(call: dict | None, where: str) -> None:
        assert call is not None, f"{where} offered no continuation"
        name = call["name"]
        # The client strips its own prefix back off before dispatching.
        stripped = name.removeprefix(prefix) if prefix and name.startswith(prefix) else name
        assert stripped in exposed, f"{where}: {name!r} is not callable in {mode.value}"
        if mode is MCPMode.DISPATCH:
            assert call["arguments"]["name"] in {
                "tuoni_command_file_download",
                "tuoni_command_get",
            }

    assert_callable(file_parts[0]["download"], "embedded file-part download")

    # A file-only result promotes the download to the top-level `next`, which the
    # envelope also renames. That is the shape a second rename would corrupt.
    world.commands[9001] = {
        "id": 9001,
        "commandTemplateName": "screenshot",
        "status": "COMPLETE",
        "created": "2026-08-01T00:00:00Z",
        "sent": "2026-08-01T00:00:01Z",
        "result": {
            "status": "SUCCESS",
            "errorMessage": None,
            "received": "2026-08-01T00:00:05Z",
            "childResults": [
                {"type": "file", "filename": "screen.png", "href": "https://c2/f/0", "uri": "t://0"}
            ],
        },
    }
    async with create_connected_server_and_client_session(server.protocol_server) as session:
        if mode is MCPMode.NATIVE:
            only_file = await session.call_tool("tuoni_command_get", {"command_id": 9001})
        else:
            only_file = await session.call_tool(
                "tuoni_call", {"name": "tuoni_command_get", "arguments": {"command_id": 9001}}
            )

    assert_callable(_payload(only_file)["next"], "file-only top-level next")


async def test_dispatch_still_routes_a_prefixed_continuation(monkeypatch, fake_c2, cfg) -> None:
    fake_c2.responses[("GET", "/api/v1/commands/7")] = {
        "id": 7,
        "commandTemplateName": "cmd",
        "status": "SENT",
        "created": "2026-08-01T00:00:00Z",
        "sent": "2026-08-01T00:00:01Z",
        "result": None,
    }
    server = await _prefixed_server(monkeypatch, fake_c2, cfg, MCPMode.DISPATCH)

    async with create_connected_server_and_client_session(server.protocol_server) as session:
        result = await session.call_tool(
            "tuoni_call", {"name": "tuoni_command_get", "arguments": {"command_id": 7}}
        )

    following = _payload(result)["next"]
    assert following["name"] == "tuoni_tuoni_call"
    # The inner name stays native: it is a dispatch argument, not a tool name.
    assert following["arguments"]["name"] == "tuoni_command_get"
