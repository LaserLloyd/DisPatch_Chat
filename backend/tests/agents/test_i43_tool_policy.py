"""4.3 (#8): the shared tool policy - path scoping and the Bash allow-list.

``tool_policy.decide`` is ONE pure function; the worker's always-on PreToolUse hook and the
parent's can_use_tool path both call it. Table-driven first, then the two call sites.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from agent_fakes import FAKE_API_KEY, ClaudeSDKClient, fake_sdk_module

from app.agents import claude_sdk_adapter as adapter
from app.agents import tool_policy
from app.agents.budget import Budget
from app.agents.claude_sdk_adapter import build_launch_plan, decide_permission, run_worker
from app.agents.interface import AgentSpec, ApprovalPolicy, TaskContext
from app.agents.tool_policy import command_matches, decide, for_mode, hook_output
from app.agents.workspace import prepare_workspace


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "ws"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "a.txt").write_text("x")
    return r


GIT = ["git "]

# (tool, input, bash_allow, verdict). "{root}" is replaced by the workspace root.
TABLE = [
    # (a) paths
    ("Read", {"file_path": "/etc/passwd"}, [], "deny"),
    ("Read", {"file_path": "{root}/sub/a.txt"}, [], "allow"),
    ("Read", {"file_path": "sub/a.txt"}, [], "allow"),
    ("Read", {"file_path": "{root}/../outside"}, [], "deny"),
    ("Read", {"file_path": "~/.ssh/id_ed25519"}, [], "deny"),
    ("Read", {}, [], "deny"),
    ("Read", {"file_path": ["/etc/passwd"]}, [], "deny"),
    ("Write", {"file_path": "/tmp/x", "content": "y"}, [], "deny"),
    ("Edit", {"file_path": "{root}/sub/a.txt", "old_string": "x", "new_string": "y"}, [], "allow"),
    ("MultiEdit", {"file_path": "/etc/hosts", "edits": []}, [], "deny"),
    ("NotebookEdit", {"notebook_path": "/etc/n.ipynb"}, [], "deny"),
    ("Glob", {"pattern": "*.py", "path": ".."}, [], "deny"),
    ("Glob", {"pattern": "*.py", "path": "{root}/sub"}, [], "allow"),
    ("Glob", {"pattern": "/etc/*"}, [], "deny"),
    ("Glob", {"pattern": "../**/*.py"}, [], "deny"),
    ("Glob", {"pattern": "**/*.py"}, [], "allow"),
    ("Grep", {"pattern": "secret", "path": "/var"}, [], "deny"),
    ("Grep", {"pattern": "secret"}, [], "allow"),                       # (e) no path = the root
    ("Grep", {"pattern": "x", "glob": "../*.env"}, [], "deny"),
    ("LS", {"path": "/"}, [], "deny"),
    ("LS", {"path": "{root}"}, [], "allow"),
    ("WebFetch", {"url": "https://example.com"}, [], "allow"),         # no path: no objection
    # (b) Bash
    ("Bash", {"command": "rm -rf /"}, GIT, "ask"),
    ("Bash", {"command": "git status"}, GIT, "allow"),
    ("Bash", {"command": "git"}, GIT, "allow"),
    ("Bash", {"command": "git status"}, [], "ask"),                     # empty list: every call asks
    ("Bash", {"command": ""}, GIT, "deny"),
    ("Bash", {}, GIT, "deny"),
    # (f) compound commands never match
    ("Bash", {"command": "git status && rm -rf ~"}, GIT, "ask"),
    ("Bash", {"command": "git status; rm -rf ~"}, GIT, "ask"),
    ("Bash", {"command": "git status || rm -rf ~"}, GIT, "ask"),
    ("Bash", {"command": "git log | sh"}, GIT, "ask"),
    ("Bash", {"command": "git log `rm -rf ~`"}, GIT, "ask"),
    ("Bash", {"command": "git log $(rm -rf ~)"}, GIT, "ask"),
    ("Bash", {"command": "git log ${HOME}"}, GIT, "ask"),
    ("Bash", {"command": "git status\nrm -rf ~"}, GIT, "ask"),
    ("Bash", {"command": "git log > ~/.bashrc"}, GIT, "ask"),
    ("Bash", {"command": "git log < /etc/passwd"}, GIT, "ask"),
    ("Bash", {"command": "git status &"}, GIT, "ask"),
    ("Bash", {"command": "gitx status"}, ["git"], "ask"),
    ("Bash", {"command": "ls -la"}, ["ls"], "allow"),
    ("Bash", {"command": "lsblk"}, ["ls"], "ask"),
    ("Bash", {"command": "rm -rf /"}, ["", " ", "*"], "ask"),          # junk patterns never match all
    ("Bash", {"command": "rm -rf /"}, ["git ; rm"], "ask"),            # an unsafe pattern is ignored
    ("PowerShell", {"command": "Remove-Item -Recurse C:\\"}, GIT, "ask"),
]


@pytest.mark.parametrize("tool,inp,allow,want", TABLE, ids=[f"{t[0]}-{i}" for i, t in enumerate(TABLE)])
def test_decide_table(root, tool, inp, allow, want):
    inp = {k: (v.replace("{root}", str(root)) if isinstance(v, str) else v) for k, v in inp.items()}
    d = decide(tool, inp, root, allow)
    assert d.verdict == want, d
    assert d.reason, "every verdict carries a reason (nothing is dropped silently)"


def test_a_denied_path_says_why(root):
    d = decide("Read", {"file_path": "/etc/passwd"}, root, [])
    assert d.verdict == "deny" and "/etc/passwd" in d.reason and "workspace" in d.reason


def test_a_symlink_out_of_the_workspace_is_followed_and_refused(root):
    """(e) resolve real paths: a link inside the workspace that points at /etc."""
    os.symlink("/etc", root / "etc-link")
    assert decide("Read", {"file_path": str(root / "etc-link" / "passwd")}, root, []).verdict == "deny"
    assert decide("Read", {"file_path": "etc-link/passwd"}, root, []).verdict == "deny"
    assert decide("Grep", {"pattern": "x", "path": "etc-link"}, root, []).verdict == "deny"
    assert decide("Glob", {"pattern": "etc-link/*"}, root, []).verdict == "deny"
    # ... and a workspace reached THROUGH a symlink still counts as inside.
    alias = root.parent / "alias"
    os.symlink(root, alias)
    assert decide("Read", {"file_path": str(alias / "sub" / "a.txt")}, alias, []).verdict == "allow"


def test_ask_folds_into_deny_where_nobody_can_be_asked(root):
    d = decide("Bash", {"command": "rm -rf /"}, root, GIT)
    assert for_mode(d, "interactive").verdict == "ask"
    assert for_mode(d, "unattended").verdict == "deny"
    assert for_mode(d, "plan").verdict == "deny"
    assert "unattended" in for_mode(d, "unattended").reason


def test_command_matches_is_whole_command():
    assert command_matches("git status", GIT)
    assert not command_matches("git status && rm -rf ~", GIT)
    assert not command_matches("git status", [])
    assert not command_matches(None, GIT)  # type: ignore[arg-type]


def test_the_hook_never_answers_allow(root):
    """An "allow" from PreToolUse would skip can_use_tool and the approval tiers."""
    for tool, inp, allow, _want in TABLE:
        inp = {k: (v.replace("{root}", str(root)) if isinstance(v, str) else v) for k, v in inp.items()}
        for mode in ("interactive", "unattended", "plan"):
            out = hook_output(decide(tool, inp, root, allow), mode)
            dec = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
            assert dec in (None, "deny", "ask"), out
            if dec is None:
                assert out == {"continue_": True}


# ---- the parent's can_use_tool path ------------------------------------------------------ #

def spec(**kw) -> AgentSpec:
    base = {"backend": "claude_sdk", "name": "a", "tool_allowlist": ["Read", "Write", "Grep", "Bash"],
            "budget": Budget(max_usd=1.0, max_turns=8)}
    base.update(kw)
    return AgentSpec(**base)


def test_parent_denies_out_of_workspace_paths_before_any_card(root):
    s = spec()
    assert decide_permission(s, "Write", {"file_path": "/etc/x"}, root)[0] == "deny"
    assert decide_permission(s, "Write", {"file_path": str(root / "x")}, root)[0] == "ask"   # destructive tier


def test_parent_bash_allow_list_interactive(root):
    s = spec(approval_policy=ApprovalPolicy(bash_allow=GIT))
    assert decide_permission(s, "Bash", {"command": "git status"}, root) == (
        "allow", "the command matches this agent's Bash allow-list")
    assert decide_permission(s, "Bash", {"command": "rm -rf /"}, root)[0] == "ask"
    assert decide_permission(s, "Bash", {"command": "git status && rm -rf ~"}, root)[0] == "ask"
    # an empty list: every Bash call asks (the 4.2 behaviour)
    assert decide_permission(spec(), "Bash", {"command": "git status"}, root)[0] == "ask"
    # the tool allowlist still comes first
    assert decide_permission(spec(tool_allowlist=["Read"]), "Bash", {"command": "git status"}, root)[0] == "deny"


def test_parent_unattended_bash_outside_the_list_is_denied(root):
    s = spec(tool_allowlist=["Read", "Bash"],
             approval_policy=ApprovalPolicy(mode="unattended", unattended_allow=["Bash"], bash_allow=GIT))
    assert decide_permission(s, "Bash", {"command": "git status"}, root)[0] == "allow"
    assert decide_permission(s, "Bash", {"command": "rm -rf /"}, root)[0] == "deny"


# ---- the launch plan and the worker's hook ------------------------------------------------ #

@pytest.fixture
def ws(agent_data):
    return prepare_workspace(agent_data, "task-pol")


def test_the_plan_carries_the_policy_and_never_pre_allows_bash(ws, agent_data):
    s = spec(approval_policy=ApprovalPolicy(always_ask=["Write"], bash_allow=GIT))   # Bash would be auto
    p = build_launch_plan(s, TaskContext(data_dir=agent_data), ws, api_key=FAKE_API_KEY)
    assert "Bash" not in p.options["allowed_tools"], "Bash must reach can_use_tool per command"
    assert p.policy == {"ws_root": str(ws.root), "bash_allow": GIT, "mode": "interactive"}
    assert p.to_wire()["policy"] == p.policy


async def _hook_for(start: dict):
    inbox: asyncio.Queue = asyncio.Queue()
    out: list[dict] = []
    ClaudeSDKClient.instances = []
    task = asyncio.create_task(run_worker(inbox, out.append, fake_sdk_module()))
    inbox.put_nowait(start)
    await asyncio.sleep(0.05)
    client = ClaudeSDKClient.instances[0]
    matchers = client.options.hooks["PreToolUse"]
    inbox.put_nowait(None)
    await asyncio.wait_for(task, 5)
    assert len(matchers) == 1 and matchers[0].matcher is None
    return matchers[0].hooks[0], out


@pytest.mark.parametrize("mode", ["interactive", "unattended", "plan"])
async def test_the_policy_hook_is_registered_in_all_three_modes(ws, agent_data, mode):
    kw = {"unattended_allow": ["Bash", "Write"]} if mode == "unattended" else {}
    s = spec(approval_policy=ApprovalPolicy(mode=mode, bash_allow=GIT, **kw))
    p = build_launch_plan(s, TaskContext(data_dir=agent_data), ws, api_key=FAKE_API_KEY)
    hook, out = await _hook_for(p.to_wire())
    deny = await hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, "tu1", None)
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "/etc/passwd" in deny["hookSpecificOutput"]["permissionDecisionReason"]
    ok = await hook({"tool_name": "Read", "tool_input": {"file_path": str(ws.root / "f")}}, "tu2", None)
    assert ok == {"continue_": True}
    rm = await hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "tu3", None)
    assert rm["hookSpecificOutput"]["permissionDecision"] == ("ask" if mode == "interactive" else "deny")
    git = await hook({"tool_name": "Bash", "tool_input": {"command": "git status"}}, "tu4", None)
    assert git == {"continue_": True}


async def test_a_start_without_a_policy_block_gets_the_strictest_one(ws):
    start = {"op": "start", "options": {"cwd": str(ws.root), "permission_mode": "dontAsk", "env": {},
                                        "setting_sources": [], "allowed_tools": ["Bash"]},
             "secret_env": {}, "secret_mcp_headers": {}, "worker": {}}
    hook, _ = await _hook_for(start)
    r = await hook({"tool_name": "Bash", "tool_input": {"command": "git status"}}, "t", None)
    assert r["hookSpecificOutput"]["permissionDecision"] == "deny"
    r = await hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, "t", None)
    assert r["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_both_call_sites_use_the_one_decide(ws, agent_data, monkeypatch):
    """Hardening: one source of truth. Replace decide; both the hook and the parent obey it."""
    calls: list[str] = []

    def fake_decide(tool, inp, root, allow):
        calls.append(tool)
        return tool_policy.Decision("deny", "sentinel")
    monkeypatch.setattr(tool_policy, "decide", fake_decide)
    s = spec()
    assert decide_permission(s, "Read", {"file_path": "x"}, ws.root) == ("deny", "sentinel")
    p = build_launch_plan(s, TaskContext(data_dir=agent_data), ws, api_key=FAKE_API_KEY)
    hook, out = await _hook_for(p.to_wire())
    r = await hook({"tool_name": "Grep", "tool_input": {}}, "t", None)
    assert r["hookSpecificOutput"]["permissionDecisionReason"] == "sentinel"
    assert calls == ["Read", "Grep"]
    assert adapter.tool_policy is tool_policy


async def test_a_hook_denial_is_reported_to_the_parent(ws, agent_data):
    p = build_launch_plan(spec(), TaskContext(data_dir=agent_data), ws, api_key=FAKE_API_KEY)
    hook, out = await _hook_for(p.to_wire())
    await hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/shadow"}}, "tu9", None)
    note = [m for m in out if m.get("kind") == "policy_denied"]
    assert note and note[0]["tool_name"] == "Read" and note[0]["tool_use_id"] == "tu9"
