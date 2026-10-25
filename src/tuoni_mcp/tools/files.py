from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import unquote

from mcp.server.fastmcp import Context, FastMCP
from pydantic import AfterValidator, Field

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.connection import to_thread
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, would_fit_success
from tuoni_mcp.staging import load_upload
from tuoni_mcp.tools._helpers import (
    authenticated_download_reference,
    call,
    get_ctx,
)


FileIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Exact hosted-file ID from the hosted-file list.",
    ),
]


def _valid_hosted_path(path: str) -> str:
    decoded = path
    for _ in range(10):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    else:
        raise ValueError("hosted path has excessive percent encoding")
    if any(ord(character) < 32 for character in decoded):
        raise ValueError("hosted path must not contain control characters")
    components = decoded.replace("\\", "/").split("/")
    if any(component in {".", ".."} for component in components):
        raise ValueError("hosted path must not contain dot traversal components")
    return path


HostedPathArg = Annotated[
    str,
    Field(min_length=1, max_length=2048, description="Exact hosted path."),
    AfterValidator(_valid_hosted_path),
]


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_files_list",
        description="List hosted files (uploaded artifacts the C2 can serve to agents/listeners).",
    )
    async def files_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        files = await call(ctx, sctx.c2.load_hosted)
        return {"files": [serialize.file_to_dict(f) for f in files]}

    @mcp.tool(
        name="tuoni_file_get_by_path",
        description="Fetch a hosted file's metadata by its hosted path (e.g. '/payloads/agent.exe').",
    )
    async def file_get_by_path(path: HostedPathArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        f = await call(ctx, sctx.c2.load_hosted_by_path, path)
        if f is None:
            return {"error": "File not found", "path": path}
        return serialize.file_to_dict(f)

    @mcp.tool(
        name="tuoni_file_download",
        description=(
            "Return an HTTP reference for an already-hosted C2 file; do not use for an agent "
            "command-result file. This tool never fetches or embeds the body."
        ),
    )
    async def file_download(file_id: FileIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        encoded_file_id = url_path_segment(file_id)
        meta = await call(ctx, sctx.c2.request_get, f"/api/v1/file/{encoded_file_id}")
        if meta is None:
            return {"error": "File not found", "file_id": file_id}
        paths = meta.get("filePaths")
        if not isinstance(paths, list):
            paths = []
        href = meta.get("downloadHref") or f"/api/v1/file/{encoded_file_id}/download"
        result = {
            "file_id": bounded_text(meta.get("fileId") or file_id, 128),
            "original_filename": bounded_text(meta.get("originalFileName"), 192),
            "size": meta.get("size") if isinstance(meta.get("size"), int) else None,
            "file_paths": [],
            "file_paths_remaining": len(paths),
            "download": authenticated_download_reference(sctx.cfg.url, str(href)),
        }
        for index, path in enumerate(paths[:12]):
            candidate_paths = [*result["file_paths"], bounded_text(path, 192)]
            candidate = {
                **result,
                "file_paths": candidate_paths,
                "file_paths_remaining": len(paths) - index - 1,
            }
            if not would_fit_success(candidate, budget=DETAIL_RESULT_BUDGET):
                break
            result = candidate
        return result

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_file_upload",
        description=(
            "Upload a file to the C2's hosted-files store. `hosted_path` is where the C2 will host it "
            "(e.g. '/artifacts/report.txt'). Provide exactly one of content_base64 or local_path under the configured "
            "MCP-server staging directory; original_filename defaults to the source basename."
        ),
    )
    async def file_upload(
        hosted_path: HostedPathArg,
        content_base64: str | None = None,
        original_filename: str | None = None,
        local_path: Annotated[str | None, Field(
            min_length=1, max_length=4096,
            description="File under TUONI_MCP_UPLOAD_DIR on the MCP server; relative paths start there.",
        )] = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        source = await to_thread(
            load_upload, sctx.cfg, "tuoni_file_upload", content_base64, local_path,
            _timeout=sctx.cfg.request_timeout,
        )
        filename = original_filename or source.filename
        result = await call(ctx, sctx.c2.add_hosted, hosted_path, source.data, filename)
        return {"hosted_path": hosted_path, "size": len(source.data), "sha256": source.sha256, "result": result}

    @mcp.tool(
        name="tuoni_file_replace",
        description="Replace a hosted file using exactly one of content_base64 or a staged local_path on the MCP server.",
    )
    async def file_replace(
        file_id: FileIdArg,
        content_base64: str | None = None,
        original_filename: str | None = None,
        local_path: Annotated[str | None, Field(
            min_length=1, max_length=4096,
            description="File under TUONI_MCP_UPLOAD_DIR on the MCP server; relative paths start there.",
        )] = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        source = await to_thread(
            load_upload, sctx.cfg, "tuoni_file_replace", content_base64, local_path,
            _timeout=sctx.cfg.request_timeout,
        )
        encoded_file_id = url_path_segment(file_id)
        meta = await call(ctx, sctx.c2.request_get, f"/api/v1/file/{encoded_file_id}")
        if meta is None:
            return {"error": "File not found", "file_id": file_id}
        filename = original_filename or meta.get("originalFileName")
        await call(
            ctx,
            sctx.c2.request_put_multipart,
            f"/api/v1/file/{encoded_file_id}",
            {"content": [filename, source.data]},
        )
        return {"file_id": file_id, "size": len(source.data), "sha256": source.sha256, "replaced": True}

    @mcp.tool(
        name="tuoni_file_delete",
        description="Delete a hosted file by its file_id (GUID).",
    )
    async def file_delete(file_id: FileIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(
            ctx,
            sctx.c2.request_delete,
            f"/api/v1/file/{url_path_segment(file_id)}",
        )
        return {"file_id": file_id, "deleted": True}

    @mcp.tool(
        name="tuoni_file_delete_by_path",
        description="Delete a hosted file by its hosted path (e.g. '/payloads/agent.exe').",
    )
    async def file_delete_by_path(path: HostedPathArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.delete_hosted_by_path, path)
        return {"path": path, "deleted": True}
