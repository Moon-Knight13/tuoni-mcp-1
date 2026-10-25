from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.http_client import ResponseTooLarge, TuoniHTTPError
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import Effect, InvocationContext, ToolRegistry, build_dispatch_specs
from tuoni_mcp.tools.command_history import register_specs

NAME = "tuoni_commands_list"
AGENT = "a1234567-89ab-cdef-0123-456789abcdef"
OTHER_AGENT = "b1234567-89ab-cdef-0123-456789abcdef"
TEMPLATE = "c1234567-89ab-cdef-0123-456789abcdef"
_DEFAULT = object()


def _command(command_id, *, agent=AGENT, status="COMPLETE", large=False):
    return {
        "id": command_id, "agentGuid": agent, "commandTemplateId": TEMPLATE,
        "commandTemplateName": ('🙂\\"\n' * 10_000) if large else "example",
        "status": status, "created": "2026-10-05T10:00:00Z", "sent": "2026-10-05T10:00:01Z",
        "configuration": {"password": "do-not-emit-configuration"},
        "execConf": {"password": "do-not-emit-execution"},
        "result": {"childResults": [{"type": "text", "value": "do-not-emit-result" * 500}]},
    }


class HistoryC2:
    """Only an existing-history GET exists; no target-operation methods are available."""

    def __init__(self, records):
        self.records = records
        self.calls = []
        self.response = _DEFAULT
        self.error = None
        self.transform = None

    def request_get(self, uri):
        self.calls.append(uri)
        parsed = urlsplit(uri)
        assert parsed.path == "/api/v2/commands"
        query = parse_qs(parsed.query)
        assert set(query) <= {"page", "pageSize", "sort", "agentGuid", "status"}
        assert query["sort"] == ["ID:DESC"]
        if self.error is not None:
            raise self.error
        if self.response is not _DEFAULT:
            return self.response
        records = sorted(self.records, key=lambda item: item["id"], reverse=True)
        if "agentGuid" in query:
            records = [item for item in records if item["agentGuid"].lower() == query["agentGuid"][0].lower()]
        if "status" in query:
            records = [item for item in records if item["status"] == query["status"][0]]
        page, size = int(query["page"][0]), int(query["pageSize"][0])
        assert 1 <= size <= 25
        items = records[page * size:(page + 1) * size]
        response = {
            "page": page, "pageSize": size, "itemCount": len(items), "totalCount": len(records),
            "totalPages": (len(records) + size - 1) // size,
            "hasMorePages": (page + 1) * size < len(records), "items": items,
        }
        return self.transform(response) if self.transform else response


