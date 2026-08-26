"""Thread avatar snapshots — the face a conversation started with.

The point of the feature is visual variety in the thread list when the avatar
rotates. The point of THESE tests is the two things that make it safe to ship:
it must never duplicate storage per thread, and it must never leak a non-safe
bot's picture to a Safe-Mode session.
"""
from __future__ import annotations

import uuid

import pytest

from app import avatar_snapshots, config


class _Bot:
    def __init__(self, avatar):
        self.avatar = avatar


def _write_avatar(name: str, data: bytes) -> None:
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (config.AVATAR_DIR / name).write_bytes(data)


def test_same_image_is_stored_once_no_matter_how_many_threads(tmp_path, monkeypatch):
    """The whole reason this is content-addressed.

    The obvious implementation writes one file per thread; measured on a live
    install that was 105 files for 39 distinct images, i.e. the same bytes about
    three times over, growing with every conversation.
    """
    snap_dir = config.DATA_DIR / "avatar-snapshots"
    # Count the delta, not the total: the test fixtures create threads of their
    # own, so this directory is not ours alone.
    before = len(list(snap_dir.iterdir())) if snap_dir.is_dir() else 0

    # Unique bytes per run: a fixed payload hashes to a fixed name, so a
    # snapshot left by an earlier run would already be on disk and the delta
    # would read 0 — the dedupe working correctly, reported as a failure.
    _write_avatar("a.png", b"PNGDATA-" + uuid.uuid4().hex.encode())
    ids = {avatar_snapshots.snapshot_id(_Bot("a.png")) for _ in range(25)}

    assert len(ids) == 1, "the same image produced more than one snapshot id"
    after = len(list(snap_dir.iterdir()))
    assert after - before == 1, (
        f"25 threads on one image added {after - before} files; expected 1")


def test_a_different_image_gets_its_own_snapshot():
    _write_avatar("a.png", b"PNGDATA-ONE")
    _write_avatar("b.png", b"PNGDATA-TWO")
    first = avatar_snapshots.snapshot_id(_Bot("a.png"))
    second = avatar_snapshots.snapshot_id(_Bot("b.png"))
    assert first and second and first != second


def test_rotating_the_same_filename_yields_a_new_snapshot():
    """The live install rotates by overwriting main-face.png, so the NAME is
    constant and only the bytes change. Hashing content rather than filename is
    what makes the feature work at all here."""
    tag = uuid.uuid4().hex.encode()
    _write_avatar("main-face.png", b"MONDAY-" + tag)
    monday = avatar_snapshots.snapshot_id(_Bot("main-face.png"))
    _write_avatar("main-face.png", b"TUESDAY-" + tag)
    tuesday = avatar_snapshots.snapshot_id(_Bot("main-face.png"))
    assert monday != tuesday, "a rotated avatar reused the old snapshot"


@pytest.mark.parametrize("avatar", ["", None, "missing.png", "evil.exe", "../../etc/passwd"])
def test_unsnapshottable_avatars_return_none_rather_than_raising(avatar):
    """A snapshot is a decoration. Anything odd yields None and the thread
    renders the live avatar — the pre-feature behaviour. It must never be able
    to fail thread creation."""
    assert avatar_snapshots.snapshot_id(_Bot(avatar)) is None


def test_snapshot_id_tolerates_a_bot_with_no_avatar_attribute():
    assert avatar_snapshots.snapshot_id(None) is None


@pytest.mark.parametrize("sid", [
    "", "../secrets", "a/b.png", "..%2Fx.png", "x.exe", "....//x.png",
])
def test_path_for_refuses_traversal_and_odd_extensions(sid):
    """The id arrives from a URL, so it is untrusted."""
    assert avatar_snapshots.path_for(sid) is None


def test_path_for_resolves_a_real_snapshot():
    _write_avatar("a.png", b"PNGDATA-ONE")
    sid = avatar_snapshots.snapshot_id(_Bot("a.png"))
    p = avatar_snapshots.path_for(sid)
    assert p is not None and p.read_bytes() == b"PNGDATA-ONE"


def test_an_oversized_avatar_is_not_snapshotted():
    _write_avatar("huge.png", b"x" * (avatar_snapshots.MAX_SNAPSHOT_BYTES + 1))
    assert avatar_snapshots.snapshot_id(_Bot("huge.png")) is None


