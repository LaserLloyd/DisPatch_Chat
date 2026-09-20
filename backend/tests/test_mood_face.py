"""Mood face — Feature 22 (Group D: mood-driven header face).

The persist chokepoint (_prepare_persist in app/main.py) already computes a
mood for an assistant reply — an explicit `:react:` marker, or the server's
own autopilot pick (see test_reaction_autopilot.py). This feature stamps
`metadata.mood` on the row when that mood is present AND the bot has an
operator-dropped face for it (avatar_pool.has_mood_face), and serves one
deterministically — hashed on the message id — through GET
/api/messages/<id>/mood-face, so the header and that message's own avatar can
swap to it for a few seconds on the client.

The faces themselves live under the bot's avatar pool in a NEW moods/<mood>/
folder: operator-dropped, REUSED, never burned — a different lifecycle from
the ready/spent one-shot thread-avatar pairs the module already had, and this
suite pins that those two never cross wires.

CRITICAL invariant, called out explicitly in the brief: a thread wears the
face it was born under, forever (see app/database.py's threads.avatar_snapshot
and the docstring on GET /api/threads/<id>/avatar). A mood face is a client-
side src swap only — nothing here may ever call db.set_thread_avatar.
"""
from __future__ import annotations

import yaml
from test_reactions import _unlocked, rx_env

from app import avatar_pool, config, main

DAY_HOUR = 14   # conftest's _pin_the_autopilot_clock defaults to night (3);
                # autopilot tests need a daytime hour to fire at all.
PLAIN_REPLY = "Crew dispatched and the verification loop is running clean."


def _enable_autopilot(bot_id: str = "main") -> None:
    """Same recipe as test_reaction_autopilot.py's helper of the same name —
    duplicated rather than imported so this file has no import-time coupling
    to that one beyond the shared rx_env fixture."""
    config.load_bots()
    raw = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    for b in raw.get("bots", []):
        if b.get("id") == bot_id:
            b["reaction_autopilot"] = True
    config.CONFIG_PATH.write_text(yaml.safe_dump(raw))
    config._invalidate_bots_cache()


def _drop_mood_face(bot_id: str, mood: str, tag: str = "a") -> bytes:
    """An operator hand-drop: a reusable face under moods/<mood>/. Returns the
    bytes written, so a test can assert a route served exactly this file —
    the filesystem IS the manifest, same contract as reactions._mood_dirs."""
    d = avatar_pool.moods_root(bot_id) / mood
    d.mkdir(parents=True, exist_ok=True)
    data = f"FACE-{bot_id}-{mood}-{tag}".encode()
    (d / f"{tag}.png").write_bytes(data)
    return data


async def _persist(monkeypatch, thread_id, text, *, hour=DAY_HOUR, metadata=None):
    """One assistant persist, broadcast + real reaction firing muted, the
    real persisted MessageOut returned so its metadata can be inspected."""
    async def _noop(_frame):
        return None

    async def _no_fire(ids, tid, bid, *, autopilot=False):
        return None

    monkeypatch.setattr(main.manager, "broadcast", _noop)
    monkeypatch.setattr(main, "_fire_marker_reactions", _no_fire)
    monkeypatch.setattr(main, "_autopilot_now_hour", lambda: hour)
    return await main._persist_and_broadcast_message(thread_id, "assistant", text,
                                                      metadata=metadata)


# --------------------------------------------------------------------------- #
# avatar_pool: the mood-face pool itself (reused, never burned)
# --------------------------------------------------------------------------- #

def test_no_face_means_no_face(rx_env):
    assert avatar_pool.has_mood_face("main", "thinking") is False
    assert avatar_pool.mood_face_files("main", "thinking") == []
    assert avatar_pool.mood_face_path("main", "thinking", "msg-1") is None


def test_dropped_face_is_found(rx_env):
    _drop_mood_face("main", "thinking")
    assert avatar_pool.has_mood_face("main", "thinking") is True
    assert len(avatar_pool.mood_face_files("main", "thinking")) == 1


