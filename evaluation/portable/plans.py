"""Scripted plans for the twenty portable workflows.

Each plan uses the interface as a tool-calling client would: search before
describing, describe before preparing, follow continuations verbatim, and never
invent an identifier.

Discovery workflows (W04, W05) deliberately query with the operator's own words
rather than a Tuoni template name. They are the harness's measurement of whether
the interface can bridge intent to vocabulary, so they must not be helped along.
Later workflows use a reliable template so they measure their own subject -
preparation, execution or result reading - instead of failing for a discovery
reason.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from evaluation.portable.runner import Call, Driver
from evaluation.portable.world import (
    AGENT_GUID,
    AGENT_HINT,
    INTERACTIVE_COMMAND_ID,
    IDENTITY_COMMAND_ID,
    LARGE_RESULT_COMMAND_ID,
    MULTIPART_COMMAND_ID,
    PENDING_COMMAND_ID,
    PROCESS_COMMAND_ID,
    PortableC2,
    SAFE_ARGUMENT_VALUE,
    STOPPABLE_COMMAND_ID,
    UNICODE_COMMAND_ID,
    UNICODE_OUTPUT,
)

Plan = Callable[[Driver, PortableC2], Awaitable[bool]]
_PLANS: list[tuple[str, Plan]] = []


def workflow(workflow_id: str, *, allowed_mutations: int = 0) -> Callable[[Plan], Plan]:
    def decorate(plan: Plan) -> Plan:
        plan.allowed_mutations = allowed_mutations  # type: ignore[attr-defined]
        _PLANS.append((workflow_id, plan))
        return plan

    return decorate


# -- helpers ---------------------------------------------------------------


def _commands(call: Call) -> list[dict[str, Any]]:
    data = call.data
    if not isinstance(data, dict):
        return []
    found = data.get("commands")
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def _reference(summary: dict[str, Any]) -> str:
    """Prefer the short name a small model can copy without transcribing a UUID."""

    return str(summary.get("name") or summary.get("id") or "")


def _continues_to(call: Call, tool_name: str) -> bool:
    """Test what a continuation targets, in either exposure.

    Native mode names the tool directly. Dispatch mode wraps the same call as
    ``tuoni_call`` and moves the target into its arguments, so both shapes have
    to be accepted for one script to measure both.
    """

    following = call.next_call
    if not following:
        return False
    name = str(following.get("name") or "")
    if name.endswith(tool_name):
        return True
    arguments = following.get("arguments")
    inner = str(arguments.get("name") or "") if isinstance(arguments, dict) else ""
    return inner.endswith(tool_name)


def _part(call: Call) -> dict[str, Any]:
    data = call.data
    result = data.get("result") if isinstance(data, dict) else None
    part = result.get("part") if isinstance(result, dict) else None
    return part if isinstance(part, dict) else {}


def _parts(call: Call) -> list[dict[str, Any]]:
    data = call.data
    result = data.get("result") if isinstance(data, dict) else None
    parts = result.get("parts") if isinstance(result, dict) else None
    return [item for item in parts if isinstance(item, dict)] if isinstance(parts, list) else []


async def _shell_preparation(driver: Driver, command_text: str) -> Call:
    """Search, describe and prepare the reliable shell path."""

    searched = await driver.call("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "cmd"})
    summaries = _commands(searched)
    if not summaries:
        return searched
    reference = _reference(summaries[0])
    await driver.call("tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": reference})
    return await driver.call(
        "tuoni_command_prepare",
        {"agent_guid": AGENT_GUID, "template": reference, "shell_command": command_text},
    )


async def _first_text_window(driver: Driver, command_id: int) -> Call | None:
    """Read the manifest, then open the first text part it advertises."""

    manifest = await driver.call("tuoni_command_get", {"command_id": command_id})
    if not manifest.ok:
        return None
    text_parts = [item for item in _parts(manifest) if item.get("type") == "text" and item.get("chunkable")]
    if not text_parts:
        return None
    window = await driver.call(
        "tuoni_command_get",
        {
            "command_id": command_id,
            "part_index": int(text_parts[0]["part_index"]),
            "result_version": text_parts[0]["result_version"],
            "offset_chars": 0,
            "max_chars": 4096,
        },
    )
    return window if window.ok else None


# -- discovery -------------------------------------------------------------


@workflow("W01")
async def w01_list_active_agents(driver: Driver, _world: PortableC2) -> bool:
    listed = await driver.call("tuoni_agents_list", {"active": True, "inactive": False, "limit": 10})
    if not listed.ok or not isinstance(listed.data, dict):
        return False
    agents = listed.data.get("agents")
    # "next_cursor" is always present, so the model can always state whether
    # another page exists. That is half of this workflow's success condition.
    return isinstance(agents, list) and len(agents) >= 1 and "next_cursor" in listed.payload


@workflow("W02")
async def w02_find_agent_from_hint(driver: Driver, _world: PortableC2) -> bool:
    listed = await driver.call("tuoni_agents_list", {"query": AGENT_HINT, "limit": 10})
    if not listed.ok or not isinstance(listed.data, dict):
        return False
    agents = listed.data.get("agents") or []
    matched = [
        item
        for item in agents
        if isinstance(item, dict) and AGENT_HINT in str(item.get("hostname") or "")
    ]
    return len(matched) == 1 and matched[0].get("guid") == AGENT_GUID


@workflow("W03")
async def w03_inspect_agent_capabilities(driver: Driver, _world: PortableC2) -> bool:
    detail = await driver.call("tuoni_agent_get", {"agent_guid": AGENT_GUID})
    if not detail.ok or not isinstance(detail.data, dict):
        return False
    metadata = detail.data.get("metadata")
    has_platform = isinstance(metadata, dict) and bool(metadata.get("os"))
    # A capability count, never a schema dump: the whole point of the tool.
    return has_platform and isinstance(detail.data.get("capability_count"), int)


@workflow("W04")
async def w04_search_identity_command(driver: Driver, _world: PortableC2) -> bool:
    for query in ("identity", "whoami"):
        searched = await driver.call(
            "tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": query}
        )
        if searched.ok and _commands(searched):
            return True
    return False


@workflow("W05")
async def w05_describe_identity_command(driver: Driver, _world: PortableC2) -> bool:
    searched = await driver.call("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "identity"})
    summaries = _commands(searched)
    if not summaries:
        return False
    described = await driver.call(
        "tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": _reference(summaries[0])}
    )
    return described.ok and isinstance(described.data, dict) and "fields" in described.data


@workflow("W06")
async def w06_search_process_command(driver: Driver, _world: PortableC2) -> bool:
    searched = await driver.call("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "process"})
    return searched.ok and bool(_commands(searched))


@workflow("W07")
async def w07_describe_argument_command(driver: Driver, _world: PortableC2) -> bool:
    searched = await driver.call("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "directory"})
    summaries = _commands(searched)
    if not summaries:
        return False
    described = await driver.call(
        "tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": _reference(summaries[0])}
    )
    if not described.ok or not isinstance(described.data, dict):
        return False
    required = {
        str(field.get("name"))
        for field in described.data.get("fields") or []
        if isinstance(field, dict) and field.get("required")
    }
    return "dir" in required


# -- preparation -----------------------------------------------------------


@workflow("W08")
async def w08_prepare_identity_enumeration(driver: Driver, _world: PortableC2) -> bool:
    prepared = await _shell_preparation(driver, "whoami /all")
    if not prepared.ok or not isinstance(prepared.data, dict):
        return False
    return bool(prepared.data.get("preparation_id")) and _continues_to(
        prepared, "tuoni_operation_execute"
    )


@workflow("W09")
async def w09_prepare_required_argument(driver: Driver, _world: PortableC2) -> bool:
    searched = await driver.call("tuoni_commands_search", {"agent_guid": AGENT_GUID, "query": "directory"})
    summaries = _commands(searched)
    if not summaries:
        return False
    reference = _reference(summaries[0])
    await driver.call("tuoni_command_describe", {"agent_guid": AGENT_GUID, "template": reference})
    prepared = await driver.call(
        "tuoni_command_prepare",
        {
            "agent_guid": AGENT_GUID,
            "template": reference,
            "configuration": {"dir": SAFE_ARGUMENT_VALUE},
        },
    )
    return prepared.ok and bool(isinstance(prepared.data, dict) and prepared.data.get("preparation_id"))


@workflow("W10")
async def w10_recover_from_validation_error(driver: Driver, _world: PortableC2) -> bool:
    """Send a shell command the wrong way, then recover using only the hint."""

    rejected = await driver.call(
        "tuoni_command_prepare",
        {"agent_guid": AGENT_GUID, "template": "cmd", "configuration": {"args": "whoami"}},
    )
    if rejected.ok or rejected.code != "INVALID_COMMAND_CONFIGURATION":
        return False
    # The hint has to carry the correction, otherwise the model cannot recover.
    if "shell_command" not in rejected.hint:
        return False
    if rejected.payload.get("retryable") is not False:
        return False
    corrected = await driver.call(
        "tuoni_command_prepare",
        {"agent_guid": AGENT_GUID, "template": "cmd", "shell_command": "whoami"},
    )
    return corrected.ok and bool(isinstance(corrected.data, dict) and corrected.data.get("preparation_id"))


# -- controlled execution --------------------------------------------------


@workflow("W11", allowed_mutations=1)
async def w11_execute_one_preparation(driver: Driver, _world: PortableC2) -> bool:
    prepared = await _shell_preparation(driver, "whoami")
    if not prepared.ok:
        return False
    executed = await driver.follow(prepared)
    if executed is None or not executed.ok or not isinstance(executed.data, dict):
        return False
    return isinstance(executed.data.get("command_id"), int)


@workflow("W12", allowed_mutations=1)
async def w12_replay_execution_receipt(driver: Driver, world: PortableC2) -> bool:
    prepared = await _shell_preparation(driver, "whoami")
    if not prepared.ok or not isinstance(prepared.data, dict):
        return False
    preparation_id = str(prepared.data.get("preparation_id") or "")
    first = await driver.call("tuoni_operation_execute", {"preparation_id": preparation_id})
    if not first.ok or not isinstance(first.data, dict):
        return False
    submissions = len(world.mutating_calls)
    replay = await driver.call("tuoni_operation_execute", {"preparation_id": preparation_id})
    if not replay.ok or not isinstance(replay.data, dict):
        return False
    # The receipt must be repeatable without queueing a second command.
    return replay.data.get("command_id") == first.data.get("command_id") and len(
        world.mutating_calls
    ) == submissions


# -- result reading --------------------------------------------------------


@workflow("W13")
async def w13_poll_pending_command(driver: Driver, _world: PortableC2) -> bool:
    polled = await driver.call("tuoni_command_get", {"command_id": PENDING_COMMAND_ID})
    if not polled.ok or not isinstance(polled.data, dict):
        return False
    pending = str(polled.data.get("status") or "") in {"CREATED", "SENT", "ONGOING"}
    return pending and _continues_to(polled, "tuoni_command_get")


@workflow("W14")
async def w14_read_body_free_manifest(driver: Driver, _world: PortableC2) -> bool:
    manifest = await driver.call("tuoni_command_get", {"command_id": MULTIPART_COMMAND_ID})
    if not manifest.ok:
        return False
    parts = _parts(manifest)
    if len(parts) < 2:
        return False
    # A manifest must describe parts without carrying any body.
    return all("value" not in part for part in parts)


@workflow("W15")
async def w15_fetch_first_result_window(driver: Driver, _world: PortableC2) -> bool:
    window = await _first_text_window(driver, MULTIPART_COMMAND_ID)
    if window is None:
        return False
    part = _part(window)
    return isinstance(part.get("value"), str) and part.get("total_chars", 0) > 0


@workflow("W16")
async def w16_reconstruct_unicode_output(driver: Driver, _world: PortableC2) -> bool:
    window = await _first_text_window(driver, UNICODE_COMMAND_ID)
    if window is None:
        return False
    part = _part(window)
    reconstructed = str(part.get("value") or "")
    guard = 0
    current = window
    while _part(current).get("has_more") and guard < 40:
        guard += 1
        following = await driver.follow(current)
        if following is None or not following.ok:
            return False
        reconstructed += str(_part(following).get("value") or "")
        current = following
    return reconstructed == UNICODE_OUTPUT


@workflow("W17")
async def w17_summarize_before_continuing(driver: Driver, _world: PortableC2) -> bool:
    window = await _first_text_window(driver, LARGE_RESULT_COMMAND_ID)
    if window is None:
        return False
    part = _part(window)
    # A large result must arrive bounded, with an explicit way to continue.
    return bool(part.get("value")) and part.get("has_more") is True and isinstance(
        part.get("next_offset_chars"), int
    )


@workflow("W18")
async def w18_correlate_two_results(driver: Driver, _world: PortableC2) -> bool:
    identity = await _first_text_window(driver, IDENTITY_COMMAND_ID)
    process = await _first_text_window(driver, PROCESS_COMMAND_ID)
    if identity is None or process is None:
        return False
    return "operator" in str(_part(identity).get("value") or "") and "explorer.exe" in str(
        _part(process).get("value") or ""
    )


# -- update and stop preparation -------------------------------------------


@workflow("W19")
async def w19_prepare_interactive_update(driver: Driver, _world: PortableC2) -> bool:
    prepared = await driver.call(
        "tuoni_command_update_prepare",
        {"command_id": INTERACTIVE_COMMAND_ID, "configuration": {"stdin": ["whoami"]}},
    )
    return prepared.ok and bool(isinstance(prepared.data, dict) and prepared.data.get("preparation_id"))


@workflow("W20")
async def w20_prepare_stop(driver: Driver, _world: PortableC2) -> bool:
    prepared = await driver.call("tuoni_command_stop_prepare", {"command_id": STOPPABLE_COMMAND_ID})
    return prepared.ok and bool(isinstance(prepared.data, dict) and prepared.data.get("preparation_id"))


WORKFLOW_PLANS: tuple[tuple[str, Plan], ...] = tuple(_PLANS)