def test_url_is_keyed_by_thread_not_by_hash():
    """A hash-keyed URL could not be gated: the id says nothing about which bot
    the picture belongs to. Going through the thread reuses the rule that
    already governs the thread. The `?s=` tag is the snapshot's own hash prefix:
    it changes when the pin changes, so the immutable-cached response can't
    outlive a re-pin — but it is cache-busting only, never an access path."""
    assert avatar_snapshots.url_for("t123", "abc.png") == "/api/threads/t123/avatar?s=abc"
    assert avatar_snapshots.url_for("t123", "0123456789abcdef.png") == \
        "/api/threads/t123/avatar?s=01234567"
    assert avatar_snapshots.url_for("t123", None) == ""


def test_prune_keeps_referenced_and_drops_orphans(tmp_path, monkeypatch):
    """Prune with the keep-set its ONLY caller actually builds.

    The earlier version of this test called `prune({one_id})` and explained in
    a comment that anything else in the directory was fair game — "prune
    legitimately removes those too". That is true of the function and false of
    the system: `_prune_avatar_snapshots` walks EVERY thread, adds each thread's
    snapshot AND its `-full` sibling, then adds every current bot avatar. A
    keep set of one never occurs unless the caller failed to read the threads.

    Describing the wrong keep set is not a harmless simplification. It is the
    shape that made a one-element keep set look like normal usage, and the day
    a suite ran against the live install it deleted 117 referenced snapshots
    while this test stayed green. So: an isolated store, a keep set built the
    way production builds it, and an exact assertion on what survives.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")

    # Two referenced avatars, each a face/full PAIR (upload and rotation both
    # write `<stem>-face.png` beside `<stem>-full.png`), plus one orphan.
    _write_avatar("main-face.png", b"FACE-ONE")
    _write_avatar("main-full.png", b"FULL-ONE")
    first = avatar_snapshots.snapshot_id(_Bot("main-face.png"))
    _write_avatar("main-face.png", b"FACE-TWO")
    _write_avatar("main-full.png", b"FULL-TWO")
    second = avatar_snapshots.snapshot_id(_Bot("main-face.png"))
    _write_avatar("gone.png", b"NOBODY-REFERENCES-ME")
    orphan = avatar_snapshots.snapshot_id(_Bot("gone.png"))
    assert first and second and orphan and first != second

    # Exactly what _prune_avatar_snapshots assembles: every referenced id and
    # its derived full-res name.
    keep = set()
    for sid in (first, second):
        keep.add(sid)
        keep.add(avatar_snapshots._full_name(sid))

    store = tmp_path / "avatar-snapshots"
    before = {p.name for p in store.iterdir()}
    assert orphan in before and len(before) == 5, before   # 2 pairs + 1 orphan

    removed = avatar_snapshots.prune(keep)

    assert removed == 1, f"expected only the orphan to go, removed {removed}"
    assert {p.name for p in store.iterdir()} == keep, "the store is not exactly the keep set"
    assert avatar_snapshots.path_for(first) is not None
    assert avatar_snapshots.path_for(second) is not None
    # The full halves matter as much as the faces: losing one leaves the
    # thumbnail working and the lightbox broken, which is how the last loss
    # was mistaken for a one-bot problem.
    assert avatar_snapshots.path_for_full(first) is not None
    assert avatar_snapshots.path_for_full(second) is not None
    assert avatar_snapshots.path_for(orphan) is None, "an orphan survived prune"


def test_prune_refuses_an_empty_keep_set(tmp_path, monkeypatch):
    """"Keep nothing" is never an instruction; it is a caller that failed.

    Every keep set is derived by reading the thread table. An empty one means
    either a store with nothing to protect (deleting is a no-op anyway) or a
    read that returned nothing — and in that case the literal reading is
    "delete the entire store". It happened: 117 snapshots, 163 threads showing
    the wrong face. The refusal is the load-bearing part of prune(), so it gets
    a test of its own rather than living only in a comment.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    _write_avatar("a.png", b"IRREPLACEABLE")
    sid = avatar_snapshots.snapshot_id(_Bot("a.png"))
    store = tmp_path / "avatar-snapshots"
    before = {p.name for p in store.iterdir()}
    assert before

    assert avatar_snapshots.prune(set()) == 0

    assert {p.name for p in store.iterdir()} == before, "an empty keep set deleted files"
    assert avatar_snapshots.path_for(sid) is not None