def test_bad_mood_name_is_rejected_not_a_path_escape(rx_env):
    # MOOD_DIR_RE (shared with reactions) admits no dot or slash — a mood
    # string built from a message must not become a directory traversal.
    assert avatar_pool.has_mood_face("main", "../../etc") is False
    assert avatar_pool.has_mood_face("main", "a/b") is False
    assert avatar_pool.mood_face_path("main", "", "msg-1") is None


def test_resolution_is_deterministic_per_message(rx_env):
    """Hashed on the MESSAGE id: reload, second device, retried GET all agree."""
    _drop_mood_face("main", "thinking", "a")
    _drop_mood_face("main", "thinking", "b")
    _drop_mood_face("main", "thinking", "c")
    first = avatar_pool.mood_face_path("main", "thinking", "msg-fixed-id")
    for _ in range(5):
        assert avatar_pool.mood_face_path("main", "thinking", "msg-fixed-id") == first

    # And the hash actually spreads across the stock — not just "always the
    # first file" — across enough distinct ids that a real spread is
    # overwhelmingly likely if the hash is doing its job.
    files = avatar_pool.mood_face_files("main", "thinking")
    picks = {avatar_pool.mood_face_path("main", "thinking", f"msg-{i}") for i in range(40)}
    assert picks <= set(files)
    assert len(picks) > 1, "40 distinct message ids all resolved to the same face"


def test_resolving_a_face_never_burns_it(rx_env):
    """REUSED, not one-shot — the ready/spent lock must not apply here."""
    _drop_mood_face("main", "thinking", "only")
    before = set(avatar_pool.mood_face_files("main", "thinking"))
    for i in range(10):
        got = avatar_pool.mood_face_path("main", "thinking", f"msg-{i}")
        assert got is not None and got.is_file()
    after = set(avatar_pool.mood_face_files("main", "thinking"))
    assert before == after, "a mood face moved/disappeared after being resolved"
    # And the one-shot pool's own ready/spent dirs are a SEPARATE namespace —
    # untouched, not just empty by coincidence.
    assert not avatar_pool.ready_dir("main").exists()
    assert not avatar_pool.spent_dir("main").exists()


# --------------------------------------------------------------------------- #
# The persist chokepoint: metadata.mood, gated on a real face existing.
#
# THE PROOF TEST: metadata.mood does not exist anywhere in this codebase
# before this feature, so both halves below fail against the pre-feature
# code — the first because there is nothing to be None OTHER than absent (a
# vacuous pass), the second (the one that actually proves the behaviour)
# because _prepare_persist never looks at avatar_pool at all, so no reply
# could ever carry metadata.mood no matter what a bot's pool holds.
# --------------------------------------------------------------------------- #

async def test_mood_is_not_stamped_without_a_matching_face(rx_env, monkeypatch):
    _enable_autopilot("main")
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-mood-noface")
    msg = await _persist(monkeypatch, "t-mood-noface", PLAIN_REPLY)
    # Autopilot picked "thinking" for this text (see test_reaction_autopilot's
    # identical case) but no face exists for it yet — must NOT be stamped, or
    # the /mood-face route would 404 forever for this row.
    assert (msg.metadata or {}).get("mood") is None


async def test_mood_is_stamped_once_a_face_exists(rx_env, monkeypatch):
    _enable_autopilot("main")
    _drop_mood_face("main", "thinking")
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-mood-face")
    msg = await _persist(monkeypatch, "t-mood-face", PLAIN_REPLY)
    assert msg.metadata is not None
    assert msg.metadata["mood"] == "thinking"


async def test_mood_stamp_leaves_other_metadata_alone(rx_env, monkeypatch):
    """metadata.mood is MERGED in, not a metadata replacement."""
    _enable_autopilot("main")
    _drop_mood_face("main", "thinking")
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-mood-merge")
    msg = await _persist(monkeypatch, "t-mood-merge", PLAIN_REPLY,
                         metadata={"model": "some/model"})
    assert msg.metadata["mood"] == "thinking"
    assert msg.metadata["model"] == "some/model"


async def test_no_mood_stamped_when_autopilot_is_off(rx_env, monkeypatch):
    # bot.reaction_autopilot is False by default (TEST_BOTS) — no marker, no
    # autopilot pick, nothing to stamp, regardless of what faces exist.
    _drop_mood_face("main", "thinking")
    await main.db.connect()
    await main.db.create_thread(bot_id="main", thread_id="t-mood-off")
    msg = await _persist(monkeypatch, "t-mood-off", PLAIN_REPLY)
    assert (msg.metadata or {}).get("mood") is None


