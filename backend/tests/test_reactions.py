"""Reaction-image tests: the registry, the one-shot pool, the fire chokepoint,
the per-bot gate, and the Safe-Mode matrix.

Hermetic like the rest of tests/: throwaway data dirs, no external image
generator, nothing touches a live data dir or a real agent gateway.
Run: cd backend && uv run pytest tests/test_reactions.py
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main, reactions
from app.database import Database

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _reset_reaction_caches() -> None:
    """Drop every per-bot cache.

    Both caches are keyed by bot id now, so a test that swaps DATA_DIR (or
    hand-writes a yaml under the module's nose) has to clear the whole dict —
    zeroing one bot's entry would leave another test's pool visible.
    """
    reactions._pool_cache.clear()
    reactions._bank_cache.clear()


@pytest.fixture
def rx_env(tmp_path, monkeypatch):
    """Isolated data dir with a seeded starter pack + a DB, plus a client factory."""
    # The reaction paths derive from DATA_DIR, so redirecting DATA_DIR is enough
    # — that is the property that keeps every other suite off the live data dir.
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    reactions.invalidate()
    _reset_reaction_caches()
    reactions.limiter.reset()

    config.ensure_dirs()
    reactions.seed_starter_pack()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client(client_addr=("127.0.0.1", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client

    for c in clients:
        c.__exit__(None, None, None)


# A browser stamps these; the fire endpoint uses them to tell a locked TAB
# apart from an on-box agent using the machine-to-machine exemption.
BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}


def _unlocked(make_client, pin="4321"):
    """A client holding a full session (PIN set + unlocked)."""
    auth.set_pin(pin)
    c = make_client()
    r = c.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text
    return c


def _fake_pool_item(suffix=".png", category="default", stem=None, bot_id=None):
    """Drop a real blob into one bot's moods-<bot>/<category>/ without touching
    the image rig.

    The folder IS the manifest, so this is now exactly what a hand-drop is:
    copy an image into the mood's directory and it is stock. ``bot_id`` picks
    whose pool (default: the default reaction bot). Returns the id the folder
    model derives for it (pool-<mood>-<stem>)."""
    import uuid
    name = f"{stem or uuid.uuid4().hex[:10]}{suffix}"
    d = reactions._moods_dir(bot_id) / category
    d.mkdir(parents=True, exist_ok=True)
    # Smallest valid PNG the image pipeline will accept.
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png"))
    (d / name).write_bytes(src.read_bytes())
    return reactions.pool_file_id(category, name)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_starter_pack_seeds_and_is_safe(rx_env):
    pack = reactions.load()
    assert len(pack.reactions) >= 10
    assert all(r.safe for r in pack.reactions), "starter cards ship Safe-Mode visible"
    assert all(reactions.image_path(r).is_file() for r in pack.reactions)


def test_seed_is_idempotent(rx_env):
    before = len(reactions.load().reactions)
    assert reactions.seed_starter_pack() == 0      # nothing re-rendered
    assert len(reactions.load().reactions) == before


def test_lookup_by_id_alias_and_name(rx_env):
    assert reactions.get("facepalm").id == "facepalm"
    assert reactions.get("smh").id == "facepalm"           # alias
    assert reactions.get("Mind Blown").id == "mindblown"   # loose name match
    assert reactions.get("no-such-thing") is None


def test_image_path_rejects_traversal(rx_env):
    bad = reactions.Reaction(id="x", name="x", file="builtin/../../etc/passwd")
    with pytest.raises(reactions.ReactionError):
        reactions.image_path(bad)
    for f in ("/etc/passwd", "builtin/x.svg", "pack/sub/dir.png", "nope/x.png",
              # Pool tier: the mood segment admits no dots, so `..` can never
              # ride in the middle; a flat (pre-folder) pool path is invalid.
              "moods/../x.png", "moods/m.m/x.png", "spent/x.png",
              "moods/m/x/y.png", "pool/x.png"):
        with pytest.raises(reactions.ReactionError):
            reactions.image_path(reactions.Reaction(id="x", name="x", file=f))


def test_pack_registry_rejects_pool_roots(rx_env):
    """A pack entry pointing into moods/ would make a one-shot image permanent."""
    pack = reactions.load()
    pack.reactions.append(reactions.Reaction(id="sneaky", name="Sneaky",
                                             file="moods/mad/x.png"))
    reactions.save(pack)
    assert reactions.load().by_id("sneaky") is None


# --------------------------------------------------------------------------- #
# Markers
# --------------------------------------------------------------------------- #


def test_marker_extraction(rx_env):
    text, ids = reactions.extract_markers("all done :react:nice: nothing else")
    assert ids == ["nice"]
    assert ":react:" not in text and text == "all done nothing else"


def test_unknown_marker_is_stripped_but_fires_nothing(rx_env):
    text, ids = reactions.extract_markers("hi :react:bogusname: there")
    assert ids == []
    assert ":react:" not in text


def test_marker_cap(rx_env):
    _, ids = reactions.extract_markers(":react:nice: :react:oof: :react:party:")
    assert len(ids) == reactions.MAX_MARKERS_PER_MESSAGE


def test_prose_is_not_a_marker(rx_env):
    text = "the ratio was 1:2:1 and the time is 10:30:00"
    assert reactions.extract_markers(text) == (text, [])


# --------------------------------------------------------------------------- #
# The one-shot pool
# --------------------------------------------------------------------------- #


def test_pool_draw_and_consume_is_one_shot(rx_env):
    rid = _fake_pool_item()
    drawn = reactions.pool_draw()
    assert drawn is not None and drawn.id == rid
    assert reactions.pool_consume(rid) is True
    # Gone: no longer resolvable, no longer drawable, and not consumable twice.
    assert reactions.pool_get(rid) is None
    assert reactions.get(rid) is None
    assert reactions.pool_draw() is None
    assert reactions.pool_consume(rid) is False


def test_consumed_image_is_kept_for_good(rx_env):
    """A fired image is chat history — the trace row re-opens it on click —
    so the sweep must never delete a spent blob, however old it is."""
    import os as _os
    import time as _t
    rid = _fake_pool_item(category="mad")
    assert reactions.pool_consume(rid) is True
    spent_blob = next(iter((config.REACTIONS_SPENT_DIR / "mad").glob("*.png")))
    assert spent_blob.is_file(), "in-flight clients still need the bytes"
    week_ago = _t.time() - 7 * 24 * 3600
    _os.utime(spent_blob, (week_ago, week_ago))
    assert reactions.pool_sweep(max_age_s=0) == 0
    assert spent_blob.is_file()
    assert reactions.get_for_display(rid) is not None
    assert reactions.get_for_display(rid).file == f"spent/mad/{spent_blob.name}"
    # ...while a hand-deleted blob simply stops resolving (the filesystem is
    # the manifest, so there is no ghost entry left behind to fail forever).
    spent_blob.unlink()
    assert reactions.get_for_display(rid) is None


def test_draw_keys_resolve_to_the_pool(rx_env):
    assert reactions.get("random") is None          # dry pool
    rid = _fake_pool_item()
    for key in ("random", "surprise", "fresh"):
        assert reactions.get(key).id == rid         # draw does not consume


def test_pool_low_water_and_daily_flags(rx_env):
    """Targets are PER MOOD: a mood under the low-water mark is urgent, a mood
    merely under its target waits for the nightly refill."""
    reactions.bank_save({"categories": {
        "solo": {"label": "Solo", "prompts": ["p"]},
        "duo": {"label": "Duo", "prompts": ["q"]},
    }})
    st = reactions.pool_load()
    st.config.per_mood = 4
    st.config.min_per_mood = 2
    reactions.pool_save(st)
    assert reactions.pool_status()["needs_refill"] is True      # both moods dry

    for _ in range(2):
        _fake_pool_item(category="solo")
    _fake_pool_item(category="duo")
    status = reactions.pool_status()
    assert status["needs_refill"] is True                       # duo still low
    assert status["low_moods"] == ["duo"]
    # The emergency path only owes the low mood — and owes it to FULL target.
    assert reactions.pool_deficits(only_low=True) == {"duo": 3}
    # The nightly path owes everything up to target.
    assert reactions.pool_deficits() == {"solo": 2, "duo": 3}

    _fake_pool_item(category="duo")
    assert reactions.pool_status()["needs_refill"] is False     # both at the mark
    assert reactions.pool_deficits(only_low=True) == {}

    # A bank stamped today is not due for tonight's refill.
    import time as _t
    st = reactions.pool_load()
    st.batch_date = _t.strftime("%Y-%m-%d")
    reactions.pool_save(st)
    assert reactions.pool_status()["due_daily"] is False


def test_pool_config_migrates_the_old_total_shape(rx_env):
    """A pre-per-mood manifest (`target`/`min_remaining` totals) must parse:
    the low-water number carries over per mood, the total target is dropped."""
    import yaml
    reactions._pool_path().write_text(yaml.safe_dump({
        "version": 1,
        "config": {"enabled": True, "target": 27, "min_remaining": 3,
                   "refresh_hour": 4, "max_per_cycle": 6},
        "items": [], "spent": [],
    }), encoding="utf-8")
    _reset_reaction_caches()
    cfg = reactions.pool_load().config
    assert cfg.min_per_mood == 3
    assert cfg.per_mood == 20                  # the new default
    assert cfg.enabled is True


def test_pool_list_marks_one_shot_entries(rx_env):
    _fake_pool_item()
    items = reactions.list_for(decoy=False)
    pool_items = [i for i in items if i.get("pool")]
    assert len(pool_items) == 1
    assert pool_items[0]["source"] == "pool"


# --------------------------------------------------------------------------- #
# Firing — the REST chokepoint
# --------------------------------------------------------------------------- #

# The shape a real machine caller uses (dispatch-react, the gateway, markers):
# an agent fire attributed to the one reaction-enabled bot. Fires are
# agent-only by design (2026-08-01): there is no user-facing fire control,
# and a user-kind fire is refused outright — see the gate tests below.
AGENT_FIRE = {"actor_kind": "agent", "bot_id": "main"}


def test_fire_named_reaction(rx_env):
    c = rx_env()
    r = c.post("/api/reactions/fire", json={"reaction": "nice", **AGENT_FIRE})
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert ev["reaction_id"] == "nice"
    assert ev["duration_ms"] == 10_000          # the brief's ten seconds
    assert ev["pool"] is False


def test_user_fires_are_refused_even_with_a_session(rx_env):
    """Reactions belong to the bots: a fire with NO actor_kind and NO bot_id is
    a user fire, and user fires are refused outright — even for a full admin
    session. The composer has no fire control any more; the only browser
    caller left is the manager's Test button, which fires as the bot."""
    c = _unlocked(rx_env)
    r = c.post("/api/reactions/fire", json={"reaction": "nice"}, headers=BROWSER)
    assert r.status_code == 403
    assert r.json()["detail"] == "Only agents may fire reactions"


def test_machine_user_fire_is_refused_too(rx_env):
    """The agent-kind requirement is API shape, not browser detection: even a
    loopback machine caller (no browser headers, keeps the session exemption)
    must claim agent kind or be refused."""
    c = rx_env()
    r = c.post("/api/reactions/fire", json={"reaction": "nice"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Only agents may fire reactions"


def test_fire_unknown_reaction_404s(rx_env):
    c = rx_env()
    assert c.post("/api/reactions/fire",
                  json={"reaction": "nope-nope", **AGENT_FIRE}).status_code == 404


def test_fire_pool_image_consumes_it(rx_env):
    c = rx_env()
    rid = _fake_pool_item()
    r = c.post("/api/reactions/fire", json={"reaction": rid, **AGENT_FIRE})
    assert r.status_code == 200, r.text
    assert r.json()["event"]["pool"] is True
    reactions.limiter.reset()
    # Second fire of the same picture is impossible — it no longer exists.
    assert c.post("/api/reactions/fire",
                  json={"reaction": rid, **AGENT_FIRE}).status_code == 404


def test_dry_pool_draw_is_503_not_404(rx_env):
    c = rx_env()
    r = c.post("/api/reactions/fire", json={"reaction": "random", **AGENT_FIRE})
    assert r.status_code == 503
    assert "fresh" in r.json()["detail"].lower()


def test_rate_limit_refuses_a_burst(rx_env):
    c = rx_env()
    assert c.post("/api/reactions/fire",
                  json={"reaction": "nice", **AGENT_FIRE}).status_code == 200
    r = c.post("/api/reactions/fire", json={"reaction": "oof", **AGENT_FIRE})
    assert r.status_code == 429                  # cooldown, same actor


def test_disabled_pack_refuses_every_fire(rx_env):
    c = rx_env()
    reactions.update_settings({"enabled": False})
    assert c.post("/api/reactions/fire",
                  json={"reaction": "nice", **AGENT_FIRE}).status_code == 403


# --------------------------------------------------------------------------- #
# Per-bot gate — only Nova ships enabled
# --------------------------------------------------------------------------- #


def test_only_reaction_enabled_bots_may_fire(rx_env):
    c = rx_env()
    assert config.get_bot("main").reactions is True
    assert config.get_bot("alpha").reactions is False

    assert c.post("/api/reactions/fire",
                  json={"reaction": "nice", "bot_id": "main"}).status_code == 200
    reactions.limiter.reset()
    r = c.post("/api/reactions/fire", json={"reaction": "nice", "bot_id": "alpha"})
    assert r.status_code == 403
    assert "enabled" in r.json()["detail"].lower()


def test_bot_gate_applies_to_threads_too(rx_env):
    """A thread inherits its bot's flag — the gate can't be dodged via thread_id."""
    c = rx_env()
    tid = c.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    r = c.post("/api/reactions/fire",
               json={"reaction": "nice", "thread_id": tid, "actor_kind": "agent"})
    assert r.status_code == 403
    # Pin the detail to the bot-capability gate — not the agent-only shape gate.
    assert "enabled" in r.json()["detail"].lower()


def test_bot_manager_can_enable_a_companion(rx_env):
    c = rx_env()
    bots = c.get("/api/bots/all").json()["bots"]
    payload = [{"id": b["id"], "order": b["order"], "visible": b["visible"],
                "safe": b["safe"], "reactions": b["id"] == "alpha"} for b in bots]
    assert c.put("/api/bots/order", json={"bots": payload}).status_code == 200
    config._invalidate_bots_cache()
    assert config.get_bot("alpha").reactions is True
    assert c.post("/api/reactions/fire",
                  json={"reaction": "nice", "bot_id": "alpha"}).status_code == 200


# --------------------------------------------------------------------------- #
# Traces — the reaction's home in the chat
# --------------------------------------------------------------------------- #


def test_fire_into_unknown_thread_is_404_not_silent_success(rx_env):
    """The trace persist swallows its own failures, so a typo'd thread id used
    to return ok:true while the trace silently FK-failed — the fire vanished
    with a success receipt. It must refuse up front instead."""
    c = rx_env()
    r = c.post("/api/reactions/fire",
               json={"reaction": "nice", "thread_id": "no-such-thread", **AGENT_FIRE})
    assert r.status_code == 404
    assert r.json()["detail"] == "Unknown thread"


def test_fire_with_thread_persists_a_trace(rx_env):
    """A thread-bound fire persists a system trace BEFORE broadcasting, and the
    event carries its id so clients can expand exactly that row in the chat."""
    c = rx_env()
    tid = c.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    r = c.post("/api/reactions/fire", json={
        "reaction": "nice", "thread_id": tid, "bot_id": "main",
        "actor": "Nova", "actor_kind": "agent"})
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert ev["thread_id"] == tid and ev["trace_id"]
    traces = [m for m in c.get(f"/api/threads/{tid}/messages").json()["messages"]
              if (m.get("metadata") or {}).get("kind") == "reaction"]
    assert len(traces) == 1
    t = traces[0]
    assert t["id"] == ev["trace_id"]
    assert t["role"] == "system"
    assert t["metadata"]["reaction_id"] == "nice"
    assert t["metadata"]["reaction_safe"] is True
    assert t["metadata"]["replayable"] is True
    assert "Nice" in t["content"]


def test_marker_reactions_leave_a_trace(rx_env):
    """`:react:` markers now embed in the chat: the reply is clean and a trace
    row lands right after it (previously markers left no trace at all)."""
    c = rx_env()
    tid = c.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    r = c.post("/api/inject", json={
        "thread_id": tid, "role": "assistant", "content": "Backups clean. :react:nice:"})
    assert r.status_code == 200, r.text
    msgs = c.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert not any(":react:" in (m["content"] or "") for m in msgs)
    assert msgs[0]["content"] == "Backups clean."
    traces = [m for m in msgs if (m.get("metadata") or {}).get("kind") == "reaction"]
    assert len(traces) == 1
    assert traces[0]["metadata"]["reaction_id"] == "nice"
    assert traces[0]["metadata"]["replayable"] is True


def test_marker_is_stripped_from_non_assistant_inject(rx_env):
    """The marker never reaches a bubble on ANY role; only assistant content
    fires the reaction (a user/system inject just loses the syntax)."""
    c = rx_env()
    tid = c.post("/api/threads", json={"bot_id": "alpha"}).json()["id"]
    r = c.post("/api/inject", json={
        "thread_id": tid, "role": "user", "content": "try :react:facepalm: ok"})
    assert r.status_code == 200, r.text
    msgs = c.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert msgs[0]["content"] == "try ok"
    assert not any((m.get("metadata") or {}).get("kind") == "reaction" for m in msgs)


def test_all_marker_message_persists_no_empty_bubble(rx_env):
    """A reply that is only markers fires the reaction but does not leave an
    empty assistant bubble — the trace row is all that lands."""
    c = rx_env()
    tid = c.post("/api/threads", json={"bot_id": "main"}).json()["id"]
    r = c.post("/api/inject", json={
        "thread_id": tid, "role": "assistant", "content": " :react:party: "})
    assert r.status_code == 200, r.text
    msgs = c.get(f"/api/threads/{tid}/messages").json()["messages"]
    assert msgs, "the trace still lands"
    assert not any(m["role"] == "assistant" and not (m["content"] or "").strip()
                   for m in msgs)
    traces = [m for m in msgs if (m.get("metadata") or {}).get("kind") == "reaction"]
    assert len(traces) == 1 and traces[0]["metadata"]["reaction_id"] == "party"


def test_pool_consume_race_refunds_the_rate_limiter(rx_env, monkeypatch):
    """A 409 (two clients racing the same one-shot) is not the actor's fault —
    the limiter slot is returned so the next fire isn't cooldown-blocked."""
    c = rx_env()
    rid = _fake_pool_item()
    monkeypatch.setattr(reactions, "pool_consume",
                        lambda _rid, **_kw: False)
    r = c.post("/api/reactions/fire", json={"reaction": rid, "bot_id": "main"})
    assert r.status_code == 409, r.text
    monkeypatch.undo()
    # Without the refund this next fire would 429 (cooldown from the 409).
    r = c.post("/api/reactions/fire", json={"reaction": "nice", "bot_id": "main"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Safe Mode
# --------------------------------------------------------------------------- #


def test_decoy_sees_only_safe_reactions(rx_env):
    unlocked = _unlocked(rx_env)
    # Add an unsafe reaction, then check what a locked client is shown.
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    r = unlocked.post("/api/reactions",
                      files={"file": ("secret.png", src, "image/png")},
                      data={"name": "Secret", "safe": "false"})
    assert r.status_code == 200, r.text
    secret_id = r.json()["reaction"]["id"]

    decoy = rx_env()
    body = decoy.get("/api/reactions", headers=BROWSER).json()
    ids = {x["id"] for x in body["reactions"]}
    assert secret_id not in ids
    assert "facepalm" in ids
    assert body["can_manage"] is False and body["can_generate"] is False
    # Image retrieval follows the same flag.
    assert decoy.get(f"/api/reactions/{secret_id}/image").status_code == 403
    assert decoy.get("/api/reactions/facepalm/image").status_code == 200


def test_pack_curation_is_full_session_only(rx_env):
    """Curating the operator's permanent shelf needs a real session — a tokenless agent
    gets no more than a locked device does."""
    _unlocked(rx_env)
    for client, label in ((rx_env(), "agent"), (rx_env(), "browser")):
        h = {} if label == "agent" else BROWSER
        assert client.delete("/api/reactions/facepalm", headers=h).status_code == 403, label
        assert client.patch("/api/reactions/facepalm", json={"safe": True},
                            headers=h).status_code == 403, label
        assert client.post("/api/reactions/reseed", headers=h).status_code == 403, label
        src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
        assert client.post("/api/reactions", files={"file": ("x.png", src, "image/png")},
                           data={"name": "X"}, headers=h).status_code == 403, label


def test_locked_browser_cannot_reach_the_agent_routes(rx_env):
    """The prompt bank / pool / settings routes are session-exempt so on-box
    agents can drive them. A locked BROWSER must not inherit that bypass."""
    _unlocked(rx_env)
    tab = rx_env()
    for method, path, body in [
        ("get", "/api/reactions/prompts", None),
        ("put", "/api/reactions/prompts", {"categories": {"a": {"prompts": ["x"]}}}),
        ("get", "/api/reactions/pool", None),
        ("put", "/api/reactions/pool", {"values": {}}),
        ("post", "/api/reactions/pool/refill", None),
        ("put", "/api/reactions/settings", {"values": {}}),
        ("post", "/api/reactions/generate", {"prompt": "x"}),
    ]:
        kw = {"headers": BROWSER}
        if body is not None:
            kw["json"] = body
        r = getattr(tab, method)(path, **kw)
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"


def test_on_box_agent_can_read_and_edit_the_prompt_bank(rx_env):
    """The whole point of externalising the bank: an agent with no session, on
    loopback, can inspect it and change what the pool generates."""
    _unlocked(rx_env)                       # a PIN exists
    agent = rx_env()                        # no session, no browser headers
    body = agent.get("/api/reactions/prompts").json()
    assert body["prompts"]["categories"]
    r = agent.put("/api/reactions/prompts", json={"prompts": {
        "base": "a tiny dragon",
        "categories": {"grumpy": {"label": "Grumpy", "prompts": ["scowling"]}}}})
    assert r.status_code == 200, r.text
    assert reactions.bank_categories() == ["grumpy"]
    assert agent.get("/api/reactions/pool").status_code == 200
    assert agent.put("/api/reactions/settings",
                     json={"values": {"cooldown_ms": 0}}).status_code == 200


def test_on_box_agent_gets_the_true_list(rx_env):
    """GET /api/reactions is session-exempt for machines: `dispatch-react
    --list` and on-box agents must see the REAL pack, pool detail and
    reaction_bots — while a sessionless browser tab on loopback still gets the
    Safe-Mode view, exactly like the fire endpoint."""
    unlocked = _unlocked(rx_env)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    secret_id = unlocked.post("/api/reactions",
                              files={"file": ("s.png", src, "image/png")},
                              data={"name": "Secret", "safe": "false"}).json()["reaction"]["id"]
    pool_id = _fake_pool_item()

    agent = rx_env()                        # no session, no browser headers
    body = agent.get("/api/reactions").json()
    ids = {x["id"] for x in body["reactions"]}
    assert secret_id in ids and pool_id in ids
    assert "main" in body["reaction_bots"]        # Nova, truthfully reported
    assert "per_mood" in body["pool"]             # management detail included
    # Curation is still full-session only, and the list must say so.
    assert body["can_manage"] is False

    tab = rx_env()
    tab_body = tab.get("/api/reactions", headers=BROWSER).json()
    tab_ids = {x["id"] for x in tab_body["reactions"]}
    assert secret_id not in tab_ids and pool_id not in tab_ids
    assert tab_body["can_manage"] is False


def test_remote_list_without_key_degrades_to_safe_view(rx_env):
    """A remote caller with no API key must not 401 off the list route (it is
    the Safe-Mode picker's data source) — it gets the decoy view instead."""
    _unlocked(rx_env)
    remote = rx_env(client_addr=("203.0.113.50", 40000))
    r = remote.get("/api/reactions")
    assert r.status_code == 200
    body = r.json()
    assert body["can_manage"] is False
    safe_only = {x.id for x in reactions.load().reactions if x.safe}
    assert {x["id"] for x in body["reactions"]} <= safe_only
    # The other machine endpoints stay fail-closed for keyless remote callers.
    assert remote.get("/api/reactions/prompts").status_code == 401
    assert remote.post("/api/reactions/fire",
                       json={"reaction": "facepalm"}).status_code == 401


def test_decoy_cannot_fire_an_unsafe_reaction(rx_env):
    unlocked = _unlocked(rx_env)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    secret_id = unlocked.post("/api/reactions",
                              files={"file": ("s.png", src, "image/png")},
                              data={"name": "Secret", "safe": "false"}).json()["reaction"]["id"]
    decoy = rx_env()
    # Fired APP-WIDE as a claimed agent (no bot_id) so the 403 lands on the
    # safe-reaction filter itself, not the bot-visibility gate — and the detail
    # is pinned so this can never again pass on some other gate (a blanket
    # "Only agents may fire reactions" check once hid this whole matrix).
    r = decoy.post("/api/reactions/fire",
                   json={"reaction": secret_id, "actor": "Nova",
                         "actor_kind": "agent"},
                   headers=BROWSER)
    assert r.status_code == 403
    assert r.json()["detail"] == "Unlock for full access"
    reactions.limiter.reset()
    # A locked device has NO fire path at all — not even a safe starter card.
    # (Locked devices still SEE safe fires; the WS frame filter handles that.)
    r = decoy.post("/api/reactions/fire", json={"reaction": "facepalm"},
                   headers=BROWSER)
    assert r.status_code == 403
    assert r.json()["detail"] == "Unlock for full access"


def test_redactor_drops_unsafe_reaction_frames(rx_env):
    safe = {"type": "reaction", "reaction_id": "facepalm", "safe": True, "bot_id": "alpha"}
    assert main.redact_for_decoy(safe) is not None
    unsafe = {"type": "reaction", "reaction_id": "x", "safe": False}
    assert main.redact_for_decoy(unsafe) is None
    # Safe reaction, but attributed to a bot Safe Mode can't see.
    assert main.redact_for_decoy(
        {"type": "reaction", "reaction_id": "facepalm", "safe": True, "bot_id": "main"}) is None
    # Pool telemetry is management detail.
    assert main.redact_for_decoy({"type": "reaction_pool", "pool": {}}) is None


def test_redactor_anonymises_an_unsafe_trace(rx_env):
    msg = {"id": "1", "content": "⚡ Nova reacted · Secret", "media_url": None,
           "metadata": {"kind": "reaction", "reaction_id": "secret",
                        "reaction_name": "Secret", "reaction_safe": False, "actor": "Nova"}}
    out = main._redact_message_dict(msg)
    assert out["content"] == "⚡ Nova reacted"
    assert "reaction_id" not in out["metadata"] and "reaction_name" not in out["metadata"]
    # A safe reaction keeps its identity.
    msg["metadata"]["reaction_safe"] = True
    assert main._redact_message_dict(msg)["metadata"]["reaction_id"] == "secret"


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #


def test_upload_rejects_a_non_image(rx_env):
    c = _unlocked(rx_env)
    r = c.post("/api/reactions", files={"file": ("x.png", b"not a png", "image/png")},
               data={"name": "Bad"})
    assert r.status_code == 415


def test_upload_rejects_svg(rx_env):
    """SVG can carry <script> and would run same-origin — raster only."""
    c = _unlocked(rx_env)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = c.post("/api/reactions", files={"file": ("x.svg", svg, "image/svg+xml")},
               data={"name": "Bad"})
    assert r.status_code == 415


def test_delete_removes_the_blob(rx_env):
    c = _unlocked(rx_env)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    rid = c.post("/api/reactions", files={"file": ("x.png", src, "image/png")},
                 data={"name": "Temp"}).json()["reaction"]["id"]
    blob = reactions.image_path(reactions.get(rid))
    assert c.delete(f"/api/reactions/{rid}").status_code == 200
    assert reactions.get(rid) is None
    assert not blob.exists()


def test_upload_with_a_long_duplicate_name_terminates(rx_env):
    """Regression: _unique_id used to truncate the counter suffix away for a
    47+-char base, so the second upload of an identical long name spun forever
    ON THE EVENT LOOP — freezing the whole app. Two identical 60-char names
    must simply yield two distinct ids."""
    c = _unlocked(rx_env)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    name = "x" * 60
    ids = set()
    for _ in range(2):
        r = c.post("/api/reactions", files={"file": ("x.png", src, "image/png")},
                   data={"name": name})
        assert r.status_code == 200, r.text
        ids.add(r.json()["reaction"]["id"])
    assert len(ids) == 2
    assert all(len(i) <= 48 for i in ids)


def test_unique_id_reserves_room_for_its_counter(rx_env):
    """The 46/47/48-char boundary directly: every candidate must stay inside
    the 48-char id cap WITHOUT collapsing back to the taken base."""
    pack = reactions.load()
    for length in (46, 47, 48):
        base = "x" * length
        pack.reactions.append(reactions.Reaction(id=base, name=base, file="pack/x.png"))
        rid = reactions._unique_id(base, pack)
        assert rid != base and len(rid) <= 48
        pack.reactions.append(reactions.Reaction(id=rid, name=rid, file="pack/x.png"))
        assert reactions._unique_id(base, pack) not in {base, rid}


def test_image_response_carries_the_sandbox_csp(rx_env):
    c = rx_env()
    r = c.get("/api/reactions/facepalm/image")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_locked_browser_on_loopback_is_still_safe_mode(rx_env):
    """The idle auto-lock drops THIS box's own tab to Safe Mode. The fire
    endpoint's machine-to-machine exemption must not hand it a way round the
    safe-reaction filter just because it connected over loopback — not even if
    the tab CLAIMS to be an agent (actor_kind is cosmetic, not a trust signal)."""
    unlocked = _unlocked(rx_env)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    secret = unlocked.post("/api/reactions", files={"file": ("s.png", src, "image/png")},
                           data={"name": "Secret", "safe": "false"}).json()["reaction"]["id"]
    tab = rx_env(("127.0.0.1", 51000))          # same box, no session, browser headers
    # App-wide (no bot_id) so the 403 comes from the safe-reaction filter under
    # test; the detail is pinned so a wrong gate can never green this again.
    r = tab.post("/api/reactions/fire",
                 json={"reaction": secret, "actor": "Nova", "actor_kind": "agent"},
                 headers=BROWSER)
    assert r.status_code == 403
    assert r.json()["detail"] == "Unlock for full access"
    # An on-box AGENT (no browser headers) keeps the exemption it needs.
    reactions.limiter.reset()
    agent = rx_env(("127.0.0.1", 51001))
    assert agent.post("/api/reactions/fire",
                      json={"reaction": secret, **AGENT_FIRE}).status_code == 200


def test_a_hand_deleted_blob_is_simply_gone(rx_env):
    """The folders are the manifest, so there is no ghost state to reconcile:
    deleting a ready blob from disk removes it from the pool entirely — never
    drawable, never resolvable, and it can't sit in the picker failing."""
    rid = _fake_pool_item(category="mad")
    blob = next(iter((reactions._moods_dir() / "mad").glob("*.png")))
    blob.unlink()
    assert reactions.pool_draw() is None           # never hand out a missing image
    assert reactions.pool_get(rid) is None
    assert reactions.pool_status()["remaining"] == 0


def test_sweep_removes_only_stale_part_staging(rx_env):
    """The only wreckage the folder model can accumulate is a *.part stranded
    by a crash between a generation's copy and its atomic rename. The sweep
    unlinks those once safely old — and touches NOTHING else: a fresh .part
    may be the rig mid-copy, real images are stock, spent blobs are chat
    history."""
    import os as _os
    import time as _t
    rid = _fake_pool_item(category="mad")
    d = reactions._moods_dir() / "mad"
    old = _t.time() - 48 * 3600
    stale = d / "crashed.png.part"
    stale.write_bytes(b"x")
    _os.utime(stale, (old, old))
    fresh = d / "inflight.png.part"
    fresh.write_bytes(b"x")
    # An old READY image is stock with its turn still coming — never swept.
    ready = next(iter(d.glob("*.png")))
    _os.utime(ready, (old, old))

    assert reactions.pool_sweep() == 1
    assert not stale.exists()
    assert fresh.exists() and ready.exists()
    assert reactions.pool_draw().id == rid


def test_part_and_dotfiles_are_not_stock(rx_env):
    """Staging and junk files must be invisible to every reader: not drawable,
    not counted, not listed — only real images are stock."""
    d = reactions._moods_dir() / "mad"
    d.mkdir(parents=True, exist_ok=True)
    (d / "half.png.part").write_bytes(b"x")
    (d / ".ds-store-ish").write_bytes(b"x")
    (d / "notes.txt").write_bytes(b"x")
    assert reactions.pool_draw() is None
    assert reactions.pool_categories() == {}
    assert not [i for i in reactions.list_for(decoy=False) if i.get("pool")]


# --------------------------------------------------------------------------- #
# Settings / pool config — validate BEFORE persist
# --------------------------------------------------------------------------- #


def test_bad_settings_value_is_rejected_and_never_persisted(rx_env):
    """A junk value must 400 at the PUT and leave reactions.yaml untouched — it
    used to be written FIRST and validated only by the next load, bricking every
    reactions endpoint across restarts until the file was hand-repaired. Both
    PUTs are session-exempt for on-box agents, so one agent typo was enough."""
    c = _unlocked(rx_env)
    r = c.put("/api/reactions/settings", json={"values": {"cooldown_ms": "abc"}})
    assert r.status_code == 400
    assert "abc" not in config.REACTIONS_PATH.read_text(encoding="utf-8")
    reactions.invalidate()
    pack = reactions.load()                        # feature still loads
    assert pack.settings.cooldown_ms == 2_000      # value untouched


def test_bad_pool_value_is_rejected_and_never_persisted(rx_env):
    c = _unlocked(rx_env)
    r = c.put("/api/reactions/pool", json={"values": {"per_mood": "lots"}})
    assert r.status_code == 400
    assert "lots" not in (reactions._pool_path().read_text(encoding="utf-8")
                          if reactions._pool_path().exists() else "")
    _reset_reaction_caches()
    assert reactions.pool_load().config.per_mood == 20      # value untouched


def test_corrupted_yaml_on_disk_degrades_to_defaults(rx_env):
    """Second layer: a junk value that somehow REACHED disk (hand-edit, old bug)
    must cost only that one field — load()/pool_load() never raise, so the
    reactions feature keeps working while the file stays hand-editable."""
    import yaml as _yaml
    raw = _yaml.safe_load(config.REACTIONS_PATH.read_text(encoding="utf-8"))
    raw["settings"]["cooldown_ms"] = "abc"
    config.REACTIONS_PATH.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    reactions.invalidate()
    pack = reactions.load()                        # must not raise
    assert pack.settings.cooldown_ms == 2_000      # the field default
    assert pack.reactions                          # the rest of the pack intact

    reactions._pool_path().write_text(_yaml.safe_dump({
        "version": 1,
        "config": {"per_mood": "lots", "refresh_hour": None, "enabled": True},
        "items": "junk",
    }), encoding="utf-8")
    _reset_reaction_caches()
    cfg = reactions.pool_load().config             # must not raise
    assert cfg.per_mood == 20 and cfg.refresh_hour == 4
    # A junk `items:` list is simply ignored — the folders are the manifest.
    assert reactions.pool_status()["remaining"] == 0


# --------------------------------------------------------------------------- #
# The prompt bank — the file an agent edits to change what gets generated
# --------------------------------------------------------------------------- #


def test_bank_materialises_a_default(rx_env):
    bank = reactions.bank_load()
    assert reactions.bank_path().exists()
    assert bank["categories"], "a default bank must be usable out of the box"
    assert all(c["prompts"] for c in bank["categories"].values())


def test_compose_applies_base_expression_and_suffix(rx_env):
    reactions.bank_save({
        "base": "a purple-haired robot",
        "suffix": "flat vector, plain background",
        "categories": {"mad": {"label": "Mad", "expressions": ["furious glare"],
                               "prompts": ["{expr}, fists clenched"]}},
    })
    prompt, label = reactions.compose_prompt("mad")
    assert label == "Mad"
    assert prompt == "a purple-haired robot, furious glare, fists clenched, flat vector, plain background"


def test_compose_survives_a_category_with_no_expressions(rx_env):
    reactions.bank_save({"base": "", "categories": {
        "plain": {"label": "Plain", "prompts": ["a cat, {expr}"]}}})
    prompt, _ = reactions.compose_prompt("plain")
    assert "{expr}" not in prompt


def test_bank_tolerates_junk_without_dying(rx_env):
    """One malformed category must not stop the pool refilling. A messy KEY is
    coerced into a usable draw key rather than thrown away — an agent writing
    "BAD ID!!" gets `bad-id`, not a silently missing mood."""
    reactions.bank_save({"categories": {
        "good": {"label": "Good", "prompts": ["a cat"]},
        "no-prompts": {"label": "Empty", "prompts": []},       # nothing to generate
        "BAD ID!!": {"label": "Bad", "prompts": ["x"]},        # coerced
        "not-a-dict": "nope",                                  # wrong shape
    }})
    assert reactions.bank_categories() == ["good", "bad-id"]
    # An id that cannot be coerced at all is dropped rather than fatal.
    reactions.bank_save({"categories": {
        "good": {"label": "Good", "prompts": ["a cat"]},
        "!!!": {"label": "Nope", "prompts": ["x"]},
    }})
    assert reactions.bank_categories() == ["good"]
    # Underscores survive, so `task_complete` stays addressable as written.
    reactions.bank_save({"categories": {
        "task_complete": {"label": "Done", "prompts": ["a cat"]}}})
    assert reactions.bank_categories() == ["task_complete"]


def test_bank_refuses_to_save_nothing_usable(rx_env):
    with pytest.raises(reactions.ReactionError):
        reactions.bank_save({"categories": {}})


def test_a_mood_name_draws_fresh_then_falls_back_to_the_pack(rx_env):
    """`:react:thinking:` should get today's picture when there is one, and the
    starter card when that mood is dry — never nothing."""
    assert reactions.get("thinking").source == "builtin"      # pool empty
    rid = _fake_pool_item(category="thinking")
    drawn = reactions.get("thinking")
    assert drawn.id == rid and drawn.source == "pool"
    reactions.pool_consume(rid)
    assert reactions.get("thinking").source == "builtin"      # graceful fallback


def test_pool_categories_counts_what_is_on_hand(rx_env):
    _fake_pool_item(category="mad")
    _fake_pool_item(category="mad")
    _fake_pool_item(category="smug")
    assert reactions.pool_categories() == {"mad": 2, "smug": 1}


def test_prompts_endpoint_round_trips(rx_env):
    c = _unlocked(rx_env)
    body = c.get("/api/reactions/prompts").json()
    assert "prompts" in body and "path" in body

    new = {"base": "a tiny dragon", "categories": {
        "smug": {"label": "Smug", "expressions": ["a smirk"], "prompts": ["{expr}, arms folded"]}}}
    r = c.put("/api/reactions/prompts", json={"prompts": new})
    assert r.status_code == 200, r.text
    assert r.json()["categories"] == ["smug"]
    assert reactions.compose_prompt("smug")[0].startswith("a tiny dragon, a smirk")

    # A bare body (no "prompts" wrapper) is accepted too.
    assert c.put("/api/reactions/prompts", json=new).status_code == 200
    assert c.put("/api/reactions/prompts", json={"categories": {}}).status_code == 400





def test_a_fired_pool_image_keeps_serving_for_reopen(rx_env):
    """The overlay is broadcast the instant the image is consumed, so every
    client's fetch lands after it has moved to spent/ — and the trace row can
    re-open it any time after that. The image endpoint must keep serving a
    spent image even after the sweep; only FIRING it again is impossible."""
    c = rx_env()
    rid = _fake_pool_item()
    assert c.post("/api/reactions/fire",
                  json={"reaction": rid, **AGENT_FIRE}).status_code == 200
    r = c.get(f"/api/reactions/{rid}/image")
    assert r.status_code == 200 and len(r.content) > 0
    # ...but it is still un-fireable: one-shot is enforced by the FIRE resolver.
    assert reactions.get(rid) is None
    reactions.limiter.reset()
    assert c.post("/api/reactions/fire",
                  json={"reaction": rid, **AGENT_FIRE}).status_code == 404
    # The sweep is staging-cleanup only — the image still serves.
    reactions.pool_sweep(max_age_s=0)
    assert c.get(f"/api/reactions/{rid}/image").status_code == 200


# --------------------------------------------------------------------------- #
# Mood folders — the filesystem is the manifest
# --------------------------------------------------------------------------- #


def test_hand_dropped_file_is_immediately_drawable(rx_env):
    """Operator workflow: copy a picture into moods/<mood>/ and it just works —
    exact id ↔ file mapping, no registry write, one-shot on fire like any
    other pool image, kept in spent/<mood>/ for the trace afterwards."""
    c = rx_env()
    d = reactions._moods_dir() / "bravo"
    d.mkdir(parents=True, exist_ok=True)
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    (d / "My Great Pic.png").write_bytes(src)

    r = reactions.get("pool-bravo-my-great-pic")   # slugged stem, exact mapping
    assert r is not None and r.source == "pool"
    # The reference names the owning bot's folder, not a shared one.
    assert r.file == "moods-main/bravo/My Great Pic.png"
    # The mood name draws the fresh drop in preference to the pack card.
    assert reactions.get("bravo").source == "pool"

    resp = c.post("/api/reactions/fire",
                  json={"reaction": "pool-bravo-my-great-pic", **AGENT_FIRE})
    assert resp.status_code == 200, resp.text
    assert (config.REACTIONS_SPENT_DIR / "bravo" / "My Great Pic.png").is_file()
    assert reactions.get("pool-bravo-my-great-pic") is None    # one-shot
    assert reactions.get("bravo").source == "builtin"          # dry → pack card
    # The trace can still re-open the picture.
    assert c.get("/api/reactions/pool-bravo-my-great-pic/image").status_code == 200


def test_manual_mood_is_counted_but_never_auto_refilled(rx_env):
    """A hand-made folder with no prompt-bank entry is a valid manual-only
    mood: drawable and visible in the status counts, but excluded from every
    refill computation — deficits only ever cover the prompt bank."""
    reactions.bank_save({"categories": {"mad": {"label": "Mad", "prompts": ["p"]}}})
    rid = _fake_pool_item(category="handmade")
    status = reactions.pool_status()
    assert status["moods"]["handmade"] == 1                 # counted
    assert status["remaining"] == 1
    assert status["low_moods"] == ["mad"]                   # only the bank mood
    assert reactions.pool_deficits() == {"mad": status["per_mood"]}
    assert reactions.pool_deficits(only_low=True) == {"mad": status["per_mood"]}
    # Drawable by folder name and by the random draw key.
    assert reactions.get("handmade").id == rid
    assert reactions.get("random").id == rid


def test_racing_consume_has_exactly_one_winner(rx_env):
    """Two clients firing the same one-shot at once: the atomic rename into
    spent/<mood>/ IS the lock, so exactly one consume reports True and the
    blob ends up in spent/ exactly once."""
    import threading
    rid = _fake_pool_item(category="mad")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(reactions.pool_consume(rid))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert len(list((config.REACTIONS_SPENT_DIR / "mad").glob("*.png"))) == 1
    assert not list((reactions._moods_dir() / "mad").glob("*.png"))


def test_migration_moves_the_flat_layout_into_mood_folders(rx_env):
    """The startup migration: flat pool/ + per-item manifest → mood folders.
    Blobs land under their manifest category (strays under default/), the
    yaml keeps config + batch_date but loses the item lists, legacy trace ids
    keep resolving for display, and the marker makes a re-run a no-op."""
    import yaml as _yaml
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    pool = config.REACTIONS_POOL_DIR
    spent = config.REACTIONS_SPENT_DIR
    pool.mkdir(parents=True, exist_ok=True)
    spent.mkdir(parents=True, exist_ok=True)
    (pool / "pool-abc123.png").write_bytes(src)
    (pool / "pool-stray99.png").write_bytes(src)      # manifest lost track of it
    (pool / "half.png.part").write_bytes(b"x")        # staging junk — not a blob
    (spent / "pool-def456.png").write_bytes(src)
    # A dir this old carries the SINGLE-POOL file name — the migration has to
    # find its item lists there, and hand the result to the default bot.
    config.REACTION_POOL_PATH.write_text(_yaml.safe_dump({
        "version": 1,
        "config": {"per_mood": 7, "min_per_mood": 3, "refresh_hour": 5,
                   "max_per_cycle": 4, "safe": True, "enabled": True},
        "batch_date": "2026-07-31",
        "items": [{"id": "pool-abc123", "file": "pool/pool-abc123.png",
                   "label": "Mad", "category": "mad"}],
        "spent": [{"id": "pool-def456", "file": "spent/pool-def456.png",
                   "label": "Smug", "category": "smug",
                   "spent_at": "2026-07-31T12:00:00"}],
    }), encoding="utf-8")
    _reset_reaction_caches()

    out = reactions.migrate_mood_folders()
    assert out == {"migrated": True, "ready": 1, "spent": 1, "strays": 1}
    # Old blob names lose the redundant "pool-" prefix on the way in.
    assert (reactions._moods_dir() / "mad" / "abc123.png").is_file()
    assert (config.REACTIONS_SPENT_DIR / "smug" / "def456.png").is_file()
    assert (reactions._moods_dir() / "default" / "stray99.png").is_file()
    assert not [p for p in pool.iterdir() if p.is_file()
                and p.suffix.lower() in reactions.IMAGE_EXTS]   # flat dir empty

    # The rewrite lands on the per-bot file, and the single-pool one is gone.
    assert not config.REACTION_POOL_PATH.exists()
    raw = _yaml.safe_load(reactions._pool_path().read_text(encoding="utf-8"))
    assert "items" not in raw and "spent" not in raw
    st = reactions.pool_load()
    assert st.config.per_mood == 7 and st.config.safe is True
    assert st.batch_date == "2026-07-31"

    # The migrated ready image is drawable under the folder model...
    assert reactions.get("mad").id == "pool-mad-abc123"
    # ...and the LEGACY ids old chat traces carry keep resolving for display.
    assert reactions.get_for_display("pool-abc123") is not None
    assert reactions.get_for_display("pool-def456").file == "spent/smug/def456.png"
    # One-shot still holds: a legacy spent id never resolves for firing.
    assert reactions.get("pool-def456") is None

    # Idempotent: the marker makes the second run a no-op.
    assert (config.REACTIONS_DIR / reactions.MIGRATION_MARKER).exists()
    assert reactions.migrate_mood_folders() == {"migrated": False}


def test_migration_runs_at_startup_and_is_safe_on_a_fresh_dir(rx_env):
    """The migration is wired into the app lifespan: simply starting the
    service migrates an old data dir — and on a brand-new one it just plants
    the marker and the moods/ root without inventing any state."""
    rx_env()                            # entering the client runs the lifespan
    assert (config.REACTIONS_DIR / reactions.MIGRATION_MARKER).exists()
    assert reactions._moods_dir().is_dir()
    assert reactions.pool_status()["remaining"] == 0


# --------------------------------------------------------------------------- #
# Per-bot pools
#
# Every reactions-enabled bot owns a shelf, a prompt bank and a schedule of its
# own. The invariant these tests defend is that the shelves never touch: one
# character's picture must not be spendable by another, or "fired exactly once,
# by this bot" stops meaning anything.
#
# The suite's roster (tests/conftest.py) ships exactly ONE reactions bot,
# `main`, which is therefore the DEFAULT — the bot an omitted bot_id resolves
# to. `Scout` is enabled per-test as the companion: it sorts AFTER main, so the
# default never moves, and its capitalised id doubles as a check that a roster
# id which is not lowercase still makes a legal directory name.
# --------------------------------------------------------------------------- #


COMPANION = "Scout"


def _enable_reactions(*bot_ids: str) -> None:
    """Turn the reactions capability on for these bots (plus `main`)."""
    wanted = {"main", *bot_ids}
    config.save_bot_order([
        {"id": b.id, "order": b.order, "visible": b.visible, "safe": b.safe,
         "reactions": b.id in wanted}
        for b in config.load_bots()
    ])
    config._invalidate_bots_cache()


def test_the_default_bot_is_the_first_reactions_enabled_one(rx_env):
    """Which bot an omitted bot_id means is a ROSTER question, not a constant:
    a fork whose reactions bot is called something else must still get a
    working pool, and the legacy anchor is only the no-roster fallback."""
    assert reactions.default_bot_id() == "main"
    assert reactions.reaction_bots() == ["main"]
    _enable_reactions(COMPANION)
    assert reactions.reaction_bots() == ["main", COMPANION]
    assert reactions.default_bot_id() == "main"          # roster order decides

    # Turn the lot off: nothing to point at, so paths stay where they were
    # rather than moving to whatever happens to be first in the roster.
    config.save_bot_order([{"id": b.id, "order": b.order, "reactions": False}
                           for b in config.load_bots()])
    config._invalidate_bots_cache()
    assert reactions.reaction_bots() == []
    assert reactions.default_bot_id() == reactions.LEGACY_BOT_ID


def test_a_bot_id_can_never_escape_the_data_dir(rx_env):
    """bot_id arrives from query strings — it is a path component, so a
    traversal attempt must degrade to the default pool, not to /etc."""
    for junk in ("../../etc", "a/b", ".", "", "  ", None, "x" * 200):
        assert reactions.resolve_bot_id(junk) == "main"
        assert reactions._moods_dir(junk).parent == config.REACTIONS_DIR
        assert reactions._pool_path(junk).parent == config.DATA_DIR
        assert reactions.bank_path(junk).parent == config.DATA_DIR


def test_each_bot_draws_only_from_its_own_shelf(rx_env):
    _enable_reactions(COMPANION)
    mine = _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)
    assert mine != theirs

    assert reactions.pool_categories() == {"mad": 1}
    assert reactions.pool_categories(COMPANION) == {"mad": 1}
    assert reactions.pool_draw(bot_id="main").id == mine
    assert reactions.pool_draw(bot_id=COMPANION).id == theirs
    # A companion's id is simply not there as far as the default pool is
    # concerned — the draw keys and the mood keys are scoped the same way.
    assert reactions.get(theirs, bot_id="main") is None
    assert reactions.get(mine, bot_id=COMPANION) is None
    assert reactions.get("mad", bot_id=COMPANION).id == theirs


def test_one_bots_fire_never_spends_another_bots_image(rx_env):
    """The one-shot lock is per shelf: consuming through the wrong bot must
    fail rather than quietly retiring a picture the other bot still owns."""
    _enable_reactions(COMPANION)
    mine = _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)

    assert reactions.pool_consume(theirs, bot_id="main") is False
    assert reactions.pool_categories(COMPANION) == {"mad": 1}   # still stocked
    assert reactions.pool_consume(mine, bot_id="main") is True
    assert reactions.pool_categories() == {}                    # only mine went
    assert reactions.pool_draw(bot_id=COMPANION).id == theirs


def test_fire_route_is_scoped_to_the_firing_bots_pool(rx_env):
    c = rx_env()
    _enable_reactions(COMPANION)
    mine = _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)

    # main cannot fire the companion's image at all...
    r = c.post("/api/reactions/fire",
               json={"reaction": theirs, "actor_kind": "agent", "bot_id": "main"})
    assert r.status_code == 404, r.text
    reactions.limiter.reset()
    # ...and firing its own leaves the companion's shelf untouched.
    assert c.post("/api/reactions/fire",
                  json={"reaction": mine, **AGENT_FIRE}).status_code == 200
    assert reactions.pool_categories(COMPANION) == {"mad": 1}

    reactions.limiter.reset()
    r = c.post("/api/reactions/fire",
               json={"reaction": theirs, "actor_kind": "agent", "bot_id": COMPANION})
    assert r.status_code == 200, r.text
    assert reactions.pool_categories(COMPANION) == {}
    # Both pictures are still SERVABLE — spent is shared, and a trace row in
    # either thread has to be able to re-open its image.
    for rid in (mine, theirs):
        assert c.get(f"/api/reactions/{rid}/image").status_code == 200


def test_a_marker_fires_from_the_replying_bots_pool(rx_env):
    """`:react:mad:` in an agent reply draws from THAT agent's shelf — the
    marker path is where a bot's own character shows up in the chat."""
    _enable_reactions(COMPANION)
    _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)

    text, ids = reactions.extract_markers("done :react:mad:", bot_id=COMPANION)
    assert text.strip() == "done"
    assert ids == [theirs]


def test_pool_routes_take_a_bot_id(rx_env):
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)
    _fake_pool_item(category="mad", bot_id=COMPANION)

    mine = c.get("/api/reactions/pool").json()["pool"]
    theirs = c.get("/api/reactions/pool", params={"bot_id": COMPANION}).json()["pool"]
    assert mine["bot_id"] == "main" and theirs["bot_id"] == COMPANION
    assert mine["remaining"] == 0 and theirs["remaining"] == 1
    assert theirs["dir"].endswith(f"moods-{COMPANION}")

    # A junk bot_id resolves to the default rather than 500ing or writing a
    # file called "../..".
    assert c.get("/api/reactions/pool",
                 params={"bot_id": "../evil"}).json()["pool"]["bot_id"] == "main"

    # Config writes are scoped too: the companion's knobs, nobody else's.
    r = c.put("/api/reactions/pool",
              json={"values": {"bot_id": COMPANION, "per_mood": 3}})
    assert r.status_code == 200, r.text
    assert r.json()["pool"]["bot_id"] == COMPANION
    assert reactions.pool_load(COMPANION).config.per_mood == 3
    assert reactions.pool_load("main").config.per_mood == 20      # untouched
    assert reactions._pool_path(COMPANION).exists()