# --------------------------------------------------------------------------- #
# End to end: capture on create, serve by thread, gated like the thread itself
# --------------------------------------------------------------------------- #

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402
from app.database import Database  # noqa: E402


@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    with TestClient(main.app, client=("testclient", 50000)) as c:
        yield c
    asyncio.run(temp_db.close())


def _bot_with_avatar(bot_id: str, safe: bool, data: bytes) -> None:
    """Give a bot an avatar file and register it in config.yaml."""
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (config.AVATAR_DIR / f"{bot_id}.png").write_bytes(data)
    entries = [{"id": b.id, "name": b.name, "avatar": b.avatar, "emoji": b.emoji,
                "order": b.order, "visible": b.visible, "safe": b.safe}
               for b in config.load_bots()]
    for e in entries:
        if e["id"] == bot_id:
            e["avatar"], e["safe"] = f"{bot_id}.png", safe
            break
    else:
        entries.append({"id": bot_id, "name": bot_id.title(), "avatar": f"{bot_id}.png",
                        "emoji": "", "order": 9, "visible": True, "safe": safe})
    config._write_bots(entries)
    config._invalidate_bots_cache()


def test_thread_captures_and_serves_the_avatar_it_started_with(snap_env):
    """The whole feature, through the API a browser actually uses."""
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"MONDAY-" + uuid.uuid4().hex.encode())

    monday = client.post("/api/threads", json={"bot_id": bot}).json()
    assert monday["avatar_url"].startswith(f"/api/threads/{monday['id']}/avatar?s=")
    r = client.get(monday["avatar_url"])
    assert r.status_code == 200 and r.content.startswith(b"MONDAY-")

    # Rotate the avatar the way the live install does — same filename, new bytes.
    _bot_with_avatar(bot, safe=True, data=b"TUESDAY-" + uuid.uuid4().hex.encode())
    tuesday = client.post("/api/threads", json={"bot_id": bot}).json()

    assert client.get(tuesday["avatar_url"]).content.startswith(b"TUESDAY-")
    # ...and Monday's thread still shows Monday's face. This is the point of
    # the feature: without it both rows render today's avatar.
    assert client.get(monday["avatar_url"]).content.startswith(b"MONDAY-")


def test_a_thread_with_no_snapshot_404s_rather_than_serving_todays_face(snap_env):
    """Older threads fall back to the live avatar IN THE FRONTEND. The route
    must not silently substitute a different picture — that would make the
    feature look broken in a way nobody could explain."""
    client = snap_env
    bot = config.load_bots()[0].id          # no avatar configured
    t = client.post("/api/threads", json={"bot_id": bot}).json()
    assert t["avatar_url"] == ""
    assert client.get(f"/api/threads/{t['id']}/avatar").status_code == 404


def test_safe_mode_cannot_fetch_a_non_safe_bots_snapshot(snap_env):
    """The gate that makes hash-keyed URLs unsafe, asserted.

    Snapshots are content-addressed, so the id reveals nothing about which bot
    owns the picture. Serving BY THREAD means the existing decoy rule applies:
    if you may not see the thread, you may not see its avatar.
    """
    client = snap_env
    non_safe = next(b.id for b in config.load_bots() if not b.safe)
    _bot_with_avatar(non_safe, safe=False, data=b"PRIVATE-" + uuid.uuid4().hex.encode())

    thread = client.post("/api/threads", json={"bot_id": non_safe}).json()
    assert client.get(thread["avatar_url"]).status_code == 200   # unlocked: fine

    # Now put the session into Safe Mode the way the gate suite does: a PIN is
    # set and never unlocked, which is what decoy MEANS
    # (pinSet and not authenticated). My first attempt posted to endpoints that
    # do not exist, so the client stayed unlocked and the assertion "passed"
    # against a session that was never gated — a green test proving nothing.
    auth.set_pin("1234")
    r = client.get(thread["avatar_url"])
    assert r.status_code == 403, f"a non-safe bot's avatar leaked to Safe Mode: {r.status_code}"


