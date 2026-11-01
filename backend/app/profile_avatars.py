"""Profile pictures: a person uploads one for their own profile in Settings.

A picture is visible to every person who uses this DisPatch, the sign-in screen included
(an unsigned device sees the profile tiles), so the served route is public by design and the
Settings card says so at upload time.

What is stored: the upload is decoded with Pillow, centre-cropped (or cropped to the face
square) to a square, resized to 256 px and re-encoded as WebP. Re-encoding drops every metadata
block (EXIF, GPS, XMP, ICC), so nothing about where or with what a photo was taken survives.

Moving pictures (maintainer 2026-10-24: "include videos or gifs also (it's low bandwidth)"): an
animated GIF / WebP, or a short video (MP4, WebM, MOV — decoded by the host's ``ffmpeg`` with a
fixed argv; refused politely where there is none), becomes a small LOOPING animated WebP: at most
:data:`ANIM_SECONDS` seconds at :data:`ANIM_FPS` frames a second, no sound, and it is shrunk
(size, then frame rate) until it fits :data:`ANIM_MAX_OUT` bytes so the sign-in screen stays
light. It is still one ``.webp`` file served exactly like a still one. The file is
``<data>/profile-avatars/<profile id>-<16 hex of the content hash>.webp`` and
``profiles.avatar`` holds its served URL (``/api/profile-avatars/<file>``). A new upload
replaces (deletes) the old file. ``profiles.avatar`` is otherwise an emoji or a few letters;
:func:`valid_text_avatar` is what the People routes accept, so a stored path can only have been
written by the upload route and an ``<img>`` can never be pointed at an arbitrary URL.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import re
import threading
from pathlib import Path

from . import accounts, config

log = logging.getLogger("dispatch.profile_avatars")

URL_PREFIX = "/api/profile-avatars/"
DIRNAME = "profile-avatars"
MAX_BYTES = 20 * 1024 * 1024        # the upload (a phone video clip); the stored file is far smaller
MAX_PIXELS = 40_000_000
SIZE = 256
TEXT_MAX = 8
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
#: a moving picture: seconds kept, frames a second, and the most the stored file may weigh
ANIM_SECONDS = 6.0
ANIM_FPS = 12
ANIM_MAX_FRAMES = int(ANIM_SECONDS * ANIM_FPS)
ANIM_MAX_OUT = 1_200_000
#: (side, fps) tried in order until the animation fits ANIM_MAX_OUT
_ANIM_STEPS = ((256, 12), (200, 10), (160, 8), (128, 6))
FFMPEG_TIMEOUT_S = 60
#: ``<profile id>-<16 hex>.webp``; the served name must match this exactly.
NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}-[0-9a-f]{16}\.webp\Z")
_PID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_LOCK = threading.Lock()


class AvatarError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def folder() -> Path:
    return Path(config.DATA_DIR) / DIRNAME


def is_path_value(value: str | None) -> bool:
    """True for a stored value that is an uploaded picture's URL."""
    return bool(value) and str(value).startswith(URL_PREFIX)


def valid_text_avatar(value: str) -> bool:
    """An emoji or a few characters, or empty. Never a path or URL."""
    return isinstance(value, str) and len(value) <= TEXT_MAX and not any(c in value for c in "/\\<>\n\r\0")


def path_for_url(url: str | None) -> Path | None:
    """The file behind a stored URL, or None when the value is not one of ours."""
    if not is_path_value(url):
        return None
    name = str(url)[len(URL_PREFIX):]
    if not NAME_RE.match(name):
        return None
    return folder() / name


