"""Exercise each operational fault through the real native/dispatch MCP boundary."""
from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from evaluation.extended.world import SyntheticBridge
from evaluation.operator_scenarios.cases import CASES
from evaluation.operator_scenarios.grading import evidence_value, grade, read_complete, status_fact
from evaluation.operator_scenarios.world import HOSTED_PATH, LINUX, REPLACEMENT, UPLOAD, WINDOWS, OperatorC2
from tuoni_mcp.config import MCPMode, Profile
from tuoni_mcp.protocol import MAX_RESULT_BUDGET, call_result_size
from tuoni_mcp.registry import Effect


@pytest.mark.parametrize("mode", list(MCPMode))
@pytest.mark.parametrize("case", list(CASES))
async def test_operator_faults_are_recoverable_without_unintended_mutations(patched_server, monkeypatch, cfg, tmp_path, mode, case):
    from tuoni_mcp import server

    world = OperatorC2(case)
    monkeypatch.setattr(server, "TuoniC2", lambda **kwargs: world)
    monkeypatch.setattr(server, "EventBridge", SyntheticBridge)
    mcp = server.build_server(replace(cfg, mode=mode,
        profiles=frozenset(Profile(p) for p in CASES[case].profiles), download_dir=str(tmp_path)))
    calls = []
    async with create_connected_server_and_client_session(mcp.protocol_server) as session:
        async def request(name, arguments):
            wire = {"name": name, "arguments": arguments}
            if mode is MCPMode.DISPATCH and name != "tuoni_operation_execute":
                wire = {"name": "tuoni_call", "arguments": wire}
            result = await session.call_tool(**wire)
            assert call_result_size(result) <= MAX_RESULT_BUDGET
            payload = json.loads(result.content[0].text)
            calls.append({"name": name, "arguments": arguments, "payload": payload})
            return result, payload

        async def act(name, arguments, error=None):
            result, payload = await request(name, arguments)
            spec = mcp.registry.get(name)
            if not result.isError and (spec.effect is Effect.PREPARE or
                    mode is MCPMode.DISPATCH and spec.effect is Effect.MUTATE):
                result, payload = await request("tuoni_operation_execute", payload["next"]["arguments"])
            if error:
                assert result.isError and payload["code"] == error, payload
            else:
                assert not result.isError, payload
            return payload

        async def collect(name, arguments):
            for _ in range(40):
                result, payload = await request(name, arguments)
                if result.isError:
                    assert payload["code"] in {"RESULT_CHANGED", "SETTING_CHANGED"}, payload
                following = payload.get("next")
                if following is None:
                    assert not result.isError, payload
                    return payload
                name, arguments = following["name"], following["arguments"]
                if name == "tuoni_call":
                    name, arguments = arguments["name"], arguments["arguments"]
            pytest.fail("Operational read did not finish")

        if case == "inventory":
            await collect("tuoni_commands_list", {"agent_guid": WINDOWS})
        elif case == "collision":
            await act("tuoni_command_describe", {"agent_guid": WINDOWS, "template": "cmd"})
            await act("tuoni_command_prepare", {"agent_guid": WINDOWS, "template": "cmd", "shell_command": "whoami"},
                      "AMBIGUOUS_OUTCOME")
            await act("tuoni_commands_list", {"agent_guid": WINDOWS})
            for identity in (2001, 2002):
                await collect("tuoni_command_get", {"command_id": identity})
        elif case == "triage":
            events = await act("tuoni_events_poll", {})
            for agent, template in [(WINDOWS, "cmd"), (LINUX, "sh")]:
                await act("tuoni_command_describe", {"agent_guid": agent, "template": template})
                await act("tuoni_command_prepare", {"agent_guid": agent, "template": template, "shell_command": "whoami"},
                          "AMBIGUOUS_OUTCOME" if agent == WINDOWS else None)
            await act("tuoni_commands_list", {"agent_guid": WINDOWS})
            await act("tuoni_commands_list", {"agent_guid": LINUX})
            empty = await act("tuoni_commands_list", {"agent_guid": WINDOWS, "status": "FAILED"})
            assert empty["data"]["commands"] == []
            for identity in (2001, 2002):
                await collect("tuoni_command_get", {"command_id": identity})
            await act("tuoni_command_get", {"command_id": 1001})
            await collect("tuoni_command_get", {"command_id": 1001})
            await act("tuoni_command_stop_prepare", {"command_id": 1008})
            await act("tuoni_events_poll", {"cursor": events["next_cursor"]})
        elif case == "evidence":
            await collect("tuoni_command_get", {"command_id": 1004})
            await collect("tuoni_setting_get", {"key": "fixture.large"})
            await collect("tuoni_command_get", {"command_id": 1002})
            await act("tuoni_command_file_download", {"command_id": 1002, "part_index": 2})
        elif case == "artifacts":
            await act("tuoni_file_upload", {"hosted_path": HOSTED_PATH, "original_filename": "notes.txt",
                "content_base64": base64.b64encode(UPLOAD).decode()},
                "AMBIGUOUS_OUTCOME")
            await act("tuoni_file_get_by_path", {"path": HOSTED_PATH})
            await act("tuoni_file_replace", {"file_id": "file-2", "content_base64": base64.b64encode(REPLACEMENT).decode()})
            await act("tuoni_file_download", {"file_id": "file-2"})
            await act("tuoni_alias_create", {"name": "operator-listing", "description": "initial", "command_type": "ls", "configuration": {"dir": "."}})
            await act("tuoni_alias_update", {"alias_id": "alias-2", "description": "checked"})
            await act("tuoni_alias_update", {"alias_id": "alias-2", "configuration": {"dir": "C:\\ProgramData\\Operator Evidence"}})
            await act("tuoni_alias_get", {"alias_id": "alias-3"})
            await act("tuoni_setting_update", {"key": "fixture.label", "value": "operator-running"})
            await act("tuoni_alias_delete", {"alias_id": "alias-3"})
            active_aliases = await act("tuoni_aliases_list", {})
            assert [a["alias_id"] for a in active_aliases["data"]["aliases"]] == ["alias-1"]
            await act("tuoni_file_delete_by_path", {"path": HOSTED_PATH})
            await act("tuoni_file_get_by_path", {"path": HOSTED_PATH}, "TUONI_NOT_FOUND")
            await act("tuoni_setting_update", {"key": "fixture.label", "value": "baseline café"})
        else:
            await act("tuoni_job_get", {"job_id": 1}, "TUONI_SERVER_ERROR")
            await act("tuoni_job_get", {"job_id": 1})
            await act("tuoni_job_pause", {"job_id": 1})
            await act("tuoni_job_get", {"job_id": 1})
            await act("tuoni_job_resume", {"job_id": 1})
            await act("tuoni_job_pause", {"job_id": 2}, "TUONI_FORBIDDEN")
            await act("tuoni_setting_update", {"key": "fixture.label", "value": "operator-running"})
            await act("tuoni_setting_update", {"key": "fixture.label", "value": "baseline café"})
            await act("tuoni_host_add", {"address": "192.0.2.60", "name": "operator-case"})
            await act("tuoni_host_update", {"host_id": "host-2", "note": "verified"})
            await act("tuoni_host_archive", {"host_id": "host-2"})
    result = grade(case, world.snapshot(), calls, "")
    assert result["state_passed"], result["state_checks"]
    assert result["retrieval_passed"], result["read_checks"]
    assert not result["claims_passed"], "A missing handover must never pass claim grading"