# --------------------------------------------------------------------------- #
# The pair contract: a thumbnail and the image its lightbox opens are the SAME
# picture — from upload, through the snapshot store, to a per-thread re-pin.
# --------------------------------------------------------------------------- #

from io import BytesIO  # noqa: E402


def _png_bytes(color, size=(64, 64)) -> bytes:
    from PIL import Image
    b = BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def test_a_snapshot_completes_its_missing_full_half_on_recapture():
    """An early return used to skip the -full block whenever the face was
    already captured — so a snapshot first taken while the sibling was missing
    could NEVER acquire one. The pair now completes itself."""
    tag = uuid.uuid4().hex.encode()
    _write_avatar("pairbot-face.png", b"FACE-" + tag)
    sid = avatar_snapshots.snapshot_id(_Bot("pairbot-face.png"))
    assert sid and avatar_snapshots.path_for_full(sid) is None

    _write_avatar("pairbot-full.png", b"FULL-" + tag)
    assert avatar_snapshots.snapshot_id(_Bot("pairbot-face.png")) == sid
    full = avatar_snapshots.path_for_full(sid)
    assert full is not None and full.read_bytes() == b"FULL-" + tag


def test_snapshot_pair_stores_both_halves_under_one_id():
    tag = uuid.uuid4().hex.encode()
    sid = avatar_snapshots.snapshot_pair(b"FACE-" + tag, b"FULL-" + tag)
    assert sid
    assert avatar_snapshots.path_for(sid).read_bytes() == b"FACE-" + tag
    assert avatar_snapshots.path_for_full(sid).read_bytes() == b"FULL-" + tag


def test_upload_uses_the_provided_face_and_keeps_the_full_original(snap_env):
    """The `face` field is where an image-CLI crop_to_face result arrives: the
    server must use it verbatim (capped at 512) and must NOT derive the face
    from the full — nor, as one repair script did, overwrite the full with the
    face crop and call an upscaled thumbnail 'full resolution'."""
    from PIL import Image
    client = snap_env
    bot = config.load_bots()[0].id
    r = client.post(f"/api/bots/{bot}/avatar",
                    files={"file": ("full.png", _png_bytes((10, 20, 200), (800, 600)), "image/png"),
                           "face": ("face.png", _png_bytes((200, 0, 0), (700, 700)), "image/png")})
    assert r.status_code == 200, r.text
    face_im = Image.open(config.AVATAR_DIR / f"{bot}-face.png")
    full_im = Image.open(config.AVATAR_DIR / f"{bot}-full.png")
    assert face_im.size == (512, 512), "provided face not capped to 512"
    assert full_im.size == (800, 600), "the full original was not preserved"
    assert face_im.convert("RGB").getpixel((5, 5))[0] > 150, \
        "face is not the provided crop (should be red, was derived from the blue full)"


def test_upload_cleans_stale_cross_extension_siblings(snap_env):
    """A leftover main-full.png beside a new main-full.jpg wins the extension
    probe and serves the PREVIOUS avatar as this one's full resolution."""
    client = snap_env
    bot = config.load_bots()[0].id
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (config.AVATAR_DIR / f"{bot}-full.jpg").write_bytes(b"stale")
    (config.AVATAR_DIR / f"{bot}-face.webp").write_bytes(b"stale")
    r = client.post(f"/api/bots/{bot}/avatar",
                    files={"file": ("full.png", _png_bytes((1, 2, 3)), "image/png")})
    assert r.status_code == 200, r.text
    assert not (config.AVATAR_DIR / f"{bot}-full.jpg").exists()
    assert not (config.AVATAR_DIR / f"{bot}-face.webp").exists()
    assert (config.AVATAR_DIR / f"{bot}-face.png").is_file()
    assert (config.AVATAR_DIR / f"{bot}-full.png").is_file()


