"""Reset to default (4.0 chat-first profiles, BUILD-PLAN-v2 §1.3), service level.

The acceptance test is ``test_a_reset_leaves_no_trace_of_the_profile_in_the_data_directory``:
every surface that holds a profile's content carries a marker, the profile is reset, and the
WHOLE data directory (databases and their write-ahead logs included) is searched for those
markers. The other profile's markers must all still be there.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from profiles_fixtures import scratch

from app import accounts, config, ext_profile_hooks, profiles_service
from app.contracts.permissions import Role
from app.profiles_service import ProfileServiceError

PNG = b"\x89PNG\r\n\x1a\n"


def _populate(s, who: str, profile) -> dict:
    """Content for ``profile`` on every surface, each carrying ``SENT-<who>-<surface>``."""
    m = lambda surface: f"SENT-{who}-{surface}"        # noqa: E731
    pid = profile.id
    shared = s.media("shared.png", PNG + b"SHARED-PICTURE")
    own = s.media(f"own-{who}.png", PNG + m("picture-bytes").encode())
    studio = s.media(f"studio-{who}.png", PNG + m("studio-bytes").encode())
    snap = s.snapshot(f"{who.lower()}0123456789abcdef.png", PNG + m("snapshot-bytes").encode())
    shared_snap = s.snapshot("fedcba9876543210.png", PNG + b"SHARED-SNAPSHOT")
    bot = profiles_service.create_personal(
        pid, {"name": m("bot"), "description": m("bot-description"), "persona": m("persona")})
    tid = s.thread(pid, "pack_otter", m("title"), snapshot=snap.name, messages=[
        ("user", m("message") + f" qzxvortex{who.lower()}"),         # one lowercase token: the search index
        ("assistant", f"look /media/{own.name}", f"/media/{own.name}", {"note": m("metadata")}),
        ("assistant", "the common one", "/media/shared.png"),
    ])
    bot_tid = s.thread(pid, bot.id, m("bot-chat"), snapshot=shared_snap.name,
                       messages=[("user", m("bot-chat-message"))])
    msg_id = s.message_ids(tid)[0]
    s.image_job(tid, msg_id, json.dumps({"prompt": m("image-spec")}))
    chat_file, chat_blob = s.file_row(pid, f"{m('file')}.txt", m("blob").encode(), source="chat")
    drop_file, drop_blob = s.file_row(pid, f"KEEP-{who}-drop.txt", f"KEEP-{who}-drop-bytes".encode(),
                                      source="fileserver")
    s.advisor_request(tid, m("advisor-brief"))
    s.agent_task(pid, m("task"), origin_thread=tid)
    s.voice_cursor(tid, m("cursor"))
    return {"shared": shared, "own": own, "studio": studio, "snap": snap, "shared_snap": shared_snap,
            "bot": bot, "tid": tid, "bot_tid": bot_tid, "chat_blob": chat_blob, "drop_blob": drop_blob,
            "markers": [m(x) for x in ("title", "message", "metadata", "bot", "bot-description", "persona",
                                       "bot-chat", "bot-chat-message", "image-spec", "file", "blob",
                                       "advisor-brief", "task", "cursor", "picture-bytes",
                                       "studio-bytes", "snapshot-bytes", "ledger-prompt", "recent-prompt",
                                       "ack")] + [f"qzxvortex{who.lower()}"],
            "keep": [f"KEEP-{who}-drop", f"KEEP-{who}-drop-bytes"]}


def _ledger_and_acks(s, a, b, a_bot, b_bot):
    s.ledger([
        {"id": "a" * 32, "url": "/media/studio-A.png", "prompt": "SENT-A-ledger-prompt", "owner_profile_id": a.id},
        {"id": "b" * 32, "url": "/media/studio-B.png", "prompt": "SENT-B-ledger-prompt", "owner_profile_id": b.id},
    ])
    (s.root / "apps-data" / "imagestudio" / f"prompts-{a.id}.json").write_text(json.dumps(["SENT-A-recent-prompt"]))
    (s.root / "apps-data" / "imagestudio" / f"prompts-{b.id}.json").write_text(json.dumps(["SENT-B-recent-prompt"]))
    s.voice_acks({"bots": {a_bot.id: {"statement": ["SENT-A-ack"]}, b_bot.id: {"statement": ["SENT-B-ack"]}}})


def _reset(profile_id):
    preview = profiles_service.reset_preview(profile_id)
    return profiles_service.reset_profile(profile_id, preview["token"])


def test_a_reset_leaves_no_trace_of_the_profile_in_the_data_directory(scratch):
    s = scratch
    s.connection()
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    ca, cb = _populate(s, "A", a), _populate(s, "B", b)
    _ledger_and_acks(s, a, b, ca["bot"], cb["bot"])
    before = s.grep(*ca["markers"], *cb["markers"], *ca["keep"], *cb["keep"])
    for marker in ca["markers"] + cb["markers"]:
        assert before[marker], f"the fixture put nothing at {marker}"        # the check can see them

    deleted = _reset(a.id)

    assert deleted["chats"] == 2 and deleted["bots"] == 1 and deleted["pictures"] == 2          # own + studio; the shared one stays
    after = s.grep(*ca["markers"], *cb["markers"], *ca["keep"], *cb["keep"])
    assert {m: files for m, files in after.items() if m in ca["markers"] and files} == {}, \
        "traces of the reset profile remain"
    assert all(after[m] for m in cb["markers"]), "the other profile lost something"
    # File Server drops are the profile's files, not its conversations: kept.
    assert all(after[m] for m in ca["keep"] + cb["keep"])
    # A picture another message still shows stays; so does a snapshot another thread still uses.
    assert ca["shared"].exists() and ca["shared_snap"].exists()
    assert not ca["own"].exists() and not ca["snap"].exists() and not ca["chat_blob"].exists()
    assert cb["own"].exists() and cb["snap"].exists() and cb["chat_blob"].exists()
    assert ca["drop_blob"].exists()


def test_reset_gives_an_admin_or_user_a_fresh_atlas_and_keeps_the_profile(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram", pin_hash=None)
    old = profiles_service.create_personal(u.id, {"name": "Mine"})
    s.thread(u.id, old.id)
    _reset(u.id)
    mine = s.personal(u.id)
    assert len(mine) == 1 and mine[0].id != old.id and mine[0].name == "Atlas"
    assert accounts.get_profile(u.id).name == "Bram"
    assert profiles_service.content_counts(u.id) == {"bots": 1, "chats": 0, "pictures": 0, "voices": 0}


def test_reset_leaves_a_basic_profile_with_its_assigned_household_list(scratch):
    s = scratch
    s.admin()
    kid = s.profile("Dee", Role.BASIC, household_bots=["pack_otter"])
    s.thread(kid.id, "pack_otter")
    deleted = _reset(kid.id)
    assert deleted["chats"] == 1 and deleted["bots"] == 0
    assert s.personal(kid.id) == []                        # no Atlas for a Basic profile
    assert accounts.get_profile(kid.id).household_bots == ["pack_otter"]


def test_reset_keeps_the_profiles_cloned_voices(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    purged = []
    ext_profile_hooks.register_profile_data("voice", count=lambda pid: {"voices": 2},
                                            purge=lambda pid: purged.append(pid) or 2)
    assert profiles_service.reset_preview(u.id)["counts"]["voices"] == 2
    assert profiles_service.reset_preview(u.id)["keeps_voices"] is True
    _reset(u.id)
    assert purged == []                                    # Reset never calls the purge hook


def test_the_protected_admin_may_reset_itself_and_loses_no_account(scratch):
    s = scratch
    admin = s.admin()
    s.thread(admin.id)
    s.thread(None)                                         # a machine-posted chat reads as the Admin's
    deleted = _reset(admin.id)
    assert deleted["chats"] == 2
    assert accounts.get_profile(admin.id).protected


# ---- the token ------------------------------------------------------------ #

def test_a_token_works_once(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    token = profiles_service.reset_preview(u.id)["token"]
    profiles_service.reset_profile(u.id, token)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_profile(u.id, token)
    assert e.value.status == 409 and e.value.code == "reset_stale"


def test_a_token_goes_stale_when_what_would_be_deleted_changes(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    token = profiles_service.reset_preview(u.id)["token"]
    s.thread(u.id, title="a new chat")                     # one more chat since the confirmation
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_profile(u.id, token)
    assert e.value.status == 409 and e.value.code == "reset_stale"
    assert profiles_service.content_counts(u.id)["chats"] == 2          # nothing was deleted


def test_a_token_expires(scratch, monkeypatch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    token = profiles_service.reset_preview(u.id)["token"]
    now = time.time()
    monkeypatch.setattr(profiles_service.time, "time", lambda: now + profiles_service.TOKEN_TTL_SECONDS + 5)
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_profile(u.id, token)
    assert e.value.code == "reset_stale"


def test_a_token_is_bound_to_the_profile_and_the_action(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    token = profiles_service.reset_preview(a.id)["token"]
    for bad in (token, "", "x", "x.y", token[:-3] + "AAA"):
        with pytest.raises(ProfileServiceError) as e:
            profiles_service.reset_profile(b.id, bad)
        assert e.value.code == "reset_stale"
    # a delete token cannot reset, a reset token cannot delete
    delete_token = profiles_service.delete_preview(a.id)["token"]
    with pytest.raises(ProfileServiceError):
        profiles_service.reset_profile(a.id, delete_token)
    with pytest.raises(ProfileServiceError):
        profiles_service.delete_profile(a.id, token)


def test_an_unknown_profile_is_a_404(scratch):
    scratch.admin()
    with pytest.raises(ProfileServiceError) as e:
        profiles_service.reset_preview("nobody")
    assert e.value.status == 404


def test_the_preview_says_what_the_profile_is_left_with(scratch):
    s = scratch
    s.admin()
    assert profiles_service.reset_preview(s.profile("Bram").id)["outcome"] == "atlas"
    assert profiles_service.reset_preview(s.profile("Dee", Role.BASIC).id)["outcome"] == "household"


def test_wiping_twice_is_harmless(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id)
    first = profiles_service.wipe_profile_content(u.id)
    second = profiles_service.wipe_profile_content(u.id)
    assert first["chats"] == 1 and second == {"bots": 0, "chats": 0, "pictures": 0, "voices": 0}


def test_the_database_stays_healthy_and_the_other_chats_searchable(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    s.thread(a.id, messages=[("user", "zebrafinch gone")])
    s.thread(b.id, messages=[("user", "zebrafinch stays")])
    profiles_service.wipe_profile_content(a.id)
    con = sqlite3.connect(s.root / "chats.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    hits = [r[0] for r in con.execute(
        "SELECT m.content FROM messages_fts f JOIN messages m ON m.rowid = f.rowid WHERE messages_fts MATCH 'zebrafinch'")]
    assert hits == ["zebrafinch stays"]
    con.close()


def test_the_pictures_a_chat_shows_go_unless_another_message_shows_them(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    only_a = s.media("only-a.png", PNG)
    both = s.media("both.png", PNG)
    s.thread(a.id, messages=[("assistant", "x /media/only-a.png", "/media/only-a.png"),
                             ("assistant", "y", "/media/both.png")])
    s.thread(b.id, messages=[("assistant", "z see /media/both.png here")])
    profiles_service.wipe_profile_content(a.id)
    assert not only_a.exists() and both.exists()


def test_a_ledger_picture_that_is_shown_in_a_chat_of_a_kept_profile_stays(scratch):
    s = scratch
    s.admin()
    a, b = s.profile("Bram"), s.profile("Cy")
    shown = s.media("sent-to-chat.png", PNG)
    free = s.media("studio-only.png", PNG)
    s.ledger([{"id": "1" * 32, "url": "/media/sent-to-chat.png", "owner_profile_id": a.id},
              {"id": "2" * 32, "url": "/media/studio-only.png", "owner_profile_id": a.id}])
    s.thread(b.id, messages=[("assistant", "see /media/sent-to-chat.png")])
    profiles_service.wipe_profile_content(a.id)
    assert shown.exists() and not free.exists()
    assert (s.root / "apps-data" / "imagestudio" / "images.jsonl").read_text() == ""


def test_a_ledger_row_without_an_owner_is_the_protected_admins(scratch):
    s = scratch
    admin = s.admin()
    u = s.profile("Bram")
    old = s.media("old-row.png", PNG)
    s.ledger([{"id": "1" * 32, "url": "/media/old-row.png"}])
    profiles_service.wipe_profile_content(u.id)
    assert old.exists()                                     # not Bram's
    profiles_service.wipe_profile_content(admin.id)
    assert not old.exists()


def test_unparseable_ledger_lines_survive_untouched(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    p = s.ledger([{"id": "1" * 32, "url": "/media/x.png", "owner_profile_id": u.id}])
    p.write_text(p.read_text() + "not json at all\n")
    profiles_service.wipe_profile_content(u.id)
    assert p.read_text() == "not json at all\n"


def test_voice_acks_entries_of_a_deleted_bot_go_and_others_stay(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    s.voice_acks({"default": {"statement": ["Right."]}, "bots": {bot.id: {"statement": ["Gone."]},
                                                                 "pack_otter": {"statement": ["Stays."]}}})
    profiles_service.wipe_profile_content(u.id)
    import yaml
    doc = yaml.safe_load((s.root / "voice-acks.yaml").read_text())
    assert doc["bots"] == {"pack_otter": {"statement": ["Stays."]}} and doc["default"]


def test_the_avatar_files_of_a_deleted_bot_go(scratch):
    s = scratch
    s.connection()
    s.admin()
    u = s.profile("Bram")
    bot = profiles_service.create_personal(u.id, {"name": "Mine"})
    av = config.AVATAR_DIR
    av.mkdir(parents=True, exist_ok=True)
    for role in ("face", "full", "thumb"):
        (av / f"{bot.id}-{role}.png").write_bytes(PNG)
    (av / "other-face.png").write_bytes(PNG)
    profiles_service.wipe_profile_content(u.id)
    assert sorted(p.name for p in av.iterdir()) == ["other-face.png"]


def test_open_image_jobs_are_reported_before_the_wipe(scratch):
    s = scratch
    s.admin()
    u = s.profile("Bram")
    tid = s.thread(u.id)
    s.image_job(tid, s.message_ids(tid)[0], "{}", state="running")
    jobs = profiles_service.open_image_jobs(u.id)
    assert [j["state"] for j in jobs] == ["running"]
    profiles_service.wipe_profile_content(u.id)
    assert profiles_service.open_image_jobs(u.id) == []


def test_the_write_ahead_log_is_emptied_while_the_app_keeps_the_database_open(scratch):
    """The running app holds chats.db open, so the -wal file is not deleted on close: the
    wipe must truncate it, or the deleted text would sit in the log."""
    s = scratch
    s.admin()
    u = s.profile("Bram")
    s.thread(u.id, title="SENT-W-title", messages=[("user", "SENT-W-message qzxvortexw")])
    app_conn = sqlite3.connect(s.root / "chats.db")           # the app's own connection stays open
    app_conn.execute("PRAGMA journal_mode=WAL")
    try:
        profiles_service.wipe_profile_content(u.id)
        found = s.grep("SENT-W-title", "SENT-W-message", "qzxvortexw")
        assert found == {"SENT-W-title": [], "SENT-W-message": [], "qzxvortexw": []}
        wal = s.root / "chats.db-wal"
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        app_conn.close()
