"""Reject distributions missing a license or carrying local secrets/cache files."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile


_LOCAL_DIRECTORIES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".tox", "build", "dist",
}


def _check_name(name: str, path: Path) -> None:
    entry = PurePosixPath(name)
    if entry.is_absolute() or ".." in entry.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path in {path.name}")
    if any(part in _LOCAL_DIRECTORIES or part.endswith(".egg-info") for part in entry.parts):
        raise ValueError(f"Local cache or checkout metadata found in {path.name}")
    if entry.suffix in {".pyc", ".pyo"} or entry.name == ".coverage" or entry.name.startswith(".coverage."):
        raise ValueError(f"Generated Python files found in {path.name}")
    example_env = entry.name == ".env.example" or entry.name.endswith(".env.example")
    local_env = entry.name.endswith(".env") or entry.name.startswith(".env.")
    if (local_env and entry.name != "default.env" and not example_env) or entry.suffix in {".pem", ".key", ".p12", ".pfx"}:
        raise ValueError(f"Potential local credentials found in {path.name}")


def check(path: Path) -> None:
    license_found = False
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                _check_name(member.filename, path)
                member_type = stat.S_IFMT(member.external_attr >> 16)
                if member_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError(f"Archive links and special files are not permitted in {path.name}")
                license_found |= (member_type != stat.S_IFDIR and not member.is_dir()
                                  and PurePosixPath(member.filename).name == "LICENSE")
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                _check_name(member.name, path)
                if not member.isfile() and not member.isdir():
                    raise ValueError(f"Archive links and special files are not permitted in {path.name}")
                license_found |= member.isfile() and PurePosixPath(member.name).name == "LICENSE"
    if not license_found:
        raise ValueError(f"License is missing from {path.name}")
    print(f"Distribution contents verified: {path.name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    wheels = list(args.directory.glob("*.whl"))
    sources = list(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("Expected exactly one wheel and source distribution")
    for path in wheels + sources:
        check(path)


if __name__ == "__main__":
    main()
