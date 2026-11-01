"""4.3 WP-A5 (#30, #37): host-level agent grants are the protected Admin's (Owner's) alone.

Since 3.0 every Admin holds ``admin_system``, so the old "owner-only" gate in
``agents_api._check_spec`` admitted any Admin. A host-level grant now needs
``principal.is_protected``: the existing owner-only features (stdio MCP, privileged MCP,
unattended tools, repo settings, a stored secret sent off this machine) plus the shell/file
tools in ``tool_allowlist``, anything removed from ``always_ask`` and an interactive agent with
an empty ``always_ask``. ``POST /api/tasks`` with ``repo_path`` needs the Owner too.

The grants are still compared grant by grant: an Admin may edit an agent the Owner approved
without re-granting, and the normal path (a default spec) stays open to every Admin.
"""
from __future__ import annotations

import pytest
from agents_world import aw, make_agent, spec

from app.agents.interface import DEFAULT_ALWAYS_ASK

NEEDS_OWNER = "agent_needs_owner"


@pytest.fixture
def ada(aw):
    """A second, non-protected Admin on the owner's event loop."""
    aw.world.person(aw.owner, "Ada", "admin", password="admin-pass1")
    c = aw.caller(20)
    assert aw.world.login(c, name="Ada", password="admin-pass1").status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["role"] == "admin" and not me.get("protected"), me
    return c


def _refused(r):
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"]["code"] == NEEDS_OWNER, body
    assert body["detail"].startswith("Only the Owner can give an agent this power:"), body
    return body


def _plain(**over):
    """A default spec with no host-level power: no shell tools, default always_ask, interactive."""
    base = {"backend": "fake", "tool_allowlist": ["Read", "Grep"]}
    base.update(over)
    return base


# (a) ---------------------------------------------------------------------- #

def test_a_shell_tool_needs_the_owner_and_an_admin_may_still_edit_the_approved_agent(aw, ada):
    for tools in (["Read", "Bash"], ["Write"], ["edit"], ["*"], ["Bash*"], ["NotebookEdit"],
                  ["MultiEdit"], ["KillShell"], ["BashOutput"]):
        body = _refused(ada.post("/api/agents", json={"name": "Sh", "spec": spec(tool_allowlist=tools)}))
        assert "Sh" not in {a["name"] for a in aw.owner.get("/api/agents").json()["agents"]}, (tools, body)
    r = aw.owner.post("/api/agents", json={"name": "Shell", "spec": spec(tool_allowlist=["Read", "Bash"])})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    # The Admin edits the description, the name, and resends the whole stored spec (what the UI
    # does): the Bash grant is unchanged, so nothing new is granted.
    assert ada.patch(f"/api/agents/{aid}", json={"description": "now with notes"}).status_code == 200
    assert ada.patch(f"/api/agents/{aid}", json={"name": "Shell2"}).status_code == 200
    stored = aw.owner.get("/api/agents").json()["agents"]
    [row] = [a for a in stored if a["id"] == aid]
    assert row["description"] == "now with notes" and "Bash" in row["spec"]["tool_allowlist"]
    assert ada.patch(f"/api/agents/{aid}", json={"spec": row["spec"]}).status_code == 200
    # ...but adding another shell/file tool to it is a new grant.
    more = dict(row["spec"], tool_allowlist=["Read", "Bash", "Write"])
    _refused(ada.patch(f"/api/agents/{aid}", json={"spec": more}))
    assert aw.owner.patch(f"/api/agents/{aid}", json={"spec": more}).status_code == 200


# (b) ---------------------------------------------------------------------- #