def test_avatar_change_repins_unused_threads_but_freezes_used_ones(snap_env):
    """A pre-created EMPTY thread tracks the current avatar until its first
    message; a thread with history keeps the face it started under. This is
    what makes 'today's daily thread wears today's face' true even though the
    thread row was created before the rotation ran."""
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"OLD-" + uuid.uuid4().hex.encode())

    empty = client.post("/api/threads", json={"bot_id": bot}).json()
    used = client.post("/api/threads", json={"bot_id": bot}).json()
    client.post(f"/api/threads/{used['id']}/messages", json={"content": "hi", "role": "user"})

    r = client.post(f"/api/bots/{bot}/avatar",
                    files={"file": ("full.png", _png_bytes((9, 9, 9)), "image/png")})
    assert r.status_code == 200, r.text

    new_face = (config.AVATAR_DIR / f"{bot}-face.png").read_bytes()
    assert client.get(f"/api/threads/{empty['id']}/avatar").content == new_face, \
        "an unused thread kept the pre-rotation face"
    assert client.get(f"/api/threads/{used['id']}/avatar").content.startswith(b"OLD-"), \
        "a thread WITH history was re-pinned — its face must be frozen"


def test_repin_endpoint_gives_one_thread_its_own_picture(snap_env):
    from PIL import Image
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"CUR-" + uuid.uuid4().hex.encode())
    t = client.post("/api/threads", json={"bot_id": bot}).json()
    client.post(f"/api/threads/{t['id']}/messages", json={"content": "x", "role": "user"})

    r = client.post(f"/api/threads/{t['id']}/avatar",
                    files={"file": ("full.png", _png_bytes((0, 120, 0), (900, 400)), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["avatar_snapshot"]
    assert body["avatar_url"].startswith(f"/api/threads/{t['id']}/avatar?s=")

    face = client.get(f"/api/threads/{t['id']}/avatar")
    full = client.get(f"/api/threads/{t['id']}/avatar?full=1")
    assert Image.open(BytesIO(face.content)).size == (400, 400)   # centered square
    assert Image.open(BytesIO(full.content)).size == (900, 400)   # the original
    # The BOT's current avatar is untouched — the pin belongs to the thread.
    assert (config.AVATAR_DIR / f"{bot}.png").read_bytes().startswith(b"CUR-")

    # source=current re-pins back to the bot's live avatar.
    r2 = client.post(f"/api/threads/{t['id']}/avatar?source=current")
    assert r2.status_code == 200, r2.text
    assert client.get(f"/api/threads/{t['id']}/avatar").content.startswith(b"CUR-")


def test_repin_upload_has_a_body_ceiling(snap_env, monkeypatch):
    """The thread pin used to sit outside `_upload_ceiling`, so an arbitrarily
    large body was read fully into RAM before anything measured it."""
    assert main._upload_ceiling("/api/threads/abc/avatar") == (
        main.UPLOAD_MAX_IMAGE + main._MULTIPART_SLACK)

    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"CUR-x")
    t = client.post("/api/threads", json={"bot_id": bot}).json()

    monkeypatch.setattr(main, "UPLOAD_MAX_IMAGE", 512)
    monkeypatch.setattr(main, "_MULTIPART_SLACK", 512)
    reached = []
    monkeypatch.setattr(main, "_avatar_pair_images",
                        lambda *a, **k: reached.append(1))
    r = client.post(f"/api/threads/{t['id']}/avatar",
                    files={"file": ("full.png", b"P" * 4096, "image/png")})
    assert r.status_code == 413
    assert r.json()["detail"] == "File too large"
    assert not reached, "the body must be refused before it is decoded"


def test_repin_with_neither_file_nor_source_is_a_400(snap_env):
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"CUR-" + uuid.uuid4().hex.encode())
    t = client.post("/api/threads", json={"bot_id": bot}).json()
    assert client.post(f"/api/threads/{t['id']}/avatar").status_code == 400


def test_thread_full_fallback_is_revalidatable_not_immutable(snap_env):
    """?full=1 on a face-only snapshot serves the face — an answer a later
    backfill can improve, so caching it for a year froze the low-res forever."""
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"ONLYFACE-" + uuid.uuid4().hex.encode())
    t = client.post("/api/threads", json={"bot_id": bot}).json()

    r_face = client.get(f"/api/threads/{t['id']}/avatar")
    assert "immutable" in r_face.headers.get("cache-control", "")
    r_full = client.get(f"/api/threads/{t['id']}/avatar?full=1")
    assert r_full.status_code == 200
    assert r_full.headers.get("cache-control") == "no-cache"