# --------------------------------------------------------------------------- #
# CRITICAL: never writes threads.avatar_snapshot.
#
# A thread wears the face it was born under, permanently and deliberately
# (see GET /api/threads/<id>/avatar's own docstring). Feature 22 is a
# client-side src swap only. This spies on the ONE function that could
# accidentally repaint a thread's own face and asserts it is never called
# across the whole flow: persisting a mooded reply AND fetching the resolved
# picture back through the route.
# --------------------------------------------------------------------------- #

async def test_mood_face_never_touches_thread_avatar_snapshot(rx_env, monkeypatch):
    # The client first — TestClient's __enter__ runs the app lifespan, which
    # is what connects main.db here. A second, separate connect() later (from
    # a manual await main.db.connect()) is not what any other suite does and
    # is not needed: creating the client is enough.
    client = _unlocked(rx_env)

    calls: list[tuple] = []
    real_set_thread_avatar = main.db.set_thread_avatar

    async def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await real_set_thread_avatar(*args, **kwargs)

    monkeypatch.setattr(main.db, "set_thread_avatar", _spy)

    _enable_autopilot("main")
    _drop_mood_face("main", "thinking")
    await main.db.create_thread(bot_id="main", thread_id="t-mood-noavatar")
    msg = await _persist(monkeypatch, "t-mood-noavatar", PLAIN_REPLY)
    assert msg.metadata["mood"] == "thinking"

    r = client.get(f"/api/messages/{msg.id}/mood-face")
    assert r.status_code == 200

    assert calls == [], (
        "db.set_thread_avatar was called by the mood-face path — a thread's "
        "own avatar must never change because of a reply's mood")


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

async def test_mood_face_route_serves_the_resolved_file(rx_env):
    client = _unlocked(rx_env)
    await main.db.create_thread(bot_id="main", thread_id="t-mood-route")
    face_bytes = _drop_mood_face("main", "thinking", "x")
    msg = await main.db.add_message("t-mood-route", "assistant", "hi there",
                                    metadata={"mood": "thinking"})
    r = client.get(f"/api/messages/{msg.id}/mood-face")
    assert r.status_code == 200
    assert r.content == face_bytes
    # Same message, fetched twice — the SAME bytes (a deterministic hash, not
    # a fresh random draw on every request).
    r2 = client.get(f"/api/messages/{msg.id}/mood-face")
    assert r2.content == face_bytes


async def test_mood_face_route_404s_cleanly(rx_env):
    client = _unlocked(rx_env)
    await main.db.create_thread(bot_id="main", thread_id="t-mood-404")
    # No such message at all.
    assert client.get("/api/messages/does-not-exist/mood-face").status_code == 404
    # A message with no mood.
    plain = await main.db.add_message("t-mood-404", "assistant", "plain reply")
    assert client.get(f"/api/messages/{plain.id}/mood-face").status_code == 404
    # A mood claimed on the row, but no face was ever dropped for it (or was
    # removed since) — never a 500, never a crash, just "nothing to show".
    orphan = await main.db.add_message("t-mood-404", "assistant", "another",
                                       metadata={"mood": "nonexistent-mood"})
    assert client.get(f"/api/messages/{orphan.id}/mood-face").status_code == 404


async def test_mood_face_route_denies_safe_mode_for_an_unsafe_bot(rx_env):
    # "main" (conftest's TEST_BOTS) is NOT flagged safe.
    unlocked = _unlocked(rx_env)
    await main.db.create_thread(bot_id="main", thread_id="t-mood-safe")
    _drop_mood_face("main", "thinking")
    msg = await main.db.add_message("t-mood-safe", "assistant", "hi",
                                    metadata={"mood": "thinking"})
    assert unlocked.get(f"/api/messages/{msg.id}/mood-face").status_code == 200

    decoy = rx_env()   # PIN is already set (via _unlocked above) — this
                       # client never unlocked, so it is served Safe Mode.
    assert decoy.get(f"/api/messages/{msg.id}/mood-face").status_code == 403
