"""Bounded reads of existing command history through Tuoni's paginated API."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlencode

from pydantic import Field
from tuoni.TuoniExceptions import ExceptionTuoniRequestFailed

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.pagination import bounded_text, compact_json, decode_cursor, encode_cursor, page_coverage
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, redact_text, would_fit_success
from tuoni_mcp.registry import (
    Effect,
    InvocationContext,
    ToolFailure,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    ToolSuccess,
)
from tuoni_mcp.tools._helpers import call

_NAME = "tuoni_commands_list"
_MAX_ID = 2**31 - 1
_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_UUID = re.compile(_UUID_PATTERN)
_STATUSES = {"CREATED", "SENT", "ONGOING", "CANCELED", "FAILED", "COMPLETE"}
CommandStatus = Literal["CREATED", "SENT", "ONGOING", "CANCELED", "FAILED", "COMPLETE"]


class CommandsListInput(ToolInput):
    agent_guid: Annotated[str, Field(strict=True, pattern=_UUID_PATTERN)] | None = None
    status: CommandStatus | None = None
    cursor: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=25)] = 10


def _digest(value: Any) -> str:
    return hashlib.sha256(compact_json(value).encode("utf-8")).hexdigest()[:24]


def _integer(value: Any, minimum: int = 0, maximum: int = _MAX_ID) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _invalid_response() -> ToolFailure:
    return ToolFailure(
        "INVALID_SERVER_RESPONSE",
        "Tuoni returned invalid command history metadata or pagination.",
        hint="Use a compatible Tuoni Server 0.17.0 or newer; no history records were returned.",
    )


def _stale_cursor() -> ToolFailure:
    return ToolFailure(
        "CURSOR_STALE",
        "Command history changed while following this cursor.",
        hint="Restart without a cursor and deduplicate previously seen command_id values.",
    )


def _cursor(arguments: CommandsListInput, scope: str) -> dict[str, Any] | None:
    if arguments.cursor is None:
        return None
    try:
        state = decode_cursor(arguments.cursor, tool=_NAME)
        keys = {"v", "tool", "scope", "page", "offset", "total", "last_id", "page_ids"}
        if set(state) != keys or type(state["v"]) is not int:
            raise ValueError("cursor fields are invalid")
        if state["scope"] != scope:
            raise ToolFailure(
                "CURSOR_SCOPE_CHANGED",
                "Command history filters or limit changed while continuing a cursor.",
                hint="Use the exact next call returned by the previous page.",
            )
        if not (
            _integer(state["total"], 1)
            and _integer(state["page"], maximum=(state["total"] - 1) // arguments.limit)
            and _integer(state["offset"], maximum=arguments.limit - 1)
            and _integer(state["last_id"], 1)
            and state["page"] * arguments.limit + state["offset"] < state["total"]
            and state["page"] + state["offset"] > 0
        ):
            raise ValueError("cursor position is invalid")
        if state["offset"]:
            if not isinstance(state["page_ids"], str) or not re.fullmatch(r"[0-9a-f]{24}", state["page_ids"]):
                raise ValueError("cursor page identity is invalid")
        elif state["page_ids"] is not None:
            raise ValueError("cursor page identity is invalid")
        return state
    except ValueError as exc:
        raise ToolFailure(
            getattr(exc, "code", "INVALID_CURSOR"), str(exc),
            hint="Restart this listing without a cursor.",
        ) from exc


def _summary(raw: Any, arguments: CommandsListInput) -> dict[str, Any]:
    if not isinstance(raw, dict) or not _integer(raw.get("id"), 1):
        raise _invalid_response()
    # IDs are validated and copied exactly. Truncating an ID would turn a
    # displayed receipt into an unusable (or different) command/template/agent.
    for field in ("agentGuid", "commandTemplateId"):
        value = raw.get(field)
        if not isinstance(value, str) or _UUID.fullmatch(value) is None:
            raise _invalid_response()
    status = raw.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        raise _invalid_response()
    if arguments.agent_guid is not None and raw["agentGuid"].lower() != arguments.agent_guid.lower():
        raise _invalid_response()
    if arguments.status is not None and status != arguments.status:
        raise _invalid_response()
    if not isinstance(raw.get("created"), str) or not raw["created"]:
        raise _invalid_response()
    for field in ("commandTemplateName", "sent"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise _invalid_response()
    return {
        "command_id": raw["id"],
        "agent_guid": raw["agentGuid"],
        "template_id": raw["commandTemplateId"],
        "template_name": bounded_text(redact_text(raw.get("commandTemplateName") or ""), 96),
        "status": status,
        "created": bounded_text(redact_text(raw["created"]), 64),
        "sent": bounded_text(redact_text(raw.get("sent") or ""), 64),
    }


def _page(raw: Any, page: int, arguments: CommandsListInput) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise _invalid_response()
    numeric = ("page", "pageSize", "itemCount", "totalCount", "totalPages")
    if any(not _integer(raw.get(field)) for field in numeric) or type(raw.get("hasMorePages")) is not bool:
        raise _invalid_response()
    total = raw["totalCount"]
    expected_count = max(0, min(arguments.limit, total - page * arguments.limit))
    if (
        raw["page"] != page
        or raw["pageSize"] != arguments.limit
        or len(raw["items"]) != raw["itemCount"]
        or raw["itemCount"] != expected_count
        or raw["totalPages"] != (total + arguments.limit - 1) // arguments.limit
        or raw["hasMorePages"] != ((page + 1) * arguments.limit < total)
    ):
        raise _invalid_response()
    summaries = [_summary(item, arguments) for item in raw["items"]]
    ids = [item["command_id"] for item in summaries]
    if any(left <= right for left, right in zip(ids, ids[1:])):
        raise _invalid_response()
    return summaries, total


def register_specs(registry: ToolRegistry, _cfg: Settings) -> None:
    async def commands_list(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(CommandsListInput, model)
        scope = _digest([arguments.agent_guid, arguments.status, arguments.limit])
        state = _cursor(arguments, scope)
        page, offset = (state["page"], state["offset"]) if state is not None else (0, 0)
        params: list[tuple[str, str | int]] = [
            ("page", page), ("pageSize", arguments.limit), ("sort", "ID:DESC"),
        ]
        if arguments.agent_guid is not None:
            params.append(("agentGuid", arguments.agent_guid))
        if arguments.status is not None:
            params.append(("status", arguments.status))
        try:
            # BoundedTuoniC2 applies the configured streamed HTTP byte/deadline
            # limits. v2 still carries configuration and bounded result entries;
            # only the allowlisted metadata above reaches the model.
            raw = await call(
                invocation.context, invocation.server.c2.request_get,
                "/api/v2/commands?" + urlencode(params),
            )
        except ExceptionTuoniRequestFailed as exc:
            if getattr(exc, "status_code", None) in {404, 405}:
                raise ToolFailure(
                    "INCOMPATIBLE_TUONI_SERVER",
                    "Tuoni does not expose the required paginated /api/v2/commands endpoint.",
                    hint="Use Tuoni Server 0.17.0 or newer; unbounded v1 history is not fetched.",
                ) from exc
            raise
        items, total = _page(raw, page, arguments)
        ids = [item["command_id"] for item in items]
        page_ids = _digest(ids)
        if state is not None:
            if total != state["total"] or offset >= len(items):
                raise _stale_cursor()
            if offset and (page_ids != state["page_ids"] or ids[offset - 1] != state["last_id"]):
                raise _stale_cursor()
            if ids[offset] >= state["last_id"]:
                raise _stale_cursor()

        def continuation(consumed: int) -> tuple[str | None, dict[str, Any] | None]:
            if page * arguments.limit + consumed >= total:
                return None, None
            partial = consumed < len(items)
            token = encode_cursor({
                "tool": _NAME, "scope": scope, "page": page if partial else page + 1,
                "offset": consumed if partial else 0, "total": total,
                "last_id": ids[consumed - 1], "page_ids": page_ids if partial else None,
            })
            next_arguments: dict[str, Any] = {"limit": arguments.limit, "cursor": token}
            if arguments.agent_guid is not None:
                next_arguments["agent_guid"] = arguments.agent_guid
            if arguments.status is not None:
                next_arguments["status"] = arguments.status
            return token, {"name": _NAME, "arguments": next_arguments}

        def data(selected: list[dict[str, Any]]) -> dict[str, Any]:
            start = page * arguments.limit + offset
            return {
                "commands": selected, "returned": len(selected), "page": page, "total": total,
                "coverage": page_coverage(offset=start, returned=len(selected), total=total,
                                          has_more=start + len(selected) < total),
                "returned_status_counts": dict(Counter(item["status"] for item in selected)),
            }

        selected: list[dict[str, Any]] = []
        next_cursor, next_call = None, None
        for index in range(offset, len(items)):
            candidate = [*selected, items[index]]
            candidate_cursor, candidate_call = continuation(index + 1)
            if not would_fit_success(
                data(candidate), next_cursor=candidate_cursor, next_call=candidate_call,
                budget=LIST_RESULT_BUDGET,
            ):
                break
            selected = candidate
            next_cursor, next_call = candidate_cursor, candidate_call
        if not selected and items:
            raise ToolFailure(
                "RESPONSE_TOO_LARGE", "One command history summary exceeds the 4 KiB list budget.",
                hint="Reduce the configured tool prefix length and restart this listing.",
            )
        return ToolSuccess(data(selected), next_cursor=next_cursor, next_call=next_call)

    registry.add(ToolSpec(
        name=_NAME,
        description="List a page of command history, newest IDs first. Status counts describe only returned records.",
        input_model=CommandsListInput,
        handler=commands_list,
        profiles=frozenset({Profile.AGENT_OPS}),
        effect=Effect.READ,
        budget=LIST_RESULT_BUDGET,
        usage_note=(
            "coverage.complete means the full filtered inventory is in this response. "
            "Follow next; restart and deduplicate IDs if history changes."
        ),
        keywords=("commands", "history", "receipts", "recent", "submitted", "recover command id"),
    ))