def _environment(records, *, dispatch=False, prefix="", read_only=False):
    cfg = Settings(
        url="https://example.test", username="test", password="test", verify=True,
        profiles=frozenset({Profile.AGENT_OPS}), read_only=read_only, tool_prefix=prefix,
        mode=MCPMode.DISPATCH if dispatch else MCPMode.NATIVE,
    )
    registry = ToolRegistry()
    register_specs(registry, cfg)
    c2 = HistoryC2(records)
    server = ServerContext(c2=c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    selected = registry.select(cfg.profiles, read_only=read_only)
    adapters = build_dispatch_specs(registry, selected, read_only=read_only) if dispatch else selected
    return SimpleNamespace(
        c2=c2, cfg=cfg, registry=registry, tools={spec.name: spec for spec in adapters},
        invocation=InvocationContext(context=context, server=server), dispatch=dispatch,
    )


async def _invoke(environment, arguments, *, continuation=False):
    name = "tuoni_call" if environment.dispatch else NAME
    if environment.dispatch and not continuation:
        arguments = {"name": NAME, "arguments": arguments}
    result = await environment.registry.invoke_spec(
        environment.tools[name], arguments, environment.invocation,
    )
    assert call_result_size(result) <= (ERROR_RESULT_BUDGET if result.isError else LIST_RESULT_BUDGET)
    return json.loads(result.content[0].text), result


@pytest.mark.parametrize("dispatch", [False, True])
@pytest.mark.parametrize("prefix", ["", "tenant_" + "p" * 57])
async def test_history_pages_are_bounded_and_complete_in_both_modes(dispatch, prefix):
    records = [_command(number, large=True) for number in range(1, 54)]
    environment = _environment(records, dispatch=dispatch, prefix=prefix, read_only=True)
    arguments = {"limit": 25}
    seen, calls, cursors = [], 0, set()
    while True:
        before = len(environment.c2.calls)
        payload, result = await _invoke(environment, arguments, continuation=calls > 0)
        assert payload["ok"]
        assert len(environment.c2.calls) == before + 1
        commands = payload["data"]["commands"]
        assert 1 <= len(commands) <= 25
        assert payload["data"]["returned"] == len(commands)
        assert payload["data"]["total"] == 53
        assert "do-not-emit" not in result.content[0].text
        for command in commands:
            assert command["agent_guid"] == AGENT
            assert command["template_id"] == TEMPLATE
            assert len(command["template_name"].encode("utf-8")) <= 96
            assert set(command) == {
                "command_id", "agent_guid", "template_id", "template_name", "status", "created", "sent",
            }
        seen.extend(command["command_id"] for command in commands)
        calls += 1
        assert calls <= len(records)
        if payload["next"] is None:
            assert payload["next_cursor"] is None
            break
        assert payload["next"]["name"] == prefix + ("tuoni_call" if dispatch else NAME)
        assert payload["next_cursor"] not in cursors
        cursors.add(payload["next_cursor"])
        arguments = payload["next"]["arguments"]
    assert seen == list(range(53, 0, -1))
    assert len(seen) == len(set(seen))
    # The byte budget splits large metadata within an upstream page, not just
    # at the API page boundary. Re-fetches remain bounded to pageSize=25.
    assert calls > 3
    assert {parse_qs(urlsplit(uri).query)["page"][0] for uri in environment.c2.calls} == {"0", "1", "2"}


async def test_filters_use_encoded_query_parameters_and_preserve_exact_ids():
    records = [_command(number, status="SENT", agent=AGENT.upper()) for number in range(1, 5)]
    records += [_command(5, status="SENT", agent=OTHER_AGENT), _command(6)]
    environment = _environment(records)
    arguments = {"agent_guid": AGENT, "status": "SENT", "limit": 2}
    payload, _ = await _invoke(environment, arguments)
    assert payload["ok"]
    assert [item["command_id"] for item in payload["data"]["commands"]] == [4, 3]
    assert payload["data"]["commands"][0]["agent_guid"] == AGENT.upper()
    assert environment.c2.calls == [
        f"/api/v2/commands?page=0&pageSize=2&sort=ID%3ADESC&agentGuid={AGENT}&status=SENT",
    ]
    next_arguments = payload["next"]["arguments"]
    assert {key: value for key, value in next_arguments.items() if key != "cursor"} == arguments
    final, _ = await _invoke(environment, next_arguments)
    assert [item["command_id"] for item in final["data"]["commands"]] == [2, 1]
    assert final["next"] is None


async def test_empty_history_has_no_continuation():
    environment = _environment([])
    payload, _ = await _invoke(environment, {})
    assert payload == {
        "ok": True, "data": {"commands": [], "returned": 0, "page": 0, "total": 0, "returned_status_counts": {},
                 "coverage": {"offset": 0, "returned": 0, "total": 0, "has_more": False, "complete": True}},
        "next_cursor": None, "next": None,
    }
    assert len(environment.c2.calls) == 1


@pytest.mark.parametrize("arguments", [
    {"agent_guid": True}, {"agent_guid": "agent-1"}, {"agent_guid": AGENT + "&status=SENT"},
    {"status": "complete"}, {"status": ["COMPLETE"]}, {"status": True},
    {"limit": True}, {"limit": "2"}, {"limit": 1.0}, {"limit": 0}, {"limit": 26},
    {"cursor": ""}, {"cursor": 1}, {"cursor": "x" * 2049}, {"unknown": "value"},
])
async def test_invalid_arguments_never_contact_the_server(arguments):
    environment = _environment([])
    payload, _ = await _invoke(environment, arguments)
    assert payload["code"] == "INVALID_ARGUMENTS"
    assert environment.c2.calls == []


@pytest.mark.parametrize("change", [
    {"limit": 1}, {"agent_guid": AGENT}, {"status": "COMPLETE"},
])
async def test_cursor_cannot_change_its_filter_scope(change):
    environment = _environment([_command(number) for number in range(1, 5)])
    first, _ = await _invoke(environment, {"limit": 2})
    payload, _ = await _invoke(environment, {**first["next"]["arguments"], **change})
    assert payload["code"] == "CURSOR_SCOPE_CHANGED"
    assert len(environment.c2.calls) == 1


@pytest.mark.parametrize("change", [
    {"page": True}, {"page": "1"}, {"page": -1}, {"page": 2**63},
    {"offset": True}, {"offset": -1}, {"offset": 2}, {"offset": 0.5},
    {"total": True}, {"total": 0}, {"last_id": True}, {"last_id": 0},
    {"page_ids": "invalid"}, {"v": True}, {"extra": 1}, {"tool": "tuoni_agents_list"},
])
async def test_malformed_cursor_fields_fail_closed_before_a_read(change):
    environment = _environment([_command(number) for number in range(1, 5)])
    first, _ = await _invoke(environment, {"limit": 2})
    arguments = first["next"]["arguments"]
    state = environment.registry.cursors.decode(arguments["cursor"], tool=NAME)
    payload, _ = await _invoke(environment, {**arguments, "cursor": environment.registry.cursors.encode({**state, **change})})
    assert payload["code"] == ("CURSOR_SCOPE_CHANGED" if "tool" in change else "INVALID_CURSOR")
    assert len(environment.c2.calls) == 1


@pytest.mark.parametrize("response", [None, [], {}, {"items": []}, {"items": "invalid"}])
async def test_malformed_server_page_never_returns_partial_success(response):
    environment = _environment([])
    environment.c2.response = response
    payload, _ = await _invoke(environment, {})
    assert payload["code"] == "INVALID_SERVER_RESPONSE"
    assert len(environment.c2.calls) == 1


@pytest.mark.parametrize("change", [
    {"page": True}, {"page": 1}, {"pageSize": 11}, {"itemCount": 2},
    {"totalCount": "1"}, {"totalCount": -1}, {"totalPages": 0}, {"hasMorePages": "false"},
    {"hasMorePages": True}, {"items": []}, {"items": [None]},
])
async def test_inconsistent_server_pagination_is_rejected(change):
    environment = _environment([_command(1)])
    environment.c2.transform = lambda response: {**response, **change}
    payload, _ = await _invoke(environment, {})
    assert payload["code"] == "INVALID_SERVER_RESPONSE"


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": "1"}, {"id": 0}, {"id": 2**31},
    {"agentGuid": "x" * 1000}, {"commandTemplateId": "x" * 1000},
    {"status": ["COMPLETE"]}, {"status": "unknown"}, {"created": {}},
    {"commandTemplateName": {"password": "never-emit"}}, {"sent": []},
])
async def test_invalid_identifiers_and_metadata_are_not_truncated_into_success(change):
    environment = _environment([{**_command(1), **change}])
    payload, _ = await _invoke(environment, {})
    assert payload["code"] == "INVALID_SERVER_RESPONSE"


