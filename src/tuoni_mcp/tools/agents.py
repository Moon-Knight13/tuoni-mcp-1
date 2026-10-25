from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, cast
from urllib.parse import urlencode

from pydantic import Field, field_validator
from tuoni.TuoniExceptions import ExceptionTuoniRequestFailed

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.pagination import bounded_text, decode_cursor, encode_cursor, redact
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET, would_fit_success
from tuoni_mcp.registry import (
    AliasedToolInput,
    Effect,
    InvocationContext,
    ToolFailure,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    ToolSuccess,
)
from tuoni_mcp.template_cache import cache_for
from tuoni_mcp.tools._helpers import call



class AgentsListInput(AliasedToolInput):
    input_aliases = {"search": "query"}
    active: bool = True
    inactive: bool = False
    query: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=256,
            description=(
                "Match all case-insensitive words across hostname, process, user, GUID or IP. "
                "Each call scans at most 100 agents."
            ),
        ),
    ] = None
    process_name: str | None = Field(
        default=None, min_length=1, max_length=256,
        description="Exact process basename, case-insensitive.",
    )
    cursor: str | None = None
    limit: Annotated[int, Field(ge=1, le=25)] = 10

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must contain a search term")
        return value


class AgentGetInput(ToolInput):
    agent_guid: Annotated[str, Field(min_length=1, max_length=256)]


class AgentGuidInput(AgentGetInput):
    pass


class AgentPropertyInput(AgentGetInput):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    value: Any = None


