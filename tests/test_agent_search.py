"""Bounded identity search uses paged inventories without exposing unrelated metadata."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tuoni_mcp.config import MCPMode
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools.agents import _agent_summary


@pytest.fixture(params=[(mode, prefix) for mode in MCPMode for prefix in ("", "client_")])
def interface(request):
    return request.param


def _harness(fake_c2, cfg, interface):
    mode, prefix = interface
    settings = replace(cfg, mode=mode, tool_prefix=prefix, read_only=True)
    registry = build_registry(settings)
    server = ServerContext(c2=fake_c2, cfg=settings, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(settings.profiles, read_only=True)
    specs = {spec.name: spec for spec in (
        selected if mode is MCPMode.NATIVE else
        build_dispatch_specs(registry, selected, read_only=True)
    )}

    async def invoke(arguments=None, *, name="tuoni_agents_list", continuation=None):
        if continuation is not None:
            name = continuation["name"].removeprefix(prefix)
            arguments = continuation["arguments"]
        elif mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments or {}}
        result = await registry.invoke_spec(specs[name], arguments or {}, invocation)
        budget = DETAIL_RESULT_BUDGET if name == "tuoni_agent_get" else LIST_RESULT_BUDGET
        assert call_result_size(result) <= budget
        return result, json.loads(result.content[0].text)

    return invoke


def _agent(index=1, **metadata):
    return {
        "guid": f"agent-{index}", "status": "ACTIVE", "active": True,
        "lastCallbackTime": "2026-10-23T22:16:18Z",
        "metadata": {
            "hostname": f"host-{index}", "username": "operator", "os": "WINDOWS",
            "osArch": "X64", "processArch": "X64", **metadata,
        },
    }


def _paged_inventory(fake_c2, agents):
    requested = []

    def get(uri, result_as_json=True, result_as_bytes=False):
        fake_c2._record("request_get", uri, result_as_json, result_as_bytes)
        params = parse_qs(urlsplit(uri).query)
        page, size = int(params["page"][0]), int(params["pageSize"][0])
        requested.append((page, size))
        assert urlsplit(uri).path == "/api/v2/agents"
        assert set(params) <= {"page", "pageSize", "status"}
        items = agents[page * size:(page + 1) * size]
        return {"page": page, "pageSize": size, "items": items,
                "totalCount": len(agents), "hasMorePages": (page + 1) * size < len(agents)}

    fake_c2.request_get = get
    return requested


async def test_typed_process_filter_matches_basename_without_matching_hostnames(fake_c2, cfg, interface):
    agents = [
        _agent(0, hostname="sample-agent.exe", proc="other.exe"),
        _agent(1, hostname="target", proc=r"C:\Users\operator\SAMPLE-AGENT.EXE"),
        _agent(2, hostname="other", processName="/opt/tools/sample-agent.exe"),
    ]
    requested = _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)
    response, payload = await invoke({"process_name": "sample-agent.exe"})
    assert not response.isError
    assert [a["guid"] for a in payload["data"]["agents"]] == ["agent-1", "agent-2"]
    assert payload["data"]["search_complete"]
    assert requested == [(0, 100)]


async def test_process_filter_is_carried_by_continuation_and_bound_to_its_scope(fake_c2, cfg, interface):
    agents = [_agent(i, proc="sample-agent.exe") for i in range(3)]
    requested = _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)
    response, payload = await invoke({"process_name": "sample-agent.exe", "limit": 1})
    assert not response.isError
    next_call = payload["next"]
    logical_args = next_call["arguments"]
    if interface[0] is MCPMode.DISPATCH:
        logical_args = logical_args["arguments"]
    assert logical_args["process_name"] == "sample-agent.exe"
    changed = {**logical_args, "process_name": "other.exe"}
    before = len(requested)
    response, error = await invoke(changed)
    assert response.isError and error["code"] == "CURSOR_SCOPE_CHANGED"
    assert len(requested) == before
    response, payload = await invoke(continuation=next_call)
    assert not response.isError and payload["data"]["agents"][0]["guid"] == "agent-1"


async def test_selects_host_and_process_with_a_bounded_cross_platform_query(fake_c2, cfg, interface):
    agents = [_agent(0), _agent(1, hostname="WORKSTATION-TEST", username="WORKSTATION-TEST\\operator",
                               proc=r"C:\Users\operator\Desktop\sample-agent.exe", pid=4242)]
    requested = _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)

    result, payload = await invoke({"query": "  workstation   SAMPLE-AGENT.EXE  "})

    assert not result.isError
    assert [item["guid"] for item in payload["data"]["agents"]] == ["agent-1"]
    summary = payload["data"]["agents"][0]
    assert summary["process"] == "sample-agent.exe"
    assert summary["pid"] == 4242
    assert summary["architecture"] == "X64"
    assert payload["data"]["scanned"] == 2
    assert payload["data"]["inventory_total"] == 2
    assert "total" not in payload["data"]
    assert payload["data"]["search_complete"] is True
    assert requested == [(0, 100)]


@pytest.mark.parametrize("query", ["AGENT-1", "desktop", "sample-agent.exe", "operator", "192.0.2.10", "2001:db8::10"])
async def test_query_matches_only_operational_identity_fields(fake_c2, cfg, query):
    agent = _agent(1, hostname="desktop", proc="/opt/bin/sample-agent.exe", ips=["192.0.2.10"],
                   netInterfaces=[{"ipv6Addresses": ["2001:db8::10"]}],
                   customProperties={"password": "unrelated-private-value"})
    _paged_inventory(fake_c2, [agent])
    invoke = _harness(fake_c2, cfg, (MCPMode.NATIVE, ""))

    result, payload = await invoke({"query": query})

    assert not result.isError
    assert payload["data"]["returned"] == 1
    assert "unrelated-private-value" not in result.content[0].text


async def test_query_does_not_search_arbitrary_custom_properties(fake_c2, cfg):
    _paged_inventory(fake_c2, [_agent(customProperties={"password": "unrelated-private-value"})])
    invoke = _harness(fake_c2, cfg, (MCPMode.NATIVE, ""))

    result, payload = await invoke({"query": "unrelated-private-value"})

    assert not result.isError
    assert payload["data"]["agents"] == []
    assert payload["data"]["search_complete"] is True


async def test_sparse_search_returns_progress_and_continuations_without_fetching_every_page(
    fake_c2, cfg, interface,
):
    agents = [_agent(index) for index in range(203)]
    agents[177]["metadata"]["proc"] = "sample-agent.exe"
    requested = _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)

    result, first = await invoke({"query": "sample-agent.exe"})

    assert not result.isError
    assert requested == [(0, 100)]
    assert first["data"]["agents"] == []
    assert first["data"]["scanned"] == 100
    assert first["data"]["search_complete"] is False
    result, second = await invoke(continuation=first["next"])
    assert not result.isError
    assert requested == [(0, 100), (1, 100)]
    assert [item["guid"] for item in second["data"]["agents"]] == ["agent-177"]
    assert second["data"]["inventory_total"] == 203
    assert second["data"]["search_complete"] is False
    result, third = await invoke(continuation=second["next"])
    assert not result.isError
    assert third["data"]["scanned"] == 3
    assert third["data"]["search_complete"] is True
    assert third["next"] is None


async def test_partial_upstream_page_keeps_exact_position_after_nonmatches(fake_c2, cfg, interface):
    agents = [_agent(index) for index in range(6)]
    for index in (1, 4):
        agents[index]["metadata"]["proc"] = "sample-agent.exe"
    requested = _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)
    found, scanned = [], []
    continuation = None

    for _ in range(3):
        result, payload = await invoke({"query": "sample-agent", "limit": 1}, continuation=continuation)
        assert not result.isError
        found.extend(item["guid"] for item in payload["data"]["agents"])
        scanned.append(payload["data"]["scanned"])
        continuation = payload["next"]

    assert found == ["agent-1", "agent-4"]
    assert scanned == [2, 3, 1]
    assert continuation is None
    assert requested == [(0, 100)] * 3


async def test_query_cursor_rejects_changed_search_without_a_request(fake_c2, cfg, interface):
    _paged_inventory(fake_c2, [_agent(index, proc="sample-agent.exe") for index in range(3)])
    invoke = _harness(fake_c2, cfg, interface)
    _, payload = await invoke({"query": "sample-agent", "limit": 1})
    calls_before = len(fake_c2.calls)
    following = payload["next"]
    arguments = following["arguments"]
    if interface[0] is MCPMode.DISPATCH:
        arguments = arguments["arguments"]
    arguments["query"] = "another-process"

    result, rejected = await invoke(continuation=following)

    assert result.isError
    assert rejected["code"] == "CURSOR_SCOPE_CHANGED"
    assert len(fake_c2.calls) == calls_before


async def test_budget_packing_never_loses_matching_agents(fake_c2, cfg, interface):
    agents = [_agent(index, hostname="host" + "a" * 200, username="u" * 200,
                     proc="p" * 200, pid=index) for index in range(25)]
    _paged_inventory(fake_c2, agents)
    invoke = _harness(fake_c2, cfg, interface)
    following = None
    found = []

    for _ in range(30):
        result, payload = await invoke({"query": "host", "limit": 25}, continuation=following)
        assert not result.isError
        found.extend(item["guid"] for item in payload["data"]["agents"])
        following = payload["next"]
        if following is None:
            break

    assert found == [f"agent-{index}" for index in range(25)]
    assert following is None


@pytest.mark.parametrize("query", ["", " \t\n", "q" * 257])
async def test_invalid_query_is_rejected_before_reading_inventory(fake_c2, cfg, query):
    invoke = _harness(fake_c2, cfg, (MCPMode.NATIVE, ""))

    result, _ = await invoke({"query": query})

    assert result.isError
    assert fake_c2.calls == []


async def test_search_rejects_an_upstream_page_that_exceeds_its_scan_bound(fake_c2, cfg):
    fake_c2.responses["/api/v2/agents?page=0&pageSize=100&status=ACTIVE"] = {
        "items": [_agent(index) for index in range(101)], "totalCount": 101,
    }
    invoke = _harness(fake_c2, cfg, (MCPMode.NATIVE, ""))

    result, payload = await invoke({"query": "host"})

    assert result.isError
    assert payload["code"] == "INVALID_SERVER_RESPONSE"
    assert len(fake_c2.calls) == 1


def test_summary_preserves_only_bounded_process_and_valid_callback_fields():
    agent = _agent(proc=r"C:\long\path\sample-agent.exe", pid=4242)
    agent["nextCallback"] = {"expectedTime": "2026-10-23T22:30:00Z", "randomMs": 2500,
                             "secret": "private-value"}

    summary = _agent_summary(agent)

    assert summary["process"] == "sample-agent.exe"
    assert summary["pid"] == 4242
    assert summary["next_callback"] == {"expected_time": "2026-10-23T22:30:00Z", "random_ms": 2500}
    assert "private-value" not in json.dumps(summary)
    invalid = _agent(proc={"password": "private-value"}, pid=True)
    invalid["nextCallback"] = {"expectedTime": "2026-10-23T22:30:00", "randomMs": True}
    invalid_summary = _agent_summary(invalid)
    assert "process" not in invalid_summary
    assert "pid" not in invalid_summary
    assert "next_callback" not in invalid_summary


async def test_agent_detail_includes_callback_estimate_without_extra_requests(fake_c2, cfg, interface):
    agent = _agent()
    agent["nextCallback"] = {"expectedTime": "2026-10-23T22:30:00+00:00", "randomMs": 0}
    fake_c2.responses["/api/v1/agents/agent-1"] = agent
    invoke = _harness(fake_c2, cfg, interface)

    result, payload = await invoke({"agent_guid": "agent-1"}, name="tuoni_agent_get")

    assert not result.isError
    assert payload["data"]["next_callback"] == {
        "expected_time": "2026-10-23T22:30:00+00:00", "random_ms": 0,
    }
    assert [entry[1][0] for entry in fake_c2.calls] == ["/api/v1/agents/agent-1"]
