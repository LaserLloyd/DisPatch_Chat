"""Profile pictures (WP-AVATAR): upload, replace, remove, the fences, the public file route."""
from __future__ import annotations

import io

import pytest

from app import accounts, auth, backups, profile_avatars, profiles_service


@pytest.fixture
def owner(world):
    return world.create_owner()


def _png(size=(300, 200), color=(200, 30, 30), fmt="PNG", exif=None, **kw):
    from PIL import Image
    buf = io.BytesIO()
    im = Image.new("RGB", size, color)
    args = dict(kw)
    if exif is not None:
        args["exif"] = exif
    im.save(buf, fmt, **args)
    return buf.getvalue()


def _user(world, owner, name="Ivy", role="user", pin="1111"):
    p = world.person(owner, name, role, pin=pin)
    c = world.client()
    assert world.login(c, profile_id=p["id"], pin=pin).status_code == 200
    return p, c


def _up(c, url, data, name="me.png", ctype="image/png"):
    return c.post(url, files={"file": (name, data, ctype)})


def _stored(pid):
    return sorted(p.name for p in profile_avatars.folder().glob(f"{pid}-*")) if profile_avatars.folder().is_dir() else []


def test_a_user_uploads_their_own_picture_and_it_is_a_clean_256_webp(world, owner):
    from PIL import Image
    p, c = _user(world, owner)
    r = _up(c, "/api/me/avatar", _png())
    assert r.status_code == 200, r.text
    url = r.json()["avatar"]
    assert url.startswith("/api/profile-avatars/") and url.endswith(".webp")
    assert accounts.get_profile(p["id"]).avatar == url
    files = _stored(p["id"])
    assert files == [url.rsplit("/", 1)[1]]
    with Image.open(profile_avatars.folder() / files[0]) as im:
        assert im.format == "WEBP" and im.size == (256, 256)
    # The sign-in screen's profile list carries it.
    anon = world.client()
    tiles = anon.get("/api/auth/status").json()["profiles"]
    assert [t["avatar"] for t in tiles if t["id"] == p["id"]] == [url]


def test_the_picture_is_served_to_an_unsigned_device_with_safe_headers(world, owner):
    p, c = _user(world, owner)
    url = _up(c, "/api/me/avatar", _png()).json()["avatar"]
    anon = world.client()
    r = anon.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "max-age" in r.headers["cache-control"]
    assert r.content[:4] == b"RIFF"


def test_exif_and_gps_are_stripped_and_orientation_is_applied(world, owner):
    from PIL import Image
    exif = Image.Exif()
    exif[0x0112] = 6                       # rotate 90
    exif[0x010F] = "SECRETCAMERA"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (35.0, 40.0, 0.0)
    jpeg = _png((400, 200), fmt="JPEG", exif=exif)
    assert b"SECRETCAMERA" in jpeg
    p, c = _user(world, owner)
    assert _up(c, "/api/me/avatar", jpeg, "p.jpg", "image/jpeg").status_code == 200
    out = (profile_avatars.folder() / _stored(p["id"])[0]).read_bytes()
    assert b"SECRETCAMERA" not in out and b"Exif" not in out and b"GPS" not in out
    with Image.open(io.BytesIO(out)) as im:
        assert not dict(im.getexif()) and "exif" not in im.info and "xmp" not in im.info
        assert im.size == (256, 256)


