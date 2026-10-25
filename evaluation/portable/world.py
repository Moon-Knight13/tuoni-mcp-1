"""A deterministic Tuoni fixture world for the portable evaluation.

Template names, field names and plugin IDs mirror the real registrations in
``tuoni-server`` so a workflow that passes here is exercising the vocabulary an
operator actually meets. Only the transport is fake.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

AGENT_GUID = "benchmark-agent-0001"
AGENT_HINT = "workstation-benchmark"
SECOND_AGENT_GUID = "benchmark-agent-0002"

# Fixture command IDs, one per result-reading scenario.
PENDING_COMMAND_ID = 1001
MULTIPART_COMMAND_ID = 1002
UNICODE_COMMAND_ID = 1003
LARGE_RESULT_COMMAND_ID = 1004
IDENTITY_COMMAND_ID = 1005
PROCESS_COMMAND_ID = 1006
INTERACTIVE_COMMAND_ID = 1007
STOPPABLE_COMMAND_ID = 1008
FIRST_CREATED_COMMAND_ID = 2001

SAFE_ARGUMENT_VALUE = "C:\\Users\\Public"
NATIVE_PLUGIN = "shelldot.commands.native"
OS_PLUGIN = "shelldot.commands.os"
FS_PLUGIN = "shelldot.commands.fs"
NET_PLUGIN = "shelldot.commands.net"


def _schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    if files:
        schema["files"] = files
    return schema


_STDIN = {"type": "array", "items": {"type": "string"}, "description": "Lines written to stdin."}


def _template(
    name: str,
    *,
    plugin_id: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
    files: dict[str, Any] | None = None,
    update_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template: dict[str, Any] = {
        "id": f"template-{name}",
        "name": name,
        "pluginId": plugin_id,
        "scope": "global",
        "qualifiedName": f"{plugin_id}:global:{name}",
        "fullyQualifiedName": f"{plugin_id}:global:{name}",
        "description": description,
        "configurationSchema": _schema(properties, required=required, files=files),
        "supportedExecUnitTypes": ["SHELLCODE_NATIVE", "DOTNET_EXE"],
        "defaultConfiguration": None,
        "exampleConfigurations": [],
    }
    if update_properties is not None:
        template["updateSchema"] = _schema(update_properties)
    return template


def build_templates() -> list[dict[str, Any]]:
    """Return the fixture catalog, mirroring real Tuoni template registrations."""

    return [
        _template(
            "cmd",
            plugin_id=NATIVE_PLUGIN,
            description="Run a command in cmd.exe on Windows.",
            properties={
                "command": {"type": "string", "description": "Command line to run."},
                "stdin": _STDIN,
                "outputEncoding": {"type": "string"},
                "outputUnicode": {"type": "boolean"},
            },
            update_properties={"stdin": _STDIN},
        ),
        _template(
            "powershell",
            plugin_id=NATIVE_PLUGIN,
            description="Run a command in PowerShell on Windows.",
            properties={
                "command": {"type": "string", "description": "PowerShell command to run."},
                "stdin": _STDIN,
                "outputEncoding": {"type": "string"},
            },
            update_properties={"stdin": _STDIN},
        ),
        _template(
            "sh",
            plugin_id=NATIVE_PLUGIN,
            description="Run a command in sh on Linux, BSD or macOS.",
            properties={
                "command": {"type": "string", "description": "Shell command to run."},
                "stdin": _STDIN,
            },
            update_properties={"stdin": _STDIN},
        ),
        _template(
            "ps",
            plugin_id=NATIVE_PLUGIN,
            description="List the processes running on the target.",
            properties={},
        ),
        _template(
            "ls",
            plugin_id=NATIVE_PLUGIN,
            description="List a directory using the native filesystem API.",
            properties={
                "dir": {"type": "string", "description": "Directory to list."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            required=["dir"],
        ),
        _template(
            "run",
            plugin_id=NATIVE_PLUGIN,
            description="Run an executable with arguments.",
            properties={
                "cmdline": {"type": "string", "description": "Command line used to run the executable."},
                "output": {"type": "boolean"},
                "stdin": _STDIN,
                "unicode": {"type": "boolean"},
                "outputEncoding": {"type": "string"},
            },
            required=["cmdline"],
        ),
        _template(
            "run-as",
            plugin_id=NATIVE_PLUGIN,
            description="Run an executable as another user.",
            properties={
                "cmdline": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "output": {"type": "boolean"},
            },
            required=["cmdline", "username", "password"],
        ),
        _template(
            "procinfo",
            plugin_id=OS_PLUGIN,
            description="Report information about the agent's own process.",
            properties={},
        ),
        _template(
            "screenshot",
            plugin_id=OS_PLUGIN,
            description="Capture a screenshot of the target desktop.",
            properties={},
        ),
        _template(
            "download",
            plugin_id=FS_PLUGIN,
            description="Read a file on the target and download it to the C2.",
            properties={"filepath": {"type": "string", "description": "Path of the file to download."}},
            required=["filepath"],
        ),
        _template(
            "portscan",
            plugin_id=NET_PLUGIN,
            description="Scan the network for open ports.",
            properties={
                "ips": {"type": "string", "description": "Addresses or ranges to scan."},
                "ports": {"type": "string", "description": "Ports or port ranges to scan."},
                "timeout": {"type": "integer"},
                "threads": {"type": "integer"},
            },
            required=["ips", "ports"],
        ),
        _template(
            "execute-assembly",
            plugin_id=OS_PLUGIN,
            description="Execute a C# assembly in memory.",
            properties={"parameters": {"type": "array", "items": {"type": "string"}}},
            files={"executable": {"required": True, "description": "C# executable to run."}},
        ),
    ]


def _text_part(name: str, value: str) -> dict[str, Any]:
    return {"type": "text", "name": name, "value": value}


def _result(*parts: dict[str, Any], status: str = "SUCCESS") -> dict[str, Any]:
    return {
        "status": status,
        "errorMessage": None,
        "received": "2026-08-01T00:00:05Z",
        "childResults": list(parts),
    }


IDENTITY_OUTPUT = (
    "BENCHBOX\\operator\r\n\r\n"
    "USER INFORMATION\r\n----------------\r\n"
    "User Name        SID\r\n"
    "benchbox\\operator  S-1-5-21-1004336348-1177238915-682003330-1013\r\n"
)

PROCESS_OUTPUT = "\r\n".join(
    ["PID   PPID  NAME", "4     0     System", "512   4     smss.exe", "884   512   explorer.exe", "1220  884   agent.exe"]
)

# Long enough to require several bounded windows, with astral and combining
# characters at a boundary so offset continuation is genuinely exercised.
UNICODE_OUTPUT = ("caf\u00e9 e\u0301 \U0001f600 \u4f60\u597d " + "0123456789" * 400) * 3

LARGE_OUTPUT = "\r\n".join(f"line {index:05d} evidence-token-{index % 7}" for index in range(1, 900))


def _command(
    command_id: int,
    *,
    template: str,
    status: str,
    configuration: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": command_id,
        "commandTemplateId": f"template-{template}",
        "commandTemplateName": template,
        "configuration": configuration or {},
        "execConf": {},
        "status": status,
        "created": "2026-08-01T00:00:00Z",
        "sent": "2026-08-01T00:00:01Z",
        "result": result,
    }


def build_commands() -> dict[int, dict[str, Any]]:
    """Return the fixture command store keyed by command ID."""

    return {
        PENDING_COMMAND_ID: _command(PENDING_COMMAND_ID, template="ps", status="SENT"),
        MULTIPART_COMMAND_ID: _command(
            MULTIPART_COMMAND_ID,
            template="cmd",
            status="COMPLETE",
            configuration={"command": "systeminfo"},
            result=_result(
                _text_part("stdout", IDENTITY_OUTPUT),
                _text_part("stderr", ""),
                {
                    "type": "file",
                    "filename": "systeminfo.txt",
                    "href": "https://c2.internal:8443/api/v1/commands/1002/files/0",
                    "uri": "tuoni://files/1002/0",
                },
            ),
        ),
        UNICODE_COMMAND_ID: _command(
            UNICODE_COMMAND_ID,
            template="sh",
            status="COMPLETE",
            configuration={"command": "cat notes.txt"},
            result=_result(_text_part("stdout", UNICODE_OUTPUT)),
        ),
        LARGE_RESULT_COMMAND_ID: _command(
            LARGE_RESULT_COMMAND_ID,
            template="ls",
            status="COMPLETE",
            configuration={"dir": SAFE_ARGUMENT_VALUE},
            result=_result(_text_part("stdout", LARGE_OUTPUT)),
        ),
        IDENTITY_COMMAND_ID: _command(
            IDENTITY_COMMAND_ID,
            template="cmd",
            status="COMPLETE",
            configuration={"command": "whoami /all"},
            result=_result(_text_part("stdout", IDENTITY_OUTPUT)),
        ),
        PROCESS_COMMAND_ID: _command(
            PROCESS_COMMAND_ID,
            template="ps",
            status="COMPLETE",
            result=_result(_text_part("stdout", PROCESS_OUTPUT)),
        ),
        INTERACTIVE_COMMAND_ID: _command(
            INTERACTIVE_COMMAND_ID,
            template="cmd",
            status="ONGOING",
            configuration={"command": "cmd"},
        ),
        STOPPABLE_COMMAND_ID: _command(STOPPABLE_COMMAND_ID, template="portscan", status="SENT"),
    }


# A real agent only advertises the commands its platform can run, so the fixture
# does the same. Handing a Windows agent `sh` would let a workflow "succeed" by
# picking a template no real Windows host would offer.
WINDOWS_TEMPLATES = (
    "cmd",
    "powershell",
    "ps",
    "ls",
    "run",
    "run-as",
    "procinfo",
    "screenshot",
    "download",
    "portscan",
    "execute-assembly",
)
LINUX_TEMPLATES = ("sh", "ps", "ls", "run", "procinfo", "download", "portscan")


def build_agents(template_ids: list[str]) -> list[dict[str, Any]]:
    available = set(template_ids)

    def for_platform(names: tuple[str, ...]) -> list[str]:
        return [f"template-{name}" for name in names if f"template-{name}" in available]

    return [
        {
            "guid": AGENT_GUID,
            "firstRegistrationTime": "2026-07-31T22:00:00Z",
            "lastCallbackTime": "2026-08-01T00:00:00Z",
            "metadata": {
                "hostname": AGENT_HINT,
                "username": "operator",
                "os": "WINDOWS",
                "osArch": "X64",
                "processArch": "X64",
                "integrity": "medium",
            },
            "active": True,
            "status": "ACTIVE",
            "recentListeners": [{"id": 1, "name": "http-primary"}],
            "availableCommandTemplates": for_platform(WINDOWS_TEMPLATES),
        },
        {
            "guid": SECOND_AGENT_GUID,
            "firstRegistrationTime": "2026-07-31T23:00:00Z",
            "lastCallbackTime": "2026-08-01T00:00:00Z",
            "metadata": {
                "hostname": "buildserver-02",
                "username": "svc_build",
                "os": "LINUX",
                "osArch": "X64",
                "processArch": "X64",
            },
            "active": True,
            "status": "ACTIVE",
            "recentListeners": [],
            "availableCommandTemplates": for_platform(LINUX_TEMPLATES),
        },
    ]


class PortableC2:
    """Deterministic stand-in for ``TuoniC2`` that answers by parsed path."""

    def __init__(self) -> None:
        self._token = "portable-token"
        self._url = "https://portable.test:8443"
        self.calls: list[tuple[str, str]] = []
        self.templates = build_templates()
        self.commands = build_commands()
        self.agents = build_agents([item["id"] for item in self.templates])
        self._next_command_id = FIRST_CREATED_COMMAND_ID

    # -- bookkeeping -------------------------------------------------------

    @property
    def mutating_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def _record(self, method: str, path: str) -> None:
        self.calls.append((method, path))

    def _agent(self, guid: str) -> dict[str, Any] | None:
        return next((item for item in self.agents if item["guid"] == guid), None)

    # -- TuoniC2 surface ---------------------------------------------------

    def login(self, url: str, username: str, password: str) -> None:
        self._token = "logged-in"

    def request_get(self, uri: str, result_as_json: bool = True, result_as_bytes: bool = False) -> Any:
        self._record("GET", uri)
        split = urlsplit(uri)
        path = split.path
        query = parse_qs(split.query)

        if path == "/api/v2/agents":
            return self._agents_page(query)
        if path.startswith("/api/v1/agents/"):
            return copy.deepcopy(self._agent(path.rsplit("/", 1)[-1]))
        if path == "/api/v1/command-templates":
            return copy.deepcopy(self.templates)
        if path.startswith("/api/v1/commands/"):
            tail = path[len("/api/v1/commands/"):]
            if tail.isdigit():
                return copy.deepcopy(self.commands.get(int(tail)))
        if result_as_bytes:
            return b"portable-fixture-bytes"
        return None

    def _agents_page(self, query: dict[str, list[str]]) -> dict[str, Any]:
        page = int(query.get("page", ["0"])[0])
        page_size = max(1, int(query.get("pageSize", ["10"])[0]))
        status = query.get("status", [None])[0]
        items = [
            item
            for item in self.agents
            if status is None or str(item.get("status")) == status
        ]
        start = page * page_size
        window = items[start : start + page_size]
        return {
            "items": copy.deepcopy(window),
            "page": page,
            "pageSize": page_size,
            "itemCount": len(window),
            "totalCount": len(items),
            "totalPages": max(1, (len(items) + page_size - 1) // page_size),
            "hasMorePages": start + page_size < len(items),
        }

    def request_post(self, uri: str, json_data: dict | None = None, files: dict | None = None) -> Any:
        self._record("POST", uri)
        split = urlsplit(uri)
        path = split.path
        if path.endswith("/commands") and path.startswith("/api/v1/agents/"):
            return self._create_command(json_data or {})
        if path.startswith("/api/v1/commands/") and path.endswith("/update"):
            command_id = int(path.split("/")[4])
            command = self.commands.get(command_id)
            if command is not None:
                command.setdefault("updates", []).append(json.loads(json.dumps(json_data or {}, default=str)))
            return {"updated": True}
        return {"ok": True}

    def _create_command(self, body: dict[str, Any]) -> dict[str, Any]:
        command_id = self._next_command_id
        self._next_command_id += 1
        template_id = str(body.get("template") or "")
        template = next(
            (item for item in self.templates if item["id"] == template_id or item["name"] == template_id),
            None,
        )
        name = template["name"] if template else "cmd"
        created = _command(
            command_id,
            template=name,
            status="COMPLETE",
            configuration=body.get("configuration") or {},
            result=_result(_text_part("stdout", IDENTITY_OUTPUT)),
        )
        self.commands[command_id] = created
        return copy.deepcopy(created)

    def request_put(self, uri: str, json_data: dict | None = None) -> Any:
        self._record("PUT", uri)
        split = urlsplit(uri)
        if split.path.startswith("/api/v1/commands/") and split.path.endswith("/stop"):
            command_id = int(split.path.split("/")[4])
            command = self.commands.get(command_id)
            if command is not None:
                command["status"] = "CANCELED"
            return {"stopped": True}
        return {"ok": True}

    def request_patch(self, uri: str, json_data: dict | None = None, files: dict | None = None) -> Any:
        self._record("PATCH", uri)
        return {"ok": True}

    def request_delete(self, uri: str, json_data: dict | None = None) -> Any:
        self._record("DELETE", uri)
        return {"ok": True}