def test_pool_put_honours_a_top_level_bot_id(rx_env):
    """`{"bot_id": ..., "values": {...}}` — the shape every SIBLING endpoint
    takes — must write the named bot's config.

    It used to write the DEFAULT bot's and return 200: ReactionSettingsIn had
    no bot_id field, so pydantic dropped it and the handler only ever looked
    inside `values`. Silent cross-bot writes, with a success response.
    """
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)

    r = c.put("/api/reactions/pool",
              json={"bot_id": COMPANION, "values": {"per_mood": 7}})
    assert r.status_code == 200, r.text
    assert r.json()["pool"]["bot_id"] == COMPANION
    assert reactions.pool_load(COMPANION).config.per_mood == 7
    assert reactions.pool_load("main").config.per_mood == 20      # NOT touched


def test_pool_put_refuses_two_disagreeing_bot_ids(rx_env):
    """Both spellings, different bots: 400. Picking a winner silently is the
    same class of mistake as ignoring the field."""
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)

    r = c.put("/api/reactions/pool",
              json={"bot_id": COMPANION, "values": {"bot_id": "main", "per_mood": 7}})
    assert r.status_code == 400, r.text
    assert reactions.pool_load("main").config.per_mood == 20
    assert reactions.pool_load(COMPANION).config.per_mood == 20


