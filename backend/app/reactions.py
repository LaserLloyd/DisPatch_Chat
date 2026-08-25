"""Reaction images — the ephemeral overlay pack.

A *reaction* is a named image that any client (a family device, or an OpenClaw
agent) can **fire**. Firing broadcasts a transient WebSocket frame; every
connected device pops the image over the chat for a few seconds and then it is
gone. The overlay itself is never persisted — the only history a reaction
leaves is a one-line collapsed trace in the thread (written by main.py).

There are two tiers, and they answer different needs:

  * the **pack** — named, permanent reactions you fire by id, over and over
    (``:react:facepalm:``). Curated, small, stable.
  * the **pool** — a bank of freshly-generated images kept on hand (a target
    number PER MOOD), each fired exactly ONCE. Topped back up nightly, and
    immediately for any mood that drops below its low-water mark. See the
    "rotating pool" section at the bottom of this module.

The pack is SHARED; the pool belongs to a bot. Each reactions-enabled bot has
its own prompt bank, its own shelf of images and its own refill schedule, so
two characters never draw from — or spend — each other's pictures. ``<bot_id>``
is the roster id from config.yaml.

    <data>/reactions/builtin/               seeded starter pack — regenerable, never precious
    <data>/reactions/pack/                  uploaded + generated images (user data)
    <data>/reactions/moods-<bot_id>/<mood>/ ready one-shot pool images, one folder per mood per bot
    <data>/reactions/spent/<mood>/          fired pool images — kept forever (chat traces);
                                            shared across bots (uuid names never collide)
    <data>/reactions.yaml                   the pack registry (metadata + settings) — SHARED
    <data>/reaction-prompts-<bot_id>.yaml   per-bot prompt bank
    <data>/reaction-pool-<bot_id>.yaml      per-bot pool config + batch date

An install that predates the split (one pool, ``reactions/moods/`` +
``reaction-pool.yaml`` + ``reaction-prompts.yaml``) is moved onto this layout
by :func:`_ensure_migration`, which hands the single pool to the default
reaction bot — see :func:`default_bot_id`.

A pack ``file`` is ``"<root>/<name>"``; a pool file is
``"moods-<bot_id>/<mood>/<name>"`` (``"spent/<mood>/<name>"`` once fired, since
the spent store is shared). Resolution goes through
:func:`image_path`, which re-checks containment — a hand-edited registry cannot
walk out of the reaction roots and turn this into an arbitrary-file-read
endpoint. For the pool the FILESYSTEM is the source of truth: whatever image
files sit in a mood's folder are that reaction's stock, so there is no manifest
to drift out of sync with the disk.

Safe Mode: every reaction carries a ``safe`` flag, defaulting to **False**.
Only explicitly-flagged reactions are visible to (or firable from) a locked
device, mirroring how safe bots' avatars work. Fail closed: an unknown or
unflagged reaction is simply not there as far as Safe Mode is concerned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import config, openclaw_text, pool_common, pool_guard

log = logging.getLogger("local-chat.reactions")

# --------------------------------------------------------------------------- #
# Constants / validation
# --------------------------------------------------------------------------- #

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ROOTS = ("builtin", "pack", "moods", "spent")
# A mood folder name. Case is tolerated (operators hand-make these folders); dots
# are not — the name becomes a path component, so this doubles as the
# traversal guard for the middle segment of a pool file reference.
MOOD_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
# A BOT id, which since the per-bot pools (2026-08) is also a path component:
# reactions/moods-<bot_id>/, reaction-pool-<bot_id>.yaml. Roster ids are
# free-form and mixed case (`Helper_2`, `Scout`), so this is deliberately
# looser than ID_RE — but, like MOOD_DIR_RE, it admits no dot and no
# separator, which is what stops a bot_id query parameter from naming a file
# outside the data dir.
BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

# Raster only, and deliberately no SVG: a reaction image is served from our own
# origin, and an inline SVG can carry <script> (stored XSS). Same rule as the
# chat media pipeline in main.py.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
UPLOAD_MAX = 8 * 1024 * 1024          # 8MB — these are overlay images, not art prints

# Registry-size ceiling: keeps a runaway generator from turning the pack into an
# unbounded blob store, and keeps GET /api/reactions cheap.
MAX_REACTIONS = 300

# The image-generation CLI used to top up the reaction and avatar pools: a bare
# name resolved on PATH, or an absolute path. Deliberately no default — pointing
# at a guessed home path would both assume somebody else's install layout and
# bake one vendor's binary name into the product. Unset = generation is simply
# unavailable, and the pools degrade to "runs dry", which they already handle.
_IMAGE_CLI = os.environ.get("DISPATCH_IMAGE_CLI", "").strip()
# Generation is a cold-model-load away from slow; the CLI retries once itself.
IMAGE_CLI_TIMEOUT_S = int(config.env("IMAGE_CLI_TIMEOUT", "300"))


class ReactionError(ValueError):
    """Bad input / not found / registry problem. Carries an HTTP-ish status.

    A ``ValueError`` subclass on purpose: some REST routes guard mutations with
    a plain ``except (TypeError, ValueError)`` (the pool-config PUT), and a
    validation refusal raised here must land as their 400 — never bubble up as
    a 500 — even where main.py doesn't name this class.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# Per-bot path helpers — the core of the multi-bot pool architecture
# --------------------------------------------------------------------------- #

# The pre-2026-08 single-pool layout. Kept ONLY as the source of the one-shot
# rename into the per-bot layout below; nothing reads or writes these once the
# migration has run.
_OLD_BANK_NAME = "reaction-prompts.yaml"
_OLD_POOL_NAME = "reaction-pool.yaml"
_OLD_MOODS_NAME = "moods"          # under reactions/

# The per-bot layout. `bot_id` is a roster id, constrained to [A-Za-z0-9_-] by
# BOT_ID_RE wherever one arrives from outside — see resolve_bot_id, the gate
# every path helper goes through, because these ids end up as filenames.
_BANK_FMT = "reaction-prompts-{bot_id}.yaml"
_POOL_FMT = "reaction-pool-{bot_id}.yaml"
_MOODS_PREFIX = "moods-"
_MOODS_FMT = _MOODS_PREFIX + "{bot_id}"

# The anchor a pool-less install falls back to. Historically DisPatch had one
# pool belonging to a bot called "main", so its files are `…-main.yaml` /
# `moods-main/`; keeping that as the fallback is what lets an existing install
# upgrade without moving a byte. It is NOT assumed to exist in the roster —
# `default_bot_id()` prefers whatever bot the operator actually enabled.
LEGACY_BOT_ID = "main"


def resolve_bot_id(bot_id: str | None) -> str:
    """A bot id that is safe to interpolate into a filename.

    Every per-bot path and every per-bot cache key goes through here, and it
    is also what an API route calls to report which pool it actually served.
    `bot_id` arrives from query strings and request bodies, so a bare format()
    would let `../..` name any file on disk; :data:`BOT_ID_RE` admits no dot
    and no separator. Anything it refuses degrades to the default rather than
    raising — a junk id must not 500 the reactions feature, it must simply not
    become a different bot's pool.
    """
    bid = str(bot_id or "").strip()
    if bid and BOT_ID_RE.match(bid):
        return bid
    return default_bot_id()


def require_bot_id(bot_id: str | None) -> str:
    """Same as :func:`resolve_bot_id`, but a NAMED bot must be a real id.

    Reads may degrade to the default pool — an unknown ``?bot_id=`` on a GET
    is at worst a confusing answer. WRITES may not: ``PUT /api/reactions/
    prompts?bot_id=nope`` silently resolved to the default bot and overwrote
    ITS prompt bank, so a typo (or a hostile caller who cannot name the real
    bot) could replace the working bank of the bot that does exist. An
    explicit id that is not a valid bot id is a 404; an omitted one still
    means "the default pool".
    """
    bid = str(bot_id or "").strip()
    if not bid:
        return default_bot_id()
    if not BOT_ID_RE.match(bid):
        raise ReactionError(f"Unknown bot_id: {bid[:40]}", 404)
    return bid


def default_bot_id() -> str:
    """Which bot an omitted ``bot_id`` refers to.

    The first reactions-enabled bot in roster order, so a fresh install where
    the operator ticked "reactions" on their own bot gets a working pool (a
    seeded prompt bank, a nightly refill, a mood dir) without knowing that the
    files are named after a bot id at all. Falls back to :data:`LEGACY_BOT_ID`
    when no bot has the capability, which keeps an install whose roster is
    momentarily empty or unreadable pointed at the same files as before.
    """
    try:
        for b in config.load_bots():
            if b.reactions and BOT_ID_RE.match(b.id or ""):
                return b.id
    except Exception:                      # unreadable roster — never fatal
        pass
    return LEGACY_BOT_ID


def bank_path(bot_id: str | None = None) -> Path:
    """The prompt bank file for one bot."""
    return config.DATA_DIR / _BANK_FMT.format(bot_id=resolve_bot_id(bot_id))


def _pool_path(bot_id: str | None = None) -> Path:
    """The pool config file for one bot."""
    return config.DATA_DIR / _POOL_FMT.format(bot_id=resolve_bot_id(bot_id))


def _moods_dir(bot_id: str | None = None) -> Path:
    """The ready-image directory for one bot's pool."""
    return (config.DATA_DIR / "reactions"
            / _MOODS_FMT.format(bot_id=resolve_bot_id(bot_id)))


def _legacy_moods_dir() -> Path:
    """The pre-per-bot ``reactions/moods/`` root (migration source only)."""
    return config.REACTIONS_LEGACY_MOODS_DIR


def _ensure_migration() -> None:
    """Move a single-pool install onto the per-bot layout. Idempotent.

    Cheap enough to re-check on every load (three stat calls) and deliberately
    NOT memoised: the data dir is swapped under the module by tests, and a
    cached "already done" would make the second data dir skip its migration.

    Each artefact is renamed only when its per-bot counterpart is ABSENT, so an
    install that is already migrated is untouched — including the case that
    bit the live install: a stale generic ``reaction-prompts.yaml`` left behind
    beside real per-bot banks. Renaming that over a bot's bank would replace a
    curated prompt set with a default one, so the rule is strictly
    "only when there is nothing to lose". Nothing ever READS the old names, so
    a leftover is inert either way.
    """
    bot_id = default_bot_id()
    pairs: list[tuple[Path, Path, str]] = [
        (config.DATA_DIR / _OLD_BANK_NAME, bank_path(bot_id), "prompt bank"),
        (config.DATA_DIR / _OLD_POOL_NAME, _pool_path(bot_id), "pool config"),
        (_legacy_moods_dir(), _moods_dir(bot_id), "mood folders"),
    ]
    migrated = False
    for old, new, what in pairs:
        try:
            if not old.exists() or old.is_symlink() or new.exists():
                continue
            os.rename(old, new)
        except OSError as e:
            # A migration that cannot complete must not break the feature —
            # the old layout is simply ignored and the bot starts empty.
            log.warning("migration: could not move the %s to %s: %s",
                        what, new.name, e)
            continue
        log.info("migration: %s → %s", old.name, new.name)
        migrated = True

    if migrated:
        _bank_cache.clear()
        _pool_cache.clear()


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    """Pack-wide behaviour. Editable from the UI, persisted in reactions.yaml."""

    enabled: bool = True
    # How long the overlay stays up before it self-dismisses. The brief says
    # 10s; a per-reaction override is clamped into [min, max].
    default_duration_ms: int = 10_000
    min_duration_ms: int = 1_500
    max_duration_ms: int = 30_000
    # Rate limiting. A reaction interrupts every screen in the house, so an
    # agent in a retry loop must not be able to strobe them.
    cooldown_ms: int = 2_000          # per-actor gap between fires
    burst_max: int = 3                # fires per actor within burst_window_ms
    burst_window_ms: int = 20_000
    global_max_per_min: int = 20      # everybody, everything, combined
    # Client-side: how many queued overlays to hold before dropping the oldest.
    queue_max: int = 3

    def clamp_duration(self, ms: int | None) -> int:
        if not ms:
            return self.default_duration_ms
        return max(self.min_duration_ms, min(self.max_duration_ms, int(ms)))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reaction:
    id: str
    name: str
    file: str                                    # "<root>/<filename>"
    aliases: list[str] = field(default_factory=list)
    category: str = "general"
    safe: bool = False                           # visible/firable in Safe Mode
    duration_ms: int | None = None               # None = pack default
    source: str = "upload"                       # builtin | upload | generated
    prompt: str = ""                             # set for generated images
    created_at: str = ""

    def to_dict(self, *, settings: Settings | None = None) -> dict:
        d = asdict(self)
        d["image_url"] = image_url(self.id, self.file.rsplit("/", 1)[-1])
        if settings is not None:
            d["effective_duration_ms"] = settings.clamp_duration(self.duration_ms)
        return d


@dataclass
class Pack:
    settings: Settings = field(default_factory=Settings)
    reactions: list[Reaction] = field(default_factory=list)

    def by_id(self, rid: str) -> Reaction | None:
        for r in self.reactions:
            if r.id == rid:
                return r
        return None


