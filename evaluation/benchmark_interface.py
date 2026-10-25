"""Measure context bytes and warm-path work with synthetic Tuoni data only.

Run with the project's Python. Timing is informational; deterministic catalog
and upstream-read counts are gated separately by the portable evaluation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.portable.runner import _connected  # noqa: E402
from evaluation.portable.world import LARGE_RESULT_COMMAND_ID, PortableC2  # noqa: E402
from tuoni_mcp.config import ALL_EXPANSION, MCPMode, Settings  # noqa: E402
from tuoni_mcp.server import SERVER_INSTRUCTIONS, build_server  # noqa: E402
from tuoni_mcp.template_cache import TemplateCache  # noqa: E402


async def benchmark():
    logging.getLogger("mcp").setLevel(logging.WARNING)
    catalogs = {}
    for label, mode, profiles, read_only in (
        ("native", MCPMode.NATIVE, None, False),
        ("native-read-only", MCPMode.NATIVE, None, True),
        ("dispatch", MCPMode.DISPATCH, None, False),
        ("dispatch-read-only", MCPMode.DISPATCH, None, True),
        ("native-all", MCPMode.NATIVE, ALL_EXPANSION, False),
        ("dispatch-all", MCPMode.DISPATCH, ALL_EXPANSION, False),
    ):
        cfg = Settings(url="https://example.test", username="test", password="test", verify=True,
                       mode=mode, read_only=read_only, **({"profiles": profiles} if profiles else {}))
        server = build_server(cfg)
        from mcp.types import ListToolsResult
        tools = await server.list_tools()
        catalogs[label] = {
            "tools": len(tools),
            "bytes": len(ListToolsResult(tools=tools).model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")),
            "instructions_bytes": len(server.instructions.encode("utf-8")),
        }

    schema = server.registry.get("tuoni_command_prepare")
    schema.input_schema  # Warm the schema just as a native tools/list does.
    started = time.perf_counter()
    for _ in range(100):
        schema.input_schema
    schema_ms = (time.perf_counter() - started) * 1000

    templates = [{"id": str(index), "name": f"command-{index}", "configurationSchema": {
        "properties": {f"field-{field}": {"type": "string", "description": "x" * 100} for field in range(50)}
    }} for index in range(250)]

    async def fetch(path):
        return templates if path.endswith("command-templates") else {
            "availableCommandTemplates": [str(index) for index in range(250)]
        }

    cache = TemplateCache()
    await cache.exact_template(fetch, "agent", "command-42")
    started = time.perf_counter()
    for _ in range(100):
        _, matches = await cache.exact_template(fetch, "agent", "command-42")
        assert len(matches) == 1
    lookup_ms = (time.perf_counter() - started) * 1000

    world = PortableC2()
    world.commands[LARGE_RESULT_COMMAND_ID]["result"]["childResults"] = [
        {"type": "text", "name": "stdout", "value": "safe result line\n" * 65536}
    ]
    started = time.perf_counter()
    async with _connected("native", world) as (_, session):
        arguments = {"command_id": LARGE_RESULT_COMMAND_ID, "part_index": 0, "max_chars": 1024}
        for _ in range(40):
            result = await session.call_tool("tuoni_command_get", arguments)
            assert not result.isError, result
            payload = json.loads(result.content[0].text)
            assert payload["data"]["result"]["part"]["returned_chars"] == 1024
            arguments = payload["next"]["arguments"]
    windows_ms = (time.perf_counter() - started) * 1000
    skill = ROOT / ".agents/skills/operate-tuoni/SKILL.md"
    return {
        "catalogs": catalogs,
        "instructions_bytes": len(SERVER_INSTRUCTIONS.encode("utf-8")),
        "skill_bytes": skill.stat().st_size,
        "schema_reads_100_ms": round(schema_ms, 2),
        "exact_lookups_100_ms": round(lookup_ms, 2),
        "result_windows_40_ms": round(windows_ms, 2),
        "result_windows_40_upstream_reads": len(world.calls),
        "note": "Synthetic data; wall-clock timing is informational, not a real-model evaluation.",
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(benchmark()), indent=2, sort_keys=True))