def file_for_name(name: str) -> Path | None:
    """The file for a request name, or None. Traversal-proof: the name must match
    :data:`NAME_RE` and the resolved file must sit directly in the folder."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    root = folder().resolve()
    p = (root / name).resolve()
    if p.parent != root or not p.is_file():
        return None
    return p


def parse_crop(x: float | None, y: float | None, size: float | None) -> tuple[float, float, float] | None:
    """The face square from the face picker (``crop_x``/``crop_y`` = its top-left corner as
    fractions of the width/height, ``crop_size`` = its side as a fraction of the shorter edge),
    or None for a centred square. All three or none; raises :class:`AvatarError`."""
    if x is None and y is None and size is None:
        return None
    if x is None or y is None or size is None or not (0 <= x <= 1 and 0 <= y <= 1 and 0 < size <= 1):
        raise AvatarError("avatar_crop_invalid", "The face square must be given as fractions from 0 to 1.")
    return (float(x), float(y), float(size))


def _square_box(w: int, h: int, crop: tuple[float, float, float] | None) -> tuple[int, int, int, int]:
    side = min(w, h)
    if crop is None:
        left, top = (w - side) // 2, (h - side) // 2
    else:
        cx, cy, cs = crop
        side = max(8, min(side, int(cs * side)))
        left = max(0, min(int(cx * w), w - side))
        top = max(0, min(int(cy * h), h - side))
    return left, top, left + side, top + side


def _is_video(raw: bytes) -> bool:
    """MP4 / MOV (``ftyp`` box) or WebM / Matroska (EBML magic) — by content, never by name."""
    return (len(raw) > 12 and raw[4:8] == b"ftyp") or raw[:4] == b"\x1a\x45\xdf\xa3"


def _frames_from_video(raw: bytes) -> tuple[list, int]:
    """Decode the first ANIM_SECONDS of a video into Pillow frames (ANIM_FPS, no audio) with the
    host's ffmpeg: fixed argv, temp files only, a timeout. Returns (frames, ms per frame)."""
    import shutil
    import subprocess
    import tempfile

    from PIL import Image
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AvatarError("avatar_video_unsupported",
                          "This DisPatch cannot read videos (no ffmpeg). Use a GIF or a picture.")
    with tempfile.TemporaryDirectory(prefix="dispatch-pp-") as td:
        src = Path(td) / "in.bin"
        src.write_bytes(raw)
        # the shorter edge to 320 px first, so a 4K clip never becomes 72 full-size PNGs
        vf = f"fps={ANIM_FPS},scale='if(gt(iw,ih),-2,320)':'if(gt(iw,ih),320,-2)'"
        argv = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(ANIM_SECONDS),
                "-i", str(src), "-an", "-sn", "-dn", "-vf", vf, "-frames:v", str(ANIM_MAX_FRAMES),
                str(Path(td) / "f%03d.png")]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise AvatarError("avatar_invalid", "That video took too long to read.") from None
        names = sorted(Path(td).glob("f*.png"))
        if r.returncode != 0 or not names:
            raise AvatarError("avatar_invalid", "That file is not a video DisPatch can read.")
        frames = []
        for n in names:
            with Image.open(n) as im:
                frames.append(im.convert("RGB"))
    return frames, round(1000 / ANIM_FPS)


def _frames_from_animation(im) -> tuple[list, list[int]]:
    """Every frame of an animated GIF / WebP within ANIM_SECONDS, resampled to at most ANIM_FPS
    (frames are dropped, their time given to the kept one). Returns (frames, durations ms)."""
    frames, durs = [], []
    t = 0.0
    step = 1000.0 / ANIM_FPS
    next_keep = 0.0
    for i in range(im.n_frames):
        im.seek(i)
        d = float(im.info.get("duration") or 100) or 100.0
        if t >= ANIM_SECONDS * 1000:
            break
        if t + 0.5 >= next_keep or not frames:
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            frames.append(im.convert("RGBA" if has_alpha else "RGB"))
            durs.append(int(d))
            next_keep = t + step
        else:
            durs[-1] += int(d)
        t += d
    return frames, durs


def _encode_animation(frames: list, durs: list[int], crop) -> bytes:
    """Square-crop every frame and save a looping WebP, shrinking until it fits ANIM_MAX_OUT."""
    from PIL import Image
    w, h = frames[0].size
    box = _square_box(w, h, crop)
    squares = [f.crop(box) for f in frames]
    out = b""
    for side, fps in _ANIM_STEPS:
        keep = max(1, round(ANIM_FPS / fps))
        sel = squares[::keep]
        sel_d = [sum(durs[i:i + keep]) for i in range(0, len(durs), keep)][:len(sel)]
        small = [f.resize((side, side), Image.LANCZOS) for f in sel]
        buf = io.BytesIO()
        small[0].save(buf, "WEBP", save_all=True, append_images=small[1:], duration=sel_d,
                      loop=0, quality=70, method=4)
        out = buf.getvalue()
        if len(out) <= ANIM_MAX_OUT:
            return out
    raise AvatarError("avatar_too_large", "That animation is too busy to keep small. Try a shorter clip.", 413)


