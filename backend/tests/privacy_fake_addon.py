"""A fake per-profile-clone voice add-on for the privacy matrix.

It plays the add-on's side of BUILD-PLAN-v2 section 1.12 with a dict instead of a voice
engine: clones have an owner (a profile id or ``household``), the routes enforce the table
using ONLY the host's ``ctx.caller`` / ``ctx.bots_visible`` and the sentinel lives in a clone's
name and its reference recording. What the matrix tests is the HOST half: that the caller it
hands over is right, that its coarse route gate is right, and that sockets, ``voice_allowed``,
``count`` and ``purge`` behave.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

EXTENSION = {"id": "voice", "name": "Fake Clone Voice", "version": "9.9.9", "api": [1, 1],
             "setup": "privacy_fake_addon:setup"}


@dataclass
class State:
    ctx: object = None
    clones: dict[str, dict] = field(default_factory=dict)   # id -> {id, name, owner, ref}
    sockets: list = field(default_factory=list)


STATE = State()


def reset() -> None:
    STATE.ctx = None
    STATE.clones.clear()
    STATE.sockets.clear()


def add_clone(cid: str, name: str, owner: str, ref: bytes) -> None:
    STATE.clones[cid] = {"id": cid, "name": name, "owner": owner, "ref": ref}


def _caller(request: Request):
    return STATE.ctx.caller(request)


def _visible(c, clone) -> bool:
    if clone["owner"] == "household":
        return True
    return c is not None and c.profile_id == clone["owner"]


def _may_write(c, clone) -> bool:
    if c is None:
        return False
    if clone["owner"] == "household":
        return bool(c.is_admin)
    return c.profile_id == clone["owner"]


def voice_access(voice_id: str, bot_owner) -> bool:
    if voice_id.startswith("builtin:") or voice_id == "":
        return True
    clone = STATE.clones.get(voice_id)
    if clone is None:
        return False
    return clone["owner"] == "household" or clone["owner"] == bot_owner


def count(profile_id: str) -> dict:
    return {"voices": sum(1 for c in STATE.clones.values() if c["owner"] == profile_id)}


def purge(profile_id: str) -> int:
    ids = [i for i, c in STATE.clones.items() if c["owner"] == profile_id]
    for i in ids:
        STATE.clones.pop(i)
    return len(ids)


def _pub(c) -> dict:
    return {"id": c["id"], "name": c["name"], "owner": c["owner"]}


async def handler(ws, sock) -> None:
    STATE.sockets.append(sock)
    await ws.accept()
    await ws.send_text(json.dumps({"type": "ready", "profile_id": getattr(sock, "profile_id", None)}))
    with contextlib.suppress(Exception):
        while True:
            await ws.receive_text()


def setup(ctx) -> None:
    STATE.ctx = ctx
    router = APIRouter()

    @router.get("/status")
    def status(request: Request):
        c = _caller(request)
        return {"caller": None if c is None else {"profile_id": c.profile_id, "role": c.role,
                                                  "is_admin": c.is_admin, "voice_lab": c.can("voice_lab"),
                                                  "use_voice": c.can("use_voice")},
                "bots": [b.id for b in ctx.bots_visible(request)]}

    @router.get("/profiles")
    def list_profiles(request: Request):
        c = _caller(request)
        return {"profiles": [_pub(x) for x in STATE.clones.values() if _visible(c, x)]}

    @router.get("/profiles/{pid}")
    def get_profile(request: Request, pid: str):
        clone = STATE.clones.get(pid)
        if clone is None or not _visible(_caller(request), clone):
            raise HTTPException(404, "No such voice")
        return _pub(clone)

    @router.get("/profiles/{pid}/files/{name}")
    def get_file(request: Request, pid: str, name: str):
        clone = STATE.clones.get(pid)
        c = _caller(request)
        if clone is None or not _may_write(c, clone):
            raise HTTPException(404, "No such voice")
        return Response(clone["ref"], media_type="audio/wav")

    @router.post("/profiles")
    def create(request: Request, body: dict):
        c = _caller(request)
        if c is None or c.profile_id is None:
            raise HTTPException(403, "Sign in")
        cid = f"clone-{len(STATE.clones) + 1}-{c.profile_id[:6]}"
        add_clone(cid, str(body.get("name") or "voice"), c.profile_id, b"RIFF")
        return _pub(STATE.clones[cid])

    @router.patch("/profiles/{pid}")
    def patch(request: Request, pid: str, body: dict):
        clone = STATE.clones.get(pid)
        if clone is None or not _may_write(_caller(request), clone):
            raise HTTPException(404, "No such voice")
        clone["name"] = str(body.get("name") or clone["name"])
        return _pub(clone)

    @router.delete("/profiles/{pid}")
    def delete(request: Request, pid: str):
        clone = STATE.clones.get(pid)
        if clone is None or not _may_write(_caller(request), clone):
            raise HTTPException(404, "No such voice")
        STATE.clones.pop(pid)
        return {"ok": True}

    ctx.include_router(router, prefix="/api/voice")
    ctx.thread_websocket("voice", handler, max_per_session=4, max_total=16)
    ctx.register_voice_access(voice_access)
    ctx.register_profile_data(count=count, purge=purge)


def install(app):
    """Load the fake add-on into the running app the way ``mount_all`` does."""
    from app import extensions
    reset()
    ext = extensions.load_one(app, extensions.Candidate("privacy_fake_addon:EXTENSION", "test", "voice"))
    extensions._register(ext)
    return ext


def uninstall(app) -> None:
    from app import extensions
    extensions.unload_all(app)
    reset()
