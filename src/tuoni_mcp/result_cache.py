"""Bounded, lifespan-owned snapshots for completed command text windows."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio

from tuoni_mcp.connection import _ABANDON_KW
from tuoni_mcp.protocol import redact as redact_result


class RedactedText(str):
    """A complete text part sanitized before offsets are applied."""


def result_text(value: Any) -> str:
    if isinstance(value, RedactedText):
        return value
    return str(redact_result(value if isinstance(value, str) else ""))


@dataclass(frozen=True)
class _Entry:
    value: Mapping[str, Any]
    expires_at: float
    size: int


def _snapshot(raw: Mapping[str, Any], max_bytes: int) -> tuple[Mapping[str, Any], int] | None:
    # Measure the retained JSON data, including non-text parts, before copying it.
    size = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    if size > max_bytes:
        return None
    value = copy.deepcopy(raw)
    for part in value["result"]["childResults"]:
        if isinstance(part, dict) and part.get("type") == "text":
            part["value"] = RedactedText(result_text(part.get("value")))
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    return (value, size) if size <= max_bytes else None


class CommandResultCache:
    """Cache terminal results for 60 seconds, at most 64 records / 8 MiB of JSON.

    Returned snapshots are private read-only inputs to the result renderer. They
    never replace fresh reads used by prepare or execution preflight. Pending
    results and terminal responses without a complete parts list stay uncached.
    """

    def __init__(
        self,
        ttl_seconds: float = 60,
        *,
        max_entries: int = 64,
        max_bytes: int = 8 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1 or max_bytes < 1:
            raise ValueError("Result cache limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._bytes = 0
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def _cached(self, command_id: int) -> Mapping[str, Any] | None:
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._bytes -= self._entries.pop(key).size
        entry = self._entries.get(command_id)
        if entry is None:
            return None
        self._entries.move_to_end(command_id)
        return entry.value

    async def get(self, command_id: int, fetch: Callable[[], Awaitable[Any]]) -> Any:
        cached = self._cached(command_id)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(command_id, asyncio.Lock())
        async with lock:
            cached = self._cached(command_id)
            if cached is not None:
                return cached
            raw = await fetch()
            if not isinstance(raw, Mapping) or str(raw.get("status", "")).upper() not in {
                "COMPLETE", "FAILED", "CANCELED"
            }:
                return raw
            result = raw.get("result")
            if not isinstance(result, Mapping) or not isinstance(result.get("childResults"), list):
                return raw
            # This copy has no side effects. Let cancellation release the caller
            # and its per-command lock even if the bounded copy is still running.
            snapshot = await anyio.to_thread.run_sync(_snapshot, raw, self.max_bytes, **{_ABANDON_KW: True})
            if snapshot is None:
                return raw
            value, size = snapshot
            while self._entries and (
                len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes
            ):
                _, removed = self._entries.popitem(last=False)
                self._bytes -= removed.size
            self._entries[command_id] = _Entry(value, self._clock() + self.ttl_seconds, size)
            self._bytes += size
            return value


def cache_for(server: Any) -> CommandResultCache:
    cache = getattr(server, "command_result_cache", None)
    if not isinstance(cache, CommandResultCache):
        cache = CommandResultCache()
        setattr(server, "command_result_cache", cache)
    return cache
