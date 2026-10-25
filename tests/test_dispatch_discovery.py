from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tuoni_mcp.config import ALL_EXPANSION, Profile, Settings
from tuoni_mcp.protocol import call_result_size
from tuoni_mcp.registry import (
    Effect,
    InvocationContext,
    ToolInput,
    ToolRegistry,
    ToolSpec,
    build_dispatch_specs,
    _DispatchCursors,
    ToolFailure,
)
from tuoni_mcp.tools import build_registry


def _catalog(*, profiles=None, read_only=False, registry=None, tool_prefix=""):
    cfg = Settings(
        url="https://example.test", username="test", password="test", verify=True,
        profiles=profiles or frozenset({Profile.AGENT_OPS}), read_only=read_only,
        tool_prefix=tool_prefix,
    )
    registry = registry or build_registry(cfg)
    targets = registry.select(cfg.profiles, read_only=read_only)
    adapters = {
        spec.name: spec
        for spec in build_dispatch_specs(registry, targets, read_only=read_only)
    }
    # No C2 client: discovery must work without contacting an upstream server.
    invocation = InvocationContext(context=None, server=SimpleNamespace(cfg=cfg))
    return registry, adapters, invocation


async def _find(catalog, query, **arguments):
    registry, adapters, invocation = catalog
    result = await registry.invoke_spec(
        adapters["tuoni_find_tool"], {"query": query, **arguments}, invocation,
    )
    assert not result.isError
    assert call_result_size(result) <= 2048
    payload = json.loads(result.content[0].text)
    return payload, [tool["name"] for tool in payload["data"]["tools"]]


@pytest.mark.parametrize(("query", "expected"), [
    ("list agents", "tuoni_agents_list"),
    ("show agents", "tuoni_agents_list"),
    ("Please show me active agents", "tuoni_agents_list"),
    ("command output", "tuoni_command_get"),
    ("read result", "tuoni_command_get"),
    ("read results", "tuoni_command_get"),
    ("command outputs", "tuoni_command_get"),
    ("prepare command execution", "tuoni_command_prepare"),
    ("prepare a command for execution", "tuoni_command_prepare"),
    ("command preparation", "tuoni_command_prepare"),
    ("command validation", "tuoni_command_prepare"),
])
async def test_plain_language_finds_the_relevant_enabled_tool(query, expected):
    _, names = await _find(_catalog(), query)
    assert names[0] == expected


@pytest.mark.parametrize(("query", "expected"), [
    ("show available commands", "tuoni_commands_search"),
    ("check backend connection", "tuoni_capabilities"),
    ("server capabilities", "tuoni_capabilities"),
    ("show supported command templates", "tuoni_commands_search"),
    ("find available templates", "tuoni_commands_search"),
    ("list supported commands", "tuoni_commands_search"),
    ("check backend health", "tuoni_capabilities"),
    ("backend connection status", "tuoni_capabilities"),
    ("show server permissions", "tuoni_capabilities"),
    ("server version", "tuoni_capabilities"),
    ("search list active agents by hostname", "tuoni_agents_list"),
    ("workstation", "tuoni_agents_list"),
    ("run shell command on agent", "tuoni_commands_search"),
    ("find workstations", "tuoni_agents_list"),
    ("search active agents by hostnames", "tuoni_agents_list"),
    ("find shell commands on agent", "tuoni_commands_search"),
])
@pytest.mark.parametrize("read_only", [False, True])
async def test_availability_and_connection_queries_find_enabled_reads(query, expected, read_only):
    _, names = await _find(_catalog(read_only=read_only), query)
    assert names[0] == expected


async def test_availability_keywords_do_not_expose_a_disabled_profile():
    catalog = _catalog(profiles=frozenset({Profile.ACCESS}), read_only=True)
    _, names = await _find(catalog, "show available commands")
    assert "tuoni_commands_search" not in names
    _, names = await _find(catalog, "check backend connection")
    assert names[0] == "tuoni_capabilities"


