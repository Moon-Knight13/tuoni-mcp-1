from __future__ import annotations

import asyncio
import functools
import inspect
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

import anyio
from tuoni import TuoniC2
from tuoni.TuoniExceptions import (
    ExceptionTuoniAuthentication,
    ExceptionTuoniRequestFailed,
)

from tuoni_mcp.config import Settings

T = TypeVar("T")

_MUTATION_ONCE_ONLY: ContextVar[bool] = ContextVar(
    "tuoni_mcp_mutation_once_only", default=False
)


@contextmanager
def mutation_once_only():
    """Mark nested client calls as exactly-once mutations."""
    token = _MUTATION_ONCE_ONLY.set(True)
    try:
        yield
    finally:
        _MUTATION_ONCE_ONLY.reset(token)


def mutation_once_only_active() -> bool:
    return _MUTATION_ONCE_ONLY.get()

# Lightweight client adapters without SDK session hooks use this bounded
# credential-login interval. The production transport refreshes its SDK session.
TOKEN_LIFETIME_SECONDS = 120
TOKEN_REFRESH_AFTER_SECONDS = 100

# anyio renamed run_sync's "cancellable" kwarg to "abandon_on_cancel" in 4.1. Pick whichever
# this anyio exposes so a request timeout can abandon the blocking worker thread (instead of
# waiting for it) without emitting a deprecation warning or breaking on anyio 4.0.
_ABANDON_KW = (
    "abandon_on_cancel"
    if "abandon_on_cancel" in inspect.signature(anyio.to_thread.run_sync).parameters
    else "cancellable"
)


@dataclass
class ServerContext:
    c2: TuoniC2
    cfg: Settings
    login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event_bridge: Any = None  # filled in by events.py at lifespan startup
    last_login: float | None = None  # monotonic timestamp of the last successful login


async def to_thread(fn: Callable[..., T], *args: Any, _timeout: float | None = None, **kwargs: Any) -> T:
    """Offload a blocking TuoniLib call to the anyio thread pool.

    When `_timeout` is set the wait is bounded by `anyio.fail_after`; the worker thread is
    abandoned on timeout (Python cannot cancel a thread mid-`requests`-call), so the blocked
    socket may linger until the OS tears it down, but the caller regains control immediately.
    """
    call = functools.partial(fn, *args, **kwargs)
    if _timeout is None:
        return await anyio.to_thread.run_sync(call)
    with anyio.fail_after(_timeout):
        return await anyio.to_thread.run_sync(call, **{_ABANDON_KW: True})


async def login(ctx: ServerContext) -> None:
    await to_thread(
        ctx.c2.login, ctx.cfg.url, ctx.cfg.username, ctx.cfg.password, _timeout=ctx.cfg.request_timeout
    )
    ctx.last_login = time.monotonic()


def _token_is_stale(ctx: ServerContext) -> bool:
    if ctx.last_login is None:
        return True
    return (time.monotonic() - ctx.last_login) >= TOKEN_REFRESH_AFTER_SECONDS


async def _ensure_fresh_token(ctx: ServerContext) -> None:
    """Establish or refresh the session before a tool crosses its request boundary."""
    preflight = getattr(ctx.c2, "ensure_authenticated", None)
    if callable(preflight):
        async with ctx.login_lock:
            if ctx.last_login is None:
                await login(ctx)
                return
            try:
                await to_thread(preflight, _timeout=ctx.cfg.request_timeout)
            except ExceptionTuoniAuthentication:
                await login(ctx)
        return
    if not _token_is_stale(ctx):
        return
    async with ctx.login_lock:
        if _token_is_stale(ctx):
            await login(ctx)


async def ensure_fresh_token(ctx: ServerContext) -> None:
    """Public preflight for a once-only mutating request.

    Call this before crossing the mutation boundary, then use
    :func:`call_without_retry`. Keeping the two phases separate lets callers
    distinguish a safe authentication failure from an ambiguous network failure
    after a command may already have been accepted by Tuoni.
    """
    await _ensure_fresh_token(ctx)


async def call_without_retry(
    ctx: ServerContext,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Invoke one authenticated request exactly once, with no auth replay."""
    with mutation_once_only():
        return await to_thread(fn, *args, _timeout=ctx.cfg.request_timeout, **kwargs)


def _looks_like_auth_error(exc: ExceptionTuoniRequestFailed) -> bool:
    # Fallback only: proactive refresh handles the common case. This catches the rare edge
    # (server-side revocation, restart) where the token dies before it is due to expire.
    # The Tuoni server returns 401 with an empty body when the JWT has expired; TuoniLib
    # wraps that as ExceptionTuoniRequestFailed with an empty/None message.
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return status_code == 401
    msg = (exc.message or "").strip().lower()
    if not msg:
        return True
    return any(t in msg for t in ("unauthorized", "401"))


def authentication_secrets(ctx: ServerContext) -> tuple[str, ...]:
    """Snapshot credential and current SDK tokens without refreshing the session."""
    client = getattr(ctx, "c2", None)
    auth = getattr(client, "_auth", None)
    values = (getattr(getattr(ctx, "cfg", None), "password", None), getattr(client, "_token", None),
              getattr(auth, "access_token", None), getattr(auth, "refresh_token", None),
              *getattr(client, "_redaction_secrets", ()))
    return tuple(value for value in values if isinstance(value, str) and value)


async def call_with_relogin(ctx: ServerContext, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    await _ensure_fresh_token(ctx)
    previous_login = ctx.last_login
    try:
        return await to_thread(fn, *args, _timeout=ctx.cfg.request_timeout, **kwargs)
    except ExceptionTuoniAuthentication:
        pass
    except ExceptionTuoniRequestFailed as exc:
        if not _looks_like_auth_error(exc):
            raise
    async with ctx.login_lock:
        if ctx.last_login == previous_login:
            await login(ctx)
    return await to_thread(fn, *args, _timeout=ctx.cfg.request_timeout, **kwargs)


def make_caller(ctx_getter: Callable[[], ServerContext]) -> Callable[..., Awaitable[Any]]:
    async def _call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await call_with_relogin(ctx_getter(), fn, *args, **kwargs)
    return _call