# --------------------------------------------------------------------------- #
# Registry load / save (mtime-cached, same shape as config.load_bots)
# --------------------------------------------------------------------------- #

_cache: Pack | None = None
_cache_mtime: float | None = None


def _norm_id(raw: str) -> str:
    """Fold a human string into a valid reaction id, or raise."""
    s = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s).strip("-_").lower()[:48]
    if not ID_RE.match(s):
        raise ReactionError(f"Invalid reaction id: {raw!r}")
    return s


def _norm_aliases(raw: Iterable | None) -> list[str]:
    # Hand-edited yaml: a bare string is treated as one alias, any other
    # non-iterable junk as none — never a TypeError out of a load path.
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, Iterable):
        raw = []
    out: list[str] = []
    for a in (raw or []):
        try:
            a2 = _norm_id(a)
        except ReactionError:
            continue
        if a2 not in out:
            out.append(a2)
    return out[:12]


def _check_file(value: str) -> str:
    """Validate a ``file`` reference.

    Pack tier: exactly ``<root>/<basename>`` (builtin/ or pack/). Pool tier:
    ``moods-<bot_id>/<mood>/<basename>`` — or ``moods/…`` / ``spent/…`` for the
    pre-per-bot spelling, which stored chat traces still carry. The mood
    segment must look like a mood folder and the bot segment like a bot id;
    neither :data:`MOOD_DIR_RE` nor :data:`BOT_ID_RE` admits a dot, so ``..``
    can never ride in as a middle component.
    """
    parts = str(value or "").split("/")
    if len(parts) == 2 and parts[0] in ("builtin", "pack"):
        pass
    elif len(parts) == 3 and parts[0] in ("moods", "spent") and MOOD_DIR_RE.match(parts[1]):
        pass
    elif (len(parts) == 3 and parts[0].startswith(_MOODS_PREFIX)
          and BOT_ID_RE.match(parts[0][len(_MOODS_PREFIX):])
          and MOOD_DIR_RE.match(parts[1])):
        pass
    else:
        raise ReactionError(f"Invalid reaction file reference: {value!r}")
    name = parts[-1]
    if not name or name in (".", "..") or "\\" in name:
        raise ReactionError(f"Invalid reaction file reference: {value!r}")
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        raise ReactionError(f"Unsupported reaction image type: {name!r}")
    return "/".join(parts)


def _root_dir(root: str) -> Path:
    """Map a stored reference's first segment to a directory.

    ``moods`` is the pre-per-bot spelling. A trace persisted before the split
    still says ``moods/<mood>/<blob>``, but the blobs themselves were RENAMED
    into the default bot's folder by :func:`_ensure_migration` — so it has to
    resolve there, or every pre-split reaction trace loses its picture. The
    legacy folder is preferred only while it still exists, i.e. before the
    migration has had a chance to run.
    """
    legacy_moods = _legacy_moods_dir()
    return {
        "builtin": config.REACTIONS_BUILTIN_DIR,
        "pack": config.REACTIONS_PACK_DIR,
        "moods": legacy_moods if legacy_moods.is_dir() else _moods_dir(),
        "spent": config.REACTIONS_SPENT_DIR,
    }.get(root, config.REACTIONS_PACK_DIR)


def image_path(reaction: Reaction) -> Path:
    """Absolute path of a reaction's image, containment re-checked.

    Never trust the stored string: reactions.yaml is hand-editable and a
    symlink inside the pack dir could otherwise point anywhere on disk.

    A pool reference names its owner — ``moods-<bot_id>/<mood>/<name>`` — and
    resolves inside that bot's own directory. Pack references (``builtin/``,
    ``pack/``) and the shared ``spent/`` go through the root mapping.
    """
    rel = _check_file(reaction.file)
    parts = rel.split("/")
    root = parts[0]

    # Per-bot pool: "moods-<bot_id>/<mood>/<name>". _check_file has already
    # validated all three segments, so this is a containment RE-check (the
    # cheap kind that catches a symlink out of the tree), not the only guard.
    if root.startswith(_MOODS_PREFIX):
        base = (config.DATA_DIR / "reactions" / root).resolve()
    else:
        base = _root_dir(root).resolve()
    name = "/".join(parts[1:])
    p = (base / name).resolve()
    if p != base and base not in p.parents:
        raise ReactionError("Reaction image is outside the pack directory", 403)
    if not p.is_file():
        raise ReactionError(f"Reaction image is missing: {rel}", 404)
    return p


def image_url(rid: str, version: str | None = None) -> str:
    """Display URL for a reaction image.

    ``version`` is the stored blob name (content-addressed by uuid suffix), so
    a replaced card gets a NEW URL — clients never serve stale cached bytes.
    """
    base = f"/api/reactions/{rid}/image"
    return f"{base}?v={version}" if version else base


