# Tuoni MCP

Connect Claude Code, Claude Desktop, or OpenCode to Tuoni C2 through
[MCP](https://modelcontextprotocol.io/). Discover agents, run commands, and read results
from your AI client.

## Quickstart

You need Git, Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/)
0.13.4+, and access to Tuoni Server 0.17.0+.

### 1. Install

```bash
git clone https://github.com/shell-dot/tuoni-mcp.git
cd tuoni-mcp
uv sync --frozen --no-dev
```

### 2. Set your connection

Replace the URL and credentials below. These examples disable TLS certificate checks
for self-signed lab servers, so no certificate files are needed. HTTPS still encrypts
traffic, but the server's identity is not verified. Use this setup on a trusted network.

**Linux / macOS**

```bash
export TUONI_URL='https://your-tuoni-server:8443'
export TUONI_USERNAME='operator'
export TUONI_PASSWORD='replace-with-your-password'
export TUONI_VERIFY_TLS=false

uv run --frozen --no-dev tuoni-mcp doctor
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
$env:TUONI_URL = 'https://your-tuoni-server:8443'
$env:TUONI_USERNAME = 'operator'
$env:TUONI_PASSWORD = 'replace-with-your-password'
$env:TUONI_VERIFY_TLS = 'false'

uv run --frozen --no-dev tuoni-mcp doctor
```

</details>

When `doctor` reports `"ok": true`, your API connection is ready.

### 3. Connect your AI client

Follow your client's guide for a configuration you can copy. Launch terminal clients
from the same terminal where you set the variables above. Claude Desktop uses a connection file.

| Client | Setup guide |
| --- | --- |
| Claude Code | [Setup](docs/claude-code/README.md) |
| Claude Desktop | [Setup for macOS and Windows](docs/claude-desktop/README.md) |
| OpenCode | [Setup](docs/opencode/README.md) |

Try this first:

> Check the Tuoni connection and list active agents.

## Optional settings

- **Browse without making changes:** set `TUONI_MCP_READ_ONLY=true` in your MCP configuration.
- **Smaller tool catalog:** set `TUONI_MCP_MODE=dispatch` in your MCP configuration.
- **Verify certificates:** set `TUONI_VERIFY_TLS=true`, or use a trusted CA-bundle path.

See [configuration and usage](docs/usage.md) for additional tools, profiles, and workflows.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Certificate error | Set `TUONI_VERIFY_TLS=false` in the client's configuration or connection file, then reconnect. |
| MCP connects, but Tuoni calls fail | Run the `doctor` command above with the same connection settings; check the URL, credentials, and account permissions. |
| `uv` is not found | Restart your terminal after installing it. Desktop clients may need the full path to `uv`. |

## Docker (Linux)

With Docker Engine installed, build the image from the clone:

```bash
docker build --target production -f scripts/docker/Dockerfile -t tuoni-mcp:local .
```

Choose the Docker configuration in your client guide and keep the connection
variables from step 2. The client starts the container for you.

## More information

- [Tool reference](docs/tool-reference.md)
- [Security](SECURITY.md)
- [Development and tests](CONTRIBUTING.md)
- [Model evaluation](evaluation/README.md)
- [Changelog](CHANGELOG.md) · [MIT license](LICENSE)
