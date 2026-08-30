"""Configuration: paths, settings, and the bot registry.

Design goals:
- Easy to modify going forward (OpenClaw can edit a single YAML file).
- Zero required setup: sensible defaults, data dir created on first run.

Two layers of bot config:
  1. DEFAULT_BOTS  — the shipped starter roster (code). Used to MATERIALISE
     <data>/config.yaml on a first run, and as the field-by-field fallback for
     an entry that names a shipped id.
  2. <data>/config.yaml — the operator's roster: ordering, visibility, flags,
     and any bots they added. Once this file exists it is the whole roster —
     shipped defaults are never merged back in, so a bot removed from it stays
     removed.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

# Project root (the repo checkout). config.py lives at backend/app/config.py.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "static"

# Avatars are DATA, not code, so they live in the data directory beside media/
# and files/ -- not under the code tree. They used to sit in frontend/static/,
# which meant a running install kept hundreds of megabytes of personal pictures
# inside the thing a deploy overwrites. Only a file allowlist stood between an
# update and someone's roster; that is too thin a thread for the one part of
# the install that cannot be regenerated.
#
# The URL does not change: /static/avatars/<file> is mounted from here
# explicitly, ahead of the general /static mount. Nothing in the frontend, the
# config schema or the Safe-Mode path checks had to move.
#
# LEGACY: an install whose avatars are still in the old location keeps working.
# Preferring the old path when it exists and the new one does not means an
# upgrade shows the same faces it did before, with no migration step and no
# blank roster on first boot.


def env(name: str, default: str = "") -> str:
    """Read a setting, preferring the DISPATCH_ name over the legacy one.

    The project was called local-chat before it was called DisPatch, and the
    original variables are baked into existing installs' service units. Rather
    than break those on upgrade, every setting answers to BOTH names:
    ``DISPATCH_PORT`` first, ``LOCAL_CHAT_PORT`` second. Only the DISPATCH_
    names are documented; the fallback exists so an old unit keeps working and
    can be migrated whenever its owner gets round to it.
    """
    return os.environ.get(f"DISPATCH_{name}",
                          os.environ.get(f"LOCAL_CHAT_{name}", default))


def _harness_default(flag: str) -> bool:
    """DISPATCH_HARNESS: "auto" (default) = on iff `dsh` is installed; truthy
    = on; anything else = off. Kept here (not in harness.py) so config stays
    import-light; the lookup mirrors harness.resolve_binary()."""
    f = (flag or "").strip().lower()
    if f in ("0", "false", "no", "off", ""):
        return False
    if f != "auto":
        return True
    import shutil
    if shutil.which("dsh"):
        return True
    explicit = os.environ.get("DISPATCH_DSH_BIN", "")
    return bool(explicit) and os.access(explicit, os.X_OK)


def _image_jobs_default(flag: str, endpoint: str) -> bool:
    """DISPATCH_IMAGE_JOBS: "auto" (default) = on iff an image server is
    configured; truthy = on; anything else = off.

    "Configured" is the whole test, and deliberately not "reachable": probing a
    remote GPU box at import time would put a network round trip in front of
    every boot and make the feature's availability depend on whether the rig
    happened to be awake when DisPatch restarted. An operator who has set
    DISPATCH_CLAWFORGE_URL has said what they want; a rig that is down then
    fails the individual job, loudly, in the thread — which is the behaviour
    this feature is built around anyway.
    """
    f = (flag or "").strip().lower()
    if f in ("0", "false", "no", "off", ""):
        return False
    if f != "auto":
        return True
    return bool((endpoint or "").strip())


def _default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "local-chat"


DATA_DIR = Path(env("DATA_DIR", str(_default_data_dir())))
DB_PATH = DATA_DIR / "chats.db"
CONFIG_PATH = DATA_DIR / "config.yaml"
_LEGACY_AVATAR_DIR = FRONTEND_DIR / "avatars"
_DATA_AVATAR_DIR = DATA_DIR / "avatars"


#: Files that can sit in the legacy avatar dir without meaning "avatars live
#: here" -- the repository ships the directory with a .gitkeep in it.
_NOT_AN_AVATAR = frozenset({".gitkeep", ".gitignore", "README.md", ".DS_Store"})


def legacy_avatars_in_use(legacy: Path) -> bool:
    """Does the OLD location actually hold avatars?

    Not "does the directory exist" -- the repository ships that directory with
    a .gitkeep in it, so it exists in every checkout and every image. Keying
    the fallback on existence sent every FRESH install straight back into the
    code tree (and, in a container, into the read-only image), which is the
    exact bug this move exists to fix. Only real files count.
    """
    if not legacy.is_dir():
        return False
    try:
        return any(f.is_file() and f.name not in _NOT_AN_AVATAR
                   for f in legacy.iterdir())
    except OSError:
        return False


def resolve_avatar_dir(legacy: Path, data: Path) -> Path:
    """Where the avatar roster lives.

    The data directory, unless this is an install that still keeps real avatars
    in the old code-tree location and has no data one yet.

    A named function rather than an inline expression because it is a CONTRACT,
    not a detail: companion tools outside this repo (``dispatch-avatar-rotate``
    and friends) must resolve the same directory, and when this rule moved they
    kept pointing at the old path -- so daily avatar rotation died silently for
    two days while every other part of the app had already migrated.
    """
    return legacy if legacy_avatars_in_use(legacy) and not data.is_dir() else data


AVATAR_DIR = resolve_avatar_dir(_LEGACY_AVATAR_DIR, _DATA_AVATAR_DIR)

MEDIA_DIR = DATA_DIR / "media"          # user-uploaded / pasted images
FILES_DIR = DATA_DIR / "files"          # File Server blobs (any type)
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"       # rotated online DB snapshots

# Reaction images. Unlike the paths above these are DERIVED from DATA_DIR on
# every access (see __getattr__ below) rather than bound at import:
#   reactions/builtin/               — the seeded starter pack (regenerable,
#                                      never precious)
#   reactions/pack/                  — uploaded + generated images (user data)
#   reactions/moods-<bot>/<mood>/    — ready one-shot pool images, one folder
#                                      per mood PER BOT; the FILESYSTEM is the
#                                      pool's manifest
#   reactions/spent/<mood>/          — fired pool images, kept forever (chat
#                                      traces re-open them), shared by all bots
# reaction-pool-<bot>.yaml carries only pool CONFIG + batch date — the mood
# folders themselves are the stock, so hand-dropping an image into
# moods-<bot>/bravo/ makes it drawable with no registry write at all.
#
# The per-bot paths are NOT constants here: which bots have pools is a roster
# question, so reactions.py derives them (`_moods_dir`, `_pool_path`,
# `_bank_path`) from config.yaml. What remains below is the shared roots plus
# the pre-per-bot names, which exist only as the migration's source.
#
# Deriving them lazily matters for tests: every suite that points DATA_DIR at a
# tmp_path gets these for free, so none of them can seed a starter pack into the
# real ~/.local/share/local-chat. Binding at import made exactly that happen.
_DERIVED_PATHS = {
    # Avatar pools: one-shot face/full pairs drawn by new threads. Per-bot
    # subdirs (avatar-pool/<bot>/{ready,spent}) are a roster question, so
    # avatar_pool.py derives them; only the shared root lives here.
    "AVATAR_POOL_DIR": ("avatar-pool",),
    "REACTIONS_DIR": ("reactions",),
    "REACTIONS_BUILTIN_DIR": ("reactions", "builtin"),
    "REACTIONS_PACK_DIR": ("reactions", "pack"),
    "REACTIONS_SPENT_DIR": ("reactions", "spent"),
    "REACTIONS_PATH": ("reactions.yaml",),
    # --- migration sources, never written ---------------------------------- #
    # The pre-2026-08 flat pool dir, and the single-pool layout that replaced
    # it. Both are read ONLY by the one-shot migrations in reactions.py; a
    # leftover file under these names is inert (in particular, a stale
    # reaction-prompts.yaml can never shadow a bot's own prompt bank).
    "REACTIONS_POOL_DIR": ("reactions", "pool"),
    "REACTIONS_LEGACY_MOODS_DIR": ("reactions", "moods"),
    "REACTION_POOL_PATH": ("reaction-pool.yaml",),
    "REACTION_PROMPTS_PATH": ("reaction-prompts.yaml",),
}


def _derived(name: str) -> Path:
    return DATA_DIR.joinpath(*_DERIVED_PATHS[name])


def __getattr__(name: str) -> Path:
    """Resolve the derived reaction paths against the CURRENT DATA_DIR.

    Module-level __getattr__ (PEP 562) only fires for names not already defined,
    so this costs nothing for every other constant here. Note it covers
    `config.REACTIONS_DIR` from OUTSIDE the module only — code in here must go
    through `_derived()`, because a bare name is a globals lookup, not an
    attribute access.
    """
    parts = _DERIVED_PATHS.get(name)
    if parts is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return DATA_DIR.joinpath(*parts)


def ensure_dirs() -> None:
    # No mood directory here: those are per bot and per mood, and reactions.py
    # creates them on demand (a hand-drop or a generation). Creating an empty
    # `reactions/moods/` would also resurrect the legacy name the migration
    # looks for, and hand a fresh install a "pool" that has never existed.
    for d in (DATA_DIR, MEDIA_DIR, FILES_DIR, LOG_DIR, BACKUP_DIR,
              _derived("REACTIONS_DIR"), _derived("REACTIONS_BUILTIN_DIR"),
              _derived("REACTIONS_PACK_DIR"), _derived("REACTIONS_SPENT_DIR")):
        d.mkdir(parents=True, exist_ok=True)



# --------------------------------------------------------------------------- #
# Runtime settings (env-overridable)
# --------------------------------------------------------------------------- #


def _int_env(name: str, default: int) -> int:
    """Integer form of env(): same DISPATCH_ / LOCAL_CHAT_ fallback."""
    try:
        return int(env(name).strip() or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Bind LOOPBACK by default. This app ships with no credential configured,
    # so a wider default means the first `uvicorn app.main:app` on a machine
    # with a port forward exposes every message to the internet before the
    # operator has finished reading the README. Containers set DISPATCH_HOST
    # explicitly (the container network is the boundary there), and anyone
    # putting a reverse proxy in front sets it too — both are deliberate acts.
    # Set DISPATCH_HOST
    # =127.0.0.1 to restrict to this machine only.
    host: str = env("HOST", "127.0.0.1")
    port: int = _int_env("PORT", 8765)
    # Path to the openclaw CLI. Default resolves via PATH; override for systemd.
    openclaw_bin: str = os.environ.get("OPENCLAW_BIN", "openclaw")
    # Per-agent turn timeout in seconds (passed to `openclaw agent --timeout`).
    agent_timeout: int = _int_env("AGENT_TIMEOUT", 900)
    # Max number of agent subprocesses running at once (protects local models).
    max_concurrency: int = _int_env("MAX_CONCURRENCY", 3)
    # Online DB snapshot interval (seconds) + how many rotated snapshots to keep.
    # 0 disables the periodic backup loop (manual /api/export still works).
    backup_interval: int = _int_env("BACKUP_INTERVAL", 6 * 3600)
    backup_keep: int = _int_env("BACKUP_KEEP", 12)
    # Native gateway transport (see app/gateway_ws.py). Three states, because
    # replacing how every reply arrives is not a thing to flip blind:
    #   ""/"0"   OFF   — transcript tailing only. The default, deliberately:
    #                    an unset env var must never change the family app.
    #   "shadow" WATCH — connects, receives, computes what it WOULD deliver and
    #                    logs it. Delivers nothing. This is the comparison run.
    #   "1"      LIVE  — the WS path delivers, and the tailing paths stand down.
    gateway_ws: str = env("GATEWAY_WS", "").strip().lower()
    # If true, a fresh thread shows a short canned greeting (no LLM call).
    greeting: bool = env("GREETING", "0") not in ("", "0", "false", "False")
    # Gates the coding terminal (server-side PTY, full-session only).
    # Default on; set DISPATCH_TERMINAL=0 to disable + 404 the routes.
    terminal_enabled: bool = env("TERMINAL", "1") not in ("0", "false", "False")
    # Gates the DeepSeek Harness (dsh) pane: `dsh web` service control, the
    # default-model switch and headless jobs (full-session only — code
    # execution). Default "auto": on when a `dsh` binary is found at boot,
    # off otherwise (an install without dsh never grows a stray pseudo-bot).
    # DISPATCH_HARNESS=1 forces it on (the pane then says "not installed"),
    # =0 disables + 404s the routes.
    harness_enabled: bool = _harness_default(env("HARNESS", "auto"))
    # The systemd --user unit that runs `dsh web`, and the loopback port it
    # listens on (both must agree with the unit file).
    # Agent-fired image jobs (app/image_jobs.py): a bot asks for a picture, a
    # placeholder lands in the thread immediately, and DisPatch drives the
    # image server in the background and rewrites that message when the render
    # arrives. Off unless an image server is configured — there is no default
    # endpoint, because the address of somebody's GPU box is site
    # configuration, not something to ship.
    clawforge_url: str = env("CLAWFORGE_URL", "").strip()
    # Where the finished files are served from. Defaults to /files/ on the same
    # origin as the MCP endpoint, which is how the reference server lays it out.
    clawforge_files_url: str = env("CLAWFORGE_FILES_URL", "").strip()
    image_jobs_enabled: bool = _image_jobs_default(
        env("IMAGE_JOBS", "auto"), env("CLAWFORGE_URL", ""))
    harness_unit: str = env("HARNESS_UNIT", "dsh-web.service")
    harness_port: int = int(env("HARNESS_PORT", "3080") or 3080)
    # Gateway chat mirror: continuously tail OpenClaw's own conversations (the
    # Control-UI webchat + each agent's main session) into DisPatch threads.
    # DISPATCH_MIRROR=0 disables the loop entirely.
    mirror_enabled: bool = env("MIRROR", "1") not in ("0", "false", "False")
    # Poll cadence in seconds (transcripts are tailed by byte offset — cheap).
    mirror_poll: int = _int_env("MIRROR_POLL", 4)
    # Idle ceiling for the mirror poll: with no gateway-side activity the poll
    # interval ramps from mirror_poll up to this (a full 7-bot session scan
    # every 4s around the clock is wasted work when nobody is chatting).
    mirror_idle_max: int = _int_env("MIRROR_IDLE_MAX", 60)
    # Sessions whose transcript is older than this many hours when FIRST seen
    # are tailed from EOF: new activity mirrors, stale history is not imported.
    mirror_horizon_h: int = _int_env("MIRROR_HORIZON_H", 72)
    # Which session kinds to mirror (see openclaw.mirror_kind). "other" adds
    # scripted/watchdog sessions; daily/cron/subagent/dashboard are never mirrored.
    mirror_kinds: str = env("MIRROR_KINDS", "webchat,main")


SETTINGS = Settings()


# --------------------------------------------------------------------------- #
# Bot registry
# --------------------------------------------------------------------------- #


@dataclass
class Bot:
    id: str                     # Agent id, as the backend knows it
    name: str                   # Display name, as the chat shows it
    avatar: str = ""            # Filename under frontend/static/avatars/;
                                # blank -> the UI draws a letter block
    emoji: str = ""             # Fallback glyph if avatar missing
    model_hint: str = ""        # Human label for the model badge (runtime wins)
    order: int = 0
    visible: bool = True
    # Shown in Safe Mode (the no-PIN, image-free view). Server-enforced: bots
    # without this flag are invisible AND unreachable to Safe-Mode sessions.
    safe: bool = False
    # Optional explicit avatar colour (any CSS colour, e.g. "#a01818"). Drives the
    # letter-block avatar gradient instead of the id-derived hue, so a character
    # whose auto-colour clashes with another can be pinned to a distinct shade.
    color: str = ""
    # May this bot fire reaction images? OFF for every shipped bot by design —
    # a reaction interrupts every screen in the house, so it is an opt-in
    # per-character trait, not an ambient capability. Toggled in the Bot
    # Manager (unlocked) and in the Companions panel for safe bots. Each bot
    # switched on here gets its OWN image pool (see reactions.py).
    reactions: bool = False
    # Server-side reaction AUTOPILOT: when this bot's reply carries no
    # `:react:` marker, the persist chokepoint fires one on its behalf
    # (heuristic mood; see main._reaction_autopilot). Exists because a model's
    # adherence to a standing "react on every reply" instruction decays with
    # context depth and tool load — measured live: 36 consecutive replies with
    # zero markers in one long session. The bot's own marker always wins;
    # autopilot only fills silence. Requires `reactions` too.
    reaction_autopilot: bool = False
    # Does this bot keep an avatar POOL — one-shot face/full pairs that new
    # user-created and daily threads draw (and burn) so each conversation
    # wears its own picture? Off by default: a pool only makes sense for a
    # companion whose look is curated (a prompt bank / seeded pairs). The
    # first eligible thread each day wears the daily face instead of drawing.
    # See app/avatar_pool.py.
    avatar_pool: bool = False
    # May this bot fire an IMAGE JOB — "draw this, put it in the thread when
    # it's ready"? Off by default, and for the same reason reactions are: it
    # spends time on a shared GPU and drops a picture into a family
    # conversation, so it is a per-character opt-in rather than something every
    # bot silently gains. Requires the feature itself to be configured
    # (DISPATCH_CLAWFORGE_URL); see app/image_jobs.py.
    image_jobs: bool = False
    # Which ClawForge workflow this bot's pictures use when a request names
    # none. A companion whose look is curated needs one specific workflow, and
    # the inline `[[pic:…]]` marker has no room to say so — it carries a prompt
    # and a caption, nothing else. Empty means the image server's own default.
    image_workflow: str = ""
    # Direct-provider backend ("Connect an AI"). When present this bot does NOT
    # go through the agent CLI at all — its turns are HTTP calls to an LLM API.
    # Shape (every field optional except provider + model):
    #   {provider, base_url, model, api_key, api_key_env, system_prompt,
    #    max_history_chars}
    # `api_key` lives in config.yaml, which is why every writer here chmods that
    # file to 0600 (see _write_bots). `api_key_env` names an environment
    # variable and WINS over a stored key when that variable is set, so an
    # operator who would rather not have a secret on disk has a first-class way
    # to say so. See app/llm_api.py for the resolution rule and the providers.
    #
    # None (not {}) means "no API backend" — the OpenClaw path stays in charge.
    api: dict | None = None

    @property
    def avatar_url(self) -> str:
        """URL for this bot's picture, or "" when it has none.

        The empty case matters: `AVATAR_DIR / ""` is the avatars DIRECTORY,
        which exists, so the naive version stat()'d it happily and produced
        `/static/avatars/?v=<mtime>` — a URL the browser then requested and
        got a 404 for, on every render, for every bot without a picture. The
        frontend already falls back to a coloured letter block on an empty
        url; it just needs to be given one.
        """
        if not self.avatar:
            return ""
        # mtime cache-buster so a re-uploaded avatar replaces the cached one.
        try:
            v = int((AVATAR_DIR / self.avatar).stat().st_mtime)
            return f"/static/avatars/{self.avatar}?v={v}"
        except OSError:
            return f"/static/avatars/{self.avatar}"

    def to_dict(self) -> dict:
        """The public bot record. Reachable by a Safe-Mode session, so it
        carries NO API credentials and no provider base URL — only the fact
        that a direct provider is configured, which is what the UI needs to
        stop offering the first-run "Connect an AI" card."""
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "avatar": self.avatar,
            "avatar_url": self.avatar_url,
            "model_hint": self.model_hint,
            "order": self.order,
            "visible": self.visible,
            "safe": self.safe,
            "color": self.color,
            "reactions": self.reactions,
            "avatar_pool": self.avatar_pool,
            "image_jobs": self.image_jobs,
            "image_workflow": self.image_workflow,
            "api_provider": str((self.api or {}).get("provider") or ""),
        }

    def api_public(self) -> dict | None:
        """The `api` block with the secret replaced by a boolean.

        Used by the admin-only routes that echo a bot back after saving it.
        There is deliberately no code path anywhere that returns `api_key`:
        the operator typed it, the server stored it, and reading it back over
        HTTP would only ever help somebody who should not have it.
        """
        if not self.api:
            return None
        out = {k: v for k, v in self.api.items() if k != "api_key"}
        out["has_key"] = bool(self.api.get("api_key"))
        return out

    def to_admin_dict(self) -> dict:
        """to_dict() plus the redacted API block (full-session routes only)."""
        return {**self.to_dict(), "api": self.api_public()}


# Shipped defaults.
DEFAULT_BOTS: list[Bot] = [
    # The starter roster a fresh install gets, and what a lost config.yaml
    # falls back to. Deliberately small and generic: two assistants, one of
    # which is reachable WITHOUT the PIN so the limited tier has something to
    # talk to out of the box, and one that is not so the distinction is visible
    # from the first minute.
    #
    # No `avatar` filenames: the repo ships no avatar images, so naming one
    # here would mean a fresh clone requesting a file that does not exist. With
    # the field empty the UI draws a coloured letter block instead — which is a
    # deliberate fallback, not a broken image. Drop your own PNG/SVG into
    # frontend/static/avatars/ and set the name here or in config.yaml.
    #
    # `id` is the identifier handed to the agent backend. If you have no agent
    # configured these are still perfectly good conversation threads; they just
    # will not answer.
    # Two, because the first question anyone has is "which one do I ask?" and
    # the honest answer is a speed/quality trade — so the roster says that out
    # loud instead of shipping two identical "Assistant" entries.
    #
    #   Quick  — a small fast model. Most messages are small and fast.
    #   Smart  — a large capable model, for the ones that are not.
    #
    # The safe flags are NOT decoration and are the other half of why there are
    # exactly two: Quick is reachable WITHOUT the PIN so the limited tier has
    # something to talk to out of the box, Smart is not. A new operator sees the
    # two-tier model working from the first minute rather than reading about it.
    #
    # model_hint is a LABEL, not a setting — it renders as the badge under the
    # name and the runtime value wins when there is one. It is deliberately
    # provider-neutral: pick the actual models in Settings → Connect an AI (or
    # point them at an agent CLI). Naming a vendor's model here would be a
    # guess about someone else's account, and would age badly.
    Bot(id="quick", name="Quick", emoji="⚡", model_hint="fast model",
        order=0, safe=True),
    Bot(id="smart", name="Smart", emoji="🧠", model_hint="capable model",
        order=1, safe=False),
]


def _bot_entry(b: Bot) -> dict:
    """The ONE place a Bot becomes a config.yaml record.

    Every writer below goes through this. That is not tidiness — it is the fix
    for a bug this file has now shipped twice: each writer used to spell the
    field list out by hand, so a field one of them forgot (`reactions`, until
    2026-08-01) silently reverted to its compiled default for EVERY bot on the
    next load. `api` would have been the third such field, and the one whose
    loss costs the operator their API key.

    It shipped a THIRD time anyway: `reaction_autopilot` was read by load_bots
    but never emitted here, so every roster write (an avatar upload was enough)
    turned autopilot off for the whole roster. The structural fix is the
    round-trip test in tests/test_bot_config.py, which builds a Bot with every
    persistable field non-default and fails if this dict drops one — so a
    fourth field cannot be lost the same way.

    `api` is omitted entirely when unset so a roster with no direct-provider
    bots keeps the same file it has always had.
    """
    entry = {
        "id": b.id,
        "name": b.name,
        "avatar": b.avatar,
        "emoji": b.emoji,
        "model_hint": b.model_hint,
        "order": b.order,
        "visible": b.visible,
        "safe": b.safe,
        "color": b.color,
        "reactions": b.reactions,
        "reaction_autopilot": b.reaction_autopilot,
        "avatar_pool": b.avatar_pool,
        "image_jobs": b.image_jobs,
        "image_workflow": b.image_workflow,
    }
    if b.api:
        entry["api"] = dict(b.api)
    return entry


def _write_bots(entries: list[dict]) -> None:
    """Persist the roster, then lock the file down to owner-only.

    config.yaml can now hold provider API keys, so it is treated as a secret
    even when it does not happen to contain one today — a chmod that only
    happens on the write that adds a key would leave the window between "key
    saved" and "next write" wide open, and the mode is what an operator will
    check. Failure to chmod is non-fatal (Windows, some bind mounts): the write
    itself is the thing that must not be lost.

    Written the way auth._write writes security.yaml: into a temp file in the
    same directory, chmod 0600 BEFORE it is published, then os.replace. The
    old shape (write_text then chmod) left the keys world-readable for the
    length of the write, and a crash in the middle truncated the roster
    instead of leaving the previous file intact.
    """
    ensure_dirs()
    text = yaml.safe_dump({"bots": entries}, sort_keys=False, allow_unicode=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(CONFIG_PATH.parent),
                                    prefix=".config.yaml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass                    # Windows / some bind mounts
        os.replace(tmp_name, CONFIG_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    _invalidate_bots_cache()


def _write_default_config() -> None:
    """Materialise config.yaml from DEFAULT_BOTS so it's easy to hand-edit."""
    _write_bots([_bot_entry(b) for b in DEFAULT_BOTS])


