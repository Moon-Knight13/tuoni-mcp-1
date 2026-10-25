"""Small, offline-first diagnostic capability shared by every profile."""

from __future__ import annotations

from pydantic import StrictBool

from tuoni_mcp.config import ALL_PROFILES, Settings
from tuoni_mcp.diagnostics import DIAGNOSTICS_BUDGET, catalog_summary, collect_diagnostics
from tuoni_mcp.registry import InvocationContext, ToolInput, ToolRegistry, ToolSpec, ToolSuccess


class CapabilitiesInput(ToolInput):
    check_connection: StrictBool = False


def register_specs(registry: ToolRegistry, _cfg: Settings) -> None:
    async def capabilities(invocation: InvocationContext, arguments: CapabilitiesInput) -> ToolSuccess:
        return ToolSuccess(await collect_diagnostics(
            invocation.server,
            check_connection=arguments.check_connection,
            catalog=catalog_summary(registry, invocation.server.cfg),
        ))

    registry.add(ToolSpec(
        name="tuoni_capabilities",
        description="Show enabled profiles, limits, and missing-capability reasons. Optionally check backend version and identity.",
        input_model=CapabilitiesInput,
        handler=capabilities,
        profiles=ALL_PROFILES,
        budget=DIAGNOSTICS_BUDGET,
        keywords=(
            "diagnostics", "doctor", "health", "version", "identity", "permissions", "configuration",
            "server", "connection", "connectivity", "status",
        ),
    ))
