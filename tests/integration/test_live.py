from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ListToolsResult

from tuoni_mcp.server import build_server
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, call_result_size

pytestmark = pytest.mark.skipif(
    os.environ.get("TUONI_LIVE") != "1",
    reason="integration tests require TUONI_LIVE=1 and valid TUONI_* env vars",
)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_smoke_against_live_server(mode) -> None:
    cfg = replace(Settings.from_env(), mode=mode, profiles={Profile.AGENT_OPS}, read_only=True, tool_prefix="")
    mcp = build_server(cfg)
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        tools = await session.list_tools()
        names = tuple(tool.name for tool in tools.tools)
        expected = (
            "tuoni_agents_list",
            "tuoni_agent_get",
            "tuoni_commands_search",
            "tuoni_command_describe",
            "tuoni_command_get",
            "tuoni_commands_list",
            "tuoni_capabilities",
        )
        assert names == (expected if mode is MCPMode.NATIVE else ("tuoni_find_tool", "tuoni_tool_schema", "tuoni_call"))

        async def call(name, arguments):
            if mode is MCPMode.DISPATCH:
                return await session.call_tool("tuoni_call", {"name": name, "arguments": arguments})
            return await session.call_tool(name, arguments)
        encoded = ListToolsResult(tools=tools.tools).model_dump_json(
            by_alias=True, exclude_none=True
        ).encode("utf-8")
        assert len(encoded) <= 10 * 1024

        diagnostics = await call("tuoni_capabilities", {"check_connection": True})
        assert diagnostics.isError is False
        diagnostic_data = json.loads(diagnostics.content[0].text)["data"]
        assert diagnostic_data["ok"] is True
        assert diagnostic_data["backend"]["status"] == "ok"

        agents_resp = await call("tuoni_agents_list", {"limit": 10})
        assert agents_resp.isError is False
        text = next(content.text for content in agents_resp.content if content.type == "text")
        envelope = json.loads(text)
        assert set(envelope) == {"ok", "data", "next_cursor", "next"}
        assert envelope["ok"] is True
        agents = envelope["data"]["agents"]
        assert isinstance(agents, list)

        # Keep the live test observational. If an active agent exists, exercise
        # the cache-backed agent-scoped discovery path without preparing or
        # executing any command.
        if agents:
            agent_guid = agents[0]["guid"]
            history_resp = await call("tuoni_commands_list", {"agent_guid": agent_guid, "limit": 2})
            assert history_resp.isError is False
            assert call_result_size(history_resp) <= LIST_RESULT_BUDGET
            history = json.loads(history_resp.content[0].text)["data"]["commands"]
            assert all(item["agent_guid"] == agent_guid for item in history)
            assert all("configuration" not in item and "result" not in item for item in history)
            search_resp = await call(
                "tuoni_commands_search",
                {"agent_guid": agent_guid, "query": "", "limit": 8},
            )
            assert search_resp.isError is False
            search_text = next(
                content.text for content in search_resp.content if content.type == "text"
            )
            search = json.loads(search_text)
            commands = search["data"]["commands"]
            assert isinstance(commands, list)
            if commands:
                describe_resp = await call(
                    "tuoni_command_describe",
                    {
                        "agent_guid": agent_guid,
                        "template": commands[0]["id"],
                        "limit": 8,
                    },
                )
                assert describe_resp.isError is False
                describe_text = next(
                    content.text
                    for content in describe_resp.content
                    if content.type == "text"
                )
                describe = json.loads(describe_text)
                assert describe["data"]["template"]["id"] == commands[0]["id"]


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize(
    ("name", "field"),
    [("tuoni_listener_plugins_list", "plugins"), ("tuoni_jobs_list", "jobs")],
)
async def test_live_optional_lists_have_callable_bounded_pages(mode, name, field) -> None:
    """Large plugin schemas and job logs must not make inventory unreadable."""
    cfg = replace(
        Settings.from_env(), mode=mode, profiles={Profile.ACCESS, Profile.ADMINISTRATION},
        read_only=True, tool_prefix="",
    )
    mcp = build_server(cfg)
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        catalog = {tool.name for tool in (await session.list_tools()).tools}
        following = {"name": name, "arguments": {"limit": 2}}
        if mode is MCPMode.DISPATCH:
            following = {"name": "tuoni_call", "arguments": following}
        seen_calls = set()
        for _ in range(3):
            assert following["name"] in catalog
            call_key = json.dumps(following, sort_keys=True)
            assert call_key not in seen_calls, "The list continuation repeated a previous call"
            seen_calls.add(call_key)
            response = await session.call_tool(following["name"], following["arguments"])
            assert response.isError is False
            assert call_result_size(response) <= LIST_RESULT_BUDGET
            payload = json.loads(response.content[0].text)
            assert isinstance(payload["data"][field], list)
            following = payload["next"]
            if following is None:
                break