# Tiny cache so we don't re-read+parse config.yaml on every request. Keyed by
# the file's mtime so external edits (or our own writes) are picked up.
_bots_cache: tuple[float, list[Bot]] | None = None


def _invalidate_bots_cache() -> None:
    global _bots_cache
    _bots_cache = None


def load_bots() -> list[Bot]:
    """Return bots ordered by `order`, from config.yaml over DEFAULT_BOTS.

    config.yaml may override any field and add brand-new bots. Missing fields
    fall back to the default for that id, then to safe generic values.

    The shipped roster seeds a FIRST RUN and nothing else. It used to be merged
    into every load — any DEFAULT_BOTS id absent from config.yaml was appended
    back — which meant an existing install grew phantom bots on upgrade: entries
    that map to no agent, that the operator never chose, and (if a shipped
    default is flagged safe) that show up on locked family devices. Removing a
    bot from config.yaml is a decision; this function must respect it.
    """
    global _bots_cache
    defaults = {b.id: b for b in DEFAULT_BOTS}

    first_run = not CONFIG_PATH.exists()
    if first_run:
        _write_default_config()

    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if _bots_cache and _bots_cache[0] == mtime:
            # Copies on every hit — callers must never share mutable instances.
            return [replace(b) for b in _bots_cache[1]]
    except OSError:
        mtime = 0.0

    raw: dict = {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except (yaml.YAMLError, OSError):
        raw = {}

    entries = raw.get("bots") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        # Copies, never the DEFAULT_BOTS singletons — callers may mutate.
        result = sorted((replace(b) for b in defaults.values()), key=lambda b: b.order)
        _bots_cache = (mtime, [replace(b) for b in result])
        return result

    bots: list[Bot] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or "id" not in e:
            continue
        bid = str(e["id"])
        base = defaults.get(bid)
        bots.append(
            Bot(
                id=bid,
                name=str(e.get("name", base.name if base else bid)),
                avatar=str(e.get("avatar", base.avatar if base else "default.png")),
                emoji=str(e.get("emoji", base.emoji if base else "🤖")),
                model_hint=str(e.get("model_hint", base.model_hint if base else "")),
                order=int(e.get("order", i)),
                visible=bool(e.get("visible", True)),
                safe=bool(e.get("safe", base.safe if base else False)),
                color=str(e.get("color", base.color if base else "")),
                reactions=bool(e.get("reactions", base.reactions if base else False)),
                reaction_autopilot=bool(e.get("reaction_autopilot",
                                              base.reaction_autopilot if base else False)),
                avatar_pool=bool(e.get("avatar_pool",
                                       base.avatar_pool if base else False)),
                image_jobs=bool(e.get("image_jobs",
                                      base.image_jobs if base else False)),
                image_workflow=str(e.get("image_workflow",
                                         base.image_workflow if base else "")),
                # No fallback to `base`: a shipped default never carries an
                # `api` block, and an entry that dropped one did so on purpose.
                api=dict(e["api"]) if isinstance(e.get("api"), dict) else None,
            )
        )
        seen.add(bid)

    # Only on the run that MATERIALISED config.yaml may a shipped default be
    # added to what the file says — and even then it is belt-and-braces, since
    # _write_default_config just wrote all of them. On every later load the
    # file is the whole roster. Copies — the DEFAULT_BOTS singletons must never
    # escape, or a caller mutating order/visible corrupts them for the process
    # lifetime (and that corruption can persist to disk).
    if first_run:
        for bid, b in defaults.items():
            if bid not in seen:
                bots.append(replace(b))

    result = sorted(bots, key=lambda b: b.order)
    _bots_cache = (mtime, [replace(b) for b in result])
    return result


def save_bot_order(items: list[dict]) -> list[Bot]:
    """Persist ordering/visibility/safe/reactions flags.

    `items` = [{id, order?, visible?, safe?, reactions?}].

    Preserves all other bot fields from the current config so the Bot Manager
    only needs to send the fields it manages.
    """
    current = {b.id: b for b in load_bots()}
    ordered = sorted(
        (it for it in items if isinstance(it, dict) and it.get("id") in current),
        key=lambda it: it.get("order", 0),
    )

    payload_bots = []
    for idx, it in enumerate(ordered):
        b = current[it["id"]]
        payload_bots.append(_bot_entry(replace(
            b,
            order=idx,
            visible=bool(it["visible"]) if it.get("visible") is not None else b.visible,
            safe=bool(it["safe"]) if it.get("safe") is not None else b.safe,
            reactions=(bool(it["reactions"]) if it.get("reactions") is not None
                       else b.reactions),
            avatar_pool=(bool(it["avatar_pool"]) if it.get("avatar_pool") is not None
                         else b.avatar_pool),
        )))

    # Keep any bots the client didn't mention (defensive).
    mentioned = {it["id"] for it in ordered}
    payload_bots += [_bot_entry(b) for b in current.values() if b.id not in mentioned]

    _write_bots(payload_bots)
    return load_bots()


def get_bot(bot_id: str) -> Bot | None:
    for b in load_bots():
        if b.id == bot_id:
            return b
    return None


def resolve_bot(bot_id: str | None) -> Bot | None:
    """get_bot, but case-forgiving — for the agent-facing API boundary.

    The gateway lowercases whole session keys, so agents routinely echo
    lowercase ids (`scout`, `ai_swift`) back at REST endpoints whose roster
    ids are mixed-case. Exact match wins; otherwise a UNIQUE case-insensitive
    match resolves. Callers must persist the returned bot's `.id`, never the
    raw input — otherwise a `daily-scout-…` thread forks off `daily-Scout-…`.
    """
    if not bot_id:
        return None
    bot = get_bot(bot_id)
    if bot:
        return bot
    matches = [b for b in load_bots() if b.id.lower() == bot_id.lower()]
    return matches[0] if len(matches) == 1 else None


def save_bot_avatar(bot_id: str, avatar_filename: str) -> Bot | None:
    """Point a bot's avatar at a new file under AVATAR_DIR and persist it."""
    bots = load_bots()
    target = next((b for b in bots if b.id == bot_id), None)
    if not target:
        return None
    # Every non-avatar field is carried over verbatim, via the shared
    # serializer — spelling the list out by hand here is what dropped
    # `reactions` for every bot on an avatar upload until 2026-08-01.
    _write_bots([
        _bot_entry(replace(b, avatar=avatar_filename) if b.id == bot_id else b)
        for b in bots
    ])
    return get_bot(bot_id)


def upsert_bot(bot: Bot) -> Bot:
    """Create-or-update ONE bot by id, leaving every other bot untouched.

    Used by the "Connect an AI" flow, which is the first writer that can add a
    bot the operator never listed by hand. An existing id is replaced in place
    (keeping its position in the file, so re-running the setup panel does not
    shuffle the sidebar); a new id lands at the end with the next free `order`.
    """
    bots = load_bots()
    if any(b.id == bot.id for b in bots):
        entries = [_bot_entry(bot if b.id == bot.id else b) for b in bots]
    else:
        entries = [_bot_entry(b) for b in bots]
        entries.append(_bot_entry(replace(
            bot, order=max((b.order for b in bots), default=-1) + 1)))
    _write_bots(entries)
    return get_bot(bot.id)  # type: ignore[return-value]


def api_bot_count() -> int:
    """How many bots have a direct-provider backend configured.

    Cheap (load_bots is mtime-cached) and used by /api/auth/status so the
    frontend can decide whether to show the first-run setup card without
    probing anything.
    """
    return sum(1 for b in load_bots() if b.api)
