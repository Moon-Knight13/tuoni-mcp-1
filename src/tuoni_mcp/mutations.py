"""Request-level mutation acknowledgments, scoped to one tool invocation.

Only counts are retained: request bodies, URLs, and credentials never enter the
journal. Unknown outcomes remain unknown when a worker outlives its caller.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MutationJournal:
    accepted: int = 0
    rejected: int = 0
    unknown: int = 0
    rejection_status: int | None = None
    _closed: bool = False
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def begin(self) -> MutationAttempt:
        with self._lock:
            if self._closed:
                raise RuntimeError("The operation ended before this mutation request began.")
            self.unknown += 1
        return MutationAttempt(self)

    def snapshot(self, *, succeeded: bool = False, started: bool = False) -> dict[str, Any]:
        with self._lock:
            accepted, rejected, unknown = self.accepted, self.rejected, self.unknown
            rejection_status = self.rejection_status
        if succeeded:
            state = "accepted"
        elif unknown:
            state = "unknown"
        elif accepted:
            state = "partial"
        elif rejected:
            state = "rejected"
        else:
            # Adapters without request observation cannot prove a rejection.
            state = "unknown" if started else "not_started"
        result = {"state": state, "accepted_requests": accepted,
                  "rejected_requests": rejected, "unknown_requests": unknown}
        if state == "rejected" and rejection_status is not None:
            result["http_status"] = rejection_status
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass
class MutationAttempt:
    journal: MutationJournal
    _answered: bool = False

    def respond(self, status: int) -> None:
        # Timeouts, proxy errors, and server errors can follow an applied write.
        # These statuses are explicit request rejections, not message guesses.
        rejected = status in {400, 401, 403, 404, 405, 406, 409, 410, 411, 412,
                              413, 414, 415, 416, 417, 422, 428, 429, 431}
        with self.journal._lock:
            if self._answered or self.journal._closed:
                return
            self._answered = True
            if 200 <= status < 300:
                self.journal.unknown -= 1
                self.journal.accepted += 1
            elif rejected:
                self.journal.unknown -= 1
                self.journal.rejected += 1
                self.journal.rejection_status = status


_JOURNAL: ContextVar[MutationJournal | None] = ContextVar("tuoni_mutations", default=None)


@contextmanager
def mutation_scope(journal: MutationJournal):
    token = _JOURNAL.set(journal)
    try:
        yield journal
    finally:
        journal.close()
        _JOURNAL.reset(token)


def mutation_request(method: str) -> MutationAttempt | None:
    journal = _JOURNAL.get()
    if journal is not None and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        return journal.begin()
    return None
