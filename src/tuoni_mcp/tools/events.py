from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.events import URI_TO_TOPIC
from tuoni_mcp.pagination import CursorError, bounded_text, decode_cursor, encode_cursor, redact
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, would_fit_success
from tuoni_mcp.registry import (
    Effect,
    InvocationContext,
    ToolFailure,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    ToolSuccess,
)


EventSourceUri = Literal[
    "tuoni://events/agents/new",
    "tuoni://events/agents/heartbeat",
    "tuoni://events/agents/active",
    "tuoni://events/agents/metadata",
    "tuoni://events/commands/sent",
    "tuoni://events/commands/results",
    "tuoni://events/listeners/new",
    "tuoni://events/listeners/modified",
    "tuoni://events/listeners/removed",
    "tuoni://events/payloads/new",
    "tuoni://events/payloads/archived",
    "tuoni://events/users/new",
    "tuoni://events/users/modified",
    "tuoni://events/command-templates/new",
    "tuoni://events/command-templates/removed",
]


class EventsPollInput(ToolInput):
    cursor: str | None = Field(
        default=None,
        max_length=2048,
        description="Opaque cursor returned by the preceding poll; omit to start now.",
    )
    topics: list[EventSourceUri] | None = Field(
        default=None,
        max_length=15,
        description="Optional event source URIs from this schema's enum; omit for all sources.",
    )
    limit: int = Field(default=20, ge=1, le=50)
    start: Literal["now", "earliest"] | None = Field(
        default=None,
        description=(
            "First poll only: now (default) subscribes before an action; earliest replays retained events "
            "after an action. Omit when following a cursor. Retention is process-local and bounded."
        ),
    )


def _cursor(sequence: int, topics: tuple[str, ...]) -> str:
    return encode_cursor({"tool": "tuoni_events_poll", "after": sequence, "topics": topics})


def _decode_cursor(value: str | None, topics: tuple[str, ...], current: int) -> int:
    if value is None:
        # A first poll observes events arriving after subscription rather than replaying
        # an arbitrary process-local ring buffer.
        return current
    try:
        payload = decode_cursor(value, tool="tuoni_events_poll")
        if tuple(payload.get("topics") or ()) != topics:
            raise ToolFailure("CURSOR_SCOPE_CHANGED", "Event topics changed while continuing a cursor.",
                              hint="Use the previous next call, or restart with start:earliest to inspect retained events.")
        after = payload.get("after")
        if type(after) is not int or after < 0 or after > current:
            raise ValueError
        return after
    except (ValueError, TypeError) as exc:
        raise ToolFailure(
            getattr(exc, "code", "INVALID_CURSOR"),
            str(exc) if isinstance(exc, CursorError) else "The event cursor is invalid for the selected topics.",
            hint="Restart polling without a cursor.",
        ) from exc


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """Bound one event structurally before the protocol-level redaction pass."""
    del depth
    return redact(value, max_depth=3, max_items=6, max_string_bytes=96)


def _event_fallback(event: dict[str, Any]) -> dict[str, Any]:
    """Represent an individually oversized event without dropping its cursor position."""
    return {
        "sequence": event.get("sequence"),
        "source_uri": bounded_text(event.get("source_uri"), 128),
        "topic": bounded_text(event.get("topic"), 128),
        "received_at": event.get("received_at"),
        "data": {"omitted": True, "reason": "event data exceeded the polling byte budget"},
    }


def _next_call(cursor: str, topics: tuple[str, ...], limit: int) -> dict[str, Any]:
    arguments: dict[str, Any] = {"cursor": cursor, "limit": limit}
    if topics:
        arguments["topics"] = list(topics)
    return {"name": "tuoni_events_poll", "arguments": arguments}


