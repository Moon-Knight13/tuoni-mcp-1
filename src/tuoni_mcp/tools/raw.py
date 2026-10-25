from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from tuoni_mcp.config import Settings
from tuoni_mcp.tools._helpers import call, decode_files, get_ctx

_READ_METHODS = {"GET"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_raw_request",
        description=(
            "Advanced escape hatch: send an arbitrary HTTP request to the Tuoni server. "
            "Prefer profile-scoped typed tools when they exist. Large responses fail the global "
            "8 KiB result ceiling; narrow the server query instead of requesting bulk data. "
            "method = GET|POST|PUT|PATCH|DELETE. uri starts with '/' (e.g. '/api/v1/agents'). "
            "When read_only mode is enabled, only GET is permitted."
        ),
    )
    async def raw_request(
        method: str,
        uri: str,
        json_body: dict[str, Any] | None = None,
        files: list[dict] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        m = method.upper()
        if cfg.read_only and m not in _READ_METHODS:
            return {"error": f"read-only mode: {m} requests are disabled"}
        if m not in _READ_METHODS and m not in _WRITE_METHODS:
            return {"error": f"unsupported HTTP method: {method}"}
        if not uri.startswith("/"):
            uri = "/" + uri

        decoded = decode_files(files)
        if m == "GET":
            data = await call(ctx, sctx.c2.request_get, uri)
        elif m == "POST":
            data = await call(ctx, sctx.c2.request_post, uri, json_body, decoded)
        elif m == "PUT":
            data = await call(ctx, sctx.c2.request_put, uri, json_body)
        elif m == "PATCH":
            data = await call(ctx, sctx.c2.request_patch, uri, json_body, decoded)
        else:  # DELETE
            data = await call(ctx, sctx.c2.request_delete, uri, json_body)
        # Do not slice a serialized response into a misleading preview. The shared
        # registry either returns this complete JSON value or emits a bounded
        # RESPONSE_TOO_LARGE tool error, prompting a narrower server-side query.
        return {"method": m, "uri": uri, "response": data}
