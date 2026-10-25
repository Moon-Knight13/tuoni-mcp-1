from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.pagination import bounded_text
from tuoni_mcp.protocol import exposed_call
from tuoni_mcp.registry import ToolFailure, ToolSuccess
from tuoni_mcp.tools._helpers import call, get_ctx
from tuoni_mcp.tools._json_detail import DetailCursor, DetailWindow, exact_catalog_id, schema_detail


PluginIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        strict=True,
        pattern=r"\S",
        description="Exact command-plugin ID returned by tuoni_command_plugins_list.",
    ),
]
TemplateIdArg = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r"\S")]


def _plugin_summary(plugin: Any) -> dict[str, Any]:
    commands = list(getattr(plugin, "commands", ()) or ())
    plugin_id = exact_catalog_id(getattr(plugin, "plugin_id", None), max_chars=256)
    return {
        "plugin_id": plugin_id,
        "name": bounded_text(getattr(plugin, "name", None), 128),
        "vendor": bounded_text(getattr(plugin, "vendor", None), 128),
        "description": bounded_text(getattr(plugin, "description", None), 256),
        "command_count": len(commands),
        "detail": exposed_call({"name": "tuoni_command_plugin_commands_list", "arguments": {"plugin_id": plugin_id}}),
    }


def _command_summary(command: Any, plugin_id: str) -> dict[str, Any]:
    template_id = exact_catalog_id(getattr(command, "id", None))
    return {
        "id": template_id,
        "name": bounded_text(getattr(command, "name", None), 128),
        "description": bounded_text(getattr(command, "description", None), 256),
        "detail": exposed_call({"name": "tuoni_command_plugin_command_get",
                                "arguments": {"plugin_id": plugin_id, "template_id": template_id}}),
    }


def register(mcp: FastMCP, _cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_command_plugins_list",
        description=(
            "List compact installed command-plugin summaries. Use "
            "tuoni_command_plugin_commands_list with one exact plugin_id to inspect its "
            "template summaries; follow their detail calls for configuration schemas."
        ),
        preserve_list_fields=("plugin_id", "detail"),
    )
    async def command_plugins_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_command_plugins)
        return {"plugins": [_plugin_summary(plugin) for plugin in plugins.values()]}

    @mcp.tool(
        name="tuoni_command_plugin_commands_list",
        description=(
            "List compact templates for one command plugin. Copy plugin_id exactly; "
            "follow detail for the configuration schema and examples."
        ),
        preserve_list_fields=("id", "detail"),
        preserve_response_fields=("plugin_id",),
    )
    async def command_plugin_commands_list(
        plugin_id: PluginIdArg,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        plugins = await call(ctx, sctx.c2.load_command_plugins)
        plugin = plugins.get(plugin_id)
        if plugin is None:
            raise ToolFailure(
                "COMMAND_PLUGIN_NOT_FOUND",
                "The selected command plugin is not installed.",
                hint="List command plugins again and copy one exact plugin_id.",
            )
        return {
            "plugin_id": exact_catalog_id(plugin_id, max_chars=256),
            "commands": [
                _command_summary(command, plugin_id)
                for command in (getattr(plugin, "commands", ()) or ())
            ],
        }

    @mcp.tool(
        name="tuoni_command_plugin_command_get",
        description=(
            "Read one installed command template's schema/examples without an agent. "
            "Large details use value_json windows: follow next, concatenate, then parse JSON. "
            "On DETAIL_CHANGED, discard windows and restart without cursor."
        ),
    )
    async def command_plugin_command_get(
        plugin_id: PluginIdArg,
        template_id: TemplateIdArg,
        cursor: DetailCursor = None,
        max_chars: DetailWindow = 4096,
        ctx: Context = None,
    ) -> ToolSuccess:
        sctx = get_ctx(ctx)
        loaded = await call(ctx, sctx.c2.load_command_plugins)
        plugin = loaded.get(plugin_id)
        if plugin is None:
            raise ToolFailure("COMMAND_PLUGIN_NOT_FOUND", "The selected command plugin is not installed.",
                              hint="List command plugins again and copy one exact plugin_id.")
        matches = [command for command in (getattr(plugin, "commands", ()) or ()) if command.id == template_id]
        if len(matches) != 1:
            raise ToolFailure("COMMAND_TEMPLATE_NOT_FOUND", "No unique command template has this ID in the plugin.",
                              hint="List the plugin's commands and copy one exact template id.")
        return schema_detail(
            serialize.command_template_to_dict(matches[0]), tool_name="tuoni_command_plugin_command_get",
            identity={"plugin_id": plugin_id, "template_id": template_id}, cursor=cursor, max_chars=max_chars,
        )
