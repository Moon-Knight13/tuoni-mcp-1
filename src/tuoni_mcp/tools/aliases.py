from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.registry import ToolFailure
from tuoni_mcp.tools._helpers import (
    authenticated_download_reference,
    call,
    decode_files,
    get_ctx,
)


AliasIdArg = Annotated[
    str,
    Field(min_length=1, max_length=256, description="Exact command-alias ID."),
]
AliasFileIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Exact configuration-file ID from the alias metadata.",
    ),
]


def _existing_file_references(current: dict[str, Any]) -> list[dict[str, str]]:
    """Translate response-only @files metadata into existing-file references.

    A supplied fixedConfiguration replaces the whole multipart configuration.
    Never interpret malformed or missing file identities as an empty file list.
    """
    configuration = current.get("fixedConfiguration")
    if not isinstance(configuration, dict):
        raise ToolFailure("INVALID_SERVER_RESPONSE", "Alias configuration metadata is invalid.",
                          hint="Inspect the alias on the server before editing its configuration.")
    files = configuration.get("@files")
    if files is None:
        return []
    invalid = ToolFailure(
        "INVALID_SERVER_RESPONSE", "Existing alias attachment metadata is invalid; nothing was changed.",
        hint="Inspect the alias on the server before editing its configuration.",
    )
    if not isinstance(files, dict):
        raise invalid
    references = []
    for field, parts in files.items():
        if not isinstance(field, str) or not field or not isinstance(parts, list):
            raise invalid
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("fileId"), str):
                raise invalid
            try:
                UUID(part["fileId"])
            except ValueError as exc:
                raise invalid from exc
            references.append({"fileName": field, "existingFileId": part["fileId"]})
    return references


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_aliases_list",
        description=(
            "List ACTIVE command aliases (shortcuts to base templates with fixed configuration). "
            "Archived aliases are excluded; fetch one by id with tuoni_alias_get."
        ),
    )
    async def aliases_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        aliases = await call(ctx, sctx.c2.load_aliases)
        return {"aliases": [serialize.alias_to_dict(a) for a in aliases]}

    @mcp.tool(
        name="tuoni_alias_get",
        description=(
            "Fetch a single command alias by id (works for ACTIVE and ARCHIVED aliases). "
            "Note: editing an alias archives the previous version and creates a new one, so "
            "old ids continue to resolve via this endpoint."
        ),
    )
    async def alias_get(alias_id: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        data = await call(
            ctx,
            sctx.c2.request_get,
            f"/api/v1/command-alias/{url_path_segment(alias_id)}",
        )
        if data is None:
            return {"error": "Alias not found", "alias_id": alias_id}
        return serialize.alias_dict_from_raw(data)

    @mcp.tool(
        name="tuoni_alias_download_file",
        description=(
            "Return an authenticated HTTP reference for an alias configuration file. "
            "This tool never fetches or embeds the file body in model context."
        ),
    )
    async def alias_download_file(
        alias_id: AliasIdArg,
        file_id: AliasFileIdArg,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        href = (
            f"/api/v1/command-alias/{url_path_segment(alias_id)}"
            f"/configuration/files/{url_path_segment(file_id)}"
        )
        return {
            "alias_id": alias_id,
            "file_id": file_id,
            "download": authenticated_download_reference(sctx.cfg.url, href),
        }

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_alias_create",
        description=(
            "Create a new command alias on top of an existing base template. "
            "`command_type` is a template id (UUID) or its short name (e.g. 'ls', 'bof'). "
            "`configuration` becomes the alias's fixed configuration. `files` is optional "
            "multipart attachments [{name, filename, content_base64}]."
        ),
    )
    async def alias_create(
        name: str,
        description: str,
        command_type: str,
        configuration: dict[str, Any] | None = None,
        files: list[dict] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        alias = await call(
            ctx,
            sctx.c2.add_alias,
            name,
            description,
            command_type,
            configuration or {},
            decode_files(files),
        )
        return serialize.alias_to_dict(alias)

    @mcp.tool(
        name="tuoni_alias_update",
        description=(
            "Edit an alias's description and/or fixed configuration. Description-only edits "
            "update in place (same id). Changing the fixed configuration triggers an "
            "archive-then-create: the original is archived and a new alias (same name, fresh "
            "id) takes its place - the response then includes a 'previous_alias_id' for the "
            "archived entry. Both fields are optional and merge with the current state. "
            "Existing files are preserved; configuration must contain JSON fields without @files metadata. "
            "The name cannot be changed via this endpoint - delete and recreate to rename."
        ),
    )
    async def alias_update(
        alias_id: str,
        description: str | None = None,
        configuration: dict[str, Any] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        if configuration is not None and "@files" in configuration:
            raise ToolFailure(
                "INVALID_ARGUMENTS", "@files is response metadata, not editable JSON configuration.",
                hint="Omit @files from configuration; existing alias files are preserved automatically.",
            )
        encoded_alias_id = url_path_segment(alias_id)
        current = await call(
            ctx,
            sctx.c2.request_get,
            f"/api/v1/command-alias/{encoded_alias_id}",
        )
        if current is None:
            return {"error": "Alias not found", "alias_id": alias_id}
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if configuration is not None:
            body["fixedConfiguration"] = configuration
            body["configurationFiles"] = _existing_file_references(current)
        if not body:
            return serialize.alias_dict_from_raw(current)
        updated = await call(
            ctx,
            sctx.c2.request_put,
            f"/api/v1/command-alias/{encoded_alias_id}",
            body,
        )
        result = serialize.alias_dict_from_raw(updated)
        new_id = result.get("alias_id")
        if new_id and new_id != alias_id:
            result["previous_alias_id"] = alias_id
            result["note"] = (
                "Configuration change triggered archive-then-create: previous_alias_id is "
                "now ARCHIVED, alias_id is the new ACTIVE entry under the same name."
            )
        return result

    @mcp.tool(
        name="tuoni_alias_delete",
        description="Archive a command alias by alias_id (UUID). Idempotent; archived aliases stay retrievable by id.",
    )
    async def alias_delete(alias_id: str, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        await call(
            ctx,
            sctx.c2.request_delete,
            f"/api/v1/command-alias/{url_path_segment(alias_id)}",
        )
        return {"alias_id": alias_id, "deleted": True}