def test_pool_put_still_takes_bot_id_inside_values(rx_env):
    """The old spelling keeps working — the Reaction Manager sends it."""
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)

    r = c.put("/api/reactions/pool",
              json={"values": {"bot_id": COMPANION, "per_mood": 5}})
    assert r.status_code == 200, r.text
    assert reactions.pool_load(COMPANION).config.per_mood == 5


def test_prompt_bank_routes_take_a_bot_id(rx_env):
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)

    body = {"base": "a small robot", "categories": {
        "beep": {"label": "Beep", "prompts": ["waving"]}}}
    r = c.put("/api/reactions/prompts", json={"bot_id": COMPANION, "prompts": body})
    assert r.status_code == 200, r.text
    assert r.json()["bot_id"] == COMPANION

    got = c.get("/api/reactions/prompts", params={"bot_id": COMPANION}).json()
    assert got["bot_id"] == COMPANION
    assert list(got["prompts"]["categories"]) == ["beep"]
    assert got["path"].endswith(f"reaction-prompts-{COMPANION}.yaml")
    # The default bot's bank is a different file with different moods.
    mine = c.get("/api/reactions/prompts").json()
    assert mine["bot_id"] == "main"
    assert mine["path"].endswith("reaction-prompts-main.yaml")
    assert list(mine["prompts"]["categories"]) != ["beep"]
    assert reactions.compose_prompt("beep", bot_id=COMPANION)[0].startswith(
        "a small robot")


