"""Gate G17, policy part: agents and tasks, exercised from the outside.

Via the real HTTP routes and WebSocket (the same client the UI uses):

* a Restricted session - and nobody signed in - cannot list, start or view agents or tasks;
  every route answers 403, listing included;
* a Member cannot use an agent that was not published to them, nor see another profile's task;
* destructive tool calls always produce an approval request, whatever the person clicks;
* a budget stop ends the task ``failed`` and raises a notification.
"""
from __future__ import annotations

import time

import pytest
from agents_world import TERMINAL, aw, event_types, make_agent, spec, wait_state

from app import agents_service
from app import agents_store as store

# Every agent/task route and the verb that reaches it. {id}/{rid} are filled with a REAL id
# in the "a task that exists" half so a 404 cannot pass for a 403.
AGENT_ROUTES = [
    ("GET", "/api/agents"), ("POST", "/api/agents"), ("PATCH", "/api/agents/{agent}"),
    ("DELETE", "/api/agents/{agent}"), ("GET", "/api/agents/available"),
    ("GET", "/api/agents/backends"), ("GET", "/api/agents/credentials/claude"),
    ("PUT", "/api/agents/credentials/claude"), ("DELETE", "/api/agents/credentials/claude"),
    ("GET", "/api/agents/secrets"), ("PUT", "/api/agents/secrets/MCP_X"),
    ("DELETE", "/api/agents/secrets/MCP_X"), ("GET", "/api/agents/settings"),
    ("PUT", "/api/agents/settings"), ("GET", "/api/agents/profile-budgets"),
    ("PUT", "/api/agents/profile-budgets/{profile}"), ("GET", "/api/agents/usage"),
    ("POST", "/api/agents/retention/purge"),
    ("POST", "/api/tasks"), ("GET", "/api/tasks"), ("GET", "/api/tasks/{task}"),
    ("GET", "/api/tasks/{task}/events?after_seq=-1"), ("POST", "/api/tasks/{task}/send"),
    ("POST", "/api/tasks/{task}/interrupt"), ("POST", "/api/tasks/{task}/resume"),
    ("POST", "/api/tasks/{task}/cancel"), ("POST", "/api/tasks/{task}/release"),
    ("POST", "/api/tasks/{task}/dismiss"), ("POST", "/api/approvals/{rid}"),
]


def _call(client, method, template, **ids):
    url = template.format(**ids)
    if method == "GET":
        return client.get(url)
    return client.request(method, url, json={"message": "x"} if url.endswith("/send") else {})


# --------------------------------------------------------------------------- #
# Restricted (and signed-out) callers
# --------------------------------------------------------------------------- #


def test_a_restricted_session_cannot_list_start_or_view_agents_or_tasks(aw):
    agent = make_agent(aw.owner, published=["*"])
    r = aw.owner.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "scenario:approval"})
    assert r.status_code == 201, r.text
    tid = r.json()["task"]["id"]
    wait_state(aw.owner, tid, "awaiting_approval")
    rid = aw.owner.get(f"/api/tasks/{tid}").json()["approvals"][0]["request_id"]
    ids = {"agent": agent["id"], "task": tid, "rid": rid, "profile": aw.eve_id}
    for who in ("kid", "anonymous"):
        client = getattr(aw, who)
        for method, template in AGENT_ROUTES:
            resp = _call(client, method, template, **ids)
            assert resp.status_code == 403, f"{who}: {method} {template} -> {resp.status_code} {resp.text[:120]}"
            assert resp.json()["error"]["code"] == "safe_mode"
    # and it is not just the status code: nothing about the task leaked
    assert aw.mgr.backends["fake"].task_info(tid).state.value == "awaiting_approval"


def test_a_machine_with_the_api_key_cannot_dispatch(aw):
    aw.owner.post("/api/auth/api-token", json={})
    machine = aw.world.client(origin=False)
    for method, template in (("POST", "/api/tasks"), ("GET", "/api/tasks"), ("GET", "/api/agents/available")):
        r = _call(machine, method, template)
        assert r.status_code in (401, 403), (method, template, r.status_code)


