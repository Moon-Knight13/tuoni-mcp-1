"""Live OpenAPI spec introspection from the connected Tuoni server."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from tuoni_mcp.config import ALL_PROFILES, Settings
from tuoni_mcp.connection import call_with_relogin
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET
from tuoni_mcp.registry import Effect, InvocationContext, ToolInput, ToolRegistry, ToolSpec, ToolSuccess


_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch"})

# The OpenAPI spec is stable for the lifetime of a Tuoni server version.
# Limit the full-schema detail dump so it fits the result budget.
_DETAIL_CHAR_LIMIT = DETAIL_RESULT_BUDGET - 256


class ApiSpecInput(ToolInput):
    filter: Annotated[
        str,
        Field(
            default="",
            max_length=256,
            description=(
                "Case-insensitive search text. "
                "Empty returns a compact list of every endpoint. "
                "An exact path (e.g. '/api/v1/agents') returns that endpoint's full schema. "
                "Otherwise lists endpoints whose path, tag, or summary contain the text."
            ),
        ),
    ] = ""


def register_specs(registry: ToolRegistry, _cfg: Settings) -> None:
    async def api_spec(invocation: InvocationContext, arguments: ApiSpecInput) -> ToolSuccess:
        """Fetch and filter the live OpenAPI spec from the connected server."""
        sctx = invocation.server
        data = await call_with_relogin(sctx, sctx.c2.request_get, "/docs/api")
        if data is None:
            return ToolSuccess("Server returned no data for /docs/api.")

        paths: dict = data.get("paths", {})
        version = data.get("info", {}).get("version", "?")
        query = arguments.filter.strip().lower()

        # Exact single-path match: return full schema for that endpoint.
        if query:
            path_lower_map = {p.lower(): p for p in paths}
            if query in path_lower_map:
                real_path = path_lower_map[query]
                detail = json.dumps({real_path: paths[real_path]}, indent=2)
                return ToolSuccess(bounded_text(
                    f"Tuoni v{version} — {real_path}:\n{detail}",
                    _DETAIL_CHAR_LIMIT,
                ))

        # List mode: compact endpoint index, optionally filtered.
        lines: list[str] = []
        for path in sorted(paths):
            for method, operation in paths[path].items():
                if method not in _HTTP_METHODS:
                    continue
                tag = (operation.get("tags") or [""])[0]
                summary = operation.get("summary", "")
                if query and not any(
                    query in text.lower()
                    for text in (path, tag, summary)
                ):
                    continue
                lines.append(f"{method.upper():6} {path}  — {summary}")

        header = f"Tuoni v{version} — {len(lines)} endpoint(s)"
        if query:
            header += f" matching '{arguments.filter.strip()}'"
        else:
            header += f" across {len(paths)} paths"

        if not lines:
            return ToolSuccess(f"{header}. No match. Call with empty filter to list all.")

        return ToolSuccess(bounded_text(
            header + ":\n" + "\n".join(lines),
            LIST_RESULT_BUDGET - 128,
        ))

    registry.add(ToolSpec(
        name="tuoni_api_spec",
        description=(
            "Fetch the live OpenAPI specification from the connected Tuoni server. "
            "Always reflects the exact version you are connected to. "
            "Empty filter returns a compact endpoint index; a path returns its full schema; "
            "text narrows by path, tag, or summary."
        ),
        input_model=ApiSpecInput,
        handler=api_spec,
        profiles=ALL_PROFILES,
        budget=DETAIL_RESULT_BUDGET,
        effect=Effect.READ,
        keywords=(
            "openapi", "swagger", "spec", "specification", "api", "endpoints",
            "schema", "documentation", "reference", "introspection",
        ),
    ))
