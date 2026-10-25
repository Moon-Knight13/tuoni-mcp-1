"""The operating skill is loaded into the model's context as instructions.

Anything false in it is not a documentation bug, it is a prompt bug: the model
acts on it. These tests keep the skill's tool names and command names tied to
what the server actually exposes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tuoni_mcp.config import ALL_PROFILES, Settings
from tuoni_mcp.tools import build_registry

SKILL = Path(__file__).parents[1] / ".agents" / "skills" / "operate-tuoni"
# Dispatch-mode adapters are built per-exposure rather than registered, so they
# are not in the registry but are legitimate names for the docs to mention.
DISPATCH_ADAPTERS = {"tuoni_find_tool", "tuoni_tool_schema", "tuoni_call"}


def _all_tool_names() -> set[str]:
    cfg = Settings(
        url="https://example.test:8443",
        username="test",
        password="test",
        verify=False,
        profiles=ALL_PROFILES,
    )
    return {spec.name for spec in build_registry(cfg).specs} | DISPATCH_ADAPTERS


def _skill_docs() -> list[Path]:
    return sorted(SKILL.rglob("*.md"))


def test_the_skill_ships_the_documents_it_links_to() -> None:
    names = {path.name for path in _skill_docs()}

    assert {"SKILL.md", "safety.md", "workflows.md", "command-catalog.md"} <= names


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda path: path.name)
def test_every_tool_the_skill_names_actually_exists(doc: Path) -> None:
    """The skill uses OpenCode's double-prefixed form, e.g. tuoni_tuoni_agents_list."""

    real = _all_tool_names()
    referenced = set(re.findall(r"`(tuoni_tuoni_[a-z_]+)`", doc.read_text(encoding="utf-8")))

    missing = sorted(name for name in referenced if name.removeprefix("tuoni_") not in real)

    assert missing == [], f"{doc.name} names tools that do not exist: {', '.join(missing)}"


def test_the_skill_never_conflates_ending_a_process_with_ending_the_agent() -> None:
    """`die` costs the session; `kill` does not. Every document must keep them apart."""

    for doc in _skill_docs():
        text = doc.read_text(encoding="utf-8")
        if "`kill`" not in text:
            continue
        assert not re.search(r"`die`\s*/\s*`kill`", text), (
            f"{doc.name} presents die and kill as interchangeable"
        )
