"""Prove the portable evaluation can still fail.

A scripted harness is only worth its runtime if breaking the interface breaks
it. These are mutation tests: each one disables a specific affordance a small
model depends on and asserts that the workflows which depend on it stop
completing. Without them the harness could silently decay into asserting its own
script, and a green CI run would mean nothing.

Only native mode is exercised; dispatch shares the same handlers and doubles the
runtime for no extra signal.
"""

from __future__ import annotations

import asyncio

import pytest

from evaluation.portable.runner import run_mode


def _failing(mode: str = "native") -> set[str]:
    records, _catalog = asyncio.run(run_mode(mode))
    return {record["workflow_id"] for record in records if not record["completed"]}


@pytest.fixture(scope="module")
def unmutated() -> set[str]:
    return _failing()


def test_nothing_fails_before_a_mutation(unmutated: set[str]) -> None:
    assert unmutated == set()


def test_removing_intent_search_breaks_discovery(monkeypatch) -> None:
    """W04 and W05 ask for a command in an operator's words, not Tuoni's."""

    import tuoni_mcp.tools.commands as commands

    monkeypatch.setattr(commands, "matched_templates", lambda query: frozenset())

    assert _failing() == {"W04", "W05"}


def test_removing_continuations_breaks_every_multi_step_workflow(monkeypatch) -> None:
    """`next` is the memory substitute; without it a chain cannot be followed."""

    import tuoni_mcp.protocol as protocol
    import tuoni_mcp.registry as registry

    original = protocol.success_result

    def without_next(data, **kwargs):
        return original(data, **{**kwargs, "next_call": None})

    monkeypatch.setattr(protocol, "success_result", without_next)
    monkeypatch.setattr(registry, "success_result", without_next)

    assert _failing() == {"W08", "W11", "W13", "W16"}


def test_removing_the_flat_shell_form_breaks_preparation(monkeypatch) -> None:
    import tuoni_mcp.tools.commands as commands

    monkeypatch.setattr(commands, "_shell_command_field", lambda template: None)

    assert _failing() == {"W08", "W10", "W11", "W12"}


def test_a_manifest_that_leaks_bodies_is_caught(monkeypatch) -> None:
    """The manifest exists to describe result parts without carrying them."""

    import tuoni_mcp.tools.commands as commands

    original = commands._part_summary

    def leaky(part, index, **kwargs):
        summary = original(part, index, **kwargs)
        if summary.get("type") == "text":
            summary["value"] = (part.get("value") or "")[:200]
        return summary

    monkeypatch.setattr(commands, "_part_summary", leaky)

    assert _failing() == {"W14"}


def test_an_error_hint_that_stops_being_actionable_is_caught(monkeypatch) -> None:
    """W10 recovers from a rejection using only what the hint told it."""

    import tuoni_mcp.tools.commands as commands

    original = commands._validate_configuration

    def hintless(template, configuration, fields, *, update=False):
        try:
            original(template, configuration, fields, update=update)
        except commands.ToolFailure as exc:
            raise commands.ToolFailure(exc.code, exc.message, hint="") from None

    monkeypatch.setattr(commands, "_validate_configuration", hintless)

    assert _failing() == {"W10"}
