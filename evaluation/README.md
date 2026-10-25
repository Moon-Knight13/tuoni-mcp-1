# Evaluation

The deterministic suite measures native and dispatch tool contracts against a
synthetic Tuoni server. An optional model probe explores tool use with a chosen
model endpoint. Neither needs live C2 credentials.

## Deterministic workflows

```bash
uv run --frozen --all-extras python evaluation/run_portable_eval.py
uv run --frozen --all-extras python evaluation/run_portable_eval.py --out run.json --report report.json
```

The twenty [workflow plans](portable/plans.py) run in both modes over the real
in-memory MCP transport. They cover agent and command discovery, schema details,
preparation and validation recovery, execution receipts, pending commands,
multipart and Unicode results, interactive updates, and stopping commands.
All state changes use a deterministic fake C2.

The scripted client uses identifiers and examples returned by tools and follows
`next` calls verbatim. Every workflow must complete with schema-valid calls,
bounded results, zero context overflows, and no unintended mutations.

CI compares the measured metrics with [the baseline](portable/baseline.json):
completion, call count, returned bytes, catalog and instruction bytes, request
bytes, and upstream reads. Tests also disable selected affordances to check that
the dependent workflows fail. A passing scripted run measures the interface's
contract; it does not establish a minimum reliable model size.

Refresh the baseline only after reviewing the behavior and measured costs:

```bash
uv run --frozen --all-extras python evaluation/run_portable_eval.py --update-baseline
```

The runner rejects incomplete, invalid, oversized, or unsafe results before it
writes a baseline.

## Interface benchmark

```bash
uv run --frozen --all-extras python evaluation/benchmark_interface.py
```

This reports serialized tool-catalog sizes and warm-path timings with synthetic
data. Timings depend on hardware; CI gates deterministic byte and request counts
through the workflow baseline.

## Model probe

```bash
uv run --frozen --all-extras python evaluation/run_model_probe.py --base-url http://127.0.0.1:8080/v1 --model your-model-name --out model-probe.json
```

The endpoint must support chat tool calls. Set `MODEL_EVAL_API_KEY` if the
endpoint requires a key. The default probes both modes; use `--mode native` or
`--mode dispatch` to select one. `--max-steps` bounds calls and `--timeout` bounds
model requests.

The three cases find an agent, read an existing identity result, and prepare,
execute, and read one synthetic identity command. Grading checks retrieved
evidence and simulated mutations as well as the final answer. Reports include
calls, tool errors, request/result bytes, and provider-reported prompt tokens
when available. Keep reports outside the repository and repeat a probe under the
model's intended context limit before relying on its results.

## Extended coding-client smoke cases

[`extended/cases.py`](extended/cases.py) defines ten bounded tasks covering all 78
operations enabled by the non-raw profiles. They exercise listener and payload
lifecycles, hosted-file replacement, discovery CRUD, users/settings/jobs, alias
replacement IDs, agent administration, interactive updates/stops, events, result
file downloads, long Unicode/JSON windows, and lost submission responses.

The [`ExtendedC2`](extended/world.py) fixture uses real SDK adapters with a synthetic
request boundary. Unknown endpoints fail explicitly. Recorded writes and resulting
state provide evidence independent of a model's final answer. The deterministic
fixture checks run with:

```bash
uv run --frozen --all-extras pytest -q tests/test_extended_fixture.py
```

To attach a coding client's MCP configuration, use the fixture's stdio entrypoint:

```bash
mkdir -p /tmp/tuoni-client-probe
uv run --frozen --all-extras python evaluation/extended/server.py dispatch listeners mcp__tuoni__ /tmp/tuoni-client-probe
```

Select the case, mode, and the client's continuation prefix (`mcp__tuoni__` for
Claude/Codex or `tuoni_` for OpenCode). Use that case's prompt with a fresh client
session; the server itself requires no credentials and never connects to a C2.
`--catalog PATH` accepts a separately captured template catalog for isolated
describe/prepare probes. Keep captured catalogs and client transcripts outside Git.

Grade requested actions and retrieved evidence separately from tool coverage.
For example, inspecting one compatible listener may satisfy a task without listing
all listeners; invoking a preparation alone does not establish a completed mutation.
Check full interval coverage for requested complete text reads, allowing consistent
overlaps. Distinguish first-attempt validity, recovery, intentional rejections,
timeouts, skipped tasks, extra mutations, and narrative claims unsupported by data.
One successful session per model is sampled smoke coverage, not a reliability rate.

## Connected operator scenarios

[`operator_scenarios/cases.py`](operator_scenarios/cases.py) adds six multi-stage handovers with independent
state, retrieval, and final-claim grading:

- Inventory audit: complete filtered history, older failures hidden behind recent
  successful pages, exact status counts, and discovery-cursor scope recovery.
- Attribution under concurrency: identical commands from two operators, a lost
  response, candidate result inspection, and explicit uncertainty instead of a guessed ID.
- Multi-agent triage: same-host process decoys, platform-specific shells, a lost
  accepted response, pending inventory, a targeted stop, and event corroboration.
- Evidence collection: changing command and JSON versions, full current-version
  coverage, Unicode facts, multipart results, file integrity, and untrusted text.
- Temporary workspace cleanup: lost upload response, stable hosted-file identity,
  percent/Unicode paths, alias replacement IDs, and restoration of prior settings.
- Partial permissions: transient reads, a denied operation, independent work,
  accurate partial reporting, and preservation of unrelated records.

The server uses the installed MCP runtime and real SDK adapters against isolated
fixture transport. It never connects to a C2 or starts a target process:

```bash
mkdir -p /tmp/tuoni-operator-probe
uv run --frozen --all-extras python evaluation/operator_scenarios/server.py dispatch triage mcp__tuoni__ /tmp/tuoni-operator-probe
uv run --frozen --all-extras pytest -q tests/test_operator_fixture.py
```

Use `COMMON + CASES[case].prompt` for a fresh coding-client session. Keep the client's
`run.json`, `events.jsonl`, `mcp.jsonl`, `final.txt`, and OpenCode `session-export.json`
beside the fixture's `world.json`. The wire log uses request/response records with
`direction`, `time`, and `message`. Group each session under `ROOT/CASE/CLIENT/`:

```bash
uv run --frozen --all-extras python -m evaluation.operator_scenarios.report /path/to/ROOT --out /path/to/report.json
```

Expected injected errors are evidence of recovery work, not automatic failures.
Grade accepted state changes, all requested current-version intervals, artifact
hashes, and handover facts independently. JSON grouping and stdout line endings
do not change a fact's meaning. A correct final answer does not establish complete
retrieval; a successful tool call does not establish truthful reporting. Validate
fixture assumptions against the real server before attributing a failure to MCP.
