"""Kit chats (4.2): the chats pushed to every copy of a template.

Since 4.2 a household bot that is handed out as copies is a **live template** (``Bot.give_to``,
see ``config.py``): a copy inherits the template's setup at load time, so this module no longer
manages any field. What is left of a "kit" is its **chats**: ``id``, ``title``, Markdown body,
``audience`` (``everyone`` | ``admins``) and a ``rev``. Each copy gets each chat once per
``rev``, as a new thread in its owner's chats; a new ``rev`` replaces the earlier version's
thread while that holds only the seeded message (the owner never wrote in it).

Who gets a copy, and what happens when a profile is unticked, lives in
``profiles_service`` (``make_copy``, ``set_give_to``, ``reconcile_copies``); this module only
answers "which kit does this copy belong to" (the copy's ``template``) and posts the chats.

Where kits come from
--------------------
* **Shipped**: Atlas (:data:`SHIPPED`), defined here and versioned with the app. Its chats are
  the guide files in ``app/guide/``. (Python, not a data file: a missing data file on a deploy
  would silently stop every Atlas getting its guides.)
* **Household**: an Admin attaches chats to any template (a direct-backend household bot) in
  Settings -> Characters. Stored in ``<data>/bot-kits/<bot_id>.yaml`` (chat bodies inline),
  written atomically. For ``atlas`` a household file adds chats to the shipped ones.

State and delivery
------------------
Chats are posted through the normal assistant funnel (``main._deliver_assistant_text``) with
``metadata.seeded`` + ``metadata.kit``. What each copy was given is recorded in
``<data>/bot-kits/state.json`` (``delivered``), so a chat the owner deleted never comes back by
itself; a copy's Reset re-posts the ones whose thread is gone. ``state.json`` also holds
``removed``: the (profile, template) pairs whose copy the owner deleted, so auto-clone respects
the deletion; an Admin's explicit tick, "Get my copy" or Reset profile clears it.

Upgrading from 4.0 (``state.migrated``): the Atlas copies made by 4.0 count their guide
chats as delivered (the ``guide:<bot>:<file>`` source ids). The sync never raises at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import config, firstrun

log = logging.getLogger("local-chat.kits")

KIT_DIR_NAME = "bot-kits"
STATE_NAME = "state.json"
ATLAS = firstrun.BOT_ID

AUDIENCES: tuple[str, ...] = ("everyone", "admins")
MAX_CHATS = 20
MAX_TITLE = 80
MAX_BODY = 20000
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
CHAT_ID = re.compile(r"\Ac[0-9a-f]{8}\Z")


@dataclass(frozen=True)
class KitChat:
    id: str
    title: str
    audience: str = "everyone"
    rev: int = 1
    body: str = ""
    file: str = ""              # a shipped chat: the guide file under app/guide/
    builtin: bool = False

    def text(self) -> str:
        if self.file:
            try:
                return (firstrun.GUIDE_DIR / self.file).read_text(encoding="utf-8").strip()
            except OSError:
                log.warning("kit chat file %s is missing", self.file)
                return ""
        return neutralise(self.body.strip())


@dataclass(frozen=True)
class Kit:
    bot_id: str
    chats: tuple[KitChat, ...]
    shipped: bool = False
    household: bool = False     # a <data>/bot-kits/<id>.yaml exists


#: The kits that ship with the app. Atlas: the "how to use DisPatch" chats every Atlas opens
#: with (4.0's PROFILE_GUIDES / ADMIN_GUIDES), first = newest = top of the list.
SHIPPED: dict[str, tuple[KitChat, ...]] = {
    ATLAS: (
        KitChat("meet-atlas", "Meet Atlas", "everyone", 1, file="meet-your-atlas.md", builtin=True),
        # rev 2 (4.1): the screenshots. The household Atlas follows these revs too
        # (firstrun.backfill_house_guides).
        KitChat("using-dispatch", "Using DisPatch", "everyone", 2, file="using-dispatch.md", builtin=True),
        KitChat("how-dispatch-works", "How DisPatch works", "admins", 1, file="how-dispatch-works.md",
                builtin=True),
    ),
}


def neutralise(text: str) -> str:
    """An Admin-written chat body is shown, never acted on: the in-text markers an assistant
    message may carry (``[[media:/path]]`` ingests a file off the host, ``[[pic:..]]`` starts a
    render, ``:react:`` fires a reaction, ``[[doc:..]]``/``[[research:..]]``) are broken with a
    zero-width space so they stay visible as text."""
    return re.sub(r"(?i):react:", lambda m: m.group(0)[0] + "\u200b" + m.group(0)[1:],
                  text.replace("[[", "[\u200b["))


def _switched_on() -> bool:
    return firstrun.guides_enabled()


# --------------------------------------------------------------------------- #
# Storage: household kits and the state file
# --------------------------------------------------------------------------- #


def kit_dir() -> Path:
    return Path(config.DATA_DIR) / KIT_DIR_NAME


def _kit_path(bot_id: str) -> Path | None:
    return kit_dir() / f"{bot_id}.yaml" if _SAFE_ID.match(bot_id or "") else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _clean_chat(raw: Any) -> KitChat | None:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "")
    title = str(raw.get("title") or "").strip()[:MAX_TITLE]
    body = str(raw.get("body") or "")[:MAX_BODY]
    audience = str(raw.get("audience") or "everyone")
    if not CHAT_ID.match(cid) or not title or not body.strip() or audience not in AUDIENCES:
        return None
    try:
        rev = max(1, int(raw.get("rev") or 1))
    except (TypeError, ValueError):
        rev = 1
    return KitChat(cid, title, audience, rev, body=body)


def load_household(bot_id: str) -> dict | None:
    """The household kit file for ``bot_id`` as ``{"auto_clone": bool | None, "chats": [KitChat]}``,
    or None when there is none (or it is unreadable). ``auto_clone`` is the 4.1 switch, read only
    so the 012 migration can turn it into ``give_to``; it is None when the file never said."""
    path = _kit_path(bot_id)
    if path is None or not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("could not read the kit %s", path, exc_info=True)
        return None
    if not isinstance(doc, dict):
        return None
    chats = [c for c in (_clean_chat(r) for r in (doc.get("chats") or [])) if c is not None]
    legacy = doc.get("auto_clone")
    return {"auto_clone": None if legacy is None else bool(legacy), "chats": chats[:MAX_CHATS]}


def save_household(bot_id: str, *, chats: list[KitChat]) -> None:
    path = _kit_path(bot_id)
    if path is None:
        raise ValueError("bad bot id")
    doc = {"chats": [{"id": c.id, "title": c.title, "audience": c.audience, "rev": c.rev, "body": c.body}
                     for c in chats[:MAX_CHATS]]}
    _atomic_write(path, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))


def delete_household(bot_id: str) -> bool:
    path = _kit_path(bot_id)
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True


_state_lock = threading.RLock()


def _state_path() -> Path:
    return kit_dir() / STATE_NAME


def _read_state() -> dict:
    try:
        doc = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc["delivered"] = doc.get("delivered") if isinstance(doc.get("delivered"), dict) else {}
    doc["removed"] = doc.get("removed") if isinstance(doc.get("removed"), dict) else {}
    return doc


def _write_state(doc: dict) -> None:
    try:
        _atomic_write(_state_path(), json.dumps(doc, sort_keys=True))
    except OSError:
        log.warning("could not write %s", _state_path(), exc_info=True)


def _update_state(fn: Callable[[dict], None]) -> dict:
    with _state_lock:
        doc = _read_state()
        fn(doc)
        _write_state(doc)
        return doc


def mark_removed(profile_id: str, kit_id: str) -> None:
    """The owner deleted its copy of ``kit_id``: auto-clone leaves this profile alone."""
    if not profile_id or not kit_id:
        return

    def fn(doc: dict) -> None:
        cur = [k for k in doc["removed"].get(profile_id, []) if k != kit_id]
        doc["removed"][profile_id] = [*cur, kit_id]
    _update_state(fn)


def clear_removed(profile_id: str, kit_id: str | None = None) -> None:
    """``kit_id`` added back (or, with None, every kit: Reset profile, a new profile)."""
    def fn(doc: dict) -> None:
        if kit_id is None:
            doc["removed"].pop(profile_id, None)
            return
        left = [k for k in doc["removed"].get(profile_id, []) if k != kit_id]
        if left:
            doc["removed"][profile_id] = left
        else:
            doc["removed"].pop(profile_id, None)
    with _state_lock:
        if kit_id is None and profile_id not in _read_state()["removed"]:
            return
        _update_state(fn)


def removed_for(profile_id: str) -> set[str]:
    with _state_lock:
        return set(_read_state()["removed"].get(profile_id, []))


def _record(copy_id: str, chat_id: str, rev: int, source: str) -> None:
    def fn(doc: dict) -> None:
        doc["delivered"].setdefault(copy_id, {})[chat_id] = {"rev": int(rev), "source": source}
    _update_state(fn)


# --------------------------------------------------------------------------- #
# Kits
# --------------------------------------------------------------------------- #


def kittable(bot: config.Bot | None) -> bool:
    """May ``bot`` carry a kit? A household direct-backend bot (agent, app and legacy API
    bots stay uncopyable, so they cannot be templates either)."""
    if bot is None or bot.scope != "household":
        return False
    from . import characters
    return characters.backend_kind(bot) == "direct"


def get_kit(bot_id: str) -> Kit | None:
    """The kit for ``bot_id`` (shipped and/or household), or None."""
    if not bot_id:
        return None
    hh = load_household(bot_id)
    if bot_id in SHIPPED:
        return Kit(bot_id, SHIPPED[bot_id] + tuple(hh["chats"] if hh else ()), shipped=True,
                   household=hh is not None)
    if hh is None or not kittable(config.get_bot(bot_id)):
        return None
    return Kit(bot_id, tuple(hh["chats"]), household=True)


def all_kits() -> list[Kit]:
    ids = set(SHIPPED)
    with contextlib.suppress(OSError):
        for p in kit_dir().glob("*.yaml"):
            ids.add(p.stem)
    return [k for k in (get_kit(i) for i in sorted(ids)) if k is not None]


def kit_id_of(bot: config.Bot) -> str:
    """The template a personal bot is a copy of ("" for a bot made by hand, a detached copy
    included). A 4.2 copy names it in ``template``; a copy made before 012 is recognised by its
    ``pack`` (Atlas) or ``cloned_from``."""
    if bot.scope != "personal":
        return ""
    if bot.template:
        return bot.template
    if bot.pack == firstrun.ATLAS_PACK:
        return ATLAS
    return bot.cloned_from or ""


def managed(bot: config.Bot) -> bool:
    """Is ``bot`` kept up to date from a template? (a linked copy, a copy with a kit, a template,
    or a household bot that has a kit)."""
    if bot.scope == "personal":
        return bool(bot.template) or get_kit(kit_id_of(bot)) is not None
    return bot.is_template or get_kit(bot.id) is not None


def copies_of(kit_id: str) -> list[config.Bot]:
    return [b for b in config.load_bots() if b.scope == "personal" and kit_id_of(b) == kit_id]


def chats_for(kit: Kit, role: Any) -> list[KitChat]:
    from .contracts.permissions import Role
    if role is Role.BASIC:
        return []
    admin = role is Role.ADMIN
    return [c for c in kit.chats if c.audience == "everyone" or admin]


# --------------------------------------------------------------------------- #
# The picture (the one thing a copy takes from its template as a file)
# --------------------------------------------------------------------------- #


def _hash_file(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    except OSError:
        return ""


def picture_source(template_id: str) -> Path | None:
    """The face file copies of ``template_id`` take their picture from, or None when it has
    none. The Atlas template always hands out the shipped picture (the household Atlas may run
    a rotating avatar pool, which would re-copy pictures to every copy each time it turns)."""
    if template_id == ATLAS:
        src = firstrun.AVATAR_SRC_DIR / "atlas-face.png"
        return src if src.is_file() else None
    tpl = config.get_bot(template_id)
    if tpl is None or not tpl.avatar:
        return None
    face = Path(config.AVATAR_DIR) / tpl.avatar
    return face if face.is_file() else None


def picture_current(copy: config.Bot) -> bool:
    """Does ``copy`` hold exactly the picture its template hands out?"""
    src = picture_source(copy.template)
    mine = Path(config.AVATAR_DIR) / copy.avatar if copy.avatar else None
    if src is None:
        return mine is None or not mine.is_file()
    return bool(mine and mine.is_file()) and _hash_file(mine) == _hash_file(src)


def copy_picture(template_id: str, copy_id: str) -> str:
    """Write the template's picture for ``copy_id`` (its own files: the avatar gate matches by
    stem, so copies never share a name); returns the new face file name ("" = the template has
    none, and the copy's own files are removed)."""
    from . import characters, profiles_service
    folder = Path(config.AVATAR_DIR)
    if template_id == ATLAS:
        face = firstrun.copy_atlas_avatar(copy_id)
    else:
        src = config.get_bot(template_id)
        face = profiles_service._copy_avatar_files(src, copy_id) if src is not None else ""
    keep = set()
    if face:
        ext = Path(face).suffix
        keep = {f"{copy_id}-{r}{ext}" for r in characters.AVATAR_ROLES}
    for role in characters.AVATAR_ROLES:
        for ext in characters.AVATAR_EXTS:
            name = f"{copy_id}-{role}{ext}"
            if name not in keep:
                with contextlib.suppress(OSError):
                    (folder / name).unlink()
    return face


# --------------------------------------------------------------------------- #
# The upgrade from 4.0 and the setup pass
# --------------------------------------------------------------------------- #


def _migrate(doc: dict) -> None:
    """Once per install (4.0 -> 4.1): see the module docstring."""
    from . import accounts
    pending: set[str] = set()
    with contextlib.suppress(OSError, ValueError, TypeError):
        rows = json.loads((Path(config.DATA_DIR) / firstrun.PENDING_NAME).read_text(encoding="utf-8"))
        pending = {str(r.get("bot")) for r in rows if isinstance(r, dict)}
    backfilled = (Path(config.DATA_DIR) / firstrun.BACKFILL_FLAG).exists()
    bots = config.load_bots()
    with_atlas: set[str] = set()
    for b in bots:
        if b.scope != "personal" or kit_id_of(b) != ATLAS:
            continue
        with_atlas.add(b.owner_profile)
        prof = accounts.get_profile(b.owner_profile)
        if prof is None or b.id in pending or not backfilled:
            continue                      # posted by the normal path; legacy ids still dedupe
        kit = Kit(ATLAS, SHIPPED[ATLAS], shipped=True)
        for chat in chats_for(kit, prof.role):
            doc["delivered"].setdefault(b.id, {}).setdefault(
                chat.id, {"rev": chat.rev, "source": f"guide:{b.id}:{chat.file}"})
    # Nobody is marked as having removed Atlas: every Admin/User profile without a copy gets one
    # on the first 4.1 start ("Atlas is cloned every time"; a profile that deleted its copy
    # under 4.0 gets it back once - maintainer 2026-10-25, profiles are expendable in development).
    with contextlib.suppress(OSError):
        (Path(config.DATA_DIR) / firstrun.PENDING_NAME).unlink()
    doc["migrated"] = True


def ensure_migrated() -> None:
    with _state_lock:
        doc = _read_state()
        if doc.get("migrated"):
            return
        try:
            _migrate(doc)
        except Exception:
            log.warning("the 4.1 kit migration failed (retried next start)", exc_info=True)
            return
        _write_state(doc)


def apply_setup() -> bool:
    """The synchronous half of :func:`sync`: the upgrade step, then the copies engine
    (``profiles_service.reconcile_copies``: every entitled profile has its copy, connection
    policy, picture). Returns True when the roster changed. Never raises."""
    from . import profiles_service
    changed = False
    try:
        with profiles_service.LOCK:
            ensure_migrated()
            changed = profiles_service.reconcile_copies()
            _gc()
    except Exception:
        log.warning("copies setup failed (ignored)", exc_info=True)
    return changed


def _gc() -> None:
    from . import accounts
    ids = {b.id for b in config.load_bots()}
    people = {p.id for p in accounts.list_profiles()}

    def fn(doc: dict) -> None:
        for k in [k for k in doc["delivered"] if k not in ids]:
            doc["delivered"].pop(k, None)
        for k in [k for k in doc["removed"] if k not in people]:
            doc["removed"].pop(k, None)
    with _state_lock:
        doc = _read_state()
        if any(k not in ids for k in doc["delivered"]) or any(k not in people for k in doc["removed"]):
            _update_state(fn)


# --------------------------------------------------------------------------- #
# Chats
# --------------------------------------------------------------------------- #


async def _source_exists(db: Any, source: str) -> bool:
    if not source:
        return False
    return bool(await db.source_id_seen(source))


async def deliver_chats(db: Any, deliver: Callable[..., Awaitable[Any]], *,
                        repost_missing_for: str | None = None) -> list[str]:
    """Post every kit chat a copy has not been given (and, for ``repost_missing_for``, the
    ones whose thread is gone). Returns the thread ids made. Never raises."""
    from . import accounts, ownership
    made: list[str] = []
    if not _switched_on():
        return made
    kits = {k.bot_id: k for k in all_kits()}
    delivered = _read_state()["delivered"]
    for bot in config.load_bots():
        kid = kit_id_of(bot)
        kit = kits.get(kid)
        if kit is None:
            continue
        prof = accounts.get_profile(bot.owner_profile)
        if prof is None:
            continue
        mine = delivered.get(bot.id, {})
        for chat in reversed(chats_for(kit, prof.role)):
            try:
                rec = mine.get(chat.id)
                new = rec is None or int(rec.get("rev") or 0) < chat.rev
                repost = False
                if not new:
                    if repost_missing_for != bot.id or await _source_exists(db, str(rec.get("source") or "")):
                        continue
                    repost = True
                source = f"kit:{bot.id}:{chat.id}:{chat.rev}"
                if new:
                    # Posted before the record was written (a crash), or by 4.0 under its own id.
                    if await db.source_id_seen(source):
                        _record(bot.id, chat.id, chat.rev, source)
                        continue
                    legacy = f"guide:{bot.id}:{chat.file}" if chat.file else ""
                    if rec is None and legacy and await db.source_id_seen(legacy):
                        _record(bot.id, chat.id, chat.rev, legacy)
                        continue
                    # The Owner's chats were re-pointed onto his copy by 012 without a kit
                    # source: his own "Meet Atlas" counts as this chat (title match, only for the
                    # protected Admin, only a thread no kit message is in).
                    if rec is None and prof.id == ownership.protected_admin_id():
                        old = await db.unsourced_thread_titled(bot.id, prof.id, chat.title)
                        if old:
                            _record(bot.id, chat.id, chat.rev, f"adopted:{old}")
                            continue
                if repost:
                    source = f"{source}:r{int(time.time() * 1000)}"
                text = chat.text()
                if not text:
                    continue
                if new and rec is not None:
                    # A revised chat: the earlier version's thread goes, unless the owner wrote
                    # in it (then it stays, and the new version is posted beside it).
                    old_thread = await db.untouched_seeded_thread(str(rec.get("source") or ""))
                    if old_thread:
                        await db.delete_thread(old_thread)
                thread = await db.create_thread(bot.id, title=chat.title, owner_profile_id=prof.id)
                try:
                    await deliver(thread.id, text,
                                  metadata={"seeded": True,
                                            "kit": {"bot": kid, "chat": chat.id, "rev": chat.rev}},
                                  source_id=source, dedup_recent_window=False)
                except BaseException:
                    # No empty chat left behind: the next sync posts it whole.
                    with contextlib.suppress(Exception):
                        await db.delete_thread(thread.id)
                    raise
                _record(bot.id, chat.id, chat.rev, source)
                made.append(thread.id)
            except Exception:
                log.warning("could not post kit chat %s to one copy", chat.id, exc_info=True)
    if made:
        log.info("posted %d kit chat(s)", len(made))
    return made


# --------------------------------------------------------------------------- #
# Running it
# --------------------------------------------------------------------------- #

_server: dict[str, Any] = {}
_lock: tuple[Any, asyncio.Lock] | None = None


def bind_server(loop: Any, db: Any, deliver: Callable[..., Awaitable[Any]],
                broadcast: Callable[[], Awaitable[Any]] | None = None) -> None:
    """The running server: :func:`request_sync` from any thread schedules a sync on it."""
    _server.clear()
    _server.update(loop=loop, db=db, deliver=deliver, broadcast=broadcast)


def unbind_server() -> None:
    _server.clear()


def bound() -> tuple[Any, Any] | None:
    db, deliver = _server.get("db"), _server.get("deliver")
    return (db, deliver) if db is not None and deliver is not None else None


def _run_lock() -> asyncio.Lock:
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


async def sync(db: Any, deliver: Callable[..., Awaitable[Any]], *,
               repost_missing_for: str | None = None) -> dict[str, Any]:
    """The whole engine: setup + auto-clone (in a thread), then the chats. Idempotent; never
    raises. Returns ``{"setup_changed": bool, "chats": [thread ids]}``."""
    out: dict[str, Any] = {"setup_changed": False, "chats": []}
    try:
        async with _run_lock():
            out["setup_changed"] = await asyncio.to_thread(apply_setup)
            out["chats"] = await deliver_chats(db, deliver, repost_missing_for=repost_missing_for)
            # A fresh install's claim (the first Admin and its Atlas copy) posts the first-run chat.
            out["chats"] += await firstrun.post_claim_guides(db, deliver)
        broadcast = _server.get("broadcast")
        # request_sync() marks the roster dirty: a copy made / detached / re-shared changes the
        # roster even when no chat was delivered, and its owner's tabs must hear of it.
        dirty = bool(_server.pop("roster_dirty", False))
        if (out["setup_changed"] or out["chats"] or dirty) and broadcast is not None:
            with contextlib.suppress(Exception):
                await broadcast()
    except Exception:
        log.exception("kit sync failed (ignored)")
    return out


def _schedule() -> None:
    if bound() is None:
        return
    task = _server.get("task")
    if task is not None and not task.done():
        _server["dirty"] = True
        return

    async def runner() -> None:
        while True:
            _server["dirty"] = False
            pair = bound()
            if pair is None:
                return
            await sync(*pair)
            if not _server.get("dirty"):
                return
    _server["task"] = asyncio.ensure_future(runner())


def request_sync() -> None:
    """Ask the running server for a sync (coalesced). Safe from any thread; a no-op without a
    server (the next start syncs anyway)."""
    loop = _server.get("loop")
    if loop is None or loop.is_closed():
        return
    _server["roster_dirty"] = True
    try:
        loop.call_soon_threadsafe(_schedule)
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- #
# The Admin's surface
# --------------------------------------------------------------------------- #


def kit_out(bot_id: str) -> dict[str, Any]:
    """What Settings -> Characters shows for one household bot. Counts only: never another
    profile's bots or chats."""
    bot = config.get_bot(bot_id)
    kit = get_kit(bot_id)
    give_to = bot.give_to if bot is not None and not bot.template else None
    chats = []
    for c in (kit.chats if kit else ()):
        chats.append({"id": c.id, "title": c.title, "audience": c.audience, "rev": c.rev,
                      "builtin": c.builtin, "body": "" if c.builtin else c.body})
    meta = [(c.id, c.rev, c.audience, c.title) for c in (kit.chats if kit else ())]
    version = hashlib.sha256(json.dumps([meta, give_to],
                                        sort_keys=True, default=str).encode()).hexdigest()[:16] if kit else ""
    return {"bot_id": bot_id, "copyable": kittable(bot), "managed": kit is not None,
            "shipped": bool(kit and kit.shipped), "auto_clone": give_to == "everyone",
            "give_to": give_to, "chats": chats, "copies": len(copies_of(bot_id)), "version": version,
            "limits": {"chats": MAX_CHATS, "title": MAX_TITLE, "body": MAX_BODY}}


def save_kit(bot_id: str, *, chats: list[dict[str, Any]]) -> None:
    """Write the household kit for ``bot_id`` from the Admin's form. ``chats`` = the household
    chats (the shipped ones are not edited here): ``{id?, title, body, audience, resend}``.
    A changed chat moves to the next ``rev`` (so every copy gets the new version once) unless
    ``resend`` is false. Who gets the bot is ``give_to`` (``PUT /api/characters/{id}/give-to``);
    the 4.1 ``auto_clone`` form field is translated by ``kits_api``. Raises ValueError for a bot
    that cannot carry a kit."""
    if not kittable(config.get_bot(bot_id)):
        raise ValueError("not_copyable")
    old = {c.id: c for c in (load_household(bot_id) or {}).get("chats", [])}
    out: list[KitChat] = []
    seen: set[str] = set()
    for raw in chats[:MAX_CHATS]:
        cid = str(raw.get("id") or "")
        title = str(raw.get("title") or "").strip()[:MAX_TITLE]
        body = str(raw.get("body") or "")[:MAX_BODY]
        audience = raw.get("audience") if raw.get("audience") in AUDIENCES else "everyone"
        prev = old.get(cid)
        if prev is None:
            cid = "c" + secrets.token_hex(4)
            rev = 1
        else:
            changed = (prev.title, prev.body, prev.audience) != (title, body, audience)
            rev = prev.rev + 1 if changed and raw.get("resend", True) else prev.rev
        if cid in seen:
            continue
        seen.add(cid)
        out.append(KitChat(cid, title, audience, rev, body=body))
    save_household(bot_id, chats=out)
