# OpenCode

Complete the [installation and connection steps](../../README.md#quickstart),
including `doctor`. Keep those connection variables in the terminal you use below.
Your existing model and provider settings apply.

## Connect

From the `tuoni-mcp` clone, run:

```sh
export TUONI_MCP_REPO="$PWD"
export OPENCODE_CONFIG="$TUONI_MCP_REPO/docs/opencode/local.jsonc"
opencode mcp list
opencode
```

Windows PowerShell:

```powershell
$env:TUONI_MCP_REPO = $PWD.Path
$env:OPENCODE_CONFIG = "$env:TUONI_MCP_REPO/docs/opencode/local.jsonc"
opencode mcp list
opencode
```

Try this first:

> Check the Tuoni connection and list active agents.

Use `/models` to select your configured model. The configuration loads operating
instructions for discovery, preparation, execution, and result reads.
You can launch from another workspace once `TUONI_MCP_REPO` contains the absolute clone path.

## Choose a configuration

Replace `local.jsonc` in `OPENCODE_CONFIG` with one of these:

| File | Use it for |
| --- | --- |
| [local.jsonc](local.jsonc) | Normal command workflow |
| [read-only.jsonc](read-only.jsonc) | Browse agents, command history, and existing results |
| [dispatch.jsonc](dispatch.jsonc) | Smaller initial tool catalog; schemas loaded on demand |
| [docker.jsonc](docker.jsonc) | Linux Docker Engine; build the image first |

For a persistent setup, merge the selected file's `mcp`, `instructions`, and
`permission` entries into your workspace's `opencode.jsonc` or global
`~/.config/opencode/opencode.json`. Keep your providers and other settings.
OpenCode merges configuration sources, including `OPENCODE_CONFIG`; check for
duplicate `mcp.tuoni` entries if the effective settings differ.

### Docker on Linux

Build the image using the [Docker steps](../../README.md#docker-linux), then select
`docker.jsonc`. Keep `-i` for stdio;
do not add a TTY or detach the container. The example uses Linux host networking.
Use the local configuration on macOS/Windows unless Docker Desktop can reach your API.

## Permissions and troubleshooting

The writable examples allow reads and preparation, then ask before execution.
Keep the exact `tuoni_tuoni_operation_execute` rule after `tuoni_*` because the
last matching rule wins. Adding optional profiles can expose other mutation
tools; review their permissions. The read-only configuration removes mutation tools.

| Problem | What to do |
| --- | --- |
| Server fails to start | Run `opencode mcp list`. Confirm `TUONI_MCP_REPO` is an absolute path and `uv` is on `PATH`, or use its full path in `command`. |
| MCP connects, API calls fail | Run the quickstart's `doctor` command in the same terminal. |
| Certificate error | Set `TUONI_VERIFY_TLS=false` before launching OpenCode for a self-signed lab server. |
| Model stalls before using tools | Check the model provider's connectivity and tool-call support; its timeout is separate from MCP. |

The examples read terminal variables, with no automatic `.env` loading. Keep the
operating skill in `instructions`. Their `TUONI_MCP_TOOL_PREFIX=tuoni_` matches
the server key `tuoni`; update both and the permission rules if you rename it.

For certificate verification, set `TUONI_VERIFY_TLS=true` or an absolute CA-bundle
path. Docker CA mounts and other settings are covered in [configuration and usage](../usage.md#artifact-directory-setup).

Official reference: [OpenCode MCP configuration](https://opencode.ai/docs/mcp-servers/).
