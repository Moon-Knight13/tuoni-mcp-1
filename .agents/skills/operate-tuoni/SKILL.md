---
name: operate-tuoni
description: Operate a live Tuoni C2 through MCP, including OpenCode. Use for agent command execution, bounded result collection, and additive C2 workflows; exclude offline data-directory analysis.
---

# Operate Tuoni

Use the tool names your client exposes. OpenCode with server key `tuoni` adds a
`tuoni_` prefix; its default native tools are:

- `tuoni_tuoni_capabilities`: inspect enabled profiles and limits; backend checks are optional.
- `tuoni_tuoni_agents_list`: select an agent and copy its guid.
- `tuoni_tuoni_agent_get`: inspect one agent.
- `tuoni_tuoni_commands_list`: recover existing command IDs from bounded history.
- `tuoni_tuoni_commands_search`: discover compatible commands by intent or name.
- `tuoni_tuoni_command_describe`: read exact fields and a preparation example.
- `tuoni_tuoni_command_prepare`: validate without queueing.
- `tuoni_tuoni_command_update_prepare`: prepare input for a running command.
- `tuoni_tuoni_command_stop_prepare`: prepare a stop.
- `tuoni_tuoni_operation_execute`: execute one reviewed preparation.
- `tuoni_tuoni_command_get`: poll, read a manifest, or read a text window.

Claude Code with server key `tuoni` instead exposes names such as
`mcp__tuoni__tuoni_agents_list`; set `TUONI_MCP_TOOL_PREFIX=mcp__tuoni__` there.
Desktop examples leave the prefix empty. Match names against the client's catalog.
With the matching `TUONI_MCP_TOOL_PREFIX`, returned continuations are already callable.
Otherwise select the visible tool with the matching suffix. Copy
`next.arguments` verbatim; never edit returned IDs, cursors, or offsets.
Omit optional limits unless following a returned continuation.

In dispatch mode, the OpenCode tools are `tuoni_tuoni_find_tool`, then `tuoni_tuoni_tool_schema`,
then `tuoni_tuoni_call`. The inner `name` is the returned operation name without
the client prefix, such as `tuoni_command_get`. The executor remains a direct
`tuoni_tuoni_operation_execute` call. Read-only mode removes preparation and
execution operations; use the available reads only.

## Command workflow

1. List agents, optionally using `query` for a host or process, then copy the selected `guid`.
   Follow search continuations even after an empty page until enough matches are found.
2. Search narrowly, for example `identity`, `process list`, or an exact name.
   Use a returned short name when unique; use the full returned ID if ambiguous.
3. Describe that template. Search summaries are not schemas. Read additional
   field pages only when needed for this command.
4. Copy `prepare_arguments_example` and replace its example values. If it
   offers `shell_command`, put shell text in that flat field. Otherwise use the
   exact described `configuration` fields. Do not invent `args` or `parameters`.
   Execution settings control plugins. Embedded/native commands report
   `execution_applicable:false` and no execution modes; omit `execution` for them.
   For plugins, keep `execution.execType` (SELF, NEW, EXISTING) separate from the
   optional describe-listed `execution.execUnitType`. Copy a describe-listed
   `execution_examples` object into `execution`, replacing its path or PID.
5. Compare the preparation summary with the selected agent, command,
   configuration, attachments, and execution mode. Execute its opaque ID once,
   using the returned continuation. Preparing alone never reaches the agent.
6. Follow the returned command ID to poll. SENT, CREATED, and ONGOING are pending.
   Wait at least `poll_after_seconds` (normally one second) between polls.
   Status/manifest reads may use integer `wait_seconds` from 0 to 30. An observed
   pending result with `wait_timed_out:true` is not a failure; follow the same ID.
   After 20 status calls, report that the command is still pending.
7. Get the body-free manifest before text. For text, follow the manifest's
   `next`, or send `command_id`, the listed `part_index`, `offset_chars:0`,
   `max_chars:4096`, and its `result_version`. Do not add manifest `cursor` or `limit` arguments.
   On `RESULT_CHANGED`, discard earlier windows and follow the fresh manifest continuation.
   Summarize each useful window before fetching another. Stop when enough
   evidence is collected; follow the exact continuation when more is needed.

Make dependent calls sequentially and inspect each result before continuing.
Discover values instead of reconstructing IDs or guessing schema fields.

## Errors and execution

A `retryable:false` error repeats identically with unchanged arguments. Apply
its hint once, choose another approach, or stop. For `retryable:true`, wait and
retry at most a few times. Never loop indefinitely.
MCP argument validation errors in dispatch mode include a read-only schema
continuation; follow it before correcting the failed call. An error continuation
never executes an operation.

Treat `AMBIGUOUS_OUTCOME` as possibly executed: inspect state and never execute
that preparation again. A receipt can be replayed, but creating another command
to recover from an ambiguous execution can duplicate the operation.
`PARTIAL_OUTCOME` also prohibits replay: earlier writes were accepted but the operation
did not finish. Follow the error's inspection call and reconcile the existing state.
A definite rejected write preserves its API error and `outcome.state:rejected`;
repeating that preparation returns its stored rejection without another request.
Use command history, optionally filtered by agent or status, to recover an ID.
If a history cursor becomes stale, restart and deduplicate by `command_id`.
History `returned_status_counts` covers the returned page only. Inventory `coverage.complete`
means the entire inventory is in this response; `total:null` means unknown. Follow pages
before making inventory-wide claims. Cursor scope errors require the previous exact next
call or a new search without cursor; an expired cursor requires restarting.

For missing capabilities, inspect `tuoni_capabilities` before assuming an operation
is unsupported. It is offline by default; `check_connection:true` adds read-only
backend checks. Inspect `data.ok` and `data.backend.status`; `not_checked` does not
mean connected. Dispatch mode discovers this capability through its router.

## File results

A file part marked `not_mcp_resource:true` is an authenticated HTTP path.
With `artifacts` and a configured download directory, follow its download call
to `tuoni_tuoni_command_file_download`, then inspect the returned `local_path`
with your client's file reader. Hosted-file tools do not retrieve command
result parts. Keep evidence unchanged and bytes outside model context.

## References

Read [execution notes](references/safety.md) for agent and process lifetimes.
Read [references/workflows.md](references/workflows.md) for additive listener,
payload, event, discovery, administration, or bulk work. Read
[references/command-catalog.md](references/command-catalog.md) only for command
orientation; the live description is authoritative.
