from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from tuoni import TuoniC2

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.protocol import LIST_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry


@pytest.fixture(params=[MCPMode.NATIVE, MCPMode.DISPATCH])
def mode(request):
    return request.param


def _harness(mode, request_get):
    # Exercise the installed SDK's real query construction and record mapping.
    c2 = TuoniC2(verify=True)
    c2.request_get = request_get
    cfg = Settings(
        url="https://example.test", username="test", password="test", verify=True,
        profiles=frozenset({Profile.DISCOVERY}), mode=mode, read_only=True,
    )
    registry = build_registry(cfg)
    server = ServerContext(c2=c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(cfg.profiles, read_only=True)
    specs = {
        spec.name: spec for spec in (
            selected if mode is MCPMode.NATIVE else
            build_dispatch_specs(registry, selected, read_only=True)
        )
    }

    async def invoke(name, arguments):
        if mode is MCPMode.DISPATCH and name != "tuoni_call":
            name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
        return await registry.invoke_spec(specs[name], arguments, invocation)

    return invoke


def _payload(result):
    assert call_result_size(result) <= LIST_RESULT_BUDGET
    return json.loads(result.content[0].text)


def _record(kind, index):
    common = {"id": f"00000000-0000-0000-0000-{index:012d}", "status": "ACTIVE", "note": ""}
    if kind == "hosts":
        return {**common, "address": f"host-{index}", "name": "lab"}
    if kind == "services":
        return {**common, "address": f"host-{index}", "port": 0, "protocol": "tcp", "banner": ""}
    return {
        **common, "username": f"user-{index}", "password": "credential-secret-must-not-leak",
        "host": "lab", "realm": "EXAMPLE", "source": "manual",
    }


@pytest.mark.parametrize(
    ("kind", "arguments", "expected"),
    [
        (
            "hosts",
            {"address": "lab & east/北?#=%", "name": "*gateway*", "search": "blue+tag & café",
             "include_archived": False},
            {"address": ["lab & east/北?#=%"], "name": ["*gateway*"],
             "anyFieldContains": ["blue+tag & café"], "includeArchived": ["false"]},
        ),
        (
            "services",
            {"address": "::1", "port": 0, "protocol": "tcp", "search": "test#?&=+",
             "include_archived": False},
            {"address": ["::1"], "port": ["0"], "protocol": ["tcp"],
             "anyFieldContains": ["test#?&=+"], "includeArchived": ["false"]},
        ),
        (
            "credentials",
            {"username": "user+name", "host": "lab", "realm": "LAB\\Research",
             "source": "manual #1", "search": "café", "include_archived": True},
            {"username": ["user+name"], "host": ["lab"], "realm": ["LAB\\Research"],
             "source": ["manual #1"], "anyFieldContains": ["café"], "includeArchived": ["true"]},
        ),
    ],
)
async def test_typed_filters_reach_real_sdk_query_without_extra_parameters(mode, kind, arguments, expected):
    calls = []

    def request_get(uri):
        calls.append(uri)
        parsed = urlsplit(uri)
        assert parsed.path == f"/api/v1/discovery/{kind}"
        assert parsed.fragment == ""
        assert parse_qs(parsed.query) == {"page": ["0"], "pageSize": ["20"], **expected}
        return {"items": [_record(kind, 1)]}

    result = await _harness(mode, request_get)(f"tuoni_{kind}_list", arguments)

    assert not result.isError, result
    assert len(calls) == 1
    assert _payload(result)["data"][kind][0]["id"] == _record(kind, 1)["id"]
    if kind == "credentials":
        assert "credential-secret-must-not-leak" not in result.model_dump_json()
        assert _payload(result)["data"][kind][0]["password"] == "[REDACTED]"


@pytest.mark.parametrize("kind", ["hosts", "services", "credentials"])
@pytest.mark.parametrize("raw_filter", [None, "", " ", "name=encoded%2Bhost&includeArchived=true"])
async def test_raw_filter_filter_is_preserved_verbatim(mode, kind, raw_filter):
    calls = []

    def request_get(uri):
        calls.append(uri)
        return {"items": []}

    result = await _harness(mode, request_get)(f"tuoni_{kind}_list", {"filter": raw_filter})

    assert not result.isError, result
    suffix = "" if raw_filter is None else f"&{raw_filter}"
    assert calls == [f"/api/v1/discovery/{kind}?page=0&pageSize=20{suffix}"]


@pytest.mark.parametrize(
    ("kind", "arguments"),
    [
        ("hosts", {"address": ""}),
        ("hosts", {"address": " \t\r\n"}),
        ("hosts", {"search": "\u2003\u2003"}),
        ("hosts", {"address": 123}),
        ("hosts", {"name": "x" * 257}),
        ("hosts", {"search": ["lab"]}),
        ("hosts", {"search": "x" * 257}),
        ("hosts", {"include_archived": "false"}),
        ("hosts", {"include_archived": 0}),
        ("hosts", {"port": 80}),
        ("services", {"port": -1}),
        ("services", {"port": 65536}),
        ("services", {"port": True}),
        ("services", {"port": "80"}),
        ("services", {"port": 80.0}),
        ("services", {"protocol": {"eq": "tcp"}}),
        ("services", {"protocol": "  "}),
        ("services", {"search": "\t"}),
        ("credentials", {"username": False}),
        ("credentials", {"host": "x" * 257}),
        ("credentials", {"realm": "\n"}),
        ("credentials", {"search": " \t "}),
        ("credentials", {"address": "lab"}),
        ("credentials", {"password": "not-a-supported-selector"}),
    ],
)
async def test_invalid_typed_selectors_fail_before_backend_access(mode, kind, arguments):
    calls = []
    result = await _harness(mode, lambda uri: calls.append(uri))(
        f"tuoni_{kind}_list", arguments,
    )

    assert result.isError
    assert _payload(result)["code"] == "INVALID_ARGUMENTS"
    assert calls == []


@pytest.mark.parametrize(
    ("kind", "selector"),
    [("hosts", "address"), ("services", "protocol"), ("credentials", "realm")],
)
async def test_nonblank_typed_filters_preserve_surrounding_whitespace(mode, kind, selector):
    calls = []

    def request_get(uri):
        calls.append(uri)
        query = parse_qs(urlsplit(uri).query)
        assert query[selector] == ["  exact value \t"]
        assert query["anyFieldContains"] == [" \tsearch value  "]
        return {"items": []}

    result = await _harness(mode, request_get)(
        f"tuoni_{kind}_list", {selector: "  exact value \t", "search": " \tsearch value  "},
    )

    assert not result.isError, result
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("kind", "typed"),
    [("hosts", {"address": "lab"}), ("hosts", {"include_archived": False}),
     ("services", {"port": 0}), ("credentials", {"search": "lab"})],
)
@pytest.mark.parametrize("raw_filter", ["", "includeArchived=true"])
async def test_raw_and_typed_filters_are_rejected_even_for_false_zero_or_empty_raw(mode, kind, typed, raw_filter):
    calls = []
    result = await _harness(mode, lambda uri: calls.append(uri))(
        f"tuoni_{kind}_list", {"filter": raw_filter, **typed},
    )

    assert result.isError
    assert _payload(result)["code"] == "INVALID_ARGUMENTS"
    assert "either typed" in _payload(result)["message"]
    assert calls == []


