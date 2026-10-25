"""Grade captured operator sessions; keep the generated report outside Git."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from evaluation.operator_scenarios.grading import grade
from evaluation.operator_scenarios.traces import load_session


def grade_session(folder: Path) -> dict:
    session = load_session(folder)
    world = json.loads((folder / "world.json").read_text())
    checks = grade(session["case"], world, session["calls"], session["final"])
    contract = {
        "runtime": session["exit_code"] == 0 and not session["failure"],
        "model_identity": session["model_identity"],
        "call_budget": len(session["calls"]) <= 120,
        "bounded_results": all(c["result_bytes"] <= 8192 for c in session["calls"]),
        "all_calls_returned": session["unanswered_calls"] == 0,
    }
    return {**session, **checks, "fixture_revision": world.get("fixture_revision", 1), "contract_checks": contract,
            "passed": checks["passed"] and all(contract.values()),
            "errors": [c for c in session["calls"] if not c["payload"].get("ok")]}


def summarize(rows: list[dict]) -> dict:
    valid = [r for r in rows if not r["infrastructure_failure"]]
    return {"sessions": len(rows), "passed": sum(r["passed"] for r in rows),
            "infrastructure_failures": sum(bool(r["infrastructure_failure"]) for r in rows),
            "valid_sessions": sum(not r["infrastructure_failure"] for r in rows),
            "state_passed": sum(r["state_passed"] for r in valid),
            "retrieval_passed": sum(r["retrieval_passed"] for r in valid),
            "claims_passed": sum(r["claims_passed"] for r in valid),
            "tool_calls": sum(len(r["calls"]) for r in rows),
            "errors": dict(collections.Counter(c["payload"].get("code") for r in rows for c in r["errors"]))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows = [grade_session(p.parent) for p in sorted(args.folder.glob("*/*/run.json"))]
    summary = {**summarize(rows), "models": {
        name: summarize([r for r in rows if r["requested_model"] == name])
        for name in sorted({r["requested_model"] for r in rows})}}
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
