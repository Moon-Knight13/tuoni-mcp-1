from __future__ import annotations

import asyncio

import pytest

from tuoni_mcp.preparations import PreparedAttachment, PreparationStore


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_store_defaults_match_public_ttl_replay_and_capacity_contract() -> None:
    store = PreparationStore()
    assert store.ttl_seconds == 5 * 60
    assert store.replay_seconds == 10 * 60
    assert store.max_records == 128
    assert store.max_bytes == 2 * 1024 * 1024


async def test_preparation_ids_are_opaque_unique_and_expire_unexecuted() -> None:
    clock = Clock()
    store = PreparationStore(ttl_seconds=300, replay_seconds=600, clock=clock)
    first = await store.create(
        "command.create",
        payload={"command": "sensitive command body"},
        summary={"kind": "command.create"},
    )
    second = await store.create(
        "command.stop",
        payload={"command_id": 7},
        summary={"kind": "command.stop"},
    )

    assert first.preparation_id != second.preparation_id
    assert len(first.preparation_id) >= 24
    assert await store.get(first.preparation_id) is first
    clock.advance(300)
    assert await store.get(first.preparation_id) is None
    assert await store.get(second.preparation_id) is None
    assert await store.stats() == (0, 0)


async def test_execution_receipt_replays_then_expires_and_drops_private_body() -> None:
    clock = Clock()
    store = PreparationStore(ttl_seconds=300, replay_seconds=600, clock=clock)
    record = await store.create(
        "command.create",
        payload={"configuration": {"password": "do-not-retain"}},
        attachments=(
            PreparedAttachment(field="input", filename="secret.bin", content=b"secret bytes"),
        ),
        summary={"kind": "command.create", "review": "s" * 4096},
        state_fingerprint="f" * 4096,
    )
    prepared_size = record.stored_size

    await store.begin(record)
    await store.complete(record, {"command_id": 42, "status": "QUEUED"})

    replay = await store.get(record.preparation_id)
    assert replay is record
    assert replay.state == "executed"
    assert replay.receipt == {"command_id": 42, "status": "QUEUED"}
    assert replay.payload == {}
    assert replay.attachments == ()
    assert replay.summary == {}
    assert replay.state_fingerprint is None
    assert replay.stored_size < prepared_size
    assert await store.stats() == (1, replay.stored_size)
    clock.advance(599.9)
    assert await store.get(record.preparation_id) is record
    clock.advance(0.1)
    assert await store.get(record.preparation_id) is None


async def test_oversized_execution_receipt_is_compacted_within_store_limit() -> None:
    store = PreparationStore(max_bytes=4096)
    record = await store.create(
        "tool.call",
        payload={"tool_name": "example_mutation"},
        summary={"operation": "tool.call"},
    )
    receipt = {
        "operation": "tool.call",
        "tool_name": "example_mutation",
        "result": {"body": "x" * 100_000},
    }

    await store.begin(record)
    await store.complete(record, receipt)

    count, stored_bytes = await store.stats()
    assert count == 1
    assert stored_bytes <= store.max_bytes
    assert record.state == "executed"
    assert record.receipt == receipt
    assert receipt["execution_succeeded"] is True
    assert receipt["details_omitted"] is True
    assert receipt["tool_name"] == "example_mutation"
    assert "result" not in receipt


async def test_ambiguous_outcome_is_replayable_and_never_restored_for_retry() -> None:
    clock = Clock()
    store = PreparationStore(clock=clock)
    record = await store.create(
        "command.update",
        payload={"command_id": 9, "update": "possibly accepted"},
        summary={"kind": "command.update", "review": "s" * 4096},
        state_fingerprint="f" * 4096,
    )
    prepared_size = record.stored_size

    await store.begin(record)
    await store.mark_ambiguous(
        record,
        {"code": "AMBIGUOUS_OUTCOME", "message": "Inspect command state."},
    )
    replay = await store.get(record.preparation_id)

    assert replay is record
    assert replay.state == "ambiguous"
    assert replay.payload == {}
    assert replay.attachments == ()
    assert replay.summary == {}
    assert replay.state_fingerprint is None
    assert replay.receipt is None
    assert replay.stored_size < prepared_size
    assert await store.stats() == (1, replay.stored_size)
    assert replay.ambiguous_error == {
        "code": "AMBIGUOUS_OUTCOME",
        "message": "Inspect command state.",
    }


