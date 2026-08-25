"""Per-companion avatar pools — one-shot face/full pairs for new threads.

Each pool-enabled bot (the ``avatar_pool`` roster flag) keeps a bank of avatar
PAIRS on hand: a full-resolution portrait plus its pre-cropped square face,
``<stem>-full.<ext>`` beside ``<stem>-face.<ext>``. When a new user-created or
daily thread needs a face of its own, it draws a random ready pair, pins it
(via the content-addressed snapshot store), and BURNS it — the pair moves to
``spent/`` and can never be drawn again, exactly like a fired reaction image.
The first eligible thread of each day wears the bot's current daily face
instead (that bookkeeping lives in database.py); everything after it draws.

Same skeleton as the reaction pool, built on the same pool_common mechanics:

    <data>/avatar-pool/<bot_id>/ready/   drawable pairs — the FILESYSTEM is
                                         the manifest; hand-drop a complete
                                         pair and it is instantly drawable
    <data>/avatar-pool/<bot_id>/spent/   burnt pairs, kept forever
    <data>/avatar-pool/.generate/        image-CLI staging (scratch)
    <data>/avatar-pool-<bot_id>.yaml     config + batch_date, NO item lists
    <data>/avatar-prompts-<bot_id>.yaml  what the nightly top-up generates

A pair is only drawable when BOTH halves exist — generation publishes the
full first and the face last, so the atomic rename of the face is the moment
the pair goes live. Consumption retires the face first: that rename is the
one-shot lock (pool_common.retire), and the winner takes the full with it.

Pool images are never web-served from here. A drawn pair enters the snapshot
store (avatar_snapshots.snapshot_pair) and serves through the existing
thread-keyed, Safe-Mode-gated route — no new static surface.

Generation is two host calls per pair, both through the image CLI (fixed
argv, never shell): a plain generate for the full portrait, then
``--crop-face`` on the local file for the square face — the rig's detector,
never a local blind crop (house doctrine).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

from . import avatar_snapshots, config, pool_common, pool_guard
from .reactions import (
    _IMAGE_CLI,
    IMAGE_CLI_TIMEOUT_S,
    IMAGE_EXTS,
    _is_image_cli_error,
    image_cli_available,
    image_cli_state,
    note_image_cli_unavailable,
)

log = logging.getLogger("local-chat.avatar_pool")

# A bot id doubles as a path component (avatar-pool/<bot_id>/); same contract
# as reactions.BOT_ID_RE — no dot, no separator, so a query-string id can
# never name a file outside the data dir.
_BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

# Bounded redraws when a consume loses its race to a concurrent creator.
_DRAW_RETRIES = 4


class PoolError(ValueError):
    """Bad input / not found. Carries an HTTP-ish status (see ReactionError)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def _require_bot_id(bot_id: str | None) -> str:
    bid = str(bot_id or "").strip()
    if not bid or not _BOT_ID_RE.match(bid):
        raise PoolError("Unknown bot_id", 404)
    return bid


def _root(bot_id: str) -> Path:
    return config.AVATAR_POOL_DIR / _require_bot_id(bot_id)


def ready_dir(bot_id: str) -> Path:
    return _root(bot_id) / "ready"


def spent_dir(bot_id: str) -> Path:
    return _root(bot_id) / "spent"


def _generate_dir() -> Path:
    return config.AVATAR_POOL_DIR / ".generate"


def _cfg_path(bot_id: str) -> Path:
    return config.DATA_DIR / f"avatar-pool-{_require_bot_id(bot_id)}.yaml"


def bank_path(bot_id: str) -> Path:
    return config.DATA_DIR / f"avatar-prompts-{_require_bot_id(bot_id)}.yaml"


def enabled_bots() -> list[str]:
    """Roster ids with the avatar_pool trait on — the pools that exist."""
    out: list[str] = []
    try:
        for b in config.load_bots():
            if getattr(b, "avatar_pool", False) and _BOT_ID_RE.match(b.id or ""):
                out.append(b.id)
    except Exception:                      # unreadable roster — never fatal
        pass
    return out


# --------------------------------------------------------------------------- #
# Pairs — the unit of stock
# --------------------------------------------------------------------------- #


class Pair(NamedTuple):
    stem: str
    face: Path
    full: Path