def test_removing_a_tool_from_always_ask_needs_the_owner(aw, ada):
    a = make_agent(aw.owner, "Asker", spec=_plain())
    cur = a["spec"]
    assert "Write" in cur["approval_policy"]["always_ask"]
    narrower = [t for t in cur["approval_policy"]["always_ask"] if t != "Write"]
    patched = dict(cur, approval_policy=dict(cur["approval_policy"], always_ask=narrower))
    body = _refused(ada.patch(f"/api/agents/{a['id']}", json={"spec": patched}))
    assert "Write" in body["detail"]
    # a new spec narrower than the shipped default counts too
    _refused(ada.post("/api/agents", json={"name": "Narrow", "spec": _plain(
        approval_policy={"mode": "interactive", "always_ask": narrower})}))
    # tightening (adding a tool) is fine
    wider = dict(cur, approval_policy=dict(cur["approval_policy"],
                                           always_ask=[*cur["approval_policy"]["always_ask"], "Glob"]))
    assert ada.patch(f"/api/agents/{a['id']}", json={"spec": wider}).status_code == 200
    # Compared grant by grant against the STORED list: removing even the tool the Admin just
    # added is a removal, so it needs the Owner as well.
    _refused(ada.patch(f"/api/agents/{a['id']}", json={"spec": cur}))
    assert aw.owner.patch(f"/api/agents/{a['id']}", json={"spec": patched}).status_code == 200
    # Once the Owner narrowed it, the Admin may edit it without re-granting.
    assert ada.patch(f"/api/agents/{a['id']}", json={"spec": patched}).status_code == 200


def test_an_interactive_agent_with_an_empty_always_ask_needs_the_owner(aw, ada):
    empty = _plain(approval_policy={"mode": "interactive", "always_ask": []})
    body = _refused(ada.post("/api/agents", json={"name": "Empty", "spec": empty}))
    assert "always-ask" in body["detail"]
    r = aw.owner.post("/api/agents", json={"name": "Empty", "spec": empty})
    assert r.status_code == 201, r.text
    assert ada.patch(f"/api/agents/{r.json()['id']}", json={"description": "x"}).status_code == 200


# (c) #37 ------------------------------------------------------------------- #

def test_a_header_secret_to_a_dot_localhost_name_counts_as_remote(aw, ada):
    def servers(url):
        return {"s": {"transport": "http", "url": url,
                      "header_secrets": {"Authorization": "STUDIOFORGE_MCP_KEY_RO"}}}
    for i, url in enumerate(("http://foo.localhost/", "http://localhost.attacker.example/mcp",
                             "http://127.0.0.1.nip.io/mcp", "http://0.0.0.0:9/mcp", "http://127.5.6.7:9/mcp",
                             "http://[::ffff:127.0.0.1]:9/mcp")):
        _refused(ada.post("/api/agents", json={"name": f"Remote{i}", "spec": _plain(mcp_servers=servers(url))}))
    assert aw.owner.post("/api/agents", json={"name": "Remote", "spec": _plain(
        mcp_servers=servers("http://foo.localhost/"))}).status_code == 201
    # the literals stay local: an Admin may send a stored secret to a server on this machine
    for i, url in enumerate(("http://localhost:9/mcp", "http://LOCALHOST:9/mcp", "http://127.0.0.1:1234/mcp",
                             "http://[::1]:9/mcp")):
        r = ada.post("/api/agents", json={"name": f"Local{i}", "spec": _plain(mcp_servers=servers(url))})
        assert r.status_code == 201, (url, r.text)


def test_is_local_url_accepts_literals_only():
    from app.agents_api import _is_local_url
    for url in ("http://localhost/", "http://127.0.0.1:9/", "http://LocalHost:9/", "http://[::1]:8/x"):
        assert _is_local_url(url), url
    for url in ("http://foo.localhost/", "http://a.b.localhost/", "http://localhost.example/",
                "http://127.0.0.1.nip.io/", "http://198.51.100.7/", "http://[::ffff:127.0.0.1]/",
                "http://0.0.0.0/", "http://[::]/", "http://127.255.0.1/", "http://127.1/", "", None, "not a url", "http://[bad/"):
        assert not _is_local_url(url), url


# (d) ---------------------------------------------------------------------- #

def test_an_admin_cannot_grant_the_old_owner_only_features(aw, ada):
    # Regression of the admin_system finding: these were 201 for any Admin since 2026-10-19.
    for i, extra in enumerate((
            {"mcp_servers": {"x": {"transport": "stdio", "command": "node", "args": ["s.js"]}}},
            {"mcp_servers": {"x": {"transport": "http", "url": "http://localhost:9/mcp", "privileged": True}}},
            {"workspace_policy": {"trust_repo_settings": True}},
            {"tool_allowlist": ["Read"], "approval_policy": {"mode": "unattended", "unattended_allow": ["Read"]}})):
        _refused(ada.post("/api/agents", json={"name": f"Risky{i}", "spec": _plain(**extra)}))
        assert aw.owner.post("/api/agents", json={"name": f"Risky{i}", "spec": _plain(**extra)}).status_code == 201


