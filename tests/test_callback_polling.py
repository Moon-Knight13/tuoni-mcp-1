from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tuoni_mcp import result_wait


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()


def _forecast(seconds=10, random_ms=None):
    return {
        "expectedTime": datetime.fromtimestamp(NOW + seconds, timezone.utc).isoformat(),
        "randomMs": random_ms,
    }


def _command(status="SENT"):
    return {"id": 42, "agentGuid": "fixture-agent", "status": status}


@pytest.mark.parametrize(
    ("forecast", "expected"),
    [
        (_forecast(10), 11),
        (_forecast(10, 2000), 9),
        (_forecast(0.1), 1),
        (_forecast(3600), 30),
        ({"nextCallback": _forecast(10, 2000)}, 9),
        ({"expectedTime": "2026-10-23T01:00:10+01:00"}, 11),
        ({"expectedTime": "2026-10-23T00:00:10Z"}, 11),
        (_forecast(-1), None),
        (_forecast(0), None),
        (_forecast(10, 10000), None),
        (_forecast(10, 20000), None),
        (_forecast(10, -1), None),
        (_forecast(10, True), None),
        (_forecast(10, 1.5), None),
        (_forecast(10, "2000"), None),
        (_forecast(10, 2**63), None),
        ({"expectedTime": "2026-10-23T00:00:10"}, None),
        ({"expectedTime": "2026-10-23"}, None),
        ({"expectedTime": "not-a-date"}, None),
        ({"expectedTime": "x" * 1000}, None),
        ({"expectedTime": 123}, None),
        ({"nextCallback": None}, None),
        ({}, None),
        (None, None),
    ],
)
def test_callback_hint_is_bounded_and_tolerates_optional_metadata(forecast, expected):
    assert result_wait.callback_poll_seconds(forecast, now=NOW) == expected


@pytest.mark.parametrize("now", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_clock_does_not_produce_callback_hint(now):
    assert result_wait.callback_poll_seconds(_forecast(), now=now) is None


def _clock(monkeypatch):
    clock = [0.0]
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(
        result_wait, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: NOW + clock[0]),
    )
    monkeypatch.setattr(result_wait, "asyncio", SimpleNamespace(sleep=sleep))
    return clock, sleeps


async def test_forecast_avoids_intermediate_polls_and_accounts_for_jitter(monkeypatch):
    clock, sleeps = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command("CREATED" if len(reads) == 1 else "COMPLETE")

    async def next_callback(command):
        hints.append(command["status"])
        return _forecast(12, 2000)

    observed = await result_wait.read_until_terminal(fetch, 30, fetch_next_callback=next_callback)

    assert observed.value["status"] == "COMPLETE"
    assert not observed.timed_out
    assert reads == [0, 11]
    assert sleeps == [11]
    assert hints == ["CREATED"]


async def test_callback_metadata_is_read_at_most_once_per_delivery_phase(monkeypatch):
    clock, sleeps = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command("CREATED" if len(reads) <= 3 else "SENT" if len(reads) <= 5 else "COMPLETE")

    async def next_callback(command):
        hints.append(command["status"])
        return None

    observed = await result_wait.read_until_terminal(fetch, 30, fetch_next_callback=next_callback)

    assert observed.value["status"] == "COMPLETE"
    assert hints == ["CREATED", "SENT"]
    assert reads == [0, 1, 3, 7, 12, 17]
    assert sleeps == [1, 2, 4, 5, 5]


async def test_callback_forecast_is_refreshed_when_command_is_delivered(monkeypatch):
    clock, sleeps = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command(["CREATED", "SENT", "COMPLETE"][len(reads) - 1])

    async def next_callback(command):
        hints.append(command["status"])
        return _forecast(4 if command["status"] == "CREATED" else 10)

    observed = await result_wait.read_until_terminal(fetch, 30, fetch_next_callback=next_callback)

    assert observed.value["status"] == "COMPLETE"
    assert hints == ["CREATED", "SENT"]
    assert reads == [0, 5, 11]
    assert sleeps == [5, 6]


async def test_forecast_and_last_sleep_fit_the_remaining_deadline(monkeypatch):
    clock, sleeps = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command()

    async def next_callback(command):
        hints.append(command["status"])
        return _forecast(100)

    observed = await result_wait.read_until_terminal(fetch, 2.5, fetch_next_callback=next_callback)

    assert observed.timed_out
    assert observed.poll_after_seconds == 30
    assert reads == [0]
    assert hints == ["SENT"]
    assert sleeps == [2.5]


async def test_optional_timing_failure_keeps_normal_command_observation(monkeypatch):
    clock, sleeps = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command("SENT" if len(reads) < 3 else "COMPLETE")

    async def next_callback(command):
        hints.append(command["status"])
        raise ConnectionError("metadata unavailable")

    observed = await result_wait.read_until_terminal(fetch, 30, fetch_next_callback=next_callback)

    assert observed.value["status"] == "COMPLETE"
    assert not observed.timed_out
    assert reads == [0, 1, 3]
    assert sleeps == [1, 2]
    assert hints == ["SENT"]


@pytest.mark.parametrize("status", ["COMPLETE", "FAILED", "CANCELED", "ONGOING"])
async def test_no_callback_lookup_for_terminal_or_ongoing_commands(monkeypatch, status):
    clock, _ = _clock(monkeypatch)
    reads, hints = [], []

    async def fetch():
        reads.append(clock[0])
        return _command(status)

    async def next_callback(command):
        hints.append(command)
        return _forecast()

    observed = await result_wait.read_until_terminal(fetch, 2, fetch_next_callback=next_callback)

    assert hints == []
    assert observed.timed_out is (status == "ONGOING")
    assert len(reads) == (2 if status == "ONGOING" else 1)


async def test_zero_wait_does_not_fetch_optional_callback_metadata():
    hints = []

    async def fetch():
        return _command()

    async def next_callback(command):
        hints.append(command)
        return _forecast()

    observed = await result_wait.read_until_terminal(fetch, 0, fetch_next_callback=next_callback)

    assert observed.value["status"] == "SENT"
    assert not observed.timed_out
    assert observed.poll_after_seconds is None
    assert hints == []


async def test_deadline_cancels_callback_metadata_read_and_keeps_last_command():
    cancelled = asyncio.Event()

    async def fetch():
        return _command()

    async def next_callback(command):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    observed = await asyncio.wait_for(
        result_wait.read_until_terminal(fetch, 0.03, fetch_next_callback=next_callback), timeout=1,
    )

    assert observed.value["status"] == "SENT"
    assert observed.timed_out
    assert cancelled.is_set()


async def test_slow_optional_metadata_does_not_consume_whole_command_wait(monkeypatch):
    cancelled = asyncio.Event()
    reads = []
    monkeypatch.setattr(result_wait, "CALLBACK_LOOKUP_SECONDS", 0.01)
    monkeypatch.setattr(result_wait, "INITIAL_POLL_SECONDS", 0.001)

    async def fetch():
        reads.append(True)
        return _command("SENT" if len(reads) == 1 else "COMPLETE")

    async def next_callback(command):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    observed = await asyncio.wait_for(
        result_wait.read_until_terminal(fetch, 5, fetch_next_callback=next_callback), timeout=1,
    )

    assert observed.value["status"] == "COMPLETE"
    assert not observed.timed_out
    assert cancelled.is_set()
    assert len(reads) == 2


async def test_caller_cancellation_propagates_from_callback_metadata_read():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fetch():
        return _command()

    async def next_callback(command):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(result_wait.read_until_terminal(fetch, 30, fetch_next_callback=next_callback))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