def _int_or(value, default: int) -> int:
    """``int()`` that degrades to the field default instead of raising.

    The load paths run through here: reactions.yaml / reaction-pool.yaml are
    hand-editable, and one junk numeric must cost that one value — not make
    every ``load()`` after it raise and 500 the whole reactions feature until
    someone repairs the file by hand.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_settings(s: Settings) -> Settings:
    """Sanity-clamp pack settings in place (and return them).

    A hand-edited file must not be able to set a 0ms cooldown or a 10-minute
    overlay — and (via :func:`_int_or`) a non-numeric value falls back to the
    field default rather than raising, so ``load()`` always succeeds.
    """
    d = Settings()
    s.enabled = bool(s.enabled)
    s.default_duration_ms = max(500, min(120_000, _int_or(s.default_duration_ms, d.default_duration_ms)))
    s.min_duration_ms = max(300, min(s.default_duration_ms, _int_or(s.min_duration_ms, d.min_duration_ms)))
    s.max_duration_ms = max(s.default_duration_ms, min(120_000, _int_or(s.max_duration_ms, d.max_duration_ms)))
    s.cooldown_ms = max(0, min(60_000, _int_or(s.cooldown_ms, d.cooldown_ms)))
    s.burst_max = max(1, min(50, _int_or(s.burst_max, d.burst_max)))
    s.burst_window_ms = max(1_000, min(600_000, _int_or(s.burst_window_ms, d.burst_window_ms)))
    s.global_max_per_min = max(1, min(600, _int_or(s.global_max_per_min, d.global_max_per_min)))
    s.queue_max = max(1, min(10, _int_or(s.queue_max, d.queue_max)))
    return s


def _parse(raw: dict) -> Pack:
    sdata = raw.get("settings") or {}
    if not isinstance(sdata, dict):
        sdata = {}
    known = set(Settings().to_dict())
    settings = _clamp_settings(Settings(**{k: v for k, v in sdata.items() if k in known}))

    out: list[Reaction] = []
    seen: set[str] = set()
    for item in (raw.get("reactions") or []):
        if not isinstance(item, dict):
            continue
        try:
            rid = _norm_id(item.get("id", ""))
            rfile = _check_file(item.get("file", ""))
        except ReactionError as e:
            log.warning("skipping bad reactions.yaml entry: %s", e.message)
            continue
        if not rfile.startswith(("builtin/", "pack/")):
            # The pool has its own manifest and its own one-shot lifecycle; a
            # pack entry pointing into pool/ or spent/ would make a rotating
            # image permanent, which is exactly what the pool must never be.
            log.warning("pack entry %r points outside the pack roots — skipped", rid)
            continue
        if rid in seen:
            log.warning("skipping duplicate reaction id %r", rid)
            continue
        seen.add(rid)
        dur = item.get("duration_ms")
        out.append(Reaction(
            id=rid,
            name=str(item.get("name") or rid)[:60],
            file=rfile,
            aliases=_norm_aliases(item.get("aliases")),
            category=str(item.get("category") or "general")[:40],
            safe=bool(item.get("safe", False)),
            duration_ms=int(dur) if isinstance(dur, (int, float)) and dur else None,
            source=str(item.get("source") or "upload")[:20],
            prompt=str(item.get("prompt") or "")[:500],
            created_at=str(item.get("created_at") or ""),
        ))
    return Pack(settings=settings, reactions=out)


def load() -> Pack:
    """Load the registry (mtime-cached). Seeds the starter pack on first run."""
    global _cache, _cache_mtime
    path = config.REACTIONS_PATH
    if not path.exists():
        _cache = _cache_mtime = None
        seed_starter_pack()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    raw: dict = {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("reactions.yaml unreadable (%s) — serving an empty pack", e)
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    _cache = _parse(raw)
    _cache_mtime = mtime
    return _cache


def invalidate() -> None:
    global _cache, _cache_mtime, _safe_ids_cache
    _cache = _cache_mtime = None
    _safe_ids_cache = None


def save(pack: Pack) -> Pack:
    """Persist the registry atomically and refresh the cache."""
    if len(pack.reactions) > MAX_REACTIONS:
        raise ReactionError(f"Reaction pack is full (max {MAX_REACTIONS})", 507)
    body = {
        "version": 1,
        "settings": pack.settings.to_dict(),
        "reactions": [
            {k: v for k, v in asdict(r).items() if v not in (None, "", [])}
            for r in pack.reactions
        ],
    }
    config.REACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.REACTIONS_PATH.with_suffix(".yaml.tmp")
    text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config.REACTIONS_PATH)
    invalidate()
    return load()


def _heal_base(rid: str) -> str:
    """``check_in-2`` shares the base ``check_in`` — a trailing ``-<n>`` is a
    regen generation counter, not identity."""
    m = re.match(r"^(.*?)-\d+$", rid)
    return m.group(1) if m else rid


def heal_pack() -> dict:
    """Reconcile the registry with what is actually on disk.

    Pack cards are content-addressed (``pack/<id>-<uuid>.png``), so a regen
    mints NEW files under NEW ids and an older entry can be left pointing at a
    blob that no longer exists. Such an entry still RESOLVES — the fire then
    dies at image time ("Reaction image is missing"), which is how 29 dangling
    ids sat in the registry refusing fires for weeks while looking perfectly
    healthy from the chat. Filesystem is truth: an entry whose file is gone
    either becomes an alias of its newest surviving kin (``check_in`` →
    ``check_in-6``) or leaves the registry.

    Idempotent and cheap (one stat per entry); runs at startup and from the
    pool maintenance loop, so the next regen that strands an id heals itself
    within a cycle instead of waiting for someone to notice the silence.

    Heal NEVER rebuilds a registry from an absent disk. If the pack roots are
    unreadable, or every entry dangles, or more than half of them do, the disk
    is the thing that is wrong (an unmounted data dir, a half-finished
    restore) — healing then would delete a working registry to match a
    temporary void. In that case it logs an error and leaves the yaml alone.
    """
    pack = load()
    if not pack.reactions:
        return {"dangling": 0, "aliased": 0, "dropped": 0}

    for root in (config.REACTIONS_BUILTIN_DIR, config.REACTIONS_PACK_DIR):
        if not root.is_dir():
            log.error("pack heal: skipped — pack root %s is not readable", root)
            return {"dangling": 0, "aliased": 0, "dropped": 0,
                    "skipped": "pack root unreadable"}

    dangling: list[Reaction] = []
    for r in pack.reactions:
        try:
            image_path(r)
        except ReactionError:
            dangling.append(r)
    if not dangling:
        return {"dangling": 0, "aliased": 0, "dropped": 0}

    dangling_ids = {r.id for r in dangling}
    survivors = [r for r in pack.reactions if r.id not in dangling_ids]
    if not survivors or len(dangling) * 2 > len(pack.reactions):
        log.error(
            "pack heal: skipped — %d of %d entries dangle, which reads as a"
            " missing/unmounted reactions directory rather than a few stranded"
            " cards; registry left untouched",
            len(dangling), len(pack.reactions))
        return {"dangling": len(dangling), "aliased": 0, "dropped": 0,
                "skipped": "too many dangling"}
    by_base: dict[str, list[Reaction]] = {}
    for r in survivors:
        by_base.setdefault(_heal_base(r.id), []).append(r)

    aliased = dropped = 0
    for r in dangling:
        kin = by_base.get(_heal_base(r.id)) or []
        target = max(kin, key=lambda s: (s.created_at or "", s.id)) if kin else None
        if target is not None:
            moved = [a for a in [r.id, *r.aliases]
                     if a != target.id and a not in target.aliases]
            target.aliases.extend(moved)
            aliased += 1
            log.warning("pack heal: %r lost its image — now an alias of %r",
                        r.id, target.id)
        else:
            dropped += 1
            log.warning("pack heal: %r lost its image and has no surviving kin"
                        " — dropped from the registry", r.id)
    save(Pack(settings=pack.settings, reactions=survivors))
    return {"dangling": len(dangling), "aliased": aliased, "dropped": dropped}


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


# Reserved keys that draw an unused image from the rotating pool instead of
# naming a permanent one. `:react:random:` is the agent-facing spelling.
DRAW_KEYS = ("random", "surprise", "any", "pool", "fresh")


def get(key: str, *, bot_id: str | None = None) -> Reaction | None:
    """Resolve by id, then by alias, then by a loose name match.

    Also handles the pool: the reserved draw keys pick an unused image, and a
    ``pool-…`` id resolves only while that image is still unfired.

    ``bot_id`` scopes pool operations to that bot's pool. Pack reactions are
    bot-agnostic (shared).
    """
    if not key:
        return None
    try:
        k = _norm_id(key)
    except ReactionError:
        return None
    if k in DRAW_KEYS:
        return pool_draw(bot_id=bot_id)
    if k.startswith(POOL_ID_PREFIX):
        return pool_get(k, bot_id=bot_id)
    # A mood name draws a FRESH image of that mood when the pool has one. This
    # is checked before the pack so `:react:mad:` gets today's picture rather
    # than a generic card; if that mood is dry it falls through to the pack, so
    # the worst case is a starter card instead of nothing.
    drawn = pool_draw(category=k, bot_id=bot_id)
    if drawn is not None:
        return drawn
    pack = load()
    for r in pack.reactions:
        if r.id == k:
            return r
    for r in pack.reactions:
        if k in r.aliases:
            return r
    for r in pack.reactions:
        if _norm_id(r.name or r.id) == k:
            return r
    return None


# safe_ids() walks EVERY mood folder plus the shared spent/ store, which only
# grows — fired images are kept forever. It sits on the request path of the
# Safe-Mode redactor, i.e. on the event loop, so the walk is memoised against
# the directory mtimes: adding or removing a file changes its folder's mtime,
# so the cache is exact rather than time-based.
_safe_ids_cache: tuple[tuple, set[str]] | None = None


def _mtime_ns(p: Path) -> int | None:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def _safe_ids_key() -> tuple:
    """One stat per directory (never per file) — what safe_ids() depends on."""
    parts: list[tuple] = [("pack", _mtime_ns(config.REACTIONS_PATH))]
    for base in (*_all_moods_roots(), config.REACTIONS_SPENT_DIR):
        parts.append((str(base), _mtime_ns(base)))
        for _mood, d in _mood_dirs(base):
            parts.append((str(d), _mtime_ns(d)))
    for base in _all_moods_roots():
        pp = _pool_path(_bot_id_from_moods_dir(base))
        parts.append((str(pp), _mtime_ns(pp)))    # the pool's `safe` flag
    return tuple(parts)


def safe_ids() -> set[str]:
    """Ids a Safe-Mode client may see the image of / fire.

    Includes spent pool images: a fired image is broadcast the instant it moves
    to spent/, so every client's fetch — a locked device's included — lands
    after the move; and the trace row can re-open it any time later.

    Pools are per bot, so this is the union over every pool ON DISK — not just
    the rostered ones. A bot whose reactions were switched off (or that was
    deleted outright) still has traces in the chat, and those must keep
    resolving for the devices allowed to see them.
    """
    global _safe_ids_cache
    key = _safe_ids_key()
    if _safe_ids_cache is not None and _safe_ids_cache[0] == key:
        return set(_safe_ids_cache[1])

    ids = {r.id for r in load().reactions if r.safe}

    def _add(mood: str, p: Path) -> None:
        ids.add(pool_file_id(mood, p.name))
        # Legacy chat traces name the blob's stem ("pool-<hex>") — keep those
        # fetchable too. See _resolve_pool_file.
        ids.add(f"{POOL_ID_PREFIX}{p.stem}")

    safe_pool = False
    for moods_base in _all_moods_roots():
        try:
            st = pool_load(_bot_id_from_moods_dir(moods_base))
        except Exception:                  # a junk pool file is not a leak
            continue
        if not st.config.safe:
            continue
        safe_pool = True
        for mood, d in _mood_dirs(moods_base):
            for p in _mood_files(d):
                _add(mood, p)

    # spent/ is SHARED (blob names are uuids, so they never collide), and the
    # bot a spent blob came from is no longer recorded — it moved out of the
    # only directory that said so. One safe pool therefore makes the spent
    # store visible, which is the conservative reading of "was this safe":
    # only reachable at all if some pool is marked safe, and every blob in
    # there was already broadcast to those same clients when it fired.
    if safe_pool:
        for mood, d in _mood_dirs(config.REACTIONS_SPENT_DIR):
            for p in _mood_files(d):
                _add(mood, p)
    _safe_ids_cache = (key, set(ids))
    return ids


def reaction_bots() -> list[str]:
    """The rostered bots with reactions enabled, in roster order.

    This is the set the refill cycle generates for and the manager lists — the
    bots that are *supposed* to have a pool. Compare :func:`_all_moods_roots`,
    which is what actually exists on disk.
    """
    try:
        return [b.id for b in config.load_bots()
                if b.reactions and BOT_ID_RE.match(b.id or "")]
    except Exception:                      # unreadable roster — never fatal
        return []


def _all_moods_roots() -> list[Path]:
    """Every pool directory present on disk, plus a not-yet-migrated legacy one.

    Filesystem, not roster: a pool that outlived its bot still owns images the
    chat's traces point at.
    """
    roots: list[Path] = []
    reactions_dir = config.DATA_DIR / "reactions"
    try:
        entries = sorted(reactions_dir.iterdir())
    except OSError:
        entries = []
    for d in entries:
        if not d.name.startswith(_MOODS_PREFIX):
            continue
        if not BOT_ID_RE.match(d.name[len(_MOODS_PREFIX):]):
            continue                       # not one of ours — leave it alone
        if d.is_dir() and not d.is_symlink():
            roots.append(d)
    legacy = _legacy_moods_dir()
    if legacy.is_dir() and not legacy.is_symlink():
        roots.append(legacy)
    return roots


def _bot_id_from_moods_dir(moods_dir: Path) -> str:
    """Which bot a mood directory belongs to.

    A legacy ``moods/`` belongs to whoever the migration is about to hand it
    to — the default reaction bot — so its config is read from the same file
    it will use afterwards.
    """
    name = moods_dir.name
    if name.startswith(_MOODS_PREFIX):
        return resolve_bot_id(name[len(_MOODS_PREFIX):])
    return default_bot_id()


def list_for(*, decoy: bool, bot_id: str | None = None) -> list[dict]:
    """The pack plus the ready pool, as one list the picker can render.

    ``bot_id`` scopes the pool half to one bot; without it every
    reactions-enabled bot's pool is listed (the manager's view). The pack half
    is shared and always the same.
    """
    pack = load()
    rs = [r for r in pack.reactions if r.safe] if decoy else list(pack.reactions)
    out = [r.to_dict(settings=pack.settings) for r in rs]

    for bid in ([resolve_bot_id(bot_id)] if bot_id else reaction_bots()):
        st = pool_load(bid)
        if st.config.enabled and (st.config.safe or not decoy):
            for mood, p in _iter_ready(bid):
                d = _file_reaction(mood, p, st.config,
                                   bot_id=bid).to_dict(settings=pack.settings)
                d["pool"] = True        # one-shot: the UI marks it, and it vanishes on use
                out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Per-actor cooldown + burst window + a global ceiling.

    In-memory and best-effort by design (resets on restart) — it exists to stop
    a looping agent from strobing the family's screens, not to be an audit
    control. ``check`` is the only mutation point: it records the fire it
    permits, so callers must not call it speculatively.
    """

    def __init__(self) -> None:
        self._recent: dict[str, list[float]] = {}
        self._global: list[float] = []

    def _prune(self, now: float, window_s: float) -> None:
        cutoff = now - window_s
        for actor in list(self._recent):
            kept = [t for t in self._recent[actor] if t >= cutoff]
            if kept:
                self._recent[actor] = kept
            else:
                del self._recent[actor]
        self._global = [t for t in self._global if t >= now - 60.0]

    def check(self, actor: str, st: Settings) -> str | None:
        """Return an error string to refuse, or None (and record the fire)."""
        now = time.monotonic()
        window_s = st.burst_window_ms / 1000.0
        self._prune(now, window_s)

        if len(self._global) >= st.global_max_per_min:
            return "Too many reactions right now — give it a minute."
        hits = self._recent.get(actor, [])
        if hits and (now - hits[-1]) * 1000 < st.cooldown_ms:
            wait = (st.cooldown_ms / 1000.0) - (now - hits[-1])
            return f"Slow down — another reaction in {wait:.1f}s."
        if len(hits) >= st.burst_max:
            return "Reaction burst limit reached — wait a moment."

        self._recent.setdefault(actor, []).append(now)
        self._global.append(now)
        return None

    def refund(self, actor: str) -> None:
        """Undo the most recent permitted fire for an actor.

        Used when a permitted fire then loses a pool-consume race (409): the
        actor wasn't at fault, so they shouldn't be taxed for it. Best-effort —
        a refund for an actor with no recorded fires is a no-op.
        """
        if self._recent.get(actor):
            self._recent[actor].pop()
            if not self._recent[actor]:
                del self._recent[actor]
        if self._global:
            self._global.pop()

    def reset(self) -> None:
        self._recent.clear()
        self._global.clear()


limiter = RateLimiter()


# Recent fire failures, newest last — a small in-memory window so /api/health
# and the fleet review can see refusals without grepping the journal. Best
# effort by design (resets on restart), like the rate limiter above.
_FIRE_FAILURES: deque = deque(maxlen=200)


def note_fire_failure(key: str, reason: str, *, actor: str = "") -> None:
    _FIRE_FAILURES.append({"at": time.time(), "key": str(key)[:60],
                           "reason": str(reason)[:200], "actor": str(actor)[:60]})


def fire_failure_stats(window_s: float = 24 * 3600.0) -> dict:
    cutoff = time.time() - window_s
    recent = [f for f in _FIRE_FAILURES if f["at"] >= cutoff]
    return {"failures_24h": len(recent), "recent": recent[-10:]}


# --------------------------------------------------------------------------- #
# Inline markers — how an agent fires a reaction from inside a reply
# --------------------------------------------------------------------------- #

# `:react:facepalm:` anywhere in an assistant message. Deliberately narrow: the
# id charset is restricted and both colons are required, so ordinary prose
# ("ratio 1:2:1") can't trip it. Case-insensitive on the tag, ids are lowercased.
_MARKER_RE = re.compile(r":react:([a-z0-9][a-z0-9_-]{0,47}):", re.IGNORECASE)
MAX_MARKERS_PER_MESSAGE = 2


# Code regions: shared with the media directives, which are quoted in prose for
# exactly the same reasons and were corrupted for want of this rule.
_CODE_RE = openclaw_text.CODE_SPAN_RE


def strip_markers(content: str) -> str:
    """The marker-free text, without touching the registry.

    Byte-identical to ``extract_markers(content)[0]`` — removal never depends on
    whether an id resolves (an unknown marker is stripped too) — but it performs
    no lookups, so it is safe to call from hot, side-effect-free paths. Dedup
    needs exactly this: a canonical key must mirror the marker strip that
    persisting applies, and computing a key must not read the reaction pack.
    """
    return extract_markers(content, resolve=False)[0]


def extract_markers(content: str, *, bot_id: str | None = None,
                    resolve: bool = True) -> tuple[str, list[str]]:
    """Strip ``:react:<id>:`` markers out of text.

    Returns ``(clean_text, ids)``. Unknown ids are stripped too — a bot that
    hallucinates a reaction name should not leave marker syntax in the chat.
    Only the first :data:`MAX_MARKERS_PER_MESSAGE` resolvable ids are returned.

    ``bot_id`` scopes pool draws to that bot's pool. ``resolve=False`` skips the
    registry entirely and returns no ids — see :func:`strip_markers`.

    CODE IS SKIPPED. A marker inside a fenced block or an inline code span is
    someone TALKING ABOUT the syntax, not using it. Without this, an assistant
    writing "the `:react:check_in:` is a reaction marker, not an attachment"
    had the marker cut out of its own sentence — leaving an empty `` and prose
    that reads as nonsense — AND fired the reaction for real, popping a picture
    on every device in the house and permanently spending a one-shot pool
    image, purely for describing the feature. Reported from live use.

    The pre-existing narrowness (both colons, restricted charset) stops prose
    like "1:2:1"; it cannot stop quotation, because a quoted marker is
    byte-identical to a real one. Only position distinguishes them.
    """
    if not content or ":react:" not in content.lower():
        return content, []
    found: list[str] = []

    def _sub(m: re.Match) -> str:
        if not resolve:
            return ""
        r = get(m.group(1), bot_id=bot_id)
        if r is not None and r.id not in found and len(found) < MAX_MARKERS_PER_MESSAGE:
            found.append(r.id)
        return ""

    # Substitute only OUTSIDE code regions, walking the string once so the
    # code spans are copied through byte-for-byte.
    def _clean(segment: str) -> str:
        """Substitute, then tidy the hole the removal leaves — PER SEGMENT.

        The tidy used to run once over the whole joined string, which undid the
        byte-for-byte preservation immediately above it: `[ \t]{2,}` collapsed
        the indentation of every fenced block in the message, so a reply that
        fired a reaction AND contained Python emitted code that no longer
        parsed. Preserving code in the walk and then reformatting it in the
        cleanup is worse than not preserving it at all, because it looks
        deliberate.
        """
        segment = _MARKER_RE.sub(_sub, segment)
        segment = re.sub(r"[ \t]{2,}", " ", segment)
        return re.sub(r"\n{3,}", "\n\n", segment)

    out: list[str] = []
    pos = 0
    for m in _CODE_RE.finditer(content):
        out.append(_clean(content[pos:m.start()]))
        out.append(m.group(0))          # verbatim: this is quoted text
        pos = m.end()
    out.append(_clean(content[pos:]))
    cleaned = "".join(out)
    return cleaned.strip(), found


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #


