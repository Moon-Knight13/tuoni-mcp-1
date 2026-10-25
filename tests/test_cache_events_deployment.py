from __future__ import annotations

import json
import time
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from tuoni_mcp.config import Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.events import EVENT_BUFFER_ITEM_MAX_BYTES, TOPIC_MAP, EventBridge
from tuoni_mcp.http_client import TuoniHTTPError
from tuoni_mcp.protocol import ERROR_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, ToolRegistry
from tuoni_mcp.template_cache import TemplateCache
from tuoni_mcp.tools.events import register_specs
import tuoni_mcp.server as server_module


ROOT = Path(__file__).resolve().parents[1]


def test_binary_result_bodies_stay_out_of_bounded_events_and_raw_results():
    from tuoni_mcp.pagination import redact as bounded_redact
    from tuoni_mcp.protocol import redact

    body = "BINARY_BODY_MUST_NOT_APPEAR" * 10_000
    raw = {"result": {"childResults": [{"type": "binary", "name": "image", "result": body}]}}
    for render in (redact, lambda value: bounded_redact(value, max_depth=8)):
        result = render(raw)
        part = result["result"]["childResults"][0]
        assert part["result"] == "[REDACTED]"
        assert part["encoded_chars"] == len(body)
        assert "BINARY_BODY_MUST_NOT_APPEAR" not in json.dumps(result)


def _settings(*, buffer_size: int = 20) -> Settings:
    return Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        event_buffer_size=buffer_size,
        profiles=frozenset({Profile.EVENTS}),
    )


def _json_result(result) -> dict:
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.is_connected = True
        self.handlers: dict[str, object] = {}
        self.disconnect_calls = 0

    def on_event(self, topic: str, handler) -> None:
        self.handlers[topic] = handler

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


async def test_template_cache_uses_one_agent_and_one_global_request_not_n_plus_one() -> None:
    calls: list[str] = []
    templates = [
        {"id": f"template-{index}", "name": f"command-{index}"}
        for index in range(70)
    ]

    async def fetch(path: str):
        calls.append(path)
        if path == "/api/v1/agents/agent-1":
            return {
                "guid": "agent-1",
                "availableCommandTemplates": [item["id"] for item in templates],
            }
        if path == "/api/v1/command-templates":
            return templates
        raise AssertionError(f"unexpected fetch: {path}")

    cache = TemplateCache(ttl_seconds=60)
    agent, compatible = await cache.compatible_templates(fetch, "agent-1")
    assert agent and agent["guid"] == "agent-1"
    assert len(compatible) == 70
    assert sorted(calls) == [
        "/api/v1/agents/agent-1",
        "/api/v1/command-templates",
    ]

    # A search followed by an exact describe in the same minute must not reload
    # either catalog and must use the shared template endpoint.
    _, exact = await cache.exact_template(fetch, "agent-1", "command-42")
    assert [item["id"] for item in exact] == ["template-42"]
    assert len(calls) == 2
    assert not any(path.endswith("/command-templates") and "/agents/" in path for path in calls)


async def test_template_cache_coalesces_concurrent_cold_misses() -> None:
    calls: list[str] = []
    unblock = __import__("asyncio").Event()

    async def fetch(path: str):
        calls.append(path)
        await unblock.wait()
        if path == "/api/v1/command-templates":
            return [{"id": "template-1", "name": "cmd"}]
        return {"guid": "agent-1", "availableCommandTemplates": ["template-1"]}

    cache = TemplateCache()
    import asyncio

    tasks = [
        asyncio.create_task(cache.compatible_templates(fetch, "agent-1"))
        for _ in range(5)
    ]
    await asyncio.sleep(0)
    unblock.set()
    await asyncio.gather(*tasks)
    assert calls.count("/api/v1/agents/agent-1") == 1
    assert calls.count("/api/v1/command-templates") == 1