def test_a_member_without_the_grant_cannot_use_agents_at_all(aw):
    make_agent(aw.owner, published=["*"])
    for method, template in AGENT_ROUTES:
        r = _call(aw.max, method, template, agent="x", task="x", rid="x", profile="x")
        assert r.status_code == 403, f"{method} {template} -> {r.status_code}"
        assert r.json()["error"]["code"] == "permission_denied"


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


def test_a_member_sees_and_uses_only_agents_published_to_them(aw):
    open_agent = make_agent(aw.owner, "Open", published=["*"])
    mine = make_agent(aw.owner, "Mine", published=[aw.eve_id])
    hidden = make_agent(aw.owner, "Hidden", published=[])
    off = make_agent(aw.owner, "Off", published=["*"], disabled=True)
    seen = {a["name"] for a in aw.eve.get("/api/agents/available").json()["agents"]}
    assert seen == {"Open", "Mine"}
    assert all("published_to" not in a and "spec" not in a
               for a in aw.eve.get("/api/agents/available").json()["agents"]), "who else may use it is not for a Member"
    # Fay holds the same permission but was not named: she gets the open one only.
    assert {a["name"] for a in aw.fay.get("/api/agents/available").json()["agents"]} == {"Open"}
    for agent in (hidden, off, mine):
        r = aw.fay.post("/api/tasks", json={"agent_id": agent["id"], "prompt": "hello"})
        assert r.status_code == 404, (agent["name"], r.text)
        assert r.json()["error"]["code"] == "agent_not_found"
    assert aw.eve.post("/api/tasks", json={"agent_id": hidden["id"], "prompt": "hi"}).status_code == 404
    ok = aw.eve.post("/api/tasks", json={"agent_id": mine["id"], "prompt": "hello"})
    assert ok.status_code == 201
    wait_state(aw.eve, ok.json()["task"]["id"], "succeeded")
    # The Owner may use any enabled agent; a switched-off one answers 409, not 404.
    assert aw.owner.post("/api/tasks", json={"agent_id": hidden["id"], "prompt": "hi"}).status_code == 201
    r = aw.owner.post("/api/tasks", json={"agent_id": off["id"], "prompt": "hi"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "agent_disabled"
    assert open_agent["published_to"] == ["*"]


def test_unpublishing_takes_effect_at_once(aw):
    a = make_agent(aw.owner, published=[aw.eve_id])
    assert aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "hi"}).status_code == 201
    assert aw.owner.patch(f"/api/agents/{a['id']}", json={"published_to": []}).status_code == 200
    assert aw.eve.get("/api/agents/available").json()["agents"] == []
    assert aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "hi"}).status_code == 404


def test_a_task_belongs_to_its_owner_and_the_owner_account_sees_all(aw):
    a = make_agent(aw.owner, published=["*"])
    t_eve = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    t_fay = aw.fay.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    for c, mine, theirs in ((aw.eve, t_eve, t_fay), (aw.fay, t_fay, t_eve)):
        assert c.get(f"/api/tasks/{mine}").status_code == 200
        for method, template in (("GET", "/api/tasks/{task}"), ("GET", "/api/tasks/{task}/events"),
                                 ("POST", "/api/tasks/{task}/send"), ("POST", "/api/tasks/{task}/interrupt"),
                                 ("POST", "/api/tasks/{task}/cancel")):
            r = _call(c, method, template, task=theirs)
            assert r.status_code == 404 and r.json()["error"]["code"] == "task_not_found", (method, template)
        assert {t["id"] for t in c.get("/api/tasks").json()["tasks"]} == {mine}
        # the profile filter is an operator's tool; for everyone else it is ignored
        assert {t["id"] for t in c.get(f"/api/tasks?profile={aw.eve_id}").json()["tasks"]} == {mine}
    assert {t_eve, t_fay} <= {t["id"] for t in aw.owner.get("/api/tasks").json()["tasks"]}
    only_eve = aw.owner.get(f"/api/tasks?profile={aw.eve_id}").json()["tasks"]
    assert [t["id"] for t in only_eve] == [t_eve]
    for tid in (t_eve, t_fay):
        aw.owner.post(f"/api/tasks/{tid}/cancel")


