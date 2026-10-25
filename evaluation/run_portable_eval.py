#!/usr/bin/env python3
"""Run the portable evaluation and report or refresh its baseline.

    python evaluation/run_portable_eval.py                 # score and print
    python evaluation/run_portable_eval.py --out run.json  # keep the records
    python evaluation/run_portable_eval.py --update-baseline

The baseline is a committed snapshot of the metrics this interface currently
achieves. CI compares against it, so a change that makes the interface worse for
a small model fails, and a change that makes it better is recorded deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "evaluation" / "portable" / "baseline.json"

from evaluation.portable.runner import run_portable_evaluation  # noqa: E402
from evaluation.portable.scoring import score  # noqa: E402


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Reduce a scored report to the committed baseline shape."""

    return {
        "modes": {
            mode: {
                "catalog_tools": scored["catalog_tools"],
                "completed_workflows": scored["completed_workflows"],
                "incomplete_workflows": scored["incomplete_workflows"],
                "tool_calls": scored["tool_calls"],
                "valid_tool_call_rate": scored["valid_tool_call_rate"],
                "mean_tool_calls_per_workflow": scored["mean_tool_calls_per_workflow"],
                "result_bytes": scored["result_bytes"],
                "max_result_bytes": scored["max_result_bytes"],
                "context_overflows": scored["context_overflows"],
                "unintended_mutations": scored["unintended_mutations"],
                **{metric: scored[metric] for metric in (
                    "catalog_bytes", "instructions_bytes", "request_bytes", "upstream_reads"
                ) if metric in scored},
            }
            for mode, scored in report["modes"].items()
        }
    }


def evaluate() -> tuple[dict[str, Any], dict[str, Any]]:
    document = asyncio.run(run_portable_evaluation())
    report = score(document["run"], document["results"])
    return document, report


def load_baseline() -> dict[str, Any] | None:
    if not BASELINE_PATH.exists():
        return None
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def compare(summary: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Return the regressions of `summary` against `baseline`, if any."""

    regressions: list[str] = []
    for mode, expected in baseline["modes"].items():
        observed = summary["modes"].get(mode)
        if observed is None:
            regressions.append(f"{mode}: mode is missing from this run")
            continue
        if observed["completed_workflows"] < expected["completed_workflows"]:
            regressions.append(
                f"{mode}: completed {observed['completed_workflows']} workflows, "
                f"baseline is {expected['completed_workflows']}"
            )
        newly_failing = sorted(
            set(observed["incomplete_workflows"]) - set(expected["incomplete_workflows"])
        )
        if newly_failing:
            regressions.append(f"{mode}: newly failing workflows {', '.join(newly_failing)}")
        if observed["valid_tool_call_rate"] < expected["valid_tool_call_rate"]:
            regressions.append(
                f"{mode}: valid tool-call rate fell to {observed['valid_tool_call_rate']} "
                f"from {expected['valid_tool_call_rate']}"
            )
        if observed["tool_calls"] > expected["tool_calls"]:
            regressions.append(
                f"{mode}: {observed['tool_calls']} hops, baseline is {expected['tool_calls']} "
                "(a longer chain is harder for a small model)"
            )
        if observed["result_bytes"] > expected["result_bytes"]:
            regressions.append(
                f"{mode}: returned {observed['result_bytes']} bytes, baseline is {expected['result_bytes']}"
            )
        for counter in ("context_overflows", "unintended_mutations"):
            if observed[counter] > expected[counter]:
                regressions.append(f"{mode}: {counter} rose to {observed[counter]}")
        for metric in ("catalog_bytes", "instructions_bytes", "request_bytes", "upstream_reads"):
            if metric in expected and (metric not in observed or observed[metric] > expected[metric]):
                regressions.append(f"{mode}: {metric} exceeds the baseline ({expected[metric]}) or is missing")
    return regressions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the portable Tuoni MCP evaluation.")
    parser.add_argument("--out", type=Path, help="write the raw 40-record document here")
    parser.add_argument("--report", type=Path, help="write the scored report here")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="overwrite the committed baseline with this run's metrics",
    )
    args = parser.parse_args(argv)

    document, report = evaluate()
    summary = summarize(report)

    if args.out:
        args.out.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if args.update_baseline:
        if any(
            scored["completed_workflows"] != 20 or scored["valid_tool_call_rate"] != 1.0
            or scored["context_overflows"] or scored["unintended_mutations"]
            or scored["max_result_bytes"] > 8192
            for scored in summary["modes"].values()
        ):
            print("Refusing to record a baseline with failed workflows, invalid calls or unsafe results.", file=sys.stderr)
            return 1
        BASELINE_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"\nbaseline written to {BASELINE_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 0

    print(json.dumps(summary, indent=2, sort_keys=True))
    if not report["portable_gate"]["passed"]:
        print("\nportable contract checks failed", file=sys.stderr)
        return 1
    baseline = load_baseline()
    if baseline is None:
        print("\nno baseline recorded yet; rerun with --update-baseline", file=sys.stderr)
        return 0
    regressions = compare(summary, baseline)
    if regressions:
        print("\nregressions against the committed baseline:", file=sys.stderr)
        for item in regressions:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("\nno regression against the committed baseline", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
