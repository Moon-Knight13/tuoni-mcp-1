from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from tuoni import TuoniUser

from tuoni_mcp import serialize
from tuoni_mcp.config import Settings
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.tools._helpers import call, get_ctx


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_users_list",
        description="List all users defined on the Tuoni server.",
    )
    async def users_list(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        users = await call(ctx, sctx.c2.load_users)
        return {"users": [serialize.user_to_dict(u) for u in users]}

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_user_create",
        description=(
            "Create a new Tuoni user. `authorities` is a list of permissions such as "
            "['MANAGE_LISTENERS','MANAGE_USERS','SEND_COMMANDS','MANAGE_PAYLOADS',"
            "'MODIFY_FILES','VIEW_RESOURCES','MANAGE_AGENTS','MANAGE_DISCOVERY','MANAGE_JOBS']."
        ),
    )
    async def user_create(
        username: str,
        password: str,
        authorities: list[str],
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        user = await call(ctx, sctx.c2.add_user, username, password, authorities)
        return serialize.user_to_dict(user)

    async def _load_user(sctx, ctx, username: str) -> TuoniUser | None:
        data = await call(
            ctx,
            sctx.c2.request_get,
            f"/api/v1/users/{url_path_segment(username)}",
        )
        if data is None:
            return None
        return TuoniUser(data, sctx.c2)

    @mcp.tool(
        name="tuoni_user_set_authorities",
        description="Replace a user's permission set.",
    )
    async def user_set_authorities(
        username: str,
        authorities: list[str],
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        user = await _load_user(sctx, ctx, username)
        if user is None:
            return {"error": "User not found", "username": username}
        await call(ctx, user.set_authorities, authorities)
        return serialize.user_to_dict(user)

    @mcp.tool(
        name="tuoni_user_set_password",
        description="Set a new password for a user.",
    )
    async def user_set_password(
        username: str,
        new_password: str,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        user = await _load_user(sctx, ctx, username)
        if user is None:
            return {"error": "User not found", "username": username}
        await call(ctx, user.set_password, new_password)
        return {"username": username, "password_updated": True}

    @mcp.tool(
        name="tuoni_user_set_enabled",
        description="Enable or disable a user.",
    )
    async def user_set_enabled(
        username: str,
        enabled: bool,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        user = await _load_user(sctx, ctx, username)
        if user is None:
            return {"error": "User not found", "username": username}
        if enabled:
            await call(ctx, user.enable)
        else:
            await call(ctx, user.disable)
        return serialize.user_to_dict(user)
