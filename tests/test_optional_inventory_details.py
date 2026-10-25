"""Large optional-profile inventories stay pageable and retain exact detail reads."""
from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from tuoni import TuoniC2, TuoniJob, TuoniListenerPlugin

from tuoni_mcp import serialize, server
from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.protocol import DETAIL_RESULT_BUDGET, LIST_RESULT_BUDGET, call_result_size, redact
from tuoni_mcp.tools._json_detail import redact_schema


def _job(index, *, large=False):
    return {
        "id": index, "name": f"fixture-{index}", "status": "RUNNING", "source": {"type": "fixture"},
        "supportedActions": [], "openResources": [{"type": "fixture", "password": "resource-secret"}],
        "messages": [{"text": 'Unicode🙂 quote" slash\\\n' * 6, "index": i} for i in range(80 if large else 1)],
    }


def _plugin(index, *, large=False):
    return {
        "identifier": {"id": f"fixture.plugin-{index}"},
        "info": {"name": f"Plugin {index}", "vendor": "fixture", "description": "Synthetic fixture"},
        "configurationSchema": {
            "type": "object", "required": ["password"], "additionalProperties": False,
            "properties": {
                "password": {"type": "string", "minLength": 8, "default": "schema-secret"},
                **{f"field-{i}": {"type": "string", "description": "Schema🙂 " * 8, "minLength": 1}
                   for i in range(100 if large else 1)},
            },
        },
        "defaultConfiguration": {"password": "example-secret", "field-0": "example"},
    }


def _setup(monkeypatch, mode, prefix=""):
    jobs = [_job(i, large=i == 1) for i in range(1, 4)]
    plugins = [_plugin(i, large=i == 2) for i in range(1, 5)]
    requests = []
    c2 = TuoniC2(verify=True)

    def request_get(uri):
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        requests.append(parsed.path)
        if parsed.path == "/api/v1/jobs/active":
            return copy.deepcopy(jobs)
        if parsed.path == "/api/v1/jobs/all":
            page, size = int(query["page"][0]), int(query["pageSize"][0])
            return {"items": copy.deepcopy(jobs[page * size:(page + 1) * size])}
        if parsed.path.startswith("/api/v1/job/"):
            return copy.deepcopy(next((j for j in jobs if str(j["id"]) == parsed.path.rsplit("/", 1)[1]), None))
        assert parsed.path == "/api/v1/plugins/listeners"
        return {p["identifier"]["id"]: copy.deepcopy(p) for p in plugins}

    c2.request_get = request_get
    c2.login = lambda *args: None
    c2.close = lambda: None
    monkeypatch.setattr(server, "TuoniC2", lambda **kwargs: c2)
    cfg = Settings(url="https://example.test", username="fixture", password="fixture", verify=True,
                   profiles=frozenset({Profile.ACCESS, Profile.ADMINISTRATION}), mode=mode,
                   read_only=True, tool_prefix=prefix)
    return server.build_server(cfg), c2, jobs, plugins, requests


def _initial(mode, name, arguments):
    if mode is MCPMode.DISPATCH:
        return {"name": "tuoni_call", "arguments": {"name": name, "arguments": arguments}}
    return {"name": name, "arguments": arguments}


async def _call(session, call, prefix, budget):
    name = call["name"]
    # The SDK has no client namespace. Undo exactly the prefix that the simulated
    # client would remove when delivering this otherwise unchanged continuation.
    if prefix and name.startswith(prefix + "tuoni_"):
        name = name[len(prefix):]
    result = await session.call_tool(name, call["arguments"])
    assert call_result_size(result) <= budget
    return result, json.loads(result.content[0].text)