async def test_exact_lookup_copies_only_selected_schemas_and_keeps_cache_isolated():
    templates = [{"id": str(index), "name": f"command-{index}",
                  "configurationSchema": {"description": "x" * 10_000}} for index in range(100)]

    async def fetch(path):
        if path.endswith("/command-templates"):
            return templates
        return {"availableCommandTemplates": [str(index) for index in range(100)]}

    cache = TemplateCache()
    _, matches, candidates = await cache.lookup_template(fetch, "agent", "command-42")
    assert len(matches) == 1
    assert len(candidates) == 100
    assert all("configurationSchema" not in item for item in candidates)
    matches[0]["configurationSchema"]["description"] = "changed"
    _, again = await cache.exact_template(fetch, "agent", "42")
    assert again[0]["configurationSchema"]["description"] == "x" * 10_000


async def test_template_cache_bounds_unique_agent_entries_and_locks() -> None:
    async def fetch(path: str):
        return {"guid": path.rsplit("/", 1)[-1], "availableCommandTemplates": []}

    cache = TemplateCache(max_agents=3)
    for index in range(20):
        agent = await cache.agent(fetch, f"agent-{index}")
        assert agent and agent["guid"] == f"agent-{index}"

    assert tuple(cache._agents) == ("agent-17", "agent-18", "agent-19")
    # Weak lock entries disappear once no active call or waiter owns them.
    assert len(cache._agent_locks) <= 1


async def test_agent_cache_bounds_aggregate_bytes_and_skips_oversized_records():
    cache = TemplateCache(max_agent_bytes=400)

    async def fetch(path):
        return {"guid": path, "metadata": "x" * 180}

    for index in range(10):
        await cache.agent(fetch, str(index))
    assert cache._agent_bytes <= 400
    assert len(cache._agents) == 1
    cache.invalidate_agent("9")
    assert cache._agent_bytes == 0

    async def oversized(path):
        return {"guid": path, "metadata": "x" * 500}

    assert (await cache.agent(oversized, "large"))["guid"] == "/api/v1/agents/large"
    assert not cache._agents


async def test_event_lifespan_keeps_watchdog_after_initial_connect_failure(
    monkeypatch,
    capsys,
) -> None:
    cfg = _settings()
    c2 = MagicMock()
    websocket = _FakeWebSocket()
    c2.connect_websocket.side_effect = [RuntimeError("temporarily unavailable"), websocket]

    async def initial_login(ctx: ServerContext) -> None:
        ctx.last_login = time.monotonic()

    monkeypatch.setattr(server_module, "TuoniC2", lambda **kwargs: c2)
    monkeypatch.setattr(server_module, "login", initial_login)

    async with server_module._make_lifespan(cfg)(MagicMock()) as server_context:
        bridge = server_context.event_bridge
        assert isinstance(bridge, EventBridge)
        await bridge.startup_task
        assert bridge._watchdog is not None
        assert not bridge._watchdog.done()
        assert bridge.status.connected is False
        assert "temporarily unavailable" in (bridge.status.last_error or "")

        await bridge._reconnect_if_needed()

        assert bridge.status.connected is True
        assert bridge.status.reconnect_attempts == 1
        assert bridge.status.last_error is None
        assert len(websocket.handlers) == len(TOPIC_MAP)

    assert bridge._watchdog is None
    assert websocket.disconnect_calls == 1
    assert "watchdog will retry" in capsys.readouterr().err


async def test_event_connect_refreshes_auth_under_the_shared_lock_before_each_attempt(
    fake_c2,
) -> None:
    cfg = _settings()
    server_context = ServerContext(c2=fake_c2, cfg=cfg)
    original_login = fake_c2.login

    def locked_login(url: str, username: str, password: str) -> None:
        assert server_context.login_lock.locked()
        original_login(url, username, password)

    def connect_websocket(timeout: int) -> _FakeWebSocket:
        fake_c2._record("connect_websocket", timeout)
        return _FakeWebSocket()

    fake_c2.login = locked_login
    fake_c2.connect_websocket = connect_websocket
    bridge = EventBridge(server_context)

    await bridge._connect()
    assert bridge.ws is not None
    bridge.ws.is_connected = False
    server_context.last_login = None
    await bridge._reconnect_if_needed()

    auth_and_connect = [
        name
        for name, _args, _kwargs in fake_c2.calls
        if name in {"login", "connect_websocket"}
    ]
    assert auth_and_connect == [
        "login",
        "connect_websocket",
        "login",
        "connect_websocket",
    ]
    assert bridge.status.reconnect_attempts == 1
    await bridge.stop()


