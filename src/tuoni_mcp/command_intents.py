"""Map an operator's phrasing onto Tuoni command template names.

Command search ranks templates by substring containment, which works only when
the caller already knows Tuoni's vocabulary. A small model does not: asked to
"enumerate the current user" it searches for ``identity`` or ``whoami``, and
neither string appears in any template name or description, so a perfectly
capable catalog looks empty.

This table closes that gap deterministically. It boosts ranking only; it never
selects a template on the model's behalf, so discovery still ends with the model
describing one exact command before preparing it.

Every name below is a real registration in ``tuoni-server``. Keep it that way:
suggesting a template that does not exist teaches a model to hallucinate a name
that command preparation will then reject. The authoritative lists are
``server/native-command/.../NativeCommandsPlugin.java`` and the command plugins
under ``plugin/``.
"""

from __future__ import annotations

import re
from functools import lru_cache

_WORD = re.compile(r"[a-z0-9]+")

# Shell templates are the general-purpose answer to a great many enumeration
# intents, because Tuoni has no dedicated "whoami" or "netstat" command.
_SHELLS = ("cmd", "powershell", "sh")

INTENT_TEMPLATES: dict[str, tuple[str, ...]] = {
    # Identity and session enumeration - the intent with no matching template name.
    "identity": _SHELLS,
    "whoami": _SHELLS,
    "who am i": _SHELLS,
    "current user": _SHELLS,
    "logged in": _SHELLS,
    "logged in user": _SHELLS,
    "username": _SHELLS,
    "user enumeration": _SHELLS,
    "groups": _SHELLS,
    "privileges": ("privilege-list", "privilege-enable", "privilege-disable"),
    "privilege": ("privilege-list", "privilege-enable", "privilege-disable"),
    "elevate": ("privilege-enable", "run-as"),
    # Shell execution.
    "shell": _SHELLS,
    "command": _SHELLS,
    "run command": _SHELLS,
    "execute command": _SHELLS,
    "terminal": _SHELLS,
    "bash": ("sh",),
    "unmanaged powershell": ("powerpick",),
    "powershell without spawning": ("powerpick",),
    # `run` launches an executable directly, rather than through a shell.
    "launch": ("run",),
    "start program": ("run",),
    "execute binary": ("run",),
    "run binary": ("run",),
    # Processes.
    "process": ("ps", "procinfo"),
    "processes": ("ps", "procinfo"),
    "process list": ("ps",),
    "process enumeration": ("ps",),
    "proclist": ("ps",),
    "task list": ("ps",),
    "tasklist": ("ps",),
    "running programs": ("ps",),
    "process info": ("procinfo",),
    "own process": ("procinfo",),
    "kill process": ("kill",),
    "terminate process": ("kill",),
    "suspend process": ("suspend",),
    "memory layout": ("memory-layout",),
    # Filesystem.
    "directory": ("ls", "cd"),
    "folder": ("ls", "cd"),
    "list files": ("ls",),
    "browse": ("ls",),
    "change directory": ("cd",),
    "working directory": ("cd",),
    "read file": ("download",),
    "get file": ("download",),
    "exfiltrate": ("download",),
    "exfil": ("download",),
    "cat": ("download",),
    "write file": ("upload",),
    "put file": ("upload",),
    "copy file": ("cp",),
    "move file": ("mv",),
    "rename": ("mv",),
    "delete file": ("rm",),
    "remove file": ("rm",),
    "create directory": ("mkdir",),
    "make directory": ("mkdir",),
    "remove directory": ("rmdir",),
    "permissions": ("chmod",),
    "timestamps": ("timestomp",),
    "timestomp": ("timestomp",),
    # Collection.
    "screenshot": ("screenshot", "screen-tracker"),
    "screen capture": ("screenshot",),
    "desktop image": ("screenshot",),
    "keystrokes": ("keylogger",),
    "keylog": ("keylogger",),
    "webcam": ("webcam",),
    "camera": ("webcam",),
    "credentials": ("mimikatz",),
    "dump credentials": ("mimikatz",),
    "lsass": ("mimikatz",),
    "passwords": ("mimikatz",),
    # Tokens.
    "token": ("token-list", "token-steal", "token-make", "token-use"),
    "impersonate": ("token-steal", "token-make", "token-use"),
    "steal token": ("token-steal",),
    # Network.
    "port scan": ("portscan",),
    "portscan": ("portscan",),
    "scan ports": ("portscan",),
    "network scan": ("portscan",),
    "proxy": ("socks5",),
    "socks": ("socks5",),
    "tunnel": ("socks5", "rportfwd"),
    "port forward": ("rportfwd",),
    "pivot": ("socks5", "rportfwd", "jump-ssh", "jump-wmi", "jump-winrm", "jump-service"),
    "lateral movement": ("jump-ssh", "jump-wmi", "jump-winrm", "jump-service"),
    "remote execution": (
        "remote-exec-ssh",
        "remote-exec-winrm",
        "remote-exec-wmi",
        "remote-exec-service",
    ),
    "remote command": (
        "remote-exec-ssh",
        "remote-exec-winrm",
        "remote-exec-wmi",
        "remote-exec-service",
    ),
    # In-memory execution.
    "dotnet": ("execute-assembly",),
    "assembly": ("execute-assembly",),
    "csharp": ("execute-assembly",),
    "shellcode": ("inject",),
    "inject": ("inject",),
    "beacon object file": ("bof",),
    "load dll": ("load-dll",),
    "run executable in memory": ("load-pe", "load-elf"),
    "amsi": ("amsi-bypass",),
    # Agent lifecycle.
    "sleep": ("sleep", "sleep-until"),
    "callback interval": ("sleep", "sleep-until"),
    "beacon interval": ("sleep", "sleep-until"),
    "kill agent": ("die",),
    "terminate agent": ("die",),
    "exit agent": ("die",),
    "reboot": ("shutdown",),
    "new agent": ("spawn",),
    "running commands": ("jobs",),
    "environment variable": ("env-var-set", "env-var-unset"),
}