def test_formats_accepted_and_a_gif_uses_its_first_frame(world, owner):
    from PIL import Image
    p, c = _user(world, owner)
    for data, name, ctype in ((_png(fmt="JPEG"), "a.jpg", "image/jpeg"), (_png(fmt="WEBP"), "a.webp", "image/webp"),
                              (_png(fmt="GIF"), "a.gif", "image/gif")):
        assert _up(c, "/api/me/avatar", data, name, ctype).status_code == 200, name
    frames = [Image.new("RGB", (40, 40), col) for col in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    assert _up(c, "/api/me/avatar", buf.getvalue(), "anim.gif", "image/gif").status_code == 200
    with Image.open(profile_avatars.folder() / _stored(p["id"])[0]) as im:
        assert im.n_frames == 1 if hasattr(im, "n_frames") else True
        r, g, b = im.convert("RGB").getpixel((128, 128))
        assert r > 180 and b < 80


def test_refusals_not_an_image_svg_bmp_empty_and_too_big(world, owner):
    p, c = _user(world, owner)
    r = _up(c, "/api/me/avatar", b"this is not a picture", "x.png")
    assert r.status_code == 422 and r.json()["error"]["code"] == "avatar_invalid"
    assert _up(c, "/api/me/avatar", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "x.svg", "image/svg+xml").status_code == 422
    assert _up(c, "/api/me/avatar", _png(fmt="BMP"), "x.bmp", "image/bmp").status_code == 422
    assert _up(c, "/api/me/avatar", b"", "x.png").status_code == 422
    big = _png((2400, 2400)) + b"\0" * (5 * 1024 * 1024 + 10)
    assert _up(c, "/api/me/avatar", big).status_code == 413
    assert accounts.get_profile(p["id"]).avatar == "" and _stored(p["id"]) == []


def test_replace_deletes_the_old_file_and_remove_clears_it(world, owner):
    p, c = _user(world, owner)
    first = _up(c, "/api/me/avatar", _png(color=(10, 200, 10))).json()["avatar"]
    second = _up(c, "/api/me/avatar", _png(color=(10, 10, 200))).json()["avatar"]
    assert first != second
    assert _stored(p["id"]) == [second.rsplit("/", 1)[1]]
    assert world.client().get(first).status_code == 404
    r = c.delete("/api/me/avatar")
    assert r.status_code == 200 and r.json()["avatar"] == ""
    assert _stored(p["id"]) == [] and accounts.get_profile(p["id"]).avatar == ""
    assert c.delete("/api/me/avatar").status_code == 200        # nothing to remove is fine


def test_an_unsigned_caller_cannot_upload_or_remove(world, owner):
    anon = world.client()
    assert _up(anon, "/api/me/avatar", _png()).status_code == 401
    assert anon.delete("/api/me/avatar").status_code == 401


def test_a_user_cannot_set_somebody_elses_picture_an_admin_can(world, owner):
    a, ca = _user(world, owner, "Ann")
    b, cb = _user(world, owner, "Bob", pin="2222")
    assert _up(ca, f"/api/people/{b['id']}/avatar", _png()).status_code == 403
    assert ca.delete(f"/api/people/{b['id']}/avatar").status_code == 403
    r = _up(owner, f"/api/people/{b['id']}/avatar", _png())
    assert r.status_code == 200 and accounts.get_profile(b["id"]).avatar.startswith("/api/profile-avatars/")
    assert accounts.get_profile(a["id"]).avatar == ""
    assert owner.delete(f"/api/people/{b['id']}/avatar").status_code == 200
    assert accounts.get_profile(b["id"]).avatar == ""


def test_the_protected_admin_picture_is_its_own_only(world, owner):
    boss = accounts.get_owner()
    pw = "ada-secret-pw1"
    other = world.person(owner, "Ada", "admin", password=pw)
    ca = world.client()
    assert world.login(ca, profile_id=other["id"], password=pw).status_code == 200
    r = _up(ca, f"/api/people/{boss.id}/avatar", _png())
    assert r.status_code == 403
    assert ca.delete(f"/api/people/{boss.id}/avatar").status_code == 403
    assert accounts.get_profile(boss.id).avatar == ""
    assert _up(owner, "/api/me/avatar", _png()).status_code == 200      # itself: fine
    assert accounts.get_profile(boss.id).avatar.startswith(profile_avatars.URL_PREFIX)


def test_the_shared_default_profile_picture_is_an_admins_only(world, owner):
    lobby = owner.post("/api/people", json={"name": "Lobby", "role": "user"}).json()
    auth.set_lock_target("default_profile", lobby["id"])
    guest = world.client()
    me = guest.get("/api/auth/me").json()
    assert me["profile"]["id"] == lobby["id"]                  # an unsigned device lands on it
    r = _up(guest, "/api/me/avatar", _png())
    assert r.status_code == 403 and r.json()["error"]["code"] == "lock_target_fence"
    assert guest.delete("/api/me/avatar").status_code == 403
    assert accounts.get_profile(lobby["id"]).avatar == ""
    assert _up(owner, f"/api/people/{lobby['id']}/avatar", _png()).status_code == 200
    assert guest.delete("/api/me/avatar").status_code == 403    # still fenced once it has one
    assert accounts.get_profile(lobby["id"]).avatar.startswith(profile_avatars.URL_PREFIX)


def test_path_traversal_and_odd_names_are_refused(world, owner):
    p, c = _user(world, owner)
    _up(c, "/api/me/avatar", _png())
    anon = world.client()
    for bad in ("..%2f..%2fsecurity.yaml", "%2e%2e%2fchats.db", "..", "x.webp", f"{p['id']}-zz.webp",
                f"{p['id']}-0123456789abcdef.png", f"{p['id']}-0123456789abcdef.webp", "a%00b-0123456789abcdef.webp",
                f"..%5c{p['id']}-0123456789abcdef.webp"):
        assert anon.get(f"/api/profile-avatars/{bad}").status_code == 404, bad
    assert profile_avatars.file_for_name("../security.yaml") is None
    assert profile_avatars.file_for_name("a/b-0123456789abcdef.webp") is None


def test_patch_takes_emoji_or_empty_never_a_path(world, owner):
    p, c = _user(world, owner)
    url = _up(c, "/api/me/avatar", _png()).json()["avatar"]
    for bad in ("/api/profile-avatars/x.webp", "http://evil.test/x.png", "a/b", "<img>", "x" * 9, "..\\x"):
        r = owner.patch(f"/api/people/{p['id']}", json={"avatar": bad})
        assert r.status_code == 422, bad
    assert owner.post("/api/people", json={"name": "Zed", "role": "user", "avatar": "/etc/passwd"}).status_code == 422
    assert accounts.get_profile(p["id"]).avatar == url
    # Sending the stored path back unchanged is a no-op, not a way to write a path.
    assert owner.patch(f"/api/people/{p['id']}", json={"avatar": url, "name": "Ivy2"}).status_code == 200
    assert accounts.get_profile(p["id"]).avatar == url
    other = world.person(owner, "Kit", "user")
    assert owner.patch(f"/api/people/{other['id']}", json={"avatar": url}).status_code == 422
    # An emoji replaces the picture and the file goes.
    r = owner.patch(f"/api/people/{p['id']}", json={"avatar": "🦊"})
    assert r.status_code == 200 and r.json()["avatar"] == "🦊"
    assert _stored(p["id"]) == []
    assert owner.patch(f"/api/people/{p['id']}", json={"avatar": ""}).status_code == 200


def test_deleting_the_profile_removes_the_file_and_reset_keeps_it(world, owner):
    p, c = _user(world, owner)
    url = _up(c, "/api/me/avatar", _png()).json()["avatar"]
    profiles_service.wipe_profile_content(p["id"])                 # what Reset to default runs
    assert accounts.get_profile(p["id"]).avatar == url and len(_stored(p["id"])) == 1
    token = profiles_service.delete_preview(p["id"])["token"]
    profiles_service.delete_profile(p["id"], token)
    assert accounts.get_profile(p["id"]) is None
    assert _stored(p["id"]) == []
    assert world.client().get(url).status_code == 404


def test_the_picture_folder_is_in_backups_and_not_an_orphan_sweep_target():
    assert "profile-avatars" in backups.DATA_DIRS and "profile-avatars" in backups.ALLOWED_TOP
    from app import media_inventory
    assert any("profile-avatars" in w["writes"] for w in media_inventory.WRITERS)