def _unique_id(base: str, pack: Pack) -> str:
    rid = base
    n = 2
    taken = {r.id for r in pack.reactions} | {a for r in pack.reactions for a in r.aliases}
    while rid in taken:
        if n > 50:
            # Pathological registry (50 collisions on one base) — stop counting
            # and go straight to unique. 39 + "-" + 8 hex stays inside 48.
            return f"{base[:39]}-{uuid.uuid4().hex[:8]}".strip("-")
        # Truncate the BASE, never the suffix: ids are capped at 48 chars, and
        # a blind f"{base}-{n}"[:48] collapses back to `base` for a 47/48-char
        # base — an infinite loop on the event loop, reachable from an upload.
        suffix = f"-{n}"
        rid = (base[:48 - len(suffix)] + suffix).strip("-")
        n += 1
    return rid


def add(*, name: str, image_bytes: bytes | None = None, src_path: Path | None = None,
        suffix: str = ".png", aliases: Iterable[str] | None = None,
        category: str = "general", safe: bool = False,
        duration_ms: int | None = None, source: str = "upload",
        prompt: str = "") -> Reaction:
    """Register a new reaction from bytes or an on-disk file."""
    if (image_bytes is None) == (src_path is None):
        raise ReactionError("add() needs exactly one of image_bytes / src_path")
    suffix = (suffix or ".png").lower()
    if suffix not in IMAGE_EXTS:
        raise ReactionError(f"Unsupported image type: {suffix}")

    pack = load()
    if len(pack.reactions) >= MAX_REACTIONS:
        raise ReactionError(f"Reaction pack is full (max {MAX_REACTIONS})", 507)

    display = (str(name or "").strip() or "Reaction")[:60]
    rid = _unique_id(_norm_id(display), pack)
    stored = f"{rid}-{uuid.uuid4().hex[:8]}{suffix}"
    dest_dir = config.REACTIONS_PACK_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    part = dest.with_name(dest.name + ".part")
    try:
        if image_bytes is not None:
            if len(image_bytes) > UPLOAD_MAX:
                raise ReactionError(
                    f"Image too large (max {UPLOAD_MAX // (1024 * 1024)}MB)", 413)
            part.write_bytes(image_bytes)
        else:
            if src_path.stat().st_size > UPLOAD_MAX:
                raise ReactionError(
                    f"Image too large (max {UPLOAD_MAX // (1024 * 1024)}MB)", 413)
            shutil.copyfile(src_path, part)
        os.replace(part, dest)          # atomic publish
    except ReactionError:
        part.unlink(missing_ok=True)
        raise
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ReactionError(f"Could not store the image: {e}", 507)

    r = Reaction(
        id=rid, name=display, file=f"pack/{stored}",
        aliases=_norm_aliases(aliases), category=str(category or "general")[:40],
        safe=bool(safe),
        duration_ms=int(duration_ms) if duration_ms else None,
        source=source, prompt=str(prompt or "")[:500],
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    pack.reactions.append(r)
    save(pack)
    return r


def update(rid: str, **fields) -> Reaction:
    pack = load()
    r = pack.by_id(rid)
    if r is None:
        raise ReactionError(f"No such reaction: {rid}", 404)
    if "name" in fields and fields["name"] is not None:
        r.name = str(fields["name"]).strip()[:60] or r.name
    if "aliases" in fields and fields["aliases"] is not None:
        r.aliases = [a for a in _norm_aliases(fields["aliases"]) if a != r.id]
    if "category" in fields and fields["category"] is not None:
        r.category = str(fields["category"]).strip()[:40] or "general"
    if "safe" in fields and fields["safe"] is not None:
        r.safe = bool(fields["safe"])
    if "duration_ms" in fields:
        d = fields["duration_ms"]
        r.duration_ms = int(d) if d else None
    save(pack)
    return r


def remove(rid: str) -> None:
    """Deregister a reaction and delete its blob (pack images only)."""
    pack = load()
    r = pack.by_id(rid)
    if r is None:
        raise ReactionError(f"No such reaction: {rid}", 404)
    pack.reactions = [x for x in pack.reactions if x.id != rid]
    save(pack)
    # Only ever unlink inside the pack root, and only once the registry no
    # longer references the blob (another entry may share a file after a
    # hand-edit).
    still_used = any(x.file == r.file for x in pack.reactions)
    if not still_used and r.file.startswith("pack/"):
        try:
            p = (config.REACTIONS_PACK_DIR / r.file.split("/", 1)[1]).resolve()
            base = config.REACTIONS_PACK_DIR.resolve()
            if base in p.parents and p.is_file():
                p.unlink()
        except (OSError, ReactionError):
            log.warning("could not delete reaction blob for %s", rid)


def _merge_validated(cur: dict, values: dict, label: str) -> dict:
    """Overlay caller-supplied fields onto a settings dict, STRICTLY.

    The two persist paths (:func:`update_settings`, :func:`pool_update_config`)
    run this BEFORE anything reaches disk. The parse layer deliberately
    degrades junk to defaults so a hand-edited file can never brick ``load()``;
    an API caller is the opposite case — they must be TOLD their value was bad
    (400), and the file must stay exactly as it was.
    """
    for k, v in (values or {}).items():
        if k not in cur or v is None:
            continue
        if isinstance(cur[k], bool):            # bool first — bool is an int
            cur[k] = bool(v)
        elif isinstance(cur[k], int):
            try:
                cur[k] = int(v)
            except (TypeError, ValueError):
                raise ReactionError(f"{label}.{k} must be a number, not {v!r}")
        else:
            cur[k] = str(v)
    return cur


def update_settings(values: dict) -> Settings:
    pack = load()
    cur = _merge_validated(pack.settings.to_dict(), values, "settings")
    # Only now — every value validated and clamped — does anything reach disk.
    pack.settings = _clamp_settings(Settings(**cur))
    return save(pack).settings


# --------------------------------------------------------------------------- #
# Image generation
# --------------------------------------------------------------------------- #


#: ``last_error``/status values for the two ways generation can be impossible.
#: They are deliberately DISTINCT: "nobody configured this" and "the thing that
#: was configured is gone" need different humans to do different things, and
#: collapsing them into one flag is what made a missing DISPATCH_IMAGE_CLI read
#: as "the image host is unreachable" for two days.
IMAGE_CLI_OK = "ok"
IMAGE_CLI_UNSET = "unset"
IMAGE_CLI_MISSING = "missing"


def image_cli_state() -> str:
    """Why generation is (or is not) possible, as one of the ``IMAGE_CLI_*``
    values above.

    ``unset``   nothing is configured -- ``DISPATCH_IMAGE_CLI`` is empty. This
                is an INSTALL gap, not a host outage: the image host may be
                perfectly healthy and we would still never call it.
    ``missing`` something is configured, but it does not resolve to a runnable
                file (typo, uninstalled companion CLI, lost +x).
    ``ok``      configured and executable.
    """
    # str() because tests (and callers) may inject a Path here.
    name = str(_IMAGE_CLI or "").strip()
    if not name:
        return IMAGE_CLI_UNSET
    resolved = name if os.sep in name else shutil.which(name)
    if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return IMAGE_CLI_OK
    return IMAGE_CLI_MISSING


def image_cli_available() -> bool:
    """True only when an image CLI is configured AND executable. Everything that
    generates checks this first, so an unconfigured install degrades to 'no
    generation' instead of raising on a missing binary."""
    return image_cli_state() == IMAGE_CLI_OK


def image_cli_error() -> str:
    """The ``last_error`` a pool should record when it cannot generate.

    Never called on the healthy path, so it always names a real problem.
    """
    state = image_cli_state()
    if state == IMAGE_CLI_UNSET:
        return "image-cli-unset"
    if state == IMAGE_CLI_MISSING:
        return f"image-cli-missing ({str(_IMAGE_CLI or '').strip()[:120]})"
    # Reached only if the CLI came back between the check and this call.
    return "image-cli-unavailable"


#: Every ``last_error`` this module can set for a CLI problem. Refill paths use
#: it to decide whether an "it produced nothing" error would overwrite a more
#: specific cause that is already recorded.
IMAGE_CLI_ERRORS = ("image-cli-unavailable", "image-cli-unset", "image-cli-missing")


def _is_image_cli_error(err: str | None) -> bool:
    return str(err or "").startswith(IMAGE_CLI_ERRORS)


def note_image_cli_unavailable(current_error: str | None) -> str:
    """Log -- LOUDLY, and once per transition -- that a pool cannot generate,
    and return the ``last_error`` to store.

    A pool that cannot generate is a broken pool, not a quiet no-op: it stops
    refilling and runs dry. Before this it returned 0 in silence, so the first
    sign of trouble was a dry pool days later. The log is an ERROR because the
    fix is always a human editing configuration.

    ``current_error`` is what the pool has recorded now; the message is only
    emitted when the state CHANGES, so a 300s poll loop cannot spam the log.
    """
    err = image_cli_error()
    if err != (current_error or ""):
        log.error(
            "image generation is UNAVAILABLE (%s): DISPATCH_IMAGE_CLI=%r. "
            "Pools cannot refill and will run dry. This is a configuration "
            "problem on this install, NOT an image-host outage -- set "
            "DISPATCH_IMAGE_CLI to an executable image CLI and restart.",
            err, str(_IMAGE_CLI or "").strip(),
        )
    return err


def _files_in(result: dict, out_dir: Path) -> list[Path]:
    """The generated files, constrained to the directory we asked for.

    The CLI's JSON names the files it wrote. We publish those paths and then
    UNLINK them, so an answer naming ``/etc/hosts`` or someone else's image
    would make us copy and then delete a file we never asked about. Only paths
    that resolve inside ``out_dir`` are kept; anything else is ignored and
    left alone.
    """
    out = out_dir.resolve()
    keep: list[Path] = []
    for raw in (result.get("files") or []):
        try:
            f = Path(raw).resolve()
        except (OSError, ValueError, TypeError):
            continue
        if out not in f.parents:
            log.warning("ignoring a generated file outside %s: %s", out, raw)
            continue
        if f.is_file():
            keep.append(f)
    return keep


def generate(prompt: str, *, name: str = "", style: str = "", workflow: str = "",
             safe: bool = False, category: str = "generated") -> Reaction:
    """Mint a new reaction image on the configured image host via the image CLI.

    Fixed argv, never a shell. The CLI prints one JSON object on stdout and
    writes the PNG into ``--output``; we copy it into the pack and register it.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ReactionError("A prompt is required")
    if len(prompt) > 1000:
        raise ReactionError("Prompt is too long (max 1000 chars)")
    _reject_dash_lead(prompt, "Prompt")
    if not image_cli_available():
        raise ReactionError(
            "No image CLI configured — generation is unavailable on this box", 503)

    out_dir = config.REACTIONS_DIR / ".generate"
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(_IMAGE_CLI), "--count", "1",
            "--ratio", "1:1", "--output", str(out_dir)]
    if style:
        _reject_dash_lead(style, "Style")
        argv += ["--style", str(style)[:60]]
    if workflow:
        _reject_dash_lead(workflow, "Workflow")
        argv += ["--workflow", str(workflow)[:60]]
    # `--` ends option parsing: the prompt is a positional, never a flag.
    argv += ["--", prompt]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=IMAGE_CLI_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise ReactionError("Image CLI timed out — the host may be cold or busy", 504)
    except OSError as e:
        raise ReactionError(f"Could not run the image CLI: {e}", 503)

    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise ReactionError(f"Image CLI returned no usable result. {detail}".strip(), 502)
    if result.get("status") != "ok":
        raise ReactionError(
            f"Image CLI: {result.get('message') or 'generation failed'}"[:300], 502)

    files = _files_in(result, out_dir)
    if not files:
        raise ReactionError("Image CLI returned no image", 502)

    src = files[0]
    try:
        return add(name=(name or prompt)[:60], src_path=src,
                   suffix=src.suffix.lower() or ".png",
                   category=category, safe=safe, source="generated", prompt=prompt)
    finally:
        for f in files:
            _unlink_quiet(f)


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Starter pack
# --------------------------------------------------------------------------- #

# (id, glyph, caption, hue, display name, aliases) — rendered as flat pictogram
# cards so the pack reads as one designed set rather than a folder of scraped
# memes. Glyphs come from NotoEmoji-Regular (monochrome outlines, so they scale
# cleanly); the COLOUR emoji font is a fixed-strike bitmap face that blurs when
# scaled up, which is why it is deliberately not used. Every glyph here was
# eyeballed at render size — several emoji (🤦, 🤷) collapse into an unreadable
# blob in the mono face, so legible stand-ins were chosen instead.
_STARTER = [
    ("facepalm",  "\U0001F62B", "FACEPALM",   265, "Facepalm",   ["smh", "ugh"]),
    ("nice",      "\U0001F44C", "NICE",       150, "Nice",       ["ok", "perfect"]),
    ("shipped",   "\U0001F680", "SHIPPED IT",  15, "Shipped It", ["ship", "deployed"]),
    ("oof",       "\U0001F480", "OOF",        340, "Oof",        ["dead", "rip", "yikes"]),
    ("bravo",     "\U0001F44F", "BRAVO",       45, "Bravo",      ["clap", "applause"]),
    ("thinking",  "\U0001F914", "HMMM",       200, "Hmmm",       ["hmm", "think"]),
    ("nope",      "\U0001F6AB", "NOPE",         0, "Nope",       ["no", "denied"]),
    ("mindblown", "\U0001F92F", "MIND BLOWN", 285, "Mind Blown", ["wow", "whoa"]),
    ("party",     "\U0001F389", "LET'S GO",   320, "Let's Go",   ["celebrate", "yay"]),
    ("eyes",      "\U0001F440", "SUSPICIOUS", 175, "Suspicious", ["sus", "watching"]),
    ("fire",      "\U0001F525", "FIRE",        25, "Fire",       ["hot", "banger"]),
]

# Fonts used to draw the built-in starter cards. Distributions disagree about
# where fonts live, so each list is a search path tried in order; the first
# readable file wins and Pillow's built-in bitmap font is the final fallback
# (ugly, but the seed never fails for want of a font).
#
# DISPATCH_GLYPH_FONTS / DISPATCH_TEXT_FONTS override the lists entirely — a
# ':'-separated list of paths, same shape as $PATH — for an install whose fonts
# are somewhere else, or that simply wants different ones.
_GLYPH_FONTS = [
    # Fedora / RHEL
    "/usr/share/fonts/google-noto-emoji-fonts/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/gdouros-symbola/Symbola.ttf",
    # Debian / Ubuntu
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    # Arch
    "/usr/share/fonts/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/TTF/Symbola.ttf",
    # Alpine (the container base)
    "/usr/share/fonts/noto-emoji/NotoEmoji-Regular.ttf",
    # macOS
    "/System/Library/Fonts/Apple Symbols.ttf",
]
_TEXT_FONTS = [
    # Fedora / RHEL
    "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    # Debian / Ubuntu
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    # Arch
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    # Alpine (the container base)
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _font_candidates(default: list[str], env_key: str) -> list[str]:
    """The search path for one font role, honouring its env override."""
    raw = os.environ.get(env_key, "").strip()
    if raw:
        return [p for p in raw.split(os.pathsep) if p.strip()]
    return default


def _load_font(candidates: list[str], size: int, bold: bool = False):
    from PIL import ImageFont
    for path in candidates:
        if not Path(path).is_file():
            continue
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        if bold:
            # Variable fonts (NotoSans[wght]) carry named instances; static
            # faces raise, which just means "already the weight we asked for".
            try:
                f.set_variation_by_name("Bold")
            except (OSError, AttributeError, ValueError):
                pass
        return f
    try:
        return ImageFont.load_default(size)
    except TypeError:            # Pillow < 9.2 has no size arg
        return ImageFont.load_default()


def _hsl(h: float, s: float, ll: float) -> tuple[int, int, int]:
    import colorsys
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, ll, s)
    return int(r * 255), int(g * 255), int(b * 255)


def _render_card(glyph: str, caption: str, hue: int, dest: Path, size: int = 640) -> None:
    """Draw one starter-pack card: pictogram over a soft vertical wash."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size), _hsl(hue, 0.42, 0.16))
    draw = ImageDraw.Draw(img)
    # Vertical wash — cheap gradient, one row at a time.
    top, bottom = _hsl(hue, 0.45, 0.28), _hsl(hue, 0.50, 0.10)
    for y in range(size):
        t = y / (size - 1)
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    glyph_font = _load_font(
        _font_candidates(_GLYPH_FONTS, "DISPATCH_GLYPH_FONTS"), int(size * 0.46))
    text_font = _load_font(
        _font_candidates(_TEXT_FONTS, "DISPATCH_TEXT_FONTS"),
        int(size * 0.105), bold=True)
    ink = _hsl(hue, 0.85, 0.93)

    # Pictogram, optically centred in the upper two-thirds.
    box = draw.textbbox((0, 0), glyph, font=glyph_font)
    gw, gh = box[2] - box[0], box[3] - box[1]
    draw.text((size / 2 - gw / 2 - box[0], size * 0.40 - gh / 2 - box[1]),
              glyph, font=glyph_font, fill=ink)

    # Caption, letterspaced by hand (PIL has no tracking).
    tracked = " ".join(caption)
    tbox = draw.textbbox((0, 0), tracked, font=text_font)
    tw = tbox[2] - tbox[0]
    if tw > size * 0.86:                       # fall back to untracked if wide
        tracked = caption
        tbox = draw.textbbox((0, 0), tracked, font=text_font)
        tw = tbox[2] - tbox[0]
    draw.text((size / 2 - tw / 2 - tbox[0], size * 0.775 - tbox[1]),
              tracked, font=text_font, fill=ink)

    tmp = dest.with_name(dest.name + ".part")
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, dest)