async def _detail(session, initial, prefix):
    current = initial
    windows = []
    for _ in range(40):
        result, payload = await _call(session, current, prefix, DETAIL_RESULT_BUDGET)
        assert not result.isError, payload
        data = payload["data"]
        if data.get("value_format") != "json":
            assert not windows and payload["next"] is None
            return data, 1
        assert data["offset_chars"] == sum(map(len, windows))
        assert data["returned_chars"] == len(data["value_json"]) > 0
        windows.append(data["value_json"])
        if payload["next"] is None:
            assert not data["has_more"]
            assert sum(map(len, windows)) == data["total_chars"]
            return json.loads("".join(windows)), len(windows)
        current = payload["next"]
        assert current["name"].startswith(prefix)
    pytest.fail("Detail continuation did not finish")


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("prefix", ["", "tuoni_"])
async def test_large_inventories_list_all_records_then_recover_exact_redacted_details(monkeypatch, mode, prefix):
    mcp, c2, jobs, plugins, _ = _setup(monkeypatch, mode, prefix)
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        for tool, collection, arguments, identities in [
            ("tuoni_jobs_list", "jobs", {"limit": 1, "page_size": 2}, [1, 2, 3]),
            ("tuoni_listener_plugins_list", "plugins", {"limit": 1}, [f"fixture.plugin-{i}" for i in range(1, 5)]),
        ]:
            current = _initial(mode, tool, arguments)
            seen, details = [], []
            for _ in range(10):
                result, payload = await _call(session, current, prefix, LIST_RESULT_BUDGET)
                assert not result.isError, payload
                for item in payload["data"][collection]:
                    seen.append(item["job_id"] if collection == "jobs" else item["plugin_id"])
                    details.append(item["detail"])
                    assert not {"messages", "open_resources", "configuration_schema", "configuration_examples"} & item.keys()
                    if collection == "jobs":
                        assert item["message_count"] == (80 if item["job_id"] == 1 else 1)
                if payload["next"] is None:
                    break
                current = payload["next"]
            else:
                pytest.fail("Inventory did not finish")
            assert seen == identities
            for index in (0, 1):
                detail, pages = await _detail(session, details[index], prefix)
                if collection == "jobs":
                    assert detail == redact(serialize.job_to_dict(TuoniJob(jobs[index], c2)))
                    assert (pages > 1) == (index == 0)
                else:
                    original = TuoniListenerPlugin(plugins[index], c2)
                    expected = redact(serialize.listener_plugin_to_dict(original))
                    expected["configuration_schema"] = redact_schema(original.conf_schema)
                    expected["configuration_schema_redacted"] = True
                    expected["configuration_schema_notice"] = (
                        "Secret literals were removed; this schema may omit constraints. "
                        "Do not treat it as complete validation."
                    )
                    assert detail == expected
                    assert detail["configuration_schema"]["properties"]["password"] == {"type": "string", "minLength": 8}
                    assert (pages > 1) == (index == 1)
                assert "example-secret" not in json.dumps(detail)
                assert "schema-secret" not in json.dumps(detail)
                assert "resource-secret" not in json.dumps(detail)


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_changed_detail_rejects_mixed_windows_and_returns_exact_restart(monkeypatch, mode):
    mcp, _, jobs, _, _ = _setup(monkeypatch, mode, "tuoni_")
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        initial = _initial(mode, "tuoni_job_get", {"job_id": 1, "max_chars": 200})
        _, first = await _call(session, initial, "tuoni_", DETAIL_RESULT_BUDGET)
        assert first["next"]
        jobs[0]["messages"].append({"text": "new fixture event"})
        result, changed = await _call(session, first["next"], "tuoni_", DETAIL_RESULT_BUDGET)
        assert result.isError and changed["code"] == "DETAIL_CHANGED"
        assert changed["retryable"] is False
        result, restarted = await _call(session, changed["next"], "tuoni_", DETAIL_RESULT_BUDGET)
        assert not result.isError and restarted["data"]["offset_chars"] == 0


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_invalid_detail_scope_and_missing_records_are_actionable(monkeypatch, mode):
    mcp, _, _, _, requests = _setup(monkeypatch, mode)
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        _, first = await _call(session, _initial(mode, "tuoni_job_get", {"job_id": 1}), "", DETAIL_RESULT_BUDGET)
        cursor = first["next_cursor"]
        for name, arguments, code in [
            ("tuoni_job_get", {"job_id": 2, "cursor": cursor}, "CURSOR_SCOPE_CHANGED"),
            ("tuoni_job_get", {"job_id": 99}, "NOT_FOUND"),
            ("tuoni_listener_plugin_get", {"plugin_id": "missing"}, "NOT_FOUND"),
            ("tuoni_job_get", {"job_id": True}, "INVALID_ARGUMENTS"),
            ("tuoni_job_get", {"job_id": "1"}, "INVALID_ARGUMENTS"),
        ]:
            before = len(requests)
            result, payload = await _call(session, _initial(mode, name, arguments), "", DETAIL_RESULT_BUDGET)
            assert result.isError and payload["code"] == code
            if code == "INVALID_ARGUMENTS":
                assert len(requests) == before


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_detail_window_size_can_change_without_gaps_or_unbounded_results(monkeypatch, mode):
    mcp, c2, jobs, _, _ = _setup(monkeypatch, mode, "tuoni_")
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        current = _initial(mode, "tuoni_job_get", {"job_id": 1, "max_chars": 200})
        chunks = []
        for index in range(40):
            result, payload = await _call(session, current, "tuoni_", DETAIL_RESULT_BUDGET)
            assert not result.isError, payload
            data = payload["data"]
            assert data["offset_chars"] == sum(map(len, chunks))
            chunks.append(data["value_json"])
            if payload["next"] is None:
                break
            current = payload["next"]
            arguments = current["arguments"]
            if mode is MCPMode.DISPATCH:
                arguments = arguments["arguments"]
            arguments["max_chars"] = (16_384, 200, 4096)[index % 3]
        else:
            pytest.fail("Resized detail windows did not finish")
    assert json.loads("".join(chunks)) == redact(serialize.job_to_dict(TuoniJob(jobs[0], c2)))


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_sensitive_schema_names_do_not_hide_actual_wire_size(monkeypatch, mode):
    mcp, _, _, plugins, _ = _setup(monkeypatch, mode, "tuoni_")
    secret_schema = plugins[0]["configurationSchema"]["properties"]["password"]
    secret_schema.update(description="x" * 12_000, enum=["enum-secret"], const="const-secret")
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        value, pages = await _detail(
            session, _initial(mode, "tuoni_listener_plugin_get", {"plugin_id": "fixture.plugin-1"}), "tuoni_",
        )
    assert pages > 1
    assert value["configuration_schema"]["properties"]["password"]["description"] == "x" * 12_000
    assert value["configuration_schema_redacted"] is True
    assert "omit constraints" in value["configuration_schema_notice"]
    assert not {"default", "enum", "const"} & value["configuration_schema"]["properties"]["password"].keys()
    assert "enum-secret" not in json.dumps(value)
    assert "const-secret" not in json.dumps(value)