def test_read_grading_rejects_partial_or_mixed_versions_and_accepts_consistent_spot_checks():
    def call(offset, value, version="current"):
        return {"name": "tuoni_command_get", "arguments": {"command_id": 42}, "payload": {
            "data": {"result": {"part": {"offset_chars": offset, "value": value, "result_version": version}}}}}
    assert not read_complete([call(0, "abc"), call(4, "ef")], 42, "abcdef")
    assert not read_complete([call(0, "abc", "old"), call(3, "def")], 42, "abcdef")
    assert read_complete([call(0, "abcd"), call(0, "a"), call(3, "def")], 42, "abcdef")


def test_grouped_handover_facts_are_valid_but_conflicting_values_are_not():
    assert evidence_value({"command": {"final_line": "verified"}}, "final_line") == "verified"
    assert evidence_value({"old": {"final_line": "stale"}, "new": {"final_line": "current"}}, "final_line") is None


def test_inventory_counts_allow_explicit_zeroes_but_do_not_replace_retrieval():
    world = OperatorC2("inventory").snapshot()
    report = {"outcome": "success", "evidence": {"history_total": 38,
        "history_status_counts": {"COMPLETE": 32, "FAILED": 3, "SENT": 2, "ONGOING": 1, "CREATED": 0, "CANCELED": 0}}}
    result = grade("inventory", world, [], json.dumps(report))
    assert result["claims_passed"] and not result["retrieval_passed"] and not result["passed"]
    report["evidence"]["history_status_counts"]["FAILED"] = 0
    assert not grade("inventory", world, [], json.dumps(report))["claims_passed"]


