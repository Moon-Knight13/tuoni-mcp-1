"""Validate and summarize deterministic native/dispatch workflow results."""

from __future__ import annotations

from typing import Any


MODES = ("native", "dispatch")
WORKFLOW_IDS = tuple(f"W{number:02d}" for number in range(1, 21))
CATALOG_TOOLS = {"native": 11, "dispatch": 4}
COUNTERS = (
    "tool_calls", "valid_tool_calls", "context_overflows", "unintended_mutations",
    "result_bytes", "max_result_bytes", "request_bytes", "upstream_reads",
)


class ScoringError(ValueError):
    """A workflow recording is incomplete or internally inconsistent."""


def _integer(value: Any, location: str) -> int:
    if type(value) is not int or value < 0:
        raise ScoringError(f"{location} must be a non-negative integer")
    return value


def score(run: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Score all twenty workflows in both modes, rejecting malformed counters."""

    if not isinstance(run, dict) or run.get("kind") != "portable":
        raise ScoringError("run.kind must be portable")
    for metric in ("catalog_tools", "catalog_bytes", "instructions_bytes"):
        values = run.get(metric)
        if not isinstance(values, dict):
            raise ScoringError(f"run.{metric} must be an object")
        for mode in MODES:
            value = _integer(values.get(mode), f"run.{metric}.{mode}")
            if metric == "catalog_tools" and value != CATALOG_TOOLS[mode]:
                raise ScoringError(f"run.catalog_tools.{mode} must be {CATALOG_TOOLS[mode]}")

    if not isinstance(results, list) or not all(isinstance(record, dict) for record in results):
        raise ScoringError("results must be an array of workflow records")
    indexed: dict[str, dict[str, dict[str, Any]]] = {mode: {} for mode in MODES}
    for position, record in enumerate(results):
        location = f"results[{position}]"
        mode, workflow_id = record.get("mode"), record.get("workflow_id")
        if mode not in MODES or workflow_id not in WORKFLOW_IDS:
            raise ScoringError(f"{location} has an unknown mode or workflow")
        if workflow_id in indexed[mode]:
            raise ScoringError(f"duplicate result for {mode}/{workflow_id}")
        if type(record.get("completed")) is not bool:
            raise ScoringError(f"{location}.completed must be a boolean")
        for counter in COUNTERS:
            _integer(record.get(counter), f"{location}.{counter}")
        if record["valid_tool_calls"] > record["tool_calls"]:
            raise ScoringError(f"{location}.valid_tool_calls cannot exceed tool_calls")
        if record["max_result_bytes"] > record["result_bytes"]:
            raise ScoringError(f"{location}.max_result_bytes cannot exceed result_bytes")
        indexed[mode][workflow_id] = record

    modes: dict[str, Any] = {}
    for mode in MODES:
        missing = sorted(set(WORKFLOW_IDS) - indexed[mode].keys())
        if missing:
            raise ScoringError(f"{mode} is missing workflows: {', '.join(missing)}")
        records = [indexed[mode][workflow_id] for workflow_id in WORKFLOW_IDS]
        completed = sum(record["completed"] for record in records)
        totals = {counter: sum(record[counter] for record in records) for counter in COUNTERS}
        totals["max_result_bytes"] = max(record["max_result_bytes"] for record in records)
        calls = totals["tool_calls"]
        checks = {
            "all_workflows_completed": completed == len(WORKFLOW_IDS),
            "all_calls_schema_valid": calls > 0 and totals["valid_tool_calls"] == calls,
            "zero_context_overflows": totals["context_overflows"] == 0,
            "zero_unintended_mutations": totals["unintended_mutations"] == 0,
            "bounded_results": totals["max_result_bytes"] <= 8192,
        }
        modes[mode] = {
            **totals,
            **{metric: run[metric][mode] for metric in ("catalog_tools", "catalog_bytes", "instructions_bytes")},
            "workflow_count": len(records),
            "completed_workflows": completed,
            "completion_rate": round(completed / len(records), 4),
            "incomplete_workflows": [record["workflow_id"] for record in records if not record["completed"]],
            "valid_tool_call_rate": round(totals["valid_tool_calls"] / calls, 4) if calls else 0.0,
            "mean_tool_calls_per_workflow": round(calls / len(records), 3),
            "mean_result_bytes_per_workflow": round(totals["result_bytes"] / len(records), 1),
            "checks": checks,
        }
    passed = {mode: all(result["checks"].values()) for mode, result in modes.items()}
    return {
        "schema_version": 1,
        "kind": "portable",
        "modes": modes,
        "portable_gate": {
            "passed": all(passed.values()),
            "mode_passed": passed,
            "note": "Scripted contract checks; these results do not measure model task performance.",
        },
    }