# "kill" alone is deliberately absent: in Tuoni `kill` terminates a process on the
# target while `die` terminates the agent. Guessing between them for a bare verb
# risks losing access, so the operator must be specific.

# Words that mean the request is about a process rather than the agent itself.
_PROCESS_WORDS = frozenset({"process", "processes", "pid", "task", "tasks"})

_TEMPLATE_NAMES = frozenset(name for names in INTENT_TEMPLATES.values() for name in names)
_TEMPLATE_QUERY_WORDS = frozenset({
    "command", "commands", "template", "templates", "plugin", "native", "embedded",
    "shell", "directory", "listing", "process", "find", "search", "run", "execute",
})


def is_template_query(query: str) -> bool:
    """Recognize known command names without assuming they are installed.

    This routes to a read-only catalog search, never to a template or execution.
    Unknown extra words and negation remain subject to ordinary tool discovery.
    """
    tokens = _WORD.findall(query.casefold())
    for name in _TEMPLATE_NAMES:
        parts = _WORD.findall(name)
        for start in range(len(tokens) - len(parts) + 1):
            if (tokens[start:start + len(parts)] == parts
                    and set(tokens[:start] + tokens[start + len(parts):]) <= _TEMPLATE_QUERY_WORDS):
                return True
    return False

# Filler carries no intent, and dropping it from both the query and the phrases
# lets "terminate the agent" reach the phrase "terminate agent" without relaxing
# adjacency, which is what keeps unrelated words from being matched together.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "on", "in", "for", "to", "from", "with",
        "this", "that", "and", "is", "are", "it", "its", "my", "all", "me", "please",
    }
)


def _significant(words: list[str]) -> list[str]:
    return [word for word in words if word not in _STOPWORDS]


# Phrases are stripped once at import so both sides of the comparison agree.
_INTENT_PHRASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = tuple(
    (tuple(_significant(phrase.split())), templates)
    for phrase, templates in INTENT_TEMPLATES.items()
    if _significant(phrase.split())
)


def _contains_run(tokens: list[str], words: tuple[str, ...] | list[str]) -> bool:
    """Test whether `words` appears in `tokens` as an adjacent, ordered run.

    Matching a phrase as an unordered bag of words is unsafe here. "kill the
    runaway process on the agent" contains every word of both "kill process"
    and "kill agent", so a bag-of-words rule would offer `kill` (ends a process)
    and `die` (ends the agent, losing access) for the same request.

    Requiring adjacency also stops a single-word phrase from matching inside a
    longer word, which a plain substring test does: "cat" would otherwise fire
    on "dupli*cat*e" and "allo*cat*e".
    """
    span = len(words)
    target = list(words)
    return any(tokens[index : index + span] == target for index in range(len(tokens) - span + 1))


@lru_cache(maxsize=512)
def matched_templates(query: str) -> frozenset[str]:
    """Return template names an operator's phrasing maps to.

    A phrase matches when its words appear in the query in order and adjacent,
    so "enumerate the current user" matches "current user" and a bare "process"
    matches "process".
    """

    tokens = _significant(_WORD.findall(query.casefold()))
    if not tokens:
        return frozenset()

    matched: set[str] = set()
    for words, templates in _INTENT_PHRASES:
        if _contains_run(tokens, words):
            matched.update(templates)

    # Guard the one pair where guessing wrong ends the session. `die` terminates
    # the agent and costs all access; `kill` terminates a process on it. Withdraw
    # `die` from any request that also reads as being about a process ("terminate
    # agent process"), and withdraw both if a query somehow reaches both, since
    # it was not specific enough to act on either.
    if "die" in matched and _PROCESS_WORDS.intersection(tokens):
        matched.discard("die")
    if {"die", "kill"} <= matched:
        matched -= {"die", "kill"}
    return frozenset(matched)
