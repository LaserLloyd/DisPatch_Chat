"""Removing a bot's picture (DELETE /api/bots/{bot_id}/avatar).

The settings screen has to be able to take a bot's picture away, not only
replace it. A removal must be a REAL one and still leave the app sane:

  * the PAIR goes together — face crop, full-res original, thumb;
  * config.yaml drops the filename, so the bot falls back to its letter/emoji
    tile (avatar_url is empty, /avatar answers 404);
  * the old face is captured into the snapshot store first, so
    /avatar/history still lists it and Restore can bring it back.

Integration-level: the real route on the isolated `world` app.
"""
from __future__ import annotations

import asyncio
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


def test_remove_deletes_the_pair_clears_config_and_keeps_history(world):
    c = world.create_owner()
    config.upsert_bot(config.Bot(id="scratch", name="Scratch"))
    config._invalidate_bots_cache()

    up = c.post("/api/bots/scratch/avatar",
                files={"file": ("a.png", _png(), "image/png")},
                params={"crop_x": 0.25, "crop_y": 0.25, "crop_size": 0.5})
    assert up.status_code == 200, up.text
    face = config.get_bot("scratch").avatar
    assert face, "the upload did not point the bot at a face"
    before = _owned_files("scratch")
    assert len(before) == 3, before          # face + full + thumb
    snapshot = up.json()["avatar_url"]

    # One thread wears the picture: that pin is what /avatar/history reads.
    asyncio.run(world.db.create_thread(bot_id="scratch", owner_profile_id=None))

    rm = c.delete("/api/bots/scratch/avatar")
    assert rm.status_code == 200, rm.text
    body = rm.json()
    assert body["removed"] is True and body["avatar_url"] == ""
    assert body["files"], "nothing was unlinked"

    # A real removal, on disk and in config.yaml.
    assert _owned_files("scratch") == []
    assert config.get_bot("scratch").avatar == ""
    assert config.get_bot("scratch").avatar_url == ""
    assert c.get("/api/bots/scratch/avatar").status_code == 404
    assert c.get("/api/bots/scratch/avatar/full").status_code == 404

    # ...and the old face is still recoverable from history.
    hist = c.get("/api/bots/scratch/avatar/history")
    assert hist.status_code == 200, hist.text
    ids = [e["id"] for e in hist.json()["avatars"]]
    assert body["snapshot_id"] and body["snapshot_id"] in ids
    old = c.get(f"/api/bots/scratch/avatar/history/{body['snapshot_id']}")
    assert old.status_code == 200 and old.content


def test_remove_is_idempotent_and_404s_an_unknown_bot(world):
    c = world.create_owner()
    config.upsert_bot(config.Bot(id="plain", name="Plain"))
    config._invalidate_bots_cache()
    first = c.delete("/api/bots/plain/avatar")
    assert first.status_code == 200, first.text
    assert first.json()["files"] == [] and first.json()["snapshot_id"] is None
    again = c.delete("/api/bots/plain/avatar")
    assert again.status_code == 200, again.text
    assert c.delete("/api/bots/nope/avatar").status_code == 404


def test_a_face_no_thread_wore_is_still_listed_after_removal(world):
    """The face removed between two chats must stay restorable.

    History is otherwise read from thread pins; a bot whose only thread was
    created earlier (or which has none) would drop the picture silently even
    though the bytes are in the store. The per-bot record keeps it listed.
    """
    c = world.create_owner()
    config.upsert_bot(config.Bot(id="lonely", name="Lonely"))
    config._invalidate_bots_cache()
    up = c.post("/api/bots/lonely/avatar", files={"file": ("a.png", _png(), "image/png")})
    assert up.status_code == 200, up.text
    rm = c.delete("/api/bots/lonely/avatar")
    assert rm.status_code == 200, rm.text
    hist = c.get("/api/bots/lonely/avatar/history")
    ids = [e["id"] for e in hist.json()["avatars"]]
    assert rm.json()["snapshot_id"] in ids, ids
    assert c.get(f"/api/bots/lonely/avatar/history/{rm.json()['snapshot_id']}").status_code == 200


def test_remove_never_touches_a_shared_shipped_default(world):
    """The repo's default pictures are not this bot's to delete."""
    c = world.create_owner()
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    shared = config.AVATAR_DIR / "atlas-face.png"
    shared.write_bytes(_png((64, 64)))
    # A bot pointed at a picture whose name is NOT its own (the shape the
    # shipped atlas-face.png has for the main bot): the file is shared.
    config.upsert_bot(config.Bot(id="keeper", name="Keeper", avatar="atlas-face.png"))
    config._invalidate_bots_cache()
    r = c.delete("/api/bots/keeper/avatar")
    assert r.status_code == 200, r.text
    assert shared.is_file(), "a file this bot does not own was deleted"
    assert config.get_bot("keeper").avatar == ""