def test_refill_route_targets_one_bot(rx_env, monkeypatch):
    c = _unlocked(rx_env)
    _enable_reactions(COMPANION)
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    monkeypatch.setattr(reactions, "pool_refill",
                        lambda *a, bot_id=None, **kw: 0)
    r = c.post("/api/reactions/pool/refill", params={"bot_id": COMPANION})
    assert r.status_code == 200, r.text
    assert r.json()["pool"]["bot_id"] == COMPANION


def test_the_refill_cycle_visits_every_reactions_bot(rx_env, monkeypatch):
    """The nightly pass is per bot: each shelf is filled against its OWN bank
    and its OWN targets, so enabling a second character does not quietly leave
    it empty forever (nor refill it out of the first one's budget)."""
    import asyncio
    rx_env()
    _enable_reactions(COMPANION)
    # Both banks non-empty and both pools due, so neither is skipped for a
    # reason other than the one under test.
    reactions.bank_save({"categories": {"mad": {"label": "Mad", "prompts": ["p"]}}})
    reactions.bank_save({"categories": {"mad": {"label": "Mad", "prompts": ["q"]}}},
                        bot_id=COMPANION)
    for bid in ("main", COMPANION):
        st = reactions.pool_load(bid)
        st.config.refresh_hour = 0
        st.batch_date = ""
        reactions.pool_save(st, bid)

    seen: list[str] = []
    # The lifespan sets this on the way out and never clears it, so an earlier
    # test's teardown would make the cycle return before doing anything.
    monkeypatch.setattr(main, "_shutting_down", False)
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)

    def _fake_refill(limit=None, *, bot_id=None, only_low=False):
        seen.append(bot_id)
        return 0                        # "rig made nothing" — ends the round
    monkeypatch.setattr(reactions, "pool_refill", _fake_refill)

    asyncio.run(main._reaction_pool_cycle())
    assert seen == ["main", COMPANION]