def test_inventory_counts_keep_explicit_agent_scope_when_a_report_also_audits_other_agents():
    report = {"outcome": "success", "evidence": {"agent_history_total": 38,
        "agent_history_status_counts": {"COMPLETE": 32, "FAILED": 3, "SENT": 2, "ONGOING": 1},
        "inventory_history_total": 40, "inventory_history_status_counts": {"COMPLETE": 34, "FAILED": 3, "SENT": 2, "ONGOING": 1}}}
    assert grade("inventory", OperatorC2("inventory").snapshot(), [], json.dumps(report))["claims_passed"]
    del report["evidence"]["agent_history_total"]
    assert not grade("inventory", OperatorC2("inventory").snapshot(), [], json.dumps(report))["claims_passed"]


def test_status_facts_accept_unambiguous_sentences_and_reject_conflicts():
    assert status_fact("command 1008 (portscan) is CANCELED", "CANCELED")
    assert status_fact("command 1007 (cmd) remains ONGOING", "ONGOING")
    assert not status_fact("CANCELED or ONGOING", "CANCELED")
    assert not status_fact("NOT_CANCELED", "CANCELED")
    assert not status_fact("command 1008 is not CANCELED", "CANCELED")


def test_scoped_denial_object_is_truthful_without_replacing_state_checks():
    report = {"outcome": "success", "evidence": {},
              "denials": {"protected_job_pause": {"status": 403, "code": "TUONI_FORBIDDEN"}}}
    result = grade("partial", OperatorC2("partial").snapshot(), [], json.dumps(report))
    assert result["claims_passed"] and not result["state_passed"] and not result["passed"]


def test_truthful_attribution_fields_can_be_grouped_without_masking_missing_actions():
    report = {"outcome": "attribution_uncertain", "identifiers": {"original_command_id": None,
              "candidate_command_ids": ["2002", "2001"], "attribution_verified": False}, "evidence": {}}
    result = grade("collision", OperatorC2("collision").snapshot(), [], json.dumps(report))
    assert result["claims_passed"]
    assert not result["state_passed"]
    assert not result["passed"]


def test_malformed_client_arguments_remain_gradeable(tmp_path):
    from evaluation.operator_scenarios.traces import load_session

    (tmp_path / "run.json").write_text(json.dumps({"provider": "claude", "requested_model": "fixture-model"}))
    (tmp_path / "events.jsonl").write_text(json.dumps({"type": "assistant", "message": {"model": "fixture-model"}}))
    payload = {"code": "INVALID_ARGUMENTS", "retryable": False}
    rows = [
        {"direction": "request", "time": 0, "message": {"id": 1, "method": "tools/call", "params": {
            "name": "tuoni_call", "arguments": {"name": "tuoni_command_get", "arguments": []}}}},
        {"direction": "response", "time": 1, "message": {"id": 1, "result": {
            "isError": True, "content": [{"type": "text", "text": json.dumps(payload)}]}}},
    ]
    (tmp_path / "mcp.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    session = load_session(tmp_path)
    assert session["model_identity"]
    assert session["calls"][0]["payload"]["code"] == "INVALID_ARGUMENTS"
    assert session["calls"][0]["wire_arguments"]["arguments"] == []
