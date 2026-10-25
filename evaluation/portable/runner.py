"""Drive the portable workflows over the real in-memory MCP transport."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp.shared.memory import create_connected_server_and_client_session

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.protocol import call_result_size

from evaluation.portable.world import PortableC2

# An MCP error with one of these codes means the scripted client emitted a call
# the advertised schema never accepted. Domain rejections stay "valid" because a
# well-formed call that Tuoni refuses still exercised the contract correctly.
INVALID_CALL_CODES = frozenset({"TOOL_NOT_FOUND", "INVALID_ARGUMENTS"})
OVERFLOW_CODES = frozenset({"RESPONSE_TOO_LARGE"})

PORTABLE_MODES = ("native", "dispatch")


@dataclass
class Call:
    """One recorded MCP tool call."""

    name: str
    ok: bool
    payload: dict[str, Any]
    size_bytes: int
    valid: bool
    overflow: bool

    @property
    def data(self) -> Any:
        return self.payload.get("data")

    @property
    def code(self) -> str:
        return str(self.payload.get("code") or "")

    @property
    def hint(self) -> str:
        return str(self.payload.get("hint") or "")

    @property
    def next_call(self) -> dict[str, Any] | None:
        following = self.payload.get("next")
        return following if isinstance(following, dict) else None


@dataclass
class WorkflowRecord:
    workflow_id: str
    completed: bool = False
    tool_calls: int = 0
    valid_tool_calls: int = 0
    context_overflows: int = 0
    unintended_mutations: int = 0
    result_bytes: int = 0
    max_result_bytes: int = 0
    request_bytes: int = 0
    upstream_reads: int = 0
    notes: list[str] = field(default_factory=list)

    def to_record(self, mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "workflow_id": self.workflow_id,
            "completed": self.completed,
            "tool_calls": self.tool_calls,
            "valid_tool_calls": self.valid_tool_calls,
            "context_overflows": self.context_overflows,
            "unintended_mutations": self.unintended_mutations,
            "result_bytes": self.result_bytes,
            "max_result_bytes": self.max_result_bytes,
            "request_bytes": self.request_bytes,
            "upstream_reads": self.upstream_reads,
        }


class Driver:
    """A disciplined scripted client.

    It only ever sends values the server handed back, and it routes through the
    dispatcher when the server is running in dispatch mode, so the same workflow
    script measures both exposures.
    """

    def __init__(self, session: Any, mode: str, world: PortableC2, record: WorkflowRecord) -> None:
        self._session = session
        self._mode = mode
        self._world = world
        self.record = record

    async def call(self, name: str, arguments: dict[str, Any]) -> Call:
        """Invoke one logical Tuoni tool, adapting to the current exposure."""

        if self._mode == "dispatch" and name != "tuoni_operation_execute":
            wire_name, wire_arguments = "tuoni_call", {"name": name, "arguments": arguments}
        else:
            wire_name, wire_arguments = name, arguments
        return await self._invoke(wire_name, wire_arguments, logical=name)

    async def follow(self, previous: Call) -> Call | None:
        """Follow a returned continuation verbatim, exactly as the skill demands."""

        following = previous.next_call
        if following is None:
            return None
        name = str(following.get("name") or "")
        arguments = following.get("arguments")
        if not name or not isinstance(arguments, dict):
            self.record.notes.append("continuation was not a callable tool call")
            return None
        # A continuation is already expressed in the exposed vocabulary, so it is
        # invoked directly rather than re-wrapped.
        return await self._invoke(name, arguments, logical=name)

    async def _invoke(self, wire_name: str, arguments: dict[str, Any], *, logical: str) -> Call:
        result = await self._session.call_tool(wire_name, arguments)
        size = call_result_size(result)
        blocks = [item.text for item in result.content if getattr(item, "type", None) == "text"]
        try:
            payload = json.loads(blocks[0]) if blocks else {}
        except (ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        is_error = bool(result.isError)
        code = str(payload.get("code") or "") if is_error else ""
        call = Call(
            name=logical,
            ok=not is_error,
            payload=payload,
            size_bytes=size,
            valid=code not in INVALID_CALL_CODES,
            overflow=code in OVERFLOW_CODES,
        )

        self.record.tool_calls += 1
        self.record.valid_tool_calls += int(call.valid)
        self.record.context_overflows += int(call.overflow)
        self.record.result_bytes += size
        self.record.request_bytes += len(json.dumps(
            {"name": wire_name, "arguments": arguments}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        self.record.max_result_bytes = max(self.record.max_result_bytes, size)
        return call


def _settings(mode: str) -> Settings:
    return Settings(
        url="https://portable.test:8443",
        username="portable",
        password="portable",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
        mode=MCPMode.NATIVE if mode == "native" else MCPMode.DISPATCH,
    )


@asynccontextmanager
async def _connected(mode: str, world: PortableC2) -> AsyncIterator[tuple[Any, Any]]:
    """Build a server bound to the fixture world and connect an MCP client."""

    from tuoni_mcp import connection, server as server_mod

    original_c2 = server_mod.TuoniC2
    original_server_login = server_mod.login
    original_connection_login = connection.login

    async def _login(context: Any) -> None:
        context.last_login = time.monotonic()

    server_mod.TuoniC2 = lambda *args, **kwargs: world  # type: ignore[assignment]
    server_mod.login = _login  # type: ignore[assignment]
    connection.login = _login  # type: ignore[assignment]
    try:
        server = server_mod.build_server(_settings(mode))
        async with create_connected_server_and_client_session(server.protocol_server) as session:
            yield server, session
    finally:
        server_mod.TuoniC2 = original_c2  # type: ignore[assignment]
        server_mod.login = original_server_login  # type: ignore[assignment]
        connection.login = original_connection_login  # type: ignore[assignment]


async def run_mode(mode: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run all twenty workflows against one exposure and return their records."""

    from evaluation.portable.plans import WORKFLOW_PLANS

    records: list[dict[str, Any]] = []
    catalog: dict[str, int] = {}
    for workflow_id, plan in WORKFLOW_PLANS:
        world = PortableC2()
        record = WorkflowRecord(workflow_id=workflow_id)
        async with _connected(mode, world) as (server, session):
            if not catalog:
                tools = await session.list_tools()
                catalog = {
                    "catalog_tools": len(tools.tools),
                    "catalog_bytes": len(tools.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")),
                    "instructions_bytes": len(server.instructions.encode("utf-8")),
                }
            driver = Driver(session, mode, world, record)
            try:
                record.completed = bool(await plan(driver, world))
            except Exception as exc:  # a harness failure is a workflow failure
                record.completed = False
                record.notes.append(f"{type(exc).__name__}: {exc}")
        allowed = plan.allowed_mutations
        observed = len(world.mutating_calls)
        record.unintended_mutations = max(0, observed - allowed)
        record.upstream_reads = sum(method == "GET" for method, _ in world.calls)
        records.append(record.to_record(mode))
    return records, catalog


async def run_portable_evaluation() -> dict[str, Any]:
    """Run every workflow in both exposures and return a scorer-ready document."""

    # The MCP server logs one line per request. Forty workflows of that buries
    # the report, and the transcript is not the harness's evidence anyway.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

    results: list[dict[str, Any]] = []
    catalogs: dict[str, dict[str, int]] = {}
    for mode in PORTABLE_MODES:
        mode_records, catalog = await run_mode(mode)
        results.extend(mode_records)
        catalogs[mode] = catalog
    return {
        "schema_version": 1,
        "run": {
            "kind": "portable",
            **{
                metric: {mode: catalog[metric] for mode, catalog in catalogs.items()}
                for metric in ("catalog_tools", "catalog_bytes", "instructions_bytes")
            },
        },
        "results": results,
    }
