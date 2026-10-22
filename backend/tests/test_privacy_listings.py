"""Privacy matrix, listings: thread list, search, export, unread, bots, characters, files, tasks,
advisor status, Image Studio lists and every parameterless GET. For each principal: only its own
threads and the bots it may use, and no sentinel of anybody else anywhere."""
from __future__ import annotations

import json

import pytest
from privacy_fixtures import (
    ACTORS,
    HOUSEHOLD_AGENT,
    HOUSEHOLD_OTHER,
    OWNERS,
    PRIVACY_MARKS,
    PROFILE_ACTORS,
    SENTINEL_RE,
    actor_permissions,
    build_install,
    foreign_intact,
    json_or_none,
    leaks,
    readable_owners,
)

pytestmark = PRIVACY_MARKS

STUDIO = "/api/apps/imagestudio"
#: parameterless GETs with side effects, host probes or huge output are not swept
SKIP_PREFIX = ("/api/local", "/api/openclaw", "/api/harness", "/api/dashboard/deep", "/api/dashboard/logs",
               "/api/media", "/api/studioforge", "/api/mail", "/api/practice", "/api/backups",
               "/api/search", "/api/export", "/api/threads", "/api/auth/api-token")


def _get(inst, actor, path, **params):
    return inst.client(actor).get(path, params=params or None)


def _no_leak(inst, actor, r, label):
    lk = leaks(inst, actor, r.content.decode("utf-8", "replace"))
    assert not lk, f"{actor}: {label} (HTTP {r.status_code}) leaks {lk}"


@pytest.mark.parametrize("actor", ACTORS)
def test_thread_lists_search_export_unread(world, monkeypatch, actor):
    inst = build_install(world, monkeypatch, actor)
    problems = []
    all_bots = ["h_direct", "h_other", "h_agent", "h_advisor"] + [
        r.ids["bot"] for (k, o), r in inst.res.items() if k == "personal_bot"]
    for bot in all_bots:
        r = _get(inst, actor, "/api/threads", bot_id=bot, include_archived="true")
        lk = leaks(inst, actor, r.content.decode("utf-8", "replace"))
        if lk:
            problems.append(f"GET /api/threads?bot_id={bot}: leaks {lk}")
        if actor in PROFILE_ACTORS and bot == "h_direct":
            th = inst.get("thread", actor)
            if th and actor in OWNERS and th.sentinel not in r.text:
                problems.append(f"{actor}: own thread missing from the list ({r.status_code})")
    for owner in OWNERS:
        s = inst.sent("thread", owner)
        r = _get(inst, actor, "/api/search", q=s)
        body = r.content.decode("utf-8", "replace")
        # INT-1: /api/search echoes the caller's own query back ({"query": q, "results": [...]}),
        # so the sentinel is judged in everything EXCEPT that echo (results, snippets, titles).
        j = json_or_none(r)
        if isinstance(j, dict) and j.get("query") == s:
            body = json.dumps({k: v for k, v in j.items() if k != "query"})
        if owner in readable_owners(actor) and actor in PROFILE_ACTORS:
            if r.status_code != 200 or s not in body:
                problems.append(f"{actor}: search for own sentinel found nothing ({r.status_code})")
        elif s in body:
            problems.append(f"{actor}: search for {owner}'s sentinel returned it (HTTP {r.status_code})")
    for fmt in ("json", "md", "html"):
        r = _get(inst, actor, "/api/export", format=fmt)
        lk = leaks(inst, actor, r.content.decode("utf-8", "replace"))
        if lk:
            problems.append(f"GET /api/export?format={fmt}: leaks {lk}")
        if actor in OWNERS and r.status_code == 200 and inst.sent("thread", actor) not in r.text:
            problems.append(f"{actor}: own export lacks own thread")
    r = _get(inst, actor, "/api/unread")
    lk = leaks(inst, actor, r.content.decode("utf-8", "replace"))
    if lk:
        problems.append(f"GET /api/unread leaks {lk}")
    assert not problems, "\n".join(problems)


def _bot_ids(r):
    j = json_or_none(r)
    rows = (j or {}).get("bots") or (j or {}).get("characters") or []
    return {b["id"] for b in rows if isinstance(b, dict)}