def _full_for(face: Path) -> Path | None:
    """The full-res half beside a face file, or None for an incomplete pair.

    Same-suffix sibling wins outright (a pair written together shares one);
    a cross-extension sibling still counts so a hand-dropped mixed pair works.
    """
    stem = face.stem
    if not stem.endswith("-face"):
        return None
    base = stem[: -len("-face")]
    same = face.with_name(f"{base}-full{face.suffix}")
    try:
        if same.is_file() and same.stat().st_size > 0:
            return same
    except OSError:
        pass
    for ext in sorted(IMAGE_EXTS):
        if ext == face.suffix.lower():
            continue
        cand = face.with_name(f"{base}-full{ext}")
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
        except OSError:
            continue
    return None


def list_pairs(d: Path) -> list[Pair]:
    """Every COMPLETE pair in a directory. Incomplete halves are invisible —
    a face without its full (or vice versa) must never be drawn."""
    out: list[Pair] = []
    for p in pool_common.image_files(d, IMAGE_EXTS):
        if not p.stem.endswith("-face"):
            continue
        full = _full_for(p)
        if full is None:
            continue
        try:
            if p.stat().st_size == 0:
                continue
        except OSError:
            continue
        out.append(Pair(p.stem[: -len("-face")], p, full))
    return out


def draw(bot_id: str) -> Pair | None:
    """A random ready pair. Does NOT consume."""
    pairs = list_pairs(ready_dir(bot_id))
    return random.choice(pairs) if pairs else None


def draw_spent(bot_id: str) -> Pair | None:
    """A random already-burnt pair — the dry-pool fallback (repeats allowed,
    so late threads still look distinct from each other more often than not)."""
    pairs = list_pairs(spent_dir(bot_id))
    return random.choice(pairs) if pairs else None


def consume(bot_id: str, pair: Pair) -> bool:
    """Burn a pair: ready/ -> spent/, face first.

    The face's atomic rename is the one-shot lock — of two racing consumers
    exactly one wins it (pool_common.retire), and only the winner moves the
    full half. The loser reports False and redraws. Spent pairs are KEPT:
    they back the dry-pool fallback and keep history explainable.
    """
    dst = spent_dir(bot_id)
    if pool_common.retire(pair.face, dst) is None:
        return False
    pool_common.retire(pair.full, dst)
    return True


def _snapshot_pair(pair: Pair) -> str | None:
    """Land a pair in the content-addressed snapshot store; the returned id is
    what a thread pins. None on unreadable or implausibly large halves.

    Both halves are STATTED before either is read — the store refuses an
    oversized image anyway, and reading first meant a hand-dropped multi-
    gigabyte file in ready/ was pulled wholly into memory just to be rejected
    (`snapshot_id` has always checked the size first; this path did not).
    """
    try:
        for half, cap in ((pair.face, avatar_snapshots.MAX_SNAPSHOT_BYTES),
                          (pair.full, avatar_snapshots.MAX_FULL_BYTES)):
            if half.stat().st_size > cap:
                log.warning("pool avatar too large to snapshot: %s", half.name)
                return None
        face_data = pair.face.read_bytes()
        full_data = pair.full.read_bytes()
    except OSError:
        return None
    if not face_data or not full_data:
        return None
    return avatar_snapshots.snapshot_pair(face_data, full_data,
                                          suffix=pair.face.suffix.lower())


def draw_snapshot_for_thread(bot) -> tuple[str | None, bool]:
    """Draw + burn a pair for a NEW thread. Returns (snapshot_id, explicit).

    ``explicit=True`` means the caller must pin with ``avatar_pinned=1`` —
    a pool draw is this thread's own picture, and the daily rotation's
    re-pin sweep of empty threads must not overwrite it.

    Blocking (file reads + hashing) — call via asyncio.to_thread. The order
    is snapshot THEN consume: the store is content-addressed, so a racer that
    snapshots the same pair and then loses the burn wastes nothing, while the
    reverse order could burn a pair and then fail to snapshot it.

    Fallbacks, in order: ready pair (burnt) -> spent pair (nothing burnt) ->
    (None, False), which the caller resolves as the bot's current face.
    """
    if bot is None or not getattr(bot, "avatar_pool", False):
        return None, False
    try:
        bot_id = _require_bot_id(getattr(bot, "id", ""))
    except PoolError:
        return None, False

    for _ in range(_DRAW_RETRIES):
        pair = draw(bot_id)
        if pair is None:
            break
        sid = _snapshot_pair(pair)
        if sid is None:
            # An unreadable pair would be redrawn forever — burn it out of
            # the way and try another.
            consume(bot_id, pair)
            continue
        if consume(bot_id, pair):
            return sid, True
        # Lost the burn race to a concurrent creator — redraw.

    pair = draw_spent(bot_id)
    if pair is not None:
        sid = _snapshot_pair(pair)
        if sid is not None:
            return sid, True
    return None, False


