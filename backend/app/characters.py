"""Characters: the 3.0 name for what 2.x called a bot, and the operations on them.

A character is a persona: who it is (name, emoji, avatar, personality), what answers for it (a
Connection and model, or an agent), who may see it (``audience`` plus an optional list of
profiles) and how it sounds (a voice reference the voice add-on validates).

Storage did not move. ``config.yaml`` stays the roster and ``config.Bot`` the record, so every
existing bot, preset, avatar and pool is a character with no data migration to get wrong; what
changed is the vocabulary and the API (``/api/characters``; ``/api/bots*`` keep working as thin
aliases for one release). What the split adds to a record:

``profiles``    profile ids allowed to see it in addition to ``audience`` (empty = everyone the
                audience allows). Owner and Admin always see everything.
``pack``        the starter pack it came from
``description`` a line for the picker
``connection`` / ``model``  are NOT stored on the bot: they are the first entry of its provider
                chain (``advisor.providers[0]`` / ``api``), which is where the turn code already
                reads them, so assigning a Connection cannot disagree with how the turn runs.

The handlers in ``main.py`` (``/api/bots*``) and ``characters_api.py`` (``/api/characters*``) both
call the functions here.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from . import accounts, apps_loader, config, connections, extensions, personas
from .config import Bot

log = logging.getLogger("local-chat.characters")


_UNSET: Any = object()


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
    row["profiles"] = list(b.profiles)
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
                    "quick_actions": b.quick_actions, "hide_unlocked": b.hide_unlocked}
    return row


# --------------------------------------------------------------------------- #
# Who may use a character (4.0, BUILD-PLAN-v2 §1.2; WP-CORE owns this region)
# --------------------------------------------------------------------------- #


def is_personal(b: Bot) -> bool:
    """A personal character belongs to exactly one profile (``scope: personal``)."""
    return getattr(b, "scope", "household") == "personal"


def is_hidden(b: Bot) -> bool:
    """The Admin's "keep it off the rail" flag (``hidden``; 3.x ``hide_unlocked`` until
    migration 010 renames it). Display only: a hidden character still opens by link."""
    return bool(getattr(b, "hidden", False) or getattr(b, "hide_unlocked", False))


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


def visible_to(bot_or_principal, principal_or_bots=None):
    """``visible_to(bot, principal) -> bool``: may use it AND it is listed - not hidden, or
    the profile's explicit household list names it (rails, ``bots`` / ``hello`` frames,
    ``/api/bots``, ``ctx.bots_visible``).

    Compat form (3.x callers): ``visible_to(principal, bots=None) -> list[Bot]`` keeps its 3.x
    meaning, every character the principal may USE (:func:`usable_bots`, hidden ones
    included: the Characters manager lists them); rails call :func:`visible_bots`."""
    if isinstance(bot_or_principal, Bot):
        return _visible(bot_or_principal, principal_or_bots)
    return usable_bots(bot_or_principal, principal_or_bots)


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


def _clean_profiles(ids: Any) -> list[str]:
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise CharacterError("character_invalid", "profiles must be a list of profile ids", 422)
    known = {p.id for p in accounts.list_profiles()}
    bad = [i for i in ids if i not in known]
    if bad:
        raise CharacterError("character_invalid", f"Unknown profile {bad[0]!r}", 422)
    return list(dict.fromkeys(ids))


def create(*, name: str, backend: str = "direct", id: str | None = None, emoji: str = "",
           agent: str | None = None, model_hint: str = "", safe: bool = False, hide_unlocked: bool = False,
           audience: str | None = None, quick_actions: bool = True, visible: bool = True,
           tools: bool | None = None, connection: str | None = None, model: str | None = None,
           description: str = "", profiles: list[str] | None = None, pack: str = "",
           persona: str | None = None, color: str = "", voice: str = "", voice_rp: str = "",
           ) -> Bot:
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
    if audience is None:
        from . import settings_store
        default_aud = settings_store.get("characters.default_audience", "members")
        if not safe and not hide_unlocked and visible and default_aud != "members":
            audience = default_aud
    bot = Bot(
        id=bot_id, name=name, emoji=emoji or ("🌍" if backend == "direct" else "🤖"),
        model_hint=model_hint or ("Direct API" if backend == "direct" else ""),
        quick_actions=quick_actions, color=color, description=description[:400], pack=pack,
        profiles=_clean_profiles(profiles),
        **config.audience_flags(audience, config.fold_visible(visible, safe, hide_unlocked)), **kw)
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
    if connection:
        bot = with_connection(bot, connection, model)
    return config.upsert_bot(bot)


def patch(bot_id: str, changes: dict[str, Any]) -> Bot:
    """Apply a PATCH body (already validated for shape) under the roster lock."""
    from fastapi import HTTPException  # the old handler raised these inside the lock

    changes = dict(changes)
    for fld in extensions.BOT_FIELDS:
        if changes.get(fld) is not None:
            check_extension_field(fld, bot_id, str(changes[fld]).strip())
    if "profiles" in changes:
        changes["profiles"] = _clean_profiles(changes["profiles"])
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
        if "visible" in ch:
            ch.update(config.fold_visible(ch.pop("visible"), ch.get("safe", bot.safe),
                                          ch.get("hide_unlocked", bot.hide_unlocked)))
        if "audience" in ch:
            ch.update(config.audience_flags(ch.pop("audience"), {
                "safe": ch.get("safe", bot.safe),
                "hide_unlocked": ch.get("hide_unlocked", bot.hide_unlocked)}))
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
    if config.get_bot(bot_id) is None:
        raise CharacterError("bot_not_found", "Unknown bot", 404)
    if apps_loader.owns_bot(bot_id):
        raise CharacterError("bot_owned_by_app",
                             "This bot belongs to an app; remove or disable the app instead", 409)
    try:
        config.remove_bot(bot_id)
    except ValueError as e:
        raise CharacterError("character_last", str(e), 409) from e
