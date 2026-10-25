from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field, TypeAdapter, ValidationError
from tuoni import TuoniListener

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.protocol import exposed_call, redact, redact_text
from tuoni_mcp.registry import ToolFailure, ToolSuccess
from tuoni_mcp.tools._helpers import call, decode_conf_files, get_ctx
from tuoni_mcp.tools._json_detail import DetailCursor, DetailWindow, json_detail, redact_schema


PluginId = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r"\S")]
_PLUGIN_ID = TypeAdapter(PluginId)
ListenerId = Annotated[int, Field(strict=True, ge=1)]


def _plugin_summary(plugin) -> dict[str, Any]:
    try:
        plugin_id = _PLUGIN_ID.validate_python(plugin.plugin_id)
    except ValidationError as exc:
        raise ToolFailure("INVALID_UPSTREAM_ID", "A listener plugin has an invalid identifier.",
                          hint="Check the backend plugin metadata before retrying.") from exc
    if redact_text(plugin_id) != plugin_id:
        raise ToolFailure("INVALID_UPSTREAM_ID", "A listener plugin identifier contains secret-like text.",
                          hint="Check the backend plugin metadata before retrying.")
    return {
        "plugin_id": plugin_id,
        "name": bounded_text(plugin.name, 128),
        "vendor": bounded_text(plugin.vendor, 96),
        "description": bounded_text(plugin.description, 256),
        "configuration_example_count": len(plugin.conf_examples),
        "detail": exposed_call({"name": "tuoni_listener_plugin_get", "arguments": {"plugin_id": plugin_id}}),
    }


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_listeners_list",
        description="List all listeners (communication channels for agent callbacks).",
    )
    async def listeners_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        listeners = await call(ctx, sctx.c2.load_listeners)
        return {"listeners": [serialize.listener_to_dict(lst) for lst in listeners]}

    @mcp.tool(
        name="tuoni_listener_get",
        description="Fetch a single listener by id.",
    )
    async def listener_get(listener_id: ListenerId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        data = await call(ctx, sctx.c2.request_get, f"/api/v1/listeners/{listener_id}")
        if data is None:
            return {"error": "Listener not found", "listener_id": listener_id}
        return serialize.listener_to_dict(TuoniListener(data, sctx.c2))

    @mcp.tool(
        name="tuoni_listener_plugins_list",
        description="List compact listener-plugin summaries; follow detail for the configuration schema and examples.",
        preserve_list_fields=("plugin_id", "detail"),
    )
    async def listener_plugins_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_listener_plugins)
        return {"plugins": [_plugin_summary(p) for p in plugins.values()]}

    @mcp.tool(
        name="tuoni_listener_plugin_get",
        description="Read one listener-plugin configuration schema/examples. Large details use value_json windows: follow next, concatenate, then parse JSON. DETAIL_CHANGED requires restarting.",
    )
    async def listener_plugin_get(
        plugin_id: PluginId,
        cursor: DetailCursor = None,
        max_chars: DetailWindow = 4096,
        ctx: Context = None,
    ) -> ToolSuccess:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_listener_plugins)
        plugin = plugins.get(plugin_id)
        if plugin is None:
            raise ToolFailure("NOT_FOUND", "No listener plugin exists with this ID.",
                              hint="List listener plugins and copy an exact plugin_id.")
        safe = redact(serialize.listener_plugin_to_dict(plugin))
        safe["configuration_schema"] = redact_schema(plugin.conf_schema)
        safe["configuration_schema_redacted"] = safe["configuration_schema"] != plugin.conf_schema
        if safe["configuration_schema_redacted"]:
            safe["configuration_schema_notice"] = (
                "Secret literals were removed; this schema may omit constraints. "
                "Do not treat it as complete validation."
            )
        return json_detail(
            safe, tool_name="tuoni_listener_plugin_get", identity={"plugin_id": plugin_id},
            cursor=cursor, max_chars=max_chars, already_redacted=True,
        )

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_listener_create",
        description=(
            "Create a new listener. Describe a tuoni_listener_plugins_list ID with tuoni_listener_plugin_get first. "
            "`configuration` matches the plugin's configuration_schema. `files` is optional "
            "multipart attachments [{name, filename, content_base64}]."
        ),
    )
    async def listener_create(
        plugin_id: str,
        configuration: dict[str, Any],
        name: str | None = None,
        files: list[dict] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_listener_plugins)
        plugin = plugins.get(plugin_id)
        if plugin is None:
            return {"error": "Unknown listener plugin_id", "plugin_id": plugin_id}
        listener = await call(ctx, plugin.create, configuration, name, decode_conf_files(files))
        return serialize.listener_to_dict(listener)

    @mcp.tool(
        name="tuoni_listener_update",
        description="Update an existing listener's name and/or configuration.",
    )
    async def listener_update(
        listener_id: ListenerId,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        data = await call(ctx, sctx.c2.request_get, f"/api/v1/listeners/{listener_id}")
        if data is None:
            return {"error": "Listener not found", "listener_id": listener_id}
        listener = TuoniListener(data, sctx.c2)
        if name is not None:
            listener.name = name
        if configuration is not None:
            listener.configuration = configuration
        await call(ctx, listener.update)
        return serialize.listener_to_dict(listener)

    @mcp.tool(
        name="tuoni_listener_start",
        description="Start a stopped listener.",
    )
    async def listener_start(listener_id: ListenerId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        data = await call(ctx, sctx.c2.request_put, f"/api/v1/listeners/{listener_id}/start")
        return serialize.listener_to_dict(TuoniListener(data, sctx.c2)) if data else {"listener_id": listener_id}

    @mcp.tool(
        name="tuoni_listener_stop",
        description="Stop a running listener.",
    )
    async def listener_stop(listener_id: ListenerId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        data = await call(ctx, sctx.c2.request_put, f"/api/v1/listeners/{listener_id}/stop")
        return serialize.listener_to_dict(TuoniListener(data, sctx.c2)) if data else {"listener_id": listener_id}

    @mcp.tool(
        name="tuoni_listener_delete",
        description="Delete a listener.",
    )
    async def listener_delete(listener_id: ListenerId, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(ctx, sctx.c2.request_delete, f"/api/v1/listeners/{listener_id}")
        return {"listener_id": listener_id, "deleted": True}
