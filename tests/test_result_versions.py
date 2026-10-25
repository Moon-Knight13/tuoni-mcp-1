from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from tuoni_mcp.config import MCPMode, Settings
from tuoni_mcp.connection import ServerContext
from tuoni_mcp.registry import InvocationContext, build_dispatch_specs
from tuoni_mcp.tools import build_registry


@pytest.fixture(params=[(mode, prefix) for mode in MCPMode for prefix in ("", "client_")])
def harness(request, fake_c2):
    mode, prefix = request.param
    cfg = Settings(url="https://example.test", username="fixture", password="fixture", verify=True,
                   mode=mode, tool_prefix=prefix)
    registry = build_registry(cfg)
    server = ServerContext(c2=fake_c2, cfg=cfg, last_login=time.monotonic())
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=server))
    invocation = InvocationContext(context=context, server=server)
    selected = registry.select(cfg.profiles, read_only=False)
    specs = {spec.name: spec for spec in (selected if mode is MCPMode.NATIVE else
                                        build_dispatch_specs(registry, selected, read_only=False))}

    async def call(arguments=None, *, continuation=None):
        if continuation is not None:
            name = continuation["name"].removeprefix(prefix)
            arguments = continuation["arguments"]
        elif mode is MCPMode.DISPATCH:
            name, arguments = "tuoni_call", {"name": "tuoni_command_get", "arguments": arguments}
        else:
            name = "tuoni_command_get"
        result = await registry.invoke_spec(specs[name], arguments, invocation)
        return result, json.loads(result.content[0].text)

    return call, server


def command(text, *, status="ONGOING", name="STDOUT"):
    return {"id": 42, "status": status,
            "result": {"status": "success", "childResults": [{"type": "text", "name": name, "value": text}]}}


async def test_manifest_version_is_carried_by_each_callable_text_continuation(harness, fake_c2):
    call, _ = harness
    text = "Zażółć 日本語 🚀\n" * 900
    fake_c2.responses["/api/v1/commands/42"] = command(text)
    _, manifest = await call({"command_id": 42})
    version = manifest["data"]["result"]["parts"][0]["result_version"]
    _, first = await call({"command_id": 42, "part_index": 0, "max_chars": 1024, "result_version": version})
    assert first["next"] is not None
    complete = first["data"]["result"]["part"]["value"]
    page = first
    while page["next"] is not None and page["data"]["result"]["part"]["has_more"]:
        result, page = await call(continuation=page["next"])
        assert not result.isError
        assert page["data"]["result"]["part"]["result_version"] == version
        complete += page["data"]["result"]["part"]["value"]
    assert complete == text


async def test_changed_ongoing_output_rejects_continuation_and_offers_manifest_restart(harness, fake_c2):
    call, _ = harness
    fake_c2.responses["/api/v1/commands/42"] = command("old value " * 20)
    _, first = await call({"command_id": 42, "part_index": 0, "max_chars": 8})
    fake_c2.responses["/api/v1/commands/42"] = command("new value " * 20)

    result, changed = await call(continuation=first["next"])
    assert result.isError
    assert changed["code"] == "RESULT_CHANGED"
    restarted, manifest = await call(continuation=changed["next"])
    assert not restarted.isError
    assert manifest["data"]["command_id"] == 42
    assert manifest["data"]["result"]["parts"][0]["result_version"] != first["data"]["result"]["part"]["result_version"]


async def test_cache_expiry_cannot_mix_completed_result_versions(harness, fake_c2):
    from tuoni_mcp.result_cache import cache_for

    call, server = harness
    now = [0.0]
    # Replace the lifespan-owned cache using its actual storage attribute.
    original = cache_for(server)
    original._clock = lambda: now[0]
    fake_c2.responses["/api/v1/commands/42"] = command("first " * 10, status="COMPLETE")
    _, first = await call({"command_id": 42, "part_index": 0, "max_chars": 5})
    fake_c2.responses["/api/v1/commands/42"] = command("second " * 10, status="COMPLETE")
    before_expiry, _ = await call(continuation=first["next"])
    assert not before_expiry.isError
    now[0] = original.ttl_seconds + 1
    after_expiry, changed = await call(continuation=first["next"])
    assert after_expiry.isError
    assert changed["code"] == "RESULT_CHANGED"


async def test_result_version_requires_a_text_part(harness, fake_c2):
    call, _ = harness
    fake_c2.responses["/api/v1/commands/42"] = command("output")
    result, error = await call({"command_id": 42, "result_version": "a" * 64})
    assert result.isError and error["code"] == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("missing", [None, {"childResults": []}, {"childResults": [None]}])
async def test_disappearing_result_rejects_its_old_text_version(harness, fake_c2, missing):
    call, _ = harness
    fake_c2.responses["/api/v1/commands/42"] = command("old value " * 20)
    _, first = await call({"command_id": 42, "part_index": 0, "max_chars": 8})
    fake_c2.responses["/api/v1/commands/42"] = {"id": 42, "status": "ONGOING", "result": missing}
    result, changed = await call(continuation=first["next"])
    assert result.isError and changed["code"] == "RESULT_CHANGED"


async def test_snapshot_version_covers_visible_text_without_secret_literals(harness, fake_c2):
    call, _ = harness
    fake_c2.responses["/api/v1/commands/42"] = command("prefix password=secret-one suffix")
    _, first = await call({"command_id": 42, "part_index": 0, "max_chars": 7})
    fake_c2.responses["/api/v1/commands/42"] = command("prefix password=secret-two suffix")
    result, second = await call(continuation=first["next"])
    assert not result.isError
    assert second["data"]["result"]["part"]["result_version"] == first["data"]["result"]["part"]["result_version"]
    assert "secret-one" not in json.dumps(first)
    assert "secret-two" not in json.dumps(second)
