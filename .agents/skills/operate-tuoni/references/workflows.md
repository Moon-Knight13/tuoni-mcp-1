# Tuoni operation workflows in OpenCode

Use the double-prefixed names OpenCode exposes. Make one dependent call at a time, and preserve
every returned continuation argument exactly.

## Stand up a listener

1. Invoke `tuoni_tuoni_listener_plugins_list`; select a summary and follow its `detail` call
   to `tuoni_tuoni_listener_plugin_get` for the configuration schema and examples.
2. Invoke `tuoni_tuoni_listener_create` with that ID, matching `configuration`, and an optional
   name. Attach files only when the live plugin schema requires them.
3. Invoke `tuoni_tuoni_listener_start` with the returned listener ID.
4. Verify with `tuoni_tuoni_listeners_list` or `tuoni_tuoni_listener_get`.

Reconfigure with `tuoni_tuoni_listener_update`; use the exact listener ID when stopping or deleting it.
If `configuration_schema_redacted` is true, secret literals or constraints were omitted;
do not treat that schema as complete validation.

## Create a payload

1. Invoke `tuoni_tuoni_payload_plugins_list`; choose a returned template and read its schema.
2. Invoke `tuoni_tuoni_payload_create` with that exact template, a listener ID, matching
   configuration, and the requested encryption setting.
3. Invoke `tuoni_tuoni_payload_download` for bounded metadata and an authenticated download URL.
   Retrieve the binary with an authenticated HTTP client outside model context.

Payload builds can be slow. Do not infer failure from a short client-side delay or resubmit an
ambiguous build.

## Obtain an agent callback

Observe callbacks with `tuoni_tuoni_agents_list` or, when the explicit events profile is enabled,
`tuoni_tuoni_events_poll`.

## Run a command

Follow the exact workflow in the parent skill:

1. `tuoni_tuoni_commands_search`
2. `tuoni_tuoni_command_describe`
3. `tuoni_tuoni_command_prepare`; use top-level `shell_command` whenever describe's example shows it
4. Check the preparation's target, command, and configuration
5. `tuoni_tuoni_operation_execute` exactly once
6. `tuoni_tuoni_command_get` without `part_index` for status/manifest
7. `tuoni_tuoni_command_get` with `part_index`, `offset_chars`, and `max_chars` for one text window
8. For a file part, follow its exact `tuoni_tuoni_command_file_download` call, then use OpenCode
   `read` on the returned `local_path`. A result `resource_path` is not an MCP resource URI.

Use `tuoni_tuoni_command_update_prepare` for an interactive update and
`tuoni_tuoni_command_stop_prepare` for cancellation; review each preparation before executing it.

## Poll events

Enable the explicit `events` profile and invoke `tuoni_tuoni_events_poll`. Omit `topics` for all
sources or choose exact values from the tool schema. Keep the same topic set and copy the returned
cursor arguments verbatim for the next call.
The first poll defaults to starting now: subscribe before the action. After an action, use
`start:"earliest"` without a cursor to replay retained events, then follow `next` unchanged.
An empty page can reflect filtering or the starting point; inspect `earliest_sequence` and
`latest_sequence`. Replay is bounded process-local evidence and cannot recover overwritten events.

## Discovery data

- List with `tuoni_tuoni_hosts_list`, `tuoni_tuoni_services_list`, or
  `tuoni_tuoni_credentials_list`; preserve pagination arguments from returned continuations.
- Add with `tuoni_tuoni_host_add`, `tuoni_tuoni_service_add`, or
  `tuoni_tuoni_credential_add`.
- Update or archive with the corresponding double-prefixed tool and exact record ID.

## Users, jobs, and settings

- Users: start with `tuoni_tuoni_users_list`; use the exact double-prefixed create/update tool
  exposed by the selected profile.
- Jobs: start with `tuoni_tuoni_jobs_list`; follow a summary's `detail` call to
  `tuoni_tuoni_job_get` for messages and resources. Use the returned job ID for restart, pause, or resume.
- Settings: read with `tuoni_tuoni_settings_get_all` or `tuoni_tuoni_setting_get`; use the
  exact key and value when updating.

Job and listener-plugin details normally return structured records. For oversized records,
follow `next`, concatenate `value_json` windows, then parse JSON. Each window re-fetches
the backend record; these are not cached snapshots. `DETAIL_CHANGED` means discard the
earlier windows and follow the returned restart call. Repeating the old cursor cannot work.

## Bulk operations

Select an explicit target list. For each agent, use the command preparation and execution
workflow above and collect bounded results. Stop after an ambiguous submission and inspect state before continuing.