def seed_starter_pack(force: bool = False) -> int:
    """Render the built-in cards and write a registry if there isn't one.

    Idempotent: existing images and an existing registry are left alone unless
    ``force``. Returns the number of images written. Never raises — a box
    without usable fonts just gets an empty pack rather than a broken app.
    """
    config.REACTIONS_BUILTIN_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    entries: list[Reaction] = []
    for rid, glyph, caption, hue, display, aliases in _STARTER:
        dest = config.REACTIONS_BUILTIN_DIR / f"{rid}.png"
        if force or not dest.is_file():
            try:
                _render_card(glyph, caption, hue, dest)
                written += 1
            except Exception as e:                     # pragma: no cover
                log.warning("could not render starter reaction %s: %s", rid, e)
                continue
        if dest.is_file():
            # The starter cards are deliberately wholesome, so they ship
            # safe=True — Safe-Mode family devices get the feature out of the
            # box. Anything added later defaults to safe=False.
            entries.append(Reaction(
                id=rid, name=display, file=f"builtin/{rid}.png",
                aliases=_norm_aliases(aliases), category="starter", safe=True,
                source="builtin", created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            ))

    if not config.REACTIONS_PATH.exists() or force:
        pack = Pack(settings=Settings(), reactions=entries)
        if config.REACTIONS_PATH.exists() and force:
            existing = load()
            keep = [r for r in existing.reactions if not r.file.startswith("builtin/")]
            pack.reactions = entries + keep
            pack.settings = existing.settings
        try:
            save(pack)
        except (ReactionError, OSError) as e:          # pragma: no cover
            log.error("could not write reactions.yaml: %s", e)
    return written


# --------------------------------------------------------------------------- #
# The rotating pool — one folder per mood
# --------------------------------------------------------------------------- #
#
# The curated pack above is permanent: named reactions you fire by id, over and
# over. The POOL is the opposite — a bank of freshly-generated images kept on
# hand, each of which fires exactly ONCE and is then gone:
#
#   * one folder per mood — moods/<mood>/ holds that reaction's ready images,
#                      and the FILESYSTEM is the manifest: hand-drop a picture
#                      into moods/bravo/ and it is immediately drawable. A
#                      hand-made folder with no prompt-bank entry is a valid
#                      manual-only mood — drawable and counted, but never
#                      auto-refilled (deficits only cover the prompt bank).
#   * kept on hand   — `per_mood` images sit ready for each prompt-bank mood,
#                      so a fire never waits on the rig
#   * never reused   — firing moves the blob into spent/<mood>/ with a single
#                      atomic os.replace. That rename IS the one-shot lock:
#                      two racing fires aim at the same source path, exactly
#                      one replace can win, and the loser redraws or falls
#                      back to the pack card.
#   * refilled nightly — at `refresh_hour` every prompt-bank mood is topped
#                      back up to its target (top-up, not discard: one-shot
#                      already guarantees a picture can never repeat)
#   * low-water top-up — a mood dropping below `min_per_mood` refills right
#                      away instead of waiting for tonight
#
# A consumed image is KEPT in spent/<mood>/ forever: the trace row a fire
# leaves in the chat can pop the picture back up any time after, so a fired
# one-shot is chat history now, not a temp file. "One-shot" still holds where
# it matters — a spent id never resolves for FIRING again. With no manifest
# there are no ghosts or orphans to reconcile; the only wreckage left is a
# `*.part` stranded by a crash mid-generate, which pool_sweep clears once old.
#
# Ids map to files exactly, both ways: ``pool-<mood>-<stem-slug>`` ↔
# ``moods/<mood>/<stem>.<ext>``. Resolution recomputes the id per candidate
# file rather than parsing the id apart, so hand-dropped names with spaces or
# unicode still round-trip. Legacy pre-folder ids ("pool-<hex>", still named
# by old chat traces) stay resolvable for DISPLAY via a stem scan.
#
# reaction-pool.yaml carries only config + batch_date (+ last_error) — no
# per-item state left to drift out of sync with the folders. Everything here
# degrades gracefully: with the image host unreachable the pool just runs dry, and
# `pool_draw()` returns None so callers fall back to the pack.


POOL_ID_PREFIX = "pool-"
# Staging-file staleness. The mechanics (and the value's rationale) live in
# pool_common; the alias keeps this module's callers and tests unchanged.
PART_STALE_S = pool_common.PART_STALE_S


@dataclass
class PoolConfig:
    bot_id: str = ""                 # which bot this pool owns (set on load)
    enabled: bool = True
    per_mood: int = 20               # unused images to keep on hand PER MOOD
    min_per_mood: int = 5            # a mood dipping below this refills right away
    refresh_hour: int = 4            # local hour of the nightly top-up
    max_per_cycle: int = 6           # generation is slow — cap one refill run
    safe: bool = False               # Safe-Mode visibility for generated images
    style: str = ""                  # image CLI style preset
    workflow: str = ""               # image CLI workflow override

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# The prompt bank — what the pool generates
# --------------------------------------------------------------------------- #
#
# Deliberately DATA, not code: `<data>/reaction-prompts.yaml` is the one file to
# edit to change how reaction images look, and it is reachable over the API so
# an agent can read and rewrite it without a deploy. Shape:
#
#     base: "<character/style fragment prefixed to every prompt>"
#     suffix: "<fragment appended to every prompt>"      # optional
#     workflow / ratio / style: image CLI knobs
#     categories:
#       <id>:
#         label: Morning                # what the overlay calls it
#         expressions: [...]            # substituted into {expr}
#         prompts: [...]                # one is picked per generation
#
# A generated prompt is `base + ", " + prompt.format(expr=...) + suffix`. The
# category id doubles as a draw key, so `:react:mad:` pulls a fresh unused image
# from the "mad" mood.

DEFAULT_BANK: dict = {
    "version": 1,
    "base": "",
    "suffix": "bold flat vector illustration, thick outlines, single subject, plain background",
    "workflow": "",
    "ratio": "1:1",
    "style": "",
    "categories": {},
}

# Fallback bank, used only when no reaction-prompts.yaml exists yet: neutral
# cartoon moods that need no character definition.
_SEED_CATEGORIES: list[tuple[str, str, str]] = [
    ("facepalm",   "Facepalm",   "a cartoon character covering their face with one hand in exasperation"),
    ("applause",   "Applause",   "a cartoon character clapping enthusiastically, motion lines"),
    ("mindblown",  "Mind Blown", "a cartoon character with an exploding head, comic sparks"),
    ("suspicious", "Suspicious", "a cartoon character narrowing their eyes suspiciously at the viewer"),
    ("celebrate",  "Celebrate",  "a cartoon character throwing confetti in the air, party popper"),
    ("shipped",    "Shipped",    "a cartoon rocket launching triumphantly off a laptop keyboard"),
    ("oof",        "Oof",        "a cartoon character wincing dramatically, comic impact star"),
    ("nice",       "Nice",       "a cartoon character giving a crisp thumbs up with a wink"),
    ("thinking",   "Thinking",   "a cartoon character stroking their chin deep in thought, thought bubble"),
    ("nope",       "Nope",       "a cartoon character crossing their arms in a firm refusal"),
    ("panic",      "Panic",      "a cartoon character running in circles with arms raised, motion blur"),
    ("smug",       "Smug",       "a cartoon character smirking with steepled fingers"),
    ("exhausted",  "Exhausted",  "a cartoon character slumped face-down on a desk beside a cold coffee"),
    ("popcorn",    "Popcorn",    "a cartoon character eating popcorn while watching intently"),
    ("confused",   "Confused",   "a cartoon character surrounded by floating question marks"),
    ("victory",    "Victory",    "a cartoon character leaping with a triumphant fist pump"),
]


