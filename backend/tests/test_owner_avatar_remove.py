"""Removing one of YOUR OWN bots' pictures (DELETE /api/me/characters/{bot_id}/avatar).

The owner-scoped character path had an upload and no removal, so a non-Admin owner could
replace a bot's picture but never take it away. The removal mirrors the Admin route
(DELETE /api/bots/{bot_id}/avatar): the face crop, the full-res original and the thumb go
together, the roster drops the filename (letter/emoji tile comes back) and the old face is
snapshotted FIRST so /avatar/history still lists it.

An avatar is a PAIR: losing only one half left the extension probe serving the previous
full-res, which is exactly the bug the shared `_clean_stale_avatar_siblings` helper exists
for — this route reuses it rather than unlinking paths by hand.

Integration-level: the real routes on the isolated `world` app.
"""
from __future__ import annotations

import hashlib
import io

import pytest

pytest.importorskip("PIL")

from app import config  # noqa: E402


def _png(size=(400, 300), color=(30, 90, 200)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _owned_files(bot_id: str) -> list[str]:
    if not config.AVATAR_DIR.is_dir():
        return []
    return sorted(p.name for p in config.AVATAR_DIR.glob(f"{bot_id}-*"))


def _md5(raw: bytes) -> str:
    return hashlib.md5(raw).hexdigest()


def _two_people(world):
    """An Admin (the household Owner) and a plain User, each signed in."""
    owner = world.create_owner()
    uma = world.person(owner, "Uma", "user", pin="2222")
    mine = world.client()
    assert world.login(mine, name="Uma", pin="2222").status_code == 200
    return owner, mine, uma


def test_an_owner_removes_their_own_bot_picture(world):
    owner, mine, _uma = _two_people(world)
    bot = mine.post("/api/me/characters", json={"name": "Scratch"}).json()
    bid = bot["id"]

    up = mine.post(f"/api/me/characters/{bid}/avatar",
                   files={"file": ("a.png", _png(), "image/png")},
                   params={"crop_x": 0.25, "crop_y": 0.25, "crop_size": 0.5})
    assert up.status_code == 200, up.text
    face_name = config.get_bot(bid).avatar
    assert face_name == f"{bid}-face.png"
    face_before = (config.AVATAR_DIR / face_name).read_bytes()
    assert len(_owned_files(bid)) == 3, _owned_files(bid)     # face + full + thumb

    rm = mine.delete(f"/api/me/characters/{bid}/avatar")
    assert rm.status_code == 200, rm.text
    body = rm.json()
    assert body["removed"] is True and body["avatar_url"] == ""
    assert sorted(body["files"]) == [f"{bid}-{r}.png" for r in ("face", "full", "thumb")]

    # A real removal: the whole pair is gone from disk and config.yaml has no filename.
    assert _owned_files(bid) == []
    assert config.get_bot(bid).avatar == ""
    assert mine.get(f"/api/bots/{bid}/avatar").status_code == 404
    assert mine.get(f"/api/bots/{bid}/avatar/full").status_code == 404

    # ...and the old face is still listed and served from history.
    hist = mine.get(f"/api/bots/{bid}/avatar/history")
    assert hist.status_code == 200, hist.text
    ids = [e["id"] for e in hist.json()["avatars"]]
    assert body["snapshot_id"] and body["snapshot_id"] in ids, ids
    old = mine.get(f"/api/bots/{bid}/avatar/history/{body['snapshot_id']}")
    assert old.status_code == 200 and old.content
    assert _md5(old.content) == _md5(face_before), "history no longer serves the removed face"


def test_the_owners_own_bot_can_be_restored_byte_for_byte(world):
    """``POST /api/bots/{bot_id}/avatar/restore`` is Admin-gated (manage_characters,
    route_policy.py:173), so the profile that can press Restore is the Admin — on its OWN
    personal bot, which the same owner-scoped DELETE removed a moment earlier."""
    owner = world.create_owner()
    bid = owner.post("/api/me/characters", json={"name": "Mine"}).json()["id"]
    up = owner.post(f"/api/me/characters/{bid}/avatar",
                    files={"file": ("a.png", _png(), "image/png")})
    assert up.status_code == 200, up.text
    face_before = (config.AVATAR_DIR / config.get_bot(bid).avatar).read_bytes()

    rm = owner.delete(f"/api/me/characters/{bid}/avatar")
    assert rm.status_code == 200, rm.text
    sid = rm.json()["snapshot_id"]
    assert sid and owner.get(f"/api/bots/{bid}/avatar").status_code == 404

    r = owner.post(f"/api/bots/{bid}/avatar/restore", json={"snapshot_id": sid})
    assert r.status_code == 200, r.text
    back = owner.get(f"/api/bots/{bid}/avatar")
    assert back.status_code == 200
    assert _md5(back.content) == _md5(face_before), "restore returned a different picture"


def test_another_users_bot_is_not_found(world):
    """``characters.get_personal`` answers an unknown bot and someone else's bot the same way:
    404 (app/characters.py:743-747) — and this route reports it through the same ``fail`` the
    other owner-scoped routes use, so the body is the generic 404 problem."""
    owner, mine, _uma = _two_people(world)
    theirs = owner.post("/api/me/characters", json={"name": "Theirs"}).json()
    r = mine.delete(f"/api/me/characters/{theirs['id']}/avatar")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Unknown bot"
    assert config.get_bot(theirs["id"]).avatar == ""


def test_a_shared_shipped_default_is_never_deleted(world):
    owner, mine, _uma = _two_people(world)
    bid = mine.post("/api/me/characters", json={"name": "Keeper"}).json()["id"]
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    shared = config.AVATAR_DIR / "atlas-face.png"
    shared.write_bytes(_png((64, 64)))
    before = _md5(shared.read_bytes())
    # A bot pointed at a picture whose name is NOT its own (the shape the shipped
    # atlas-face.png has): the file is shared and is not this bot's to delete.
    config.save_bot_avatar(bid, "atlas-face.png")
    assert config.get_bot(bid).avatar == "atlas-face.png"

    r = mine.delete(f"/api/me/characters/{bid}/avatar")
    assert r.status_code == 200, r.text
    assert r.json()["files"] == []
    assert shared.is_file(), "a shared shipped default was deleted"
    assert _md5(shared.read_bytes()) == before
    assert config.get_bot(bid).avatar == ""
