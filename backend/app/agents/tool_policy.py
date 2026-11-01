"""The tool-call policy shared by the parent and the worker (4.3, issue #8).

ONE pure function, :func:`decide`, answers "may this tool call run as asked?" from the
tool name, its input, the workspace root and the agent's Bash allow-list. Two callers use it:

* the worker's always-on ``PreToolUse`` hook (claude_sdk_adapter.run_worker), registered in
  all three modes. It sees EVERY tool call, the pre-allowed auto-tier ones (Read/Glob/Grep/LS)
  included, which ``can_use_tool`` never sees;
* the parent's ``can_use_tool`` path (claude_sdk_adapter.decide_permission), so an approval
  card is never shown for a call the policy refuses anyway.

Verdicts:

``deny``   a path outside the workspace (after resolving symlinks and ``..``), a malformed
           input, or a ``~``/NUL path. The reason travels back to the model, so nothing is
           dropped silently.
``ask``    a Bash/PowerShell command that does not match the allow-list as a WHOLE command.
           Interactive agents ask a person; unattended and plan agents deny (:func:`for_mode`).
``allow``  the policy has no objection. This is NOT a grant: the tool allowlist, the approval
           tiers and the SDK's own permission mode still apply. The worker hook therefore
           never answers "allow" to the SDK (that would skip ``can_use_tool``).

"Pure" means no state and no side effects; resolving a path reads the file system's
symlinks, which is the point (a link inside the workspace that points at ``/etc`` is refused).
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple

from .workspace import WorkspaceError, resolve_tool_path

Verdict = Literal["allow", "deny", "ask"]


class Decision(NamedTuple):
    verdict: Verdict
    reason: str


#: Built-in tools and the input keys that name a file or directory. ``None`` = the key may be
#: absent and then means the workspace root (the tool's cwd).
PATH_KEYS: dict[str, tuple[tuple[str, bool], ...]] = {
    # (key, required)
    "Read": (("file_path", True),),
    "Write": (("file_path", True),),
    "Edit": (("file_path", True),),
    "MultiEdit": (("file_path", True),),
    "NotebookEdit": (("notebook_path", True),),
    "NotebookRead": (("notebook_path", True),),
    "Glob": (("path", False),),
    "Grep": (("path", False),),
    "LS": (("path", False),),
}
#: Glob-like inputs that may smuggle a root of their own ("/etc/*", "../**").
PATTERN_KEYS: dict[str, tuple[str, ...]] = {"Glob": ("pattern",), "Grep": ("glob",)}
COMMAND_TOOLS = ("Bash", "PowerShell")

# Anything that can chain, substitute or redirect makes a command NOT match the allow-list:
# ; & | ` < > newline CR NUL, $( and ${. Quoting does not help: a quoted ";" also refuses
# the match (the call then asks, it is not denied outright in an interactive agent).
_UNSAFE_COMMAND = re.compile(r"[;&|`<>\n\r\x00]|\$\(|\$\{")
_GLOB_CHARS = re.compile(r"[*?\[{]")
# A brace or bracket group ("{a,b}", "[ab]"). The fixed-prefix check below stops at the first
# wildcard, so a group that names a directory of its own ("{/etc,src}", "[/]etc", "{..,x}")
# would slip past it: such a group is refused outright (4.3 Fable, a precaution - whether the
# SDK's glob expands it is not verified). An unclosed group runs to the end of the pattern.
_GROUP = re.compile(r"\{[^}]*\}?|\[[^\]]*\]?")


def _deny(reason: str) -> Decision:
    return Decision("deny", reason)


def _check_path(ws_root: Path, value: Any, key: str) -> Decision | None:
    if not isinstance(value, str):
        return _deny(f"{key} must be a path string")
    try:
        resolve_tool_path(ws_root, value)
    except WorkspaceError as e:
        return _deny(f"{key} {value!r} is outside this task's workspace ({e}); "
                     "only files inside the workspace may be used")
    return None


def _check_pattern(ws_root: Path, value: Any, key: str) -> Decision | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _deny(f"{key} must be a string")
    parts = [p for p in re.split(r"[\\/]+", value) if p]
    if ".." in parts:
        return _deny(f"{key} {value!r} may not climb out of the workspace ('..')")
    if value.startswith("~"):
        return _deny(f"{key} {value!r} is outside this task's workspace")
    for g in _GROUP.findall(value):
        if "/" in g or "\\" in g or ".." in g:
            return _deny(f"{key} {value!r} may not name a folder inside {{...}} or [...]; "
                         "write the folder before the wildcard")
    # The fixed directory part before the first wildcard ("/etc/*" -> "/etc", "link/**" -> "link")
    # must resolve inside the workspace, so a symlinked directory cannot widen the search.
    m = _GLOB_CHARS.search(value)
    static = value if m is None else value[: m.start()]
    if m is not None:
        static = static.rsplit("/", 1)[0] if "/" in static else ""
        if value.startswith("/") and not static:
            static = "/"
    if not static:
        return None
    return _check_path(ws_root, static, key)


def command_matches(command: str, bash_allow: Iterable[str]) -> bool:
    """True when ``command`` as a WHOLE is one plain command that starts with an allowed prefix.

    A prefix ending in a space (``"git "``) matches ``git`` followed by arguments, or the bare
    word. A prefix without a trailing space matches that exact command or that command plus
    arguments (``"ls"`` matches ``ls`` and ``ls -la``, never ``lsblk``). Empty or unsafe
    patterns never match, so a stray ``""`` cannot allow everything.
    """
    if not isinstance(command, str):
        return False
    cmd = command.strip(" \t")
    if not cmd or _UNSAFE_COMMAND.search(cmd):
        return False
    for pat in bash_allow or ():
        if not isinstance(pat, str):
            continue
        p = pat.strip(" \t")
        if not p or _UNSAFE_COMMAND.search(p):
            continue
        if cmd == p or cmd.startswith((p + " ", p + "\t")):
            return True
    return False


def decide(tool: str, input: Mapping[str, Any] | None, ws_root: str | os.PathLike[str],
           bash_allow: Iterable[str]) -> Decision:
    """The single policy decision for one tool call (see the module docstring)."""
    inp: Mapping[str, Any] = input if isinstance(input, Mapping) else {}
    root = Path(ws_root)
    if tool in COMMAND_TOOLS:
        command = inp.get("command")
        if not isinstance(command, str) or not command.strip():
            return _deny(f"{tool} needs a command string")
        if command_matches(command, bash_allow):
            return Decision("allow", f"the command matches this agent's {tool} allow-list")
        return Decision("ask", f"the command is not on this agent's {tool} allow-list "
                               "(only whole, single commands match; ; && || | ` $( and redirections never do)")
    for key, required in PATH_KEYS.get(tool, ()):
        if key not in inp or inp.get(key) in (None, ""):
            if required:
                return _deny(f"{tool} needs {key}")
            continue                                 # absent: the tool runs in the workspace root
        bad = _check_path(root, inp[key], key)
        if bad is not None:
            return bad
    for key in PATTERN_KEYS.get(tool, ()):
        bad = _check_pattern(root, inp.get(key), key)
        if bad is not None:
            return bad
    return Decision("allow", "inside the workspace")


def for_mode(decision: Decision, mode: str) -> Decision:
    """Fold ``ask`` into ``deny`` where nobody can be asked (unattended, plan)."""
    if decision.verdict == "ask" and mode != "interactive":
        who = "unattended agents cannot ask for approval" if mode == "unattended" \
            else "this agent is investigate-only (plan mode)"
        return _deny(f"{decision.reason}; {who}")
    return decision


def hook_output(decision: Decision, mode: str) -> dict[str, Any]:
    """What the worker's PreToolUse hook returns to the SDK. Never ``allow`` (that would skip
    ``can_use_tool`` and the approval tiers): ``deny`` and ``ask`` are explicit, no objection
    just keeps the stream going (the documented keep-alive)."""
    d = for_mode(decision, mode)
    if d.verdict == "allow":
        return {"continue_": True}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": d.verdict,
                                   "permissionDecisionReason": d.reason}}