def test_safe_mode_gets_thumbnails_never_full_res(snap_env):
    """Locked = view thumbnails only, uniformly: the thread ?full=1 route obeys
    the same rule as /api/bots/<id>/avatar/full, even for SAFE bots."""
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"SAFE-" + uuid.uuid4().hex.encode())
    t = client.post("/api/threads", json={"bot_id": bot}).json()

    auth.set_pin("1234")                     # decoy: pin set, never unlocked
    assert client.get(f"/api/threads/{t['id']}/avatar").status_code == 200
    assert client.get(f"/api/threads/{t['id']}/avatar?full=1").status_code == 403


def test_snapshot_pair_distinct_fulls_get_distinct_ids():
    """The re-pin path (snapshot_pair) is keyed by hash(face+full), NOT the face
    alone: two threads pinning the SAME face crop with DIFFERENT full images must
    not collapse onto the first full — the little/big mismatch the whole system
    prevents, reappearing by construction. Regression for a confirmed defect."""
    face = _png_bytes((90, 90, 90), (100, 100))
    full_a = _png_bytes((200, 0, 0), (800, 600))
    full_b = _png_bytes((0, 0, 200), (640, 480))
    sid_a = avatar_snapshots.snapshot_pair(face, full_a)
    sid_b = avatar_snapshots.snapshot_pair(face, full_b)
    assert sid_a and sid_b and sid_a != sid_b, "same face + different full collided"
    from io import BytesIO

    from PIL import Image
    a = Image.open(avatar_snapshots.path_for_full(sid_a))
    b = Image.open(avatar_snapshots.path_for_full(sid_b))
    assert a.size == (800, 600) and b.size == (640, 480)
    # An identical pair still dedupes to one id.
    assert avatar_snapshots.snapshot_pair(face, full_a) == sid_a


def test_explicit_thread_pin_survives_a_rotation(snap_env):
    """A custom picture given to an EMPTY thread must not be clobbered when the
    bot's avatar next changes. repin_unused only touches auto-pinned threads."""
    from io import BytesIO

    from PIL import Image
    client = snap_env
    bot = config.load_bots()[0].id
    _bot_with_avatar(bot, safe=True, data=b"CUR-" + uuid.uuid4().hex.encode())
    t = client.post("/api/threads", json={"bot_id": bot}).json()   # empty
    # Give this empty thread its own picture.
    r = client.post(f"/api/threads/{t['id']}/avatar",
                    files={"file": ("f.png", _png_bytes((240, 200, 0), (500, 300)), "image/png")})
    assert r.status_code == 200, r.text
    mine = client.get(f"/api/threads/{t['id']}/avatar").content
    # Rotate the bot avatar — the empty thread must KEEP its explicit pin.
    client.post(f"/api/bots/{bot}/avatar",
                files={"file": ("f.png", _png_bytes((9, 200, 9), (640, 640)), "image/png")})
    after = client.get(f"/api/threads/{t['id']}/avatar").content
    assert after == mine, "an explicit pin on an empty thread was clobbered by rotation"


