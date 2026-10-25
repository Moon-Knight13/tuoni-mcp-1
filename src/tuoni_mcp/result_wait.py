"""Deadline-bounded observation of an existing command, without submission."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anyio

TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED", "CANCELED"})
INITIAL_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 5.0
MAX_CALLBACK_POLL_SECONDS = 30
CALLBACK_GRACE_SECONDS = 0.25
CALLBACK_LOOKUP_SECONDS = 1.0


@dataclass(frozen=True)
class Observation:
    value: Any
    timed_out: bool = False
    poll_after_seconds: int | None = None


class InitialReadTimeout(TimeoutError):
    """The wait elapsed before a first observation was available."""


def callback_poll_seconds(value: Any, *, now: float | None = None) -> int | None:
    """Return a bounded polling hint from an agent's callback prediction.

    The backend's expected time is a forecast, not proof of delivery. Poll from
    the earliest edge of its jitter window so uncertainty does not become an
    additional execution delay. Invalid or elapsed forecasts use normal backoff.
    This helper accepts either nextCallback itself or the complete agent record.
    """
    if not isinstance(value, Mapping):
        return None
    if "nextCallback" in value:
        value = value["nextCallback"]
    if not isinstance(value, Mapping):
        return None
    expected = value.get("expectedTime")
    if not isinstance(expected, str) or not expected or len(expected) > 64:
        return None
    try:
        instant = datetime.fromisoformat(
            expected.removesuffix("Z") + "+00:00" if expected.endswith("Z") else expected
        )
        if instant.tzinfo is None or instant.utcoffset() is None:
            return None
        predicted = instant.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    jitter = value.get("randomMs")
    if jitter is not None and (type(jitter) is not int or not 0 <= jitter <= 2**63 - 1):
        return None
    wall_clock = time.time() if now is None else now
    if not math.isfinite(wall_clock) or not math.isfinite(predicted):
        return None
    until_expected = predicted - wall_clock
    if until_expected <= 0:
        return None
    # Jitter exceeding the whole remaining interval makes an immediate callback
    # possible. Do not sleep through that uncertainty or trust enormous values.
    earliest = until_expected - (jitter or 0) / 1000
    if earliest <= 0:
        return None
    return max(1, min(MAX_CALLBACK_POLL_SECONDS, math.ceil(earliest + CALLBACK_GRACE_SECONDS)))


async def read_until_terminal(
    fetch: Callable[[], Awaitable[Any]], wait_seconds: float,
    *,
    fetch_next_callback: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
) -> Observation:
    """Bound authentication, pool waits, requests and sleeps by one deadline.

    A pending observation is returned on deadline, explicitly marked timed out.
    A failed read remains an error; cancellation from the MCP caller propagates.
    The synchronous HTTP worker may finish later, within its own transport limit.
    """
    if wait_seconds <= 0:
        return Observation(await fetch())
    deadline = time.monotonic() + wait_seconds
    observed = False
    latest: Any = None
    delay = INITIAL_POLL_SECONDS
    callback_by_phase: dict[str, Any] = {}
    callback: Any = None
    with anyio.move_on_after(wait_seconds):
        while time.monotonic() < deadline:
            latest = await fetch()
            observed = True
            if not isinstance(latest, Mapping) or str(latest.get("status", "")).upper() in TERMINAL_STATUSES:
                return Observation(latest)
            phase = str(latest.get("status", "")).upper()
            callback = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if fetch_next_callback is not None and phase in {"CREATED", "SENT"}:
                if phase not in callback_by_phase:
                    callback_by_phase[phase] = None
                    try:
                        # Optional timing must not consume the whole command
                        # wait when this separate metadata read is slow.
                        with anyio.move_on_after(min(CALLBACK_LOOKUP_SECONDS, remaining)):
                            callback_by_phase[phase] = await fetch_next_callback(latest)
                    except Exception:
                        # Callback metadata is optional. Failure must not hide an
                        # observed command or cause resubmission. Caller/deadline
                        # cancellation is a BaseException and still propagates.
                        callback_by_phase[phase] = None
                callback = callback_by_phase[phase]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            predicted_delay = callback_poll_seconds(callback)
            await asyncio.sleep(min(predicted_delay if predicted_delay is not None else delay, remaining))
            delay = min(delay * 2, MAX_POLL_SECONDS)
    if not observed:
        raise InitialReadTimeout("Wait expired before command status was observed.")
    return Observation(latest, timed_out=True, poll_after_seconds=callback_poll_seconds(callback))