@pytest.mark.parametrize("kind", ["hosts", "services", "credentials"])
async def test_typed_filters_survive_local_and_upstream_continuations_with_redaction(mode, kind):
    records = [_record(kind, index) for index in range(5)]
    calls = []

    def request_get(uri):
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        assert parsed.path == f"/api/v1/discovery/{kind}"
        assert query["anyFieldContains"] == ["lab + east"]
        assert query["includeArchived"] == ["false"]
        page, size = int(query["page"][0]), int(query["pageSize"][0])
        calls.append(page)
        return {"items": records[page * size:(page + 1) * size]}

    invoke = _harness(mode, request_get)
    name = f"tuoni_{kind}_list"
    arguments = {"search": "lab + east", "include_archived": False, "page_size": 2, "limit": 1}
    ids = []
    for _ in range(10):
        result = await invoke(name, arguments)
        assert not result.isError, result
        assert "credential-secret-must-not-leak" not in result.model_dump_json()
        payload = _payload(result)
        ids.extend(record["id"] for record in payload["data"][kind])
        if payload["next"] is None:
            break
        name, arguments = payload["next"]["name"], payload["next"]["arguments"]
    else:
        pytest.fail("Discovery continuation did not terminate")

    assert ids == [record["id"] for record in records]
    assert calls == [0, 0, 1, 1, 2]


@pytest.mark.parametrize(
    ("kind", "typed", "changed"),
    [("hosts", {"include_archived": False}, {"include_archived": True}),
     ("services", {"port": 0}, {"port": 1}),
     ("credentials", {"host": "lab"}, {"host": "another-lab"})],
)
async def test_cursor_is_bound_to_typed_filters_before_backend_access(mode, kind, typed, changed):
    calls = []

    def request_get(uri):
        calls.append(uri)
        return {"items": [_record(kind, index) for index in range(2)]}

    invoke = _harness(mode, request_get)
    name = f"tuoni_{kind}_list"
    first = await invoke(name, {**typed, "limit": 1})
    assert not first.isError, first
    cursor = _payload(first)["next_cursor"]
    assert cursor

    result = await invoke(name, {**changed, "limit": 1, "cursor": cursor})

    assert result.isError
    assert _payload(result)["code"] == "CURSOR_SCOPE_CHANGED"
    assert len(calls) == 1
