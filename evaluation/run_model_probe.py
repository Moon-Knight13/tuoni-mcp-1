"""Probe any tool-calling model against isolated synthetic Tuoni workflows.

This contacts only the chosen model endpoint. Every MCP request uses PortableC2;
it cannot reach a live C2. Its three cases explore model task performance;
the deterministic suite and deployment checks cover separate behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.portable.runner import _connected  # noqa: E402
from evaluation.portable.world import (  # noqa: E402
    AGENT_GUID, AGENT_HINT, FIRST_CREATED_COMMAND_ID, IDENTITY_COMMAND_ID, IDENTITY_OUTPUT, PortableC2,
)
from tuoni_mcp.protocol import call_result_size  # noqa: E402


CASES = {
    "find_agent": f"Find the active agent matching {AGENT_HINT}. Report its exact GUID and hostname. Do not mutate anything.",
    "read_identity": f"Read command {IDENTITY_COMMAND_ID}'s identity result and report the account name. Do not mutate anything.",
    "execute_once": f"On agent {AGENT_GUID}, run cmd with shell text whoami exactly once and report the account name.",
}


def completed(case, final, calls, world):
    successful = [(name, arguments, payload) for name, arguments, payload in calls if payload.get("ok")]
    mutations = world.mutating_calls
    if case == "find_agent":
        return not mutations and AGENT_GUID in final and AGENT_HINT in final and any(
            name == "tuoni_agents_list" for name, _, _ in successful
        )
    expected_id = IDENTITY_COMMAND_ID if case == "read_identity" else FIRST_CREATED_COMMAND_ID
    read = any(
        name == "tuoni_command_get" and arguments.get("command_id") == expected_id
        and "part_index" in arguments
        and IDENTITY_OUTPUT.strip() in payload.get("data", {}).get("result", {}).get("part", {}).get("value", "")
        for name, arguments, payload in successful
    )
    reported = IDENTITY_OUTPUT.splitlines()[0].casefold() in final.casefold()
    if case == "read_identity":
        return not mutations and read and reported
    created = world.commands.get(FIRST_CREATED_COMMAND_ID, {})
    return (
        mutations == [("POST", f"/api/v1/agents/{AGENT_GUID}/commands")]
        and created.get("configuration") == {"command": "whoami"}
        and any(name == "tuoni_command_describe" for name, _, _ in successful)
        and read and reported
    )


async def probe(client, *, base_url, model, mode, case, max_steps):
    world = PortableC2()
    calls = []
    result_bytes = request_bytes = peak_prompt_tokens = 0
    final, failure = "", ""
    async with _connected(mode, world) as (server, session):
        catalog = await session.list_tools()
        tools = [{"type": "function", "function": {
            "name": tool.name, "description": tool.description, "parameters": tool.inputSchema
        }} for tool in catalog.tools]
        messages = [
            {"role": "system", "content": "All Tuoni tools operate on an isolated synthetic fixture.\n" + server.instructions},
            {"role": "user", "content": CASES[case]},
        ]
        for _ in range(max_steps):
            body = {"model": model, "messages": messages, "tools": tools, "temperature": 0, "max_tokens": 1024}
            request_bytes += len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            try:
                response = await client.post(base_url.rstrip("/") + "/chat/completions", json=body)
                response.raise_for_status()
                document = response.json()
                message = document["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise ValueError("Model message must be an object")
                usage = document.get("usage") or {}
                peak_prompt_tokens = max(peak_prompt_tokens, int(usage.get("prompt_tokens") or 0))
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                failure = f"Model request failed ({type(exc).__name__})"
                break
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final = str(message.get("content") or "")
                break
            if not isinstance(tool_calls, list) or len(tool_calls) > max_steps - len(calls):
                failure = "Model exceeded the bounded tool-call allowance"
                break
            # Retain only protocol fields accepted by subsequent chat requests.
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
            for tool_call in tool_calls:
                try:
                    function = tool_call["function"]
                    name = function["name"]
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    result = await session.call_tool(name, arguments)
                    payload = json.loads(result.content[0].text)
                    logical, actual = name, arguments
                    if name == "tuoni_call":
                        logical, actual = arguments.get("name"), arguments.get("arguments", {})
                    calls.append((logical, actual, payload))
                    result_bytes += call_result_size(result)
                    messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result.content[0].text})
                except (KeyError, TypeError, ValueError) as exc:
                    failure = f"Malformed tool call ({type(exc).__name__})"
                    break
            if failure or len(calls) >= max_steps:
                break
        else:
            failure = "Model reached the step limit without a final answer"
    return {
        "case": case, "mode": mode, "completed": not failure and completed(case, final, calls, world),
        "tool_calls": len(calls), "tool_errors": sum(not payload.get("ok") for _, _, payload in calls),
        "request_bytes": request_bytes, "result_bytes": result_bytes,
        "peak_prompt_tokens": peak_prompt_tokens or None, "failure": failure,
        "final": final, "calls": calls,
    }


async def run(args):
    headers = {}
    if os.environ.get("MODEL_EVAL_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["MODEL_EVAL_API_KEY"]
    records = []
    async with httpx.AsyncClient(timeout=args.timeout, headers=headers) as client:
        for mode in ([args.mode] if args.mode != "both" else ["native", "dispatch"]):
            for case in CASES:
                record = await probe(client, base_url=args.base_url, model=args.model, mode=mode,
                                     case=case, max_steps=args.max_steps)
                records.append(record)
                print(f"{mode}/{case}: {'passed' if record['completed'] else 'failed'}; {record['tool_calls']} calls", file=sys.stderr)
    return {"kind": "synthetic-model-probe", "model": args.model, "results": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible API URL ending in /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=["native", "dispatch", "both"], default="both")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        parser.error("--base-url must be an HTTP(S) API URL without credentials, query or fragment")
    if not 1 <= args.max_steps <= 100 or not 0 < args.timeout <= 300:
        parser.error("--max-steps must be 1-100 and --timeout must be 0-300 seconds")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    report = asyncio.run(run(args))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(record["completed"] for record in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
