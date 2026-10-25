from __future__ import annotations

import hashlib
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from tuoni_mcp.config import MCPMode, Settings
from tuoni_mcp.pagination import CursorError, decode_cursor, encode_cursor, fit_text_window, success_payload
from tuoni_mcp.paths import url_path_segment
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, compact_json, redact, would_fit_success
from tuoni_mcp.registry import ToolFailure, ToolSuccess
from tuoni_mcp.tools._helpers import call, get_ctx


SettingKeyArg = Annotated[
    str,
    Field(min_length=1, max_length=512, description="Exact Tuoni setting key."),
]
SettingCursorArg = Annotated[
    str | None,
    Field(max_length=2048, description="Opaque continuation cursor."),
]
SettingOffsetArg = Annotated[
    int,
    Field(default=0, ge=0, description="Character offset for an initial JSON value window."),
]
SettingWindowArg = Annotated[
    int,
    Field(default=4096, ge=1, le=16_384, description="Maximum JSON characters requested."),
]


def register(mcp: FastMCP, cfg: Settings) -> None:
    @mcp.tool(
        name="tuoni_settings_get_all",
        description="Retrieve all server settings (both core 'server' and 'plugin' sections).",
    )
    async def settings_get_all(ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        return await call(ctx, sctx.c2.load_settings) or {}

    @mcp.tool(
        name="tuoni_setting_get",
        description=(
            "Retrieve one setting by exact key. Small values retain their native response shape. "
            "Oversized values return reconstructable, redacted JSON text windows; follow the "
            "opaque cursor until next is null, concatenate value_json, then parse JSON."
        ),
    )
    async def setting_get(
        key: SettingKeyArg,
        cursor: SettingCursorArg = None,
        offset_chars: SettingOffsetArg = 0,
        max_chars: SettingWindowArg = 4096,
        ctx: Context = None,
    ) -> dict[str, Any] | ToolSuccess:
        sctx = get_ctx(ctx)
        value = await call(
            ctx,
            sctx.c2.request_get,
            f"/api/v1/setting/{url_path_segment(key)}",
        ) or {}
        safe_value = redact(value)
        if cursor is None and offset_chars == 0 and would_fit_success(
            safe_value,
            budget=DETAIL_RESULT_BUDGET,
        ):
            return value

        serialized = compact_json(safe_value)
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        scope = hashlib.sha256(key.encode("utf-8")).hexdigest()
        start = offset_chars
        if cursor is not None:
            if offset_chars != 0:
                raise ToolFailure(
                    "INVALID_ARGUMENTS",
                    "offset_chars cannot be combined with a continuation cursor.",
                    hint="Follow next exactly, or restart without cursor.",
                )
            try:
                decoded = decode_cursor(cursor, tool="tuoni_setting_get")
                # Window size can change without changing the saved position
                # or the setting/version being read.
                if decoded.get("scope") != scope:
                    raise CursorError("CURSOR_SCOPE_CHANGED", "The setting key changed while continuing a cursor.")
                start = decoded["offset"]
                if type(start) is not int or start < 0:
                    raise ValueError("invalid cursor offset")
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolFailure(
                    getattr(exc, "code", "INVALID_CURSOR"),
                    str(exc) if isinstance(exc, CursorError) else "The setting cursor is invalid for these arguments.",
                    hint="Restart this setting read without a cursor.",
                ) from exc
            if decoded.get("fingerprint") != fingerprint:
                raise ToolFailure(
                    "SETTING_CHANGED",
                    "The setting changed while its value was being read.",
                    retryable=True,
                    hint="Restart this setting read without a cursor.",
                )

        def continuation(next_offset: int) -> tuple[str, dict[str, Any]]:
            next_cursor = encode_cursor(
                {
                    "tool": "tuoni_setting_get",
                    "scope": scope,
                    "fingerprint": fingerprint,
                    "offset": next_offset,
                }
            )
            target_arguments = {
                "key": key,
                "cursor": next_cursor,
                "max_chars": max_chars,
            }
            next_call: dict[str, Any] = {
                "name": "tuoni_setting_get",
                "arguments": target_arguments,
            }
            if sctx.cfg.mode is MCPMode.DISPATCH:
                next_call = {
                    "name": "tuoni_call",
                    "arguments": {
                        "name": "tuoni_setting_get",
                        "arguments": target_arguments,
                    },
                }
            return next_cursor, next_call

        def make_payload(window: str, end: int, has_more: bool) -> dict[str, Any]:
            next_cursor, next_call = continuation(end) if has_more else (None, None)
            return success_payload(
                {
                    "key": key,
                    "value_format": "json",
                    "offset_chars": start,
                    "returned_chars": end - start,
                    "total_chars": len(serialized),
                    "value_json": window,
                    "has_more": has_more,
                    "next_offset_chars": end if has_more else None,
                },
                next_cursor=next_cursor,
                next_call=next_call,
            )

        try:
            _, end, has_more, payload = fit_text_window(
                serialized,
                offset_chars=start,
                requested_chars=max_chars,
                budget=DETAIL_RESULT_BUDGET,
                make_payload=make_payload,
            )
        except ValueError as exc:
            raise ToolFailure(
                "INVALID_OFFSET",
                "offset_chars is outside the serialized setting value.",
                hint="Restart without an offset or follow the returned cursor exactly.",
            ) from exc
        # Rebuild the continuation rather than reusing payload["next"]. That copy
        # was already rendered in the client's vocabulary so the window could be
        # measured at its true size; returning it here would let success_result
        # apply the prefix a second time.
        next_cursor, next_call = continuation(end) if has_more else (None, None)
        return ToolSuccess(
            payload["data"],
            next_cursor=next_cursor,
            next_call=next_call,
            redact_output=False,
        )

    if cfg.read_only:
        return

    @mcp.tool(
        name="tuoni_setting_update",
        description="Update a single setting by key. `value` must be JSON-serialisable.",
    )
    async def setting_update(key: str, value: Any, ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        result = await call(
            ctx,
            sctx.c2.request_put,
            f"/api/v1/setting/{url_path_segment(key)}",
            {"value": value},
        )
        return result or {"key": key, "updated": True}

    @mcp.tool(
        name="tuoni_settings_update_bulk",
        description=(
            "Bulk-update multiple settings in one call. `settings` follows Tuoni's "
            "AllSettingsUpdateRequest schema (a mapping of setting key to new value)."
        ),
    )
    async def settings_update_bulk(settings: dict[str, Any], ctx: Context = None) -> dict[str, Any]:
        sctx = get_ctx(ctx)
        result = await call(ctx, sctx.c2.update_settings, settings)
        return result or {"updated": True}
