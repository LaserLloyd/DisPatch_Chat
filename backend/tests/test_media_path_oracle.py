"""``GET /api/media?path=`` answers one status (404) for everything it does not serve, so it
cannot tell a profile which paths exist on the host (a 403 for an existing file outside the
media store did)."""
from __future__ import annotations

from app import config


def test_not_served_is_404_whether_the_path_exists_or_not(world, tmp_path):
    owner = world.create_owner(world.client())
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    pic = config.MEDIA_DIR / "seen.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (config.MEDIA_DIR / "notes.txt").write_text("x")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    statuses = {}
    for name, p in {"exists, not media": "/etc/hostname", "missing": "/etc/definitely-not-here",
                    "outside the store": str(outside), "wrong type in the store": str(config.MEDIA_DIR / "notes.txt"),
                    "missing in the store": str(config.MEDIA_DIR / "nope.png")}.items():
        statuses[name] = owner.get("/api/media", params={"path": p}).status_code
    assert set(statuses.values()) == {404}, statuses
    assert owner.get("/api/media", params={"path": str(pic)}).status_code == 200