# Per-bot caches: {bot_id: (data, mtime)}
_bank_cache: dict[str, tuple[dict, float | None]] = {}


def _default_bank() -> dict:
    b = dict(DEFAULT_BANK)
    b["categories"] = {
        cid: {"label": label, "expressions": [], "prompts": [prompt]}
        for cid, label, prompt in _SEED_CATEGORIES
    }
    return b


# Anything handed to the image CLI as a positional argument or an option
# VALUE is rejected when it starts with a dash. The argv is fixed and a `--`
# separator precedes the prompt, so this is the second lock, not the only one:
# it keeps a hostile bank entry from ever reaching the process table, and it
# fails loudly at the write instead of silently at the next nightly refill.
def _reject_dash_lead(value: str, label: str) -> None:
    if str(value).strip().startswith("-"):
        raise ReactionError(
            f"{label} may not start with '-' — it would be read as a command-line"
            " option by the image CLI", 400)


def _clean_bank(raw: dict) -> dict:
    """Normalise a hand- or agent-edited bank. Never raises on junk — a bad
    category is dropped, not fatal, so one typo can't stop the pool refilling."""
    out = {
        "version": 1,
        "base": str(raw.get("base") or "")[:600],
        "suffix": str(raw.get("suffix") or "")[:300],
        "workflow": str(raw.get("workflow") or "")[:60],
        "ratio": str(raw.get("ratio") or "1:1")[:12],
        "style": str(raw.get("style") or "")[:60],
        "categories": {},
    }
    cats = raw.get("categories")
    if not isinstance(cats, dict):
        cats = {}
    for cid, spec in list(cats.items())[:60]:
        if not isinstance(spec, dict):
            continue
        try:
            key = _norm_id(cid)
        except ReactionError:
            log.warning("reaction-prompts.yaml: dropping bad category id %r", cid)
            continue
        prompts = [str(x)[:600] for x in (spec.get("prompts") or []) if str(x).strip()][:40]
        if not prompts:
            continue
        out["categories"][key] = {
            "label": str(spec.get("label") or key.replace("_", " ").title())[:60],
            "expressions": [str(x)[:200] for x in (spec.get("expressions") or [])][:40],
            "prompts": prompts,
        }
    return out


_EMPTY_BANK = {"base": "", "suffix": "", "categories": {}}


def bank_load(bot_id: str | None = None) -> dict:
    """The prompt bank for one bot (mtime-cached).

    The DEFAULT reaction bot gets the shipped starter bank materialised on
    first use, so a fresh install has something to generate the moment the
    capability is switched on. Every OTHER bot starts EMPTY — no file means no
    pool — because a second bot's images are a deliberate choice: seeding it
    would silently double the nightly generation and give two characters the
    same prompts. Write its bank (PUT /api/reactions/prompts?bot_id=…) and the
    pool starts filling.

    A bank is never read from the pre-per-bot ``reaction-prompts.yaml``: only
    :func:`_ensure_migration` touches that name, and only to RENAME it when the
    per-bot file is absent. A leftover generic file can therefore never shadow
    a bot's own bank.
    """
    global _bank_cache
    _ensure_migration()
    bot_id = resolve_bot_id(bot_id)
    path = bank_path(bot_id)

    if not path.exists():
        if bot_id == default_bot_id():
            bank_save(_default_bank(), bot_id=bot_id)
        else:
            _bank_cache[bot_id] = (_clean_bank(dict(_EMPTY_BANK)), None)
            return _bank_cache[bot_id][0]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if bot_id in _bank_cache and _bank_cache[bot_id][1] == mtime:
        return _bank_cache[bot_id][0]

    # A hand-edited bank that no longer parses must not take the feature down:
    # the default bot falls back to the shipped bank, a companion to an empty
    # one (which reads as "nothing to generate", not as another bot's moods).
    fallback = _default_bank() if bot_id == default_bot_id() else dict(_EMPTY_BANK)
    raw: dict = {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.error("%s unreadable (%s) — using defaults", path.name, e)
        raw = fallback
    if not isinstance(raw, dict):
        raw = fallback

    _bank_cache[bot_id] = (_clean_bank(raw), mtime)
    return _bank_cache[bot_id][0]


def bank_save(bank: dict, *, bot_id: str | None = None) -> dict:
    """Write the bank atomically, with a header telling the next reader what it is."""
    global _bank_cache
    bot_id = require_bot_id(bot_id)
    body = _clean_bank(bank)
    if not body["categories"]:
        raise ReactionError("A prompt bank needs at least one category with prompts")
    for key in ("base", "suffix", "style", "workflow", "ratio"):
        _reject_dash_lead(body[key], f"Prompt bank {key}")
    for cid, spec in body["categories"].items():
        for text in spec["prompts"]:
            _reject_dash_lead(text, f"Prompt in mood {cid!r}")
        for expr in spec["expressions"]:
            _reject_dash_lead(expr, f"Expression in mood {cid!r}")
    header = (
        f"# Reaction image prompts for bot '{bot_id}' — what the rotating pool generates.\n"
        "# Edit freely: `base` is prefixed to every prompt, `suffix` appended, and\n"
        "# {expr} in a prompt is replaced by a random entry from that category's\n"
        "# `expressions`. A category id doubles as a draw key, so a category named\n"
        "# `mad` is fired with :react:mad:.  Changes apply to the NEXT refill.\n"
        "# Also reachable at GET/PUT /api/reactions/prompts?bot_id=<id>.\n"
    )
    path = bank_path(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True,
                                           width=100), encoding="utf-8")
    os.replace(tmp, path)
    _bank_cache.pop(bot_id, None)
    return bank_load(bot_id)


def bank_invalidate(bot_id: str | None = None) -> None:
    """Drop one bot's cached bank — or every bot's, with ``bot_id="*"``."""
    global _bank_cache
    if bot_id == "*":
        _bank_cache.clear()
        return
    _bank_cache.pop(resolve_bot_id(bot_id), None)


def bank_categories(bot_id: str | None = None) -> list[str]:
    return list(bank_load(bot_id)["categories"])


def compose_prompt(category: str, *, bot_id: str | None = None) -> tuple[str, str]:
    """Build one generation prompt for a mood. Returns (prompt, label)."""
    import random
    bank = bank_load(bot_id)
    spec = bank["categories"].get(category)
    if spec is None:
        raise ReactionError(f"No such reaction mood: {category}", 404)
    template = random.choice(spec["prompts"])
    exprs = spec.get("expressions") or []
    if "{expr}" in template:
        template = template.replace("{expr}", random.choice(exprs) if exprs else "")
    parts = [p.strip().strip(",") for p in (bank["base"], template, bank["suffix"]) if p.strip()]
    return ", ".join(parts), spec["label"]


# --- Folder storage: the mood directories ARE the manifest ----------------- #


def _mood_dirs(base: Path) -> list[tuple[str, Path]]:
    """The mood folders under a root, as ``(mood_key, dir)`` pairs.

    The key is the folder name lowercased — draw keys and ids are lowercase,
    but a hand-made ``Bravo/`` folder should still just work. Dotted or
    otherwise path-suspect names are not moods (MOOD_DIR_RE admits no dots),
    and symlinked dirs are skipped: the serve path's containment check would
    refuse their files anyway, so refusing to draw them fails closed.
    """
    out: list[tuple[str, Path]] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []                            # root not created yet — no stock
    for d in entries:
        if not MOOD_DIR_RE.match(d.name):
            continue
        try:
            if not d.is_dir() or d.is_symlink():
                continue
        except OSError:
            continue
        out.append((d.name.lower(), d))
    return out


def _mood_files(d: Path) -> list[Path]:
    """The stock inside one mood folder: plain image files only.

    Dotfiles and non-image suffixes (which covers ``*.part`` staging) are
    invisible, so a crash mid-generate or a stray .DS_Store can never be drawn.
    """
    return pool_common.image_files(d, IMAGE_EXTS)


def _iter_ready(bot_id: str | None = None) -> list[tuple[str, Path]]:
    """Every ready pool image for one bot, as ``(mood_key, path)`` — the whole shelf."""
    return [(mood, p)
            for mood, d in _mood_dirs(_moods_dir(bot_id))
            for p in _mood_files(d)]


