"""Characters: the 3.0 name for what 2.x called a bot, and the operations on them.

A character is a persona: who it is (name, emoji, avatar, personality), what answers for it (a
Connection and model, or an agent) and how it sounds (a voice reference the voice add-on
validates). Who may use it is :func:`may_use` (4.0: scope, the profile's ``household_bots``).

Storage did not move. ``config.yaml`` stays the roster and ``config.Bot`` the record, so every
existing bot, preset, avatar and pool is a character with no data migration to get wrong; what
changed is the vocabulary and the API (``/api/characters``; ``/api/bots*`` keep working as thin
aliases for one release). What the split adds to a record:

``pack``        the starter pack it came from
``description`` a line for the picker
``connection`` / ``model``  are NOT stored on the bot: they are the first entry of its provider
                chain (``advisor.providers[0]`` / ``api``), which is where the turn code already
                reads them, so assigning a Connection cannot disagree with how the turn runs.

The handlers in ``main.py`` (``/api/bots*``) and ``characters_api.py`` (``/api/characters*``) both
call the functions here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from . import (
    accounts,
    apps_loader,
    backends_policy,
    config,
    connections,
    ext_profile_hooks,
    extensions,
    personas,
)
from .config import Bot

log = logging.getLogger("local-chat.characters")


_UNSET: Any = object()


def _hidden_field() -> str:
    """The Bot field the Admin's "Hide" switch writes: ``hidden`` once the roster model has it,
    ``hide_unlocked`` (its 3.x spelling, which migration 010 renames) until then."""
    return "hidden" if "hidden" in Bot.__dataclass_fields__ else "hide_unlocked"


class CharacterError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def backend_kind(b: Bot) -> str:
    """direct (the advisor backend), legacy-api (the older ``api:`` block), app, or agent."""
    if config.is_direct(b):
        return "direct"
    if b.api:
        return "legacy-api"
    if apps_loader.owns_bot(b.id):
        return "app"
    return "agent"


def first_entry(b: Bot) -> dict | None:
    """The provider entry that answers for ``b`` (``advisor.providers[0]``, or ``api``)."""
    if config.is_direct(b):
        return next((p for p in (b.advisor or {}).get("providers") or [] if isinstance(p, dict)), None)
    return b.api or None


def connection_of(b: Bot) -> str:
    e = first_entry(b)
    return str((e or {}).get("connection") or "")


def model_of(b: Bot) -> str:
    return str((first_entry(b) or {}).get("model") or "")


def personality_of(b: Bot) -> dict:
    kind = personas.kind(b)
    text, custom = personas.current(b)
    preset = None
    if kind != "none":
        if not custom:
            preset = personas.DEFAULT_ID
        else:
            norm = text.strip()
            preset = next((p.id for p in personas.presets() if p.text.strip() == norm), "custom")
    return {"kind": kind, "preset": preset, "custom": bool(custom and preset == "custom"), "text": text}


def bot_row(b: Bot) -> dict:
    """One bot as the Bots tab / Connect panel see it (full-access routes only).

    ``backend`` says which engine answers: ``direct``, ``legacy-api``, ``app`` or ``agent``.
    No key and no base URL ever leave through here - that is what makes the row safe to return
    from the write routes too."""
    backend = backend_kind(b)
    row = {**b.to_dict(), "voice": b.voice, "voice_rp": b.voice_rp, "backend": backend,
           "tools": config.direct_tools_on(b) if backend == "direct" else False,
           "connected": config.direct_connected(b)}
    row.update(personas.row_fields(b))
    if backend == "direct":
        first = first_entry(b)
        row["provider"] = str((first or {}).get("provider") or "")
        row["model"] = str((first or {}).get("model") or "")
    row["connection"] = connection_of(b)
    row["description"] = b.description
    row["pack"] = b.pack
    return row


def character_out(b: Bot) -> dict:
    """The character as ``/api/characters`` returns it: the manifest shape
    (``contracts/manifests/character``) plus what the Settings page needs."""
    kind = backend_kind(b)
    row = bot_row(b)
    row["backend"] = {"kind": {"direct": "direct", "legacy-api": "direct", "agent": "agent",
                               "app": "agent"}[kind],
                      "connection": connection_of(b) or None, "model": model_of(b) or None,
                      "agent": (b.agent or None)}
    row["backend_kind"] = kind
    row["personality"] = personality_of(b)
    row["voice_link"] = {"section": "voice", "anchor": "voices", "character": b.id}
    row["flags"] = {"reactions": b.reactions, "reaction_autopilot": b.reaction_autopilot,
                    "avatar_pool": b.avatar_pool, "image_jobs": b.image_jobs,
                    "quick_actions": b.quick_actions, "hidden": is_hidden(b)}
    return row


# --------------------------------------------------------------------------- #
# Who may use a character (4.0, BUILD-PLAN-v2 §1.2; WP-CORE owns this region)
# --------------------------------------------------------------------------- #


def is_personal(b: Bot) -> bool:
    """A personal character belongs to exactly one profile (``scope: personal``)."""
    return getattr(b, "scope", "household") == "personal"


def is_hidden(b: Bot) -> bool:
    """The Admin's "keep it off the rail" flag (``hidden``). Display only: a hidden character
    still opens by link. Migration 010 wrote ``hidden`` for every 3.x ``hide_unlocked`` bot;
    the legacy ``hide_unlocked`` / ``safe`` / ``audience`` keys are inert data since then and
    are never read here (an ``audience: restricted`` left in config.yaml hides nothing)."""
    return bool(getattr(b, "hidden", False))


def is_agent_backed(b: Bot) -> bool:
    """Its turns run an agent (or an app's agent) on the host."""
    return backend_kind(b) in ("agent", "app")


def _household_list(principal) -> str | list[str]:
    prof = getattr(principal, "profile", None)
    if prof is None and getattr(principal, "profile_id", None):
        prof = accounts.get_profile(principal.profile_id)
    hb = getattr(prof, "household_bots", "all") if prof is not None else "all"
    return hb if hb == "all" else list(hb or [])


def may_use(b: Bot | None, principal) -> bool:
    """May ``principal`` see ``b`` and talk to it? One function, in this order:

    1. an open install (nobody owns it yet): yes;
    2. a machine (the API key): household characters and the protected Admin's personal ones;
    3. a personal character: only its owner (``owner_profile``) - never an Admin;
    4. a household character: an Admin yes; a User when its ``household_bots`` is ``"all"``
       or names the id; a Basic profile only when its list names the id (``"all"`` gives a
       Basic profile nothing: the Admin picks its characters);
    5. an agent-backed household character also needs ``dispatch_agents`` for every
       non-Admin (an app-backed one - the Job Board - is Admin-only).

    No principal, no character: False. ``audience`` / ``safe`` / the 3.x ``profiles`` list
    are inert here (migration 010 folds them into ``household_bots``)."""
    if b is None or principal is None:
        return False
    kind = getattr(principal, "kind", "")
    if kind == "open":
        return True
    if kind == "machine":
        if is_personal(b):
            from . import ownership
            own = ownership.protected_admin_id()
            return bool(own) and b.owner_profile == own
        return True
    if kind != "profile":
        return False
    me = getattr(principal, "profile_id", None)
    if not me and getattr(principal, "protected", False):
        from . import ownership
        me = ownership.protected_admin_id()
    if is_personal(b):
        return bool(me) and bool(b.owner_profile) and b.owner_profile == me
    if getattr(principal, "is_admin", False):
        return True
    role = getattr(principal, "role", None)
    hb = _household_list(principal)
    if role == "basic":
        listed = hb != "all" and b.id in hb
    else:
        listed = hb == "all" or b.id in hb
    if not listed:
        return False
    if is_agent_backed(b):
        if backend_kind(b) == "app":
            return False
        return bool(principal.can("dispatch_agents"))
    return True


def _visible(b: Bot, principal) -> bool:
    if not may_use(b, principal):
        return False
    if not is_hidden(b):
        return True
    # A hidden household character is still listed for a profile whose explicit list names it.
    hb = _household_list(principal) if getattr(principal, "kind", "") == "profile" else "all"
    return hb != "all" and b.id in hb


def visible_to(bot: Bot, principal) -> bool:
    """May ``principal`` use ``bot`` AND is it listed - not hidden, or the profile's explicit
    household list names it (rails, ``bots`` / ``hello`` frames, ``/api/bots``,
    ``ctx.bots_visible``). Lists: :func:`visible_bots` (rails) and :func:`usable_bots`
    (every bot the principal may use, hidden ones included: the Characters manager)."""
    return _visible(bot, principal)


def visible_bots(principal, bots: list[Bot] | None = None) -> list[Bot]:
    return [b for b in (bots if bots is not None else config.load_bots()) if _visible(b, principal)]


def usable_bots(principal, bots: list[Bot] | None = None) -> list[Bot]:
    """Every character ``principal`` may use (hidden ones included)."""
    return [b for b in (bots if bots is not None else config.load_bots()) if may_use(b, principal)]


def may_use_id(bot_id: str | None, principal) -> bool:
    """:func:`may_use` by id. An unknown id (or none) is False: fail closed."""
    if not bot_id:
        return False
    return may_use(config.resolve_bot(bot_id), principal)


# --------------------------------------------------------------------------- #
# Extension-owned fields (voice, voice_rp)
# --------------------------------------------------------------------------- #


def check_extension_field(field: str, bot_id: str, value: str) -> None:
    """CharacterError unless an extension's validator accepts ``value`` for this extension-owned
    field. With no extension validating it (the voice add-on is not installed) a save may only
    carry "" or the value the character already has, so uninstalling the add-on never breaks the
    Bots tab and a stale value in config.yaml is preserved."""
    fn = extensions.field_validator(field)
    if fn is None:
        bot = config.get_bot(bot_id)
        current = getattr(bot, field, "") if bot is not None else ""
        if value not in ("", current):
            raise CharacterError("voice_addon_missing",
                                 f"{field} needs the voice add-on, which is not installed", 422)
        return
    try:
        fn(value)
    except ValueError as e:
        raise CharacterError("character_invalid", (str(e) or f"Unknown {field}")[:200], 422) from e
    except Exception as e:
        log.exception("bot field validator for %s failed", field)
        raise CharacterError("character_invalid", f"Could not check {field}", 422) from e


# --------------------------------------------------------------------------- #
# Connection assignment
# --------------------------------------------------------------------------- #


def with_connection(b: Bot, connection_id: str, model: str | None = None) -> Bot:
    """``b`` answering through connection ``connection_id`` (and ``model``, or the connection's
    default). Replaces the FIRST provider entry, keeping a fallback chain behind it; the inline
    provider / base_url / model stay beside the ``connection`` reference, readable."""
    c = connections.get(connection_id)
    if c is None:
        raise CharacterError("connection_not_found", "Unknown connection", 404)
    kind = backend_kind(b)
    if kind in ("agent", "app"):
        raise CharacterError(
            "character_invalid",
            "This character is answered by an agent runtime, so it has no connection. "
            "Create a direct character to use a connection.", 422)
    old = first_entry(b) or {}
    mdl = (model if model is not None else "") or str(old.get("model") or "") or c.default_model
    if not mdl:
        raise CharacterError("character_invalid",
                             "Pick a model (run Test on the connection to list what it offers).", 422)
    entry: dict[str, Any] = {k: v for k, v in old.items()
                             if k in ("max_tokens", "read_timeout_s", "extra_body", "system_prompt",
                                      "max_history_chars")}
    entry.update({"provider": c.ctype.provider, "base_url": c.base_url, "model": mdl,
                  "connection": c.id})
    if kind == "legacy-api":
        return replace(b, api=entry, model_hint=mdl)
    adv = dict(b.advisor or {})
    rest = [p for p in (adv.get("providers") or [])[1:] if isinstance(p, dict)]
    adv["providers"] = [entry, *rest]
    hint = b.model_hint
    return replace(b, advisor=adv, model_hint=mdl if hint in ("", "Direct API", model_of(b)) else hint)


def with_model(b: Bot, model: str) -> Bot:
    e = first_entry(b)
    if e is None:
        raise CharacterError("character_invalid", "This character has no model to change.", 422)
    kind = backend_kind(b)
    e2 = {**e, "model": model}
    if kind == "legacy-api":
        return replace(b, api=e2, model_hint=model)
    adv = dict(b.advisor or {})
    provs = [p for p in (adv.get("providers") or []) if isinstance(p, dict)]
    provs[0] = e2
    adv["providers"] = provs
    return replace(b, advisor=adv, model_hint=model)


def refresh_inline_mirrors(conn: connections.Connection) -> int:
    """After a connection's address changes, update the inline copy in every entry that uses it
    (the connection is what answers; the copy is what a 2.x reader would see)."""
    n = 0

    def mutate(b: Bot) -> Bot:
        nonlocal n
        changed = False

        def fix(e: dict) -> dict:
            nonlocal changed
            if str(e.get("connection") or "") == conn.id and (
                    e.get("base_url") != conn.base_url or e.get("provider") != conn.ctype.provider):
                changed = True
                return {**e, "base_url": conn.base_url, "provider": conn.ctype.provider}
            return e
        api = fix(b.api) if b.api else b.api
        adv = b.advisor
        if adv:
            adv = {**adv, "providers": [fix(p) if isinstance(p, dict) else p
                                         for p in adv.get("providers") or []]}
        if changed:
            n += 1
            return replace(b, api=api, advisor=adv)
        return b
    for bid in connections.used_by(conn.id):
        config.update_bot(bid, mutate)
    return n


# --------------------------------------------------------------------------- #
# Create / change / remove
# --------------------------------------------------------------------------- #


def create(*, name: str, backend: str = "direct", id: str | None = None, emoji: str = "",
           agent: str | None = None, model_hint: str = "", quick_actions: bool = True,
           tools: bool | None = None, connection: str | None = None, model: str | None = None,
           description: str = "", pack: str = "",
           persona: str | None = None, color: str = "", voice: str = "", voice_rp: str = "",
           hidden: bool = False) -> Bot:
    bots = config.load_bots()
    taken = {b.id for b in bots}
    if id is not None:
        if id.lower() in {t.lower() for t in taken}:
            raise CharacterError("bot_id_taken", f"A bot with the id {id!r} already exists", 409)
        bot_id = id
    else:
        bot_id = config.derive_bot_id(name, taken)
    kw: dict[str, Any] = {}
    if backend == "direct":
        kw["advisor"] = {"providers": [], "tools": True if tools is None else tools}
    else:
        kw["agent"] = (agent or "").strip()
    bot = Bot(
        id=bot_id, name=name, emoji=emoji or ("🌍" if backend == "direct" else "🤖"),
        model_hint=model_hint or ("Direct API" if backend == "direct" else ""),
        quick_actions=quick_actions, color=color, description=description[:400], pack=pack, **kw)
    if persona is not None and persona.strip():
        try:
            bot = personas.apply(bot, persona)
        except ValueError as e:
            raise CharacterError("character_invalid", str(e), 422) from e
    for fld, val in (("voice", voice), ("voice_rp", voice_rp)):
        if val:
            check_extension_field(fld, bot_id, val.strip())
    if voice or voice_rp:
        bot = replace(bot, voice=voice.strip()[:80], voice_rp=voice_rp.strip()[:32])
    if hidden:
        bot = replace(bot, **{_hidden_field(): True})
    if connection:
        bot = with_connection(bot, connection, model)
    return config.upsert_bot(bot)


def patch(bot_id: str, changes: dict[str, Any]) -> Bot:
    """Apply a PATCH body (already validated for shape) under the roster lock.

    HOUSEHOLD characters only (an Admin's surface): a personal one answers 404, exactly as if
    it did not exist - an Admin cannot see or edit another profile's bot. The owner's route is
    :func:`patch_personal`."""
    from fastapi import HTTPException  # the old handler raised these inside the lock

    existing = config.get_bot(bot_id)
    if existing is not None and existing.scope == "personal":
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    changes = dict(changes)
    if "hidden" in changes:                                  # the Admin's "Hide"
        want = bool(changes.pop("hidden"))
        changes[_hidden_field()] = want
        if not want:
            # 010 keeps the 3.x hide_unlocked beside hidden as inert data; showing a bot
            # again clears it too, so the file never says two things.
            changes["hide_unlocked"] = False
    for fld in extensions.BOT_FIELDS:
        if changes.get(fld) is not None:
            check_extension_field(fld, bot_id, str(changes[fld]).strip())
    if "description" in changes:
        changes["description"] = str(changes["description"] or "")[:400]
    connection = changes.pop("connection", _UNSET)         # None / "" detaches; absent leaves it alone
    model = changes.pop("model", None)
    personality = changes.pop("personality", None)

    def _apply(bot: Bot) -> Bot:
        ch = dict(changes)
        tools = ch.pop("tools", None)
        if "tools" in changes:
            if not config.is_direct(bot):
                raise HTTPException(422, "tools only applies to direct-API bots")
            ch["advisor"] = config.set_direct_tools(bot.advisor, bool(tools))
        persona = ch.pop("persona", None)
        for fld in ("voice", "voice_rp"):
            if fld in ch:
                ch[fld] = str(ch[fld]).strip()[:80 if fld == "voice" else 32]
        updated = replace(bot, **ch)
        if personality is not None:
            if personality == personas.DEFAULT_ID:
                persona = ""
            else:
                preset = next((p for p in personas.presets() if p.id == personality), None)
                if preset is None:
                    raise CharacterError("character_invalid", f"Unknown personality {personality!r}", 422)
                persona = preset.text
        if persona is not None:
            try:
                updated = personas.apply(updated, persona)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        if connection is not _UNSET:
            updated = with_connection(updated, connection, model) if connection else _detach(updated)
        elif model is not None:
            updated = with_model(updated, model)
        return updated

    saved = config.update_bot(bot_id, _apply)
    if saved is None:
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    return saved


def _detach(b: Bot) -> Bot:
    """Drop the ``connection`` reference, keeping the inline settings (the key is not copied back:
    re-enter it in the Connect panel if the character should stand alone)."""
    def strip(e: dict) -> dict:
        return {k: v for k, v in e.items() if k != "connection"}
    if b.api:
        return replace(b, api=strip(b.api))
    if b.advisor:
        adv = dict(b.advisor)
        adv["providers"] = [strip(p) if isinstance(p, dict) else p for p in adv.get("providers") or []]
        return replace(b, advisor=adv)
    return b


def remove(bot_id: str) -> None:
    """Remove a HOUSEHOLD character (a personal one is 404 here; its owner uses
    :func:`remove_personal`)."""
    existing = config.get_bot(bot_id)
    if existing is None or existing.scope == "personal":
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    if apps_loader.owns_bot(bot_id):
        raise CharacterError("bot_owned_by_app",
                             "This bot belongs to an app; remove or disable the app instead", 409)
    try:
        config.remove_bot(bot_id)
    except ValueError as e:
        raise CharacterError("character_last", str(e), 409) from e


# --------------------------------------------------------------------------- #
# Personal characters (4.0): a profile's own bots
# --------------------------------------------------------------------------- #
#
# A personal character is a config.yaml entry with ``scope: personal`` and
# ``owner_profile: <profile id>``. Its id is opaque (``p`` + 12 hex digits) because ids reach logs
# and add-ons and must say nothing about the bot or its owner. Direct backend only: a personal
# bot never reaches an agent runtime, runs no image jobs, keeps no reaction pool and reads no
# knowledge folder. Who may SEE one is decided by ``may_use`` (WP-CORE); everything here is
# the owner's own surface and answers 404 for anybody else's bot, Admins included.

#: The editor's "own text" limit (the global ceiling, personas.MAX_CHARS, is larger).
MAX_PERSONAL_PERSONA = 4000
#: A cap per profile, so a runaway client cannot grow config.yaml without bound.
MAX_PERSONAL_BOTS = 50
#: What the owner's PATCH may carry. Everything else (flags, agent, pools) is household.
PERSONAL_PATCH_FIELDS = frozenset({
    "name", "emoji", "description", "color", "quick_actions", "tools", "persona", "personality",
    "voice", "connection", "model"})
AVATAR_ROLES = ("face", "full", "thumb")
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def new_personal_id(taken: set[str] | None = None) -> str:
    """A fresh opaque personal-bot id, never one already on the roster."""
    used = {t.lower() for t in (taken if taken is not None else {b.id for b in config.load_bots()})}
    while True:
        bid = "p" + secrets.token_hex(6)
        if bid not in used:
            return bid


def personal_bots(profile_id: str) -> list[Bot]:
    """Every personal character ``profile_id`` owns (config order)."""
    return [b for b in config.load_bots()
            if b.scope == "personal" and b.owner_profile == profile_id]


def get_personal(bot_id: str, profile_id: str) -> Bot:
    """``bot_id`` if ``profile_id`` owns it, else the same 404 an unknown bot gets."""
    b = config.get_bot(bot_id)
    if b is None or b.scope != "personal" or not profile_id or b.owner_profile != profile_id:
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    return b


def _personal_text(persona: str | None, personality: str | None) -> str | None:
    """The personality text the editor asked for (None = leave it alone, "" = the default)."""
    if personality:
        if personality == personas.DEFAULT_ID:
            return ""
        preset = next((p for p in personas.presets() if p.id == personality), None)
        if preset is None:
            raise CharacterError("character_invalid", f"Unknown personality {personality!r}", 422)
        return preset.text
    if persona is None:
        return None
    text = str(persona)
    if len(text.strip()) > MAX_PERSONAL_PERSONA:
        raise CharacterError("character_invalid",
                             f"A personality is at most {MAX_PERSONAL_PERSONA} characters", 422)
    return text


def _check_personal_voice(voice: str, bot_id: str, owner: str) -> str:
    """The voice to store: it must pass the add-on's validator AND be one this owner may use
    (a household voice or one of the owner's own clones; BUILD-PLAN-v2 §1.12)."""
    v = str(voice or "").strip()[:80]
    if not v:
        return ""
    check_extension_field("voice", bot_id, v)
    if not ext_profile_hooks.voice_allowed(v, owner):
        raise CharacterError("voice_not_allowed", "That voice is not available to you.", 422)
    return v


def _connect_personal(bot: Bot, profile, connection: str | None, model: str | None, *,
                      may_choose: bool, fallback_ok: bool = False, soft: bool = False) -> Bot:
    """Point a personal bot at a connection ``profile`` may use (see backends_policy).

    ``soft`` (the automatic Atlas and copies): a connection with no model to offer leaves the
    bot unconnected instead of failing - it answers with the "not connected" notice."""
    wanted = (connection or "").strip() or None
    if wanted and not may_choose:
        # Without the permission the pick is not honoured: the bot gets the household default.
        if not fallback_ok:
            raise CharacterError("backend_not_allowed",
                                 "Your profile may not choose a bot's backend.", 403)
        wanted = None
    try:
        cid = backends_policy.resolve_connection(profile, wanted, fallback_ok=fallback_ok)
    except backends_policy.BackendsError as e:
        raise CharacterError(e.code, str(e), 404 if e.code == "connection_not_found" else 422) from e
    if not cid:
        return bot
    # A copy that fell back to another connection cannot carry the source's model name over.
    use_model = model if (not wanted or cid == wanted) else None
    try:
        return with_connection(bot, cid, use_model)
    except CharacterError:
        if soft:
            return bot
        raise


def create_personal(profile, *, name: str, emoji: str = "", description: str = "",
                    persona: str | None = None, personality: str | None = None,
                    voice: str = "", connection: str | None = None, model: str | None = None,
                    tools: bool = True, color: str = "", quick_actions: bool = True,
                    cloned_from: str = "", pack: str = "", avatar: str = "",
                    may_choose_backend: bool = True, fallback_ok: bool = False,
                    soft_connection: bool = False) -> Bot:
    """Make a personal character for ``profile`` (an ``accounts.Profile``) and save it.

    Direct backend only. The backend is checked against ``profile.backends.allowed`` and the
    voice against ``ext_profile_hooks.voice_allowed``; both refuse with a 422
    :class:`CharacterError`. Returns the saved :class:`Bot`."""
    name = str(name or "").strip()[:40]
    if not name:
        raise CharacterError("character_invalid", "A bot needs a name", 422)
    if len(personal_bots(profile.id)) >= MAX_PERSONAL_BOTS:
        raise CharacterError("too_many_bots",
                             f"A profile can have at most {MAX_PERSONAL_BOTS} bots", 409)
    bot_id = new_personal_id()
    bot = Bot(
        id=bot_id, name=name, emoji=(emoji or "🌍").strip()[:8], model_hint="Direct API",
        quick_actions=bool(quick_actions), color=str(color or "").strip()[:32],
        description=str(description or "")[:400], pack=pack, avatar=avatar,
        scope="personal", owner_profile=profile.id, cloned_from=cloned_from,
        advisor={"providers": [], "tools": bool(tools)})
    text = _personal_text(persona, personality)
    if text:
        try:
            bot = personas.apply(bot, text)
        except ValueError as e:
            raise CharacterError("character_invalid", str(e), 422) from e
    voice = _check_personal_voice(voice, bot_id, profile.id)
    if voice:
        bot = replace(bot, voice=voice)
    bot = _connect_personal(bot, profile, connection, model, may_choose=may_choose_backend,
                           fallback_ok=fallback_ok, soft=soft_connection)
    return config.upsert_bot(bot)


def patch_personal(bot_id: str, profile, changes: dict[str, Any], *,
                   may_choose_backend: bool = True) -> Bot:
    """Change one of ``profile``'s own characters. Only :data:`PERSONAL_PATCH_FIELDS` are
    accepted; anything else is a 422. An unknown bot or somebody else's is a 404."""
    get_personal(bot_id, profile.id)
    changes = dict(changes)
    bad = sorted(set(changes) - PERSONAL_PATCH_FIELDS)
    if bad:
        raise CharacterError("character_invalid", f"Cannot change {bad[0]!r} on your own bot", 422)
    connection = changes.pop("connection", _UNSET)
    model = changes.pop("model", None)
    persona = changes.pop("persona", None)
    personality = changes.pop("personality", None)
    if persona is not None and personality:
        raise CharacterError("character_invalid", "Send a personality preset or your own text, not both", 422)
    text = _personal_text(persona, personality)
    if "name" in changes:
        changes["name"] = str(changes["name"] or "").strip()[:40]
        if not changes["name"]:
            raise CharacterError("character_invalid", "A bot needs a name", 422)
    if "emoji" in changes:
        changes["emoji"] = str(changes["emoji"] or "").strip()[:8]
    if "description" in changes:
        changes["description"] = str(changes["description"] or "")[:400]
    if "color" in changes:
        changes["color"] = str(changes["color"] or "").strip()[:32]
    if "voice" in changes:
        changes["voice"] = _check_personal_voice(changes["voice"], bot_id, profile.id)
    tools = changes.pop("tools", None)

    def _apply(bot: Bot) -> Bot:
        updated = replace(bot, **changes)
        if tools is not None:
            updated = replace(updated, advisor=config.set_direct_tools(updated.advisor, bool(tools)))
        if text is not None:
            try:
                updated = personas.apply(updated, text)
            except ValueError as e:
                raise CharacterError("character_invalid", str(e), 422) from e
        if connection is not _UNSET or model is not None:
            if connection is _UNSET or not connection:
                if model is not None:
                    updated = with_model(updated, model)
            else:
                updated = _connect_personal(updated, profile, connection, model,
                                            may_choose=may_choose_backend)
        return updated

    saved = config.update_bot(bot_id, _apply)
    if saved is None:
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    return saved


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def drop_bot_assets(bots: list[Bot]) -> None:
    """Delete what a removed personal bot leaves on disk: its avatar triplet and its entry in
    ``voice-acks.yaml``. (Chats are the caller's: ``profiles_service`` deletes them first.)"""
    ids = [b.id for b in bots if b.scope == "personal"]
    if not ids:
        return
    avatar_dir = Path(config.AVATAR_DIR)
    for bid in ids:
        for role in AVATAR_ROLES:
            for ext in AVATAR_EXTS:
                _unlink_quiet(avatar_dir / f"{bid}-{role}{ext}")
        with contextlib.suppress(OSError):
            for stray in avatar_dir.glob(f".{bid}-*.partial"):
                _unlink_quiet(stray)
    _drop_voice_ack_entries(ids, [b.name for b in bots if b.scope == "personal"])


def _drop_voice_ack_entries(bot_ids: list[str], names: list[str]) -> None:
    """Remove ``bots.<id>`` entries from the voice add-on's ``voice-acks.yaml`` (its style
    keys are a bot's id or display name; a name is only dropped when no surviving bot has it).
    Rewritten only when something was removed, atomically and 0600."""
    path = Path(config.DATA_DIR) / "voice-acks.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    bots = doc.get("bots") if isinstance(doc, dict) else None
    if not isinstance(bots, dict):
        return
    surviving = {b.name.lower() for b in config.load_bots() if b.id not in set(bot_ids)}
    drop = {k for k in bots if str(k) in set(bot_ids)}
    drop |= {k for k in bots if str(k).lower() in {n.lower() for n in names}
             and str(k).lower() not in surviving}
    if not drop:
        return
    for k in drop:
        bots.pop(k, None)
    if not bots:
        doc.pop("bots", None)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".voice-acks.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(Path(tmp))
        raise


def remove_personal(bot_id: str, profile_id: str) -> Bot:
    """Remove one of ``profile_id``'s own characters (config entry, avatars, voice-acks entry).
    Its chats are NOT touched here - ``profiles_service.delete_personal`` deletes them first so
    nothing is left pointing at a bot that is gone. Returns the removed bot."""
    bot = get_personal(bot_id, profile_id)
    try:
        config.remove_bot(bot_id)
    except ValueError as e:
        raise CharacterError("character_last", str(e), 409) from e
    drop_bot_assets([bot])
    return bot
