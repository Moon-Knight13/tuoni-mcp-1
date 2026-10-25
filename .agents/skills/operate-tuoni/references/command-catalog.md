# Command catalog and config schemas

## The catalog is dynamic

Commands are provided by server plugins, so the exact set and their configuration vary per
server and per agent. **Always query the live schema** rather than hardcoding:

- `tuoni_tuoni_commands_search` (compact, agent-scoped discovery), followed by
- `tuoni_tuoni_command_describe` (one complete agent-compatible schema).

The lean search tool never returns bulk schemas. Narrow by query and describe only the selected
template.

Each template returns:
- the exact template identity to pass to `tuoni_tuoni_command_prepare`;
- normalized, paginated configuration/update fields;
- ready-to-adapt examples. Start from these.

## Search by intent, not by guessing a name

`tuoni_tuoni_commands_search` accepts plain intent words and maps them to the commands that
actually serve them. Search `identity`, `process list`, `screenshot`, `lateral movement` or
`dump credentials` rather than inventing a template name. Every name below is a real
registration; anything not listed here almost certainly does not exist.

## Real command names (starting points)

Native commands ship with the native-commands plugin; the rest come from server plugins, so
availability varies per server and per agent. Use the id/name from the live template list.

| Real name | Kind | Purpose |
|---|---|---|
| `ls`, `cd`, `chmod` | native | Filesystem navigation and permissions |
| `cmd`, `powershell`, `sh` | native | Run a shell command |
| `run`, `run-as` | native | Run an executable, optionally as another user |
| `ps` | native | Process enumeration |
| `jobs` | native | List commands running inside the agent |
| `sleep`, `sleep-until` | native | Change the callback interval |
| `shutdown` | native | Shut down or reboot the target machine |
| `die` | native | Terminate the **agent**, ending its session |
| `token-list`, `token-steal`, `token-make`, `token-use` | native | Token and impersonation operations |
| `privilege-list`, `privilege-enable`, `privilege-disable` | native | Token privileges |
| `bof` | native | Run a Beacon Object File |
| `download`, `upload`, `cp`, `mv`, `rm`, `mkdir`, `rmdir`, `timestomp` | plugin (fs) | File operations |
| `procinfo`, `inject`, `spawn`, `execute-assembly`, `powerpick`, `screenshot` | plugin (os) | Post-exploitation primitives |
| `jump-ssh`, `jump-wmi`, `jump-winrm`, `jump-service` | plugin (os) | Lateral movement |
| `remote-exec-ssh`, `remote-exec-wmi`, `remote-exec-winrm`, `remote-exec-service` | plugin (os) | Run a command on another host |
| `portscan`, `socks5`, `rportfwd` | plugin (net) | Network scanning and pivoting |
| `mimikatz`, `keylogger`, `webcam`, `screen-tracker` | plugin (enterprise) | Credential and surveillance collection |
| `kill`, `suspend` | plugin (enterprise) | Terminate or suspend **another process** by pid |
| `load-pe`, `load-elf`, `load-dll`, `amsi-bypass` | plugin (enterprise) | In-memory execution |
| `memory-layout` | plugin (enterprise) | Show a process's memory layout |
| `env-var-set`, `env-var-unset` | plugin (enterprise) | Set or remove an environment variable (there is no read) |

### Names that do not exist

There is no `pwd`, `cat`, `proclist`, `id`, `whoami`, `shell`, `getuid`, `netstat`, or
`jump-smb`. Use `ps` for a process list, `download` to read a file, and `cmd`/`sh` to run
`whoami`. `connect-smb` and `connect-tcp` connect to a bind relay agent; they are not lateral
movement.

`kill` and `die` are different commands. `die` terminates the agent and costs you access;
`kill` terminates some other process by pid. Never substitute one for the other.

## Reading a schema

The described schema is JSON-schema-like, but some plugins do not mark operationally required
fields. Copy property names exactly and never assume a generic `args` field exists. Preparation
rejects invalid configuration, misplaced attachments, schema-declared missing fields, and missing
required attachments before any command is queued; correct the call rather than retrying it
unchanged.

When a template needs nothing but a command string, pass it as top-level `shell_command`. That
covers `cmd`, `powershell`, `sh`, `run`, `powerpick` and `mimikatz`. The flat string avoids
nested-JSON quote failures. Never use a generic `args` field.

Describe tells you which form applies: its `prepare_arguments_example` shows `shell_command` when
the flat form is accepted, and a `configuration` object otherwise. Templates that need more than a
command - `run-as` (credentials), `remote-exec-*` and `jump-*` (a target), `execute-assembly` (a
file) - always use `configuration`.

## BOFs and assemblies

`bof` / `execute-assembly` take a binary payload plus arguments in `configuration`. Command
preparation accepts existing C2 file IDs or inline
`{field, filename, content_base64}` attachments. Inline data is limited to 4 KiB decoded in
`agent-ops`; use the `artifacts` profile first for anything larger. Check the described template
for exact field names.
