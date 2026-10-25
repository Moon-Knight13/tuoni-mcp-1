from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, Tool

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext, login
from tuoni_mcp.diagnostics import catalog_summary, collect_diagnostics
from tuoni_mcp.events import EventBridge
from tuoni_mcp.http_client import BoundedTuoniC2 as TuoniC2
from tuoni_mcp.protocol import error_result
from tuoni_mcp.registry import (
    InvocationContext,
    ToolRegistry,
    ToolSpec,
    build_dispatch_specs,
)
from tuoni_mcp.tools import build_registry


_COMMAND_WORKFLOW = (
    "To run a new command: tuoni_agents_list, tuoni_commands_search, tuoni_command_describe, "
    "tuoni_command_prepare, tuoni_operation_execute, then tuoni_command_get. "
    "Prepare only validates. "
    "Additive tools marked destructive change remote state immediately.\n"
    "Copy every prepare_arguments_example field, including template. "
    "Use shell_command if shown; otherwise its exact configuration fields.\n"
    "Execution settings control plugins; omit execution for embedded/native commands.\n"
)

_COMMON_INSTRUCTIONS = (
    "Search by intent or name; use returned short names and copy IDs exactly.\n"
    "Omit optional limits unless following a returned continuation.\n"
    "Follow next with unchanged arguments. If the client prefixes names, use the matching exposed tool.\n"
    "Get a result manifest before reading text. Summarize each window before continuing. "
    "Wait poll_after_seconds between polls. File paths are HTTP, not MCP resources.\n"
    "retryable:false: apply the hint or stop. Never repeat an ambiguous execution."
)

SERVER_INSTRUCTIONS = "Tuoni controls agents on target machines.\n" + _COMMAND_WORKFLOW + _COMMON_INSTRUCTIONS


def instructions_for(cfg: Settings) -> str:
    prefix = cfg.tool_prefix
    if Profile.AGENT_OPS not in cfg.profiles:
        workflow = ("Use the exposed profile-enabled read tools.\n" if cfg.read_only else
                    "Use the exposed profile-enabled tools. Destructive tools change remote state.\n")
    elif cfg.read_only:
        workflow = "Read existing commands with tuoni_command_get. This session exposes read tools only.\n"
    else:
        workflow = _COMMAND_WORKFLOW
    if cfg.mode is MCPMode.NATIVE:
        for name in ("tuoni_agents_list", "tuoni_commands_search", "tuoni_command_describe",
                     "tuoni_command_prepare", "tuoni_operation_execute", "tuoni_command_get"):
            workflow = workflow.replace(name, prefix + name)
        dispatch = ""
    else:
        names = [prefix + name for name in ("tuoni_find_tool", "tuoni_tool_schema", "tuoni_call")]
        dispatch = (
            "Dispatch entrypoints: " + ", ".join(names) + ". "
            "Discover the needed MCP operation first. Inner name values are unprefixed.\n"
        )
    return "Tuoni controls agents on target machines.\n" + dispatch + workflow + _COMMON_INSTRUCTIONS


def _safe_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "configured Tuoni server"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}" if parsed.scheme else host


class RegistryFastMCP(FastMCP):
    """FastMCP transports backed by a startup-static first-party ToolRegistry."""

    def __init__(
        self,
        name: str,
        *,
        registry: ToolRegistry,
        exposure: tuple[ToolSpec, ...],
        **kwargs: Any,
    ) -> None:
        # FastMCP registers these overridden public methods with its protocol server
        # during super().__init__, so keep the immutable exposure ready first.
        self.registry = registry
        self.exposure = exposure
        self._exposure_by_name = {spec.name: spec for spec in exposure}
        # The exposure is frozen at startup and every wire Tool derived from it is
        # therefore immutable. Building them costs a full Pydantic schema
        # generation per tool, so do it once instead of on every tools/list.
        self._wire_tools: list[Tool] | None = None
        super().__init__(name, **kwargs)

    async def list_tools(self) -> list[Tool]:
        if self._wire_tools is None:
            self._wire_tools = self.registry.wire_tools(self.exposure)
        return [tool.model_copy(deep=True) for tool in self._wire_tools]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        spec = self._exposure_by_name.get(name)
        if spec is None:
            return error_result(
                "TOOL_NOT_FOUND",
                f"No enabled tool named {name}.",
                hint="List tools again and use one exact enabled name.",
            )
        context = self.get_context()
        invocation = InvocationContext(
            context=context,
            server=context.request_context.lifespan_context,
        )
        return await self.registry.invoke_spec(spec, arguments, invocation)

    @property
    def protocol_server(self):
        """Project-owned test/integration hook for MCP in-memory transports."""
        return self._mcp_server


