# Configuration and usage

Start with the [quickstart](../README.md) and your client’s setup guide.
This reference covers profiles, command workflows, and result limits.

- [Startup configuration](#startup-configuration)
- [Default catalog and command workflows](#default-catalog)
- [Additional profiles](#profiles-beyond-agent-operations)
- [Result and error contract](#result-and-error-contract)
- [Event topics](#event-topics)
- [Operational notes](#operational-notes)
- [Artifact directory setup](#artifact-directory-setup)

## Startup configuration

Application Settings in the server are the source of truth. Environment variables are read once
at startup; changing a profile or mode requires restarting the MCP process.

| Variable | Default | Meaning |
|---|---|---|
| `TUONI_URL` | required | Tuoni base URL, for example `https://your-tuoni-server:8443`. |
| `TUONI_USERNAME` | required | Tuoni account name. |
| `TUONI_PASSWORD` | required | Tuoni account password. |
| `TUONI_VERIFY_TLS` | `true` | System CAs by default, a CA-bundle path for a private CA, or explicit `false` for development. |
| `TUONI_MCP_PROFILES` | `agent-ops` | Additive, comma-separated profiles selected at startup. |
| `TUONI_MCP_MODE` | `native` | `native` or experimental `dispatch`. |
| `TUONI_MCP_READ_ONLY` | `false` | Remove preparation, execution, and other mutation tools after profile expansion. |
| `TUONI_REQUEST_TIMEOUT` | `30` | Positive finite network timeout in seconds. Applied to connect/read operations and checked between streamed chunks. Increase for slow upstream operations. |
| `TUONI_MCP_RESPONSE_MAX_BYTES` | `16777216` | Maximum upstream JSON/text response bytes, enforced while reading decompressed data. |
| `TUONI_MCP_DOWNLOAD_DIR` | unset | Absolute local directory for command-result file downloads; requires `artifacts`. |
| `TUONI_MCP_DOWNLOAD_MAX_BYTES` | `134217728` | Maximum download bytes, enforced while streaming. |
| `TUONI_MCP_UPLOAD_DIR` | unset | Existing absolute staging directory for `local_path` uploads on the MCP server; requires `artifacts`. |
| `TUONI_MCP_UPLOAD_MAX_BYTES` | `16777216` | Maximum decoded upload bytes for staged files and base64 upload/replace inputs. |
| `TUONI_MCP_EVENT_BUFFER` | `200` | Maximum retained event count; each item is also redacted and capped at 32 KiB before buffering. |
| `TUONI_MCP_TOOL_PREFIX` | unset | Client-added tool prefix: `tuoni_` for OpenCode or `mcp__tuoni__` for Claude Code with server key `tuoni`. Desktop examples leave it empty. |

Available profiles are `agent-ops`, `access`, `artifacts`, `discovery`, `administration`,
`authoring`, `events`, `raw`, and `all`. Profiles are additive. `all` deliberately excludes `raw`;
select `raw` explicitly, and only with native mode. Events are disabled unless `events` is in the
selected profile set, so the default publishes no event resources.

An unknown profile, an empty profile value, an invalid mode, or `raw` in dispatch mode fails
startup. Use `TUONI_MCP_PROFILES` to select capabilities and enable events.

Read-only filtering happens after profile expansion. The default read-only catalog contains seven
observational tools: `tuoni_capabilities`, `tuoni_agents_list`, `tuoni_agent_get`,
`tuoni_commands_list`, `tuoni_commands_search`, `tuoni_command_describe`, and `tuoni_command_get`.

### Checking configuration and connectivity

`tuoni_capabilities` reports effective profiles, mode, read-only state, TLS configuration,
transport limits, and counts of enabled and withheld operations. It does not contact Tuoni by
default. Set `check_connection:true` to check the backend version and current identity; only the
username and a bounded authority list are returned. No credentials, raw URLs, or local paths
appear in the report. In dispatch mode, discover and call this operation through the router.

The CLI uses the same diagnostics:

```bash
uv run --frozen tuoni-mcp doctor           # authenticated backend checks
uv run --frozen tuoni-mcp doctor --offline # configuration only
```

Both commands print JSON. Exit codes are 0 for successful requested checks, 1 for a failed
backend check, and 2 for invalid configuration. Offline `backend.status:not_checked` does not
assert connectivity. For the MCP tool, inspect `data.ok` and `data.backend.status`: a diagnostic
call can complete successfully while reporting a degraded backend. Failures distinguish
authentication, permissions, TLS, connectivity, timeouts, and incompatible responses.

### Experimental dispatch mode

`TUONI_MCP_MODE=dispatch` exposes a four-tool router after profile filtering:

1. `tuoni_find_tool` returns ranked names, summaries, effect classes, and either `input_schema` or an exact `schema` call.
2. `tuoni_tool_schema` returns one exact schema and usage note.
3. `tuoni_call` performs reads or creates mutation preparations; it never applies a mutation.
4. `tuoni_operation_execute` remains separately visible and state-changing.

`raw` is intentionally unavailable in dispatch mode. With `TUONI_MCP_READ_ONLY=true`,
`tuoni_operation_execute` is removed and dispatch therefore exposes three tools.

Copy discovery calls and their arguments exactly. Discovery cursors expire after five
minutes and belong to the current server process and query; restart without a cursor
after expiry or a server restart. Discovery accepts limits up to 100 and still packs
each page into its 2 KiB response budget.

Discovery recognizes common action forms such as "prepare command execution",
"command preparation", and "command validation". Every meaningful query word must
still match; unknown or negated phrases do not broaden the enabled catalog.

Command search results include callable `describe` links. MCP tool schemas describe
the wrapper inputs; command-template schemas come from `tuoni_command_describe`.
Omitting `agent_guid` from command search returns an agent-selection continuation;
repeat the query with the selected GUID before describing or preparing a command.
Read tools accept a few equivalent inputs: `search` for an agent `query`, `command_id`
for a description's `template`, and canonical decimal strings for `tuoni_command_get` IDs.
`tuoni_agents_list` also accepts `process_name`, an exact case-insensitive basename
filter that combines with `query` when both are supplied.
Conflicting aliases fail validation. Dispatch also accepts client-prefixed read names
and one copied read-call envelope; preparation and execution still require exact inputs.

### Choosing an exposure for a small context

Clients often include the catalog in each model request. Token counts depend on
the model's tokenizer and the client's tool formatting.

| Exposure | Initial tools |
|---|---|
| native, `agent-ops` (default) | 11 |
| native, `agent-ops`, read-only | 7 |
| dispatch, `agent-ops` | 4 |
| dispatch, read-only | 3 |
| native, `all` profiles | 78 |
| dispatch, `all` profiles | 4 |

For current serialized sizes, run
`uv run --frozen --all-extras python evaluation/benchmark_interface.py`.

Server instructions and the optional operating skill also consume context.
Load the skill's references only when needed. A wide native catalog consumes substantial context;
dispatch keeps the initial exposure small and trades that saving for schema-discovery calls.

Native mode is the default. Measure both modes with your client and model before choosing
dispatch for its smaller initial catalog.

For models that miss dispatch entrypoints, load the operating skill or give an explicit
first-step hint using your client's exposed name, for example:
`Start with mcp__tuoni__tuoni_find_tool to locate the needed MCP operation.`

## Default catalog

With `TUONI_MCP_PROFILES` unset, native mode exposes these eleven tools. Diagnostics are shared
by every profile; the other default tools belong to `agent-ops`.

| Tool | Purpose |
|---|---|
| `tuoni_capabilities` | Effective profiles, limits, and missing-capability reasons; optional backend version and identity checks. |
| `tuoni_agents_list` | Paginated compact agent summaries; active agents by default, limit 10, maximum 25. |
| `tuoni_agent_get` | One agent's operational metadata and capabilities, without template dumps. |
| `tuoni_commands_list` | Paginated command history without result bodies; recover existing IDs, optionally by agent or status. |
| `tuoni_commands_search` | Compact agent-compatible command search by intent or name; limit 8, maximum 20. |
| `tuoni_command_describe` | One template's normalized, paginated fields and examples. |
| `tuoni_command_prepare` | Validate a command, configuration, attachments, and execution settings without queueing. |
| `tuoni_command_update_prepare` | Validate an interactive command update without sending it. |
| `tuoni_command_stop_prepare` | Prepare a stop or cancellation without applying it. |
| `tuoni_operation_execute` | Execute an opaque preparation ID; the only state-changing default tool. |
| `tuoni_command_get` | Read command status, a body-free part manifest, or one bounded result window. |

The normal command sequence is:

```text
tuoni_commands_search -> tuoni_command_describe -> tuoni_command_prepare
                      -> tuoni_operation_execute -> tuoni_command_get
```

Use `tuoni_agents_list` with `query` to find a host or process, for example
`{"query":"workstation sample-agent.exe"}`. All query words must match operational identity fields.
Search scans at most 100 agents per call and reports `scanned`, `inventory_total`, and
`search_complete`; an empty page can still have a continuation. Follow `next` until enough
matches are found or `search_complete` is true. Summaries include process/PID and callback
predictions when the backend supplies them.

Preparation IDs are process-local, opaque, and expire after five minutes if unexecuted. Inspect
the preparation summary, then execute it once. A completed receipt can be replayed for ten minutes,
but never automatically retry an execution after an ambiguous server response. The in-memory
store is concurrency-safe and bounded to 128 records or 2 MiB.
Execution revalidates the live agent, selected template, and schema before applying the operation.
Describe examples expand required nested fields and local references within a bounded budget.
`prepare_example_notes` identifies branch choices or unsupported constraints. When
`prepare_example_incomplete:true` is present, inspect the remaining fields before preparing;
the example is a partial editable skeleton, not a validated configuration.

### Reading command output

Call `tuoni_command_get` without `part_index` to poll status and page through a body-free result
manifest. When a result is ready, call it with `part_index`, `offset_chars`, and `max_chars` to
read only a needed text window. Continue from `next_offset_chars`; Unicode character offsets are
stable even when the server reduces a requested window to satisfy the final UTF-8 byte ceiling.
Non-text parts return metadata or resource links, not inline bodies.

Text manifests and windows include `result_version`. Preserve it in the exact returned
continuation. If the visible part changes, `RESULT_CHANGED` rejects the old continuation and
returns a fresh manifest call for the same command. Discard previous windows before restarting.
An initial unversioned text read remains supported and starts a new versioned sequence.

For a status or manifest read, optional `wait_seconds` accepts an integer from 0 to 30 and
defaults to 0. A bounded wait can return an observed pending state with `wait_timed_out:true`;
follow its continuation for the same command ID. A wait timeout does not mean execution failed.
Text-window reads do not support waiting, and execution itself never waits for completion.

Completed results are cached for 60 seconds, with at most 64 records and 8 MiB of retained JSON.
This avoids fetching and redacting the complete result for every window. Larger results stay
uncached. Pending commands always refresh; preparation and execution checks always read live
state. Follow `poll_after_seconds` between pending status calls instead of polling in a tight loop.
For bounded waits in CREATED/SENT, callback estimates can reduce polling. Metadata lookup and
waiting share the total deadline; stale, unavailable, or uncertain estimates use normal backoff.
ONGOING remains a running command and does not imply that a server Job exists.

Summarize each useful window before fetching the next. Do not ingest every result part merely
because it exists.

Use `tuoni_commands_list` to recover an existing command after reconnecting or losing a receipt.
It supports an exact `agent_guid`, an uppercase `status`, and a limit of 10 by default (maximum
25). Follow the exact returned continuation to read older history. If the history changes and
the cursor becomes stale, restart the listing and deduplicate `command_id` values. Inspect an
existing command before considering a replacement after an ambiguous execution.
History responses expose metadata only. Backend pages can still contain result data;
the configured upstream byte limit applies to each request.
`returned_status_counts` counts only this response's commands. It cannot establish the
statuses of unread history records.

### Attachments

Command preparation accepts an existing C2 file ID or an inline object shaped as
`{"field":"...","filename":"...","content_base64":"..."}`. In `agent-ops`, inline data is
limited to 4 KiB after base64 decoding. Put larger content into C2 with the `artifacts` profile,
then prepare the command with the resulting file ID.

With an upload staging directory configured, `tuoni_file_upload` and `tuoni_file_replace`
accept `local_path` instead of `content_base64`. Supply exactly one source. Staged uploads keep
binary bytes out of the model's arguments and respect the configured upload limit. A dispatch
preparation fingerprints the source and rejects changes before execution; successful execution
uses the exact inspected bytes. Existing hosted file IDs remain the attachment mechanism for
agent commands.

Binary downloads are intentionally body-free. `tuoni_file_download`, `tuoni_payload_download`,
and `tuoni_alias_download_file` return bounded metadata plus an authenticated HTTP URL; they do
not fetch a binary or place base64 in model context. Use that URL with a Tuoni-authenticated
client outside the model, or pass an existing hosted-file ID directly to command preparation.

### Finding a command

`tuoni_commands_search` ranks by intent as well as by name. A query like `identity`,
`process list`, `screenshot` or `lateral movement` resolves to the commands that serve it, so a
small model does not have to know that Tuoni spells process enumeration `ps`. An exact or
prefix name match always outranks an intent match, and an intent match never selects a template
on the model's behalf: describe one before preparing it.

The mapping lives in [`src/tuoni_mcp/command_intents.py`](../src/tuoni_mcp/command_intents.py).
Every name in it is a real server-side registration; keep it that way, because suggesting a
command that does not exist teaches a model to hallucinate one that preparation then rejects.
`tests/test_command_intents.py` holds the map to the names listed in the skill's
[command catalog](../.agents/skills/operate-tuoni/references/command-catalog.md) so the two cannot
drift apart.

Tuoni returns command aliases from the same endpoint as built-in commands, so a team's saved
shorthands are searchable here too, ranked on the description whoever created them wrote. The
intent table deliberately maps only built-in names; an alias is found by its own name and
description.

### Flat shell commands

`shell_command` accepts a command as a flat string, so embedded quotes and backslashes never have
to survive nesting inside a JSON configuration object. It applies to any template whose command
string is the only thing it requires, which the server decides from the live schema rather than a
fixed name list: `cmd`, `powershell` and `sh`, and equally `run`, `powerpick` and `mimikatz`.

Templates needing more than a command take a `configuration` object instead: `run-as`
(credentials), `remote-exec-*` and `jump-*` (a target), and anything that declares an attachment
at all. The attachment rule is deliberately conservative - `load-pe`, `load-elf` and `load-dll`
publish an optional file plus a free-form `commandline`, but the plugin rejects the command
without the file, and that rule is not visible in the schema. `tuoni_command_describe` shows which
form applies in its `prepare_arguments_example`.

### Execution settings

Execution settings control plugin execution. Embedded/native commands run inside the agent
and do not use execution settings. Their descriptions return `execution_applicable:false`
and `execution_modes:[]`; omit `execution` entirely, including `SELF`. Supplying it is
rejected with `EXECUTION_NOT_APPLICABLE` before a preparation is stored or a command is queued.
Aliases of embedded commands follow the same rule.

For plugin commands, `execution.execType` selects `SELF`, `NEW` (with `executable`), or
`EXISTING` (with `pid`). `execution.execUnitType` is a separate, optional execution-unit format;
use only a value from `tuoni_command_describe`'s `supported_exec_unit_types`, or omit it so C2
selects a compatible format. For `NEW`, optional `suspended`, `ppid`, and paired
`username`/`password` fields are supported. Credentials are redacted from preparation output.

The first plugin describe page includes `execution_examples`. Copy the selected object into
`execution` and replace its executable path or process ID with the intended target value.
Execution is an object with an `execType` key, for example
`{"execution":{"execType":"NEW","executable":"<executable path>"}}`.
MCP argument validation errors produce field-specific corrections. In dispatch mode,
these errors also return a read-only `next` call to the exact MCP tool schema.

When `tuoni_find_tool` recognizes a command name such as `cmd`, `ls`, or `procinfo`,
it directs the caller to `tuoni_commands_search`. That search selects an agent if needed;
its returned catalog determines which templates are actually available. Tool discovery
does not contact the C2 or assume the named command is installed.

## Profiles beyond agent operations

Additional capabilities are opt-in so they do not consume the default model context:

- `access`: listener and payload access workflows.
- `artifacts`: C2 file and larger attachment workflows.
- `discovery`: host, service, and credential records.
- `administration`: users, jobs, and application settings.
- `authoring`: command-plugin discovery and command-alias authoring.
- `events`: one bounded, cursor-based `tuoni_events_poll` tool; its `topics` schema enumerates all
  supported source URIs and the default publishes no replaying event-resource catalog.
- `raw`: the native-mode-only raw request escape hatch.
- `all`: every non-raw profile.

Discovery lists accept typed selectors, so models can narrow stored records without writing
filter syntax. `tuoni_hosts_list` accepts `address` and `name`; `tuoni_services_list` accepts
`address`, integer `port` (0–65535), and `protocol`; `tuoni_credentials_list` accepts `username`,
`host`, `realm`, and `source`. Each also accepts substring `search` and `include_archived`.
Text selectors match exactly unless `*` is placed at either edge. Use typed selectors or the
raw `filter`, never both. These tools query stored records; credential values remain
redacted.

Listener, command, and payload plugin inventories return compact summaries with callable
`detail` links. Command and payload plugins first list template summaries, then link to one
template's schema/examples. Use `tuoni_listener_plugin_get`,
`tuoni_command_plugin_command_get`, or `tuoni_payload_template_get` for configuration details.
Job summaries link to `tuoni_job_get` for messages/resources.
Details that exceed one response return `value_json` windows: follow
`next`, concatenate the windows, then parse the JSON. Each window re-reads the backend;
`DETAIL_CHANGED` means discard the partial document and follow the returned restart call.
`configuration_schema_redacted:true` flags a schema whose secret literals or constraints
were removed, so it must not be treated as complete validation. Unresolved sensitive schema
references conservatively remove literals; references never trigger external network requests.

## Result and error contract

Lean tools return one compact JSON text item. Successes have the shape
`{"ok":true,"data":...,"next_cursor":null,"next":null}`; a continuation includes the exact next
tool call. Following the
[MCP tool-error contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
tool failures use `isError: true` with a bounded
`{"code","message","retryable","hint"}` object. Results are structurally paginated and measured
as complete serialized UTF-8 `CallToolResult` values; JSON, Unicode, and base64 are never cut in
the middle of a value. Sensitive configuration, credentials, authorization material, and
attachment bodies are redacted from summaries, failures, and logs.

Command history and profile inventory lists include `coverage` with `offset`, `returned`,
`total`, `has_more`, and `complete`. `complete:true` means this response contains the entire
matching inventory; it does not track pages previously read by a client. `total:null` means
the upstream API did not establish a total. Follow `next` until null, combine consistent
pages, and scope aggregate claims to records actually retrieved. Inventory coverage describes
record membership; bounded or redacted fields can still omit detail.

Mutation receipts include an `outcome` with request acknowledgment counts. A definite first-write
rejection preserves its API error (for example `TUONI_FORBIDDEN`) and reports `state:rejected`.
`PARTIAL_OUTCOME` means earlier HTTP writes were acknowledged but the complete operation failed;
`AMBIGUOUS_OUTCOME` means at least one attempted write lacks a definite acknowledgment. Both
prohibit replay. Inspect the error's read-only `next` before deciding how to reconcile state.
Prepared execution retains terminal failures: repeating the same preparation ID returns the
stored failure without submitting another write. These counts describe HTTP acknowledgments,
not a backend transaction or rollback. Transport adapters without request observation remain
conservative about lost receipts.

Omit optional limits unless following a returned continuation. Numeric validation errors
include the allowed bounds and an omission/default hint when a numeric default is available.

List, schema, JSON-window, manifest, and event cursors are short opaque handles. Copy
them unchanged; their state stays in the server registry, with at most 4096 entries
and one hour of inactivity before expiry. They do not survive a server restart and
may be evicted when the store fills. Unknown or altered handles return `INVALID_CURSOR`;
recognized expired handles return `CURSOR_EXPIRED`, and changed query/filter/identity bindings
return `CURSOR_SCOPE_CHANGED`. Once an expired handle has been purged, it is also unknown.
Cursor errors are separate from changes to the upstream value. Actual value changes still
return `SETTING_CHANGED` or `DETAIL_CHANGED`. Follow the error's exact read-only
`next` call to restart and discard earlier windows. Dispatch discovery cursors use
the separate five-minute lifetime described above.
JSON-window reads may change `max_chars` while continuing; the record identity,
fingerprint, and character offset remain bound to the cursor and every response
still fits its byte budget.

A bounded-wait failure can also include `next`, pointing to a read of the same command ID.
Apply its hint before retrying; this continuation never submits a replacement command.

Tool failures are classified rather than flattened. A rejection Tuoni is certain about
(`TUONI_NOT_FOUND`, `TUONI_FORBIDDEN`, `TUONI_API_ERROR`) is returned with `retryable: false` and
the server's own message. A failure that produced no definite answer (`TUONI_SERVER_ERROR`,
`TUONI_TIMEOUT`, `TUONI_UNREACHABLE`) is `retryable: true` for reads and preparations only. After
anything that may already have submitted an operation the same failure is reported as not
retryable, because repeating it could queue the operation twice.

Complete serialized limits are 4 KiB for list/search responses, 6 KiB for detail/describe/prepare,
an 8 KiB absolute maximum for any tool result, and 1 KiB for errors. A command text window defaults
to roughly 4 KiB and is reduced when escaping or multibyte text would exceed the final result
ceiling.

## Event topics

When the explicit `events` profile is selected, `tuoni_events_poll.topics` accepts these exact
source URIs (omit `topics` to poll all sources):

- `tuoni://events/agents/new`, `tuoni://events/agents/heartbeat`,
  `tuoni://events/agents/active`, `tuoni://events/agents/metadata`
- `tuoni://events/commands/sent`, `tuoni://events/commands/results`
- `tuoni://events/listeners/new`, `tuoni://events/listeners/modified`,
  `tuoni://events/listeners/removed`
- `tuoni://events/payloads/new`, `tuoni://events/payloads/archived`
- `tuoni://events/users/new`, `tuoni://events/users/modified`
- `tuoni://events/command-templates/new`, `tuoni://events/command-templates/removed`

Each continuation cursor is scoped to its selected topic set. Keep the returned cursor and reuse
the same topics on the next poll. A first poll defaults to `start:"now"`: call it before an
action, then follow its cursor afterward. To inspect earlier retained events, start a new poll
with `start:"earliest"` and no cursor. Continuations omit `start`; combining it with a cursor
is an argument error. Replay uses only this MCP process's bounded buffer, not durable server history.

`after_sequence`/`through_sequence` describe the inspected interval; `earliest_sequence` and
`latest_sequence` describe the retained snapshot boundaries. `has_more` concerns matching events
in that snapshot. An empty filtered page does not establish an empty buffer. A traffic burst can
overwrite queued events; polls report the
cumulative `dropped_pending_events` count when nonzero. Use command or agent reads to recover
current state when either this count increases or `dropped_before_cursor` is true.

## Operational notes

- Agent list `architecture` is the OS architecture. `process_architecture` is included when a known
  process architecture differs; its absence can mean either a match or unavailable process metadata.
- Prefer read-only discovery before preparation, and inspect each preparation before execution.
- TuoniLib is synchronous; MCP calls are offloaded to a thread pool. Extreme concurrency can
  still saturate that pool.
- Requests have socket timeouts, share a bounded pool of at most eight HTTP sessions, and enforce
  streamed body limits. Cancelling an MCP request cannot undo a command already accepted by Tuoni.
- Configuration schemas must bundle their referenced resources. Preparation never fetches external
  schema URLs or files. TLS certificates need a matching Subject Alternative Name as well as a trusted CA.
- Never blindly retry a non-idempotent operation after a timeout or ambiguous response. Inspect
  the agent and command state first.

## Artifact directory setup

For uploads, create a staging directory and set `TUONI_MCP_UPLOAD_DIR` to its absolute path on
the MCP server. Mount it read-only in Docker. A relative `local_path` resolves inside this
directory; absolute paths must also remain inside it. Symlinks, reparse points, traversal, and
nonregular files are rejected. The default upload limit is 16 MiB and also applies to base64
upload/replace inputs. Local uploads remain unavailable when the staging directory is unset.

To inspect an agent command's file result, add the `artifacts` profile and configure
`TUONI_MCP_DOWNLOAD_DIR` as an absolute directory bind-mounted into the MCP container at the same
path. `tuoni_command_get` then returns an exact continuation to `tuoni_command_file_download`,
which authenticates to C2 and saves the bytes outside model context. Allow OpenCode read-only
`external_directory` access to that one path and configure permissions for newly enabled
artifact tools. Use the flat `shell_command` preparation field whenever describe's example
offers it, so embedded quotes are not nested inside a JSON configuration object.

The mounted directory must be writable by UID/GID 10001. The supplied launchers use a read-only
root filesystem, drop capabilities, disable new privileges, and provide a temporary `/tmp`.
Keep downloads in an explicit writable bind mount. Overriding the container user changes the
deployment covered by the image assessment and requires a new review. Mount private CA bundles read-only and
set `TUONI_VERIFY_TLS` to their container path. Downloads use exclusive filenames, restrictive
permissions, and SHA-256 verification before reusing an existing file.