def _stem_slug(stem: str) -> str:
    """Fold a file stem into id-safe characters, deterministically.

    Never empty: a stem with no ASCII at all (e.g. an all-kanji hand-drop)
    falls back to a hash of the original name, so the file still has a stable
    id and the moods/<x>/<file> ↔ pool-<x>-<slug> mapping stays exact.
    """
    s = unicodedata.normalize("NFKD", str(stem)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")
    return s or hashlib.md5(str(stem).encode("utf-8", "surrogatepass")).hexdigest()[:10]


def pool_file_id(mood: str, filename: str) -> str:
    """The canonical id of a pool blob: ``pool-<mood>-<stem-slug>``.

    This is the ONLY id mint; resolution compares candidates against it rather
    than parsing the id apart (a mood may itself contain hyphens). The slug is
    truncated so the whole id fits the 48-char marker charset when the mood
    leaves room — a hand-dropped monster of a filename still gets an id, it
    just won't be addressable from a ``:react:`` marker.
    """
    slug = _stem_slug(Path(filename).stem)
    budget = 48 - len(POOL_ID_PREFIX) - len(mood) - 1
    if 0 < budget < len(slug):
        slug = slug[:budget].rstrip("-_") or slug[:budget]
    return f"{POOL_ID_PREFIX}{mood}-{slug}"


def _resolve_pool_file(rid: str, base: Path) -> tuple[str, Path] | None:
    """Find the blob a pool id names under one root (moods/ or spent/).

    Two passes: the exact folder-model mapping first, then the legacy fallback
    — pre-folder blobs were named ``pool-<hex>.<ext>`` and their ids live on in
    old chat traces, so a stem equal to the id's bare tail (or the whole id,
    for a blob still wearing its old name) also matches.
    """
    if not rid or not rid.startswith(POOL_ID_PREFIX):
        return None
    dirs = _mood_dirs(base)
    for mood, d in dirs:
        if not rid.startswith(f"{POOL_ID_PREFIX}{mood}-"):
            continue
        for p in _mood_files(d):
            if pool_file_id(mood, p.name) == rid:
                return mood, p
    bare = rid[len(POOL_ID_PREFIX):]
    for mood, d in dirs:
        for p in _mood_files(d):
            if p.stem in (bare, rid):
                return mood, p
    return None


def _mood_label(mood: str, bot_id: str | None = None) -> str:
    """What the overlay calls a mood: the prompt bank's display name when the
    mood is a bank category, a title-cased folder name otherwise."""
    spec = bank_load(bot_id)["categories"].get(mood)
    if spec:
        return spec["label"]
    return (mood.replace("_", " ").replace("-", " ").title() or "Reaction")[:60]


def _file_reaction(mood: str, path: Path, cfg: PoolConfig, *,
                   spent: bool = False, bot_id: str | None = None) -> Reaction:
    """Present a pool blob through the same shape as a pack reaction, so the
    fire chokepoint and the frontend need no special case.

    The file reference uses ``moods-<bot_id>/<mood>/<name>`` to encode which
    bot's pool the image belongs to. Fired images use ``spent/<mood>/<name>``
    (the shared spent store).
    """
    bot_id = resolve_bot_id(bot_id)
    try:
        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        created = ""
    if spent:
        rel = f"spent/{path.parent.name}/{path.name}"
    else:
        rel = f"{_MOODS_FMT.format(bot_id=bot_id)}/{path.parent.name}/{path.name}"
    return Reaction(
        id=pool_file_id(mood, path.name), name=_mood_label(mood, bot_id),
        file=rel, aliases=[],
        category=mood, safe=cfg.safe, duration_ms=None,
        source="pool", prompt="", created_at=created,
    )


@dataclass
class PoolState:
    """What reaction-pool.yaml still holds: knobs and telemetry, no items."""

    config: PoolConfig = field(default_factory=PoolConfig)
    batch_date: str = ""             # local YYYY-MM-DD of the last completed fill
    last_error: str = ""


_pool_cache: dict[str, tuple[PoolState, float | None]] = {}


def _clamp_pool_config(cfg: PoolConfig) -> PoolConfig:
    """Sanity-clamp pool config in place (and return it).

    Same contract as :func:`_clamp_settings`: reaction-pool.yaml is
    hand-editable, so a junk value falls back to the field default (via
    :func:`_int_or`) instead of making ``pool_load()`` raise.
    """
    d = PoolConfig()
    cfg.enabled = bool(cfg.enabled)
    cfg.safe = bool(cfg.safe)
    cfg.per_mood = max(0, min(100, _int_or(cfg.per_mood, d.per_mood)))
    cfg.min_per_mood = max(0, min(cfg.per_mood, _int_or(cfg.min_per_mood, d.min_per_mood)))
    cfg.refresh_hour = max(0, min(23, _int_or(cfg.refresh_hour, d.refresh_hour)))
    cfg.max_per_cycle = max(1, min(50, _int_or(cfg.max_per_cycle, d.max_per_cycle)))
    cfg.style = str(cfg.style or "")[:60]
    cfg.workflow = str(cfg.workflow or "")[:60]
    cfg.bot_id = resolve_bot_id(cfg.bot_id)
    return cfg


def _pool_parse(raw: dict, bot_id: str | None = None) -> PoolState:
    cdata = raw.get("config") or {}
    cdata = dict(cdata) if isinstance(cdata, dict) else {}
    # Pre-per-mood manifests (built 2026-08-01) sized the pool as a TOTAL
    # (`target`/`min_remaining`). The low-water number carries the same meaning
    # per mood, so migrate it; the old total target is simply dropped.
    if "min_per_mood" not in cdata and "min_remaining" in cdata:
        cdata["min_per_mood"] = cdata["min_remaining"]
    # The FILENAME owns the identity, not the body: reaction-pool-<bot>.yaml
    # copied from another bot (or hand-edited) would otherwise claim to be that
    # bot's pool and mislabel every status frame.
    cdata["bot_id"] = resolve_bot_id(bot_id)
    known = set(PoolConfig().to_dict())
    cfg = _clamp_pool_config(PoolConfig(**{k: v for k, v in cdata.items() if k in known}))
    # Pre-folder manifests also carried per-item `items:`/`spent:` lists. The
    # filesystem is the manifest now, so any such lists are simply ignored —
    # the startup migration moves their blobs into moods/<mood>/ and strips
    # them from the file.
    return PoolState(
        config=cfg,
        batch_date=str(raw.get("batch_date") or ""),
        last_error=str(raw.get("last_error") or "")[:300],
    )


def pool_load(bot_id: str | None = None) -> PoolState:
    """One bot's pool config + telemetry (mtime-cached, per bot).

    A missing file is not an error: the folders under
    ``reactions/moods-<bot_id>/`` are the stock, so a bot with no yaml simply
    runs on defaults until something needs persisting.
    """
    global _pool_cache
    _ensure_migration()
    bot_id = resolve_bot_id(bot_id)
    path = _pool_path(bot_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if bot_id in _pool_cache and _pool_cache[bot_id][1] == mtime:
        return _pool_cache[bot_id][0]
    raw: dict = {}
    if mtime is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("%s unreadable (%s) — starting empty", path.name, e)
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    st = _pool_parse(raw, bot_id)
    _pool_cache[bot_id] = (st, mtime)
    return st


def pool_save(state: PoolState, bot_id: str | None = None) -> PoolState:
    global _pool_cache
    bot_id = require_bot_id(bot_id)
    state.config.bot_id = bot_id           # the file's name is the identity
    body = {
        "version": 2,                # 2 = mood folders; no per-item lists
        "config": state.config.to_dict(),
        "batch_date": state.batch_date,
    }
    if state.last_error:
        body["last_error"] = state.last_error
    path = _pool_path(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)
    _pool_cache.pop(bot_id, None)
    return pool_load(bot_id)


def pool_get(rid: str, *, bot_id: str | None = None, include_spent: bool = False) -> Reaction | None:
    """Resolve a pool id.

    Ready blobs (moods-<bot_id>/) only by default — that is what keeps a fired
    image un-fireable. ``include_spent`` additionally resolves spent/<mood>/,
    which is what the IMAGE endpoint needs: the overlay is broadcast the
    instant the image is consumed, so every client's fetch necessarily arrives
    after it has already moved to spent/. Without this the picture 404s and
    nothing pops — and old traces would lose their pictures, since spent blobs
    are kept for good exactly so a trace click can re-open them.
    """
    if not rid or not rid.startswith(POOL_ID_PREFIX):
        return None
    st = pool_load(bot_id)
    hit = _resolve_pool_file(rid, _moods_dir(bot_id))
    if hit is not None:
        return _file_reaction(hit[0], hit[1], st.config, bot_id=bot_id)
    if include_spent:
        hit = _resolve_pool_file(rid, config.REACTIONS_SPENT_DIR)
        if hit is not None:
            return _file_reaction(hit[0], hit[1], st.config, spent=True, bot_id=bot_id)
    return None


def get_for_display(key: str, *, bot_id: str | None = None) -> Reaction | None:
    """Resolve for SERVING an image, not for firing.

    Same as :func:`get` but also finds a fired pool image — spent entries stay
    servable for good so the chat's reaction traces can pop them back up. Never
    use this to decide whether something may be fired — one-shot depends on
    `get` refusing a spent id.

    Pool ids do not carry their owner, and the client asking for the picture is
    a browser rendering a trace, not the bot that fired it — so a miss in the
    named bot's pool falls through to EVERY pool on disk (spent/ is shared, so
    that half is covered by the first lookup). Serving is not firing: the
    one-shot lock lives in :func:`pool_consume`, which only ever looks in the
    firing bot's own folder.
    """
    if key and key.startswith(POOL_ID_PREFIX):
        bot_id = resolve_bot_id(bot_id)
        r = pool_get(key, bot_id=bot_id, include_spent=True)
        if r is not None:
            return r
        for root in _all_moods_roots():
            bid = _bot_id_from_moods_dir(root)
            if bid == bot_id:
                continue
            r = pool_get(key, bot_id=bid, include_spent=False)
            if r is not None:
                return r
    return get(key, bot_id=bot_id)


def pool_draw(category: str | None = None,
              exclude: set[str] | None = None,
              *, bot_id: str | None = None) -> Reaction | None:
    """Pick a random ready image from a bot's pool, optionally from one mood.
    Does NOT consume.

    Random rather than FIFO: a batch is generated together, so drawing in order
    would march predictably through the same moods every day. `random` (no
    category) draws across ALL stocked folders, hand-made ones included.
    """
    st = pool_load(bot_id)
    if not st.config.enabled:
        return None
    ready = [(mood, p) for mood, p in _iter_ready(bot_id)
             if (not category or mood == category)
             and (not exclude or pool_file_id(mood, p.name) not in exclude)]
    if not ready:
        return None
    import random
    mood, p = random.choice(ready)
    return _file_reaction(mood, p, st.config, bot_id=bot_id)


def pool_categories(bot_id: str | None = None) -> dict[str, int]:
    """How many unused images are on hand per mood (stocked folders only)."""
    counts: dict[str, int] = {}
    for mood, d in _mood_dirs(_moods_dir(bot_id)):
        n = len(_mood_files(d))
        if n:
            counts[mood] = counts.get(mood, 0) + n
    return counts


def _retire(src: Path) -> Path | None:
    """Move one ready blob into spent/<mood>/ — the one-shot lock itself.

    The atomic-rename lock lives in pool_common.retire; what is ours is the
    destination: spent/<mood>/ mirrors the mood folder the blob came from, so
    its id survives into spent/ and the chat trace keeps resolving it for
    display.
    """
    return pool_common.retire(src, config.REACTIONS_SPENT_DIR / src.parent.name)


def pool_consume(rid: str, *, bot_id: str | None = None) -> bool:
    """Retire a fired image: out of its mood folder, into spent/<mood>/ —
    where it STAYS, because the trace row in the chat re-opens it on click.

    This is what "not reused" means mechanically — the atomic rename out of
    moods-<bot_id>/ is the lock, so the id stops resolving for FIRING the
    instant it is spent and a second fire of the same picture is impossible
    even if two clients race (the loser's rename finds no source and reports
    False). Serving is separate: get_for_display resolves spent blobs for good.
    """
    hit = _resolve_pool_file(rid, _moods_dir(bot_id))
    if hit is None:
        return False
    return _retire(hit[1]) is not None


def pool_sweep(bot_id: str | None = None, max_age_s: int = PART_STALE_S) -> int:
    """Sweep: clear ``*.part`` staging stranded in mood dirs by a crash between
    a generation's copy and its atomic rename. Returns the number removed.

    If ``bot_id`` is None, sweeps all bots' mood directories.

    That is ALL that is left to sweep — with the folders as the manifest there
    are no ghost entries or orphaned blobs to reconcile, and spent blobs are
    chat history (the trace row re-opens them), so they are never touched,
    however old. The age guard keeps this from racing an in-flight generate:
    a fresh .part is a rig mid-copy, not wreckage.
    """
    roots = [_moods_dir(bot_id)] if bot_id else _all_moods_roots()
    dirs = [d for root in roots for _mood, d in _mood_dirs(root)]
    removed = pool_common.sweep_parts(dirs, max_age_s)
    if removed:
        log.info("reaction pool: removed %d stale staging file(s)", removed)
    return removed


def pool_deficits(*, bot_id: str | None = None, only_low: bool = False) -> dict[str, int]:
    """How many images each mood is short of the per-mood target for a bot.

    ``only_low`` restricts the table to moods under the low-water mark — the
    emergency top-up path fires only for those, so a busy evening doesn't turn
    into all-day generation; everything else waits for the nightly refill.
    """
    st = pool_load(bot_id)
    have = pool_categories(bot_id)
    out: dict[str, int] = {}
    for c in bank_categories(bot_id):
        n = have.get(c, 0)
        if only_low and n >= st.config.min_per_mood:
            continue
        want = st.config.per_mood - n
        if want > 0:
            out[c] = want
    return out


def pool_status(bot_id: str | None = None) -> dict:
    """One bot's pool, as the manager and the WS frame report it."""
    bot_id = resolve_bot_id(bot_id)
    st = pool_load(bot_id)
    have = pool_categories(bot_id)
    cats = bank_categories(bot_id)
    # Bank moods are always listed (0 when dry); stocked hand-made folders are
    # appended after them — counted, though they never join the deficits table.
    moods = {c: have.get(c, 0) for c in cats}
    for mood in sorted(have):
        moods.setdefault(mood, have[mood])
    return {
        **st.config.to_dict(),
        "remaining": sum(have.values()),
        "moods": moods,
        "low_moods": [c for c in cats if moods[c] < st.config.min_per_mood],
        "deficit": sum(max(0, st.config.per_mood - moods[c]) for c in cats),
        # Historic key (spent blobs once awaited a purge); now simply how many
        # fired images are being kept in spent/<mood>/ for trace re-opens.
        "spent_pending": sum(len(_mood_files(d))
                             for _, d in _mood_dirs(config.REACTIONS_SPENT_DIR)),
        "batch_date": st.batch_date,
        # Scoped to THIS bot: passing the id is what stops a companion's status
        # from being computed against the default bot's shelf.
        "needs_refill": _pool_needs_refill(st, bot_id),
        "due_daily": _pool_daily_due(st),
        "available": image_cli_available(),
        # WHY generation is unavailable, not just THAT it is: watchdogs report
        # "unreachable image host" otherwise, which sends people to the rig
        # when the actual problem is an unset DISPATCH_IMAGE_CLI here.
        "image_cli": image_cli_state(),
        "last_error": st.last_error,
        # Where to hand-drop images (the manager shows it as a hint). Additive
        # — every pre-folder key above is unchanged.
        "dir": str(_moods_dir(bot_id)),
        "bot_id": bot_id,
    }


def _pool_needs_refill(st: PoolState, bot_id: str | None = None) -> bool:
    """True while ANY mood sits below the low-water mark."""
    if not st.config.enabled:
        return False
    have = pool_categories(bot_id)
    return any(have.get(c, 0) < st.config.min_per_mood for c in bank_categories(bot_id))


def _pool_daily_due(st: PoolState) -> bool:
    """True once tonight's refill window has opened and it hasn't completed.

    ``batch_date`` is stamped only when a nightly fill actually reaches the
    per-mood targets (see main's pool cycle), so a rig that was off at the
    refresh hour keeps the window open and retries every cycle.
    """
    return pool_common.daily_due(st.config.enabled, st.batch_date,
                                 st.config.refresh_hour)


def pool_mark_daily(bot_id: str | None = None) -> None:
    """Stamp tonight's refill as complete for one bot."""
    st = pool_load(bot_id)
    st.batch_date = time.strftime("%Y-%m-%d")
    pool_save(st, bot_id)


def pool_update_config(values: dict, bot_id: str | None = None) -> PoolConfig:
    bot_id = require_bot_id(bot_id)
    st = pool_load(bot_id)
    cur = _merge_validated(st.config.to_dict(), values, "pool")
    # Validated and clamped BEFORE the write — a junk value must 400 here, not
    # poison reaction-pool.yaml for every pool_load() after it.
    st.config = _clamp_pool_config(PoolConfig(**cur))
    st.config.bot_id = bot_id
    return pool_save(st, bot_id).config


# True when the last _pool_generate_one failure was RIG-side (refusal /
# timeout / unreadable answer) rather than a local (compose/store) problem.
# The refill loop breaks on rig-side failures — a refused round must stop
# immediately instead of burning the whole plan — and continues past local
# ones. Safe to keep as a module flag: every refill path runs under the
# shared _pool_lock in main.py, so calls never interleave.
_LAST_GEN_RIG_REFUSED = False


def _pool_generate_one(cfg: PoolConfig, category: str, *, bot_id: str | None = None) -> str | None:
    """Generate one pool image for a mood, into moods-<bot_id>/<mood>/.
    Returns the new image's id, or None on failure (never raises).

    Everything about *what* is drawn comes from the prompt bank; PoolConfig
    only overrides the image CLI knobs when it has been given explicit values.
    """
    global _LAST_GEN_RIG_REFUSED
    _LAST_GEN_RIG_REFUSED = False
    try:
        full, label = compose_prompt(category, bot_id=bot_id)
    except ReactionError as e:
        log.warning("cannot compose a prompt for %r (bot %s): %s", category, bot_id, e.message)
        return None
    bank = bank_load(bot_id)
    out_dir = config.REACTIONS_DIR / ".generate"
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(_IMAGE_CLI), "--count", "1",
            "--ratio", (bank.get("ratio") or "1:1"), "--output", str(out_dir)]
    style = cfg.style or bank.get("style") or ""
    workflow = cfg.workflow or bank.get("workflow") or ""
    if style and not style.strip().startswith("-"):
        argv += ["--style", style[:60]]
    if workflow and not workflow.strip().startswith("-"):
        argv += ["--workflow", workflow[:60]]
    # `--` ends option parsing: the composed prompt is a positional, never a flag.
    argv += ["--", full]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=IMAGE_CLI_TIMEOUT_S, check=False)
        result = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        _LAST_GEN_RIG_REFUSED = True
        pool_guard.note_refill_failure("rig-error", str(e),
                                       actor=resolve_bot_id(bot_id))
        log.warning("pool generation failed for %r (bot %s): %s", label, bot_id, e)
        return None
    if result.get("status") != "ok":
        _LAST_GEN_RIG_REFUSED = True
        pool_guard.note_refill_failure("refused", str(result.get("message"))[:200],
                                       actor=resolve_bot_id(bot_id))
        log.warning("pool generation refused for %r (bot %s): %s",
                    label, resolve_bot_id(bot_id), result.get("message"))
        return None
    files = _files_in(result, out_dir)
    if not files:
        _LAST_GEN_RIG_REFUSED = True
        pool_guard.note_refill_failure("no-files", "rig returned ok but no files",
                                       actor=resolve_bot_id(bot_id))
        return None

    src = files[0]
    suffix = src.suffix.lower() if src.suffix.lower() in IMAGE_EXTS else ".png"
    # The stem is a fresh uuid, so the derived id (pool-<mood>-<hex>) is unique
    # and short enough for a :react: marker; the folder gives it its mood.
    dest_dir = _moods_dir(bot_id) / category
    dest = dest_dir / f"{uuid.uuid4().hex[:10]}{suffix}"
    ok = pool_common.publish_copy(src, dest)   # atomic publish = now drawable
    for f in files:
        _unlink_quiet(f)
    if not ok:
        log.warning("could not store pool image for bot %s", bot_id)
        return None

    pool_guard.note_refill_success()
    log.debug("pool image generated for %r (bot %s): %s", label, bot_id, dest.name)
    return pool_file_id(category, dest.name)


