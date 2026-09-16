"""Tests for the avatar thumb pipeline (added 2026-09-15).

WHAT THIS FILE IS
-----------------
Three layers:

  1. `_avatar_pair_images` now returns a TRIPLET (face, full, thumb),
     not a pair. The thumb is a 128×128 downscale of the face.

  2. `_process_avatar_upload` writes all three files
     (`<bot>-face.png`, `<bot>-full.png`, `<bot>-thumb.png`).

  3. `avatar_snapshots.snapshot_id` and `snapshot_pair` also capture
     the thumb half (best-effort — older uploads without a thumb file
     fall back to a PIL rescale of the face bytes).

`avatar_snapshots.path_for_thumb` and `_thumb_sibling` are the lookup
helpers the serving route uses.

These are pure unit tests; no DB, no async runtime. The integration
suite (`tests/.../test_avatar_*.py`) covers the route-level happy path.
"""
from __future__ import annotations

import importlib.util
import zlib

import pytest

# Make PIL optional — the test environment for jobs may not have it.
# A `find_spec` probe, not a try/except import: the import existed only to
# answer "is Pillow here?", so the name it bound was never used and ruff
# rightly flagged it. `HAS_PIL` is what the skipif below actually needs.
HAS_PIL = importlib.util.find_spec("PIL") is not None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_png(width: int, height: int, color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    """A minimal valid PNG of the given size, painted solid color.

    Built without PIL — uses zlib + the PNG byte format directly so the
    test runs in envs without pillow installed.
    """
    def _chunk(tag: bytes, data: bytes) -> bytes:
        import struct
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(color) * width
    idat = zlib.compress(raw, 9)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# --------------------------------------------------------------------------- #
# Triplet shape — face, full, thumb
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAS_PIL, reason="Pillow required for resize math")
def test_thumb_is_smaller_than_face():
    """The thumb half of the upload triplet is a downscale of the face crop.

    The face cap is 512×512; the thumb is fixed at 128×128. Anything else
    would mean the upload pipeline forgot to resize, and the jobs board
    would load the full face crop on every row of the list.
    """
    from app.main import THUMB_EDGE
    assert THUMB_EDGE == 128


# --------------------------------------------------------------------------- #
# avatar_snapshots helpers
# --------------------------------------------------------------------------- #


def test_thumb_name_derives_from_face_name():
    """`<hash>.png` -> `<hash>-thumb.png`. Same shape as `_full_name` so
    the serving route and the snapshotter agree on filename derivation."""
    from app import avatar_snapshots
    assert avatar_snapshots._thumb_name("abcdef0123456789.png") == \
        "abcdef0123456789-thumb.png"
    assert avatar_snapshots._thumb_name("abcdef0123456789.jpg") == \
        "abcdef0123456789-thumb.jpg"


def test_path_for_thumb_returns_none_for_missing_snapshot():
    """A snapshot id with no thumbnail on disk yields None — the route
    falls back to the face crop."""
    from app import avatar_snapshots
    assert avatar_snapshots.path_for_thumb("") is None
    assert avatar_snapshots.path_for_thumb("/etc/passwd") is None


def test_path_for_thumb_rejects_traversal():
    """Untrusted snapshot id -> None, never a path outside the store."""
    from app import avatar_snapshots
    assert avatar_snapshots.path_for_thumb("../etc/passwd") is None
    assert avatar_snapshots.path_for_thumb("..\\windows") is None
    assert avatar_snapshots.path_for_thumb("foo/bar.png") is None


def test_path_for_thumb_returns_path_when_file_exists(tmp_path, monkeypatch):
    """A real `<hash>-thumb.png` on disk is returned. Same containment
    check as ``path_for`` — no escaping the store."""
    from app import avatar_snapshots
    store = tmp_path / "avatar-snapshots"
    store.mkdir()
    sid = "0123456789abcdef.png"
    (store / sid).write_bytes(_make_png(128, 128))
    (store / "0123456789abcdef-thumb.png").write_bytes(_make_png(128, 128))

    monkeypatch.setattr(avatar_snapshots, "_dir", lambda: store)
    out = avatar_snapshots.path_for_thumb(sid)
    assert out is not None
    assert out.name == "0123456789abcdef-thumb.png"
    assert out.is_file()


def test_thumb_sibling_finds_sibling_file(tmp_path):
    """`_thumb_sibling` mirrors `_full_sibling` for the third file in
    the pair/triplet. Same-suffix sibling wins (no cross-extension
    fallback — pool pairs are written together and always share one)."""
    from app import avatar_snapshots
    face = tmp_path / "main-face.png"
    face.write_bytes(_make_png(64, 64))
    (tmp_path / "main-thumb.png").write_bytes(_make_png(128, 128))

    out = avatar_snapshots._thumb_sibling(face)
    assert out is not None
    assert out.name == "main-thumb.png"


def test_thumb_sibling_returns_none_when_missing(tmp_path):
    """Missing thumb sibling -> None. The route falls back to the face."""
    from app import avatar_snapshots
    face = tmp_path / "main-face.png"
    face.write_bytes(_make_png(64, 64))
    out = avatar_snapshots._thumb_sibling(face)
    assert out is None


# --------------------------------------------------------------------------- #
# snapshot_pair signature — the thumb argument is accepted and used
# --------------------------------------------------------------------------- #


def test_snapshot_pair_signature_has_thumb_kwarg():
    """Adding the thumb to ``snapshot_pair`` is a NEW kwarg; the call
    sites must keep working without one (backward-compat), and pass one
    in to get the thumb half written. A future refactor that demotes the
    argument would break pool snapshots silently — pin the surface here.
    """
    import inspect

    from app import avatar_snapshots
    sig = inspect.signature(avatar_snapshots.snapshot_pair)
    assert "thumb_data" in sig.parameters
    assert sig.parameters["thumb_data"].default is None


# --------------------------------------------------------------------------- #
# Helper for the test_image_*.py tests below
# --------------------------------------------------------------------------- #


import struct  # noqa: E402  (after the helpers above)