@pytest.mark.parametrize("status, retry_succeeds", [(401, True), (401, False), (403, False)])
async def test_event_handshake_refreshes_revoked_token_once_but_not_permissions(
    fake_c2, status, retry_succeeds,
):
    ctx = ServerContext(c2=fake_c2, cfg=_settings(), last_login=time.monotonic())
    attempts = []

    def connect(timeout):
        attempts.append(timeout)
        if len(attempts) == 1 or not retry_succeeds:
            raise TuoniHTTPError(status, "fixture rejected handshake")
        return _FakeWebSocket()

    original_login = fake_c2.login

    def locked_login(*args):
        assert ctx.login_lock.locked()
        original_login(*args)

    fake_c2.login = locked_login
    fake_c2.connect_websocket = connect
    bridge = EventBridge(ctx)
    try:
        await bridge.start()
        assert bridge.status.connected is retry_succeeds
        assert len(attempts) == (2 if status == 401 else 1)
        assert sum(name == "login" for name, *_ in fake_c2.calls) == (1 if status == 401 else 0)
        if not retry_succeeds:
            assert "fixture rejected handshake" in bridge.status.last_error
    finally:
        await bridge.stop()


async def test_event_bridge_is_not_installed_without_the_events_profile(
    monkeypatch,
) -> None:
    cfg = Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=frozenset({Profile.AGENT_OPS}),
    )
    c2 = MagicMock()

    async def initial_login(ctx: ServerContext) -> None:
        ctx.last_login = time.monotonic()

    monkeypatch.setattr(server_module, "TuoniC2", lambda **kwargs: c2)
    monkeypatch.setattr(server_module, "login", initial_login)

    async with server_module._make_lifespan(cfg)(MagicMock()) as server_context:
        assert server_context.event_bridge is None
        c2.connect_websocket.assert_not_called()


async def test_event_poll_is_cursor_based_bounded_and_redacted() -> None:
    cfg = _settings(buffer_size=3)
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    bridge = EventBridge(server_context)
    bridge.status.connected = True
    server_context.event_bridge = bridge
    registry = ToolRegistry()
    register_specs(registry, cfg)
    spec = registry.get("tuoni_events_poll")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=server_context)

    uri = "tuoni://events/commands/results"
    initial = await registry.invoke_spec(spec, {"topics": [uri]}, invocation)
    cursor = _json_result(initial)["next_cursor"]
    assert cursor

    for index in range(5):
        bridge._enqueue(
            uri,
            {
                "received_at": float(index),
                "topic": "command-result",
                "data": {
                    "index": index,
                    "authorization": "Bearer never-return-this",
                    "large": "🙂" * 2_000,
                },
            },
        )

    page = await registry.invoke_spec(
        spec,
        {"cursor": cursor, "topics": [uri], "limit": 50},
        invocation,
    )
    payload = _json_result(page)
    serialized = page.model_dump_json(by_alias=True, exclude_none=True)

    assert page.isError is False
    assert payload["ok"] is True
    assert payload["data"]["dropped_before_cursor"] is True
    assert 1 <= len(payload["data"]["events"]) <= 3
    assert payload["next"]["name"] == "tuoni_events_poll"
    assert payload["next"]["arguments"]["cursor"] == payload["next_cursor"]
    assert "never-return-this" not in serialized
    assert call_result_size(page) <= LIST_RESULT_BUDGET


