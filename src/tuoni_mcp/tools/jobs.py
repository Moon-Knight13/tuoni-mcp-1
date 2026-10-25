from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from tuoni import TuoniJob

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.pagination import bounded_text, redact
from tuoni_mcp.protocol import exposed_call
from tuoni_mcp.registry import ToolFailure, ToolSuccess
from tuoni_mcp.tools._helpers import DEFAULT_PAGE_SIZE, PageArg, PageSizeArg, call, get_ctx
from tuoni_mcp.tools._json_detail import DetailCursor, DetailWindow, json_detail


JobId = Annotated[int, Field(strict=True, ge=1)]


def _summary(job: TuoniJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "name": bounded_text(job.name, 128),
        "status": bounded_text(job.status, 48),
        "source": redact(job.source, max_depth=2, max_items=4, max_string_bytes=96),
        "supported_actions": redact(job.supportedActions, max_depth=1, max_items=8, max_string_bytes=32),
        "open_resource_count": len(job.openResources),
        "message_count": len(job.messages),
        "detail": exposed_call({"name": "tuoni_job_get", "arguments": {"job_id": job.job_id}}),
    }


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_jobs_list",
        description="List compact background-job summaries; follow detail for messages/resources. include_inactive includes finished/stopped jobs.",
    )
    async def jobs_list(
        include_inactive: bool = False,
        page: PageArg = 0,
        page_size: PageSizeArg = DEFAULT_PAGE_SIZE,
        sort_col: str = "id",
        sort_order: str = "asc",
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        jobs = await call(
            ctx, sctx.c2.load_jobs, page, page_size, sort_col, sort_order, include_inactive
        )
        if not include_inactive:
            # /jobs/active returns the entire sorted collection; the SDK ignores
            # page inputs for this endpoint. Page it locally so continuation
            # cannot repeatedly return the same full page. /jobs/all already
            # applies pagination upstream when inactive jobs are included.
            start = page * page_size
            jobs = jobs[start:start + page_size]
        return {"jobs": [_summary(j) for j in jobs]}

    @mcp.tool(
        name="tuoni_job_get",
        description="Read one job including messages/resources. Large details use value_json windows: follow next, concatenate, then parse JSON. DETAIL_CHANGED requires restarting.",
    )
    async def job_get(
        job_id: JobId,
        cursor: DetailCursor = None,
        max_chars: DetailWindow = 4096,
        ctx: Context = None,
    ) -> ToolSuccess:
        sctx = get_ctx(ctx)
        data = await call(ctx, sctx.c2.request_get, f"/api/v1/job/{job_id}")
        if data is None:
            raise ToolFailure("NOT_FOUND", "No job exists with this ID.", hint="List jobs and copy an exact job_id.")
        return json_detail(
            serialize.job_to_dict(TuoniJob(data, sctx.c2)), tool_name="tuoni_job_get",
            identity={"job_id": job_id}, cursor=cursor, max_chars=max_chars,
        )

    if cfg.read_only:
        return

    @mcp.tool(name="tuoni_job_restart", description="Restart a persistent job.")
    async def job_restart(job_id: JobId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.request_post, f"/api/v1/job/{job_id}/restart")
        return {"job_id": job_id, "action": "restart"}

    @mcp.tool(name="tuoni_job_pause", description="Pause a running job.")
    async def job_pause(job_id: JobId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.request_post, f"/api/v1/job/{job_id}/pause")
        return {"job_id": job_id, "action": "pause"}

    @mcp.tool(name="tuoni_job_resume", description="Resume a paused job.")
    async def job_resume(job_id: JobId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.request_post, f"/api/v1/job/{job_id}/resume")
        return {"job_id": job_id, "action": "resume"}
