# Claude Code

Complete the [installation and connection steps](../../README.md#quickstart),
including `doctor`. Keep those connection variables in the terminal you use below.

## Connect

From the `tuoni-mcp` clone, run:

```sh
export TUONI_MCP_REPO="$PWD"
claude --mcp-config "$TUONI_MCP_REPO/docs/claude-code/local.json" \
  --append-system-prompt-file "$TUONI_MCP_REPO/.agents/skills/operate-tuoni/SKILL.md"
```

Windows PowerShell:

```powershell
$env:TUONI_MCP_REPO = $PWD.Path
claude --mcp-config "$env:TUONI_MCP_REPO/docs/claude-code/local.json" --append-system-prompt-file "$env:TUONI_MCP_REPO/.agents/skills/operate-tuoni/SKILL.md"
```

Approve the server when Claude Code asks. Use `/mcp` to check its status, then try:

> Check the Tuoni connection and list active agents.

The operating instructions guide Claude through discovery, preparation, execution,
and result reads. You can launch from another workspace once `TUONI_MCP_REPO`
contains the absolute clone path.

## Choose a configuration

Replace `local.json` in the launch command with one of these:

| File | Use it for |
| --- | --- |
| [local.json](local.json) | Normal command workflow |
| [read-only.json](read-only.json) | Browse agents, command history, and existing results |
| [dispatch.json](dispatch.json) | Smaller initial tool catalog; schemas loaded on demand |
| [docker.json](docker.json) | Linux Docker Engine; build the image first |

For a persistent project setup, merge the selected file's `mcpServers.tuoni`
entry into your workspace's `.mcp.json`. Keep existing servers. Launch `claude`
with the same connection variables and operating instructions.

### Docker on Linux

Build the image using the [Docker steps](../../README.md#docker-linux), then select
`docker.json`. Keep `-i` for stdio;
do not add a TTY or detach the container. The example uses Linux host networking.
Use the local configuration on macOS/Windows unless Docker Desktop can reach your API.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Server fails to start | Check `/mcp`. Confirm `TUONI_MCP_REPO` is an absolute path and `uv` is on `PATH`, or use its full path in `command`. |
| MCP connects, API calls fail | Run the quickstart's `doctor` command in the same terminal. |
| Certificate error | Set `TUONI_VERIFY_TLS=false` before launching Claude for a self-signed lab server. |
| Wrong tools or duplicate servers | Check `/mcp` and your project/user MCP configurations. Add `--strict-mcp-config` to use only the supplied file. |

The examples use terminal variables, with no automatic `.env` loading. Their
`TUONI_MCP_TOOL_PREFIX=mcp__tuoni__` matches the server key `tuoni`; update both
if you rename it. Client permissions control confirmations. The read-only
configuration removes mutation tools at the server.

For certificate verification, set `TUONI_VERIFY_TLS=true` or an absolute CA-bundle
path. Docker CA mounts and other settings are covered in [configuration and usage](../usage.md#artifact-directory-setup).

Official reference: [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).
