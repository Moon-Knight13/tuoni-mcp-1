# Claude Desktop

Complete the [installation step](../../README.md#1-install). Desktop uses a
connection file, so you do not need the quickstart's terminal exports.

## 1. Create your connection file

Copy [tuoni.env.example](tuoni.env.example) to a file outside the clone, for example:

- macOS: `/Users/YOU/.config/tuoni-mcp/tuoni.env`
- Windows: `C:/Users/YOU/AppData/Local/Tuoni/tuoni.env`

Edit the URL, username, and password. The example sets `TUONI_VERIFY_TLS=false`
for self-signed lab servers; no certificate files are needed. Keep the file
accessible only to your account (`chmod 600` on macOS).

## 2. Edit the configuration

Choose [macos.json](macos.json) or [windows.json](windows.json). Replace three paths:

| Setting | Replace it with |
| --- | --- |
| `command` | Full path to `uv`; find it with `command -v uv` on macOS or `(Get-Command uv).Source` in PowerShell. |
| Argument after `--directory` | Full path to your `tuoni-mcp` clone. |
| Argument after `--env-file` | Full path to your connection file. |

Use literal absolute paths. On Windows, forward slashes work in JSON; backslashes
must be doubled. Desktop does not expand shell variables in these examples.

Before opening Desktop, check the connection with the same paths:

```sh
/Users/YOU/.local/bin/uv --directory /Users/YOU/src/tuoni-mcp \
  run --frozen --no-dev --env-file /Users/YOU/.config/tuoni-mcp/tuoni.env \
  tuoni-mcp doctor
```

PowerShell:

```powershell
& 'C:/Users/YOU/.local/bin/uv.exe' --directory 'C:/Users/YOU/src/tuoni-mcp' run --frozen --no-dev --env-file 'C:/Users/YOU/AppData/Local/Tuoni/tuoni.env' tuoni-mcp doctor
```

Look for `"ok": true`.

## 3. Connect

Open **Settings → Developer → Edit Config**. Merge the edited example's
`mcpServers.tuoni` entry into that file, keeping existing servers. Save valid JSON,
fully quit Claude Desktop, and reopen it.

Try this first:

> Check the Tuoni connection and list active agents.

Desktop controls tool confirmations. For browsing only, use
[read-only.json](read-only.json); for a smaller initial tool catalog, use
[dispatch.json](dispatch.json). Edit their three paths as above; both ship with
macOS placeholders. Read-only mode removes mutation tools at the server.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Server fails to start | Check all three paths and run the doctor command above. Desktop may have a different `PATH` from your terminal. |
| No tools after editing | Check JSON syntax and fully restart Desktop. Check your organization's policy if local MCP is unavailable. |
| Certificate error | Set `TUONI_VERIFY_TLS=false` in the connection file and restart Desktop. Existing process variables override the file. |
| Tool names do not match | Leave `TUONI_MCP_TOOL_PREFIX` empty; Desktop may add its own namespace. |

<details>
<summary>Connection-file paths containing spaces</summary>

`uv` requires a backslash before each space in the `--env-file` value, in addition
to JSON escaping. For example:

```json
"--env-file", "C:/Users/Ada\\ Lovelace/AppData/Local/Tuoni/tuoni.env"
```

This applies only to the `--env-file` value. See [uv's path handling](https://github.com/astral-sh/uv/pull/15815).

</details>

To verify certificates, set `TUONI_VERIFY_TLS=true` or an absolute CA-bundle path.
See [configuration and usage](../usage.md) for additional capabilities.

Official reference: [Desktop local MCP setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers).