# --------------------------------------------------------------------------- #
# Config — avatar-pool-<bot>.yaml (knobs + telemetry, no items)
# --------------------------------------------------------------------------- #


@dataclass
class AvatarPoolConfig:
    bot_id: str = ""
    enabled: bool = True             # pause generation without dropping the trait
    target: int = 20                 # ready pairs to keep on hand
    min_ready: int = 5               # dipping below this refills right away
    refresh_hour: int = 4            # local hour of the nightly top-up
    max_per_cycle: int = 3           # a pair is TWO rig calls — keep runs short
    style: str = ""                  # image CLI style preset (overrides bank)
    workflow: str = ""               # image CLI workflow (overrides bank)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PoolState:
    config: AvatarPoolConfig
    batch_date: str = ""             # local YYYY-MM-DD of the last completed fill
    last_error: str = ""


_state_cache: dict[str, tuple[PoolState, float | None]] = {}


def _int_or(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clamp(cfg: AvatarPoolConfig) -> AvatarPoolConfig:
    d = AvatarPoolConfig()
    cfg.enabled = bool(cfg.enabled)
    cfg.target = max(0, min(100, _int_or(cfg.target, d.target)))
    cfg.min_ready = max(0, min(cfg.target, _int_or(cfg.min_ready, d.min_ready)))
    cfg.refresh_hour = max(0, min(23, _int_or(cfg.refresh_hour, d.refresh_hour)))
    cfg.max_per_cycle = max(1, min(10, _int_or(cfg.max_per_cycle, d.max_per_cycle)))
    cfg.style = str(cfg.style or "")[:60]
    cfg.workflow = str(cfg.workflow or "")[:60]
    return cfg


def load_state(bot_id: str) -> PoolState:
    """One bot's pool config + telemetry (mtime-cached). A missing file runs
    on defaults — the ready/ folder is the stock, not the yaml."""
    bot_id = _require_bot_id(bot_id)
    path = _cfg_path(bot_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = _state_cache.get(bot_id)
    if cached and cached[1] == mtime:
        return cached[0]
    raw: dict = {}
    if mtime is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("%s unreadable (%s) — starting empty", path.name, e)
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cdata = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    known = set(AvatarPoolConfig().to_dict())
    cfg = _clamp(AvatarPoolConfig(**{k: v for k, v in cdata.items() if k in known}))
    cfg.bot_id = bot_id                  # the FILENAME owns the identity
    st = PoolState(config=cfg,
                   batch_date=str(raw.get("batch_date") or ""),
                   last_error=str(raw.get("last_error") or "")[:300])
    _state_cache[bot_id] = (st, mtime)
    return st


def save_state(st: PoolState, bot_id: str) -> PoolState:
    bot_id = _require_bot_id(bot_id)
    st.config.bot_id = bot_id
    body: dict = {"version": 1, "config": st.config.to_dict(),
                  "batch_date": st.batch_date}
    if st.last_error:
        body["last_error"] = st.last_error
    path = _cfg_path(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, path)
    _state_cache.pop(bot_id, None)
    return load_state(bot_id)


def update_config(values: dict, bot_id: str) -> AvatarPoolConfig:
    """Apply a settings PUT. Validated and clamped BEFORE the write — a junk
    value must 400 here, not poison the yaml for every load after it."""
    bot_id = _require_bot_id(bot_id)
    if not isinstance(values, dict):
        raise PoolError("Malformed pool settings")
    st = load_state(bot_id)
    cur = st.config.to_dict()
    editable = set(cur) - {"bot_id"}
    for k, v in values.items():
        if k == "bot_id":
            continue
        if k not in editable:
            raise PoolError(f"Unknown pool setting: {k}")
        cur[k] = v
    st.config = _clamp(AvatarPoolConfig(**cur))
    return save_state(st, bot_id).config


# --------------------------------------------------------------------------- #
# The prompt bank — what the nightly top-up generates
# --------------------------------------------------------------------------- #
#
# Deliberately DATA, not code, same as the reaction bank: one yaml per bot,
# reachable over the API. Unlike reactions there is NO shipped seed — an
# avatar bank defines a companion's on-camera look, which is an authored
# choice, so an empty bank simply generates nothing (hand-dropped pairs and
# the existing bank keep the pool alive meanwhile).
#
#     base: "<character description prefixed to every prompt>"
#     suffix: "<fragment appended to every prompt>"          # optional
#     ratio / style / workflow: image CLI knobs for the FULL render
#     shot: face framing for the crop (wide|close|bust|waist|extreme_close)
#     look: detector hint for the crop (anime|realistic)
#     variations: ["...", ...]     # one is picked per generation

DEFAULT_BANK: dict = {
    "version": 1,
    "base": "",
    "suffix": "",
    "ratio": "1:1",
    "style": "",
    "workflow": "",
    "shot": "",
    "look": "",
    "variations": [],
}

_SHOTS = {"", "extreme_close", "close", "wide", "bust", "waist"}
_LOOKS = {"", "anime", "realistic"}

_bank_cache: dict[str, tuple[dict, float | None]] = {}


# See reactions._reject_dash_lead — same rule, same reason: a value that
# starts with a dash would be read as an option by the image CLI, so it is
# refused at the write boundary as well as neutralised by the `--` separator
# in the argv.
def _reject_dash_lead(value: str, label: str) -> None:
    if str(value).strip().startswith("-"):
        raise PoolError(
            f"{label} may not start with '-' — it would be read as a command-line"
            " option by the image CLI", 400)


def _clean_bank(raw: dict) -> dict:
    """Normalise a hand- or agent-edited bank. Never raises on junk — a bad
    field falls back rather than stopping the pool refilling."""
    shot = str(raw.get("shot") or "")[:16]
    look = str(raw.get("look") or "")[:16]
    return {
        "version": 1,
        "base": str(raw.get("base") or "")[:600],
        "suffix": str(raw.get("suffix") or "")[:300],
        "ratio": str(raw.get("ratio") or "1:1")[:12],
        "style": str(raw.get("style") or "")[:60],
        "workflow": str(raw.get("workflow") or "")[:60],
        "shot": shot if shot in _SHOTS else "",
        "look": look if look in _LOOKS else "",
        "variations": [str(x)[:600] for x in (raw.get("variations") or [])
                       if str(x).strip()][:60],
    }


def bank_load(bot_id: str) -> dict:
    bot_id = _require_bot_id(bot_id)
    path = bank_path(bot_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = _bank_cache.get(bot_id)
    if cached and cached[1] == mtime:
        return cached[0]
    raw: dict = {}
    if mtime is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.error("%s unreadable (%s) — treating as empty", path.name, e)
            raw = {}
    bank = _clean_bank(raw if isinstance(raw, dict) else {})
    _bank_cache[bot_id] = (bank, mtime)
    return bank


def bank_save(raw: dict, bot_id: str) -> dict:
    bot_id = _require_bot_id(bot_id)
    if not isinstance(raw, dict):
        raise PoolError("Malformed prompt bank")
    bank = _clean_bank(raw)
    for key in ("base", "suffix", "ratio", "style", "workflow"):
        _reject_dash_lead(bank[key], f"Prompt bank {key}")
    for text in bank["variations"]:
        _reject_dash_lead(text, "Prompt bank variation")
    path = bank_path(bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(bank, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, path)
    _bank_cache.pop(bot_id, None)
    return bank_load(bot_id)


def compose_prompt(bot_id: str) -> str | None:
    """base + one random variation + suffix. None when the bank has no base —
    an avatar without a character description would be a stranger's face."""
    bank = bank_load(bot_id)
    if not bank["base"].strip():
        return None
    parts = [bank["base"]]
    if bank["variations"]:
        parts.append(random.choice(bank["variations"]))
    if bank["suffix"].strip():
        parts.append(bank["suffix"])
    return ", ".join(p.strip().strip(",") for p in parts if p.strip())


# --------------------------------------------------------------------------- #
# Status / deficits / sweep
# --------------------------------------------------------------------------- #


def deficit(bot_id: str, *, only_low: bool = False) -> int:
    """How many pairs short of target the shelf is. ``only_low`` reports 0
    unless the stock has dipped under the low-water mark (the emergency path)."""
    st = load_state(bot_id)
    have = len(list_pairs(ready_dir(bot_id)))
    if only_low and have >= st.config.min_ready:
        return 0
    return max(0, st.config.target - have)


def needs_refill(bot_id: str) -> bool:
    st = load_state(bot_id)
    if not st.config.enabled:
        return False
    return len(list_pairs(ready_dir(bot_id))) < st.config.min_ready


def daily_due(bot_id: str) -> bool:
    st = load_state(bot_id)
    return pool_common.daily_due(st.config.enabled, st.batch_date,
                                 st.config.refresh_hour)


def mark_daily(bot_id: str) -> None:
    st = load_state(bot_id)
    st.batch_date = time.strftime("%Y-%m-%d")
    save_state(st, bot_id)


def status(bot_id: str) -> dict:
    """One bot's pool, as the manager panel and the WS frame report it."""
    bot_id = _require_bot_id(bot_id)
    st = load_state(bot_id)
    ready = len(list_pairs(ready_dir(bot_id)))
    spent = len(list_pairs(spent_dir(bot_id)))
    bank = bank_load(bot_id)
    return {
        **st.config.to_dict(),
        "ready": ready,
        "spent": spent,
        "deficit": max(0, st.config.target - ready),
        "batch_date": st.batch_date,
        "needs_refill": needs_refill(bot_id),
        "due_daily": daily_due(bot_id),
        "available": image_cli_available(),
        # WHY, not just THAT — see the reaction pool's status for the reason.
        "image_cli": image_cli_state(),
        "has_prompts": bool(bank["base"].strip()),
        "last_error": st.last_error,
        "dir": str(ready_dir(bot_id)),    # where to hand-drop pairs
        "bot_id": bot_id,
    }


def sweep(max_age_s: int = pool_common.PART_STALE_S) -> int:
    """Clear crash-stranded ``*.part`` staging from every pool dir + scratch."""
    dirs = [_generate_dir()]
    try:
        for entry in config.AVATAR_POOL_DIR.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                dirs += [entry / "ready", entry / "spent"]
    except OSError:
        pass
    removed = pool_common.sweep_parts(dirs, max_age_s)
    if removed:
        log.info("avatar pool: removed %d stale staging file(s)", removed)
    return removed


# --------------------------------------------------------------------------- #
# Generation — two host calls per pair, via the image CLI
# --------------------------------------------------------------------------- #


def _run_image_cli(argv: list[str], out_dir: Path) -> list[Path]:
    """One CLI call; the saved files from its JSON result (may be empty).

    Only files that resolve inside ``out_dir`` are returned. The caller
    publishes these paths and then UNLINKS them, so a result naming a file
    elsewhere on disk would have us copy and delete something we never asked
    for; such an entry is ignored and left untouched.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=IMAGE_CLI_TIMEOUT_S, check=False)
        result = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        pool_guard.note_refill_failure("rig-error", str(e))
        log.warning("image CLI call failed (%s): %s", argv[1] if len(argv) > 1 else "?", e)
        return []
    if result.get("status") != "ok":
        pool_guard.note_refill_failure("refused", str(result.get("message"))[:200])
        log.warning("image CLI refused: %s", result.get("message"))
        return []
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


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def generate_pair(bot_id: str) -> str | None:
    """Generate one full+face pair into ready/. Returns the stem, or None.

    Full portrait first (plain generate), then the rig's face detector on the
    local file (``--crop-face`` — never a local blind crop). Published full
    first and face LAST: a pair is only drawable once complete, so the face's
    atomic rename is the go-live moment. BLOCKING — call in a thread.
    """
    bot_id = _require_bot_id(bot_id)
    st = load_state(bot_id)
    prompt = compose_prompt(bot_id)
    if prompt is None:
        if st.last_error != "no-prompt-bank":
            st.last_error = "no-prompt-bank"
            save_state(st, bot_id)
        return None
    bank = bank_load(bot_id)
    out_dir = _generate_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    argv = [str(_IMAGE_CLI), "--count", "1",
            "--ratio", bank["ratio"] or "1:1", "--output", str(out_dir)]
    style = st.config.style or bank["style"]
    workflow = st.config.workflow or bank["workflow"]
    if style and not style.strip().startswith("-"):
        argv += ["--style", style[:60]]
    if workflow and not workflow.strip().startswith("-"):
        argv += ["--workflow", workflow[:60]]
    # `--` ends option parsing: the composed prompt is a positional, never a flag.
    argv += ["--", prompt]
    fulls = _run_image_cli(argv, out_dir)
    if not fulls:
        return None
    full_src = fulls[0]

    argv = [str(_IMAGE_CLI), "--crop-face", str(full_src),
            "--output", str(out_dir)]
    if bank["shot"]:
        argv += ["--shot", bank["shot"]]
    if bank["look"]:
        argv += ["--look", bank["look"]]
    faces = _run_image_cli(argv, out_dir)

    stem = uuid.uuid4().hex[:10]
    ok = False
    try:
        if not faces:
            return None
        face_src = faces[0]
        sfx = full_src.suffix.lower() if full_src.suffix.lower() in IMAGE_EXTS else ".png"
        fsfx = face_src.suffix.lower() if face_src.suffix.lower() in IMAGE_EXTS else ".png"
        dest = ready_dir(bot_id)
        if not pool_common.publish_copy(full_src, dest / f"{stem}-full{sfx}"):
            return None
        # Face last — this rename is what makes the pair drawable.
        if not pool_common.publish_copy(face_src, dest / f"{stem}-face{fsfx}"):
            _unlink_quiet(dest / f"{stem}-full{sfx}")   # never leave a half-pair
            return None
        ok = True
        return stem
    finally:
        for f in fulls + faces:
            _unlink_quiet(f)
        if ok:
            pool_guard.note_refill_success()
            st = load_state(bot_id)
            if st.last_error or not st.batch_date:
                st.last_error = ""
                if not st.batch_date:
                    st.batch_date = time.strftime("%Y-%m-%d")
                save_state(st, bot_id)


def refill(limit: int | None = None, *, bot_id: str, only_low: bool = False) -> int:
    """Generate toward the target for one bot. BLOCKING — call in a thread.

    Capped at ``max_per_cycle`` pairs per run (a pair is two rig calls); the
    caller loops until the deficit is gone, same shape as the reaction pool.
    """
    st = load_state(bot_id)
    if not st.config.enabled:
        return 0
    if not image_cli_available():
        # Same contract as the reaction pool: a pool that cannot generate says
        # so loudly and records which way it is broken.
        err = note_image_cli_unavailable(st.last_error)
        if st.last_error != err:
            st.last_error = err
            save_state(st, bot_id)
        return 0
    need = deficit(bot_id, only_low=only_low)
    # The CLI answered, so any CLI fault on record is disproved; and if the pool
    # is already at target, nothing is attempted this round and nothing failed.
    # Without this, only a successful mint ever cleared last_error -- so a full
    # pool reported a stale error forever, still naming an image-CLI fault days
    # after the CLI came back.
    if pool_common.stale_error(st.last_error, cli_ok=True, at_target=need <= 0,
                               cli_fault=_is_image_cli_error(st.last_error)):
        st.last_error = ""
        save_state(st, bot_id)
    want = need
    if limit is not None:
        want = min(want, limit)
    want = min(want, st.config.max_per_cycle)
    if want <= 0:
        return 0
    # VRAM guard, same as the reaction pool: free the rig BEFORE minting and
    # skip the round (loudly) if it is still short after unloading non-pinned
    # idle LLM models on the image host.
    guard = pool_guard.free_vram_before_mint()
    if not guard["ok"]:
        st = load_state(bot_id)
        st.last_error = f"rig-vram-short ({guard['reason']})"[:300]
        save_state(st, bot_id)
        pool_guard.note_refill_failure("vram-short", guard["reason"], actor=bot_id)
        log.error("avatar pool refill for %s BLOCKED by VRAM guard: %s",
                  bot_id, guard["reason"])
        return 0
    made = 0
    for _ in range(want):
        if generate_pair(bot_id) is None:
            break
        made += 1
    if made == 0:
        st = load_state(bot_id)
        # Never let the generic "it produced nothing" overwrite the specific
        # reason already on record (missing bank, or any image-CLI fault).
        if st.last_error != "no-prompt-bank" and not _is_image_cli_error(st.last_error):
            st.last_error = "generation-failed"
            save_state(st, bot_id)
    return made