def test_only_the_owner_grants_host_level_powers_to_an_agent(aw):
    aw.world.person(aw.owner, "Ada", "admin", password="admin-pass1")
    admin = aw.world.client(("203.0.113.60", 1))
    assert aw.world.login(admin, name="Ada", password="admin-pass1").status_code == 200
    # An Admin manages agents...
    ok = admin.post("/api/agents", json={"name": "Plain", "spec": spec()})
    assert ok.status_code == 201
    # ...but not the powers that are code execution in effect.
    for extra in ({"workspace_policy": {"trust_repo_settings": True}},
                  {"mcp_servers": {"x": {"transport": "stdio", "command": "node", "args": ["s.js"]}}},
                  {"mcp_servers": {"x": {"transport": "http", "url": "http://localhost:9/mcp", "privileged": True}}}):
        r = admin.post("/api/agents", json={"name": "Risky", "spec": spec(**extra)})
        assert r.status_code == 403 and r.json()["error"]["code"] == "agent_needs_owner", extra
        assert aw.owner.post("/api/agents", json={"name": "Risky" + str(len(str(extra))), "spec": spec(**extra)}).status_code == 201


def test_an_agent_definition_is_validated(aw):
    def post(**over):
        return aw.owner.post("/api/agents", json={"name": over.pop("name", "A1"), "spec": spec(**over)})
    assert post(backend="nope").json()["error"]["code"] == "agent_invalid"
    # a reader of untrusted content may not also hold a shell (quarantine rule)
    r = post(name="Reader", reads_untrusted_content=True, tool_allowlist=["WebFetch", "Bash"])
    assert r.status_code == 422 and "split it into a reader" in r.json()["detail"]
    # an MCP header may not carry the Anthropic key to a server of the admin's choosing
    r = post(name="Leaky", tool_allowlist=["Read"], mcp_servers={
        "s": {"transport": "http", "url": "http://localhost:9/mcp",
              "header_secrets": {"x-api-key": "ANTHROPIC_API_KEY"}}})
    assert r.status_code == 422 and "MCP_ or STUDIOFORGE_" in r.json()["detail"]
    r = post(name="Fine", tool_allowlist=["Read"], mcp_servers={
        "sf": {"transport": "http", "url": "http://localhost:9/mcp",
               "header_secrets": {"Authorization": "STUDIOFORGE_MCP_KEY_RO"}}})
    assert r.status_code == 201
    assert post(name="fine").status_code == 409                    # names are unique, case-folded
    assert post(name="Unattended", approval_policy={"mode": "unattended"}).status_code == 422   # Bash/Read need asking
    assert post(name="BadPerm", required_permission="make_coffee").status_code == 422


