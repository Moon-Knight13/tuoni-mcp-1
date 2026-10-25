"""Generate the human tool reference from the offline, canonical ToolRegistry.

Run with --check in CI to reject documentation drift without writing files.
No environment configuration, credentials, backend, or network is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tuoni_mcp.config import ALL_EXPANSION, ALL_PROFILES, DEFAULT_PROFILES, Profile, Settings  # noqa: E402
from tuoni_mcp.registry import Effect, ToolSpec, build_dispatch_specs  # noqa: E402
from tuoni_mcp.tools import build_registry  # noqa: E402


def _names(specs: tuple[ToolSpec, ...]) -> str:
    return ", ".join(f"`{spec.name}`" for spec in specs) or "None"


def _tool_reference(spec: ToolSpec, *, readonly_spec: ToolSpec | None = None) -> list[str]:
    wire = spec.wire_tool()
    schema = wire.inputSchema
    required = ", ".join(f"`{name}`" for name in schema.get("required", []))
    alternatives = schema.get("anyOf", [])
    if alternatives and all(set(branch) == {"required"} for branch in alternatives):
        choices = " or ".join(" and ".join(f"`{name}`" for name in branch["required"]) for branch in alternatives)
        required = f"{required}; {choices}" if required else choices
    required = required or "None"
    if not spec.profiles:
        effect = f"`{spec.effect.value}`"
        if readonly_spec is not None and readonly_spec.effect is not spec.effect:
            effect += f"; `{readonly_spec.effect.value}` in read-only mode"
        availability = "yes" if readonly_spec is not None else "no"
        details = f"Effect: {effect}. Read-only mode: **{availability}**."
    else:
        availability = "yes" if spec.effect is Effect.READ else "no"
        dispatch = (
            "unavailable" if not spec.dispatch_visible else
            "execute entrypoint" if spec.effect is Effect.EXECUTE else
            "prepared through `tuoni_call`" if spec.effect is Effect.MUTATE else
            "called through `tuoni_call`"
        )
        if Profile.RAW in spec.profiles:
            dispatch = "unavailable (raw is native-only)"
        profiles = ", ".join(f"`{profile.value}`" for profile in sorted(spec.profiles, key=lambda p: p.value))
        details = (
            f"Profiles: {profiles}. Native effect: `{spec.effect.value}`. "
            f"Read-only mode: **{availability}**. Dispatch: {dispatch}."
        )
    return [
        f"### `{spec.name}`", "", wire.description or "", "", details, "",
        f"Required arguments: {required}. Response budget: {spec.budget:,} bytes.", "",
        "<details>", "<summary>Exact input schema</summary>", "", "```json",
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True),
        "```", "", "</details>", "",
    ]


def render_reference() -> str:
    # Construct Settings directly: never load .env files or the caller's values.
    cfg = Settings(
        url="https://reference.invalid", username="reference", password="unused",
        verify=True, profiles=ALL_PROFILES,
    )
    registry = build_registry(cfg)
    native = registry.select(DEFAULT_PROFILES, read_only=False)
    native_readonly = registry.select(DEFAULT_PROFILES, read_only=True)
    dispatch = build_dispatch_specs(registry, native, read_only=False)
    dispatch_readonly = build_dispatch_specs(registry, native_readonly, read_only=True)
    readonly_by_name = {spec.name: spec for spec in dispatch_readonly}
    defaults = ", ".join(f"`{profile.value}`" for profile in sorted(DEFAULT_PROFILES, key=lambda p: p.value))
    expanded = ", ".join(f"`{profile.value}`" for profile in Profile if profile in ALL_EXPANSION)
    lines = [
        "# Tool reference", "",
        "Generated from `ToolRegistry`; edit the source definitions, then run:", "",
        "```bash", "uv run python scripts/generate_tool_reference.py",
        "uv run python scripts/generate_tool_reference.py --check", "```", "",
        "This reference describes static MCP tools, not the connected server's dynamic command catalog. "
        "Schemas below are the exact advertised input schemas. No backend is contacted to generate this file.", "",
        "## Entry points", "",
        f"The default profile is {defaults}. `TUONI_MCP_MODE` selects native or dispatch; "
        "`TUONI_MCP_READ_ONLY=true` removes preparation and state-changing operations.", "",
        "| Mode | Read-only | Exposed default entrypoints |",
        "| --- | --- | --- |",
        f"| Native | No | {_names(native)} |",
        f"| Native | Yes | {_names(native_readonly)} |",
        f"| Dispatch | No | {_names(dispatch)} |",
        f"| Dispatch | Yes | {_names(dispatch_readonly)} |", "",
        "Native mode exposes each selected tool directly. Dispatch mode discovers the selected tools "
        "through `tuoni_find_tool`, retrieves one schema with `tuoni_tool_schema`, and routes calls through "
        "`tuoni_call`. Mutations submitted through `tuoni_call` produce preparations; "
        "`tuoni_operation_execute` executes them. Follow returned continuations exactly.", "",
        "## Profiles", "",
        "Set `TUONI_MCP_PROFILES` to comma-separated profile names. "
        f"`all` expands to {expanded}. It excludes `raw`, which requires explicit selection and native mode.", "",
        "Read-only availability below means exposure when `TUONI_MCP_READ_ONLY=true`, "
        "not a guarantee of upstream account permissions. Native effects are `read`, `prepare` "
        "(local validation/preparation), `execute` (a prepared operation), or `mutate` "
        "(immediate remote change).", "",
        "| Profile | Native tools | Read-only native tools | Included in `all` |",
        "| --- | ---: | ---: | --- |",
    ]
    for profile in Profile:
        total = len(registry.select({profile}, read_only=False))
        readonly = len(registry.select({profile}, read_only=True))
        lines.append(f"| `{profile.value}` | {total} | {readonly} | {'Yes' if profile in ALL_EXPANSION else 'No'} |")
    lines += ["", "## Dispatch entrypoint schemas", ""]
    for spec in dispatch:
        lines += _tool_reference(spec, readonly_spec=readonly_by_name.get(spec.name))
    for profile in Profile:
        lines += [f"## `{profile.value}` tools", ""]
        for spec in sorted(registry.select({profile}, read_only=False), key=lambda s: s.name):
            lines += _tool_reference(spec)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "tool-reference.md")
    parser.add_argument("--check", action="store_true", help="Fail if the reference is missing or differs; do not write")
    args = parser.parse_args(argv)
    expected = render_reference()
    try:
        if args.check:
            # Git may check out Markdown with CRLF on Windows. Compare logical
            # text while keeping generated files canonical UTF-8 with LF.
            if not args.out.is_file() or args.out.read_text(encoding="utf-8") != expected:
                print("Tool reference is missing or stale; run scripts/generate_tool_reference.py", file=sys.stderr)
                return 1
            print(f"Tool reference verified: {args.out}")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(expected.encode("utf-8"))
            print(f"Tool reference generated: {args.out}")
    except OSError as exc:
        print(f"Tool reference failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
