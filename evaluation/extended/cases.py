"""Bounded model tasks; required tool coverage is distinct from goal grading."""
from __future__ import annotations

from dataclasses import dataclass

from evaluation.extended.world import AGENT_GUID, template_id


@dataclass(frozen=True)
class Case:
    profiles: tuple[str, ...]
    prompt: str
    steps: tuple[tuple[str, dict], ...]
    fault: str | None = None


CASES = {
    "listeners": Case(("agent-ops", "access"),
        "Inspect listener plugins and describe one. Create a listener named smoke-listener with port 9443. "
        "Fetch it, rename it to checked-listener, start it, stop it, then delete only that new listener. "
        "List existing listeners too. Report its ID and final outcome.", (
            ("tuoni_listeners_list", {}), ("tuoni_listener_plugins_list", {}),
            ("tuoni_listener_plugin_get", {"plugin_id": "fixture.listener"}),
            ("tuoni_listener_create", {"plugin_id": "fixture.listener", "configuration": {"port": 9443}, "name": "smoke-listener"}),
            ("tuoni_listener_get", {"listener_id": 2}),
            ("tuoni_listener_update", {"listener_id": 2, "name": "checked-listener"}),
            ("tuoni_listener_start", {"listener_id": 2}), ("tuoni_listener_stop", {"listener_id": 2}),
            ("tuoni_listener_delete", {"listener_id": 2}),
        )),
    "payloads": Case(("agent-ops", "access"),
        "List payloads and inspect an existing payload. Discover payload plugins, list one plugin's templates, "
        "and describe the Windows template. Discover a compatible listener. Create one payload named smoke-payload "
        "with type executable, rename it to checked-payload, obtain its download reference, then delete only that new payload. "
        "Report the new ID and whether any binary was included in tool output.", (
            ("tuoni_payloads_list", {}), ("tuoni_payload_get", {"payload_id": 42}),
            ("tuoni_payload_plugins_list", {}), ("tuoni_payload_plugin_templates_list", {"plugin_id": "fixture.payload"}),
            ("tuoni_payload_template_get", {"template_id": "fixture.windows-x64"}), ("tuoni_listeners_list", {}),
            ("tuoni_payload_create", {"template": "fixture.windows-x64", "listener_id": 1,
                                      "configuration": {"type": "executable"}, "name": "smoke-payload"}),
            ("tuoni_payload_rename", {"payload_id": 43, "new_name": "checked-payload"}),
            ("tuoni_payload_download", {"payload_id": 43}), ("tuoni_payload_delete", {"payload_id": 43}),
        )),
    "hosted_files": Case(("agent-ops", "artifacts"),
        "List hosted files; inspect the seed file by hosted path and obtain its HTTP download reference. "
        "Upload literal text smoke (base64 c21va2U=) as /fixture/smoke.txt with original filename smoke.txt. "
        "Replace that upload with literal text replaced (base64 cmVwbGFjZWQ=). Delete the seed file by its ID "
        "and delete the new file by hosted path. Report IDs, replacement size, and final state.", (
            ("tuoni_files_list", {}), ("tuoni_file_get_by_path", {"path": "/fixture/seed.txt"}),
            ("tuoni_file_download", {"file_id": "file-1"}),
            ("tuoni_file_upload", {"hosted_path": "/fixture/smoke.txt", "content_base64": "c21va2U=", "original_filename": "smoke.txt"}),
            ("tuoni_file_replace", {"file_id": "file-2", "content_base64": "cmVwbGFjZWQ="}),
            ("tuoni_file_delete", {"file_id": "file-1"}),
            ("tuoni_file_delete_by_path", {"path": "/fixture/smoke.txt"}),
        )),
    "discovery": Case(("agent-ops", "discovery"),
        "Read stored discovery hosts, services, and credentials. Add a host at 192.0.2.20 named smoke-host, "
        "a tcp service at that address on port 8443, and a credential with username smoke-user/password fixture-pass. "
        "Update the note of each new record to checked; then archive only those three records. "
        "Report their IDs. This edits stored discovery records and does not scan a network.", (
            ("tuoni_hosts_list", {}), ("tuoni_services_list", {}), ("tuoni_credentials_list", {}),
            ("tuoni_host_add", {"address": "192.0.2.20", "name": "smoke-host"}),
            ("tuoni_service_add", {"address": "192.0.2.20", "port": 8443, "protocol": "tcp"}),
            ("tuoni_credential_add", {"username": "smoke-user", "password": "fixture-pass"}),
            ("tuoni_host_update", {"host_id": "host-2", "note": "checked"}),
            ("tuoni_service_update", {"service_id": "service-2", "note": "checked"}),
            ("tuoni_credential_update", {"credential_id": "credential-2", "note": "checked"}),
            ("tuoni_host_archive", {"host_id": "host-2"}),
            ("tuoni_service_archive", {"service_id": "service-2"}),
            ("tuoni_credential_archive", {"credential_id": "credential-2"}),
        )),
    "administration": Case(("agent-ops", "administration"),
        "List users. Create smoke-user with password fixture-pass and VIEW_RESOURCES authority. "
        "Replace its authorities with SEND_COMMANDS, change its password to fixture-new-pass, then disable it. "
        "Read all settings and read fixture.label individually. Set fixture.label to checked and bulk-update "
        "fixture.enabled to false. List jobs, inspect fixture-job, then pause, resume, and restart it. "
        "Report the final user, setting, and job states.", (
            ("tuoni_users_list", {}),
            ("tuoni_user_create", {"username": "smoke-user", "password": "fixture-pass", "authorities": ["VIEW_RESOURCES"]}),
            ("tuoni_user_set_authorities", {"username": "smoke-user", "authorities": ["SEND_COMMANDS"]}),
            ("tuoni_user_set_password", {"username": "smoke-user", "new_password": "fixture-new-pass"}),
            ("tuoni_user_set_enabled", {"username": "smoke-user", "enabled": False}),
            ("tuoni_settings_get_all", {}), ("tuoni_setting_get", {"key": "fixture.label"}),
            ("tuoni_setting_update", {"key": "fixture.label", "value": "checked"}),
            ("tuoni_settings_update_bulk", {"settings": {"fixture.enabled": False}}),
            ("tuoni_jobs_list", {}), ("tuoni_job_get", {"job_id": 1}),
            ("tuoni_job_pause", {"job_id": 1}), ("tuoni_job_resume", {"job_id": 1}),
            ("tuoni_job_restart", {"job_id": 1}),
        )),
    "authoring": Case(("agent-ops", "authoring"),
        "List command plugins, list one plugin's command templates, and inspect the ls template's schema. "
        "List aliases and fetch seed-listing by ID; obtain its configuration-file reference using file ID fixture-file. "
        "Create alias smoke-listing on ls with fixed dir '.', update only its description to checked, "
        "then change its fixed dir to C:\\Users\\Public. Track any replacement ID, fetch the replacement, "
        "then archive it. Report the original and replacement IDs and which aliases remain active.", (
            ("tuoni_command_plugins_list", {}),
            ("tuoni_command_plugin_commands_list", {"plugin_id": "fixture.commands"}),
            ("tuoni_command_plugin_command_get", {"plugin_id": "fixture.commands", "template_id": template_id("ls")}),
            ("tuoni_aliases_list", {}), ("tuoni_alias_get", {"alias_id": "alias-1"}),
            ("tuoni_alias_download_file", {"alias_id": "alias-1", "file_id": "fixture-file"}),
            ("tuoni_alias_create", {"name": "smoke-listing", "description": "initial", "command_type": "ls", "configuration": {"dir": "."}}),
            ("tuoni_alias_update", {"alias_id": "alias-2", "description": "checked"}),
            ("tuoni_alias_update", {"alias_id": "alias-2", "configuration": {"dir": "C:\\Users\\Public"}}),
            ("tuoni_alias_get", {"alias_id": "alias-3"}), ("tuoni_alias_delete", {"alias_id": "alias-3"}),
        )),
    "agent_control": Case(("agent-ops", "administration"),
        "Find workstation-benchmark and inspect its metadata. Set custom property smoke to checked, clear its queued commands, "
        "block it, restore it, and finally mark it inactive. Do not submit agent commands. Report its GUID and resulting state.", (
            ("tuoni_agents_list", {"query": "workstation-benchmark"}), ("tuoni_agent_get", {"agent_guid": AGENT_GUID}),
            ("tuoni_agent_set_custom_property", {"agent_guid": AGENT_GUID, "name": "smoke", "value": "checked"}),
            ("tuoni_agent_clear_queue", {"agent_guid": AGENT_GUID}),
            ("tuoni_agent_block", {"agent_guid": AGENT_GUID}), ("tuoni_agent_restore", {"agent_guid": AGENT_GUID}),
            ("tuoni_agent_set_inactive", {"agent_guid": AGENT_GUID}),
        )),
    "interactive_results": Case(("agent-ops", "artifacts", "events"),
        "Inspect capabilities and command history. Poll command-result events and retain the cursor. "
        "Update interactive command 1007 with stdin line ping using preparation/execution. Stop command 1008 using preparation/execution. "
        "Then discover cmd on workstation-benchmark, run whoami exactly once, and read its account result. "
        "Poll events from the retained cursor and report the new command-result event. Inspect command 1002's multipart manifest "
        "and download its file part to the MCP download directory using the result-file tool. Report the local path and new account.", (
            ("tuoni_capabilities", {}), ("tuoni_commands_list", {}), ("tuoni_events_poll", {}),
            ("tuoni_command_update_prepare", {"command_id": 1007, "configuration": {"stdin": ["ping"]}}),
            ("tuoni_command_stop_prepare", {"command_id": 1008}),
            ("tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": "cmd"}),
            ("tuoni_command_prepare", {"agent_guid": AGENT_GUID, "template": "cmd", "shell_command": "whoami"}),
            ("tuoni_command_get", {"command_id": 2001}),
            ("tuoni_command_get", {"command_id": 2001, "part_index": 0}),
            ("tuoni_command_file_download", {"command_id": 1002, "part_index": 2}),
        )),
    "result_windows": Case(("agent-ops", "administration"),
        "Read the complete text of existing command 1004. Report its exact final line and total line count. "
        "Read command 1003's Unicode text and report whether accented characters, combining characters, emoji, and Chinese text are present. "
        "Read the complete fixture.large setting and report its entry count and exact last entry. "
        "Follow returned continuations until each requested result is complete; do not mutate anything.", (
            ("tuoni_command_get", {"command_id": 1004}),
            ("tuoni_command_get", {"command_id": 1004, "part_index": 0}),
            ("tuoni_command_get", {"command_id": 1003}),
            ("tuoni_command_get", {"command_id": 1003, "part_index": 0}),
            ("tuoni_setting_get", {"key": "fixture.large"}),
        )),
    "ambiguous_execution": Case(("agent-ops",),
        "Find workstation-benchmark, discover and describe cmd, run exact shell text whoami once, and read its account. "
        "A submission response may be lost. If the outcome is ambiguous, inspect command history to locate the accepted command; "
        "do not submit a replacement. Report whether submission was confirmed and the recovered command ID.", (
            ("tuoni_agents_list", {"query": "workstation-benchmark"}),
            ("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "cmd"}),
            ("tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": "cmd"}),
            ("tuoni_command_prepare", {"agent_guid": AGENT_GUID, "template": "cmd", "shell_command": "whoami"}),
        ), "ambiguous-command"),
}


