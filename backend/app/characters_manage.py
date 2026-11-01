"""The Characters page's service layer (4.2, PLAN-v2 section 4): one row serializer, the manage
list, the unified PATCH, "Who can use it" preview and set, Stop sharing and Share.

Routes live in ``characters_api.py`` (and the owner-side Share in the same file); everything that
decides lives here, so the routes stay thin and the old routes can call the same code.

Row ``kind`` drives the menu: ``template`` (household, direct, handed out as copies), ``copy``
(a linked copy: its owner's, follows a template), ``own`` (made by hand, or a detached copy; a
pre-4.2 household bot reads as ``own`` too) and ``agent`` (an agent / app / legacy-api bot, the
protected Admin's). ``actions`` are computed here from the caller's role and the row's kind, so a
client draws buttons from them and holds no role logic.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from . import accounts, apps_loader, characters, config, kits, ownership, profiles_service, reactions
from .characters import CharacterError
from .profiles_service import ProfileServiceError

log = logging.getLogger("local-chat.characters.manage")

KINDS = ("template", "copy", "own", "agent")

#: What the owner of a copy / own bot may PATCH (the quick toggles included).
OWNED_FIELDS = frozenset(characters.PERSONAL_PATCH_FIELDS)
#: What an Admin may PATCH on a template. No ``agent`` (a template is direct, R1) and no pools or
#: reactions (a template has no chats and copies never inherit them, so there is nothing to set).
TEMPLATE_FIELDS = frozenset({
    "name", "emoji", "model_hint", "description", "quick_actions", "image_jobs", "tools", "persona",
    "personality", "connection", "model", "color", "voice", "voice_rp"})
#: A pre-4.2 household direct bot (not yet a template): the whole household set.
HOUSEHOLD_FIELDS = frozenset({
    "name", "emoji", "model_hint", "description", "quick_actions", "reactions", "reaction_autopilot",
    "avatar_pool", "image_jobs", "tools", "persona", "personality", "connection", "model", "color",
    "agent", "voice", "voice_rp", "hidden"})
#: The protected Admin's agent / app / legacy-api bots: no connection, model or lookups switch
#: (an agent runtime answers; those are direct-only), so a body carrying one is a 422.
AGENT_FIELDS = HOUSEHOLD_FIELDS - {"connection", "model", "tools"}


class ManageError(ProfileServiceError):
    """A refusal; ``code`` / ``status`` as every service error."""


# --------------------------------------------------------------------------- #
# Who is asking
# --------------------------------------------------------------------------- #


def _me(principal) -> str | None:
    return characters._me(principal)


def _profile(principal) -> accounts.Profile | None:
    pid = _me(principal)
    return accounts.get_profile(pid) if pid else None


def can_manage_household(principal) -> bool:
    return bool(principal is not None and principal.can("manage_characters"))


def can_create(principal) -> bool:
    return bool(principal is not None and principal.can("create_characters"))


def kind_of(b: config.Bot) -> str:
    if b.is_template:
        return "template"
    if b.template:
        return "copy"
    if characters.backend_kind(b) != "direct":
        return "agent"
    return "own"


# --------------------------------------------------------------------------- #
# Actions (server-computed booleans)
# --------------------------------------------------------------------------- #


def _editable(b: config.Bot, principal) -> bool:
    """May ``principal`` change this bot's settings? A template (or a pre-4.2 household bot):
    ``manage_characters``. A personal one: its owner, holding ``create_characters`` (a Basic
    owner sees its managed copies read-only)."""
    if principal is None:
        return False
    if characters.is_personal(b):
        return bool(characters.may_manage(b, principal)) and can_create(principal)
    return can_manage_household(principal)


def can_fire_reactions(b: config.Bot) -> bool:
    """Could a ``:react:`` marker from this bot fire anything? Firing resolves through
    :func:`reactions.get` (``main._fire_reaction``): a ready image in the bot's own pool
    (``reactions._iter_ready(bot_id)``) or an entry in the shared pack (``reactions.load()``).
    Read-only: a missing ``reactions.yaml`` is "no pack" here, never seeded by a GET."""
    try:
        if config.REACTIONS_PATH.exists() and reactions.load().reactions:
            return True
        return bool(reactions.BOT_ID_RE.match(b.id or "") and reactions._iter_ready(b.id))
    except Exception:                       # a broken pack or pool must not break the page
        log.debug("reaction stock check failed for %s", b.id, exc_info=True)
        return False


def owns_row(b: config.Bot, principal) -> bool:
    """A personal bot the caller owns (a copy, an own bot or the Owner's agent bot)."""
    me = _me(principal)
    return bool(me and b.scope == "personal" and b.owner_profile == me and not b.is_template)


def actions_of(b: config.Bot, principal, *, copy_of_me: config.Bot | None = None) -> dict[str, bool]:
    kind = kind_of(b)
    edit = _editable(b, principal)
    fields = allowed_fields(b)
    direct = characters.backend_kind(b) == "direct"
    prof = _profile(principal)
    owner_edit = edit and characters.is_personal(b)
    # An agent / app bot (and Sage) survives a wipe, but the Owner may delete it on purpose.
    deliberate = edit and characters.is_protected_personal(b) \
        and _me(principal) == ownership.protected_admin_id() and bool(_me(principal))
    return {
        "edit": edit,
        "details": edit,
        "hide": edit and kind != "template",
        "pictures": edit,
        "lookups": edit and direct,
        "voice": edit,
        "reset": kind == "copy" and owner_edit and bool(kits.kit_id_of(b)) and _template_alive(b),
        "delete": (edit and kind != "agent" and not characters.is_protected_personal(b)
                   and not (kind == "template" and apps_loader.owns_bot(b.id))) or deliberate,
        "stop_sharing": kind == "template" and can_manage_household(principal),
        "share": kind == "own" and direct and owner_edit and can_manage_household(principal),
        "make_copy": kind == "template" and can_create(principal) and prof is not None
        and characters.entitled(prof, b) and copy_of_me is None,
        # 4.3 F1a: the switches and the reorder the old roster had
        "reactions": edit and "reactions" in fields and (bool(b.reactions) or can_fire_reactions(b)),
        "avatar_pool": edit and "avatar_pool" in fields,
        "reorder": owns_row(b, principal),
        "voice_extras": edit and "voice" in fields,
    }


def _template_alive(b: config.Bot) -> bool:
    t = config.get_bot(b.template) if b.template else None
    return t is not None and t.is_template


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

overridden_fields = characters.overridden_fields     # one computation, shared with /api/me/characters


def _kit_chats(kit_id: str) -> int:
    try:
        kit = kits.get_kit(kit_id)
    except Exception:
        return 0
    return len(kit.chats) if kit else 0


def thread_counts() -> dict[tuple[str, str], int]:
    """``{(bot_id, owner_profile_id): number of chats}``; a thread without an owner counts as the
    protected Admin's. Numbers only."""
    own = ownership.protected_admin_id() or ""
    out: dict[tuple[str, str], int] = {}
    with profiles_service._chats() as con:
        for r in con.execute("SELECT bot_id, owner_profile_id, COUNT(*) FROM threads "
                             "GROUP BY bot_id, owner_profile_id"):
            key = (r[0], r[1] or own)
            out[key] = out.get(key, 0) + int(r[2])
    return out


def _copies_table(tpl: config.Bot, bots: list[config.Bot], profs: list[accounts.Profile],
                  counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    rows = []
    for p in profs:
        mine = next((x for x in bots if x.template == tpl.id and x.scope == "personal"
                     and x.owner_profile == p.id), None)
        if mine is not None:
            state, chats = "has", counts.get((mine.id, p.id), 0)
        else:
            state = "removed" if tpl.id in kits.removed_for(p.id) else "none"
            chats = 0
        rows.append({"profile_id": p.id, "state": state, "chats": chats})
    return rows


def manage_row(b: config.Bot, principal, *, bots: list[config.Bot] | None = None,
               profiles: list[accounts.Profile] | None = None,
               counts: dict[tuple[str, str], int] | None = None) -> dict[str, Any]:
    """THE serializer: one bot as the Characters page shows it (PLAN-v2 section 4). Carries no
    key, URL or content; ``copies`` is numbers per profile and only for a template."""
    bots = bots if bots is not None else config.load_bots()
    kind = kind_of(b)
    backend = characters.backend_kind(b)
    direct = backend == "direct"
    me = _me(principal)
    mine_copy = None
    if kind == "template" and me:
        mine_copy = next((x for x in bots if x.template == b.id and x.scope == "personal"
                          and x.owner_profile == me), None)
    tpl = None
    if kind == "copy":
        t = next((x for x in bots if x.id == b.template), None)
        tpl = {"id": t.id, "name": t.name} if t is not None else {"id": b.template, "name": ""}
    kit_id = b.id if kind == "template" else kits.kit_id_of(b)
    row: dict[str, Any] = {
        "id": b.id, "name": b.name, "emoji": b.emoji, "avatar": b.avatar_url, "color": b.color,
        "description": b.description, "kind": kind, "backend_kind": backend, "order": int(b.order),
        "voice": b.voice, "voice_rp": b.voice_rp,
        "connection": characters.connection_of(b), "model": characters.model_of(b),
        "hidden": characters.is_hidden(b) and kind != "template",
        "badges": {
            "voice": bool(b.voice), "pictures": bool(b.image_jobs), "reactions": bool(b.reactions),
            "lookups": bool(direct and config.direct_tools_on(b)),
            "quick_actions": bool(b.quick_actions), "avatar_pool": bool(b.avatar_pool),
            "managed": bool(kind == "copy" and b.template),
            "kit_chats": _kit_chats(kit_id) if kit_id else 0},
        "template": tpl, "overridden": overridden_fields(b),
        "give_to": b.give_to if kind == "template" else None,
        "copies": [], "actions": actions_of(b, principal, copy_of_me=mine_copy),
        "voice_link": {"section": "voice", "anchor": "voices", "character": b.id},
    }
    if kind == "template" and can_manage_household(principal):
        profs = profiles if profiles is not None else accounts.list_profiles()
        row["copies"] = _copies_table(b, bots, profs, counts if counts is not None else thread_counts())
    return row


def _missing_copy_rows(principal, bots, profs, counts) -> list[dict[str, Any]]:
    """For a profile that may not manage the household: a template given to it whose copy it
    deleted, as a ``kind: "template"`` row in ``mine`` with only ``actions.make_copy`` true
    ("Get my copy"). No ``give_to`` and no ``copies`` travel: it is a way back, not a template."""
    prof = _profile(principal)
    if prof is None:
        return []
    out = []
    for b in bots:
        if b.scope != "household" or not b.is_template or characters.backend_kind(b) != "direct":
            continue
        if not characters.entitled(prof, b) or any(
                x.template == b.id and x.scope == "personal" and x.owner_profile == prof.id for x in bots):
            continue
        row = manage_row(b, principal, bots=bots, profiles=profs, counts=counts)
        if not row["actions"]["make_copy"]:
            continue
        row["give_to"], row["copies"] = None, []
        row["actions"] = {k: k == "make_copy" for k in row["actions"]}
        out.append(row)
    return out


def manage_list(principal) -> dict[str, Any]:
    """``GET /api/characters/manage``: who you are, the profiles (Admins), Mine and Household."""
    bots = config.load_bots()
    profs = accounts.list_profiles()
    counts = thread_counts()
    me = _me(principal)
    admin = can_manage_household(principal)
    mine = [manage_row(b, principal, bots=bots, profiles=profs, counts=counts)
            for b in bots if me and b.scope == "personal" and b.owner_profile == me]
    household = ([manage_row(b, principal, bots=bots, profiles=profs, counts=counts)
                  for b in bots if b.scope == "household"] if admin else [])
    if not admin:
        mine += _missing_copy_rows(principal, bots, profs, counts)
    from . import policy
    return {
        "me": {"id": me, "role": getattr(principal, "role", None),
               "protected": bool(getattr(principal, "protected", False)),
               "can_manage_household": admin, "can_create": can_create(principal),
               # where the Brain select reads its connections: every connection for a caller
               # holding manage_connections, else the ones its own bots may use
               "backends_url": ("/api/connections" if principal is not None
                                and principal.can("manage_connections") else "/api/me/backends")},
        "profiles": ([{"id": p.id, "name": p.name, "role": p.role.value, "avatar": p.avatar,
                       "default_picture": policy.default_picture(p)} for p in profs]
                     if admin else []),
        "mine": mine, "household": household,
    }


# --------------------------------------------------------------------------- #
# Unified PATCH
# --------------------------------------------------------------------------- #


def _find(bot_id: str, principal) -> config.Bot:
    b = config.get_bot(bot_id)
    if b is None:
        if not can_manage_household(principal):     # as before 4.2: the gate said 403 first
            raise ManageError("permission_denied", "Your profile may not change that character.", 403)
        raise ManageError("bot_not_found", "Unknown bot", 404)
    # Another profile's personal bot does not exist for anyone (the gate says the same).
    if characters.is_personal(b) and not characters.may_manage(b, principal):
        raise ManageError("bot_not_found", "Unknown bot", 404)
    return b


def allowed_fields(b: config.Bot) -> frozenset[str]:
    kind = kind_of(b)
    if kind == "template":
        return TEMPLATE_FIELDS
    if kind == "agent":
        return AGENT_FIELDS
    if not characters.is_personal(b):
        return HOUSEHOLD_FIELDS
    return OWNED_FIELDS


def patch(principal, bot_id: str, changes: dict[str, Any]) -> config.Bot:
    """Change one character, whichever kind it is, with the kind's own allowlist and the
    caller's own permission. A template: ``manage_characters``. A copy or own bot: its owner
    (``create_characters``; a copy writes overrides). An agent bot: its owner, the protected
    Admin. Refusals: 404 (not yours / unknown), 403 (no permission), 422 (field not allowed)."""
    b = _find(bot_id, principal)
    if not _editable(b, principal):
        raise ManageError("permission_denied", "Your profile may not change that character.", 403)
    bad = sorted(set(changes) - allowed_fields(b))
    if bad:
        raise ManageError("character_invalid",
                          f"Cannot change {bad[0]!r} on this character.", 422)
    try:
        if characters.is_personal(b):
            return _patch_personal(principal, b, changes)
        return characters.patch(bot_id, changes)
    except CharacterError as e:
        raise ManageError(e.code, str(e), e.status) from e


def _patch_personal(principal, b: config.Bot, changes: dict[str, Any]) -> config.Bot:
    if kind_of(b) == "agent":
        # R1: only the protected Admin owns these; the household field set applies (SF-1).
        return characters.patch(b.id, changes, allow_personal=True)
    pid = _me(principal)
    saved = profiles_service.update_personal(pid, b.id, changes)
    return saved


# --------------------------------------------------------------------------- #
# Reorder my own bots (4.3 F1a)
# --------------------------------------------------------------------------- #


def reorder(principal, ids: list[str]) -> list[str]:
    """``PUT /api/me/characters/order``: the caller's own personal bots in the order ``ids``
    gives (unlisted ones after, in their current order), in the roster slots they already hold
    (:func:`config.reorder_subset`). Another profile's bot, a template or an unknown id is a 404
    ``bot_not_found``; a duplicate a 422 ``order_invalid``. Returns the caller's ids in order."""
    me = _me(principal)
    if not me:
        raise ManageError("profile_required", "Sign in to a profile first.", 409)
    try:
        return config.reorder_subset(list(ids), me)
    except KeyError as e:
        raise ManageError("bot_not_found", "Unknown bot", 404) from e
    except ValueError as e:
        raise ManageError("order_invalid", "A bot is listed twice.", 422) from e


# --------------------------------------------------------------------------- #
# Give to: preview and set
# --------------------------------------------------------------------------- #


def _template(bot_id: str, principal) -> config.Bot:
    b = config.get_bot(bot_id)
    if b is None or b.scope != "household" or not b.is_template:
        raise ManageError("bot_not_found", "Unknown bot", 404)
    if not can_manage_household(principal):
        raise ManageError("permission_denied", "Your profile may not change that character.", 403)
    return b


def _canon(given: str | list) -> str | list:
    return given if given == "everyone" else sorted(given)


def _actor(principal) -> str:
    """Who a preview token is bound to (H-2): the caller's profile id, or its kind on an open
    install (nobody owns it yet)."""
    return _me(principal) or f"kind:{getattr(principal, 'kind', '')}"


def _ids_of(plan: dict) -> dict[str, list[str]]:
    """The preview's buckets as sorted id lists: the token binds WHO, not only how many."""
    return {k: sorted(v) for k, v in plan.items()}


def _spend(kind: str, token: str | None, key: str, counts: dict, extra: dict, code: str,
           message: str, *, actor: str) -> None:
    """Accept a preview token once (profiles_service's signed, expiring, single-use tokens) and
    only from the principal that previewed it, or refuse with this action's own stale code."""
    try:
        profiles_service._spend(kind, token, key, counts, extra, code, actor=actor)
    except ProfileServiceError as e:
        raise ManageError(code, message, e.status) from e


def give_to_preview(principal, bot_id: str, give_to: Any) -> dict[str, Any]:
    """``{add, remove, detach, token}``: what ticking exactly ``give_to`` does. ``remove`` = a copy
    nobody wrote in (deleted with its guide chats), ``detach`` = a copy with chats (kept as the
    owner's own bot). The token binds the numbers; ``PUT`` spends it."""
    tpl = _template(bot_id, principal)
    plan = profiles_service.give_to_preview(tpl.id, give_to)
    given = profiles_service._check_give_to(give_to)
    token = profiles_service._mint(
        "give_to", tpl.id, {k: len(v) for k, v in plan.items()},
        {"give_to": _canon(given), "ids": _ids_of(plan)}, actor=_actor(principal))
    # "everyone" -> an explicit list: profiles made later no longer get a copy (the page says so).
    return {**plan, "everyone_to_list": bool(tpl.give_to == "everyone" and isinstance(given, list)),
            "token": token}


def set_give_to(principal, bot_id: str, give_to: Any, token: str | None) -> dict[str, Any]:
    tpl = _template(bot_id, principal)
    given = profiles_service._check_give_to(give_to)
    with profiles_service.LOCK:
        plan = profiles_service._plan(tpl, given)
        _spend("give_to", token, tpl.id, {k: len(v) for k, v in plan.items()},
               {"give_to": _canon(given), "ids": _ids_of(plan)}, "give_to_stale",
               "Who gets this bot has changed. Look at the numbers again before confirming.",
               actor=_actor(principal))
        profiles_service.set_give_to(tpl.id, given)
    return manage_row(config.get_bot(tpl.id), principal)


# --------------------------------------------------------------------------- #
# Stop sharing / Share
# --------------------------------------------------------------------------- #


def _copies(tpl_id: str) -> list[config.Bot]:
    return [b for b in config.load_bots() if b.template == tpl_id and b.scope == "personal"]


def stop_sharing_preview(principal, bot_id: str) -> dict[str, Any]:
    """``{detach: [profile ids], token}``: every copy becomes its owner's own bot (chats kept)."""
    tpl = _template(bot_id, principal)
    detach = sorted(b.owner_profile for b in _copies(tpl.id))
    token = profiles_service._mint("stop_sharing", tpl.id, {"detach": len(detach)}, {"detach": detach},
                                   actor=_actor(principal))
    return {"detach": detach, "token": token}


def stop_sharing(principal, bot_id: str, token: str | None) -> dict[str, Any]:
    """Template gone, every copy detached (nobody loses a chat). Returns ``{"mine": Row | None}``:
    the caller's own copy, now a plain own bot, or None when it had none."""
    tpl = _template(bot_id, principal)
    if apps_loader.owns_bot(tpl.id):
        raise ManageError("bot_owned_by_app",
                          "This bot belongs to an app; remove or disable the app instead", 409)
    me = _me(principal)
    with profiles_service.LOCK:
        detach = sorted(b.owner_profile for b in _copies(tpl.id))
        _spend("stop_sharing", token, tpl.id, {"detach": len(detach)}, {"detach": detach},
               "stop_sharing_stale",
               "Who has a copy of this bot has changed. Look at the numbers again before confirming.",
               actor=_actor(principal))
        mine_id = next((b.id for b in _copies(tpl.id) if b.owner_profile == me), None)
        try:
            characters.remove(tpl.id)
        except CharacterError as e:
            raise ManageError(e.code, str(e), e.status) from e
        for p in accounts.list_profiles():
            kits.clear_removed(p.id, tpl.id)
    kits.request_sync()
    mine = config.get_bot(mine_id) if mine_id else None
    return {"mine": manage_row(mine, principal) if mine is not None else None}


def share(principal, bot_id: str) -> dict[str, Any]:
    """An Admin shares one of its own direct bots: a new template (``give_to`` = the sharer) with
    the bot's whole setup, and the original becomes the sharer's linked copy (same id, same chats,
    its own picture). Agent bots, copies and other people's bots are refused."""
    if not can_manage_household(principal):
        raise ManageError("permission_denied", "Only an Admin can share a bot with the household.", 403)
    me = _me(principal)
    if not me:
        raise ManageError("profile_required", "Sign in to a profile first.", 409)
    src = config.get_bot(bot_id)
    if src is None or src.scope != "personal" or src.owner_profile != me:
        raise ManageError("bot_not_found", "Unknown bot", 404)
    if kind_of(src) != "own" or characters.backend_kind(src) != "direct":
        raise ManageError("not_shareable", "Only a bot of your own that runs on a direct "
                                           "connection can be shared.", 422)
    with profiles_service.LOCK, config._ROSTER_LOCK:
        taken = {b.id for b in config.load_bots()}
        tid = config.derive_bot_id(src.name, taken)
        face = profiles_service._copy_avatar_files(src, tid)
        tpl = replace(src, id=tid, scope="household", owner_profile="", template="", overrides=None,
                      resolved_view=None, give_to=[me], avatar=face, cloned_from="", pack="",
                      hidden=False, reactions=False, reaction_autopilot=False, avatar_pool=False)
        config.upsert_bot(tpl)
        ov = {"picture": True} if src.avatar else {}
        config.update_bot(src.id, lambda b: replace(
            b, template=tid, overrides=ov, cloned_from=tid, resolved_view=None))
    kits.request_sync()
    return {"template": manage_row(config.get_bot(tid), principal)}


__all__ = ["can_fire_reactions", "give_to_preview", "kind_of", "manage_list", "manage_row", "patch",
           "reorder", "set_give_to", "share", "stop_sharing", "stop_sharing_preview"]