def test_pool_broadcast_carries_every_bots_status(rx_env, monkeypatch):
    """One frame, two readings: `pool` for a single bot's round, `pools` keyed
    by bot id for the whole picture — plus a `pool` alongside `pools` so a
    client written against the original single-pool frame still refreshes."""
    import asyncio
    rx_env()
    _enable_reactions(COMPANION)
    _fake_pool_item(category="mad", bot_id=COMPANION)

    frames: list[dict] = []

    async def _capture(frame):
        frames.append(frame)
    monkeypatch.setattr(main.manager, "broadcast", _capture)

    asyncio.run(main._broadcast_pool_state())
    assert frames[-1]["type"] == "reaction_pool"
    pools = frames[-1]["pools"]
    assert set(pools) == {"main", COMPANION}
    assert pools[COMPANION]["remaining"] == 1 and pools["main"]["remaining"] == 0
    assert pools["main"]["bot_id"] == "main"
    # Back-compat: the default bot's status rides along under the old key.
    assert frames[-1]["pool"] == pools["main"]

    asyncio.run(main._broadcast_pool_state(COMPANION))
    assert "pools" not in frames[-1]
    assert frames[-1]["pool"]["bot_id"] == COMPANION


def test_a_bot_without_a_pool_falls_back_to_the_pack(rx_env):
    """A newly enabled bot has no bank and no images. That is a QUIET state:
    the pack still answers, the status reads empty, and nothing is seeded on
    its behalf — a second character's images are a deliberate choice."""
    c = rx_env()
    _enable_reactions(COMPANION)
    assert reactions.bank_load(COMPANION)["categories"] == {}
    assert not reactions.bank_path(COMPANION).exists()

    status = reactions.pool_status(COMPANION)
    assert status["remaining"] == 0 and status["moods"] == {}
    assert status["needs_refill"] is False and status["deficit"] == 0
    assert reactions.pool_draw(bot_id=COMPANION) is None
    assert not [i for i in reactions.list_for(decoy=False, bot_id=COMPANION)
                if i.get("pool")]

    # A mood key still fires — it falls through to the shared pack card.
    r = reactions.get("thinking", bot_id=COMPANION)
    assert r is not None and r.source == "builtin"
    assert c.post("/api/reactions/fire",
                  json={"reaction": "thinking", "actor_kind": "agent",
                        "bot_id": COMPANION}).status_code == 200


