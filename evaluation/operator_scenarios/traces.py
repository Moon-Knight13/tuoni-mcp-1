"""Read coding-client wire traces without consulting private reasoning text."""
from __future__ import annotations

import json
from pathlib import Path

from mcp.types import CallToolResult

from tuoni_mcp.protocol import call_result_size, redact_text


def canonical(name: str) -> str:
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    if name.startswith("tuoni_tuoni_"):
        name = name.removeprefix("tuoni_")
    return name


def load_session(folder: Path) -> dict:
    run = json.loads((folder / "run.json").read_text())
    calls, pending, models, limits, client_errors = [], {}, set(), [], []
    path = folder / "mcp.jsonl"
    for line in path.read_text().splitlines() if path.exists() else []:
        row = json.loads(line)
        message = row["message"]
        if row["direction"] == "request" and message.get("method") == "tools/call":
            params = message["params"]
            pending[message["id"]] = (row["time"], params)
            metadata = params.get("_meta") or {}
            for value in metadata.values() if isinstance(metadata, dict) else ():
                if isinstance(value, dict) and isinstance(value.get("model"), str):
                    models.add(value["model"])
        elif row["direction"] == "response" and message.get("id") in pending:
            start, params = pending.pop(message["id"])
            name, arguments = canonical(params["name"]), params.get("arguments", {})
            for _ in range(3):
                if name != "tuoni_call" or not isinstance(arguments, dict) or not isinstance(arguments.get("name"), str):
                    break
                name, arguments = canonical(arguments["name"]), arguments.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            result = message.get("result", {})
            try:
                payload = json.loads(result["content"][0]["text"])
                if not isinstance(payload, dict):
                    raise ValueError("tool payload is not an object")
                size = call_result_size(CallToolResult.model_validate(result))
            except (KeyError, IndexError, ValueError):
                payload, size = {"code": "INVALID_TOOL_RESPONSE", "ok": False}, 0
            calls.append({"name": name, "arguments": arguments, "payload": payload,
                          "wire_name": params["name"], "wire_arguments": params.get("arguments", {}),
                          "result_bytes": size, "response_seconds": row["time"] - start})
    path = folder / "events.jsonl"
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if run["provider"] == "claude" and event.get("type") == "assistant":
            model = event.get("message", {}).get("model")
            if model:
                models.add(model)
        if event.get("type") == "result" and event.get("is_error"):
            client_errors.append({"subtype": event.get("subtype"),
                                  "message": redact_text(str(event.get("result", "")))[:1000]})
        if event.get("type") == "step_finish" and event.get("part", {}).get("reason") == "length":
            limits.append({"reason": "length", "tokens": event["part"].get("tokens", {})})
    if run["provider"] == "opencode":
        path = folder / "session-export.json"
        exported = json.loads(path.read_text()) if path.exists() else {}
        models = {m["info"].get("providerID", "") + "/" + m["info"].get("modelID", "")
                  for m in exported.get("messages", []) if m.get("info", {}).get("role") == "assistant"}
    path = folder / "final.txt"
    final = path.read_text() if path.exists() else ""
    auth_failure = any("not logged in" in error["message"].casefold() for error in client_errors)
    return {**run, "calls": calls, "final": final, "observed_models": sorted(models),
            "client_errors": client_errors, "infrastructure_failure": "client_auth" if auth_failure else None,
            "generation_limits": limits, "unanswered_calls": len(pending),
            "model_identity": models == {run["requested_model"]}}
