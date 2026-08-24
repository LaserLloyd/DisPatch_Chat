"""The interactive ```checklist table: PATCH /api/messages/{id}/checklist.

Persisting checkbox state on the message itself is the load-bearing half of the
feature — it is what makes a checked row survive a reload and reach another
device. These tests pin the endpoint's contract:

  * a full session may write checked rows (and only a full session),
  * the payload is validated (list of non-negative ints, deduped, bounded),
  * the state is stored on the MESSAGE's metadata (merged, not clobbered),
  * other clients are told via a checklist_update broadcast.

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs, nothing touches the live data dir or ~/.openclaw.
Run: cd backend && uv run pytest tests/test_checklist.py
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

CHECKLIST = (
    "```checklist\n"
    "| Exercise | Sets | Reps | Rest |\n"
    "|---|---|---|---|\n"
    "| Squat | 3 | 10 | 60s |\n"
    "| Push-up | 3 | 15 | 30s |\n"
    "| Deadlift | 5 | 5 | 3min |\n"
    "| Plank | 1 | 60s | — |\n"
    "```"
)
NOT_CHECKLIST = "just some prose, no table here"


@pytest.fixture
def checklist_env(tmp_path, monkeypatch):
    """Isolated data dir + DB + PIN (so unauthenticated == Safe Mode)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._delivered.clear()
    main._thread_bot.clear()
    main._decoy_upload_used.clear()

    with TestClient(main.app, client=("testclient", 50000)) as client:
        auth.set_pin("1234")
        auth._bust_cache()
        yield client
    asyncio.run(temp_db.close())


def _unlock(client):
    r = client.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    return r


def _checklist_message(client, content=CHECKLIST, metadata=None):
    """Unlock, create a thread + an assistant checklist message, return its id."""
    _unlock(client)
    thread = client.post("/api/threads", json={"bot_id": "alpha"}).json()
    body = {"role": "assistant", "content": content}
    if metadata is not None:
        body["metadata"] = metadata
    msg = client.post(f"/api/threads/{thread['id']}/messages", json=body).json()
    return thread["id"], msg["id"]


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #

def test_redactor_scopes_checklist_update_to_safe_bots(checklist_env):
    """Safe Mode must not learn a non-safe bot's checklist changed."""
    base = {"type": "checklist_update", "thread_id": "t", "message_id": "m",
            "checklist": {"checked": [0]}}
    assert main.redact_for_decoy({**base, "bot_id": "alpha"}) is not None  # safe
    assert main.redact_for_decoy({**base, "bot_id": "main"}) is None       # unsafe


def test_checklist_patch_requires_a_full_session(checklist_env):
    tid, mid = _checklist_message(checklist_env)
    checklist_env.post("/api/auth/lock")
    r = checklist_env.patch(f"/api/messages/{mid}/checklist", json={"checked": [0]})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Unlock for full access"


def test_missing_message_is_404(checklist_env):
    _unlock(checklist_env)
    r = checklist_env.patch("/api/messages/does-not-exist/checklist",
                            json={"checked": []})
    assert r.status_code == 404, r.text


def test_non_checklist_message_is_refused(checklist_env):
    tid, mid = _checklist_message(checklist_env, content=NOT_CHECKLIST)
    r = checklist_env.patch(f"/api/messages/{mid}/checklist", json={"checked": [0]})
    assert r.status_code == 400, r.text
    assert "not a checklist" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Payload validation
# --------------------------------------------------------------------------- #

def test_checked_must_be_a_list(checklist_env):
    tid, mid = _checklist_message(checklist_env)
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"checked": "0,1"})
    assert r.status_code == 400, r.text


def test_checked_entries_must_be_integers(checklist_env):
    tid, mid = _checklist_message(checklist_env)
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"checked": [0, "1", True]})
    assert r.status_code == 400, r.text


def test_checked_indices_are_deduped_and_clamped(checklist_env):
    tid, mid = _checklist_message(checklist_env)
    r = checklist_env.patch(
        f"/api/messages/{mid}/checklist",
        json={"checked": [3, 0, 3, 1, -1, 50000, 2]})
    assert r.status_code == 200, r.text
    assert r.json()["checklist"]["checked"] == [3, 0, 1, 2]


# --------------------------------------------------------------------------- #
# Persistence + broadcast
# --------------------------------------------------------------------------- #