def test_the_manager_list_shows_every_bots_pool(rx_env):
    _enable_reactions(COMPANION)
    mine = _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)
    ids = {i["id"] for i in reactions.list_for(decoy=False) if i.get("pool")}
    assert ids == {mine, theirs}
    scoped = {i["id"] for i in reactions.list_for(decoy=False, bot_id=COMPANION)
              if i.get("pool")}
    assert scoped == {theirs}


def test_safe_mode_sees_safe_pool_images_from_any_bot(rx_env):
    """Safe Mode is decided by the POOL's safe flag, per bot — a companion
    marked safe is visible to a locked device even though the default bot's
    pool is not."""
    _enable_reactions(COMPANION)
    mine = _fake_pool_item(category="mad")
    theirs = _fake_pool_item(category="mad", bot_id=COMPANION)
    st = reactions.pool_load(COMPANION)
    st.config.safe = True
    reactions.pool_save(st, COMPANION)

    ids = reactions.safe_ids()
    assert theirs in ids and mine not in ids


# --------------------------------------------------------------------------- #
# Migration: single pool → one pool per bot
# --------------------------------------------------------------------------- #


def _write_legacy_layout(bank: dict | None = None) -> None:
    """Lay down the pre-per-bot files: reactions/moods/, reaction-pool.yaml,
    reaction-prompts.yaml — exactly what an install upgrading into per-bot
    pools has on disk."""
    import yaml as _yaml
    src = next(config.REACTIONS_BUILTIN_DIR.glob("*.png")).read_bytes()
    d = config.REACTIONS_LEGACY_MOODS_DIR / "mad"
    d.mkdir(parents=True, exist_ok=True)
    (d / "abc123.png").write_bytes(src)
    config.REACTION_POOL_PATH.write_text(_yaml.safe_dump({
        "version": 2,
        "config": {"per_mood": 9, "min_per_mood": 2, "enabled": True},
        "batch_date": "2026-08-01",
    }), encoding="utf-8")
    config.REACTION_PROMPTS_PATH.write_text(_yaml.safe_dump(
        bank or {"base": "an old character", "categories": {
            "mad": {"label": "Mad", "prompts": ["stomping"]}}}),
        encoding="utf-8")
    _reset_reaction_caches()