def goal_checks(case, world):
    """Grade actual backend state independently from model narrative claims."""
    writes = world["writes"]
    if case == "listeners":
        return {"created_once": sum(w["uri"] == "/api/v1/listeners" and w["method"] == "POST" for w in writes) == 1,
                "only_seed_remains": list(world["listeners"]) == ["1"]}
    if case == "payloads":
        return {"created_once": sum(w["uri"] == "/api/v1/payloads" and w["method"] == "POST" for w in writes) == 1,
                "only_seed_remains": list(world["payloads"]) == ["42"]}
    if case == "hosted_files":
        return {"files_deleted": not world["files"],
                "replacement_uploaded": any(w["method"] == "PUT" and (w["upload"] or {}).get("size") == 8 for w in writes)}
    if case == "discovery":
        return {"all_new_records_archived": all(any(v.get("note") == "checked" and v["status"] == "ARCHIVED"
                for v in collection.values()) for collection in world["discovery"].values())}
    if case == "administration":
        user = world["users"].get("smoke-user", {})
        return {"user_state": user.get("enabled") is False and user.get("authorities") == ["SEND_COMMANDS"]
                and user.get("newPassword") == "fixture-new-pass",
                "settings_state": world["settings"]["fixture.label"] == "checked" and world["settings"]["fixture.enabled"] is False,
                "job_state": world["jobs"]["1"]["status"] == "RUNNING"}
    if case == "authoring":
        return {"tracked_alias_replacement": world["aliases"].get("alias-2", {}).get("status") == "ARCHIVED"
                and world["aliases"].get("alias-3", {}).get("status") == "ARCHIVED"
                and world["aliases"].get("alias-3", {}).get("fixedConfiguration") == {"dir": "C:\\Users\\Public"}}
    if case == "agent_control":
        agent = world["agents"][0]
        return {"agent_state": agent["active"] is False and agent["metadata"].get("customProperties", {}).get("smoke") == "checked",
                "no_commands_submitted": not any(w["method"] == "POST" and w["uri"].endswith("/commands") for w in writes)}
    if case == "interactive_results":
        return {"one_new_command": len([w for w in writes if w["uri"].endswith("/commands")]) == 1,
                "stdin_delivered": world["commands"]["1007"].get("updates", [{}])[0].get("updateConfiguration") == {"stdin": ["ping"]},
                "command_stopped": world["commands"]["1008"]["status"] == "CANCELED"}
    if case == "result_windows":
        return {"read_only": not writes}
    if case == "ambiguous_execution":
        return {"one_submission_despite_lost_response": len(writes) == 1 and "2001" in world["commands"]}
    raise KeyError(case)
