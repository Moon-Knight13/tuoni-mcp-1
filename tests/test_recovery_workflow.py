"""Exercise recovery through the real MCP protocol and default read-only catalog."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp import server
from tuoni_mcp.config import MCPMode, Profile, Settings


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_recover_existing_id_then_read_manifest_and_text_over_mcp(monkeypatch, mode):
    command = {
        "id": 42, "agentGuid": "a1234567-89ab-cdef-0123-456789abcdef",
        "commandTemplateId": "c1234567-89ab-cdef-0123-456789abcdef",
        "commandTemplateName": "example", "created": "2026-10-05T10:00:00Z",
        "status": "COMPLETE", "result": {"status": "SUCCESS", "childResults": [
            {"type": "text", "name": "stdout", "value": "existing output"},
        ]},
    }
    reads = []
    closed = []

    class ExistingHistory:
        # Deliberately has no mutation APIs or live network access.
        def login(self, *args):
            pass

        def close(self):
            closed.append(True)

        def request_get(self, path):
            reads.append(path)
            parsed = urlsplit(path)
            if parsed.path == "/api/v2/commands":
                assert parse_qs(parsed.query) == {"page": ["0"], "pageSize": ["10"], "sort": ["ID:DESC"]}
                return {"page": 0, "pageSize": 10, "itemCount": 1, "totalCount": 1,
                        "totalPages": 1, "hasMorePages": False, "items": [command]}
            assert path == "/api/v1/commands/42"
            return command

    monkeypatch.setattr(server, "TuoniC2", lambda **kwargs: ExistingHistory())
    cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                   mode=mode, read_only=True)
    mcp = server.build_server(cfg)
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        async def call(name, arguments):
            if mode is MCPMode.DISPATCH:
                arguments, name = {"name": name, "arguments": arguments}, "tuoni_call"
            result = await session.call_tool(name, arguments)
            assert not result.isError
            return json.loads(result.content[0].text)

        capabilities = await call("tuoni_capabilities", {})
        assert capabilities["data"]["backend"]["status"] == "not_checked"
        assert capabilities["data"]["catalog"]["enabled_operations"] == 7
        assert reads == []
        history = await call("tuoni_commands_list", {})
        assert "existing output" not in json.dumps(history)
        command_id = history["data"]["commands"][0]["command_id"]
        manifest = await call("tuoni_command_get", {"command_id": command_id, "wait_seconds": 1})
        next_call = manifest["next"]
        result = await session.call_tool(next_call["name"], next_call["arguments"])
        assert not result.isError
        assert json.loads(result.content[0].text)["data"]["result"]["part"]["value"] == "existing output"
    assert len(reads) == 2  # History plus one result; text reuses the terminal snapshot.
    assert closed == [True]


async def test_cancelled_mcp_scope_still_stops_events_and_closes_http_client(monkeypatch):
    from unittest.mock import MagicMock
    c2 = MagicMock()
    stopped = []

    class Events:
        def __init__(self, ctx):
            self.status = MagicMock(connected=True)

        async def start(self):
            await anyio.sleep_forever()

        async def stop(self):
            await anyio.sleep(0)
            stopped.append(True)

    monkeypatch.setattr(server, "TuoniC2", lambda **kwargs: c2)
    monkeypatch.setattr(server, "EventBridge", Events)
    cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                   profiles=frozenset({Profile.EVENTS}))
    with anyio.CancelScope() as scope:
        async with server._make_lifespan(cfg)(None):
            scope.cancel()
            await anyio.sleep(0)
    assert stopped == [True]
    c2.close.assert_called_once_with()