def test_a_single_pool_install_is_handed_to_the_default_bot(rx_env):
    _write_legacy_layout()
    # Any load runs the migration — there is no separate "upgrade" step.
    st = reactions.pool_load()
    assert st.config.per_mood == 9 and st.batch_date == "2026-08-01"
    assert reactions._pool_path().exists()
    assert not config.REACTION_POOL_PATH.exists()

    assert reactions.bank_load()["base"] == "an old character"
    assert reactions.bank_path().exists()
    assert not config.REACTION_PROMPTS_PATH.exists()

    assert (reactions._moods_dir() / "mad" / "abc123.png").is_file()
    assert not config.REACTIONS_LEGACY_MOODS_DIR.exists()
    # The images kept their ids, so a trace written before the split still
    # resolves — and the reference now names the bot that owns them.
    r = reactions.get("pool-mad-abc123")
    assert r is not None and r.file == "moods-main/mad/abc123.png"
    assert reactions.pool_categories() == {"mad": 1}


def test_migrating_is_idempotent_and_survives_a_second_bot(rx_env):
    _write_legacy_layout()
    reactions.pool_load()                       # migrates
    _enable_reactions(COMPANION)
    _reset_reaction_caches()
    reactions.pool_load()                       # a second pass changes nothing
    assert reactions.pool_load().config.per_mood == 9
    assert reactions.pool_categories() == {"mad": 1}
    # The companion did NOT inherit the migrated pool: it starts empty.
    assert reactions.pool_categories(COMPANION) == {}
    assert reactions.bank_load(COMPANION)["categories"] == {}


def test_a_stale_generic_prompt_file_never_shadows_a_bots_bank(rx_env):
    """The trap this migration walks into: an install can end up with per-bot
    banks AND a leftover reaction-prompts.yaml holding stock defaults. Renaming
    that over a curated bank would silently replace a character's prompts, so
    an artefact whose per-bot counterpart already exists is left alone — and
    never read."""
    import yaml as _yaml
    _enable_reactions(COMPANION)
    reactions.bank_save({"base": "the real character", "categories": {
        "mad": {"label": "Mad", "prompts": ["curated"]}}})
    reactions.bank_save({"base": "the companion", "categories": {
        "beep": {"label": "Beep", "prompts": ["curated too"]}}}, bot_id=COMPANION)

    stale = {"base": "STALE DEFAULTS", "categories": {
        "nope": {"label": "Nope", "prompts": ["should never be read"]}}}
    config.REACTION_PROMPTS_PATH.write_text(_yaml.safe_dump(stale), encoding="utf-8")
    _reset_reaction_caches()

    assert reactions.bank_load()["base"] == "the real character"
    assert reactions.bank_load(COMPANION)["base"] == "the companion"
    # Left where it was — inert, not adopted, and not deleted behind the
    # operator's back either.
    assert config.REACTION_PROMPTS_PATH.exists()
    assert _yaml.safe_load(
        config.REACTION_PROMPTS_PATH.read_text(encoding="utf-8")) == stale


def test_a_half_migrated_dir_only_moves_what_is_missing(rx_env):
    """Per artefact, not all-or-nothing: a dir that got its pool file renamed
    but not its bank must finish the job, without touching the file that made
    it across."""
    import yaml as _yaml
    _write_legacy_layout()
    reactions.pool_save(reactions.pool_load())            # migrates + rewrites
    assert not config.REACTION_POOL_PATH.exists()
    # Put a legacy bank back and prove it is still picked up afterwards.
    config.REACTION_PROMPTS_PATH.write_text(_yaml.safe_dump(
        {"base": "late bank", "categories": {
            "mad": {"label": "Mad", "prompts": ["x"]}}}), encoding="utf-8")
    reactions.bank_path().unlink(missing_ok=True)
    _reset_reaction_caches()
    assert reactions.bank_load()["base"] == "late bank"
    assert not config.REACTION_PROMPTS_PATH.exists()


# --------------------------------------------------------------------------- #
# The roster the pools hang off
#
# Which bots exist is config.yaml's business alone once it exists. The shipped
# defaults seed a FIRST RUN; merging them into an existing roster injected bots
# nobody chose — and a shipped default flagged `safe` would then appear on
# locked family devices.
# --------------------------------------------------------------------------- #


def test_shipped_defaults_are_not_injected_into_an_existing_roster(rx_env):
    import yaml as _yaml
    config.CONFIG_PATH.write_text(_yaml.safe_dump({"bots": [
        {"id": "solo", "name": "Solo", "order": 0, "safe": False},
    ]}), encoding="utf-8")
    config._invalidate_bots_cache()
    assert [b.id for b in config.load_bots()] == ["solo"]
    # Including the safe one, which is what would have reached locked devices.
    assert not {b.id for b in config.DEFAULT_BOTS} & {b.id
                                                      for b in config.load_bots()}


def test_a_removed_bot_stays_removed(rx_env):
    """Deleting a bot from config.yaml is a decision, and it has to survive a
    reload — the phantom-injection bug made it look like a failed edit."""
    import yaml as _yaml
    assert "alpha" in [b.id for b in config.load_bots()]   # materialises the file
    raw = _yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8"))
    raw["bots"] = [b for b in raw["bots"] if b["id"] != "alpha"]
    config.CONFIG_PATH.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    config._invalidate_bots_cache()
    assert "alpha" not in [b.id for b in config.load_bots()]