async def _poll(invocation: InvocationContext, arguments: BaseModel) -> ToolSuccess:
    assert isinstance(arguments, EventsPollInput)
    if arguments.cursor is not None and arguments.start is not None:
        raise ToolFailure("INVALID_ARGUMENTS", "start is only valid on the first poll.",
                          hint="Follow next unchanged, or omit cursor to choose a new starting point.")
    bridge = invocation.server.event_bridge
    if bridge is None:
        raise ToolFailure(
            "EVENTS_UNAVAILABLE",
            "The event bridge is not active.",
            retryable=True,
            hint="Start the server with the events profile and verify WebSocket connectivity.",
        )

    topics = tuple(sorted(set(arguments.topics or ())))
    unknown = sorted(set(topics) - URI_TO_TOPIC.keys())
    if unknown:
        raise ToolFailure(
            "UNKNOWN_EVENT_TOPIC",
            "One or more event source URIs are unknown.",
            hint="Use exact tuoni://events/... source URIs documented for tuoni_events_poll.",
        )

    # Snapshot retained data and sequence bounds together, without an await.
    retained, oldest = bridge.events_after(0)
    current = max(0, bridge._next_sequence - 1)
    after = _decode_cursor(arguments.cursor, topics, current)
    if arguments.cursor is None and arguments.start == "earliest":
        after = oldest - 1 if oldest is not None else current
    available = [event for event in retained if int(event["sequence"]) > after]
    selected = [
        event for event in available if not topics or event.get("source_uri") in topics
    ]
    dropped = oldest is not None and after < oldest - 1
    loss = (
        {"dropped_pending_events": bridge.status.dropped_pending_events}
        if bridge.status.dropped_pending_events else {}
    )

    items: list[dict[str, Any]] = []
    consumed = after

    def page_data(events: list[dict[str, Any]], through: int) -> dict[str, Any]:
        return {
            "events": events, "returned": len(events),
            "connected": bridge.status.connected, "dropped_before_cursor": dropped,
            "after_sequence": after, "through_sequence": through,
            "earliest_sequence": oldest, "latest_sequence": current,
            "has_more": any(int(event["sequence"]) > through for event in selected),
            **loss,
        }

    for event in selected:
        if len(items) >= arguments.limit:
            break
        sequence = int(event["sequence"])
        candidate = [*items, _bounded(event)]
        through = current if event is selected[-1] else sequence
        candidate_cursor = _cursor(through, topics)
        candidate_next = _next_call(candidate_cursor, topics, arguments.limit)
        data = page_data(candidate, through)
        if not would_fit_success(
            data,
            next_cursor=candidate_cursor,
            next_call=candidate_next,
            budget=LIST_RESULT_BUDGET,
        ):
            if not items:
                candidate = [_event_fallback(event)]
                data["events"] = candidate
                if would_fit_success(
                    data,
                    next_cursor=candidate_cursor,
                    next_call=candidate_next,
                    budget=LIST_RESULT_BUDGET,
                ):
                    items = candidate
                    consumed = through
                else:
                    raise ToolFailure("RESPONSE_TOO_LARGE", "Event metadata exceeds the polling byte budget.")
            break
        items = candidate
        consumed = through

    # If no matching event was returned, advance across the snapshot so repeated
    # filtered polls do not rescan unrelated topics forever.
    if not items and available:
        consumed = int(available[-1]["sequence"])
    next_cursor = _cursor(consumed, topics)
    return ToolSuccess(
        page_data(items, consumed),
        next_cursor=next_cursor,
        next_call=_next_call(next_cursor, topics, arguments.limit),
    )


def register_specs(registry: ToolRegistry, _cfg: Settings) -> None:
    registry.add(
        ToolSpec(
            name="tuoni_events_poll",
            description=(
                "Poll before an action to subscribe from now; use start:earliest after an action to replay retained events. "
                "The topics input enumerates every supported source URI."
            ),
            input_model=EventsPollInput,
            handler=_poll,
            profiles=frozenset({Profile.EVENTS}),
            effect=Effect.READ,
            budget=LIST_RESULT_BUDGET,
            usage_note=(
                "Default first poll skips earlier events. Empty events does not mean an empty buffer: inspect "
                "earliest_sequence/latest_sequence. Replay is bounded process-local history. "
                "Follow next unchanged; start is only for a first poll."
            ),
            keywords=("websocket", "agent callback", "command result", "listener"),
        )
    )
