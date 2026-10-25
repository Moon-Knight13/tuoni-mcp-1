from __future__ import annotations

import os
from typing import Any

import pytest

# Provide harmless defaults for required env vars so Settings.from_env() works in unit tests.
os.environ.setdefault("TUONI_URL", "https://example.test:8443")
os.environ.setdefault("TUONI_USERNAME", "test")
os.environ.setdefault("TUONI_PASSWORD", "test")


class FakeC2:
    """Minimal stand-in for TuoniC2 used in unit tests."""

    def __init__(self) -> None:
        self._token = "fake-token"
        self._url = "https://example.test:8443"
        self.calls: list[tuple[str, tuple, dict]] = []
        self.responses: dict[str, Any] = {}

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def login(self, url: str, username: str, password: str) -> None:
        self._record("login", url, username, password)
        self._token = "logged-in"

    def request_get(self, uri: str, result_as_json: bool = True, result_as_bytes: bool = False):
        self._record("request_get", uri, result_as_json, result_as_bytes)
        return self.responses.get(("GET", uri), self.responses.get(uri, None))

    def request_post(self, uri: str, json_data: dict | None = None, files: dict | None = None):
        self._record("request_post", uri, json_data, files)
        return self.responses.get(("POST", uri), self.responses.get(uri, {"ok": True}))

    def request_put(self, uri: str, json_data: dict | None = None):
        self._record("request_put", uri, json_data)
        return self.responses.get(("PUT", uri), self.responses.get(uri, {"ok": True}))

    def request_patch(self, uri: str, json_data: dict | None = None, files: dict | None = None):
        self._record("request_patch", uri, json_data, files)
        return self.responses.get(("PATCH", uri), self.responses.get(uri, {"ok": True}))

    def request_delete(self, uri: str, json_data: dict | None = None):
        self._record("request_delete", uri, json_data)
        return self.responses.get(("DELETE", uri), self.responses.get(uri, {"ok": True}))

    def load_agents(self, active: bool = True, unactive: bool = False):
        self._record("load_agents", active, unactive)
        return self.responses.get("load_agents", [])

    def load_listeners(self):
        self._record("load_listeners")
        return self.responses.get("load_listeners", [])

    def load_payloads(self, include_non_active: bool = False):
        self._record("load_payloads", include_non_active)
        return self.responses.get("load_payloads", [])

    def load_payload(self, payload_id: int):
        self._record("load_payload", payload_id)
        return self.responses.get(("load_payload", payload_id))

    def load_hosted(self):
        self._record("load_hosted")
        return self.responses.get("load_hosted", [])

    def load_users(self):
        self._record("load_users")
        return self.responses.get("load_users", [])

    def load_command_templates(self):
        self._record("load_command_templates")
        return self.responses.get("load_command_templates", [])

    def load_settings(self):
        self._record("load_settings")
        return self.responses.get("load_settings", {"server": {}, "plugin": {}})

    def load_aliases(self):
        self._record("load_aliases")
        return self.responses.get("load_aliases", [])

    def load_jobs(self, *args, **kwargs):
        self._record("load_jobs", *args, **kwargs)
        return self.responses.get("load_jobs", [])

    def load_datamodel_hosts(self, page=0, pageSize=256, filter=None):
        self._record("load_datamodel_hosts", page, pageSize, filter)
        return self.responses.get("load_datamodel_hosts", [])

    def load_datamodel_services(self, page=0, pageSize=256, filter=None):
        self._record("load_datamodel_services", page, pageSize, filter)
        return self.responses.get("load_datamodel_services", [])

    def load_datamodel_credentials(self, page=0, pageSize=256, filter=None):
        self._record("load_datamodel_credentials", page, pageSize, filter)
        return self.responses.get("load_datamodel_credentials", [])

    def load_listener_plugins(self):
        self._record("load_listener_plugins")
        return self.responses.get("load_listener_plugins", {})

    def load_payload_plugins(self):
        self._record("load_payload_plugins")
        return self.responses.get("load_payload_plugins", {})

    def load_command_plugins(self):
        self._record("load_command_plugins")
        return self.responses.get("load_command_plugins", {})


@pytest.fixture
def fake_c2() -> FakeC2:
    return FakeC2()


@pytest.fixture
def cfg():
    from tuoni_mcp.config import Profile, Settings

    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )


@pytest.fixture
def patched_server(monkeypatch, fake_c2, cfg):
    """The real in-memory MCP transport backed by a FakeC2 lifespan context."""
    import time

    from tuoni_mcp import connection, server as server_mod

    login_calls = []

    async def _fake_login(context):
        context.last_login = time.monotonic()
        login_calls.append(context)

    monkeypatch.setattr(connection, "login", _fake_login)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda *args, **kwargs: fake_c2)
    monkeypatch.setattr(server_mod, "login", _fake_login)
    server = server_mod.build_server(cfg)
    server.smoke_login_calls = login_calls
    return server


@pytest.fixture
def wide_template() -> tuple[dict[str, Any], dict[str, Any]]:
    """An agent plus a many-field template, mirroring the real `jump-service`.

    Wide templates are where a validation hint is most likely to overflow, so
    they are the case worth testing.
    """
    properties = {
        name: {"type": "string"}
        for name in (
            "target",
            "payloadId",
            "copyType",
            "copyPath",
            "servicePath",
            "serviceName",
            "serviceDisplayName",
            "cleanup",
            "username",
            "password",
        )
    }
    template = {
        "id": "template-jump-service",
        "name": "jump-service",
        "pluginId": "shelldot.commands.os",
        "scope": "global",
        "qualifiedName": "shelldot.commands.os:global:jump-service",
        "fullyQualifiedName": "shelldot.commands.os:global:jump-service",
        "description": "Lateral movement through the remote service manager.",
        "configurationSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": properties,
            "required": ["target"],
            "additionalProperties": False,
        },
        "supportedExecUnitTypes": ["SHELLCODE_NATIVE"],
        "exampleConfigurations": [],
    }
    agent = {
        "guid": "agent-1",
        "firstRegistrationTime": "2026-07-20T00:00:00Z",
        "lastCallbackTime": "2026-07-20T00:00:01Z",
        "metadata": {"hostname": "workstation-1", "os": "WINDOWS"},
        "active": True,
        "status": "ACTIVE",
        "recentListeners": [],
        "availableCommandTemplates": [template["id"]],
    }
    return agent, template