def test_a_fresh_install_still_gets_the_shipped_roster(rx_env):
    config.CONFIG_PATH.unlink(missing_ok=True)
    config._invalidate_bots_cache()
    assert ([b.id for b in config.load_bots()]
            == [b.id for b in sorted(config.DEFAULT_BOTS, key=lambda b: b.order)])
    assert config.CONFIG_PATH.exists()          # materialised on the way past


# --------------------------------------------------------------------------- #
# Markers inside code are QUOTED, not fired
# --------------------------------------------------------------------------- #

def test_marker_inside_inline_code_is_not_fired_or_stripped():
    """An agent explaining the syntax must not trigger it.

    Reported from live use: an assistant wrote "the `:react:check_in:` is a
    DisPatch reaction marker, not a file attachment" and the message persisted
    as "the `` is a ..." — an empty code span and a sentence that reads as
    nonsense. Worse, the marker also FIRED: a picture popped on every device in
    the house and a one-shot pool image was spent, because the agent described
    the feature.

    The existing narrowness (both colons required, restricted charset) guards
    against prose like "1:2:1"; it does not guard against quoting the marker.
    """
    from app import reactions

    text = "No files — the `:react:check_in:` is a reaction marker, not an attachment."
    clean, ids = reactions.extract_markers(text)
    assert ids == [], f"a quoted marker fired a reaction: {ids}"
    assert "`:react:check_in:`" in clean, f"quoted marker was stripped: {clean!r}"
    assert "``" not in clean, f"left an empty code span: {clean!r}"


def test_marker_inside_fenced_block_is_not_fired_or_stripped():
    from app import reactions

    text = "Fire one like this:\n```\n:react:facepalm:\n```\nThat is the syntax."
    clean, ids = reactions.extract_markers(text)
    assert ids == [], f"a fenced marker fired a reaction: {ids}"
    assert ":react:facepalm:" in clean, f"fenced marker was stripped: {clean!r}"


def test_a_real_marker_outside_code_still_fires():
    """The guard must not break the feature it is protecting."""
    from app import reactions

    clean, ids = reactions.extract_markers("All done. :react:facepalm:")
    assert ids, "a genuine marker stopped firing"
    assert ":react:" not in clean, f"marker syntax leaked into the chat: {clean!r}"


def test_code_span_and_real_marker_in_one_message():
    """Explain the syntax AND fire one — the common case for a docs reply."""
    from app import reactions

    text = "Use `:react:check_in:` to check in. Watch: :react:facepalm:"
    clean, ids = reactions.extract_markers(text)
    assert ids == ["facepalm"] or (len(ids) == 1 and "facepalm" in ids[0]), ids
    assert "`:react:check_in:`" in clean, clean
    assert clean.count(":react:") == 1, f"the fired marker was not removed: {clean!r}"


def test_unclosed_fence_swallows_to_end_of_message():
    """An unclosed fence extends to the end — CommonMark says so, and this
    keeps the rule "code is quoted" honest rather than guessing where the
    author meant the block to stop. A marker after an unterminated fence is
    therefore inside code and does NOT fire. Pinned deliberately: it looks like
    a false negative until you know it is the markdown rule.
    """
    from app import reactions

    clean, ids = reactions.extract_markers("```\ncode\n:react:facepalm:")
    assert ids == [], "an unterminated fence should still count as code"
    assert ":react:facepalm:" in clean


def test_a_lone_backtick_does_not_swallow_a_real_marker():
    """One stray backtick in prose must not disable the feature for the rest of
    the message — an inline span needs a matching pair."""
    from app import reactions

    clean, ids = reactions.extract_markers("it's a ` char. :react:facepalm:")
    assert ids, "a lone backtick suppressed a genuine marker"


def test_marker_extraction_is_not_quadratic_on_hostile_input():
    """Message bodies are attacker-influenced; _CODE_RE uses DOTALL and a
    backreference, which is the shape ReDoS lives in. Measured, not assumed."""
    import time

    from app import reactions

    for payload in ("`" * 5000, "```\n" + "a" * 100_000, ("`a") * 20_000):
        start = time.perf_counter()
        reactions.extract_markers(payload)
        assert time.perf_counter() - start < 2.0, "marker extraction went quadratic"


def test_a_fired_marker_does_not_reindent_a_code_block():
    """Preserving code in the walk is useless if the cleanup pass mangles it.

    extract_markers copies code regions through byte-for-byte, then collapsed
    runs of 2+ spaces across the WHOLE joined result — code included. A reply
    that both fires a reaction and contains a Python block came out reindented
    to one space, i.e. no longer parsing. It only bites when a marker is
    present, which makes it look like model flakiness rather than a server
    transform, and it happens at the persist chokepoint so the DB keeps the
    mangled copy.
    """
    from app import reactions

    src = "def f(x):\n    if x:\n        return 1\n    return 0"
    clean, ids = reactions.extract_markers(f":react:facepalm:\n\n```python\n{src}\n```\n")
    assert ids, "the marker should still fire"
    assert src in clean, f"code block was reindented: {clean!r}"
    compile(src, "<test>", "exec")  # the input was valid; the output must be too


# --------------------------------------------------------------------------- #
# A named bot must EXIST on a write (reads stay lenient)
# --------------------------------------------------------------------------- #


def test_writes_404_on_an_unknown_bot_instead_of_hitting_the_default(rx_env):
    """`?bot_id=nope` used to resolve to the default bot, so a junk id
    silently OVERWROTE the working bot's prompt bank / pool config."""
    client = rx_env()
    good = reactions.default_bot_id()
    before = reactions.bank_load(good)

    bad_bank = {"categories": {"x": {"prompts": ["a cat"]}}}
    r = client.put("/api/reactions/prompts",
                   json={"bot_id": "no/such", "prompts": bad_bank})
    assert r.status_code == 404
    assert reactions.bank_load(good) == before, "the default bank was overwritten"

    r = client.put("/api/reactions/pool",
                   json={"values": {"bot_id": "no/such", "per_mood": 3}})
    assert r.status_code == 404

    r = client.post("/api/reactions/pool/refill?bot_id=no/such")
    assert r.status_code in (404, 503)      # 503 only when no CLI is present
    if r.status_code == 503:
        # Prove the id check runs even with the CLI available.
        with pytest.raises(reactions.ReactionError) as e:
            reactions.require_bot_id("no/such")
        assert e.value.status == 404


def test_reads_still_degrade_to_the_default_bot(rx_env):
    """A malformed id on a READ is at worst a confusing answer, so it keeps
    the lenient path — only writes were turning it into someone's data loss."""
    client = rx_env()
    r = client.get("/api/reactions/prompts?bot_id=no/such")
    assert r.status_code == 200
    assert r.json()["bot_id"] == reactions.default_bot_id()


def test_an_omitted_bot_id_still_means_the_default_pool(rx_env):
    assert reactions.require_bot_id("") == reactions.default_bot_id()
    assert reactions.require_bot_id(None) == reactions.default_bot_id()


# --------------------------------------------------------------------------- #
# safe_ids() is memoised against directory mtimes
# --------------------------------------------------------------------------- #


def test_safe_ids_does_not_rewalk_unchanged_pools(rx_env, monkeypatch):
    """It sits on the Safe-Mode redactor's request path and walked every mood
    folder plus the ever-growing spent/ store on EVERY call."""
    st = reactions.pool_load()
    st.config.safe = True
    reactions.pool_save(st)
    mood = reactions._moods_dir() / "happy"
    mood.mkdir(parents=True, exist_ok=True)
    (mood / "a.png").write_bytes(b"x")
    reactions.invalidate()

    walks: list[str] = []
    real = reactions._mood_files
    monkeypatch.setattr(reactions, "_mood_files",
                        lambda d: (walks.append(str(d)), real(d))[1])

    first = reactions.safe_ids()
    assert walks, "the first call must actually walk the pool"
    n = len(walks)
    assert reactions.safe_ids() == first
    assert len(walks) == n, "the second call re-walked unchanged directories"

    # A new image changes its folder's mtime, so the cache must reopen.
    (mood / "b.png").write_bytes(b"y")
    grown = reactions.safe_ids()
    assert len(walks) > n
    assert grown > first


def test_full_reaction_pool_clears_a_stale_error(rx_env, monkeypatch):
    """The reaction pool carries the same phantom-error bug the avatar pool did:
    only a successful mint cleared last_error, so a pool sitting at target wore
    a weeks-old `image-cli-unavailable` forever."""
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    reactions.bank_save({"categories": {"solo": {"label": "Solo", "prompts": ["p"]}}})
    st = reactions.pool_load()
    st.config.per_mood = 1
    st.config.min_per_mood = 1
    st.last_error = "image-cli-unavailable"
    reactions.pool_save(st)
    _fake_pool_item(category="solo")                 # at target — nothing to mint

    assert reactions.pool_refill() == 0
    assert reactions.pool_load().last_error == ""


def test_reaction_pool_keeps_a_missing_bank_error_when_full(rx_env, monkeypatch):
    """`no-prompt-categories` is a missing input, not a failed attempt: a full
    pool does not disprove it and must not clear it."""
    monkeypatch.setattr(reactions, "image_cli_available", lambda: True)
    reactions.bank_save({"categories": {"solo": {"label": "Solo", "prompts": ["p"]}}})
    st = reactions.pool_load()
    st.config.per_mood = 1
    st.config.min_per_mood = 1
    st.last_error = "no-prompt-categories"
    reactions.pool_save(st)
    _fake_pool_item(category="solo")                 # at target — nothing to mint

    assert reactions.pool_refill() == 0
    assert reactions.pool_load().last_error == "no-prompt-categories"