def _make_lifespan(cfg: Settings):
    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[ServerContext]:
        c2 = TuoniC2(
            verify=cfg.verify, request_timeout=cfg.request_timeout,
            response_max_bytes=cfg.response_max_bytes, download_max_bytes=cfg.download_max_bytes,
        )
        ctx = ServerContext(c2=c2, cfg=cfg)

        # Login is best-effort during startup. If the Tuoni server is unreachable,
        # the MCP handshake should still complete so Claude can see the tool list and
        # show a meaningful error per tool call. call_with_relogin retries automatically
        # whenever a tool runs while the token is missing or expired.
        async def initial_login() -> None:
            async with ctx.login_lock:
                if ctx.last_login is not None:
                    return
                try:
                    await login(ctx)
                except Exception as exc:
                    print(
                        f"[tuoni-mcp] initial login to {_safe_endpoint(cfg.url)} failed "
                        f"({type(exc).__name__}). Tools will retry on first use.",
                        file=sys.stderr,
                    )

        # Upstream availability must not delay MCP initialization or tools/list.
        startup_tasks = [asyncio.create_task(initial_login())]

        if cfg.events_enabled:
            bridge = EventBridge(ctx)
            # Retain the bridge even when its first connection fails: start()
            # installs a watchdog first, and later polls can observe recovery.
            ctx.event_bridge = bridge
            async def initial_events() -> None:
                try:
                    await bridge.start()
                except Exception as exc:
                    print(
                        f"[tuoni-mcp] WebSocket event bridge startup failed ({type(exc).__name__}); "
                        "the watchdog will retry.",
                        file=sys.stderr,
                    )
                if not bridge.status.connected:
                    print(
                        "[tuoni-mcp] WebSocket event bridge is disconnected; the watchdog will retry.",
                        file=sys.stderr,
                    )

            bridge.startup_task = asyncio.create_task(initial_events())
            startup_tasks.append(bridge.startup_task)

        try:
            yield ctx
        finally:
            for task in startup_tasks:
                task.cancel()
            try:
                # MCP task groups cancel their lifespan during disconnect. Allow
                # cleanup checkpoints to run even inside that cancelled scope.
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*startup_tasks, return_exceptions=True)
                    if ctx.event_bridge is not None:
                        await ctx.event_bridge.stop()
            finally:
                close = getattr(c2, "close", None)
                if callable(close):
                    close()

    return lifespan


def build_server(
    cfg: Settings | None = None,
    *,
    registry: ToolRegistry | None = None,
) -> RegistryFastMCP:
    if cfg is None:
        cfg = Settings.from_env()
    registry = registry or build_registry(cfg)
    target_specs = registry.select(cfg.profiles, read_only=cfg.read_only)
    exposure = (
        target_specs
        if cfg.mode is MCPMode.NATIVE
        else build_dispatch_specs(registry, target_specs, read_only=cfg.read_only)
    )
    return RegistryFastMCP(
        "tuoni",
        registry=registry,
        exposure=exposure,
        instructions=instructions_for(cfg),
        lifespan=_make_lifespan(cfg),
    )


async def _doctor(cfg: Settings, *, offline: bool) -> dict[str, Any]:
    registry = build_registry(cfg)
    c2 = TuoniC2(
        verify=cfg.verify, request_timeout=cfg.request_timeout,
        response_max_bytes=cfg.response_max_bytes, download_max_bytes=cfg.download_max_bytes,
    )
    try:
        return await collect_diagnostics(
            ServerContext(c2=c2, cfg=cfg), check_connection=not offline,
            catalog=catalog_summary(registry, cfg),
        )
    finally:
        c2.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tuoni-mcp", description="Tuoni MCP server")
    parser.add_argument("command", nargs="?", choices=("doctor",), help="Check configuration and backend access.")
    parser.add_argument("--offline", action="store_true", help="doctor: check configuration without network access.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport. Default: stdio (for Claude Desktop / Code).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP transports.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port for HTTP transports.")
    args = parser.parse_args()

    if args.offline and args.command != "doctor":
        parser.error("--offline requires the doctor command")

    if args.command is None and args.transport != "stdio":
        try:
            local = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            local = False
        if not local:
            parser.error("HTTP transports are local-only: use 127.0.0.1, ::1 or localhost; use stdio for remote clients")
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")

    try:
        cfg = Settings.from_env()
    except (RuntimeError, ValueError) as exc:
        print(f"[tuoni-mcp] {exc}", file=sys.stderr)
        sys.exit(2)

    if args.command == "doctor":
        report = asyncio.run(_doctor(cfg, offline=args.offline))
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        sys.exit(0 if report["ok"] else 1)

    mcp = build_server(cfg)

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