def test_sensitive_schema_references_redact_shared_nested_and_cyclic_definitions():
    schema = {
        "type": "object", "properties": {
            "normal": {"$ref": "#/$defs/Value"},
            "password": {"$ref": "#/$defs/Wrapper"},
        },
        "$defs": {
            "Wrapper": {"allOf": [{"$ref": "#/$defs/Value"}], "default": "wrapper-secret"},
            "Value": {"type": "string", "default": "value-secret", "enum": ["enum-secret"],
                      "allOf": [{"$ref": "#/$defs/Wrapper"}]},
            "Public": {"type": "integer", "default": 42},
        },
    }
    clean = redact_schema(schema)
    assert "secret" not in json.dumps(clean)
    assert clean["properties"] == schema["properties"]
    assert clean["$defs"]["Value"]["type"] == "string"
    assert clean["$defs"]["Public"]["default"] == 42
    assert clean["$defs"]["Value"]["allOf"] == schema["$defs"]["Value"]["allOf"]


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("exact_id", ["fixture." + "界" * 180, "fixture:/+opaque plugin"])
async def test_plugin_identity_and_detail_arguments_are_never_altered(monkeypatch, mode, exact_id):
    mcp, _, _, plugins, _ = _setup(monkeypatch, mode, "tuoni_")
    # The Unicode fixture is below the input character cap but above the detail
    # 512-byte cap; the opaque fixture must not inherit an invented ID grammar.
    plugins[0]["identifier"]["id"] = exact_id
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        result, payload = await _call(
            session, _initial(mode, "tuoni_listener_plugins_list", {"limit": 1}), "tuoni_", LIST_RESULT_BUDGET,
        )
        assert not result.isError
        first = payload["data"]["plugins"][0]
        assert first["plugin_id"] == exact_id
        detail, _ = await _detail(session, first["detail"], "tuoni_")
        assert detail["plugin_id"] == exact_id


@pytest.mark.parametrize("mode", list(MCPMode))
async def test_unfittable_exact_plugin_record_fails_explicitly_without_altered_ids(monkeypatch, mode):
    mcp, _, _, plugins, _ = _setup(monkeypatch, mode, "tuoni_")
    plugins[0]["identifier"]["id"] = "𐐀" * 512  # Four-byte Unicode letters exceed the wire budget when repeated in detail.
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        result, payload = await _call(
            session, _initial(mode, "tuoni_listener_plugins_list", {"limit": 1}), "tuoni_", LIST_RESULT_BUDGET,
        )
    assert result.isError and payload["code"] == "RESPONSE_TOO_LARGE"
    assert not payload.get("next")


@pytest.mark.parametrize("invalid", ["token=secret", " ", "x" * 513, 12])
async def test_plugin_summary_validates_identity_before_preserving_it(monkeypatch, invalid):
    mcp, _, _, plugins, _ = _setup(monkeypatch, MCPMode.NATIVE)
    plugins[0]["identifier"]["id"] = invalid
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        result, payload = await _call(
            session, _initial(MCPMode.NATIVE, "tuoni_listener_plugins_list", {}), "", LIST_RESULT_BUDGET,
        )
    assert result.isError and payload["code"] == "INVALID_UPSTREAM_ID"
