"""Validate the synthetic backend before attributing model failures to MCP."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from evaluation.extended.cases import CASES, goal_checks
from evaluation.extended.world import ExtendedC2, SyntheticBridge
from tuoni_mcp.config import ALL_EXPANSION, MCPMode, Profile
from tuoni_mcp.protocol import MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import Effect
from tuoni_mcp.server import build_server
from tuoni_mcp.tools import build_registry


def test_extended_cases_cover_every_non_raw_operation(cfg):
    registry = build_registry(replace(cfg, profiles=ALL_EXPANSION))
    enabled = {spec.name for spec in registry.select(ALL_EXPANSION, read_only=False)}
    planned = {name for case in CASES.values() for name, _ in case.steps} | {"tuoni_operation_execute"}
    assert planned == enabled


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("case_name", list(CASES))
async def test_extended_workflow_fixture(patched_server, monkeypatch, cfg, tmp_path, mode, case_name):
    from tuoni_mcp import server as server_mod

    case = CASES[case_name]
    world = ExtendedC2(fault=case.fault)
    monkeypatch.setattr(server_mod, "TuoniC2", lambda **kwargs: world)
    monkeypatch.setattr(server_mod, "EventBridge", SyntheticBridge)
    mcp = build_server(replace(cfg, mode=mode, profiles=frozenset(Profile(p) for p in case.profiles),
                               download_dir=str(tmp_path / "downloads")))
    events_cursor = None
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        async def invoke(name, arguments):
            if mode is MCPMode.DISPATCH and name != "tuoni_operation_execute":
                name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
            result = await session.call_tool(name, arguments)
            assert call_result_size(result) <= MAX_RESULT_BUDGET
            payload = json.loads(result.content[0].text)
            assert not result.isError, payload
            return payload

        for name, arguments in case.steps:
            payload = await invoke(name, arguments)
            if name == "tuoni_events_poll":
                events_cursor = payload["next_cursor"]
            effect = mcp.registry.get(name).effect
            if effect is Effect.PREPARE or (effect is Effect.MUTATE and mode is MCPMode.DISPATCH):
                if case.fault:
                    result = await session.call_tool(**payload["next"])
                    assert result.isError
                    assert json.loads(result.content[0].text)["code"] == "AMBIGUOUS_OUTCOME"
                    continue
                await invoke("tuoni_operation_execute", payload["next"]["arguments"])
            if case_name == "result_windows":
                for _ in range(40):
                    if not payload.get("next"):
                        break
                    result = await session.call_tool(**payload["next"])
                    assert not result.isError
                    payload = json.loads(result.content[0].text)
                else:
                    pytest.fail("Fixture result pagination did not finish")
        if events_cursor:
            payload = await invoke("tuoni_events_poll", {"cursor": events_cursor})
            assert any(e["data"].get("command_id") == 2001 for e in payload["data"]["events"])
        if case.fault:
            await invoke("tuoni_commands_list", {})
            await invoke("tuoni_command_get", {"command_id": 2001, "part_index": 0})
    checks = goal_checks(case_name, json.loads(json.dumps(world.snapshot())))
    assert all(checks.values()), checks