async def test_state_fingerprint_is_included_in_store_byte_accounting() -> None:
    without_fingerprint = PreparationStore()
    baseline = await without_fingerprint.create(
        "command.create",
        payload={"configuration": {"command": "whoami"}},
        summary={"kind": "command.create"},
    )
    with_fingerprint = PreparationStore()
    fingerprinted = await with_fingerprint.create(
        "command.create",
        payload={"configuration": {"command": "whoami"}},
        summary={"kind": "command.create"},
        state_fingerprint="f" * 4096,
    )

    # Replacing JSON null with a quoted 4096-byte value adds all but the two
    # quote/null representation bytes to the retained-size estimate.
    assert fingerprinted.stored_size >= baseline.stored_size + 4090
    assert await with_fingerprint.stats() == (1, fingerprinted.stored_size)


async def test_capacity_never_evicts_live_preparations() -> None:
    store = PreparationStore(max_records=1, max_bytes=16_384)
    active = await store.create(
        "command.stop",
        payload={"command_id": 1},
        summary={"kind": "command.stop"},
    )

    with pytest.raises(ValueError, match="capacity"):
        await store.create(
            "command.stop",
            payload={"command_id": 2},
            summary={"kind": "command.stop"},
        )
    assert await store.get(active.preparation_id) is active


async def test_single_preparation_cannot_exceed_private_store_byte_limit() -> None:
    store = PreparationStore(max_bytes=2 * 1024 * 1024)
    with pytest.raises(ValueError, match="storage limit"):
        await store.create(
            "command.create",
            payload={"private_body": "x" * (2 * 1024 * 1024)},
            summary={"kind": "command.create"},
        )
    assert await store.stats() == (0, 0)


async def test_terminal_receipt_is_not_evicted_under_capacity_pressure() -> None:
    clock = Clock()
    store = PreparationStore(
        max_records=1,
        max_bytes=16_384,
        replay_seconds=600,
        clock=clock,
    )
    completed = await store.create(
        "command.stop",
        payload={"command_id": 1},
        summary={"kind": "command.stop"},
    )
    await store.begin(completed)
    await store.complete(completed, {"command_id": 1, "stopped": True})

    with pytest.raises(ValueError, match="capacity"):
        await store.create(
            "command.stop",
            payload={"command_id": 2},
            summary={"kind": "command.stop"},
        )
    assert await store.get(completed.preparation_id) is completed

    clock.advance(600)
    replacement = await store.create(
        "command.stop",
        payload={"command_id": 2},
        summary={"kind": "command.stop"},
    )
    assert await store.get(completed.preparation_id) is None
    assert await store.get(replacement.preparation_id) is replacement


async def test_ambiguous_marker_is_not_evicted_under_capacity_pressure() -> None:
    clock = Clock()
    store = PreparationStore(max_records=1, replay_seconds=600, clock=clock)
    ambiguous = await store.create(
        "command.stop",
        payload={"command_id": 1},
        summary={"kind": "command.stop"},
    )
    await store.begin(ambiguous)
    await store.mark_ambiguous(ambiguous, {"message": "Inspect state."})

    with pytest.raises(ValueError, match="capacity"):
        await store.create(
            "command.stop",
            payload={"command_id": 2},
            summary={"kind": "command.stop"},
        )
    assert await store.get(ambiguous.preparation_id) is ambiguous
    clock.advance(600)
    replacement = await store.create(
        "command.stop",
        payload={"command_id": 2},
        summary={"kind": "command.stop"},
    )
    assert await store.get(ambiguous.preparation_id) is None
    assert await store.get(replacement.preparation_id) is replacement


async def test_record_lock_serializes_competing_execution_attempts() -> None:
    store = PreparationStore()
    record = await store.create(
        "command.stop",
        payload={"command_id": 1},
        summary={"kind": "command.stop"},
    )
    entered: list[int] = []
    release_first = asyncio.Event()

    async def contender(number: int) -> None:
        async with record.lock:
            entered.append(number)
            if number == 1:
                await release_first.wait()

    first = asyncio.create_task(contender(1))
    await asyncio.sleep(0)
    second = asyncio.create_task(contender(2))
    await asyncio.sleep(0)
    assert entered == [1]
    release_first.set()
    await asyncio.gather(first, second)
    assert entered == [1, 2]


async def test_begin_rejects_any_non_prepared_state() -> None:
    store = PreparationStore()
    record = await store.create(
        "command.stop",
        payload={"command_id": 1},
        summary={"kind": "command.stop"},
    )
    await store.begin(record)

    with pytest.raises(RuntimeError, match="already executing"):
        await store.begin(record)
