from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry


@pytest.mark.parametrize("mode", [MCPMode.NATIVE, MCPMode.DISPATCH])
async def test_discovery_continues_through_all_upstream_pages(fake_c2, mode):
    calls = []

    def hosts(page, page_size, filter):
        calls.append((page, page_size, filter))
        return [SimpleNamespace(id=str(index), address=str(index), name="host", note="", status="ACTIVE")
                for index in range(page * page_size, min(5, (page + 1) * page_size))]

    fake_c2.load_datamodel_hosts = hosts
    cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                   profiles=frozenset({Profile.DISCOVERY}), mode=mode)
    registry = build_registry(cfg)
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = MagicMock()
    context.request_context.lifespan_context = server
    invocation = InvocationContext(context=context, server=server)
    specs = {spec.name: spec for spec in (
        registry.select(cfg.profiles, read_only=False) if mode is MCPMode.NATIVE else
        build_dispatch_specs(registry, registry.select(cfg.profiles, read_only=False), read_only=False)
    )}
    name, arguments = "tuoni_hosts_list", {"page_size": 2, "limit": 1, "filter": "status=ACTIVE"}
    if mode is MCPMode.DISPATCH:
        name, arguments = "tuoni_call", {"name": name, "arguments": arguments}
    ids = []
    for _ in range(10):
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        assert not result.isError, result
        payload = json.loads(result.content[0].text)
        ids.extend(item["id"] for item in payload["data"]["hosts"])
        if payload["next"] is None:
            break
        name, arguments = payload["next"]["name"], payload["next"]["arguments"]
    assert ids == ["0", "1", "2", "3", "4"]
    assert {page for page, _, _ in calls} == {0, 1, 2}
    assert all(size == 2 and filter == "status=ACTIVE" for _, size, filter in calls)
