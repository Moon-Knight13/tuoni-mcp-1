# Security

Report vulnerabilities through this repository's private vulnerability reporting when enabled.
If it is unavailable, open an issue asking for a private reporting channel without including
credentials, exploitable details, or live agent data.

Security fixes target the latest release. Use the committed lockfile and keep runtime
dependencies patched.
Use uv 0.13.4 or newer when installing or building. If using pip, use 26.2.2 or newer;
older installers may retain compromised CA roots in their own bundled trust stores.

## Trust boundaries

This server holds Tuoni credentials and can perform operations with that account's permissions.
Use a dedicated account restricted to the intended deployment. MCP tool annotations describe
effects; they do not grant or enforce user approval. Configure permissions in the MCP client.

Stdio is the default transport. CLI HTTP transports bind only to loopback, keep the SDK's
Host/Origin protections, and have no MCP authentication layer. Do not expose them with port
forwarding or an unauthenticated proxy. An authenticated remote MCP deployment needs a separately
designed authorization boundary.

TLS verification is enabled by default. Use a trusted CA bundle for private certificates;
an explicit `TUONI_VERIFY_TLS=false` is available for development. HTTP base URLs transmit
credentials without TLS and belong only in controlled development environments.

Read-only mode removes remote mutation and preparation tools after profile expansion. Downloads
write local files even though they do not change C2 state. Preparation is process-local and is
not a durable job queue. After an ambiguous execution, inspect server state instead of resubmitting.

Upstream responses, downloads, caches, preparations, event buffers, and model-facing results have
bounds. Common secret fields and authentication text are redacted, but arbitrary command output
can still contain sensitive evidence. Restrict access to transcripts and downloaded files, and
remove sensitive data before sharing evaluation results.

## Container advisories

CI scans the production image and retains the complete report, including vendor-unfixed
findings. The automated gate rejects HIGH/CRITICAL findings with available fixes, all
unreviewed findings, and all CRITICAL findings. The reviewed
[image policy](scripts/image_risks.json) covers specific unfixed HIGH advisories, exact package
versions, and one pinned base. It expires and must be reviewed again by its `review_before`
date. It is an assessment of the specified deployment, not a claim that every package is patched.

The [runtime check](scripts/check_image.py) binds the assessment to the scanned image ID and
verifies UID/GID 10001, no capabilities, no new privileges, no setuid/setgid files, patched CA
stores, and the absence of the affected optional components. Runtime pip and ensurepip are
removed so their vendored certificates cannot retain a compromised root. The unused infocmp
executable is removed while keeping Debian's essential package installed.

The supplied Docker launchers use stdio, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, and a temporary `/tmp` filesystem. Downloads need an explicit
writable bind mount. The accepted deployment excludes privileged/root containers, privileged
operator execution inside them, added packages, and remote HTTP exposure. Those changes need
a new assessment. Keep the full scan and generated `image-audit-acceptance.json` with release
evidence, and rebuild when fixes become available.