def test_checklist_state_is_stored_on_the_message_and_broadcast(checklist_env, monkeypatch):
    tid, mid = _checklist_message(checklist_env)

    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(main.manager, "broadcast", capture)

    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"checked": [2, 0]})
    assert r.status_code == 200, r.text
    assert r.json()["checklist"] == {"checked": [2, 0]}

    # Stored on the message, readable back through the normal messages route.
    msgs = checklist_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    msg = [m for m in msgs if m["id"] == mid][0]
    assert msg["metadata"]["checklist"] == {"checked": [2, 0]}

    # Other devices are told, with the check order intact.
    updates = [f for f in frames if f.get("type") == "checklist_update"]
    assert len(updates) == 1
    assert updates[0]["message_id"] == mid
    assert updates[0]["thread_id"] == tid
    assert updates[0]["checklist"] == {"checked": [2, 0]}


def test_checklist_state_merges_with_existing_metadata(checklist_env):
    """The patch must not clobber unrelated metadata (sub, model, …)."""
    tid, mid = _checklist_message(checklist_env,
                                  metadata={"sub": True, "model": "x"})

    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"checked": [1]})
    assert r.status_code == 200, r.text

    msgs = checklist_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    meta = [m for m in msgs if m["id"] == mid][0]["metadata"]
    assert meta["sub"] is True
    assert meta["model"] == "x"
    assert meta["checklist"] == {"checked": [1]}


# --- row operations -------------------------------------------------------
#
# The widget sends ONE ROW, not the whole array. Sending the array meant two
# devices ticking different rows raced: the second write was computed from a
# snapshot taken before the first landed, and silently discarded it. On a
# shared family list that is data loss with no error shown to anyone.


def test_row_op_checks_and_unchecks_one_row(checklist_env):
    tid, mid = _checklist_message(checklist_env)

    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": 1, "checked": True})
    assert r.status_code == 200, r.text
    assert r.json()["checklist"]["checked"] == [1]

    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": 1, "checked": False})
    assert r.status_code == 200, r.text
    assert r.json()["checklist"]["checked"] == []


def test_two_devices_checking_different_rows_keep_both(checklist_env):
    """The bug this pins: whole-array writes lost the earlier check.

    Both callers start from the same empty view, exactly as two devices
    rendering the same message do.
    """
    tid, mid = _checklist_message(checklist_env)

    checklist_env.patch(f"/api/messages/{mid}/checklist",
                        json={"index": 0, "checked": True})
    checklist_env.patch(f"/api/messages/{mid}/checklist",
                        json={"index": 2, "checked": True})

    msgs = checklist_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    stored = [m for m in msgs if m["id"] == mid][0]["metadata"]["checklist"]
    assert stored["checked"] == [0, 2], "neither device's check may be dropped"


def test_row_op_keeps_check_order(checklist_env):
    """Completed rows pin to the bottom in the order they were done, so a
    re-check moves the row to the END, not back to its authored slot."""
    tid, mid = _checklist_message(checklist_env)
    for i in (2, 0, 1):
        checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": i, "checked": True})
    checklist_env.patch(f"/api/messages/{mid}/checklist",
                        json={"index": 2, "checked": False})
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": 2, "checked": True})
    assert r.json()["checklist"]["checked"] == [0, 1, 2]


def test_row_op_rejects_a_non_boolean(checklist_env):
    tid, mid = _checklist_message(checklist_env)
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": 0, "checked": [0]})
    assert r.status_code == 400


def test_two_checklists_in_one_message_do_not_clobber_each_other(checklist_env):
    """A warm-up list and a main-set list share a message but not an index
    space. Every widget used to be handed the same array, so checking row 0 of
    the second wiped row 0 of the first."""
    tid, mid = _checklist_message(checklist_env)

    checklist_env.patch(f"/api/messages/{mid}/checklist",
                        json={"list": 0, "index": 0, "checked": True})
    checklist_env.patch(f"/api/messages/{mid}/checklist",
                        json={"list": 1, "index": 0, "checked": True})

    msgs = checklist_env.get(f"/api/threads/{tid}/messages").json()["messages"]
    stored = [m for m in msgs if m["id"] == mid][0]["metadata"]["checklist"]
    assert stored["checked"] == [0], "list 0 must survive a write to list 1"
    assert stored["lists"]["1"] == [0]


def test_oversized_payload_is_refused_before_it_is_walked(checklist_env):
    """Validation alone did not bound this: every entry after the first
    duplicate is a dedup-skip, so the 1000-entry OUTPUT cap never trips and a
    multi-million-element body was parsed and iterated in full."""
    tid, mid = _checklist_message(checklist_env)
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"checked": [0] * 5001})
    assert r.status_code == 400


def test_row_op_requires_a_full_session(checklist_env):
    """Safe Mode is VIEW + SEND. The row-op path must inherit the same gate as
    the array path -- a new payload shape must not become a way around it."""
    tid, mid = _checklist_message(checklist_env)
    checklist_env.post("/api/auth/lock")
    r = checklist_env.patch(f"/api/messages/{mid}/checklist",
                            json={"index": 0, "checked": True})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Unlock for full access"
