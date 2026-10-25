"""Recovery contracts measured through real HTTP, SDK, and tool boundaries."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_http_client import upstream as upstream
from test_command_history import _command, _environment, _invoke
from evaluation.extended.world import ExtendedC2, SyntheticBridge
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.events import EventBridge
from tuoni_mcp.http_client import BoundedTuoniC2
from tuoni_mcp.mutations import MutationJournal, mutation_request, mutation_scope
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import Effect, InvocationContext, ToolInput, ToolSpec, build_dispatch_specs
from tuoni_mcp.tools import build_registry
from tuoni_mcp.tools._helpers import call


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("paths,code,state,counts", [
    (["/forbidden"], "TUONI_FORBIDDEN", "rejected", (0, 1, 0)),
    (["/read", "/forbidden"], "PARTIAL_OUTCOME", "partial", (1, 1, 0)),
    (["/read", "/unavailable"], "AMBIGUOUS_OUTCOME", "unknown", (1, 0, 1)),
    (["/request-timeout"], "AMBIGUOUS_OUTCOME", "unknown", (0, 0, 1)),
    (["/forbidden-trickle"], "TUONI_FORBIDDEN", "rejected", (0, 1, 0)),
    (["/oversize"], "PARTIAL_OUTCOME", "partial", (1, 0, 0)),
])
async def test_http_acknowledgments_distinguish_denial_partial_and_unknown(upstream, mode, paths, code, state, counts):
    url, requests = upstream
    c2 = BoundedTuoniC2(request_timeout=0.15, response_max_bytes=128)
    c2.login(url, "user", "pass")
    cfg = Settings(url=url, username="user", password="pass", verify=False,
                   mode=mode, profiles=frozenset({Profile.ADMINISTRATION}), tool_prefix="tenant_")
    registry = build_registry(cfg)
    server = ServerContext(c2=c2, cfg=cfg, last_login=time.monotonic())
    invocation = InvocationContext(
        context=SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server)), server=server,
    )

    def composite():
        for path in paths:
            c2.request_post(path)

    async def handler(invocation, _arguments):
        await call(invocation.context, composite)
        return {"updated": True}

    spec = ToolSpec(name="tuoni_setting_fixture", description="Synthetic multi-request SDK operation",
                    input_model=ToolInput, handler=handler, effect=Effect.MUTATE,
                    profiles=frozenset({Profile.ADMINISTRATION}))
    registry.add(spec)
    specs = {s.name: s for s in (build_dispatch_specs(registry, registry.select(cfg.profiles, read_only=False),
                                                     read_only=False) if mode is MCPMode.DISPATCH else registry.specs)}
    try:
        name, args = spec.name, {}
        if mode is MCPMode.DISPATCH:
            prepared = await registry.invoke_spec(specs["tuoni_call"], {"name": name, "arguments": {}}, invocation)
            assert not prepared.isError
            args = json.loads(prepared.content[0].text)["next"]["arguments"]
            name = "tuoni_operation_execute"
        result = await registry.invoke_spec(specs[name], args, invocation)
        payload = json.loads(result.content[0].text)
        assert result.isError and payload["code"] == code, payload
        assert payload["retryable"] is False
        assert payload["outcome"]["state"] == state
        assert tuple(payload["outcome"][key] for key in
                     ("accepted_requests", "rejected_requests", "unknown_requests")) == counts
        assert call_result_size(result) <= ERROR_RESULT_BUDGET
        assert [path for path, _, _ in requests if not path.startswith("/api/v2/auth/")] == paths
        if mode is MCPMode.DISPATCH:
            following = payload["next"]
            assert following["name"] == "tenant_tuoni_call"
            assert registry.get(following["arguments"]["name"]).effect is Effect.READ
            size = len(requests)
            repeated = await registry.invoke_spec(specs[name], args, invocation)
            repeated_payload = json.loads(repeated.content[0].text)
            assert repeated_payload["code"] == code
            assert repeated_payload["outcome"] == payload["outcome"]
            assert len(requests) == size
    finally:
        c2.close()


def test_closed_journal_blocks_late_writes_and_does_not_reclassify_unknown():
    journal = MutationJournal()
    with mutation_scope(journal):
        attempt = mutation_request("POST")
        assert attempt is not None
        journal.close()
        attempt.respond(200)
        assert journal.snapshot()["state"] == "unknown"
        with pytest.raises(RuntimeError, match="operation ended"):
            mutation_request("DELETE")
    assert journal.unknown == 1 and journal.accepted == 0


def test_synthetic_event_bridge_preserves_real_empty_and_retained_bounds():
    bridge = SyntheticBridge(SimpleNamespace(c2=ExtendedC2()))
    assert bridge.events_after(0) == ([], None)
    assert bridge._next_sequence == 1
    bridge.emit(1005)
    events, oldest = bridge.events_after(0)
    assert oldest == events[0]["sequence"] == 1


@pytest.mark.parametrize("dispatch", [False, True])
async def test_history_status_counts_and_coverage_never_describe_unread_records(dispatch):
    records = [_command(i) for i in range(1, 22)]
    for record in records[:3]:
        record["status"] = "FAILED"
    environment = _environment(records, dispatch=dispatch)
    payload, _ = await _invoke(environment, {})
    assert payload["data"]["coverage"] == {
        "offset": 0, "returned": 10, "total": 21, "has_more": True, "complete": False,
    }
    assert payload["data"]["returned_status_counts"] == {"COMPLETE": 10}
    ids, counts = [], {"COMPLETE": 0, "FAILED": 0}
    while True:
        data = payload["data"]
        ids.extend(c["command_id"] for c in data["commands"])
        for status, count in data["returned_status_counts"].items():
            counts[status] += count
        if payload["next"] is None:
            break
        following = payload["next"]["arguments"]
        payload, _ = await _invoke(environment, following, continuation=dispatch)
    assert len(ids) == len(set(ids)) == 21 and counts == {"COMPLETE": 18, "FAILED": 3}
    assert data["coverage"]["has_more"] is False
    assert data["coverage"]["complete"] is False  # Earlier pages are still required.


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_late_event_replay_is_bounded_filterable_and_gap_aware(cfg, mode):
    cfg = replace(cfg, mode=mode, profiles=frozenset({Profile.EVENTS}), event_buffer_size=3)
    server = ServerContext(c2=SimpleNamespace(), cfg=cfg)
    bridge = server.event_bridge = EventBridge(server)
    bridge.status.connected = True
    result_uri, other_uri = "tuoni://events/commands/results", "tuoni://events/agents/new"
    for i in range(5):
        bridge._enqueue(result_uri if i % 2 else other_uri,
                        {"received_at": float(i), "topic": "fixture", "data": {"id": i}})
    registry = build_registry(cfg)
    specs = {s.name: s for s in (build_dispatch_specs(registry, registry.select(cfg.profiles, read_only=True),
                                                     read_only=True) if mode is MCPMode.DISPATCH else registry.specs)}
    invocation = InvocationContext(context=None, server=server)

    async def poll(arguments):
        name = "tuoni_events_poll"
        if mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        return json.loads(result.content[0].text)

    now = await poll({})
    assert now["data"]["events"] == []
    assert (now["data"]["earliest_sequence"], now["data"]["latest_sequence"]) == (3, 5)
    replay = await poll({"start": "earliest", "limit": 1})
    assert [e["sequence"] for e in replay["data"]["events"]] == [3]
    assert replay["data"]["after_sequence"] == 2 and replay["data"]["has_more"]
    assert "start" not in replay["next"]["arguments"]
    filtered = await poll({"start": "earliest", "topics": [result_uri]})
    assert [e["sequence"] for e in filtered["data"]["events"]] == [4]
    assert filtered["data"]["through_sequence"] == 5 and not filtered["data"]["has_more"]
    changed = await poll({"cursor": now["next_cursor"], "topics": [result_uri]})
    assert changed["code"] == "CURSOR_SCOPE_CHANGED"
    recovery = changed["next"]["arguments"]
    if mode is MCPMode.DISPATCH:
        recovery = recovery["arguments"]
    assert recovery["start"] == "earliest" and "cursor" not in recovery
    invalid = await poll({"start": "earliest", "cursor": now["next_cursor"]})
    assert invalid["code"] == "INVALID_ARGUMENTS"
    empty = await poll({"start": "earliest", "topics": ["tuoni://events/listeners/new"]})
    assert not empty["data"]["events"] and empty["data"]["through_sequence"] == 5


async def test_cancelling_one_scope_does_not_close_another():
    journals = [MutationJournal(), MutationJournal()]
    ready = asyncio.Event()

    async def work(journal, cancel):
        with mutation_scope(journal):
            mutation_request("POST")
            if cancel:
                ready.set()
                raise asyncio.CancelledError
            await ready.wait()
            mutation_request("PUT").respond(200)

    values = await asyncio.gather(work(journals[0], True), work(journals[1], False), return_exceptions=True)
    assert isinstance(values[0], asyncio.CancelledError)
    assert journals[0].unknown == 1
    assert journals[1].accepted == 1 and journals[1].unknown == 1


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_cancelled_sdk_worker_cannot_submit_a_later_write(upstream, mode):
    url, requests = upstream
    c2 = BoundedTuoniC2()
    c2.login(url, "user", "pass")
    cfg = Settings(url=url, username="user", password="pass", verify=False, mode=mode,
                   profiles=frozenset({Profile.ADMINISTRATION}), request_timeout=2)
    registry = build_registry(cfg)
    server = ServerContext(c2=c2, cfg=cfg, last_login=time.monotonic())
    invocation = InvocationContext(
        context=SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server)), server=server,
    )
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def delayed_sdk_operation():
        started.set()
        try:
            assert release.wait(2)
            c2.request_post("/late-write")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    async def handler(invocation, arguments):
        await call(invocation.context, delayed_sdk_operation)
        return {"updated": True}

    spec = ToolSpec(name="tuoni_setting_fixture", description="Delayed synthetic SDK operation",
                    input_model=ToolInput, handler=handler, effect=Effect.MUTATE,
                    profiles=frozenset({Profile.ADMINISTRATION}))
    registry.add(spec)
    specs = {s.name: s for s in (build_dispatch_specs(registry, registry.select(cfg.profiles, read_only=False),
                                                     read_only=False) if mode is MCPMode.DISPATCH else registry.specs)}
    try:
        name, arguments = spec.name, {}
        if mode is MCPMode.DISPATCH:
            prepared = await registry.invoke_spec(specs["tuoni_call"], {"name": name, "arguments": {}}, invocation)
            arguments = json.loads(prepared.content[0].text)["next"]["arguments"]
            name = "tuoni_operation_execute"
        task = asyncio.create_task(registry.invoke_spec(specs[name], arguments, invocation))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert errors and "operation ended" in str(errors[0])
        assert all(path != "/late-write" for path, _, _ in requests)
        if mode is MCPMode.DISPATCH:
            record = await server.preparation_store.get(arguments["preparation_id"])
            assert record.state == "prepared"
    finally:
        release.set()
        c2.close()