def test_upload_bakes_in_exif_orientation(snap_env):
    """A phone portrait carries orientation=6 and stores pixels landscape. The
    saved full must be upright, not sideways, and the face cropped from the
    upright image. Regression for a confirmed defect (no exif_transpose)."""
    from io import BytesIO

    from PIL import Image
    client = snap_env
    bot = config.load_bots()[0].id
    # A 600x400 image tagged orientation=6 → displays as 400x600 upright.
    im = Image.new("RGB", (600, 400), (10, 10, 10))
    exif = im.getexif()
    exif[0x0112] = 6
    buf = BytesIO()
    im.save(buf, "JPEG", exif=exif)
    r = client.post(f"/api/bots/{bot}/avatar",
                    files={"file": ("phone.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    full = Image.open(BytesIO(client.get(f"/api/bots/{bot}/avatar/full").content))
    assert full.size == (400, 600), f"EXIF orientation not applied: {full.size}"


def test_history_image_refuses_a_snapshot_from_another_bot(snap_env):
    """The store is shared and content-addressed. Gating only on the bot named
    in the PATH let any snapshot id be pulled by naming a safe bot instead of
    the one that actually wore it."""
    client = snap_env
    non_safe = next(b.id for b in config.load_bots() if not b.safe)
    safe = next(b.id for b in config.load_bots() if b.safe)
    _bot_with_avatar(non_safe, safe=False, data=b"PRIVATE-" + uuid.uuid4().hex.encode())
    _bot_with_avatar(safe, safe=True, data=b"PUBLIC-" + uuid.uuid4().hex.encode())

    client.post("/api/threads", json={"bot_id": non_safe})
    hist = client.get(f"/api/bots/{non_safe}/avatar/history").json()["avatars"]
    assert hist, "expected a snapshot for the non-safe bot"
    sid = hist[0]["id"]

    # Its own bot still serves it.
    assert client.get(f"/api/bots/{non_safe}/avatar/history/{sid}").status_code == 200
    # Borrowing a safe bot's path does not.
    r = client.get(f"/api/bots/{safe}/avatar/history/{sid}")
    assert r.status_code == 404, f"a foreign snapshot was served: {r.status_code}"
    assert client.get(f"/api/bots/{safe}/avatar/history/{sid}?full=1").status_code == 404


# --------------------------------------------------------------------------- #
# Losing a snapshot must be LOUD.
#
# The store is content-addressed and written once, so a file a thread points at
# going missing is always a fault. It used to render as an ordinary broken
# thumbnail and nothing else: no log line, no health signal, no way to tell it
# apart from a thread that simply never had a snapshot. That silence is what
# made a wiped store take two days to notice.
# --------------------------------------------------------------------------- #

def test_prune_refuses_an_empty_keep_set():
    """"Keep nothing" means the caller failed to read the threads.

    Obeying it deletes the whole store -- which is exactly what happened when a
    test ran prune() against the live install with a keep-set built from one
    fixture bot.
    """
    _write_avatar("a.png", b"PRECIOUS-" + uuid.uuid4().hex.encode())
    sid = avatar_snapshots.snapshot_id(_Bot("a.png"))
    assert avatar_snapshots.prune(set()) == 0, "an empty keep set pruned something"
    assert avatar_snapshots.path_for(sid) is not None, \
        "the store was emptied on an empty keep set"


def test_missing_blobs_names_the_threads_whose_picture_is_gone():
    class _T:
        def __init__(self, tid, sid):
            self.id, self.avatar_snapshot = tid, sid

    _write_avatar("a.png", b"STILL-HERE-" + uuid.uuid4().hex.encode())
    live = avatar_snapshots.snapshot_id(_Bot("a.png"))
    threads = [_T("t-ok", live), _T("t-gone", "0123456789abcdef.png"), _T("t-none", None)]

    gone = avatar_snapshots.missing_blobs(threads)
    assert gone == [("t-gone", "0123456789abcdef.png")], gone


def test_a_vanished_snapshot_is_reported_not_just_404ed(snap_env, caplog):
    """Serving a thread whose blob was deleted logs it and shows up in health.

    The 404 itself is correct (the frontend falls back to the live avatar); the
    point is that the failure stops being invisible.
    """
    import logging

    client = snap_env
    bot = next(b.id for b in config.load_bots() if not b.safe)
    _bot_with_avatar(bot, safe=False, data=b"DOOMED-" + uuid.uuid4().hex.encode())
    created = client.post("/api/threads", json={"bot_id": bot}).json()
    thread = created.get("thread", created)
    tid, sid = thread["id"], thread["avatar_snapshot"]
    assert avatar_snapshots.path_for(sid) is not None

    before = client.get("/api/health?detailed=1").json()["avatar_snapshots_missing"]
    avatar_snapshots.path_for(sid).unlink()          # the loss

    with caplog.at_level(logging.WARNING):
        assert client.get(f"/api/threads/{tid}/avatar").status_code == 404
    assert any("avatar snapshot missing" in r.getMessage() for r in caplog.records), \
        "a vanished snapshot was served as a silent 404"

    after = client.get("/api/health?detailed=1").json()["avatar_snapshots_missing"]
    assert after == before + 1, f"health did not surface the loss ({before} -> {after})"
