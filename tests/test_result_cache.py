from __future__ import annotations

import asyncio

from tuoni_mcp.result_cache import CommandResultCache, RedactedText


def command(text="output", status="COMPLETE"):
    return {"id": 42, "status": status, "result": {"childResults": [{"type": "text", "value": text}]}}


async def test_completed_snapshot_coalesces_fetches_and_redacts_before_offsets():
    calls = 0
    original = command("prefix password=secret-value suffix")

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return original

    cache = CommandResultCache()
    snapshots = await asyncio.gather(*(cache.get(42, fetch) for _ in range(8)))
    assert calls == 1
    assert all(value is snapshots[0] for value in snapshots)
    text = snapshots[0]["result"]["childResults"][0]["value"]
    assert isinstance(text, RedactedText)
    assert text == "prefix password=[REDACTED] suffix"
    original["result"]["childResults"][0]["value"] = "changed"
    assert (await cache.get(42, fetch))["result"]["childResults"][0]["value"] == text
    assert not cache._locks


async def test_pending_and_incomplete_results_are_never_cached():
    cache = CommandResultCache()
    calls = 0
    value = command(status="ONGOING")

    async def fetch():
        nonlocal calls
        calls += 1
        return value

    for value in (command(status="ONGOING"), {"status": "COMPLETE", "result": None}):
        await cache.get(42, fetch)
        await cache.get(42, fetch)
    assert calls == 4
    assert not cache._entries


async def test_cache_expiry_lru_and_oversize_limits():
    now = 0
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return command()

    cache = CommandResultCache(ttl_seconds=10, max_entries=2, max_bytes=300, clock=lambda: now)
    await cache.get(1, fetch)
    await cache.get(2, fetch)
    await cache.get(1, fetch)
    await cache.get(3, fetch)
    assert list(cache._entries) == [1, 3]
    assert cache._bytes <= 300
    await cache.get(2, fetch)
    assert calls == 4
    now = 11
    await cache.get(2, fetch)
    assert calls == 5
    assert list(cache._entries) == [2]

    async def oversized():
        return command("x" * 500)

    await cache.get(4, oversized)
    assert 4 not in cache._entries
    assert cache._bytes <= 300