# (e) ---------------------------------------------------------------------- #

def test_the_normal_path_stays_open_to_every_admin(aw, ada):
    # A default spec: default always_ask, interactive, no shell tools - the new gates must not refuse it.
    for i, s in enumerate((_plain(), {"backend": "fake"}, _plain(approval_policy={"mode": "interactive"}),
                           _plain(approval_policy={"mode": "interactive", "always_ask": list(DEFAULT_ALWAYS_ASK)}),
                           _plain(approval_policy={"mode": "plan"}),
                           _plain(tool_allowlist=["Read", "Grep", "Glob", "WebSearch"]))):
        r = ada.post("/api/agents", json={"name": f"Normal{i}", "spec": s, "published_to": ["*"]})
        assert r.status_code == 201, (s, r.text)
    # and the Admin may use it
    agent = r.json()
    t = ada.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "hello"})
    assert t.status_code == 201, t.text


# (f) ---------------------------------------------------------------------- #

def test_a_task_with_a_repo_path_needs_the_owner(aw, ada, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = make_agent(aw.owner, "Repo", published=["*"], spec=_plain())
    r = ada.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "hello", "repo_path": str(repo)})
    _refused(r)
    assert "folder" in r.json()["detail"]
    assert ada.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "hello"}).status_code == 201
    r = aw.owner.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "hello", "repo_path": str(repo)})
    assert r.status_code == 201, r.text


# machine callers are unchanged ---------------------------------------------- #

def test_a_machine_key_is_refused_on_agent_routes_as_before(aw):
    # No MachineScope reaches the agent routes, so a keyed caller is judged as a person with no
    # session: refused (401 signin_required on P0 and after this WP), never let in, and no new
    # gate is reached. The plan text said "403"; the unchanged pre-4.3 answer is 401.
    token = aw.owner.post("/api/auth/api-token", json={"rotate": True}).json()["api_token"]
    machine = aw.world.client(("203.0.113.90", 1), origin=False)
    machine.headers.update({"X-API-Key": token})
    for method, url, body in (("POST", "/api/agents", {"name": "M", "spec": _plain()}),
                              ("POST", "/api/agents", {"name": "M2", "spec": spec()}),
                              ("GET", "/api/agents", None)):
        r = machine.request(method, url, json=body)
        assert r.status_code == 401, (method, url, r.status_code, r.text)
        assert r.json()["error"]["code"] == "signin_required"
    names = {a["name"] for a in aw.owner.get("/api/agents").json()["agents"]}
    assert not {"M", "M2"} & names


# INT-1: approval_policy.bash_allow is a host-level grant ----------------------------------- #

def test_bash_allow_needs_the_owner_and_an_admin_may_still_edit_the_approved_agent(aw, ada):
    """A matching Bash command skips always_ask and runs unattended, so each bash_allow entry the
    saved spec did not already have needs the protected Admin."""
    def with_allow(cur, allow):
        return dict(cur, approval_policy=dict(cur["approval_policy"], bash_allow=allow))
    base = spec(tool_allowlist=["Read", "Bash"])
    # a new agent: any entry is new
    _refused(ada.post("/api/agents", json={"name": "Ba", "spec": with_allow(base, ["git status"])}))
    r = aw.owner.post("/api/agents", json={"name": "Basher", "spec": with_allow(base, ["git status"])})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    # the Admin edits the description and resends the Owner-approved spec unchanged
    assert ada.patch(f"/api/agents/{aid}", json={"description": "notes"}).status_code == 200
    [row] = [a for a in aw.owner.get("/api/agents").json()["agents"] if a["id"] == aid]
    assert row["spec"]["approval_policy"]["bash_allow"] == ["git status"]
    assert ada.patch(f"/api/agents/{aid}", json={"spec": row["spec"]}).status_code == 200
    # ...but adding or widening an entry is a new grant
    body = _refused(ada.patch(f"/api/agents/{aid}", json={"spec": with_allow(row["spec"], ["git status", "rm"])}))
    assert "rm" in body["detail"]
    _refused(ada.patch(f"/api/agents/{aid}", json={"spec": with_allow(row["spec"], ["git"])}))
    # removing one narrows: every Admin may do that
    assert ada.patch(f"/api/agents/{aid}", json={"spec": with_allow(row["spec"], [])}).status_code == 200