@pytest.mark.parametrize("query", [
    "cmd", "ls", "procinfo", "cmd command", "ls directory listing",
    "plugin procinfo process", "execute-assembly", "jump-service", "token-list",
])
@pytest.mark.parametrize("read_only", [False, True])
async def test_command_names_offer_a_read_only_catalog_search_offline(query, read_only):
    payload, names = await _find(_catalog(read_only=read_only, tool_prefix="client_"), query)
    assert names == ["tuoni_commands_search"]
    assert payload["data"]["tools"][0]["effect"] == "read"
    assert payload["next"] == {"name": "client_tuoni_call", "arguments": {
        "name": "tuoni_commands_search", "arguments": {"query": query},
    }}
    assert "agent command" in payload["data"]["note"]


@pytest.mark.parametrize("query", ["do not run cmd", "cmd rhinoceros", "procinfo quantum"])
async def test_command_routing_does_not_ignore_negation_or_unknown_words(query):
    _, names = await _find(_catalog(), query)
    assert names == []


async def test_command_routing_does_not_enable_an_excluded_profile():
    payload, names = await _find(_catalog(profiles=frozenset({Profile.ACCESS})), "cmd")
    assert "tuoni_commands_search" not in names
    assert payload["next"]["name"] == "tuoni_find_tool"


@pytest.mark.parametrize("query,expected", [
    ("jobs", "tuoni_jobs_list"), ("download", "tuoni_file_download"),
])
async def test_command_routing_preserves_enabled_operations_with_the_same_word(query, expected):
    payload, names = await _find(_catalog(profiles=ALL_EXPANSION), query)
    assert expected in names
    assert "agent command" not in payload["data"].get("note", "")


async def test_listener_phrase_uses_only_enabled_profiles():
    _, disabled = await _find(_catalog(), "list listeners")
    assert disabled == []
    _, enabled = await _find(_catalog(profiles=ALL_EXPANSION), "list listeners")
    assert enabled[0] == "tuoni_listeners_list"


@pytest.mark.parametrize("query", [
    "agents quantum teleportation", "read result rhinoceros", "zeppelins",
    "please me", "   ", "!!!", "do not list agents",
    "available commands rhinoceros", "server connection quantum teleportation",
    "do not check backend connection", "server credentials",
    "do not run shell command on agent", "search agents by encryption key", "by on",
    "do not prepare command execution", "prepare command execution rhinoceros",
])
async def test_unknown_or_negated_terms_do_not_broaden_discovery(query):
    _, names = await _find(_catalog(profiles=ALL_EXPANSION), query)
    assert names == []


@pytest.mark.parametrize("query", ["tuoni_command_get", "  TUONI_COMMAND_GET  "])
async def test_exact_names_rank_first(query):
    _, names = await _find(_catalog(profiles=ALL_EXPANSION), query)
    assert names[0] == "tuoni_command_get"


async def test_read_only_discovery_never_reveals_mutating_or_prepare_tools():
    catalog = _catalog(profiles=ALL_EXPANSION, read_only=True)
    registry, _, _ = catalog
    for query in ("command", "prepare command", "prepare command execution", "command preparation",
                  "listener", "tuoni_command_prepare"):
        _, names = await _find(catalog, query)
        assert all(registry.get(name).effect is Effect.READ for name in names)
        assert "tuoni_command_prepare" not in names
        assert "tuoni_operation_execute" not in names


async def test_search_does_not_make_hidden_operations_callable():
    catalog = _catalog()
    registry, adapters, invocation = catalog
    name = "tuoni_listeners_list"
    _, names = await _find(catalog, name)
    assert name not in names
    result = await registry.invoke_spec(
        adapters["tuoni_tool_schema"], {"name": name}, invocation,
    )
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "TOOL_NOT_FOUND"

    _, names = await _find(catalog, "tuoni_operation_execute")
    assert "tuoni_operation_execute" not in names
    result = await registry.invoke_spec(
        adapters["tuoni_call"], {"name": "tuoni_operation_execute"}, invocation,
    )
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "TOOL_NOT_FOUND"