def test_event_topic_enum_is_complete_and_discoverable() -> None:
    registry = ToolRegistry()
    register_specs(registry, _settings())
    spec = registry.get("tuoni_events_poll")
    assert spec is not None
    topics = spec.input_schema["properties"]["topics"]

    assert set(topics["items"]["enum"]) == {uri for uri, _ in TOPIC_MAP}
    assert topics["maxItems"] == len(TOPIC_MAP)
    assert "enumerates every supported source URI" in spec.description


def test_event_bridge_bounds_and_redacts_each_item_before_buffering() -> None:
    cfg = _settings(buffer_size=3)
    bridge = EventBridge(ServerContext(c2=MagicMock(), cfg=cfg))
    uri = "tuoni://events/commands/results"
    bridge._enqueue(
        uri,
        {
            "received_at": 1.0,
            "topic": "command-result",
            "data": {
                "authorization": "Bearer retained-secret",
                "password": "retained-password",
                "huge": "🙂" * 1_000_000,
            },
        },
    )

    buffered = bridge.buffers["tuoni://events/all"][0]
    encoded = json.dumps(
        buffered, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(encoded) <= EVENT_BUFFER_ITEM_MAX_BYTES
    assert b"retained-secret" not in encoded
    assert b"retained-password" not in encoded
    assert len(buffered["data"]["huge"].encode("utf-8")) <= 256


async def test_event_burst_has_one_bounded_handoff_and_reports_loss() -> None:
    cfg = _settings(buffer_size=3)
    context = ServerContext(c2=MagicMock(), cfg=cfg)
    bridge = EventBridge(context)
    context.event_bridge = bridge
    bridge.loop = MagicMock()
    bridge.loop.is_closed.return_value = False
    uri = "tuoni://events/commands/results"
    handler = bridge._make_handler(uri)
    for index in range(500):
        handler({"index": index, "password": "private-secret", "large": "x" * 10000})
    bridge.loop.call_soon_threadsafe.assert_called_once()
    bridge._drain_pending()
    events, _ = bridge.events_after(0)
    assert [event["data"]["index"] for event in events] == [497, 498, 499]
    assert "private-secret" not in json.dumps(events)
    assert bridge.snapshot("tuoni://events/_status")["dropped_pending_events"] == 497

    registry = ToolRegistry()
    register_specs(registry, cfg)
    response = await registry.invoke_spec(
        registry.get("tuoni_events_poll"), {}, InvocationContext(context=MagicMock(), server=context)
    )
    assert _json_result(response)["data"]["dropped_pending_events"] == 497
    assert call_result_size(response) <= LIST_RESULT_BUDGET
    await bridge.stop()
    handler({"index": 999})
    bridge._drain_pending()
    assert len(bridge.events_after(0)[0]) == 3


async def test_event_cursor_is_scoped_to_topics_and_errors_are_bounded() -> None:
    cfg = _settings()
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    bridge = EventBridge(server_context)
    server_context.event_bridge = bridge
    registry = ToolRegistry()
    register_specs(registry, cfg)
    spec = registry.get("tuoni_events_poll")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=server_context)

    first = await registry.invoke_spec(
        spec,
        {"topics": ["tuoni://events/agents/new"]},
        invocation,
    )
    cursor = _json_result(first)["next_cursor"]
    invalid = await registry.invoke_spec(
        spec,
        {
            "cursor": cursor,
            "topics": ["tuoni://events/commands/results"],
        },
        invocation,
    )

    assert invalid.isError is True
    assert _json_result(invalid)["code"] == "CURSOR_SCOPE_CHANGED"
    assert call_result_size(invalid) <= ERROR_RESULT_BUDGET


async def test_event_cursor_cannot_claim_a_future_sequence() -> None:
    cfg = _settings()
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    bridge = EventBridge(server_context)
    server_context.event_bridge = bridge
    registry = ToolRegistry()
    register_specs(registry, cfg)
    spec = registry.get("tuoni_events_poll")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=server_context)
    future = registry.cursors.encode({"tool": "tuoni_events_poll", "after": 999, "topics": []})

    response = await registry.invoke_spec(spec, {"cursor": future}, invocation)

    assert response.isError is True
    assert _json_result(response)["code"] == "INVALID_CURSOR"
    assert call_result_size(response) <= ERROR_RESULT_BUDGET


