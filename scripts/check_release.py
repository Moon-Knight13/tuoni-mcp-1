"""Validate release metadata for an exact source commit.

Package, dependency, transport, and container checks run separately in CI.
This check produces an identity report and never publishes artifacts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development extra
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


class ReleaseCheckError(ValueError):
    pass


def check_release(*, root: Path, version: str, commit: str) -> dict:
    root = root.resolve()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if version != project["version"]:
        raise ReleaseCheckError("Requested release version does not match pyproject.toml")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise ReleaseCheckError("An exact full release commit is required")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(r"^##\s+" + re.escape(version) + r"(?:\s|$)", changelog, re.MULTILINE):
        raise ReleaseCheckError("Changelog has no release notes for this version")
    license_path = root / "LICENSE"
    if not license_path.is_file() or not license_path.read_text(encoding="utf-8").strip():
        raise ReleaseCheckError("Release is missing its license")
    return {"version": version, "commit": commit, "publish": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", help="Full Git commit; defaults to this checkout's HEAD")
    args = parser.parse_args(argv)
    try:
        commit = args.commit or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        report = check_release(root=ROOT, version=args.version, commit=commit)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