@pytest.mark.parametrize("actor", PROFILE_ACTORS)
def test_bot_and_character_rosters(world, monkeypatch, actor):
    inst = build_install(world, monkeypatch, actor)
    personal = {o: r.ids["bot"] for (k, o), r in inst.res.items() if k == "personal_bot"}
    foreign = {b for o, b in personal.items() if o != actor}
    mine = personal.get(actor)
    problems = []
    for path in ("/api/bots", "/api/bots/all", "/api/characters", "/api/characters/all"):
        r = _get(inst, actor, path)
        if path.endswith("/all") and "manage_characters" not in actor_permissions(actor):
            # INT-1: the "/all" rosters are the Admin's Characters manager (manage_characters);
            # anyone else is refused outright, which leaks nothing. The body is still judged.
            if r.status_code not in (401, 403) or leaks(inst, actor, r.text):
                problems.append(f"{path}: expected a refusal without manage_characters, got HTTP "
                                f"{r.status_code} {leaks(inst, actor, r.text)}")
            continue
        if r.status_code != 200:
            problems.append(f"{path}: HTTP {r.status_code}")
            continue
        ids = _bot_ids(r)
        text = r.text
        if ids & foreign:
            problems.append(f"{path}: lists another profile's personal bot {sorted(ids & foreign)}")
        if leaks(inst, actor, text):
            problems.append(f"{path}: leaks {leaks(inst, actor, text)}")
        if mine and path in ("/api/bots", "/api/characters") and mine not in ids:
            problems.append(f"{path}: own personal bot missing")
        if actor == "B1":
            if ids != {"h_direct"}:
                problems.append(f"{path}: a Basic profile with a one-bot list sees {sorted(ids)}")
        else:
            if HOUSEHOLD_OTHER not in ids and path in ("/api/bots", "/api/characters"):
                problems.append(f"{path}: household bot {HOUSEHOLD_OTHER} missing")
        if HOUSEHOLD_AGENT in ids and "dispatch_agents" not in actor_permissions(actor) and actor != "A0" \
                and actor != "A2":
            problems.append(f"{path}: agent-backed bot offered without dispatch_agents")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("actor", PROFILE_ACTORS)
def test_files_tasks_advisor_studio_lists(world, monkeypatch, actor):
    inst = build_install(world, monkeypatch, actor)
    perms = actor_permissions(actor)
    problems = []
    checks = [("/api/files", "file", "files_read"), ("/api/tasks", "task", "dispatch_agents"),
              (f"{STUDIO}/images", "studio", "use_images"), (f"{STUDIO}/state", "studio", "use_images"),
              (f"{STUDIO}/chat-images", "thread", "use_images"), (f"{STUDIO}/targets", "thread", "use_images"),
              ("/api/advisor/status", None, None)]
    for path, kind, perm in checks:
        r = _get(inst, actor, path)
        body = r.content.decode("utf-8", "replace")
        if leaks(inst, actor, body):
            problems.append(f"{path}: leaks {leaks(inst, actor, body)}")
        if kind and actor in OWNERS and perm in perms and inst.get(kind, actor):
            if r.status_code != 200 or inst.sent(kind, actor) not in body:
                problems.append(f"{path}: own {kind} missing (HTTP {r.status_code})")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("actor", ACTORS)
def test_every_parameterless_get_is_leak_free(world, monkeypatch, actor):
    from app import main
    inst = build_install(world, monkeypatch, actor)
    paths = sorted({r.path for r in main.app.routes
                    if "GET" in (getattr(r, "methods", None) or ()) and "{" not in r.path
                    and r.path.startswith("/api") and not r.path.startswith(SKIP_PREFIX)})
    problems = []
    for path in paths:
        r = inst.client(actor).get(path)
        if r.status_code >= 500:
            problems.append(f"GET {path}: server error {r.status_code}")
        lk = leaks(inst, actor, r.content.decode("utf-8", "replace"))
        if lk:
            problems.append(f"GET {path}: leaks {lk}")
    problems += [f"integrity: {p}" for p in foreign_intact(inst, actor)]
    assert not problems, "\n".join(problems)
