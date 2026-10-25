from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.registry import ToolFailure
from tuoni_mcp.tools._helpers import DEFAULT_PAGE_SIZE, PageArg, PageSizeArg, call, get_ctx


SelectorArg = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=256, pattern=r"\S",
          description="Exact match; use * at either end for a partial match."),
]
SearchArg = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=256, pattern=r"\S",
          description="Substring to find across the stored record's searchable fields."),
]
ArchivedArg = Annotated[
    bool,
    Field(strict=True, description="Include archived records; omitted means active records only."),
]
PortArg = Annotated[int, Field(strict=True, ge=0, le=65535, description="Exact service port.")]
BackendFilterArg = Annotated[
    str,
    Field(description="URL-encoded backend query; cannot be combined with typed filters."),
]


def _query_filter(backend_filter: str | None, **selectors: str | int | bool | None) -> str | None:
    selected = {name: value for name, value in selectors.items() if value is not None}
    if backend_filter is not None:
        if selected:
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "Use either typed discovery filters or the backend filter query, not both.",
                hint="Remove filter to use typed fields, or remove the typed fields.",
            )
        return backend_filter
    if not selected:
        return None
    return urlencode({
        name: str(value).lower() if isinstance(value, bool) else value
        for name, value in selected.items()
    })


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_hosts_list",
        description="List Tuoni's stored host records with backend filters; this does not scan hosts.",
    )
    async def hosts_list(
        page: PageArg = 0,
        page_size: PageSizeArg = DEFAULT_PAGE_SIZE,
        filter: BackendFilterArg | None = None,
        ctx: Context = None,
        *,
        address: SelectorArg | None = None,
        name: SelectorArg | None = None,
        search: SearchArg | None = None,
        include_archived: ArchivedArg | None = None,
    ) -> dict[str, Any]:
        query = _query_filter(
            filter, address=address, name=name, anyFieldContains=search,
            includeArchived=include_archived,
        )
        sctx = get_ctx(ctx)
        hosts = await call(ctx, sctx.c2.load_datamodel_hosts, page, page_size, query)
        return {"hosts": [serialize.host_to_dict(h) for h in hosts]}

    @mcp.tool(
        name="tuoni_services_list",
        description="List Tuoni's stored service records with backend filters; this does not probe services.",
    )
    async def services_list(
        page: PageArg = 0,
        page_size: PageSizeArg = DEFAULT_PAGE_SIZE,
        filter: BackendFilterArg | None = None,
        ctx: Context = None,
        *,
        address: SelectorArg | None = None,
        port: PortArg | None = None,
        protocol: SelectorArg | None = None,
        search: SearchArg | None = None,
        include_archived: ArchivedArg | None = None,
    ) -> dict[str, Any]:
        query = _query_filter(
            filter, address=address, port=port, protocol=protocol, anyFieldContains=search,
            includeArchived=include_archived,
        )
        sctx = get_ctx(ctx)
        services = await call(ctx, sctx.c2.load_datamodel_services, page, page_size, query)
        return {"services": [serialize.service_to_dict(s) for s in services]}

    @mcp.tool(
        name="tuoni_credentials_list",
        description="List Tuoni's stored credential records with backend filters and redacted secrets.",
    )
    async def credentials_list(
        page: PageArg = 0,
        page_size: PageSizeArg = DEFAULT_PAGE_SIZE,
        filter: BackendFilterArg | None = None,
        ctx: Context = None,
        *,
        username: SelectorArg | None = None,
        host: SelectorArg | None = None,
        realm: SelectorArg | None = None,
        source: SelectorArg | None = None,
        search: SearchArg | None = None,
        include_archived: ArchivedArg | None = None,
    ) -> dict[str, Any]:
        query = _query_filter(
            filter, username=username, host=host, realm=realm, source=source,
            anyFieldContains=search, includeArchived=include_archived,
        )
        sctx = get_ctx(ctx)
        creds = await call(ctx, sctx.c2.load_datamodel_credentials, page, page_size, query)
        return {"credentials": [serialize.credential_to_dict(c) for c in creds]}

    if cfg.read_only:
        return

    @mcp.tool(name="tuoni_host_add", description="Add a host entry to the discovery data model.")
    async def host_add(address: str, name: str = "", note: str = "", ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        h = await call(ctx, sctx.c2.add_datamodel_host, address, name, note)
        return serialize.host_to_dict(h)

    @mcp.tool(name="tuoni_host_update", description="Update name/note for an existing host.")
    async def host_update(
        host_id: str,
        name: str | None = None,
        note: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if note is not None:
            body["note"] = note
        await call(
            ctx,
            sctx.c2.request_patch,
            f"/api/v1/discovery/host/{url_path_segment(host_id)}",
            body,
        )
        return {"host_id": host_id, "updated": True}

    @mcp.tool(name="tuoni_host_archive", description="Archive a host entry.")
    async def host_archive(host_id: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(
            ctx,
            sctx.c2.request_post,
            "/api/v1/discovery/hosts/bulk-archive",
            {"hostIds": [host_id]},
        )
        return {"host_id": host_id, "archived": True}

    @mcp.tool(name="tuoni_service_add", description="Add a service entry to the discovery data model.")
    async def service_add(
        address: str,
        port: int,
        protocol: str = "",
        banner: str = "",
        note: str = "",
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        s = await call(ctx, sctx.c2.add_datamodel_service, address, port, protocol, banner, note)
        return serialize.service_to_dict(s)

    @mcp.tool(name="tuoni_service_update", description="Update protocol/banner/note for a service.")
    async def service_update(
        service_id: str,
        protocol: str | None = None,
        banner: str | None = None,
        note: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        body: dict[str, Any] = {}
        if protocol is not None:
            body["protocol"] = protocol
        if banner is not None:
            body["banner"] = banner
        if note is not None:
            body["note"] = note
        await call(
            ctx,
            sctx.c2.request_patch,
            f"/api/v1/discovery/service/{url_path_segment(service_id)}",
            body,
        )
        return {"service_id": service_id, "updated": True}

    @mcp.tool(name="tuoni_service_archive", description="Archive a service entry.")
    async def service_archive(service_id: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(
            ctx,
            sctx.c2.request_post,
            "/api/v1/discovery/services/bulk-archive",
            {"serviceIds": [service_id]},
        )
        return {"service_id": service_id, "archived": True}

    @mcp.tool(name="tuoni_credential_add", description="Add a credential entry to the discovery data model.")
    async def credential_add(
        username: str,
        password: str,
        host: str = "",
        realm: str = "",
        source: str = "",
        note: str = "",
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        c = await call(
            ctx, sctx.c2.add_datamodel_credential, username, password, host, realm, source, note
        )
        return serialize.credential_to_dict(c)

    @mcp.tool(name="tuoni_credential_update", description="Update source/note for a credential.")
    async def credential_update(
        credential_id: str,
        source: str | None = None,
        note: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        body: dict[str, Any] = {}
        if source is not None:
            body["source"] = source
        if note is not None:
            body["note"] = note
        await call(
            ctx,
            sctx.c2.request_patch,
            f"/api/v1/discovery/credential/{url_path_segment(credential_id)}",
            body,
        )
        return {"credential_id": credential_id, "updated": True}

    @mcp.tool(name="tuoni_credential_archive", description="Archive a credential entry.")
    async def credential_archive(credential_id: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(
            ctx,
            sctx.c2.request_post,
            "/api/v1/discovery/credentials/bulk-archive",
            {"credentialIds": [credential_id]},
        )
        return {"credential_id": credential_id, "archived": True}
