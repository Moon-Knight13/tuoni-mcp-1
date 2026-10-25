from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from tuoni_mcp.protocol import redact


PreparationKind = Literal["command.create", "command.update", "command.stop", "tool.call"]
PreparationState = Literal["prepared", "executing", "executed", "ambiguous", "rejected"]


@dataclass(frozen=True)
class PreparedAttachment:
    field: str
    filename: str | None = None
    content: bytes | None = field(default=None, repr=False)
    file_id: str | None = None
    file_fingerprint: str | None = None
    content_fingerprint: str | None = None

    @property
    def private_size(self) -> int:
        return len(self.content or b"") + len(
            (
                self.field
                + (self.filename or "")
                + (self.file_id or "")
                + (self.file_fingerprint or "")
                + (self.content_fingerprint or "")
            ).encode("utf-8")
        )

    def summary(self) -> dict[str, Any]:
        if self.file_id is not None:
            return {
                "field": self.field,
                "source": "c2_file",
                "file_id": self.file_id,
                "filename": self.filename,
            }
        return {
            "field": self.field,
            "source": "inline",
            "filename": self.filename,
            "decoded_bytes": len(self.content or b""),
        }


@dataclass
class PreparationRecord:
    preparation_id: str
    kind: PreparationKind
    payload: dict[str, Any] = field(repr=False)
    attachments: tuple[PreparedAttachment, ...] = field(default=(), repr=False)
    summary: dict[str, Any] = field(default_factory=dict)
    state_fingerprint: str | None = None
    created_at: float = 0.0
    expires_at: float = 0.0
    state: PreparationState = "prepared"
    receipt: dict[str, Any] | None = None
    replay_until: float | None = None
    ambiguous_error: dict[str, Any] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    stored_size: int = 0


