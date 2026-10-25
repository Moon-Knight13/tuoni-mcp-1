# Contributing

Tuoni MCP prioritizes reliable tools in a small model context. Preserve the compact default
catalog; put specialized capabilities in additive profiles. Keep advanced schemas available
through description and dispatch rather than forcing every client to load them upfront.

## Repository scope

`src/` is the runtime shipped in the wheel. `docs/` contains client setup and the current
usage/tool references; `.agents/` contains the optional operating skill. `tests/`,
`evaluation/`, and `scripts/` maintain behavioral, performance, and release checks.
They remain in the source distribution so contributors can reproduce those checks.

Keep one maintained guide per topic and keep user examples provider-independent.
Do not commit local reports, credentials, downloaded artifacts, or comparison snapshots.

The local `.venv/` contains installed dependencies and can be much larger than the
tracked source. It and Python caches are ignored by Git and excluded from release
archives. Run `make clean-local` to remove that environment, caches, and build
outputs; `uv sync --frozen --all-extras` recreates the development environment.

## Development

Clone this repository, install Python 3.10 or newer and uv 0.13.4 or newer, then run:

```bash
uv sync --frozen --all-extras
uv run --frozen --all-extras ruff check src tests evaluation scripts
uv run --frozen --all-extras python scripts/generate_tool_reference.py --check
uv run --frozen --all-extras pytest -q
uv run --frozen --all-extras python evaluation/run_portable_eval.py
uv build
```

Tests use synthetic data and need no C2 credentials. Live integration tests are opt-in.
Real credentials belong in a protected environment or ignored `scripts/env/override.env`,
never fixtures or transcripts.

With `TUONI_URL`, `TUONI_USERNAME`, `TUONI_PASSWORD`, and the appropriate
`TUONI_VERIFY_TLS` already set, run the observational integration suite:

```bash
TUONI_LIVE=1 uv run --frozen --all-extras pytest -q tests/integration/test_live.py
```

It checks native and dispatch diagnostics, agent/template discovery, existing command
history, and bounded job/plugin inventory pages. It does not submit agent commands or
change backend records. Model-driven synthetic probes are documented in
[`evaluation/README.md`](evaluation/README.md); passing live API checks alone does not
establish model task performance.

The lockfile resolves public packages. Restore the locked environment before final validation
after testing any local dependency changes.

## Interface changes

After changing tool definitions, regenerate the [tool reference](docs/tool-reference.md)
with `uv run --frozen python scripts/generate_tool_reference.py`. CI and the release
workflow reject documentation drift. Generation is offline and does not load credentials.

- Prefer one exact, actionable continuation over instructions requiring an agent to rebuild a call.
- Preserve strict input validation, explicit mutation effects, redaction, and full-result byte limits.
- Keep command mutations behind preparation and execution. Never replay ambiguous submissions.
- Page complete structural items and sanitize whole text parts before applying offsets.
- Add tests for observable behavior, especially pagination, concurrency, failure classification,
  and mutation handling. Avoid tests that merely copy implementation details.

The portable baseline gates completion, valid calls, hops, returned bytes, catalog bytes,
instructions, request bytes, and upstream reads. Refresh it only after understanding the change:

```bash
uv run --frozen --all-extras python evaluation/run_portable_eval.py --update-baseline
```

Describe the problem, resulting behavior, measured costs, and relevant validation in a pull request.
The scripted evaluation is not evidence that a particular model can operate the server reliably.

## Release preparation

Update the package version and matching changelog entry, then refresh the lockfile.
Run the development checks above from the committed release snapshot. Validate the intended
deployment using its configured Tuoni account before distributing the configuration.
Keep scan reports and deployment transcripts in CI artifacts or external review evidence.

CI checks Python compatibility, Windows, standalone wheels, dependency advisories, and containers.
Both the frozen runtime and dependencies resolved by a fresh wheel installation are audited;
installing a wheel does not apply this repository's lockfile. The release workflow also checks
that the lockfile matches the release metadata before using frozen installs.
Installed wheels and production containers must also pass real stdio initialization, input
validation, authenticated reads, and exact pagination continuations in both modes. The smoke
check runs outside the checkout against its own synthetic upstream:

```bash
uv run --frozen --all-extras python scripts/smoke_install.py --executable /absolute/path/to/tuoni-mcp
# Linux Docker, using host networking for the temporary synthetic upstream:
uv run --frozen --all-extras python scripts/smoke_install.py --docker-image tuoni-mcp:local
```

The manual **Release artifacts** workflow checks the requested version against package metadata
and the changelog, audits dependencies, verifies distribution contents, and produces artifacts
named for the version and exact commit. It creates artifacts without publishing a package.
The version check can also run locally:

```bash
uv run --frozen --all-extras python scripts/check_release.py --version 0.0.1
```

Model usability measurements are optional and remain separate from the technical release checks.
Record the actual client, model, context limit, and workload when making model-specific claims.

Docker bases and GitHub Actions are pinned by digest or commit. Refresh them deliberately, inspect
the publisher's release, and rerun package, container, and dependency checks after updates.
The pinned image scanner blocks HIGH/CRITICAL advisories with available fixes and retains the full
report, including vendor-unfixed findings. `scripts/check_image.py` also verifies the actual runtime
and checks each remaining HIGH advisory against `scripts/image_risks.json`; unknown, expired,
CRITICAL, or changed-version findings fail. Refresh that assessment whenever the base or runtime
changes. The generated `image-audit-acceptance.json` records the scanned image ID, policy/report
hashes, checked deployment, and reasons for every assessed finding. Keep it with the full scan;
a passing gate does not mean every installed package is patched.
Repository visibility, private vulnerability reporting, and registry publishing are configured by maintainers.
