from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.protocol import exposed_call
from tuoni_mcp.registry import ToolFailure, ToolSuccess
from tuoni_mcp.tools._helpers import authenticated_download_reference, call, get_ctx
from tuoni_mcp.tools._json_detail import DetailCursor, DetailWindow, exact_catalog_id, schema_detail


PayloadIdArg = Annotated[
    int,
    Field(strict=True, ge=1, description="Exact payload ID returned by tuoni_payloads_list or creation."),
]
CatalogIdArg = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r"\S")]
_compatibility_prechecked: ContextVar[tuple[int, str, int] | None] = ContextVar(
    "payload_compatibility_prechecked", default=None,
)


@contextmanager
def payload_compatibility_prechecked(ctx: Context, template: str, listener_id: int) -> Iterator[None]:
    """Reuse a preflight only inside the matching execution's mutation boundary."""
    token = _compatibility_prechecked.set((id(get_ctx(ctx).c2), template, listener_id))
    try:
        yield
    finally:
        _compatibility_prechecked.reset(token)


async def check_payload_listener_compatibility(
    ctx: Context, template: str, listener_id: int,
) -> dict[str, str]:
    """Reject mutually confirmed incompatibility; defer missing metadata to Tuoni.

    Compatibility is live metadata, not a transaction or a replacement for the
    backend's build validation. Empty, absent, or inconsistent lists cannot prove
    that an otherwise supported plugin/listener combination is unavailable.
    """
    sctx = get_ctx(ctx)
    unknown = {"status": "unknown", "reason": "compatibility_metadata_unavailable"}
    try:
        raw = await call(ctx, sctx.c2.request_get, f"/api/v1/payloads/templates/{url_path_segment(template)}")
    except Exception:
        return unknown
    if not isinstance(raw, Mapping):
        return unknown
    reported_identities = {
        value.casefold() for value in (raw.get("id"), raw.get("name"))
        if isinstance(value, str) and value
    }
    if template.casefold() not in reported_identities:
        return unknown
    allowed = raw.get("availableListenerIds")
    if not isinstance(allowed, list) or not allowed or any(type(item) is not int or item < 1 for item in allowed):
        return unknown
    if listener_id in allowed:
        return {"status": "compatible"}
    try:
        listener = await call(ctx, sctx.c2.request_get, f"/api/v1/listeners/{listener_id}")
    except Exception:
        return unknown
    if not isinstance(listener, Mapping) or type(listener.get("id")) is not int or listener["id"] != listener_id:
        return unknown
    supported = listener.get("supportedPayloadTemplateIds")
    if not isinstance(supported, list) or not supported or any(
        not isinstance(item, str) or not item for item in supported
    ):
        return unknown
    if any(value.casefold() in reported_identities for value in supported):
        return {"status": "unknown", "reason": "compatibility_metadata_disagrees"}
    raise ToolFailure(
        "PAYLOAD_LISTENER_INCOMPATIBLE",
        "The selected payload template and listener both report that they are incompatible.",
        hint="Describe the payload template and choose one of its available_listener_ids before creating a payload.",
    )


def _plugin_summary(plugin: Any) -> dict[str, Any]:
    plugin_id = exact_catalog_id(plugin.plugin_id)
    return {
        "plugin_id": plugin_id,
        "name": bounded_text(plugin.name, 128),
        "vendor": bounded_text(plugin.vendor, 128),
        "description": bounded_text(plugin.description, 256),
        "template_count": len(plugin.templates),
        "detail": exposed_call({"name": "tuoni_payload_plugin_templates_list", "arguments": {"plugin_id": plugin_id}}),
    }


