"""The intent table is what lets a small model search in its own words."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tuoni_mcp.command_intents import INTENT_TEMPLATES, matched_templates
from tuoni_mcp.tools.commands import _template_rank


def _template(name: str, *, description: str = "", plugin: str = "shelldot.commands.native") -> dict:
    return {
        "id": f"template-{name}",
        "name": name,
        "pluginId": plugin,
        "qualifiedName": f"{plugin}:global:{name}",
        "description": description,
    }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("identity", "cmd"),
        ("whoami", "powershell"),
        ("enumerate the current user", "cmd"),
        ("process list", "ps"),
        ("proclist", "ps"),
        ("running programs", "ps"),
        ("list files", "ls"),
        ("screen capture", "screenshot"),
        ("scan ports", "portscan"),
        ("dump credentials", "mimikatz"),
        ("lateral movement", "jump-winrm"),
        ("kill agent", "die"),
        ("kill process", "kill"),
    ],
)
def test_operator_phrasing_reaches_a_real_template(query: str, expected: str) -> None:
    assert expected in matched_templates(query)


def test_bare_kill_stays_ambiguous_between_process_and_agent() -> None:
    """`kill` ends a process and `die` ends the agent; guessing can lose access."""

    matched = matched_templates("kill")

    assert "die" not in matched
    assert "kill" not in matched


@pytest.mark.parametrize(
    "query",
    [
        "kill the runaway process on the agent",
        "terminate agent process",
        "kill a process and keep the agent alive",
    ],
)
def test_killing_the_agent_is_never_offered_for_a_process_request(query: str) -> None:
    """Bag-of-words matching would offer `die` here, and `die` costs the session."""

    matched = matched_templates(query)

    assert "die" not in matched, f"{query!r} must not suggest terminating the agent"


def test_a_phrase_must_match_as_an_adjacent_run() -> None:
    assert "ps" in matched_templates("give me a process list")
    # "process" and "list" are present but not adjacent in that order.
    assert matched_templates("list every agent and its process") >= {"ps", "procinfo"}
    assert "die" not in matched_templates("list every agent and its process")


@pytest.mark.parametrize("query", ["duplicate this file", "find the location", "allocate memory"])
def test_a_single_word_phrase_does_not_fire_inside_a_longer_word(query: str) -> None:
    """A raw substring test matched "cat" inside "dupli*cat*e"."""

    assert "download" not in matched_templates(query)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("list the files", "ls"),
        ("who is the current user", "cmd"),
        ("take a screenshot of the desktop", "screenshot"),
        ("terminate the agent", "die"),
    ],
)
def test_filler_words_do_not_break_a_phrase(query: str, expected: str) -> None:
    """Adjacency keeps matching safe; dropping filler keeps it usable."""

    assert expected in matched_templates(query)


def test_ending_the_agent_needs_an_unambiguous_request() -> None:
    """`die` costs all access, so it is only offered when nothing else fits."""

    assert matched_templates("kill agent") == frozenset({"die"})
    # Naming a process anywhere withdraws it, even next to "agent".
    assert "die" not in matched_templates("kill a process and keep the agent alive")
    assert "kill" in matched_templates("kill a process and keep the agent alive")


def test_unrelated_text_matches_nothing() -> None:
    assert matched_templates("please do something entirely unrelated") == frozenset()
    assert matched_templates("") == frozenset()


def test_intent_lifts_a_template_that_substring_search_cannot_reach() -> None:
    """The exact W04 failure: no template mentions "identity" anywhere."""

    cmd = _template("cmd", description="Run a command in cmd.exe on Windows.")

    without_intent = _template_rank(cmd, "identity")
    with_intent = _template_rank(cmd, "identity", matched_templates("identity"))

    assert without_intent[0] == 0
    assert with_intent[0] == 70


def test_an_operator_written_alias_is_searchable_like_any_command() -> None:
    """Tuoni returns aliases from /api/v1/command-templates alongside built-ins
    (CommandTemplateService.getCommandTemplateStream(includeAliases=true)), so a
    team's saved shorthand has to rank on the description they wrote for it. The
    intent table maps only built-in names and must not shadow one."""

    alias = {
        "id": "alias-psx",
        "name": "psx",
        "pluginId": "shelldot.commands.native",
        "qualifiedName": "shelldot.commands.native:global:psx",
        "description": "Our standard process listing for triage.",
    }

    # Found by the words the operator actually wrote.
    assert _template_rank(alias, "process listing")[0] > 0
    assert _template_rank(alias, "triage")[0] > 0
    # And by its own short name, which still wins outright.
    assert _template_rank(alias, "psx")[0] == 100
    # An intent that maps to built-ins does not demote it below zero or crash.
    assert _template_rank(alias, "process list", matched_templates("process list"))[0] >= 0


def test_an_exact_name_still_outranks_an_intent_match() -> None:
    intents = matched_templates("ps")
    exact = _template_rank(_template("ps", description="List processes."), "ps", intents)
    intent_only = _template_rank(_template("procinfo", description="Own process."), "ps", intents)

    assert exact[0] == 100
    assert exact[0] > intent_only[0]


def test_every_mapped_name_looks_like_a_real_template() -> None:
    """Suggesting a name Tuoni does not register teaches the model to hallucinate."""

    for phrase, templates in INTENT_TEMPLATES.items():
        assert templates, f"{phrase} maps to nothing"
        for name in templates:
            assert name == name.strip().casefold(), f"{phrase} -> {name} is not a bare template name"
            assert " " not in name, f"{phrase} -> {name} is not a bare template name"


def test_every_mapped_name_is_documented_in_the_command_catalog() -> None:
    """The map and the catalog reference must not drift apart.

    Every name suggested by intent search must have a documented command entry.
    """
    catalog = (
        Path(__file__).parents[1]
        / ".agents"
        / "skills"
        / "operate-tuoni"
        / "references"
        / "command-catalog.md"
    ).read_text(encoding="utf-8")
    documented = set(re.findall(r"`([a-z0-9][a-z0-9-]*)`", catalog))
    mapped = {name for names in INTENT_TEMPLATES.values() for name in names}

    undocumented = sorted(mapped - documented)

    assert undocumented == [], (
        "these templates can be surfaced by intent search but are absent from "
        f"references/command-catalog.md: {', '.join(undocumented)}"
    )


def test_the_catalog_does_not_reintroduce_names_that_do_not_exist() -> None:
    """These were listed as real commands before; none of them are registrations."""

    catalog = (
        Path(__file__).parents[1]
        / ".agents"
        / "skills"
        / "operate-tuoni"
        / "references"
        / "command-catalog.md"
    ).read_text(encoding="utf-8")
    table = catalog.split("### Names that do not exist")[0]
    documented = set(re.findall(r"`([a-z0-9][a-z0-9-]*)`", table))

    assert documented.isdisjoint({"proclist", "pwd", "cat", "jump-smb", "getuid", "netstat"})
