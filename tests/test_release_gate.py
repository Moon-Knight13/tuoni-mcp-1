"""Release identity must agree with package metadata and public release notes."""
from __future__ import annotations

import json

import pytest

from scripts.check_release import ReleaseCheckError, check_release, main

VERSION = "0.0.1"
COMMIT = "a" * 40


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(f"## {VERSION}\nInitial public release.\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
    return tmp_path


def check(project, **changes):
    arguments = {"root": project, "version": VERSION, "commit": COMMIT}
    return check_release(**{**arguments, **changes})


@pytest.mark.parametrize("commit", ["a" * 40, "b" * 64])
def test_release_reports_exact_version_and_commit_without_publishing(project, commit):
    assert check(project, commit=commit) == {"version": VERSION, "commit": commit, "publish": False}


@pytest.mark.parametrize("commit", ["a" * 7, "a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40])
def test_release_rejects_abbreviated_or_malformed_commit_identity(project, commit):
    with pytest.raises(ReleaseCheckError, match="exact full release commit"):
        check(project, commit=commit)


def test_release_version_must_match_package_metadata(project):
    with pytest.raises(ReleaseCheckError, match="version does not match"):
        check(project, version="9.9.9")


@pytest.mark.parametrize("notes", ["## Unreleased\n", "## 0.0.10\n", "0.0.1\n"])
def test_release_requires_notes_for_the_exact_public_version(project, notes):
    (project / "CHANGELOG.md").write_text(notes, encoding="utf-8")
    with pytest.raises(ReleaseCheckError, match="no release notes"):
        check(project)


@pytest.mark.parametrize("license_text", [None, "", " \n"])
def test_release_requires_a_nonempty_license(project, license_text):
    license_path = project / "LICENSE"
    if license_text is None:
        license_path.unlink()
    else:
        license_path.write_text(license_text, encoding="utf-8")
    with pytest.raises(ReleaseCheckError, match="missing its license"):
        check(project)


def test_release_cli_reports_validation_failure_without_traceback(project, monkeypatch, capsys):
    monkeypatch.setattr("scripts.check_release.ROOT", project)
    assert main(["--version", VERSION, "--commit", "bad"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exact full release commit" in captured.err
    assert "Traceback" not in captured.err


def test_release_cli_accepts_explicit_commit_outside_git(project, monkeypatch, capsys):
    monkeypatch.setattr("scripts.check_release.ROOT", project)
    assert main(["--version", VERSION, "--commit", COMMIT]) == 0
    assert json.loads(capsys.readouterr().out) == {"version": VERSION, "commit": COMMIT, "publish": False}