def _template_summary(template: Any) -> dict[str, Any]:
    template_id = exact_catalog_id(template.id)
    return {
        "id": template_id,
        "name": bounded_text(template.name, 128),
        "description": bounded_text(getattr(template, "description", None), 256),
        "detail": exposed_call({"name": "tuoni_payload_template_get", "arguments": {"template_id": template_id}}),
    }


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_payloads_list",
        description="List generated payloads. Set include_non_active=true to also see archived ones.",
    )
    async def payloads_list(include_non_active: bool = False, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        payloads = await call(ctx, sctx.c2.load_payloads, include_non_active)
        return {"payloads": [serialize.payload_to_dict(p) for p in payloads]}

    @mcp.tool(
        name="tuoni_payload_get",
        description="Fetch a single payload by id.",
    )
    async def payload_get(payload_id: PayloadIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        payload = await call(ctx, sctx.c2.load_payload, payload_id)
        if payload is None:
            return {"error": "Payload not found", "payload_id": payload_id}
        return serialize.payload_to_dict(payload)

    @mcp.tool(
        name="tuoni_payload_plugins_list",
        description=(
            "List compact payload-plugin summaries. Follow detail to list templates, "
            "then describe one before creating a payload."
        ),
        preserve_list_fields=("plugin_id", "detail"),
    )
    async def payload_plugins_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_payload_plugins)
        return {"plugins": [_plugin_summary(p) for p in plugins.values()]}

    @mcp.tool(
        name="tuoni_payload_plugin_templates_list",
        description="List compact templates for one payload plugin; follow detail for configuration fields and examples.",
        preserve_list_fields=("id", "detail"),
        preserve_response_fields=("plugin_id",),
    )
    async def payload_plugin_templates_list(plugin_id: CatalogIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_payload_plugins)
        plugin = plugins.get(plugin_id)
        if plugin is None:
            raise ToolFailure("PAYLOAD_PLUGIN_NOT_FOUND", "The selected payload plugin is not installed.",
                              hint="List payload plugins and copy one exact plugin_id.")
        return {"plugin_id": exact_catalog_id(plugin_id), "templates": [_template_summary(t) for t in plugin.templates]}

    @mcp.tool(
        name="tuoni_payload_template_get",
        description=(
            "Read one payload template's configuration schema/examples. "
            "Large details use value_json windows: follow next, concatenate, then parse JSON. "
            "On DETAIL_CHANGED, discard windows and restart without cursor."
        ),
    )
    async def payload_template_get(
        template_id: CatalogIdArg,
        cursor: DetailCursor = None,
        max_chars: DetailWindow = 4096,
        ctx: Context = None,
    ) -> ToolSuccess:
        sctx = get_ctx(ctx)
        raw = await call(ctx, sctx.c2.request_get, f"/api/v1/payloads/templates/{url_path_segment(template_id)}")
        if raw is None:
            raise ToolFailure("NOT_FOUND", "No payload template has this ID.",
                              hint="List the plugin's templates and copy one exact id.")
        if not isinstance(raw, dict) or not isinstance(raw.get("configurationSchema"), (dict, bool)):
            raise ToolFailure("INVALID_RESPONSE", "The backend returned invalid payload-template metadata.",
                              hint="Check the backend template metadata before retrying.")
        return schema_detail({
            "id": exact_catalog_id(raw.get("name") or raw.get("id")),
            "name": raw.get("name"), "plugin_id": raw.get("pluginId"),
            "description": raw.get("description"), "status": raw.get("status"),
            "payload_type": raw.get("payloadType"),
            "configuration_schema": raw["configurationSchema"],
            "configuration_examples": raw.get("exampleConfigurations", []),
            "available_listener_ids": raw.get("availableListenerIds", []),
        }, tool_name="tuoni_payload_template_get", identity={"template_id": template_id},
            cursor=cursor, max_chars=max_chars)

    @mcp.tool(
        name="tuoni_payload_download",
        description=(
            "Return bounded payload metadata and an authenticated HTTP download reference. "
            "This tool never builds, fetches, or embeds the payload binary in model context."
        ),
    )
    async def payload_download(payload_id: PayloadIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        payload = await call(ctx, sctx.c2.load_payload, payload_id)
        if payload is None:
            return {"error": "Payload not found", "payload_id": payload_id}
        href = f"/api/v1/payloads/{payload_id}/download"
        return {
            "payload_id": payload_id,
            "name": bounded_text(getattr(payload, "name", None), 256),
            "status": bounded_text(getattr(payload, "status", None), 64),
            "os": bounded_text(getattr(payload, "os", None), 64),
            "architecture": bounded_text(getattr(payload, "architecture", None), 64),
            "download": authenticated_download_reference(sctx.cfg.url, href),
        }

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_payload_create",
        description=(
            "Create a new payload. `template` is a payload template id (e.g. from "
            "tuoni_payload_plugin_templates_list). Describe it with tuoni_payload_template_get first. "
            "`listener_id` ties the payload to a callback listener. `configuration` matches the "
            "template's configuration_schema. Returns the new payload id."
        ),
    )
    async def payload_create(
        template: str,
        listener_id: Annotated[int, Field(strict=True, ge=1)],
        configuration: dict[str, Any],
        encrypted: bool = True,
        name: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        if _compatibility_prechecked.get() != (id(sctx.c2), template, listener_id):
            await check_payload_listener_compatibility(ctx, template, listener_id)
        payload_id = await call(
            ctx, sctx.c2.create_payload, template, listener_id, configuration, encrypted, name
        )
        payload = await call(ctx, sctx.c2.load_payload, payload_id)
        if payload is None:
            return {"payload_id": payload_id, "note": "created but could not be reloaded"}
        return serialize.payload_to_dict(payload)

    @mcp.tool(
        name="tuoni_payload_rename",
        description="Rename a payload.",
    )
    async def payload_rename(payload_id: PayloadIdArg, new_name: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.rename_payload, payload_id, new_name)
        return {"payload_id": payload_id, "name": new_name}

    @mcp.tool(
        name="tuoni_payload_delete",
        description="Delete a payload.",
    )
    async def payload_delete(payload_id: PayloadIdArg, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.request_delete, f"/api/v1/payloads/{payload_id}")
        return {"payload_id": payload_id, "deleted": True}
