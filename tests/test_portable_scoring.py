"""Portable scoring rejects malformed recordings and fails incomplete contracts."""

from __future__ import annotations

import copy

import pytest

from evaluation.portable.scoring import CATALOG_TOOLS, COUNTERS, MODES, WORKFLOW_IDS, ScoringError, score


@pytest.fixture
def recording():
    run = {
        "kind": "portable",
        "catalog_tools": dict(CATALOG_TOOLS),
        "catalog_bytes": dict.fromkeys(MODES, 100),
        "instructions_bytes": dict.fromkeys(MODES, 100),
    }
    results = [{
        "mode": mode, "workflow_id": workflow_id, "completed": True,
        **dict.fromkeys(COUNTERS, 0),
        "tool_calls": 2, "valid_tool_calls": 2, "result_bytes": 100, "max_result_bytes": 50,
    } for mode in MODES for workflow_id in WORKFLOW_IDS]
    return run, results


def test_passing_records_report_both_complete_modes(recording):
    report = score(*recording)
    assert report["portable_gate"]["passed"]
    assert tuple(report["modes"]) == MODES
    for mode in MODES:
        assert report["modes"][mode]["completed_workflows"] == 20
        assert report["modes"][mode]["tool_calls"] == 40


@pytest.mark.parametrize("change", [
    {"completed": False}, {"valid_tool_calls": 1}, {"context_overflows": 1},
    {"unintended_mutations": 1}, {"result_bytes": 9000, "max_result_bytes": 9000},
])
def test_any_incomplete_invalid_oversized_or_unsafe_workflow_fails(recording, change):
    run, results = recording
    results[0].update(change)
    report = score(run, results)
    assert not report["portable_gate"]["passed"]
    assert report["portable_gate"]["mode_passed"] == {"native": False, "dispatch": True}


@pytest.mark.parametrize("value", [-1, True, 0.5, "1", None])
def test_counters_require_nonnegative_json_integers(recording, value):
    run, results = recording
    results[0]["tool_calls"] = value
    with pytest.raises(ScoringError, match="non-negative integer"):
        score(run, results)


@pytest.mark.parametrize("change", [
    {"completed": 1}, {"valid_tool_calls": 3}, {"max_result_bytes": 101},
    {"mode": "unknown"}, {"workflow_id": "W99"},
])
def test_malformed_records_are_rejected(recording, change):
    run, results = recording
    results[0].update(change)
    with pytest.raises(ScoringError):
        score(run, results)


def test_missing_and_duplicate_workflows_are_rejected(recording):
    run, results = recording
    missing = copy.deepcopy(results)
    missing.pop()
    with pytest.raises(ScoringError, match="missing workflows"):
        score(run, missing)
    with pytest.raises(ScoringError, match="duplicate result"):
        score(run, [*results, results[0]])


def test_catalog_identity_and_metrics_are_validated(recording):
    run, results = recording
    run["catalog_tools"]["native"] = 12
    with pytest.raises(ScoringError, match="must be 11"):
        score(run, results)
    run["catalog_tools"]["native"] = 11
    run["catalog_bytes"]["native"] = True
    with pytest.raises(ScoringError, match="non-negative integer"):
        score(run, results)