class PreparationStore:
    """Bounded process-local store for explicit two-phase mutations."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 5 * 60,
        replay_seconds: float = 10 * 60,
        max_records: int = 128,
        max_bytes: int = 2 * 1024 * 1024,
        clock=time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.replay_seconds = replay_seconds
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._clock = clock
        self._records: dict[str, PreparationRecord] = {}
        self._bytes = 0
        self._guard = asyncio.Lock()

    @staticmethod
    def _estimate_size(
        payload: dict[str, Any],
        attachments: tuple[PreparedAttachment, ...],
        summary: dict[str, Any],
        *,
        state_fingerprint: str | None = None,
        receipt: dict[str, Any] | None = None,
        ambiguous_error: dict[str, Any] | None = None,
    ) -> int:
        encoded = json.dumps(
            {
                "payload": payload,
                "summary": summary,
                "state_fingerprint": state_fingerprint,
                "receipt": receipt,
                "ambiguous_error": ambiguous_error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return len(encoded) + sum(item.private_size for item in attachments) + 256

    def _remove_unlocked(self, preparation_id: str) -> None:
        record = self._records.pop(preparation_id, None)
        if record is not None:
            self._bytes -= record.stored_size

    def _purge_unlocked(self, now: float) -> None:
        for preparation_id, record in list(self._records.items()):
            expired_preparation = record.state == "prepared" and record.expires_at <= now
            expired_receipt = record.state in {"executed", "ambiguous", "rejected"} and (
                record.replay_until is None or record.replay_until <= now
            )
            if expired_preparation or expired_receipt:
                self._remove_unlocked(preparation_id)

    @staticmethod
    def _compact_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        """Retain a definite-success marker and useful scalar identities only."""
        retained: dict[str, Any] = {
            "execution_succeeded": True,
            "details_omitted": True,
        }
        for key in (
            "operation",
            "tool_name",
            "command_id",
            "template_id",
            "template_name",
            "status",
            "created",
            "sent",
            "updated",
            "stopped",
        ):
            value = receipt.get(key)
            if value is None or not isinstance(value, (str, bool, int, float)):
                continue
            safe = redact({key: value})[key]
            if isinstance(safe, str) and len(safe.encode("utf-8")) > 256:
                encoded = safe.encode("utf-8")[:256]
                safe = encoded.decode("utf-8", errors="ignore") + "…"
            retained[key] = safe
        return retained

    async def create(
        self,
        kind: PreparationKind,
        *,
        payload: dict[str, Any],
        attachments: tuple[PreparedAttachment, ...] = (),
        summary: dict[str, Any],
        state_fingerprint: str | None = None,
    ) -> PreparationRecord:
        now = self._clock()
        stored_size = self._estimate_size(
            payload,
            attachments,
            summary,
            state_fingerprint=state_fingerprint,
        )
        if stored_size > self.max_bytes:
            raise ValueError("preparation exceeds the process-local storage limit")
        async with self._guard:
            self._purge_unlocked(now)
            # A terminal receipt is the idempotency record for a remote mutation.
            # Never evict it before its replay deadline: rejecting new work is
            # safer than turning an execute retry into a duplicate submission.
            if (
                len(self._records) >= self.max_records
                or self._bytes + stored_size > self.max_bytes
            ):
                raise ValueError("preparation store is at capacity")

            preparation_id = secrets.token_urlsafe(24)
            while preparation_id in self._records:  # practically impossible, kept deterministic
                preparation_id = secrets.token_urlsafe(24)
            record = PreparationRecord(
                preparation_id=preparation_id,
                kind=kind,
                payload=payload,
                attachments=attachments,
                summary=summary,
                state_fingerprint=state_fingerprint,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                stored_size=stored_size,
            )
            self._records[preparation_id] = record
            self._bytes += stored_size
            return record

    async def get(self, preparation_id: str) -> PreparationRecord | None:
        now = self._clock()
        async with self._guard:
            self._purge_unlocked(now)
            return self._records.get(preparation_id)

    async def begin(self, record: PreparationRecord) -> None:
        now = self._clock()
        async with self._guard:
            current = self._records.get(record.preparation_id)
            if current is not record:
                raise KeyError("preparation no longer exists")
            if record.state == "prepared" and record.expires_at <= now:
                self._remove_unlocked(record.preparation_id)
                raise KeyError("preparation expired")
            if record.state != "prepared":
                raise RuntimeError(f"preparation is already {record.state}")
            record.state = "executing"

    async def restore_prepared(self, record: PreparationRecord) -> None:
        async with self._guard:
            if self._records.get(record.preparation_id) is record and record.state == "executing":
                record.state = "prepared"

    async def complete(self, record: PreparationRecord, receipt: dict[str, Any]) -> None:
        async with self._guard:
            if self._records.get(record.preparation_id) is not record:
                return
            self._purge_unlocked(self._clock())
            self._bytes -= record.stored_size

            retained = dict(receipt)
            retained_size = self._estimate_size({}, (), {}, receipt=retained)
            if retained_size > self.max_bytes:
                retained = self._compact_receipt(receipt)
                retained_size = self._estimate_size({}, (), {}, receipt=retained)

            if self._bytes + retained_size > self.max_bytes:
                retained = {"execution_succeeded": True, "details_omitted": True}
                retained_size = self._estimate_size({}, (), {}, receipt=retained)
            if self._bytes + retained_size > self.max_bytes:
                # The record's executed state is itself the definite marker. An
                # empty receipt is smaller than the JSON null stored before
                # execution, so a record that previously fit still fits here.
                retained = {}
                retained_size = self._estimate_size({}, (), {}, receipt=retained)

            record.state = "executed"
            receipt.clear()
            receipt.update(retained)
            record.receipt = receipt
            record.ambiguous_error = None
            record.replay_until = self._clock() + self.replay_seconds
            # Drop request bodies and decoded attachment data immediately after a
            # definite success. The review summary and drift fingerprint are no
            # longer needed either; only the compact receipt remains replayable.
            record.payload = {}
            record.attachments = ()
            record.summary = {}
            record.state_fingerprint = None
            record.stored_size = self._estimate_size(
                {},
                (),
                {},
                receipt=retained,
            )
            self._bytes += record.stored_size

    async def mark_ambiguous(
        self, record: PreparationRecord, error: dict[str, Any], *, rejected: bool = False,
    ) -> None:
        async with self._guard:
            if self._records.get(record.preparation_id) is not record:
                return
            self._purge_unlocked(self._clock())
            self._bytes -= record.stored_size
            retained_error = redact(error)
            retained_size = self._estimate_size(
                {},
                (),
                {},
                ambiguous_error=retained_error,
            )
            if self._bytes + retained_size > self.max_bytes:
                retained_error = {
                    "message": "The operation outcome is ambiguous; inspect server state."
                }
                retained_size = self._estimate_size(
                    {},
                    (),
                    {},
                    ambiguous_error=retained_error,
                )
            if self._bytes + retained_size > self.max_bytes:
                retained_error = {}
                retained_size = self._estimate_size(
                    {},
                    (),
                    {},
                    ambiguous_error=retained_error,
                )
            record.state = "rejected" if rejected else "ambiguous"
            record.receipt = None
            record.ambiguous_error = retained_error
            record.replay_until = self._clock() + self.replay_seconds
            record.payload = {}
            record.attachments = ()
            record.summary = {}
            record.state_fingerprint = None
            record.stored_size = self._estimate_size(
                {},
                (),
                {},
                ambiguous_error=retained_error,
            )
            self._bytes += record.stored_size

    async def stats(self) -> tuple[int, int]:
        async with self._guard:
            self._purge_unlocked(self._clock())
            return len(self._records), self._bytes