def test_an_agent_with_running_tasks_cannot_be_deleted(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    wait_state(aw.owner, tid, "running")
    r = aw.owner.delete(f"/api/agents/{a['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "agent_has_active_tasks"
    aw.owner.post(f"/api/tasks/{tid}/cancel")
    wait_state(aw.owner, tid, "cancelled")
    assert aw.owner.delete(f"/api/agents/{a['id']}").status_code == 200


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


def test_a_destructive_tool_call_always_asks_and_task_scope_is_never_honoured(aw):
    a = make_agent(aw.owner, published=["*"])
    for round_ in (1, 2):                                   # asked EVERY time, not once per agent
        tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:approval"}).json()["task"]["id"]
        t = wait_state(aw.owner, tid, "awaiting_approval")
        [pending] = t["pending_approvals"]
        assert pending["tool"] == "Bash" and pending["scopes"] == ["once"]
        r = aw.owner.post(f"/api/approvals/{pending['request_id']}", json={"decision": "allow", "scope": "task"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "approval_scope_not_allowed"
        r = aw.owner.post(f"/api/approvals/{pending['request_id']}", json={"decision": "allow", "scope": "once"})
        assert r.status_code == 200, r.text
        assert r.json()["approval"]["decided_by"]
        done = wait_state(aw.owner, tid, "succeeded")
        assert done["approvals"] == {"allowed": 1, "denied": 0}
        assert event_types(aw.owner, tid).count("approval_request") == 1, round_
    # answering twice is refused
    r = aw.owner.post(f"/api/approvals/{pending['request_id']}", json={"decision": "allow"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "approval_not_found"


def test_deny_reaches_the_agent_and_is_recorded(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:approval"}).json()["task"]["id"]
    rid = wait_state(aw.owner, tid, "awaiting_approval")["pending_approvals"][0]["request_id"]
    assert aw.owner.post(f"/api/approvals/{rid}", json={"decision": "deny"}).status_code == 200
    done = wait_state(aw.owner, tid, "succeeded")
    assert done["approvals"] == {"allowed": 0, "denied": 1}
    results = [e for e in aw.owner.get(f"/api/tasks/{tid}/events").json()["events"] if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False and "denied" in results[0]["output"]
    [row] = aw.owner.get(f"/api/tasks/{tid}").json()["approvals"]
    assert row["state"] == "deny" and row["decided_by"] and row["decided_at"]


def test_a_non_destructive_tool_can_be_allowed_for_the_whole_task(aw):
    a = make_agent(aw.owner, "Asker", published=["*"],
                   spec=spec(approval_policy={"mode": "interactive", "ask_tools": ["Read"]}))
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:tools"}).json()["task"]["id"]
    [pending] = wait_state(aw.owner, tid, "awaiting_approval")["pending_approvals"]
    assert pending["scopes"] == ["once", "task"]
    assert aw.owner.post(f"/api/approvals/{pending['request_id']}",
                         json={"decision": "allow", "scope": "task"}).status_code == 200
    wait_state(aw.owner, tid, "succeeded")


def test_approving_needs_the_approve_permission_and_sight_of_the_task(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:approval"}).json()["task"]["id"]
    rid = wait_state(aw.eve, tid, "awaiting_approval")["pending_approvals"][0]["request_id"]
    # Eve may dispatch but was not granted approve_agent_actions: her own task waits for an operator.
    r = aw.eve.post(f"/api/approvals/{rid}", json={"decision": "allow"})
    assert r.status_code == 403 and r.json()["required_permission"] == "approve_agent_actions"
    # Fay holds dispatch too, but cannot see Eve's task - not even to say no.
    assert aw.fay.post(f"/api/approvals/{rid}", json={"decision": "deny"}).status_code == 403
    assert aw.owner.post(f"/api/approvals/{rid}", json={"decision": "allow"}).status_code == 200
    wait_state(aw.eve, tid, "succeeded")


# --------------------------------------------------------------------------- #
# Budgets and notifications
# --------------------------------------------------------------------------- #


def test_a_budget_stop_fails_the_task_and_notifies(aw):
    a = make_agent(aw.owner, "Frugal", published=["*"],
                   spec=spec(budget={"max_usd": 0.05}))
    r = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:budget"})
    tid = r.json()["task"]["id"]
    t = wait_state(aw.owner, tid, "failed")
    assert t["reason"] == "budget_exceeded"
    assert t["cost_is_estimate"] is True and t["resumable"] is False
    types = event_types(aw.owner, tid)
    assert types[-3:] == ["cost_update", "error", "result"]
    err = [e for e in aw.owner.get(f"/api/tasks/{tid}/events").json()["events"] if e["type"] == "error"][0]
    assert err["code"] == "budget_exceeded"
    assert ("task_failed", f"task:{tid}", "A task failed") in aw.notes.pushes
    # resuming a budget stop is refused: raising the budget is a deliberate act, not a click
    assert aw.owner.post(f"/api/tasks/{tid}/resume", json={}).status_code == 409
    # the audit row says who, which agent, what happened and what it cost (an estimate)
    from app import accounts
    audit = [r for r in accounts.recent_activity(category="audit") if r["kind"] == "agent_task"]
    assert audit and '"outcome":"failed"' in audit[0]["detail"] and "Frugal" in audit[0]["subject"]


def test_the_daily_limit_is_shared_by_every_task_of_the_agent(aw):
    a = make_agent(aw.owner, "Daily", published=["*"], spec=spec(budget={"max_usd_per_day": 0.015}))
    first = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    assert wait_state(aw.owner, first, TERMINAL)["state"] == "succeeded"        # 0.01 of 0.015
    second = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    t = wait_state(aw.owner, second, TERMINAL)
    assert t["state"] == "failed" and t["reason"] == "budget_exceeded"           # +0.01 crosses it
    r = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "agent_budget_exhausted"
    spent = {(s["kind"], s["subject"]): s["usd"] for s in aw.owner.get("/api/agents/usage").json()["spend"]}
    assert spent[("agent", a["id"])] == pytest.approx(0.02)


def test_a_profile_budget_limits_what_that_profile_can_spend(aw):
    a = make_agent(aw.owner, "Shared", published=["*"])
    r = aw.owner.put(f"/api/agents/profile-budgets/{aw.eve_id}", json={"usd_per_day": 0.015})
    assert r.status_code == 200
    ok = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    assert wait_state(aw.eve, ok, TERMINAL)["state"] == "succeeded"
    over = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    assert wait_state(aw.eve, over, TERMINAL)["reason"] == "budget_exceeded"
    assert aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "x"}).status_code == 429
    # Fay is not limited by Eve's budget
    fay = aw.fay.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    assert wait_state(aw.fay, fay, TERMINAL)["state"] == "succeeded"
    b = aw.eve.get("/api/agents/available").json()["budget"]
    assert b["usd_per_day"] == 0.015 and b["estimate"] is True and b["spent_today_usd"] > 0.015 - 0.001


def test_turn_and_time_limits_come_from_the_tighter_of_agent_and_profile(aw):
    from app.agents.interface import AgentSpec
    a = make_agent(aw.owner, "Limits", published=["*"], spec=spec(budget={"max_turns": 10, "max_wall_clock_s": 600}))
    aw.owner.put(f"/api/agents/profile-budgets/{aw.eve_id}", json={"max_turns": 3, "max_tokens": 5000})
    b = aw.mgr.effective_budget(AgentSpec.model_validate(store.get_agent(a["id"])["spec"]),
                                a["id"], aw.eve_id, 0.0)
    assert (b.max_turns, b.max_wall_clock_s, b.max_tokens) == (3, 600, 5000)
    b2 = aw.mgr.effective_budget(AgentSpec.model_validate(store.get_agent(a["id"])["spec"]),
                                 a["id"], "someone-else", 0.0)
    assert (b2.max_turns, b2.max_tokens) == (10, None)


def test_done_and_approval_notifications_reach_the_chat_and_the_phone(aw):
    a = make_agent(aw.owner, "Notifier", published=["*"])
    thread = aw.owner.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:approval",
                                            "origin_thread_id": thread}).json()["task"]["id"]
    rid = wait_state(aw.owner, tid, "awaiting_approval")["pending_approvals"][0]["request_id"]
    aw.owner.post(f"/api/approvals/{rid}", json={"decision": "allow"})
    wait_state(aw.owner, tid, "succeeded")
    kinds = [k for k, _, _ in aw.notes.pushes]
    assert "approval" in kinds and "task_done" in kinds
    assert all(title in ("A task needs your approval", "A task finished", "A task failed")
               for _, _, title in aw.notes.pushes), "a push carries no task content"
    texts = [t for tid_, t, _ in aw.notes.posts if tid_ == thread]
    assert any("Assigned to **Notifier**" in t for t in texts)
    assert any("needs your approval to use `Bash`" in t for t in texts)
    assert any("finished" in t for t in texts)
    cards = [m.get("task_card") for _, _, m in aw.notes.posts]
    assert all(c and c["task_id"] == tid for c in cards)
    # the message that carries the live card is remembered on the task
    assert aw.owner.get(f"/api/tasks/{tid}").json()["task"]["origin_message_id"] == "msg-1"


def test_a_task_can_only_attach_to_a_chat_the_caller_may_see(aw):
    a = make_agent(aw.owner, published=["*"])
    r = aw.eve.post("/api/tasks", json={"agent_id": a["id"], "prompt": "x", "origin_thread_id": "nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "thread_not_found"


# --------------------------------------------------------------------------- #
# Steering, stopping, resuming
# --------------------------------------------------------------------------- #


def test_steer_interrupt_resume_and_cancel(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    wait_state(aw.owner, tid, "running")
    assert aw.owner.post(f"/api/tasks/{tid}/send", json={"message": "go left"}).json()["mode"] == "steered"
    done = wait_state(aw.owner, tid, "succeeded")
    texts = "".join(e.get("text", "") for e in aw.owner.get(f"/api/tasks/{tid}/events").json()["events"]
                    if e["type"] == "text_delta")
    assert "steered: go left" in texts
    # a finished task continues on the same session with a follow-up
    assert done["resumable"] is True
    assert aw.owner.post(f"/api/tasks/{tid}/send", json={"message": "again"}).json()["mode"] == "resumed"
    time.sleep(0.2)                              # the follow-up turn is the fake's short "resume" script
    wait_state(aw.owner, tid, "succeeded")
    # interrupt -> resumable; cancel -> not
    t2 = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    wait_state(aw.owner, t2, "running")
    assert aw.owner.post(f"/api/tasks/{t2}/interrupt").status_code == 202
    t = wait_state(aw.owner, t2, "cancelled")
    assert t["reason"] == "interrupted" and t["resumable"] is True
    assert aw.owner.post(f"/api/tasks/{t2}/resume", json={"message": "continue"}).status_code == 202
    wait_state(aw.owner, t2, "succeeded")
    t3 = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:long"}).json()["task"]["id"]
    wait_state(aw.owner, t3, "running")
    assert aw.owner.post(f"/api/tasks/{t3}/cancel").status_code == 202
    t = wait_state(aw.owner, t3, "cancelled")
    assert t["reason"] == "cancelled" and t["resumable"] is False
    assert aw.owner.post(f"/api/tasks/{t3}/resume", json={}).status_code == 409
    assert aw.owner.post(f"/api/tasks/{t3}/interrupt").status_code == 409


def test_events_replay_from_a_cursor_and_the_sequence_has_no_gaps(aw):
    a = make_agent(aw.owner, published=["*"])
    tid = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:subagent"}).json()["task"]["id"]
    wait_state(aw.owner, tid, "succeeded")
    evs = aw.owner.get(f"/api/tasks/{tid}/events").json()["events"]
    assert [e["seq"] for e in evs] == list(range(len(evs)))
    tail = aw.owner.get(f"/api/tasks/{tid}/events?after_seq=4").json()["events"]
    assert [e["seq"] for e in tail] == list(range(5, len(evs)))
    started = [e for e in evs if e["type"] == "subagent_started"][0]
    child = [e for e in evs if e["type"] == "text_delta" and e.get("parent_id")][0]
    assert child["parent_id"] == started["subagent_id"]          # the tree the UI draws


def test_task_listing_filters(aw):
    a = make_agent(aw.owner, "One", published=["*"])
    b = make_agent(aw.owner, "Two", published=["*"])
    t1 = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "scenario:simple"}).json()["task"]["id"]
    t2 = aw.owner.post("/api/tasks", json={"agent_id": b["id"], "prompt": "scenario:error_refused"}).json()["task"]["id"]
    wait_state(aw.owner, t1, "succeeded")
    wait_state(aw.owner, t2, "failed")
    ids = lambda q: [t["id"] for t in aw.owner.get("/api/tasks" + q).json()["tasks"]]    # noqa: E731
    assert set(ids("")) == {t1, t2}
    assert ids("?state=failed") == [t2]
    assert ids("?state=succeeded,failed&agent_id=" + a["id"]) == [t1]
    assert ids("?since=2999-01-01") == [] and set(ids("?until=2999-01-01")) == {t1, t2}
    assert aw.owner.get("/api/tasks?state=bogus").status_code == 422
    assert aw.owner.get("/api/tasks?since=yesterday").status_code == 422
    assert ids("?limit=1") == [t2]                                   # newest first
    row = aw.owner.get("/api/tasks").json()["tasks"][0]
    assert "prompt" not in row and "result_text" not in row          # list rows carry no transcript


# --------------------------------------------------------------------------- #
# Credentials: write-only, and never a subscription login
# --------------------------------------------------------------------------- #

FAKE_KEY = "sk-ant-api03-" + "A1b2C3d4" * 6


def test_the_claude_key_is_stored_but_never_returned(aw, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = aw.owner.put("/api/agents/credentials/claude", json={"route": "api_key", "api_key": FAKE_KEY})
    assert r.status_code == 200
    assert r.json()["api_key_set"] is True and r.json()["api_key_source"] == "stored"
    bodies = [aw.owner.get(p).text for p in ("/api/agents/credentials/claude", "/api/agents/backends",
                                             "/api/agents", "/api/agents/settings")]
    assert all(FAKE_KEY not in b and "A1b2C3d4A1b2" not in b for b in bodies)
    assert aw.mgr.secrets.get("ANTHROPIC_API_KEY") == FAKE_KEY            # it reached the secret store
    # a subscription login token is refused by name, as is a plain string
    for bad in ("sk-ant-oat01-" + "z" * 40, "hunter2", FAKE_KEY + " extra"):
        r = aw.owner.put("/api/agents/credentials/claude", json={"route": "api_key", "api_key": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "agent_credentials_invalid", bad
    r = aw.owner.delete("/api/agents/credentials/claude")
    assert r.json()["api_key_set"] is False


def test_there_is_no_subscription_route(aw):
    bad = aw.owner.put("/api/agents/credentials/claude", json={"route": "claude_ai_login"})
    assert bad.status_code == 422
    st = aw.owner.get("/api/agents/credentials/claude").json()
    assert set(st["routes"]) == {"api_key", "bedrock", "google", "foundry"}


def test_the_backend_list_says_what_each_one_needs(aw, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    got = {b["name"]: b for b in aw.owner.get("/api/agents/backends").json()["backends"]}
    assert got["claude_sdk"]["label"] == "Claude Agent" and got["openclaw"]["label"] == "OpenClaw"
    assert got["claude_sdk"]["needs_api_key"] is True
    assert got["claude_sdk"]["capabilities"]["data_leaves_host"] is True
    assert got["openclaw"]["capabilities"]["same_host_required"] is True
    assert got["claude_sdk"]["capabilities"]["approvals"] is True
    assert got["openclaw"]["capabilities"]["approvals"] is False
    # no claude.ai login anywhere in the answer
    text = aw.owner.get("/api/agents/backends").text.lower()
    assert "claude.ai" not in text and "subscription" not in text and "oauth" not in text


def test_a_backend_that_is_not_installed_refuses_the_task_politely(aw, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = make_agent(aw.owner, "NoKey", published=["*"], spec=spec(backend="claude_sdk", tool_allowlist=["Read"]))
    r = aw.owner.post("/api/tasks", json={"agent_id": a["id"], "prompt": "hi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "agent_backend_unavailable"
    avail = aw.owner.get("/api/agents/available").json()["agents"]
    assert [x for x in avail if x["id"] == a["id"]][0]["available"] is False


def test_mcp_secrets_are_write_only_and_listed_by_name(aw):
    make_agent(aw.owner, "WithMcp", published=["*"], spec=spec(tool_allowlist=["Read"], mcp_servers={
        "sf": {"transport": "http", "url": "http://localhost:9/mcp",
               "header_secrets": {"Authorization": "STUDIOFORGE_MCP_KEY_RO"}}}))
    assert aw.owner.get("/api/agents/secrets").json()["secrets"] == [
        {"name": "STUDIOFORGE_MCP_KEY_RO", "set": False}]
    assert aw.owner.put("/api/agents/secrets/ANTHROPIC_API_KEY", json={"value": "x"}).status_code == 422
    r = aw.owner.put("/api/agents/secrets/STUDIOFORGE_MCP_KEY_RO", json={"value": "ro-key-123456"})
    assert r.status_code == 200 and "ro-key-123456" not in r.text
    assert aw.owner.get("/api/agents/secrets").json()["secrets"][0]["set"] is True
    assert "ro-key-123456" not in aw.owner.get("/api/agents/secrets").text
    assert aw.mgr.secrets.get("STUDIOFORGE_MCP_KEY_RO") == "ro-key-123456"
    aw.owner.delete("/api/agents/secrets/STUDIOFORGE_MCP_KEY_RO")


def test_settings_roundtrip_and_the_flag_defaults_off(aw):
    s = aw.owner.get("/api/agents/settings").json()
    assert s["unified_dispatch"] is False and s["retention_days"] == 30
    r = aw.owner.put("/api/agents/settings", json={"retention_days": 7, "max_concurrent": 2})
    assert r.json()["retention_days"] == 7 and r.json()["max_concurrent"] == 2
    assert aw.owner.put("/api/agents/settings", json={"retention_days": -1}).status_code == 422
    assert agents_service.unified_dispatch_enabled() is False
    aw.owner.put("/api/agents/settings", json={"unified_dispatch": True})
    assert agents_service.unified_dispatch_enabled() is True
    aw.owner.put("/api/agents/settings", json={"unified_dispatch": False})