async def test_token_matches_page_deterministically_with_complete_continuations():
    registry = ToolRegistry()

    async def unused_handler(_invocation, _arguments):
        raise AssertionError("Discovery must not invoke a discovered tool")

    for index in reversed(range(25)):
        registry.add(ToolSpec(
            name=f"tuoni_probe_{index:02d}",
            description="List retained probe records. " + "記" * 180,
            input_model=ToolInput, handler=unused_handler,
            profiles=frozenset({Profile.AGENT_OPS}), keywords=("probe", "records"),
        ))
    catalog = _catalog(registry=registry, tool_prefix="client_")
    arguments = {"query": "list probe records", "limit": 20}
    seen = []
    pages = 0
    while True:
        payload, names = await _find(catalog, **arguments)
        assert names
        seen.extend(names)
        pages += 1
        if payload["next"] is None:
            break
        assert payload["next"]["name"] == "client_tuoni_find_tool"
        arguments = payload["next"]["arguments"]
    assert pages > 1
    assert seen == [f"tuoni_probe_{index:02d}" for index in range(25)]
    _, repeated = await _find(catalog, "list probe records", limit=20)
    assert repeated == seen[:len(repeated)]


async def test_paraphrasing_does_not_allow_reusing_a_cursor_for_another_query():
    catalog = _catalog(profiles=ALL_EXPANSION)
    payload, _ = await _find(catalog, "command", limit=1)
    assert payload["next_cursor"]
    registry, adapters, invocation = catalog
    result = await registry.invoke_spec(
        adapters["tuoni_find_tool"],
        {"query": "commands", "cursor": payload["next_cursor"], "limit": 1}, invocation,
    )
    assert result.isError
    assert json.loads(result.content[0].text)["code"] == "CURSOR_SCOPE_CHANGED"


@pytest.mark.parametrize("limit", [50, 100])
async def test_large_discovery_requests_still_page_with_exact_bounded_schemas(limit):
    catalog = _catalog(profiles=ALL_EXPANSION, tool_prefix="client_")
    arguments = {"query": "tuoni", "limit": limit}
    seen = []
    for _ in range(100):
        payload, names = await _find(catalog, **arguments)
        seen.extend(names)
        for item in payload["data"]["tools"]:
            assert item["schema"] == {
                "name": "client_tuoni_tool_schema", "arguments": {"name": item["name"]},
            }
            if "input_schema" in item:
                assert item["input_schema"] == catalog[0].get(item["name"]).input_schema
        if payload["next"] is None:
            break
        arguments = payload["next"]["arguments"]
    else:
        pytest.fail("Discovery did not finish")
    assert len(seen) == len(set(seen))
    expected = {s.name for s in catalog[0].select(ALL_EXPANSION, read_only=False)
                if s.dispatch_visible and s.effect is not Effect.EXECUTE}
    assert set(seen) == expected


async def test_no_match_offers_explicit_catalog_browse_and_keeps_negation_narrow():
    catalog = _catalog()
    payload, names = await _find(catalog, "command result manifest")
    assert names == []
    assert "browse" in payload["data"]["note"]
    assert payload["next"] == {"name": "tuoni_find_tool", "arguments": {"query": "tuoni"}}
    _, names = await _find(catalog, **payload["next"]["arguments"])
    assert names
    payload, names = await _find(catalog, "do not list agents")
    assert names == [] and payload["next"] is None


def test_discovery_handles_bind_scope_expire_and_do_not_survive_a_restart():
    now = [100.0]
    store = _DispatchCursors(clock=lambda: now[0])
    token = store.encode(8, "private query")
    assert len(token) == len(store.placeholder) == 14
    assert "private" not in token
    assert store.decode(token, "private query") == 8
    for other in ("different query", "PRIVATE QUERY"):
        with pytest.raises(ToolFailure, match="query changed") as error:
            store.decode(token, other)
        assert error.value.code == "CURSOR_SCOPE_CHANGED"
    with pytest.raises(ToolFailure):
        _DispatchCursors().decode(token, "private query")
    with pytest.raises(ToolFailure):
        store.decode(token[:-1], "private query")
    now[0] += 300
    with pytest.raises(ToolFailure, match="expired"):
        store.decode(token, "private query")


def test_discovery_handle_memory_is_bounded_and_expired_handles_are_reclaimed():
    now = [0.0]
    store = _DispatchCursors(clock=lambda: now[0])
    oldest = store.encode(1, "query")
    for offset in range(2, 150):
        newest = store.encode(offset, "query")
    assert len(store.records) == 128
    with pytest.raises(ToolFailure):
        store.decode(oldest, "query")
    assert store.decode(newest, "query") == 149
    now[0] = 301
    fresh = store.encode(1, "query")
    assert len(store.records) == 1 and store.decode(fresh, "query") == 1