def pool_refill(limit: int | None = None, *, bot_id: str | None = None,
                only_low: bool = False) -> int:
    """Generate toward the per-mood targets for one bot. BLOCKING — call in a thread.

    ``only_low`` = the emergency path: only moods under the low-water mark are
    topped up (to their full target). Generates at most `max_per_cycle` images
    per run so one call can't tie up the rig for ten minutes; the caller loops
    until the deficits are gone.
    """
    st = pool_load(bot_id)
    cfg = st.config
    if not cfg.enabled:
        return 0
    if not image_cli_available():
        # A pool that cannot generate is broken, not idle: say so in the log
        # and record WHICH way it is broken, so "unset on this install" never
        # again reads as "the image host is down".
        err = note_image_cli_unavailable(st.last_error)
        if st.last_error != err:
            st.last_error = err
            pool_save(st, bot_id)
        return 0

    if not bank_categories(bot_id):
        st.last_error = "no-prompt-categories"
        pool_save(st, bot_id)
        return 0

    deficits = pool_deficits(bot_id=bot_id, only_low=only_low)
    need = sum(deficits.values())
    # Same reset as the avatar pool: the CLI answered (so a CLI fault on record
    # is disproved) and a pool at target attempted nothing (so nothing failed).
    # Only a successful mint used to clear this, which left a full pool wearing
    # a weeks-old error that watchdogs kept reporting.
    if pool_common.stale_error(st.last_error, cli_ok=True, at_target=need <= 0,
                               cli_fault=_is_image_cli_error(st.last_error)):
        st.last_error = ""
        pool_save(st, bot_id)
    want = need
    if limit is not None:
        want = min(want, limit)
    want = min(want, cfg.max_per_cycle)
    if want <= 0:
        return 0

    import random
    # Always generate for the thinnest mood next (largest deficit first, ties
    # random), so a run cut short by the per-cycle cap still helps every mood
    # that needs it rather than filling one and starving the rest.
    plan: list[str] = []
    work = dict(deficits)
    for _ in range(want):
        live = [c for c, d in work.items() if d > 0]
        if not live:
            break
        pick = max(live, key=lambda c: (work[c], random.random()))
        plan.append(pick)
        work[pick] -= 1

    # VRAM guard: check the mint GPU BEFORE spending a single generation call.
    # If the rig is short, free it (unload non-pinned LLM models) and
    # re-check; if it is STILL short, say so loudly and skip the round — this
    # is what stops the overnight silent refusal hammer at the source.
    guard = pool_guard.free_vram_before_mint()
    if not guard["ok"]:
        st = pool_load(bot_id)
        st.last_error = f"rig-vram-short ({guard['reason']})"[:300]
        pool_save(st, bot_id)
        pool_guard.note_refill_failure("vram-short", guard["reason"],
                                       actor=resolve_bot_id(bot_id))
        log.error("pool refill for %s BLOCKED by VRAM guard: %s",
                  resolve_bot_id(bot_id), guard["reason"])
        return 0

    made = 0
    for category in plan:
        if _pool_generate_one(cfg, category, bot_id=bot_id) is None:
            # Rig-side failure (refusal/timeout) ends the round NOW — the
            # loop's backoff plus the VRAM guard handle the rig. Local
            # failures (compose/store) only skip this mood.
            if _LAST_GEN_RIG_REFUSED:
                break
            continue
        made += 1
        # The image itself is already live — its folder IS the manifest — so
        # the yaml only needs touching when the telemetry it holds changes.
        st = pool_load(bot_id)
        if st.last_error or not st.batch_date:
            st.last_error = ""
            if not st.batch_date:
                st.batch_date = time.strftime("%Y-%m-%d")
            pool_save(st, bot_id)
    if made == 0:
        st = pool_load(bot_id)
        st.last_error = "generation-failed"
        pool_save(st, bot_id)
    return made


def pool_replace_batch(bot_id: str | None = None) -> int:
    """MANUAL full swap (the manager's "Replace batch now" button): retire
    everything on hand for one bot and start regenerating — for when the
    character's look changed and the shelf is stale. The nightly path never
    does this; it only tops up what was consumed.

    Leftovers go through the same spent/<mood>/ path as a fired image — kept,
    not deleted — so a client mid-download is still covered and nothing the
    operator might want back is destroyed.
    """
    st = pool_load(bot_id)
    if not st.config.enabled:
        return 0
    for _mood, p in _iter_ready(bot_id):
        _retire(p)
    st = pool_load(bot_id)
    st.batch_date = time.strftime("%Y-%m-%d")
    pool_save(st, bot_id)
    return pool_refill(bot_id=bot_id)


# --------------------------------------------------------------------------- #
# One-shot layout migration: flat pool/ + manifest → mood folders
# --------------------------------------------------------------------------- #


MIGRATION_MARKER = ".mood-folders-migrated"


def migrate_mood_folders() -> dict:
    """Move the pre-2026-08 flat layout into mood folders. Runs at startup.

    Marker-guarded (``<reactions>/.mood-folders-migrated``) and idempotent:
    a brand-new data dir just gets its mood root and the marker; an already
    migrated dir is a no-op. The destination is the DEFAULT reaction bot's
    ``moods-<bot_id>/`` — this layout predates per-bot pools, so everything it
    finds belonged to the one pool that existed then. Blobs are only ever MOVED (``os.replace``), never
    deleted — the old manifest's category decides each blob's folder, and
    anything the manifest lost track of lands in "default" rather than being
    judged. Old blob names ("pool-<hex>.png") lose their redundant prefix so
    their folder-era ids stay marker-length; the legacy-id resolver matches
    either spelling, which is what keeps pre-migration chat traces re-opening
    their pictures. Finally the per-item lists are stripped from
    reaction-pool.yaml (config, batch_date and last_error survive).
    """
    marker = config.REACTIONS_DIR / MIGRATION_MARKER
    if marker.exists():
        return {"migrated": False}
    moods_root = _moods_dir()
    moods_root.mkdir(parents=True, exist_ok=True)
    config.REACTIONS_SPENT_DIR.mkdir(parents=True, exist_ok=True)

    raw: dict = {}
    # The pool file, read here for its per-item lists. A dir this old normally
    # still has the single-pool name, but _ensure_migration may already have
    # renamed it (any pool_load does that), so accept either — losing the lists
    # would file every blob under "default" and scramble the moods.
    pool_yaml = config.DATA_DIR / _OLD_POOL_NAME
    if not pool_yaml.exists():
        pool_yaml = _pool_path()
    had_yaml = pool_yaml.exists()
    if had_yaml:
        try:
            raw = yaml.safe_load(pool_yaml.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("migration: %s unreadable (%s) — "
                      "blobs will be filed under 'default'", pool_yaml.name, e)
    if not isinstance(raw, dict):
        raw = {}

    def _mood_of(key: str) -> dict[str, str]:
        """Old manifest list → {blob basename: mood folder}, junk-tolerantly."""
        out: dict[str, str] = {}
        entries = raw.get(key)
        if not isinstance(entries, list):
            return out
        for it in entries:
            if not isinstance(it, dict):
                continue
            name = str(it.get("file") or "").rsplit("/", 1)[-1]
            if not name or "\\" in name or name in (".", ".."):
                continue
            try:
                out[name] = _norm_id(it.get("category") or "default")
            except ReactionError:
                out[name] = "default"
        return out

    def _folder_name(name: str) -> str:
        # "pool-<hex>.png" → "<hex>.png": the folder-era id is derived from the
        # stem, and keeping the old prefix would double it up (pool-mad-pool-…).
        stem, ext = Path(name).stem, Path(name).suffix
        if stem.startswith(POOL_ID_PREFIX) and len(stem) > len(POOL_ID_PREFIX):
            return stem[len(POOL_ID_PREFIX):] + ext
        return name

    moved = {"ready": 0, "spent": 0, "strays": 0}

    def _relocate(src_dir: Path, mood_of: dict[str, str],
                  dest_root: Path, kind: str) -> None:
        try:
            entries = sorted(src_dir.iterdir())
        except OSError:
            return                         # source dir never existed — fine
        for p in entries:
            try:
                if not p.is_file() or p.is_symlink():
                    continue               # mood subdirs (spent/), junk — skip
            except OSError:
                continue
            if p.suffix.lower() not in IMAGE_EXTS:
                continue                   # stale .part staging — not a blob
            mood = mood_of.get(p.name)
            d = dest_root / (mood or "default")
            dst = d / _folder_name(p.name)
            while dst.exists():            # never clobber — uniquify instead
                dst = d / f"{dst.stem}-{uuid.uuid4().hex[:6]}{dst.suffix}"
            try:
                d.mkdir(parents=True, exist_ok=True)
                os.replace(p, dst)
            except OSError as e:
                log.warning("migration: could not move %s: %s", p.name, e)
                continue
            moved[kind if mood is not None else "strays"] += 1

    # Ready items → moods-<bot>/<category>/; fired items (and any flat blob the
    # old lost-consume race stranded in spent/) → spent/<category|default>/.
    _relocate(config.REACTIONS_POOL_DIR, _mood_of("items"),
              moods_root, "ready")
    _relocate(config.REACTIONS_SPENT_DIR, _mood_of("spent"),
              config.REACTIONS_SPENT_DIR, "spent")

    if had_yaml:
        # Re-parsing drops the per-item lists (the new parser ignores them);
        # saving writes back only config + batch_date + last_error.
        global _pool_cache
        _pool_cache.clear()
        try:
            pool_save(pool_load())
        except OSError as e:               # pragma: no cover
            log.error("migration: could not rewrite the pool config: %s", e)

    summary = (f"ready={moved['ready']} spent={moved['spent']} "
               f"strays={moved['strays']}")
    try:
        marker.write_text(
            time.strftime("%Y-%m-%dT%H:%M:%S ") + summary + "\n", encoding="utf-8")
    except OSError as e:                   # pragma: no cover
        log.error("could not write the migration marker (will retry next "
                  "start, which is safe): %s", e)
    log.info("reaction pool migrated to mood folders (%s)", summary)
    return {"migrated": True, **moved}