def _metadata_value(metadata: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in metadata.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def _next_callback(raw: dict[str, Any]) -> dict[str, Any]:
    callback = raw.get("nextCallback")
    if not isinstance(callback, dict):
        return {}
    result = {}
    expected = callback.get("expectedTime")
    if isinstance(expected, str) and len(expected) <= 64:
        try:
            parsed = datetime.fromisoformat(expected.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                result["expected_time"] = expected
    random_ms = callback.get("randomMs")
    if type(random_ms) is int and 0 <= random_ms <= 2**63 - 1:
        result["random_ms"] = random_ms
    return result


def _matches_query(raw: dict[str, Any], query: str) -> bool:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    values = [
        raw.get("guid"),
        _metadata_value(metadata, "hostname", "hostName"),
        _metadata_value(metadata, "username", "user"),
        _metadata_value(metadata, "proc", "processName", "process"),
    ]
    ips = _metadata_value(metadata, "ips", "ip", "ipAddress")
    values.extend(ips[:64] if isinstance(ips, list) else [ips])
    interfaces = _metadata_value(metadata, "netInterfaces")
    for interface in interfaces[:64] if isinstance(interfaces, list) else []:
        if not isinstance(interface, dict):
            continue
        for key in ("ipv4Addresses", "ipv6Addresses"):
            addresses = interface.get(key)
            if isinstance(addresses, list):
                values.extend(addresses[:64])
    haystack = "\n".join(value.casefold() for value in values if isinstance(value, str))
    return all(term in haystack for term in query.casefold().split())


def _matches_process(raw: dict[str, Any], name: str) -> bool:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    process = _metadata_value(metadata, "proc", "processName", "process")
    return isinstance(process, str) and process.replace("\\", "/").rsplit("/", 1)[-1].casefold() == name.casefold()


def _agent_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep architecture as the OS value; report a distinct known process value.

    Tuoni's wire fields are osArch/processArch. Older integrations used
    architecture/arch for the OS value. An omitted process_architecture does
    not assert that the architectures match: the process value may be unknown.
    """
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    architecture = bounded_text(
        _metadata_value(metadata, "osArch")
        or _metadata_value(metadata, "architecture", "arch"),
        32,
    )
    process_architecture = bounded_text(_metadata_value(metadata, "processArch"), 32)
    summary = {
        "guid": bounded_text(raw.get("guid"), 128),
        "status": bounded_text(raw.get("status"), 32),
        "active": bool(raw.get("active")),
        "last_callback_time": bounded_text(raw.get("lastCallbackTime"), 64),
        "hostname": bounded_text(_metadata_value(metadata, "hostname", "hostName"), 128),
        "username": bounded_text(_metadata_value(metadata, "username", "user"), 128),
        "os": bounded_text(_metadata_value(metadata, "os", "operatingSystem"), 96),
        "architecture": architecture,
    }
    if process_architecture and process_architecture.casefold() != architecture.casefold():
        summary["process_architecture"] = process_architecture
    process = _metadata_value(metadata, "proc", "processName", "process")
    if isinstance(process, str) and process:
        summary["process"] = bounded_text(process.replace("\\", "/").rsplit("/", 1)[-1], 128)
    pid = _metadata_value(metadata, "pid", "processId")
    if type(pid) is int and 0 <= pid <= 2**63 - 1:
        summary["pid"] = pid
    callback = _next_callback(raw)
    if callback:
        summary["next_callback"] = callback
    return summary


async def _fetch(invocation: InvocationContext, path: str) -> Any:
    return await call(invocation.context, invocation.server.c2.request_get, path)


def _cursor_failure(exc: ValueError) -> ToolFailure:
    return ToolFailure(
        getattr(exc, "code", "INVALID_CURSOR"),
        str(exc),
        hint="Restart this listing without a cursor.",
    )


def register_specs(registry: ToolRegistry, _cfg: Settings) -> None:
    """Register the lean agent-ops reads and additive administration actions."""

    async def agents_list(invocation: InvocationContext, model: ToolInput) -> ToolSuccess:
        arguments = cast(AgentsListInput, model)
        if not arguments.active and not arguments.inactive:
            raise ToolFailure(
                "EMPTY_AGENT_FILTER",
                "At least one of active or inactive must be true.",
                hint="Use active=true for the default operational view.",
            )

        page = 0
        item_offset = 0
        query_hash = (
            hashlib.sha256(arguments.query.casefold().encode()).hexdigest()
            if arguments.query is not None else None
        )
        process_hash = (hashlib.sha256(arguments.process_name.casefold().encode()).hexdigest()
                        if arguments.process_name is not None else None)
        searching = arguments.query is not None or arguments.process_name is not None
        if arguments.cursor:
            try:
                cursor = decode_cursor(arguments.cursor, tool="tuoni_agents_list")
            except ValueError as exc:
                raise _cursor_failure(exc) from exc
            expected = {
                "active": arguments.active,
                "inactive": arguments.inactive,
                "limit": arguments.limit,
                "query_hash": query_hash,
                "process_hash": process_hash,
            }
            if any(cursor.get(key) != value for key, value in expected.items()):
                raise ToolFailure(
                    "CURSOR_SCOPE_CHANGED",
                    "Agent filters or limit changed while continuing a cursor.",
                    hint="Use the exact next call returned by the previous page.",
                )
            try:
                page = cursor["page"]
                item_offset = cursor.get("item_offset", 0)
                if any(type(value) is not int or value < 0 for value in (page, item_offset)):
                    raise ValueError("invalid cursor position")
            except (KeyError, TypeError, ValueError) as exc:
                raise _cursor_failure(ValueError("cursor has no valid page")) from exc

        page_size = 100 if searching else arguments.limit
        params: list[tuple[str, str | int]] = [("page", page), ("pageSize", page_size)]
        if arguments.active != arguments.inactive:
            params.append(("status", "ACTIVE" if arguments.active else "INACTIVE"))
        try:
            raw = await _fetch(invocation, "/api/v2/agents?" + urlencode(params))
        except ExceptionTuoniRequestFailed as exc:
            message = str(exc.message or exc).casefold()
            status_code = getattr(exc, "status_code", None)
            if status_code in {404, 405} or (
                status_code is None
                and any(marker in message for marker in ("404", "405", "not found", "no route"))
            ):
                raise ToolFailure(
                    "INCOMPATIBLE_TUONI_SERVER",
                    "Tuoni does not expose the required paginated /api/v2/agents endpoint.",
                    hint="Requires Tuoni Server 0.17.0 or newer.",
                ) from exc
            # Preserve definite permission failures and transient transport
            # failures for the shared classifier rather than retrying every 4xx.
            raise
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ToolFailure(
                "INCOMPATIBLE_TUONI_SERVER",
                "Tuoni did not return the required paginated /api/v2/agents response.",
                hint="Requires Tuoni Server 0.17.0 or newer.",
            )
        if searching and len(raw["items"]) > page_size:
            raise ToolFailure(
                "INVALID_SERVER_RESPONSE",
                "Tuoni returned more agents than the bounded search page requested.",
                hint="The server must honor the agents pageSize limit.",
            )
        raw_items = [item for item in raw["items"] if isinstance(item, dict)]
        page_items = [_agent_summary(item) for item in raw_items]
        if not 0 <= item_offset <= len(page_items):
            raise ToolFailure("INVALID_CURSOR", "Agent cursor offset is outside the current server page.")

        def continuation(next_page: int, next_offset: int) -> tuple[str, dict[str, Any]]:
            position = {
                "tool": "tuoni_agents_list", "page": next_page, "item_offset": next_offset,
                "active": arguments.active, "inactive": arguments.inactive, "limit": arguments.limit,
            }
            if query_hash is not None:
                position["query_hash"] = query_hash
            if process_hash is not None:
                position["process_hash"] = process_hash
            cursor_value = encode_cursor(position)
            next_arguments = {
                "active": arguments.active, "inactive": arguments.inactive,
                "limit": arguments.limit, "cursor": cursor_value,
            }
            if arguments.query is not None:
                next_arguments["query"] = arguments.query
            if arguments.process_name is not None:
                next_arguments["process_name"] = arguments.process_name
            return cursor_value, {
                "name": "tuoni_agents_list",
                "arguments": next_arguments,
            }

        def page_data(items: list[dict[str, Any]], scanned: int, has_more: bool) -> dict[str, Any]:
            data = {"agents": items, "page": int(raw.get("page", page)), "returned": len(items)}
            total = int(raw.get("totalCount", len(page_items)))
            if not searching:
                data["total"] = total
            else:
                data.update(scanned=scanned, inventory_total=total, search_complete=not has_more)
            return data

        selected: list[dict[str, Any]] = []
        consumed = item_offset
        oversized = False
        for index in range(item_offset, len(page_items)):
            if arguments.query is not None and not _matches_query(raw_items[index], arguments.query):
                consumed = index + 1
                continue
            if arguments.process_name is not None and not _matches_process(raw_items[index], arguments.process_name):
                consumed = index + 1
                continue
            candidate = [*selected, page_items[index]]
            next_index = index + 1
            if next_index < len(page_items):
                candidate_cursor, candidate_call = continuation(page, next_index)
            elif raw.get("hasMorePages"):
                candidate_cursor, candidate_call = continuation(page + 1, 0)
            else:
                candidate_cursor, candidate_call = None, None
            candidate_data = page_data(candidate, next_index - item_offset, candidate_cursor is not None)
            if not would_fit_success(
                candidate_data,
                next_cursor=candidate_cursor,
                next_call=candidate_call,
                budget=LIST_RESULT_BUDGET,
            ):
                oversized = True
                break
            selected = candidate
            consumed = index + 1
            if len(selected) == arguments.limit:
                break

        if consumed < len(page_items):
            next_cursor, next_call = continuation(page, consumed)
        elif raw.get("hasMorePages"):
            next_cursor, next_call = continuation(page + 1, 0)
        else:
            next_cursor, next_call = None, None
        data = page_data(selected, consumed - item_offset, next_cursor is not None)
        if not selected and oversized:
            raise ToolFailure(
                "RESPONSE_TOO_LARGE",
                "One compact agent record exceeds the 4 KiB list budget.",
                hint="Inspect one agent by its exact GUID.",
            )
        return ToolSuccess(data, next_cursor=next_cursor, next_call=next_call)

    async def agent_get(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        arguments = cast(AgentGetInput, model)
        cache = cache_for(invocation.server)

        async def fetch(path: str) -> Any:
            return await _fetch(invocation, path)

        raw = await cache.agent(fetch, arguments.agent_guid)
        if raw is None:
            raise ToolFailure("AGENT_NOT_FOUND", "Agent was not found.", hint="Refresh the agent list.")
        capabilities = raw.get("availableCommandTemplates") or []
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        for max_items, max_string in ((32, 512), (16, 256), (8, 128)):
            data = {
                "guid": bounded_text(raw.get("guid"), 128),
                "status": bounded_text(raw.get("status"), 32),
                "active": bool(raw.get("active")),
                "first_registration_time": bounded_text(raw.get("firstRegistrationTime"), 64),
                "last_callback_time": bounded_text(raw.get("lastCallbackTime"), 64),
                "recent_listeners": redact(raw.get("recentListeners") or [], max_items=16, max_string_bytes=128),
                "metadata": redact(metadata, max_items=max_items, max_string_bytes=max_string),
                "capability_count": len(capabilities),
                "capabilities_note": "Use command search for bounded compatible-command discovery.",
            }
            callback = _next_callback(raw)
            if callback:
                data["next_callback"] = callback
            if would_fit_success(data, budget=DETAIL_RESULT_BUDGET):
                return data
        raise ToolFailure(
            "RESPONSE_TOO_LARGE",
            "Agent operational metadata cannot fit the 6 KiB detail budget.",
            hint="Use the compact agent list.",
        )

    registry.add(
        ToolSpec(
            name="tuoni_agents_list",
            description="List or search active agents. An agent is a program running on a target machine.",
            input_model=AgentsListInput,
            handler=agents_list,
            profiles=frozenset({Profile.AGENT_OPS}),
            effect=Effect.READ,
            budget=LIST_RESULT_BUDGET,
            usage_note=(
                "Copy a returned guid to work with that agent. Follow next for more pages, "
                "even when a search page has no matches."
            ),
            keywords=(
                "agents", "sessions", "hosts", "active", "implants", "beacons",
                "targets", "machines", "who is connected", "find", "search", "hostname", "workstation",
            ),
        )
    )
    registry.add(
        ToolSpec(
            name="tuoni_agent_get",
            description="Get one agent's platform, status and capability count by guid.",
            input_model=AgentGetInput,
            handler=agent_get,
            profiles=frozenset({Profile.AGENT_OPS}),
            effect=Effect.READ,
            budget=DETAIL_RESULT_BUDGET,
            usage_note="Returns no command schemas; use command search for those.",
            keywords=("agent", "metadata", "capabilities", "host", "platform", "os"),
        )
    )

    async def admin_put(invocation: InvocationContext, path: str, data: dict[str, Any]) -> dict[str, Any]:
        await call(invocation.context, invocation.server.c2.request_put, path)
        return data

    async def agent_block(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        args = cast(AgentGuidInput, model)
        return await admin_put(
            invocation,
            f"/api/v1/agents/{url_path_segment(args.agent_guid)}/block",
            {"agent_guid": args.agent_guid, "status": "BLOCKED"},
        )

    async def agent_restore(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        args = cast(AgentGuidInput, model)
        return await admin_put(
            invocation,
            f"/api/v1/agents/{url_path_segment(args.agent_guid)}/restore",
            {"agent_guid": args.agent_guid, "status": "ACTIVE"},
        )

    async def agent_set_inactive(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        args = cast(AgentGuidInput, model)
        return await admin_put(
            invocation,
            f"/api/v1/agents/{url_path_segment(args.agent_guid)}/inactive",
            {"agent_guid": args.agent_guid, "active": False},
        )

    async def agent_set_property(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        args = cast(AgentPropertyInput, model)
        updated = await call(
            invocation.context,
            invocation.server.c2.request_put,
            f"/api/v1/agents/{url_path_segment(args.agent_guid)}/metadata",
            {"customProperties": {args.name: args.value}},
        )
        return {"agent_guid": args.agent_guid, "metadata": redact(updated)}

    async def agent_clear_queue(invocation: InvocationContext, model: ToolInput) -> dict[str, Any]:
        args = cast(AgentGuidInput, model)
        await call(
            invocation.context,
            invocation.server.c2.request_post,
            f"/api/v1/agents/{url_path_segment(args.agent_guid)}/commands/queue/clear",
        )
        return {"agent_guid": args.agent_guid, "cleared": True}

    admin_specs = (
        ("tuoni_agent_block", "Block an agent.", AgentGuidInput, agent_block),
        ("tuoni_agent_restore", "Restore a blocked agent.", AgentGuidInput, agent_restore),
        ("tuoni_agent_set_inactive", "Mark an agent inactive.", AgentGuidInput, agent_set_inactive),
        (
            "tuoni_agent_set_custom_property",
            "Set or remove one agent custom property.",
            AgentPropertyInput,
            agent_set_property,
        ),
        ("tuoni_agent_clear_queue", "Clear an agent's pending command queue.", AgentGuidInput, agent_clear_queue),
    )
    for name, description, input_model, handler in admin_specs:
        registry.add(
            ToolSpec(
                name=name,
                description=description,
                input_model=input_model,
                handler=handler,
                profiles=frozenset({Profile.ADMINISTRATION}),
                effect=Effect.MUTATE,
                budget=DETAIL_RESULT_BUDGET,
                keywords=("agent", "administration"),
            )
        )
