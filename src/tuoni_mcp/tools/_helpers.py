from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from mcp.server.fastmcp import Context
from pydantic import Field
from tuoni.TuoniExceptions import (
    ExceptionTuoniAuthentication,
    ExceptionTuoniDeleted,
    ExceptionTuoniRequestFailed,
)

from tuoni_mcp.connection import (
    ServerContext,
    call_with_relogin,
    call_without_retry,
    mutation_once_only_active,
)
from tuoni_mcp.pagination import bounded_text

# Pagination bounds shared by the paginated list tools (discovery, jobs). Capping page_size
# stops the model (or a runaway loop) from requesting an unbounded page that could exhaust
# server or client memory.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 1000

PageArg = Annotated[int, Field(ge=0, description="Zero-based page index.")]
PageSizeArg = Annotated[
    int,
    Field(ge=1, le=MAX_PAGE_SIZE, description=f"Rows per page (1-{MAX_PAGE_SIZE})."),
]


def get_ctx(ctx: Context) -> ServerContext:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


async def call(ctx: Context, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a TuoniLib call with auth retry, re-raising with a non-empty message.

    TuoniLib's request exceptions sometimes carry an empty `.message` (e.g. when the
    Tuoni server returns a 401 with no body). FastMCP serialises the resulting tool
    error as `str(exc)`, which would then come back to the model as an empty string,
    making the failure look silent. Replace empty messages with a synthesised one.
    A request timeout (when TUONI_REQUEST_TIMEOUT is set) surfaces as a clear error too.
    """
    try:
        caller = call_without_retry if mutation_once_only_active() else call_with_relogin
        return await caller(get_ctx(ctx), fn, *args, **kwargs)
    except TimeoutError as exc:
        sctx = get_ctx(ctx)
        raise ExceptionTuoniRequestFailed(
            f"Tuoni request timed out after {sctx.cfg.request_timeout}s against {sctx.cfg.url}"
        ) from exc
    except (ExceptionTuoniAuthentication, ExceptionTuoniRequestFailed, ExceptionTuoniDeleted) as exc:
        msg = (getattr(exc, "message", None) or str(exc) or "").strip()
        if not msg:
            sctx = get_ctx(ctx)
            msg = (
                f"{type(exc).__name__} from Tuoni server with no body "
                f"(likely 401/expired token against {sctx.cfg.url}; "
                "re-login attempted and failed)"
            )
            exc.message = msg
            exc.args = (msg,)
        # Preserve subclass metadata such as the HTTP status. Reconstructing
        # type(exc)(message) also fails for subclasses with richer constructors.
        raise


def decode_b64(value: str, field: str = "content_base64") -> bytes:
    """base64-decode a tool input, raising a clean error instead of crashing on bad data.

    Stays lenient about embedded whitespace/newlines (matching the previous behaviour, so
    MIME-wrapped base64 still works) but reports padding/format errors as a readable message.
    """
    try:
        normalized = "".join(value.split())
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"'{field}' is not valid base64: {exc}") from exc


def decode_files(files: list[dict] | None) -> dict[str, list] | None:
    """Convert MCP-input `[{name, filename, content_base64}]` to TuoniLib `{name: [filename, bytes]}`."""
    if not files:
        return None
    out: dict[str, list] = {}
    for entry in files:
        name = entry["name"]
        filename = entry.get("filename", name)
        out[name] = [filename, decode_b64(entry["content_base64"], f"files[{name}].content_base64")]
    return out


def decode_conf_files(files: list[dict] | None) -> list[tuple[str, str, bytes]] | None:
    """Convert MCP-input `[{name, filename, content_base64}]` to the (name, filename, bytes)
    tuples that TuoniLib's listener/payload plugin `create` expects."""
    if not files:
        return None
    return [
        (
            entry["name"],
            entry.get("filename", entry["name"]),
            decode_b64(entry["content_base64"], f"files[{entry['name']}].content_base64"),
        )
        for entry in files
    ]


def authenticated_download_reference(
    base_url: str,
    href: str,
) -> dict[str, Any]:
    """Return a bounded, credential-free reference to a C2 binary endpoint.

    Download bodies do not belong in MCP model context.  The reference deliberately
    drops user-info, query parameters, and fragments so credentials or signed values
    cannot be reflected into tool output.  Tuoni's documented binary endpoints use
    stable paths and require the caller's normal authenticated session.
    """
    parsed_base = urlsplit(base_url)
    safe_netloc = parsed_base.netloc.rsplit("@", 1)[-1]
    safe_base = urlunsplit(
        (parsed_base.scheme, safe_netloc, parsed_base.path.rstrip("/") + "/", "", "")
    )
    path = urlsplit(href).path or "/"
    relative_href = quote("/" + path.lstrip("/"), safe="/:@!$&'()*+,;=%-._~")
    absolute_url = quote(
        urljoin(safe_base, relative_href.lstrip("/")),
        safe="/:@!$&'()*+,;=%[]-._~",
    )
    return {
        "href": bounded_text(relative_href, 512),
        "url": bounded_text(absolute_url, 768),
        "method": "GET",
        "requires_tuoni_authentication": True,
        "content_included": False,
    }