async def test_oversized_first_event_returns_omission_metadata_and_advances_once() -> None:
    cfg = _settings(buffer_size=10)
    server_context = ServerContext(c2=MagicMock(), cfg=cfg)
    bridge = EventBridge(server_context)
    bridge.status.connected = True
    server_context.event_bridge = bridge
    registry = ToolRegistry()
    register_specs(registry, cfg)
    spec = registry.get("tuoni_events_poll")
    assert spec is not None
    invocation = InvocationContext(context=MagicMock(), server=server_context)
    uri = "tuoni://events/commands/results"
    initial = await registry.invoke_spec(spec, {"topics": [uri]}, invocation)
    cursor = _json_result(initial)["next_cursor"]

    huge_nested = {
        (f"outer-{outer}-" + "o" * 120): {
            (f"inner-{inner}-" + "i" * 120): {
                "payload": ("🙂e\u0301\\\"" * 10_000)
            }
            for inner in range(10)
        }
        for outer in range(10)
    }
    bridge._enqueue(
        uri,
        {"received_at": 1.0, "topic": "command-result", "data": huge_nested},
    )
    response = await registry.invoke_spec(
        spec,
        {"cursor": cursor, "topics": [uri], "limit": 10},
        invocation,
    )
    payload = _json_result(response)
    events = payload["data"]["events"]
    decoded_cursor = registry.cursors.decode(payload["next_cursor"], tool="tuoni_events_poll")

    assert response.isError is False
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["data"] == {
        "omitted": True,
        "reason": "event data exceeded the polling byte budget",
    }
    assert decoded_cursor["after"] == 1
    assert call_result_size(response) <= LIST_RESULT_BUDGET


def test_frozen_dependency_and_container_defaults_are_reproducible() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = set(project["project"]["dependencies"])
    assert "mcp>=1.30,<2" in dependencies
    assert "tuoni>=0.17.0,<0.18" in dependencies
    assert not project["tool"].get("uv", {}).get("sources")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {item["name"]: item for item in lock["package"]}
    assert packages["mcp"]["version"].startswith("1.")
    assert packages["tuoni"]["version"].startswith("0.17.")
    assert "registry" in packages["tuoni"]["source"]
    assert not any("directory" in item["source"] for item in lock["package"])

    dockerfile = (ROOT / "scripts/docker/Dockerfile").read_text()
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "uv sync --frozen --all-extras --no-editable" in dockerfile
    production_section = dockerfile.split("FROM build-base AS production-builder", 1)[1].split(
        "FROM python", 1
    )[0]
    assert "--all-extras" not in production_section
    assert "TUONI_MCP_PROFILES=agent-ops" in dockerfile
    assert "TUONI_MCP_MODE=native" in dockerfile
    assert "scripts/env/override.env " not in dockerfile
    assert "COPY .agents/ ./.agents/" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "TUONI_VERIFY_TLS=true" in dockerfile

    dockerignore = (ROOT / "scripts/docker/Dockerfile.dockerignore").read_text()
    assert dockerignore.startswith("# The build context is this standalone repository")
    assert "\n**\n" in dockerignore
    assert "!scripts/env/default.env" in dockerignore
    assert "!scripts/env/override.env.example" in dockerignore
    assert "!scripts/env/override.env\n" not in dockerignore
    assert "!.agents/**" in dockerignore

    default_env = (ROOT / "scripts/env/default.env").read_text()
    assert "TUONI_MCP_PROFILES=agent-ops" in default_env
    assert "TUONI_MCP_MODE=native" in default_env
    assert "TUONI_MCP_READ_ONLY=false" in default_env

    workflows = "\n".join(path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml"))
    assert "uv lock --check" in workflows
    assert "uv sync --frozen --all-extras" in workflows
    docker_test = next(line for line in workflows.splitlines() if "docker run" in line and "tuoni-mcp:test" in line)
    for option in ("--rm", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--tmpfs"):
        assert option in docker_test