def process(raw: bytes, crop: tuple[float, float, float] | None = None) -> bytes:
    """Upload bytes -> a 256 px square WebP with no metadata (``crop``: the face square, see
    :func:`parse_crop`; None = centred); a moving picture -> a small looping WebP.
    Raises :class:`AvatarError`."""
    if not raw:
        raise AvatarError("avatar_invalid", "No picture was sent.")
    if len(raw) > MAX_BYTES:
        raise AvatarError("avatar_too_large", "The file is too large (20 MB at most).", 413)
    if _is_video(raw):
        frames, ms = _frames_from_video(raw)
        if len(frames) == 1:
            return _encode_still(frames[0], crop)
        return _encode_animation(frames, [ms] * len(frames), crop)
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ALLOWED_FORMATS:
                raise AvatarError("avatar_invalid", "Use a picture (PNG, JPEG, WebP, GIF) or a short video.")
            if im.width * im.height > MAX_PIXELS:
                raise AvatarError("avatar_too_large", "The picture has too many pixels.", 413)
            if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
                frames, durs = _frames_from_animation(im)
                if len(frames) > 1:
                    return _encode_animation(frames, durs, crop)
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            return _encode_still(im.convert("RGBA" if has_alpha else "RGB"), crop)
    except AvatarError:
        raise
    except Exception:
        raise AvatarError("avatar_invalid", "That file is not a picture DisPatch can read.") from None


def _encode_still(im, crop) -> bytes:
    from PIL import Image
    square = im.crop(_square_box(im.width, im.height, crop)).resize((SIZE, SIZE), Image.LANCZOS)
    out = io.BytesIO()
    square.save(out, "WEBP", quality=88, method=4)      # no exif=/icc_profile=: metadata dropped
    return out.getvalue()


def _safe_pid(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PID_RE.match(profile_id):
        raise AvatarError("profile_not_found", "No such profile.", 404)
    return profile_id


def _unlink(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def remove_files(profile_id: str) -> int:
    """Delete every picture file of ``profile_id`` (also strays from an interrupted replace)."""
    if not isinstance(profile_id, str) or not _PID_RE.match(profile_id):
        return 0
    root = folder()
    n = 0
    with _LOCK, contextlib.suppress(OSError):
        if root.is_dir():
            for p in root.glob(f"{profile_id}-*"):
                if p.is_file() and (NAME_RE.match(p.name) or p.name.endswith(".partial")):
                    _unlink(p)
                    n += 1
    return n


def set_avatar(profile_id: str, raw: bytes,
               crop: tuple[float, float, float] | None = None) -> accounts.Profile:
    """Process ``raw`` (cropped to ``crop``, else centred) and make it the profile's picture;
    the old file goes away."""
    _safe_pid(profile_id)
    data = process(raw, crop)
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{profile_id}-{digest}.webp"
    with _LOCK:
        prof = accounts.get_profile(profile_id)
        if prof is None:
            raise AvatarError("profile_not_found", "No such profile.", 404)
        root = folder()
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".{name}.partial"
        tmp.write_bytes(data)
        tmp.replace(root / name)
        url = URL_PREFIX + name
        updated = accounts.update_profile(profile_id, avatar=url)
        old = path_for_url(prof.avatar)
        if old is not None and old.name != name:
            _unlink(old)
        return updated


def clear_avatar(profile_id: str) -> accounts.Profile:
    """Back to no picture (an empty value; the UI shows the initial)."""
    _safe_pid(profile_id)
    with _LOCK:
        prof = accounts.get_profile(profile_id)
        if prof is None:
            raise AvatarError("profile_not_found", "No such profile.", 404)
        updated = accounts.update_profile(profile_id, avatar="") if prof.avatar else prof
        _unlink(path_for_url(prof.avatar))
    remove_files(profile_id)
    return updated


def drop_replaced(old_value: str | None, new_value: str | None) -> None:
    """After a text avatar replaced an uploaded one: delete the file that is no longer used."""
    if old_value and old_value != new_value:
        _unlink(path_for_url(old_value))
