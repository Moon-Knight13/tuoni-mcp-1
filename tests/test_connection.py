from __future__ import annotations

import time

from tuoni_mcp.config import Settings
from tuoni_mcp.connection import (
    TOKEN_REFRESH_AFTER_SECONDS,
    ServerContext,
    call_with_relogin,
)


def _cfg() -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        read_only=False,
        event_buffer_size=200,
    )


async def test_proactive_relogin_when_token_stale(fake_c2):
    ctx = ServerContext(c2=fake_c2, cfg=_cfg())
    ctx.last_login = time.monotonic() - (TOKEN_REFRESH_AFTER_SECONDS + 5)
    await call_with_relogin(ctx, fake_c2.load_agents)
    names = [c[0] for c in fake_c2.calls]
    assert "login" in names, "a stale token should trigger a proactive re-login"
    assert names.index("login") < names.index("load_agents"), "re-login must happen before the call"


async def test_no_relogin_when_token_fresh(fake_c2):
    ctx = ServerContext(c2=fake_c2, cfg=_cfg())
    ctx.last_login = time.monotonic()
    await call_with_relogin(ctx, fake_c2.load_agents)
    assert "login" not in [c[0] for c in fake_c2.calls], "a fresh token must not re-login"


async def test_first_call_logs_in_when_never_logged_in(fake_c2):
    ctx = ServerContext(c2=fake_c2, cfg=_cfg())
    assert ctx.last_login is None
    await call_with_relogin(ctx, fake_c2.load_agents)
    assert "login" in [c[0] for c in fake_c2.calls]
    assert ctx.last_login is not None
