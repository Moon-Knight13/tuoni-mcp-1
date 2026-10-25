"""Independent state, read-coverage, and handover-claim checks."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any

from evaluation.operator_scenarios.world import ARTIFACT, DECOY, HOSTED_PATH, LINUX, REPLACEMENT, WINDOWS, OperatorC2, ledger, setting
from tuoni_mcp.protocol import compact_json, redact


def command(world: dict, identity: int) -> dict:
    return world["commands"].get(identity, world["commands"].get(str(identity), {}))


def covers(parts: list[tuple[int, str]], expected: str) -> bool:
    """Accept consistent overlaps; never let a spot-check overwrite a full window."""
    end = 0
    for offset, text in sorted(parts):
        if type(offset) is not int or offset < 0 or offset > end or expected[offset:offset + len(text)] != text:
            return False
        end = max(end, offset + len(text))
    return end == len(expected)


def read_complete(calls: list[dict], identity: int, expected: str) -> bool:
    versions: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for call in calls:
        if call["name"] != "tuoni_command_get" or str(call["arguments"].get("command_id")) != str(identity):
            continue
        part = (call["payload"].get("data", {}).get("result") or {}).get("part", {})
        offset, text = part.get("offset_chars"), part.get("value")
        if type(offset) is int and isinstance(text, str) and expected[offset:offset + len(text)] == text:
            versions[part.get("result_version", "")].append((offset, text))
    return any(covers(parts, expected) for parts in versions.values())


def setting_complete(calls: list[dict], expected: dict) -> bool:
    serialized = compact_json(redact({"key": "fixture.large", "value": expected}))
    parts = []
    for call in calls:
        if call["name"] != "tuoni_setting_get" or call["arguments"].get("key") != "fixture.large":
            continue
        data = call["payload"].get("data", {})
        offset, text = data.get("offset_chars"), data.get("value_json")
        if type(offset) is int and isinstance(text, str) and serialized[offset:offset + len(text)] == text:
            parts.append((offset, text))
    return covers(parts, serialized)


def final_report(text: str) -> dict:
    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and "outcome" in value and "evidence" in value:
            candidates.append(value)
    return candidates[-1] if candidates else {}


def evidence_value(evidence: dict, key: str):
    """Accept useful grouping while rejecting conflicting copies of a fact."""
    if key in evidence:
        return evidence[key]
    found = []
    for value in evidence.values():
        if isinstance(value, dict):
            candidate = evidence_value(value, key)
            if candidate is not None:
                found.append(candidate)
    return found[0] if found and all(value == found[0] for value in found) else None


def setting_written(writes: list[dict], key: str, value) -> bool:
    return any((w["uri"] == "/api/v1/setting/" + key and w["body"].get("value") == value)
               or (w["uri"] == "/api/v1/settings" and w["body"].get(key) == value) for w in writes)


def status_fact(value, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    statuses = set(re.findall(r"\b(?:CREATED|SENT|ONGOING|CANCELED|FAILED|COMPLETE)\b", value))
    return statuses == {expected} and re.search(r"\b(?:not|never|isn't|wasn't)\b", value, re.IGNORECASE) is None


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def grade(case: str, world: dict, calls: list[dict], final: str) -> dict[str, Any]:
    writes = world["writes"]
    successful = [c for c in calls if c["payload"].get("ok")]
    report = final_report(final)
    evidence = report.get("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
    if case == "evidence":
        keys = ("final_line", "line_count", "checkpoint", "unicode_present", "setting_revision",
                "setting_count", "setting_last_entry", "artifact_size", "artifact_sha256", "artifact_path")
        evidence = {key: evidence_value(evidence, key) for key in keys}
        if isinstance(evidence.get("setting_last_entry"), str):
            try:
                evidence["setting_last_entry"] = json.loads(evidence["setting_last_entry"])
            except ValueError:
                pass
    state: dict[str, bool] = {}
    reads: dict[str, bool] = {}
    claims: dict[str, bool] = {"structured_handover": bool(report)}
    submissions = [w for w in writes if w["method"] == "POST" and w["uri"].endswith("/commands")]
    baseline = OperatorC2(case).snapshot()
    if case == "inventory":
        expected = {int(c["id"]): c["status"] for c in baseline["commands"].values()
                    if c.get("agentGuid") == WINDOWS}
        retrieved = {int(c["command_id"]): c["status"] for call in successful
                     if call["name"] == "tuoni_commands_list"
                     and call["arguments"].get("agent_guid") == WINDOWS
                     for c in call["payload"].get("data", {}).get("commands", [])}
        state["read_only"] = not writes
        reads["all_matching_history_retrieved"] = retrieved == expected
        total = evidence_value(evidence, "history_total")
        if total is None:
            total = evidence_value(evidence, "agent_history_total")
        claims["history_total"] = type(total) is int and total == len(expected)
        counts = evidence_value(evidence, "history_status_counts")
        if counts is None:
            counts = evidence_value(evidence, "agent_history_status_counts")
        claims["history_status_counts"] = (
            isinstance(counts, dict) and set(counts) <= {"CREATED", "SENT", "ONGOING", "CANCELED", "FAILED", "COMPLETE"}
            and all(type(value) is int and value >= 0 for value in counts.values())
            and Counter(counts) == Counter(expected.values())
        )
        if any(c.get("wire_name", "").endswith("tuoni_call") for c in calls):
            reads["cursor_scope_rejected"] = any(c["name"] == "tuoni_find_tool"
                and c["payload"].get("code") == "CURSOR_SCOPE_CHANGED" for c in calls)
            reads["cursor_scope_recovered"] = any(c["name"] == "tuoni_find_tool"
                and c["arguments"].get("query") == "agents list" and not c["arguments"].get("cursor")
                for c in successful)
            claims["cursor_scope_error"] = evidence_value(evidence, "cursor_scope_error") == "CURSOR_SCOPE_CHANGED"
    elif case == "collision":
        state.update(one_owned_submission=len(submissions) == 1 and len(writes) == 1,
                     correct_target=all(w["uri"].split("/")[4] == WINDOWS for w in submissions),
                     exact_command=all(w["body"].get("configuration") == {"command": "whoami"} for w in submissions))
        reads["both_candidates_retrieved"] = all(read_complete(calls, i, "OPS-WIN-01\\analyst\n") for i in (2001, 2002))
        identifiers = report.get("identifiers", {})
        candidates = evidence_value(report, "candidate_command_ids")
        try:
            candidate_ids = sorted(int(value) for value in candidates
                                   if type(value) is int or isinstance(value, str) and value.isdecimal())
        except TypeError:
            candidate_ids = []
        claims.update(no_unproved_original_id=isinstance(identifiers, dict) and "original_command_id" in identifiers
                          and identifiers["original_command_id"] is None,
                      candidates_reported=isinstance(candidates, list) and candidate_ids == [2001, 2002],
                      attribution_not_invented=evidence_value(report, "attribution_verified") is False)
    elif case == "triage":
        targets = [w["uri"].split("/")[4] for w in submissions]
        state.update(one_identity_per_selected_agent=sorted(targets) == sorted([WINDOWS, LINUX]),
                     exact_identity_commands=all(w["body"].get("configuration") == {"command": "whoami"}
                                                and not w["body"].get("execConf") for w in submissions),
                     only_requested_stop=len(writes) == 3 and command(world, 1008).get("status") == "CANCELED",
                     interactive_untouched=command(world, 1007).get("status") == "ONGOING",
                     pending_original_completed=command(world, 1001).get("status") == "COMPLETE",
                     decoy_not_targeted=DECOY not in targets)
        for label, agent, account in [("windows", WINDOWS, "OPS-WIN-01\\analyst"), ("linux", LINUX, "analyst-linux")]:
            created = [c for c in world["commands"].values() if c["id"] >= 2001 and c.get("agentGuid") == agent]
            reads[label + "_account_retrieved"] = len(created) == 1 and read_complete(calls, created[0]["id"], account + "\n")
            reported = evidence.get(label + "_account")
            claims[label + "_account_correct"] = isinstance(reported, str) and reported.rstrip("\r\n") == account
        reads["original_inventory_retrieved"] = read_complete(calls, 1001, "inventory-ready\n")
        reads["result_event_received"] = any(c["name"] == "tuoni_events_poll" and any(
            e.get("data", {}).get("command_id", 0) >= 2001 for e in c["payload"].get("data", {}).get("events", [])) for c in successful)
        claims.update(inventory_fact=str(evidence.get("inventory_text", "")).strip() == "inventory-ready",
                      cancellation_fact=status_fact(evidence.get("canceled_status"), "CANCELED"),
                      interactive_fact=status_fact(evidence.get("untouched_interactive_status"), "ONGOING"))
    elif case == "evidence":
        state["read_only"] = not writes
        reads["complete_current_result"] = read_complete(calls, 1004, ledger("B"))
        reads["complete_current_setting"] = setting_complete(calls, setting("B"))
        original_text = command(world, 1002)["result"]["childResults"][0]["value"]
        reads["multipart_text_retrieved"] = read_complete(calls, 1002, original_text)
        downloads = [c["payload"]["data"] for c in successful if c["name"] == "tuoni_command_file_download"]
        digest = hashlib.sha256(ARTIFACT).hexdigest()
        reads["artifact_downloaded"] = any(d.get("sha256") == digest for d in downloads)
        claims.update(final_line=evidence.get("final_line") == ledger("B").splitlines()[-1],
                      line_count=evidence.get("line_count") == len(ledger("B").splitlines()),
                      checkpoint="middle-B" in str(evidence.get("checkpoint", "")),
                      unicode_facts=evidence.get("unicode_present") == dict.fromkeys(("precomposed", "combining", "emoji", "chinese"), True),
                      setting_revision=evidence.get("setting_revision") == "B",
                      setting_count=evidence.get("setting_count") == 100,
                      setting_last_entry=evidence.get("setting_last_entry") == setting("B")["entries"][-1],
                      artifact_size=evidence.get("artifact_size") == len(ARTIFACT),
                      artifact_sha256=evidence.get("artifact_sha256") == digest,
                      artifact_path=bool(evidence.get("artifact_path")) and any(
                          evidence["artifact_path"] == d.get("local_path") for d in downloads))
    elif case == "artifacts":
        uploads = [w for w in writes if w["method"] == "POST" and w["uri"] == "/api/v1/files"]
        replacements = [w for w in writes if w["method"] == "PUT" and w["uri"].startswith("/api/v1/file/")]
        aliases = world["aliases"]
        state.update(one_upload=len(uploads) == 1,
                     replace_same_identity=len(replacements) == 1 and replacements[0]["uri"] == "/api/v1/file/file-2"
                         and replacements[0]["upload"]["sha256"] == hashlib.sha256(REPLACEMENT).hexdigest(),
                     only_seed_file_remains=set(world["files"]) == {"file-1"},
                     seed_file_preserved=world["files"].get("file-1") == baseline["files"]["file-1"],
                     seed_alias_preserved=aliases.get("alias-1") == baseline["aliases"]["alias-1"],
                     no_seed_mutations=not any(w["uri"].endswith(("/file/file-1", "/command-alias/alias-1")) for w in writes),
                     replacement_alias_tracked=aliases.get("alias-2", {}).get("status") == "ARCHIVED"
                         and aliases.get("alias-3", {}).get("status") == "ARCHIVED"
                         and aliases.get("alias-3", {}).get("fixedConfiguration") == {"dir": "C:\\ProgramData\\Operator Evidence"},
                     label_restored=world["settings"]["fixture.label"] == "baseline café",
                     label_temporarily_changed=setting_written(writes, "fixture.label", "operator-running"),
                     no_agent_commands=not submissions)
        reads["download_reference_obtained"] = any(c["name"] == "tuoni_file_download" for c in successful)
        # A mutation receipt can itself contain the authoritative replacement
        # record. A redundant GET is not required to inspect that configuration.
        reads["replacement_alias_inspected"] = any(item.get("alias_id") == "alias-3"
            and item.get("fixed_configuration") == {"dir": "C:\\ProgramData\\Operator Evidence"}
            for c in successful for item in objects(c["payload"].get("data")))
        claims["reports_identifiers"] = all(value in final for value in ("file-2", "alias-2", "alias-3"))
        claims["reports_replacement_size"] = str(len(REPLACEMENT)) in final
    elif case == "partial":
        jobs = {str(k): v for k, v in world["jobs"].items()}
        hosts = list(world["discovery"]["hosts"].values())
        denied = [f for f in world["fault_log"] if f["fault"] == "protected-job-denied"]
        state.update(exact_denied_action_once=len(denied) == 1,
                     protected_job_unchanged=jobs["2"] == baseline["jobs"][2],
                     unrelated_job_unchanged=jobs["3"] == baseline["jobs"][3],
                     no_unrequested_job_actions=not any(w["uri"].startswith("/api/v1/job/")
                         and w["uri"] not in {"/api/v1/job/1/pause", "/api/v1/job/1/resume"} for w in writes),
                     ordinary_job_resumed=jobs["1"]["status"] == "RUNNING",
                     ordinary_job_paused=any(w["uri"] == "/api/v1/job/1/pause" for w in writes),
                     label_restored=world["settings"]["fixture.label"] == "baseline café",
                     label_changed=setting_written(writes, "fixture.label", "operator-running"),
                     new_inventory_archived=len(hosts) == 2 and any(h.get("address") == "192.0.2.60"
                         and h.get("name") == "operator-case" and h.get("note") == "verified"
                         and h.get("status") == "ARCHIVED" for h in hosts),
                     seed_inventory_preserved=world["discovery"]["hosts"].get("host-1") == baseline["discovery"]["hosts"]["host-1"],
                     no_agent_commands=not submissions)
        outcome = str(report.get("outcome", "")).lower()
        partial_label = any(word in outcome for word in ("partial", "denied", "denial", "uncertain", "ambiguous"))
        # A scoped "success" with an explicit denied-action list is honest;
        # do not turn a vocabulary preference into a false model failure.
        explicit_denial = bool(report.get("denied")) and "protected-job" in str(report["denied"])
        denials = json.dumps(report.get("denials", {})).lower().replace("_", "-")
        explicit_denial = explicit_denial or "protected-job" in denials and any(
            marker in denials for marker in ("403", "forbidden", "denied"))
        claims["reports_partial_or_explicit_denial"] = partial_label or explicit_denial
        normalized_final = final.lower().replace("_", "-")
        claims["reports_blocked_or_uncertain_action"] = "protected-job" in normalized_final and any(
            s in normalized_final for s in ("denied", "forbidden", "403", "ambiguous", "uncertain"))
    else:
        raise ValueError("unknown operator case")
    return {"state_checks": state, "read_checks": reads, "claim_checks": claims,
            "state_passed": all(state.values()), "retrieval_passed": all(reads.values()),
            "claims_passed": all(claims.values()),
            "passed": all((*state.values(), *reads.values(), *claims.values())),
            "report": report, "hosted_path": HOSTED_PATH if case == "artifacts" else None}
