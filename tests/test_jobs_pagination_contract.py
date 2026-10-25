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


@pytest.mark.parametrize("mode", [MCPMode.NATIVE, MCPMode.DISPATCH])
@pytest.mark.parametrize("count", [19, 20, 21])
@pytest.mark.parametrize("include_inactive", [False, True])
async def test_job_pages_follow_real_sdk_endpoint_contract(mode, count, include_inactive):
    """The real SDK ignores page inputs on /active but honors them on /all."""
    records = [
        {
            "id": index,
            "name": f"job-{index}",
            "status": "FINISHED" if include_inactive and index % 2 else "RUNNING",
            "source": {},
            "supportedActions": [],
            "openResources": [],
            "messages": [],
        }
        for index in range(count)
    ]
    calls = []

    def request_get(uri):
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        calls.append((parsed.path, query))
        assert query["sort"] == ["id:asc"]
        if parsed.path == "/api/v1/jobs/active":
            assert "page" not in query and "pageSize" not in query
            return records
        assert parsed.path == "/api/v1/jobs/all"
        page, size = int(query["page"][0]), int(query["pageSize"][0])
        return {"items": records[page * size:(page + 1) * size]}

    c2 = TuoniC2(verify=True)
    c2.request_get = request_get
    cfg = Settings(
        url="https://example.test", username="test", password="test", verify=True,
        profiles=frozenset({Profile.ADMINISTRATION}), mode=mode, read_only=True,
    )
    registry = build_registry(cfg)
    server = ServerContext(c2=c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(cfg.profiles, read_only=True)
    specs = {spec.name: spec for spec in (
        selected if mode is MCPMode.NATIVE else build_dispatch_specs(registry, selected, read_only=True)
    )}
    name = "tuoni_jobs_list"
    arguments = {"include_inactive": include_inactive, "page_size": 20, "limit": 7}
    if mode is MCPMode.DISPATCH:
        name, arguments = "tuoni_call", {"name": name, "arguments": arguments}

    ids = []
    statuses = []
    for _ in range(10):
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        assert not result.isError, result
        assert call_result_size(result) <= LIST_RESULT_BUDGET
        payload = json.loads(result.content[0].text)
        coverage = payload["data"]["coverage"]
        assert coverage["returned"] == len(payload["data"]["jobs"])
        assert coverage["has_more"] == (payload["next"] is not None)
        assert coverage["complete"] == (coverage["offset"] == 0 and not coverage["has_more"])
        if include_inactive and payload["next"] is not None and count >= 20:
            assert coverage["total"] is None
        ids.extend(job["job_id"] for job in payload["data"]["jobs"])
        statuses.extend(job["status"] for job in payload["data"]["jobs"])
        if payload["next"] is None:
            break
        name, arguments = payload["next"]["name"], payload["next"]["arguments"]
    else:
        pytest.fail("Job continuation did not terminate within ten bounded pages")

    assert ids == list(range(count))
    assert len(ids) == len(set(ids))
    assert statuses == [record["status"] for record in records]
    expected_path = "/api/v1/jobs/all" if include_inactive else "/api/v1/jobs/active"
    assert {path for path, _ in calls} == {expected_path}
    # Every local output window still fetches upstream. A full final page needs
    # one empty probe because the SDK does not preserve page metadata.
    assert len(calls) == (3 if count == 19 else 4)
    assert len(payload["data"]["jobs"]) == {19: 5, 20: 0, 21: 1}[count]
    if include_inactive:
        expected_pages = {0} if count < 20 else {0, 1}
        assert {int(query["page"][0]) for _, query in calls} == expected_pages