async def test_server_ignoring_filters_or_sort_is_rejected():
    environment = _environment([_command(1), _command(2)])
    environment.c2.transform = lambda response: {**response, "items": list(reversed(response["items"]))}
    payload, _ = await _invoke(environment, {})
    assert payload["code"] == "INVALID_SERVER_RESPONSE"
    environment.c2.transform = lambda response: {
        **response, "items": [{**item, "agentGuid": OTHER_AGENT} for item in response["items"]],
    }
    payload, _ = await _invoke(environment, {"agent_guid": AGENT})
    assert payload["code"] == "INVALID_SERVER_RESPONSE"


@pytest.mark.parametrize("partial", [False, True])
async def test_detected_membership_changes_reject_cursor_without_duplicate_records(partial):
    environment = _environment([_command(number, large=partial) for number in range(1, 31)])
    first, _ = await _invoke(environment, {"limit": 25 if partial else 2})
    state = environment.registry.cursors.decode(first["next_cursor"], tool=NAME)
    assert bool(state["offset"]) == partial
    # Keep totalCount unchanged but shift descending pages. Partial-page
    # fingerprints and cross-page last-ID boundaries both detect this.
    environment.c2.records.pop(0)
    environment.c2.records.append(_command(31, large=partial))
    payload, _ = await _invoke(environment, first["next"]["arguments"])
    assert payload["code"] == "CURSOR_STALE"
    assert "data" not in payload


async def test_changed_total_count_rejects_cursor():
    environment = _environment([_command(number) for number in range(1, 5)])
    first, _ = await _invoke(environment, {"limit": 2})
    environment.c2.records.append(_command(5))
    payload, _ = await _invoke(environment, first["next"]["arguments"])
    assert payload["code"] == "CURSOR_STALE"


@pytest.mark.parametrize(("error", "code"), [
    (TuoniHTTPError(404, "missing"), "INCOMPATIBLE_TUONI_SERVER"),
    (TuoniHTTPError(405, "wrong method"), "INCOMPATIBLE_TUONI_SERVER"),
    (TuoniHTTPError(403, "forbidden"), "TUONI_FORBIDDEN"),
    (ResponseTooLarge("response limit exceeded"), "UPSTREAM_RESPONSE_TOO_LARGE"),
])
async def test_upstream_failures_have_no_unbounded_v1_fallback(error, code):
    environment = _environment([])
    environment.c2.error = error
    payload, _ = await _invoke(environment, {})
    assert payload["code"] == code
    assert len(environment.c2.calls) == 1


async def test_read_only_selection_and_discovery_expose_history_without_side_effects():
    environment = _environment([_command(1)], dispatch=True, read_only=True)
    spec = environment.registry.get(NAME)
    assert spec.effect is Effect.READ
    assert spec.wire_tool().annotations.readOnlyHint is True
    assert spec.wire_tool().annotations.idempotentHint is True
    assert environment.registry.select({Profile.AGENT_OPS}, read_only=True) == (spec,)
    assert environment.registry.select({Profile.ACCESS}, read_only=True) == ()
    found = await environment.registry.invoke_spec(
        environment.tools["tuoni_find_tool"], {"query": "command history"}, environment.invocation,
    )
    assert json.loads(found.content[0].text)["data"]["tools"][0]["name"] == NAME
    assert environment.c2.calls == []
    before = copy.deepcopy(environment.c2.records)
    payload, _ = await _invoke(environment, {})
    assert payload["ok"]
    assert environment.c2.records == before
    assert len(environment.c2.calls) == 1
