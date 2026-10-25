from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
from requests.exceptions import Timeout
from tuoni.TuoniExceptions import ExceptionTuoniAuthentication, ExceptionTuoniRequestFailed

from tuoni_mcp import result_cache, result_wait
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry

COMMAND_ID = 987654321


def _command(status="SENT", *, parts=None):
    return {
        "id": COMMAND_ID,
        "commandTemplateId": "template-id",
        "commandTemplateName": "inspect",
        "status": status,
        "result": None if parts is None else {
            "status": "SUCCESS", "childResults": parts,
        },
    }


@pytest.fixture(params=[(mode, prefix) for mode in MCPMode for prefix in ("", "client_")])
def interface(request):
    return request.param


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(result_wait, "INITIAL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(result_wait, "MAX_POLL_SECONDS", 0.004)


def _harness(fake_c2, interface):
    mode, prefix = interface
    cfg = Settings(
        url="https://example.test", username="test", password="test", verify=True,
        profiles=frozenset({Profile.AGENT_OPS}), mode=mode, tool_prefix=prefix,
        read_only=True,
    )
    registry = build_registry(cfg)
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(cfg.profiles, read_only=True)
    specs = {
        spec.name: spec for spec in (
            selected if mode is MCPMode.NATIVE else
            build_dispatch_specs(registry, selected, read_only=True)
        )
    }

    async def invoke(arguments, *, exposed=None):
        if exposed is not None:
            name = exposed["name"].removeprefix(prefix)
            arguments = exposed["arguments"]
        elif mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": "tuoni_command_get", "arguments": arguments}
        else:
            name = "tuoni_command_get"
        return await registry.invoke_spec(specs[name], arguments, invocation)

    return invoke


def _payload(result):
    budget = ERROR_RESULT_BUDGET if result.isError else MAX_RESULT_BUDGET
    assert call_result_size(result) <= budget
    return json.loads(result.content[0].text)


def _expected_next(interface, arguments):
    mode, prefix = interface
    if mode is MCPMode.DISPATCH:
        return {
            "name": prefix + "tuoni_call",
            "arguments": {"name": "tuoni_command_get", "arguments": arguments},
        }
    return {"name": prefix + "tuoni_command_get", "arguments": arguments}


@pytest.mark.parametrize("terminal", ["COMPLETE", "FAILED", "CANCELED", "complete"])
async def test_observer_returns_first_terminal_state_without_another_poll(fast_poll, terminal):
    observations = [_command("CREATED"), _command("ONGOING"), _command(terminal)]
    calls = []

    async def fetch():
        calls.append(len(calls))
        return observations[len(calls) - 1]

    observed = await result_wait.read_until_terminal(fetch, 1)

    assert observed.value is observations[-1]
    assert not observed.timed_out
    assert len(calls) == 3


async def test_zero_wait_reads_once_without_waiting_for_terminal_state():
    calls = []
    pending = _command()

    async def fetch():
        calls.append(True)
        return pending

    observed = await result_wait.read_until_terminal(fetch, 0)

    assert observed.value is pending
    assert not observed.timed_out
    assert calls == [True]


async def test_initial_read_is_cancelled_at_total_deadline():
    cancelled = asyncio.Event()

    async def fetch():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(result_wait.InitialReadTimeout):
        await asyncio.wait_for(result_wait.read_until_terminal(fetch, 0.03), timeout=1)
    assert cancelled.is_set()


async def test_later_blocking_read_returns_last_observation_at_total_deadline(fast_poll, monkeypatch):
    pending = _command()
    calls = []
    cancelled = asyncio.Event()
    scope = anyio.move_on_after(30)
    monkeypatch.setattr(result_wait, "anyio", SimpleNamespace(move_on_after=lambda _: scope))

    async def fetch():
        calls.append(True)
        if len(calls) == 1:
            return pending
        # Expire only after the second read has entered, independent of the
        # platform's timer resolution or the time spent scheduling that read.
        scope.deadline = anyio.current_time()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    observed = await asyncio.wait_for(result_wait.read_until_terminal(fetch, 30), timeout=1)

    assert observed.value is pending
    assert observed.timed_out
    assert len(calls) == 2
    assert cancelled.is_set()


async def test_poll_backoff_is_capped_and_last_sleep_fits_remaining_deadline(monkeypatch):
    clock = [0.0]
    sleeps = []
    calls = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    async def fetch():
        calls.append(clock[0])
        return _command()

    # Replace only this module's clock, preserving the event loop's real clock.
    monkeypatch.setattr(result_wait, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(result_wait, "asyncio", SimpleNamespace(sleep=sleep))
    observed = await result_wait.read_until_terminal(fetch, 15)

    assert observed.timed_out
    assert sleeps == [1, 2, 4, 5, 3]
    assert calls == [0, 1, 3, 7, 12]


async def test_caller_cancellation_propagates_without_becoming_a_timeout():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def fetch():
        calls.append(True)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(result_wait.read_until_terminal(fetch, 30))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert calls == [True]


async def test_deadline_also_bounds_terminal_snapshot_worker(monkeypatch):
    """Cache snapshots must not shield a result wait from its total deadline."""
    original = result_cache._snapshot
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def snapshot(*args):
        started.set()
        try:
            release.wait(timeout=2)
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(result_cache, "_snapshot", snapshot)
    cache = result_cache.CommandResultCache()
    scope = anyio.move_on_after(30)
    monkeypatch.setattr(result_wait, "anyio", SimpleNamespace(move_on_after=lambda _: scope))

    async def backend():
        return _command("COMPLETE", parts=[])

    async def fetch():
        return await cache.get(COMMAND_ID, backend)

    task = asyncio.create_task(result_wait.read_until_terminal(fetch, 30))
    try:
        # A busy CI worker need not begin within a few milliseconds. Expire the
        # real cancellation deadline only after snapshot work has entered.
        assert await asyncio.to_thread(started.wait, 2)
        scope.deadline = anyio.current_time()
        with pytest.raises(result_wait.InitialReadTimeout):
            await asyncio.wait_for(task, timeout=1)
        assert not finished.is_set(), "The caller waited for snapshot work after the deadline"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


async def test_tool_wait_observes_terminal_and_reuses_cache_without_submission(
    fake_c2, interface, fast_poll,
):
    responses = iter([
        _command("CREATED"), _command("ONGOING"),
        _command("COMPLETE", parts=[{"type": "text", "name": "stdout", "value": "observed"}]),
    ])

    def request_get(uri):
        fake_c2._record("request_get", uri)
        assert uri == f"/api/v1/commands/{COMMAND_ID}"
        return next(responses)

    fake_c2.request_get = request_get
    invoke = _harness(fake_c2, interface)
    result = await invoke({"command_id": COMMAND_ID, "wait_seconds": 1})
    payload = _payload(result)

    assert not result.isError, payload
    assert payload["data"]["command_id"] == COMMAND_ID
    assert payload["data"]["status"] == "COMPLETE"
    assert "wait_timed_out" not in payload["data"]
    assert payload["next"] == _expected_next(
        interface, {
            "command_id": COMMAND_ID, "part_index": 0, "offset_chars": 0, "max_chars": 4096,
            "result_version": payload["data"]["result"]["parts"][0]["result_version"],
        },
    )
    text_result = await invoke({}, exposed=payload["next"])
    assert _payload(text_result)["data"]["result"]["part"]["value"] == "observed"
    assert [name for name, _, _ in fake_c2.calls] == ["request_get"] * 3


async def test_pending_deadline_returns_exact_wait_continuation(fake_c2, interface, monkeypatch):
    scopes = []

    def controlled_deadline(wait_seconds):
        assert wait_seconds == 1
        scope = anyio.move_on_after(30)
        scopes.append(scope)
        return scope

    async def expire_after_observation(delay):
        # Sleeping for the remaining 20 ms can wake early on Windows and allow
        # another poll. Expire the actual scope after exactly one observation.
        assert [name for name, _, _ in fake_c2.calls] == ["request_get"]
        scopes[-1].deadline = anyio.current_time()
        await anyio.sleep_forever()

    monkeypatch.setattr(result_wait, "anyio", SimpleNamespace(move_on_after=controlled_deadline))
    monkeypatch.setattr(result_wait, "asyncio", SimpleNamespace(sleep=expire_after_observation))
    fake_c2.responses[f"/api/v1/commands/{COMMAND_ID}"] = _command()
    invoke = _harness(fake_c2, interface)
    result = await invoke({"command_id": COMMAND_ID, "wait_seconds": 1})
    payload = _payload(result)

    assert not result.isError, payload
    assert payload["data"]["command_id"] == COMMAND_ID
    assert payload["data"]["wait_timed_out"] is True
    assert payload["next"] == _expected_next(interface, {"command_id": COMMAND_ID, "wait_seconds": 1})
    fake_c2.responses[f"/api/v1/commands/{COMMAND_ID}"] = _command("COMPLETE", parts=[])
    completed = await invoke({}, exposed=payload["next"])
    assert _payload(completed)["data"]["status"] == "COMPLETE"
    assert [name for name, _, _ in fake_c2.calls] == ["request_get"] * 2


@pytest.mark.parametrize("phase", ["initial", "later"])
@pytest.mark.parametrize("error_kind", ["timeout", "forbidden"])
async def test_wait_read_errors_preserve_command_id_and_executable_continuation(
    fake_c2, interface, fast_poll, phase, error_kind,
):
    calls = []

    def request_get(uri):
        fake_c2._record("request_get", uri)
        calls.append(uri)
        assert uri == f"/api/v1/commands/{COMMAND_ID}"
        if phase == "later" and len(calls) == 1:
            return _command()
        if error_kind == "timeout":
            raise Timeout("read timeout password=do-not-leak")
        error = ExceptionTuoniRequestFailed("forbidden password=do-not-leak")
        error.status_code = 403
        raise error

    fake_c2.request_get = request_get
    result = await _harness(fake_c2, interface)({"command_id": COMMAND_ID, "wait_seconds": 1})
    payload = _payload(result)

    assert result.isError
    assert payload["code"] == {"timeout": "TUONI_TIMEOUT", "forbidden": "TUONI_FORBIDDEN"}[error_kind]
    assert payload["retryable"] is (error_kind == "timeout")
    assert str(COMMAND_ID) in payload["message"]
    assert "do-not-leak" not in result.model_dump_json()
    assert payload["next"] == _expected_next(interface, {"command_id": COMMAND_ID})
    assert [name for name, _, _ in fake_c2.calls] == ["request_get"] * (1 if phase == "initial" else 2)


async def test_initial_wait_timeout_has_same_command_read_continuation(fake_c2, interface, monkeypatch):
    import tuoni_mcp.tools.commands as commands

    async def timeout(fetch, wait_seconds, **kwargs):
        assert wait_seconds == 1
        raise result_wait.InitialReadTimeout()

    monkeypatch.setattr(commands, "read_until_terminal", timeout)
    result = await _harness(fake_c2, interface)({"command_id": COMMAND_ID, "wait_seconds": 1})
    payload = _payload(result)

    assert result.isError
    assert payload["code"] == "RESULT_WAIT_TIMEOUT"
    assert payload["retryable"] is True
    assert str(COMMAND_ID) in payload["message"]
    assert payload["next"] == _expected_next(interface, {"command_id": COMMAND_ID})
    assert fake_c2.calls == []


async def test_callback_hint_is_used_then_reused_without_extra_zero_wait_read(
    fake_c2, interface, monkeypatch,
):
    from datetime import datetime, timezone

    now = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()
    agent_guid = "12345678-1234-5678-9abc-123456789012"
    scope = anyio.move_on_after(30)
    original_scope = anyio.move_on_after
    scopes = []

    def deadline(seconds):
        value = scope if not scopes else original_scope(seconds)
        scopes.append(value)
        return value

    async def expire(delay):
        assert 0 < delay <= 1
        scope.deadline = anyio.current_time()
        await anyio.sleep_forever()

    monkeypatch.setattr(result_wait, "anyio", SimpleNamespace(move_on_after=deadline))
    monkeypatch.setattr(result_wait, "asyncio", SimpleNamespace(sleep=expire))
    monkeypatch.setattr(result_wait, "time", SimpleNamespace(monotonic=time.monotonic, time=lambda: now))
    fake_c2.responses[f"/api/v1/commands/{COMMAND_ID}"] = {**_command(), "agentGuid": agent_guid}
    fake_c2.responses[f"/api/v1/agents/{agent_guid}"] = {
        "guid": agent_guid,
        "nextCallback": {
            "expectedTime": datetime.fromtimestamp(now + 20, timezone.utc).isoformat(),
            "randomMs": 5000,
        },
    }
    invoke = _harness(fake_c2, interface)

    pending = await invoke({"command_id": COMMAND_ID, "wait_seconds": 1})
    first = _payload(pending)
    cached = await invoke({"command_id": COMMAND_ID})
    second = _payload(cached)

    assert not pending.isError and not cached.isError
    assert first["data"]["wait_timed_out"] is True
    assert first["data"]["poll_after_seconds"] == second["data"]["poll_after_seconds"] == 16
    assert second["next"] == _expected_next(interface, {"command_id": COMMAND_ID})
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [
        f"/api/v1/commands/{COMMAND_ID}", f"/api/v1/agents/{agent_guid}", f"/api/v1/commands/{COMMAND_ID}",
    ]


async def test_callback_lookup_does_not_turn_ongoing_command_into_server_job(
    fake_c2, interface, monkeypatch,
):
    agent_guid = "12345678-1234-5678-9abc-123456789012"
    pending = iter(["SENT", "ONGOING", "COMPLETE"])

    async def sleep(delay):
        return None

    def request_get(uri):
        fake_c2._record("request_get", uri)
        if uri == f"/api/v1/agents/{agent_guid}":
            return {"guid": agent_guid, "nextCallback": None}
        assert uri == f"/api/v1/commands/{COMMAND_ID}"
        return {**_command(next(pending)), "agentGuid": agent_guid}

    monkeypatch.setattr(result_wait, "asyncio", SimpleNamespace(sleep=sleep))
    fake_c2.request_get = request_get

    response = await _harness(fake_c2, interface)({"command_id": COMMAND_ID, "wait_seconds": 1})
    payload = _payload(response)

    assert not response.isError
    assert payload["data"]["status"] == "COMPLETE"
    assert payload["next"] is None
    assert [args[0] for name, args, _ in fake_c2.calls if name == "request_get"] == [
        f"/api/v1/commands/{COMMAND_ID}", f"/api/v1/agents/{agent_guid}",
        f"/api/v1/commands/{COMMAND_ID}", f"/api/v1/commands/{COMMAND_ID}",
    ]


@pytest.mark.parametrize("phase", ["initial", "later"])
async def test_total_tool_deadline_bounds_a_blocking_sdk_worker(fake_c2, fast_poll, phase):
    """The outer wait must beat the separately configured 30-second HTTP limit."""
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def request_get(uri):
        fake_c2._record("request_get", uri)
        calls.append(uri)
        assert uri == f"/api/v1/commands/{COMMAND_ID}"
        if phase == "later" and len(calls) == 1:
            return _command()
        try:
            release.wait(timeout=3)
            return _command("COMPLETE")
        finally:
            finished.set()

    fake_c2.request_get = request_get
    interface = (MCPMode.NATIVE, "")
    try:
        result = await asyncio.wait_for(
            _harness(fake_c2, interface)({"command_id": COMMAND_ID, "wait_seconds": 1}),
            timeout=2,
        )
        payload = _payload(result)
        assert not finished.is_set(), "The caller remained blocked on the abandoned SDK worker"
        if phase == "initial":
            assert result.isError
            assert payload["code"] == "RESULT_WAIT_TIMEOUT"
            expected_arguments = {"command_id": COMMAND_ID}
        else:
            assert not result.isError
            assert payload["data"]["status"] == "SENT"
            assert payload["data"]["wait_timed_out"] is True
            expected_arguments = {"command_id": COMMAND_ID, "wait_seconds": 1}
        assert payload["next"] == _expected_next(interface, expected_arguments)
        assert [name for name, _, _ in fake_c2.calls] == ["request_get"] * (1 if phase == "initial" else 2)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


async def test_tool_caller_cancellation_propagates_through_cache_and_registry(
    fake_c2, interface, monkeypatch,
):
    import tuoni_mcp.tools.commands as commands

    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def read(invocation, path):
        calls.append(path)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(commands, "_read", read)
    task = asyncio.create_task(_harness(fake_c2, interface)({
        "command_id": COMMAND_ID, "wait_seconds": 30,
    }))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert calls == [f"/api/v1/commands/{COMMAND_ID}"]
    assert fake_c2.calls == []


@pytest.mark.parametrize("arguments", [
    {"wait_seconds": -1}, {"wait_seconds": 31}, {"wait_seconds": True},
    {"wait_seconds": "1"}, {"wait_seconds": 1.0}, {"wait_seconds": None},
    {"wait_seconds": 1, "part_index": 0}, {"wait_seconds": 1, "offset_chars": 0},
    {"wait_seconds": 1, "max_chars": 12},
])
async def test_invalid_wait_or_text_window_combinations_fail_before_read(fake_c2, interface, arguments):
    result = await _harness(fake_c2, interface)({"command_id": COMMAND_ID, **arguments})

    assert result.isError
    assert _payload(result)["code"] == "INVALID_ARGUMENTS"
    assert fake_c2.calls == []


async def test_zero_wait_preserves_existing_text_window_arguments(fake_c2, interface):
    fake_c2.responses[f"/api/v1/commands/{COMMAND_ID}"] = _command("COMPLETE", parts=[
        {"type": "text", "name": "stdout", "value": "abc"},
    ])
    result = await _harness(fake_c2, interface)({
        "command_id": COMMAND_ID, "wait_seconds": 0, "part_index": 0, "max_chars": 1,
    })

    assert not result.isError, _payload(result)
    assert _payload(result)["data"]["result"]["part"]["value"] == "a"
    assert _payload(result)["next"] == _expected_next(interface, {
        "command_id": COMMAND_ID, "part_index": 0, "offset_chars": 1, "max_chars": 1,
        "result_version": _payload(result)["data"]["result"]["part"]["result_version"],
    })


async def test_auth_refresh_during_wait_retries_only_the_same_read(fake_c2, interface, fast_poll):
    calls = []

    def request_get(uri):
        fake_c2._record("request_get", uri)
        calls.append(uri)
        assert uri == f"/api/v1/commands/{COMMAND_ID}"
        if len(calls) == 1:
            raise ExceptionTuoniAuthentication("expired")
        return _command("SENT" if len(calls) == 2 else "COMPLETE")

    fake_c2.request_get = request_get
    result = await _harness(fake_c2, interface)({"command_id": COMMAND_ID, "wait_seconds": 1})

    assert not result.isError, _payload(result)
    assert _payload(result)["data"]["status"] == "COMPLETE"
    assert [name for name, _, _ in fake_c2.calls] == ["request_get", "login", "request_get", "request_get"]
