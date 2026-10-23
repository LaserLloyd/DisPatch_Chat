"""The first-run wizard, as an API.

A new install walks six steps. The page (``wizard.js``) is a thin skin over these routes, and every
step can be driven by a script or a test:

1. **Owner account** - not a route of its own: ``POST /api/auth/setup`` (loopback, or the one-time
   ``setup_token`` from the server log). The state below reports ``owner`` once it exists.
2. **First connection** - ``POST /api/wizard/detect`` finds StudioForge / LM Studio / Ollama /
   OpenAI-style servers on this computer (and optionally the LAN); ``POST /api/wizard/connection``
   saves one, Tests it, makes it the default and points the default character at it.
3. **Voice (optional)** - the state says whether the voice add-on is installed and where its
   section is; the wizard only links to it.
4. **People (optional)** - ``POST /api/wizard/family`` creates profiles - Users (with their own
   Atlas) and Basic ones (the household bots an Admin picks) - and makes sure the default profile
   exists: "DisPatch", a User with no PIN that the lock lands on. The first Admin is the protected
   one from step 1 and sees the household Atlas.
5. **Network** - ``POST /api/wizard/network``: this computer only (the default) or the home network,
   with the TLS note and the exact flags.
6. **Done** - ``GET /api/wizard`` returns a checklist of next steps; ``POST /api/wizard/finish`` closes it.

``GET /api/wizard`` is public but, before sign-in, answers only whether a wizard is needed; the full
checklist is for ``manage_settings``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from . import (
    accounts,
    characters,
    config,
    connections,
    credentials,
    extensions,
    policy,
    profiles_service,
    settings_store,
)
from .api_common import audit, fail
from .contracts.permissions import Permission, Role, normalise_role

router = APIRouter()

_SETTINGS = Depends(policy.require_permission(Permission.MANAGE_SETTINGS))
_CONN = Depends(policy.require_permission(Permission.MANAGE_CONNECTIONS))
_HOST = Depends(policy.require_permission(Permission.ADMIN_SYSTEM))
_USERS = Depends(policy.require_permission(Permission.MANAGE_USERS))

STEPS = ("owner", "connection", "voice", "family", "network", "done")
OPTIONAL = {"voice", "family"}

_state_writer = settings_store.writer("wizard")


class DetectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosts: list[str] = Field(default_factory=list, max_length=32)
    lan: bool = False


class WizardConnectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(max_length=32)
    name: str = Field(default="", max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    api_key_env: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=200)
    character: str | None = Field(default=None, max_length=48)      # default: the first direct character


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    role: Literal["user", "basic", "member", "restricted"] = "user"     # member/restricted: 3.x words
    pin: str = Field(default="", max_length=128)                        # optional on every profile
    #: Basic only: the household bots this profile gets (default: every bot a Basic profile can use).
    household_bots: list[str] | None = Field(default=None, max_length=100)


class FamilyIn(BaseModel):
    # "ignore", not "forbid": pages from before 4.0 still send the two starter-pack flags,
    # which no longer do anything.
    model_config = ConfigDict(extra="ignore")

    people: list[Person] = Field(default_factory=list, max_length=20)


class NetworkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exposure: Literal["local", "lan"] = "local"
    public_url: str = Field(default="", max_length=2048)
    behind_tls: bool | None = None


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "skipped"] = "skipped"


def _saved() -> dict:
    st = settings_store.get("wizard.state", {})
    return st if isinstance(st, dict) else {}


def _save(**changes: Any) -> dict:
    st = {**_saved(), **changes}
    _state_writer("wizard.state", st)
    return st


def _claimed() -> bool:
    return policy.claimed()


def _has_connection() -> bool:
    return connections.exists_any() or config.api_bot_count() > 0


def needed() -> bool:
    """Does this install still need the wizard? Never for one that is already set up (an upgrade)."""
    st = _saved()
    if st.get("finished"):
        return False
    return not (_claimed() and _has_connection())


def _checklist() -> list[dict]:
    st = _saved()
    skipped = set(st.get("skipped") or [])
    conns = connections.list_all()
    tested = any((c.last_test or {}).get("ok") for c in conns)
    connected = [b for b in config.load_bots() if config.direct_connected(b) or b.api]
    voice = next((e for e in extensions.registry() if e.id == "voice"), None)
    profiles = accounts.list_profiles()
    others = [p for p in profiles if not p.protected]
    owner = accounts.get_owner()
    items = [
        {"id": "owner", "title": "Main Admin account", "done": _claimed(), "optional": False,
         "hint": "Create the main Admin on this computer, or with the setup token from the server log."
         if not _claimed() else ("Give the main Admin a password in Settings > Account."
                                 if owner is not None and not owner.has_password else "")},
        {"id": "connection", "title": "A model connection", "done": (bool(conns) and tested) or bool(connected),
         "optional": False, "hint": "Add a connection and press Test." if not conns else
         ("Press Test on your connection." if not tested else "")},
        {"id": "voice", "title": "Voice", "optional": True, "done": bool(voice and voice.state == "ok"),
         "skipped": "voice" in skipped,
         "hint": "Install the voice add-on from Settings > Voice." if voice is None else
         ("The voice add-on is installed." if voice.state == "ok" else f"The voice add-on is {voice.state}.")},
        {"id": "family", "title": "People", "optional": True, "done": bool(others),
         "skipped": "family" in skipped, "hint": "Add people in Settings > People & Access."},
        {"id": "network", "title": "Who can reach DisPatch", "optional": False,
         "done": bool(st.get("network_chosen")),
         "hint": "Choose this computer only, or the home network."},
        {"id": "backup", "title": "Encrypted backups", "optional": True,
         "done": bool(settings_store.get("system.encrypted_backup_enabled", False)),
         "hint": "Turn on scheduled encrypted backups in Settings > System."},
        {"id": "recovery", "title": "Recovery code saved", "optional": True,
         "done": bool(st.get("recovery_acknowledged")),
         "hint": "A RECOVERY-CODE.txt sits in the data folder; keep a copy somewhere safe."},
    ]
    return items


@router.get("/api/wizard")
async def wizard_state(request: Request):
    pol = policy.of(request)
    base = {"needed": await asyncio.to_thread(needed), "claimed": _claimed()}
    if not (pol.can(Permission.MANAGE_SETTINGS) or pol.principal.kind == "open"):
        return base
    items = await asyncio.to_thread(_checklist)
    nxt = next((i["id"] for i in items if not i["done"] and not i.get("skipped") and not i["optional"]), None)
    return {**base, "finished": bool(_saved().get("finished")), "steps": items, "next": nxt,
            "network": {"exposure": settings_store.get("system.network_exposure"),
                        "reported_host": config.SETTINGS.host, "reported_port": config.SETTINGS.port}}


@router.post("/api/wizard/detect", dependencies=[_CONN])
async def wizard_detect(payload: DetectIn | None = None):
    p = payload or DetectIn()
    found = await connections.discover(hosts=p.hosts, lan=p.lan)
    return {"found": found, "types": connections.types_public()}


@router.post("/api/wizard/connection", dependencies=[_CONN])
async def wizard_connection(request: Request, payload: WizardConnectionIn):
    """Save a connection, Test it, make it the default, and point a character at it."""
    try:
        c = await asyncio.to_thread(
            connections.create, type=payload.type, name=payload.name, base_url=payload.base_url,
            api_key=payload.api_key, api_key_env=payload.api_key_env, default_model=payload.model)
    except connections.ConnectionError_ as e:
        raise fail(e.status, e.code, str(e)) from e
    result = await connections.test_and_record(c)
    c = connections.get(c.id) or c
    out: dict[str, Any] = {"connection": c.public(connections.used_by(c.id)), "test": result,
                           "assigned": None}
    if result.get("ok"):
        if not connections.default_id():
            settings_store.set_many({"connections.default": c.id})
        target = payload.character
        bots = await asyncio.to_thread(config.load_bots)
        if target is None:
            target = next((b.id for b in bots if b.scope == "household" and config.is_direct(b)
                          and not characters.connection_of(b)),
                          None)
        if target:
            try:
                model = payload.model or c.default_model or (result.get("models") or [""])[0]
                bot = await asyncio.to_thread(config.get_bot, target)
                if bot is not None and bot.scope == "household":     # a personal bot is its owner's to set up
                    await asyncio.to_thread(config.update_bot, target,
                                            lambda b: characters.with_connection(b, c.id, model))
                    out["assigned"] = {"character": target, "model": model}
            except characters.CharacterError as e:
                out["assigned_error"] = str(e)
        # Every profile's own Atlas made before there was a connection answers through this one now.
        model = payload.model or c.default_model or (result.get("models") or [""])[0]
        out["personal_connected"] = await asyncio.to_thread(
            profiles_service.connect_unconnected_atlases, c.id, model)
    audit(request, "wizard.connection", c.id, "ok" if result.get("ok") else str(result.get("problem")))
    await asyncio.to_thread(_save, connection_added=True)
    return out


@router.post("/api/wizard/family", dependencies=[_USERS])
async def wizard_family(request: Request, payload: FamilyIn):
    def run() -> dict:
        created = []
        basic_ids = []
        for person in payload.people:
            pin_hash = None
            if person.pin:
                bad = credentials.check_pin(person.pin)
                if bad:
                    raise fail(400, "pin_too_short", bad)
                pin_hash = credentials.hash_secret(person.pin)
            role = normalise_role(person.role)
            try:
                household = (profiles_service.basic_default_household()
                             if person.household_bots is None else person.household_bots) \
                    if role is Role.BASIC else None
                prof = accounts.create_profile(person.name, role, pin_hash=pin_hash,
                                               household_bots=household)
            except accounts.AccountError as e:
                raise fail(e.status, e.code, str(e)) from e
            profiles_service.on_profile_created(prof)          # a User gets its own Atlas
            created.append({"id": prof.id, "name": prof.name, "role": prof.role.value})
            if role is Role.BASIC:
                basic_ids.append(prof.id)
        default = profiles_service.ensure_default_profile()
        return {"created": created, "basic": basic_ids, "default_profile": default.id}

    out = await asyncio.to_thread(run)
    audit(request, "wizard.family", "", f"{len(out['created'])} profiles")
    await asyncio.to_thread(_save, family_done=True)
    return out


@router.post("/api/wizard/network", dependencies=[_HOST])
async def wizard_network(request: Request, payload: NetworkIn):
    notes: list[str] = []
    changes: dict[str, Any] = {"system.network_exposure": payload.exposure}
    if payload.public_url or payload.exposure == "local":
        changes["system.public_url"] = payload.public_url
    if payload.behind_tls is not None:
        changes["system.behind_tls"] = payload.behind_tls
    if payload.exposure == "lan":
        if not _claimed():
            raise fail(409, "wizard_owner_required",
                       "Create the owner account first: DisPatch will not open itself to the network with no lock.")
        notes += [
            "Start DisPatch listening on the network: uvicorn --host 0.0.0.0 --port 8765, or in Docker set BIND_ADDR=0.0.0.0.",
            "Phones and tablets need HTTPS for install-to-home-screen, push notifications and the microphone. "
            "Put it behind Caddy (the compose file has a profile) or Tailscale Serve; see docs/remote-access.md.",
            "Anyone on your network can reach the sign-in screen. Use a strong Admin password, and give the people you share with their own profiles.",
        ]
    else:
        notes += ["DisPatch is only reachable from this computer. Start it with --host 127.0.0.1 (the default)."]
    res = await asyncio.to_thread(settings_store.set_many, changes)
    if res.errors:
        raise fail(422, "settings_invalid", next(iter(res.errors.values())))
    audit(request, "wizard.network", payload.exposure)
    await asyncio.to_thread(_save, network_chosen=True)
    host = config.SETTINGS.host
    wide = host not in ("127.0.0.1", "localhost", "::1")
    mismatch = (payload.exposure == "local" and wide) or (payload.exposure == "lan" and not wide)
    note = ("DisPatch reports it is listening on " + host +
            (", which is wider than 'this computer only'." if wide else
             ", so other devices cannot reach it yet.")) if mismatch else ""
    if _in_container():
        # The image always binds 0.0.0.0 inside its own network namespace (Dockerfile CMD,
        # compose `LOCAL_CHAT_HOST`); who can reach it is decided by the address the port is
        # published on (`-p 127.0.0.1:8765:8765`, `BIND_ADDR` in compose), which the app cannot
        # see. Reporting "wider than this computer only" here was wrong for every Docker install.
        mismatch, note = False, ""
        notes.append("In Docker the container itself listens on 0.0.0.0; the address you publish the port on "
                     "decides who can reach it: BIND_ADDR=127.0.0.1 (compose) or -p 127.0.0.1:8765:8765 for "
                     "this computer only, 0.0.0.0 for your network.")
    return {"exposure": payload.exposure, "reported_host": host, "notes": notes,
            "mismatch": mismatch, "mismatch_note": note}


def _in_container() -> bool:
    return connections.in_container()


@router.post("/api/wizard/step/{step}", dependencies=[_SETTINGS])
async def wizard_step(step: str, payload: StepIn | None = None):
    if step not in STEPS or step == "done":
        raise fail(404, "wizard_step_unknown", "Unknown wizard step")
    p = payload or StepIn()
    st = _saved()
    skipped = set(st.get("skipped") or [])
    if p.status == "skipped":
        if step not in OPTIONAL:
            raise fail(409, "wizard_step_required", "That step cannot be skipped.")
        skipped.add(step)
    else:
        skipped.discard(step)
    extra = {"network_chosen": True} if step == "network" and p.status == "done" else {}
    await asyncio.to_thread(_save, skipped=sorted(skipped), **extra)
    return {"step": step, "status": p.status}


@router.post("/api/wizard/finish", dependencies=[_SETTINGS])
async def wizard_finish(request: Request):
    if not _claimed():
        raise fail(409, "wizard_owner_required", "Create the owner account before finishing.")
    from datetime import UTC, datetime
    await asyncio.to_thread(profiles_service.ensure_default_profile)   # idempotent; skipped steps still get it
    await asyncio.to_thread(_save, finished=True,
                            finished_at=datetime.now(UTC).isoformat(timespec="seconds"))
    audit(request, "wizard.finish", "")
    items = await asyncio.to_thread(_checklist)
    return {"finished": True, "next_steps": [i for i in items if not i["done"] and not i.get("skipped")]}

