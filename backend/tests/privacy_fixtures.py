"""The privacy matrix: fixture install, principals, resources and the expectation rules.

Written for WP-PM from F-REPORT section 6 WITHOUT reading WP-CORE: nothing here imports a
CORE name. It speaks only to public surfaces (HTTP, WebSocket frames, ``accounts``, ``config``,
``database``, ``push``, ``advisor``, ``extensions`` hooks) and to the contracts Foundation
pinned, so it can judge CORE instead of agreeing with it.

The shape
---------
* One install (:func:`build_install`) with ten principals and one resource of every kind per
  resource owner. Every resource body carries a unique sentinel ``SENTINEL-<kind>-<owner>-<8hex>``.
* ONE rule table decides every cell (:func:`expected`): own resource -> allowed (when the role
  holds the permission, else denied), a foreign resource -> 401/403/404, an unsigned caller ->
  401/403, a machine -> only what its scopes allow.
* Two invariants hold for EVERY response, positive or negative: it never contains a sentinel
  owned by somebody the caller may not read (:func:`leaks`), and a denied write leaves the
  foreign resource exactly as it was (:func:`foreign_intact`).

Running it
----------
The modules are collected by a plain ``pytest`` run but skipped unless ``DISPATCH_PRIVACY_MATRIX=1``
(marker ``privacy_matrix``). On the Foundation base many cells fail because enforcement is not
wired yet; that is the point. Run it with::

    cd backend && DISPATCH_PRIVACY_MATRIX=1 uv run pytest tests/test_privacy_*.py -q \\
        --basetemp ~/.cache/cfp/pytest/pm
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from app import accounts, auth, config, main
from app.contracts.permissions import Role, effective_permissions

ENV_FLAG = "DISPATCH_PRIVACY_MATRIX"

#: Applied as ``pytestmark = PRIVACY_MARKS`` by every ``test_privacy_*`` module.
PRIVACY_MARKS = [
    pytest.mark.privacy_matrix,
    pytest.mark.skipif(os.environ.get(ENV_FLAG) != "1",
                       reason=f"privacy matrix is opt-in: set {ENV_FLAG}=1"),
]

# --------------------------------------------------------------------------- #
# Principals
# --------------------------------------------------------------------------- #

#: Every actor. ``A0`` protected Admin, ``A2`` second Admin, ``U1``/``U2`` Users, ``B1`` Basic,
#: ``D`` the default profile reached UNSIGNED (a User), ``N`` an unsigned caller on an install
#: whose lock target is the profile list, ``M`` a machine with the default scopes, ``Mi`` a
#: machine holding only ``machine:inject``.
ACTORS = ("A0", "A2", "U1", "U2", "B1", "D", "N", "M", "Mi")
#: Profiles that own one resource of each kind.
OWNERS = ("A0", "U1", "B1", "D")
#: Actors that are a profile (signed in or the default).
PROFILE_ACTORS = ("A0", "A2", "U1", "U2", "B1", "D")

NAME = {"A0": "Adm0", "A2": "Adm2", "U1": "Usr1", "U2": "Usr2", "B1": "Bas1", "D": "Dflt"}
ROLE = {"A0": "admin", "A2": "admin", "U1": "user", "U2": "user", "B1": "basic", "D": "user"}
PIN = {"A0": "2468", "A2": "1111", "U1": "2222", "U2": "3333", "B1": "4444", "D": None}
#: Granted so a denial between peers is the OWNER rule and not the missing permission.
GRANTS = {"U1": ["dispatch_agents"], "U2": ["dispatch_agents"],
          "B1": ["dispatch_agents", "use_images"]}

LAN = {"A2": ("203.0.113.12", 41002), "U1": ("203.0.113.21", 41003), "U2": ("203.0.113.22", 41004),
       "B1": ("203.0.113.31", 41005), "D": ("203.0.113.41", 41006), "N": ("203.0.113.51", 41007),
       "M": ("203.0.113.61", 41008), "Mi": ("203.0.113.62", 41009)}

PASSWORD = "matrix-pass-1"  # scrub-ok: throwaway fixture credential for a temp install
MACHINE_TOKEN = "tok-privacy-matrix-0123456789abcdef"

HOUSEHOLD_BOT = "h_direct"       # on every list
HOUSEHOLD_OTHER = "h_other"      # not on B1's list
HOUSEHOLD_AGENT = "h_agent"      # an agent-backed household bot (needs dispatch_agents)
HOUSEHOLD_ADVISOR = "h_advisor"  # the household direct bot that has a corpus

KINDS = ("thread", "personal_bot", "file", "task", "image_job", "advisor_request", "studio")

SENTINEL_RE = re.compile(r"SENTINEL-([a-z_]+)-(A0|A2|U1|U2|B1|D)-([0-9a-f]{8})")

DENY = (401, 403, 404)
DENY_UNSIGNED = (401, 403)


def role_of(actor: str) -> Role:
    return Role(ROLE[actor])


def actor_permissions(actor: str, *, lock_target: bool | None = None) -> frozenset[str]:
    """What the role table gives ``actor`` (contracts only; no CORE)."""
    if actor not in PROFILE_ACTORS:
        return frozenset()
    fenced = (actor == "D") if lock_target is None else lock_target
    return effective_permissions(role_of(actor), GRANTS.get(actor, ()), (), lock_target=fenced)


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #


@dataclass
class Res:
    kind: str
    owner: str
    sentinel: str
    ids: dict[str, str] = field(default_factory=dict)
    #: what a by-id read must still show after the foreign probes ran
    intact: dict[str, Any] = field(default_factory=dict)


def _png(extra: bytes = b"") -> bytes:
    """A valid, larger-than-the-10KB-floor PNG (the image providers refuse tiny files)."""
    import io

    from PIL import Image
    im = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue() + extra


class Install:
    """One built install. ``actor`` fixes the security configuration (lock target, machine key)."""

    def __init__(self, world, monkeypatch, actor: str, *, machine_key: bool = False):
        self.world = world
        self.mp = monkeypatch
        self.actor = actor
        self.machine_key = machine_key
        self.tag = secrets.token_hex(4)
        self.sentinels: dict[tuple[str, str], str] = {}
        self.res: dict[tuple[str, str], Res] = {}
        self.pids: dict[str, str] = {}
        self.clients: dict[str, Any] = {}
        self.problems: list[str] = []
        self.base = None            # the A0 client; its portal runs everything
        self.studio_calls: list = []

    # ---- helpers ---------------------------------------------------------- #

    def sent(self, kind: str, owner: str) -> str:
        key = (kind, owner)
        if key not in self.sentinels:
            self.sentinels[key] = f"SENTINEL-{kind}-{owner}-{secrets.token_hex(4)}"
        return self.sentinels[key]

    def run(self, fn: Callable[[], Any]) -> Any:
        return self.base.portal.call(fn)

    def pid_owner(self) -> dict[str, str]:
        return {v: k for k, v in self.pids.items()}

    def owner_of_sentinel(self, text: str) -> list[str]:
        return [m.group(2) for m in SENTINEL_RE.finditer(text)]

    def get(self, kind: str, owner: str) -> Res | None:
        return self.res.get((kind, owner))

    # ---- build ------------------------------------------------------------ #

    def build(self) -> Install:
        from agents_world import sibling
        w = self.world
        self._rebind_mounts()
        self.base = w.create_owner(w.client(), name=NAME["A0"], pin=PIN["A0"])
        self.clients["A0"] = self.base
        for key in ("A2", "U1", "U2", "B1", "D"):
            if key == "D":      # PIN-less User: the People API only allows it once CORE lands
                accounts.create_profile(NAME[key], "user")
                continue
            extra = {"password": PASSWORD} if ROLE[key] == "admin" else {}
            if PIN[key]:
                extra["pin"] = PIN[key]
            w.person(self.base, NAME[key], ROLE[key], **extra)
        for p in accounts.list_profiles():
            for key, name in NAME.items():
                if p.name == name:
                    self.pids[key] = p.id
        assert set(self.pids) == set(NAME), self.pids
        for key, grants in GRANTS.items():
            accounts.update_profile(self.pids[key], grants=list(grants))
        accounts.update_profile(self.pids["B1"], household_bots=[HOUSEHOLD_BOT])
        self._bots()
        # Creator sessions: every owner signs in explicitly (D too, even though its actor client
        # is an UNSIGNED one) so resource creation never depends on enforcement being wired.
        creators: dict[str, Any] = {"A0": self.base}
        for key in ("A2", "U1", "U2", "B1"):
            c = sibling(w, self.base, LAN[key])
            if ROLE[key] == "admin":
                r = w.login(c, profile_id=self.pids[key], password=PASSWORD)
            elif PIN[key]:
                r = w.login(c, profile_id=self.pids[key], pin=PIN[key])
            else:
                r = w.login(c, profile_id=self.pids[key])
            assert r.status_code == 200, (key, r.status_code, r.text)
            creators[key] = c
            self.clients[key] = c
        self._resources(creators)
        self._security(lock_target="default_profile")
        # Actor-facing clients (after the security configuration so the lock target is live).
        self.clients["D"] = sibling(w, self.base, LAN["D"])
        # The default profile has no credential, so its Image Studio picture is made the way a
        # person would: from the device that lands on it. (Foundation cannot sign it in; the
        # missing picture is reported by test_fixture_is_complete, not hidden.)
        self._studio("D", self.clients["D"])
        self.clients["N"] = sibling(w, self.base, LAN["N"])
        for key in ("M", "Mi"):
            c = sibling(w, self.base, LAN[key], origin=False)
            c.headers.update({"X-API-Key": MACHINE_TOKEN})
            self.clients[key] = c
        if self.actor in ("N", "M", "Mi"):
            # INT-1: a request that carries the machine key on a route with no machine scope is
            # not a machine request - the gate treats it like any cookie-less caller, which on a
            # default-profile install IS the shared default profile (SPEC: everyone who reaches
            # the port without signing in lands on it). That sharing is D's own row of the
            # matrix. M and Mi therefore run on a profile-list install, where the only thing a
            # key can buy is the machine surface, so "a machine never reads another profile's
            # data" is asserted without the shared profile standing in for it.
            self._security(lock_target="profile_list")
        return self

    def client(self, actor: str):
        return self.clients[actor]

    def _rebind_mounts(self) -> None:
        """``/media`` and ``/static/avatars`` are mounted at import time on the LIVE directories.
        Point them at this test's directories so the matrix can serve (and never reads) live files."""
        from starlette.staticfiles import StaticFiles
        for route in main.app.routes:
            name = getattr(route, "name", "")
            if name == "media":
                (config.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
                self.mp.setattr(route, "app", StaticFiles(directory=str(config.MEDIA_DIR)))
            elif name == "avatars":
                (config.AVATAR_DIR).mkdir(parents=True, exist_ok=True)
                self.mp.setattr(route, "app", StaticFiles(directory=str(config.AVATAR_DIR)))

    # ---- household bots ----------------------------------------------------- #

    def _bots(self) -> None:
        direct = {"providers": [{"provider": "custom", "base_url": "http://p.invalid/v1", "model": "m"}]}
        api = {"provider": "custom", "base_url": "http://p.invalid/v1", "model": "m"}
        config.upsert_bot(config.Bot(id=HOUSEHOLD_BOT, name="Household One", advisor=dict(direct), api=dict(api)))
        config.upsert_bot(config.Bot(id=HOUSEHOLD_OTHER, name="Household Two", advisor=dict(direct), api=dict(api)))
        config.upsert_bot(config.Bot(id=HOUSEHOLD_AGENT, name="Household Agent"))
        # The household bot that has a knowledge corpus (profile text of the protected Admin).
        corpus = self.world.tmp / "corpus-household"
        (corpus / "notes").mkdir(parents=True, exist_ok=True)
        s_prof = self.sent("corpus", "A0")
        (corpus / "_profile.md").write_text(f"# Profile\n\nThe Admin likes {s_prof}-profile.\n", encoding="utf-8")
        (corpus / "notes" / "n.md").write_text(f"# Note\n\nPrivate note {s_prof}-note about rooftop solar.\n",
                                               encoding="utf-8")
        self.corpus_dir = corpus
        config.upsert_bot(config.Bot(id=HOUSEHOLD_ADVISOR, name="Household Advisor", advisor={
            **direct, "knowledge_dir": str(corpus), "owner": "Adm0"}))
        config._invalidate_bots_cache()

    # ---- resources ---------------------------------------------------------- #

    def _resources(self, creators: dict[str, Any]) -> None:
        for owner in OWNERS:
            self._thread(owner)
            self._file(owner)
            self._task(owner)
            self._image_job(owner)
            self._advisor_request(owner)
            if owner != "B1":                  # a Basic profile owns no personal bot
                self._personal_bot(owner)
        for owner in ("A0", "U1", "B1"):
            self._studio(owner, creators[owner])

    def _thread(self, owner: str) -> None:
        s, pid, db = self.sent("thread", owner), self.pids[owner], self.world.db

        async def go():
            t = await db.create_thread(HOUSEHOLD_BOT, title=f"{s}-title", owner_profile_id=pid)
            mu = await db.add_message(t.id, "user", f"{s}-user-says")
            ma = await db.add_message(t.id, "assistant", f"{s}-assistant-says")
            return t.id, mu.id, ma.id
        tid, mu, ma = self.run(go)
        self.res[("thread", owner)] = Res("thread", owner, s, {"thread": tid, "m_user": mu, "m_asst": ma, "pid": pid},
                                          {"title": f"{s}-title", "m_user": f"{s}-user-says"})

    def _file(self, owner: str) -> None:
        s, pid, db = self.sent("file", owner), self.pids[owner], self.world.db
        config.FILES_DIR.mkdir(parents=True, exist_ok=True)
        ids: dict[str, str] = {}
        for src in ("fileserver", "chat"):
            stored = f"{secrets.token_hex(8)}.txt"
            (config.FILES_DIR / stored).write_text(f"{s}-{src}-body\n", encoding="utf-8")
            row = self.run(lambda stored=stored, src=src: db.add_file(
                f"{s}-{src}.txt", stored, 20, "text/plain", src, owner_profile_id=pid))
            ids[f"file_{src}"] = row["id"]
            ids[f"stored_{src}"] = stored
        self.res[("file", owner)] = Res("file", owner, s, {**ids, "pid": pid})

    def _task(self, owner: str) -> None:
        from app import agents_store
        s, pid = self.sent("task", owner), self.pids[owner]
        row = agents_store.create_task(owner=pid, agent_id="matrix-agent", agent_name=f"{s}-agent",
                                       backend="fake", prompt=f"{s}-prompt", result_text=f"{s}-result",
                                       state="succeeded", spec_json="{}")
        self.res[("task", owner)] = Res("task", owner, s, {"task": row["task_id"], "pid": pid},
                                        {"state": "succeeded"})

    def _image_job(self, owner: str) -> None:
        from app import image_jobs
        s, db = self.sent("image_job", owner), self.world.db
        th = self.res[("thread", owner)]
        jid = secrets.token_hex(8)
        spec = image_jobs.ImageSpec(prompt=f"{s}-spec-prompt").to_json()

        async def go():
            await db.add_image_job(jid, th.ids["m_asst"], th.ids["thread"], HOUSEHOLD_BOT, spec,
                                   state="queued", callback_token=secrets.token_hex(8))
        self.run(go)
        self.res[("image_job", owner)] = Res("image_job", owner, s, {"job": jid, "pid": self.pids[owner]},
                                             {"state": "queued"})

    def _advisor_request(self, owner: str) -> None:
        from app import advisor
        s = self.sent("advisor_request", owner)
        th = self.res[("thread", owner)]
        advisor._ledger = None
        row = advisor.ledger().add(bot_id=HOUSEHOLD_ADVISOR, thread_id=th.ids["thread"], kind="research",
                                   agent="worker", brief=f"{s}-brief", state="done")
        self.res[("advisor_request", owner)] = Res("advisor_request", owner, s,
                                                   {"rid": row["id"], "pid": self.pids[owner]}, {"state": "done"})

    def _personal_bot(self, owner: str) -> None:
        s, pid = self.sent("personal_bot", owner), self.pids[owner]
        bid = "p" + secrets.token_hex(6)
        config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        files = {}
        for part in ("face", "full", "thumb"):
            name = f"{bid}-{part}.png"
            (config.AVATAR_DIR / name).write_bytes(b"\x89PNG\r\n\x1a\n" + f"{s}-{part}".encode() + os.urandom(64))
            files[part] = name
        config.upsert_bot(config.Bot(
            id=bid, name=f"{s}-name", description=f"{s}-description", avatar=files["face"],
            scope="personal", owner_profile=pid,
            advisor={"providers": [{"provider": "custom", "base_url": "http://p.invalid/v1", "model": "m"}]}))
        config._invalidate_bots_cache()
        self.res[("personal_bot", owner)] = Res("personal_bot", owner, s,
                                                {"bot": bid, "face": files["face"], "full": files["full"],
                                                 "thumb": files["thumb"], "pid": pid}, {"name": f"{s}-name"})

    def _studio(self, owner: str, creator) -> None:
        """Image Studio pictures are made through the app's own generate route (a mocked provider), so
        the matrix does not depend on how CORE stores the owner."""
        import httpx

        from app import image_providers
        s = self.sent("studio", owner)
        image_providers.save_config({"provider": "minimax", "api_key": "sk-test-MATRIX-0123456789"})
        body = _png()

        def handler(request):
            return httpx.Response(200, json={
                "id": "t", "data": {"image_base64": [__import__("base64").b64encode(body).decode()]},
                "metadata": {"success_count": 1, "failed_count": 0},
                "base_resp": {"status_code": 0, "status_msg": "success"}})
        self.mp.setattr(image_providers, "transport", httpx.MockTransport(handler))
        r = creator.post("/api/apps/imagestudio/generate",
                         json={"prompt": f"{s}-prompt", "ratio": "1:1", "n": 1})
        if r.status_code != 200:
            self.problems.append(f"Image Studio picture for {owner} could not be made: "
                                 f"HTTP {r.status_code} {r.text[:100]}")
            return
        img = r.json()["images"][0]
        # Put the owner's picture into a chat message so chat-images / targets have something to list.
        th = self.res[("thread", owner)]
        self.run(lambda: self.world.db.add_message(th.ids["thread"], "assistant", f"look {img['url']}",
                                                   media_url=img["url"]))
        self.res[("studio", owner)] = Res("studio", owner, s, {"image": img["id"], "url": img["url"],
                                                               "pid": self.pids[owner]})

    # ---- security configuration -------------------------------------------- #

    def _security(self, lock_target: str | None = None) -> None:
        cfg = auth.load()
        cfg.default_profile = self.pids["D"]
        if lock_target is not None:
            cfg.lock_target = lock_target
        if self.actor in ("M", "Mi") or self.machine_key:
            cfg.api_token = MACHINE_TOKEN
            cfg.machine_scopes = ["machine:inject"] if self.actor == "Mi" else None
        auth._write(cfg)
        auth._bust_cache()


def build_install(world, monkeypatch, actor: str, *, machine_key: bool = False) -> Install:
    return Install(world, monkeypatch, actor, machine_key=machine_key).build()


# --------------------------------------------------------------------------- #
# Leak and integrity invariants
# --------------------------------------------------------------------------- #


def readable_owners(actor: str) -> set[str]:
    """The resource owners whose sentinels ``actor`` may legitimately see in a response."""
    if actor in PROFILE_ACTORS:
        return {actor}
    if actor == "M":
        return {"A0"}          # a machine reads only the protected Admin's
    return set()


def leaks(inst: Install, actor: str, text: str) -> list[str]:
    """Sentinels in ``text`` that belong to somebody ``actor`` may not read."""
    ok = readable_owners(actor)
    return sorted({m.group(0) for m in SENTINEL_RE.finditer(text) if m.group(2) not in ok})


def foreign_intact(inst: Install, actor: str) -> list[str]:
    """Every resource NOT owned by ``actor`` still exists, unchanged (a denied write wrote nothing)."""
    from app import advisor, agents_store
    problems: list[str] = []
    db = inst.world.db
    skip = readable_owners(actor)
    for owner in OWNERS:
        if owner in skip:
            continue
        th = inst.get("thread", owner)
        if th:
            t = inst.run(lambda th=th: db.get_thread(th.ids["thread"]))
            if t is None:
                problems.append(f"{owner}: thread was deleted")
            elif t.title != th.intact["title"]:
                problems.append(f"{owner}: thread title changed to {t.title!r}")
            msg = inst.run(lambda th=th: db.get_message(th.ids["m_user"]))
            if msg is None:
                problems.append(f"{owner}: message was deleted")
            elif msg.content != th.intact["m_user"]:
                problems.append(f"{owner}: message text changed")
        f = inst.get("file", owner)
        if f:
            for src in ("fileserver", "chat"):
                if not (config.FILES_DIR / f.ids[f"stored_{src}"]).exists():
                    problems.append(f"{owner}: {src} file blob was deleted")
                row = inst.run(lambda f=f, src=src: db.get_file(f.ids[f"file_{src}"]))
                if row is None:
                    problems.append(f"{owner}: {src} file row was deleted")
        tk = inst.get("task", owner)
        if tk:
            row = agents_store.get_task(tk.ids["task"])
            if row is None or row["state"] != tk.intact["state"]:
                problems.append(f"{owner}: task changed or vanished ({row and row['state']})")
        ar = inst.get("advisor_request", owner)
        if ar:
            row = advisor.ledger().get(ar.ids["rid"])
            if row is None or row["state"] != ar.intact["state"]:
                problems.append(f"{owner}: advisor request changed or vanished")
        pb = inst.get("personal_bot", owner)
        if pb:
            b = config.get_bot(pb.ids["bot"])
            if b is None or b.name != pb.intact["name"] or b.owner_profile != inst.pids[owner]:
                problems.append(f"{owner}: personal bot changed or vanished")
            for part in ("face", "full", "thumb"):
                if not (config.AVATAR_DIR / pb.ids[part]).exists():
                    problems.append(f"{owner}: personal bot {part} avatar deleted")
    return problems


# --------------------------------------------------------------------------- #
# Probes: one route against one resource
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Probe:
    kind: str                      # the resource kind whose ids fill the path
    name: str
    method: str
    path: str                      # may use {thread} {m_user} {m_asst} {bot} {pid} {task} {rid} ... of the target
    perm: str | None = None        # permission the OWNER needs; None = signed in is enough
    body: Any = None               # dict, or callable(inst, res) -> dict
    query: dict | None = None
    files: Any = None              # callable(inst, res) -> files dict
    reveals: bool = True           # an allowed response contains the resource's sentinel
    own: str = "ok"                # ok = 2xx required for the owner | any = only < 500
    mutates: bool = False
    destroys: bool = False
    machine: str | None = None     # None | read (A0's only) | write (any existing resource)
    inject: bool = False           # also reachable with machine:inject alone
    admin_surface: bool = False    # an Admin may legitimately get 2xx on someone else's id: only leaks are judged
    own_5xx_ok: bool = False       # the owner's call reaches a host service that is absent in tests
    deferred: str = ""             # package that owns this cell if it is not CORE's (xfail instead of fail)
    note: str = ""

    def build(self, inst: Install, res: Res) -> dict:
        ids = {**res.ids}
        kw: dict[str, Any] = {}
        body = self.body(inst, res) if callable(self.body) else self.body
        if body is not None:
            kw["json"] = body
        if self.query:
            kw["params"] = {k: (v.format(**ids) if isinstance(v, str) else v) for k, v in self.query.items()}
        if self.files:
            kw["files"] = self.files(inst, res)
        return {"method": self.method, "url": self.path.format(**ids), **kw}


def _t(name, method, path, **kw) -> Probe:
    return Probe("thread", name, method, path, **kw)


def _files_png(inst, res):
    return {"file": ("pic.png", _png(), "image/png")}


PROBES: tuple[Probe, ...] = (
    # ---- thread + messages ---------------------------------------------------------------- #
    _t("GET thread", "GET", "/api/threads/{thread}", machine="read"),
    _t("GET messages", "GET", "/api/threads/{thread}/messages", machine="read"),
    _t("GET thread avatar", "GET", "/api/threads/{thread}/avatar", reveals=False, own="any", machine="read"),
    _t("GET mood-face", "GET", "/api/messages/{m_asst}/mood-face", reveals=False, own="any"),
    _t("GET export one", "GET", "/api/export", query={"thread_id": "{thread}", "format": "json"}),
    _t("GET export one md", "GET", "/api/export", query={"thread_id": "{thread}", "format": "md"}),
    _t("POST read", "POST", "/api/threads/{thread}/read", reveals=False, mutates=True, own="any", machine="read"),
    _t("POST abort", "POST", "/api/threads/{thread}/abort", reveals=False, mutates=True, own="any", machine="read", own_5xx_ok=True),
    _t("PATCH thread", "PATCH", "/api/threads/{thread}", perm="manage_history", body={"title": "renamed-by-matrix"},
       reveals=False, mutates=True, machine="read"),
    _t("PATCH message", "PATCH", "/api/messages/{m_user}", perm="manage_history",
       body={"content": "edited-by-matrix"}, reveals=False, mutates=True),
    _t("POST feedback", "POST", "/api/messages/{m_asst}/feedback", body={"vote": "up"},
       reveals=False, mutates=True, own="any"),
    _t("POST thread avatar", "POST", "/api/threads/{thread}/avatar", files=_files_png, reveals=False,
       mutates=True, own="any", machine="read"),       # INT-1: machine:avatars route; the protected Admin's only
    _t("POST message (inbound alias)", "POST", "/api/threads/{thread}/messages",
       body={"role": "assistant", "content": "posted-by-matrix"}, reveals=False, mutates=True, own="any",
       machine="write"),
    _t("POST inject by thread", "POST", "/api/inject", body=lambda i, r: {"thread_id": r.ids["thread"],
                                                                         "content": "injected-by-matrix"},
       reveals=False, mutates=True, own="any", machine="write", inject=True),
    _t("GET profile export", "GET", "/api/users/{pid}/export", own="any", admin_surface=True,
       reveals=False),
    _t("DELETE message", "DELETE", "/api/messages/{m_user}", perm="manage_history", reveals=False, destroys=True,
       machine="read"),
    _t("DELETE thread", "DELETE", "/api/threads/{thread}", perm="manage_history", reveals=False, destroys=True,
       machine="read"),
    # ---- files ---------------------------------------------------------------------------- #
    Probe("file", "GET file download (fileserver)", "GET", "/api/files/{file_fileserver}/download", perm="files_read", deferred="WP-DEL"),
    Probe("file", "GET file raw (fileserver)", "GET", "/api/files/{file_fileserver}/raw", perm="files_read",
          deferred="WP-DEL"),
    Probe("file", "GET file download (chat)", "GET", "/api/files/{file_chat}/download", perm=None,
          deferred="WP-DEL", note="the thread owner downloads own source='chat' files regardless of files_read (wave 2)"),
    Probe("file", "GET file raw (chat)", "GET", "/api/files/{file_chat}/raw", perm=None, deferred="WP-DEL"),
    Probe("file", "DELETE file (fileserver)", "DELETE", "/api/files/{file_fileserver}", perm="files_read",
          reveals=False, destroys=True, own="any", deferred="WP-DEL"),
    Probe("file", "DELETE file (chat)", "DELETE", "/api/files/{file_chat}", perm=None, reveals=False,
          destroys=True, own="any", deferred="WP-DEL"),
    # ---- tasks ---------------------------------------------------------------------------- #
    Probe("task", "GET task", "GET", "/api/tasks/{task}", perm="dispatch_agents"),
    Probe("task", "GET task events", "GET", "/api/tasks/{task}/events", perm="dispatch_agents", reveals=False, own="any"),
    Probe("task", "POST task send", "POST", "/api/tasks/{task}/send", perm="dispatch_agents",
          body={"message": "hello"}, reveals=False, mutates=True, own="any"),
    Probe("task", "POST task interrupt", "POST", "/api/tasks/{task}/interrupt", perm="dispatch_agents",
          reveals=False, mutates=True, own="any"),
    Probe("task", "POST task resume", "POST", "/api/tasks/{task}/resume", perm="dispatch_agents",
          body={"message": "go"}, reveals=False, mutates=True, own="any"),
    Probe("task", "POST task cancel", "POST", "/api/tasks/{task}/cancel", perm="dispatch_agents",
          reveals=False, mutates=True, own="any"),
    Probe("task", "POST task release", "POST", "/api/tasks/{task}/release", perm="approve_agent_actions",
          reveals=False, mutates=True, own="any"),
    Probe("task", "POST task dismiss", "POST", "/api/tasks/{task}/dismiss", perm="approve_agent_actions",
          reveals=False, mutates=True, own="any"),
    # ---- image jobs ----------------------------------------------------------------------- #
    Probe("image_job", "GET image job", "GET", "/api/image-jobs/{job}", own="any", machine="read"),
    Probe("image_job", "POST image job callback (no token)", "POST", "/api/image-jobs/{job}/callback",
          reveals=False, own="any", mutates=True, machine="write"),
    # ---- advisor requests ----------------------------------------------------------------- #
    Probe("advisor_request", "POST advisor send", "POST", "/api/advisor/requests/{rid}/send",
          body={"message_id": "card-matrix"}, reveals=False, own="any", mutates=True),
    Probe("advisor_request", "POST advisor dismiss", "POST", "/api/advisor/requests/{rid}/dismiss",
          body={"message_id": "card-matrix"}, reveals=False, own="any", mutates=True),
    # ---- personal bots + avatars ---------------------------------------------------------- #
    # INT-1: the four avatar GETs (and the avatar POST below) are machine:avatars routes, so a
    # machine reads the protected Admin's personal bot's avatar there (machine="read" = A0 only).
    Probe("personal_bot", "GET /api/bots/{id}/avatar", "GET", "/api/bots/{bot}/avatar", own="ok", perm=None, machine="read"),
    Probe("personal_bot", "GET /api/bots/{id}/avatar/full", "GET", "/api/bots/{bot}/avatar/full", machine="read"),
    Probe("personal_bot", "GET /api/bots/{id}/avatar/thumb", "GET", "/api/bots/{bot}/avatar/thumb", machine="read"),
    Probe("personal_bot", "GET /api/bots/{id}/avatar/history", "GET", "/api/bots/{bot}/avatar/history",
          reveals=False, own="any", machine="read"),
    Probe("personal_bot", "GET /api/bots/{id}/models", "GET", "/api/bots/{bot}/models", reveals=False, own="any"),
    Probe("personal_bot", "GET /static/avatars face", "GET", "/static/avatars/{face}"),
    Probe("personal_bot", "GET /static/avatars full", "GET", "/static/avatars/{full}"),
    Probe("personal_bot", "GET /static/avatars thumb", "GET", "/static/avatars/{thumb}"),
    Probe("personal_bot", "GET /api/characters/{id}", "GET", "/api/characters/{bot}", own="any"),
    Probe("personal_bot", "PATCH /api/characters/{id}", "PATCH", "/api/characters/{bot}",
          body={"name": "renamed-by-matrix"}, reveals=False, own="any", mutates=True),
    Probe("personal_bot", "PATCH /api/bots/{id}", "PATCH", "/api/bots/{bot}", body={"name": "renamed-by-matrix"},
          reveals=False, own="any", mutates=True),
    Probe("personal_bot", "POST /api/bots/{id}/avatar", "POST", "/api/bots/{bot}/avatar", files=_files_png,
          reveals=False, own="any", mutates=True, machine="read"),
    Probe("personal_bot", "DELETE /api/characters/{id}", "DELETE", "/api/characters/{bot}", reveals=False,
          own="any", destroys=True),
    Probe("personal_bot", "DELETE /api/bots/{id}", "DELETE", "/api/bots/{bot}", reveals=False, own="any",
          destroys=True),
    # ---- Image Studio --------------------------------------------------------------------- #
    Probe("studio", "POST studio clear (dry run)", "POST", "/api/apps/imagestudio/clear", perm="use_images",
          body=lambda i, r: {"id": r.ids["image"], "order": "newest", "dry_run": True}, reveals=False,
          own="any"),
)

ORDER_KEY = {False: 0, True: 1}


def sorted_probes(kind: str) -> list[Probe]:
    ps = [p for p in PROBES if p.kind == kind]
    return sorted(ps, key=lambda p: (p.destroys, p.mutates))


@dataclass
class Expect:
    outcome: str                   # allow | deny | any
    statuses: tuple[int, ...] = ()
    why: str = ""


def expected(inst: Install, actor: str, owner: str, probe: Probe) -> Expect:
    """The single rule that decides every (actor, owner, probe) cell."""
    if actor == "N":
        return Expect("deny", DENY_UNSIGNED, "unsigned on a profile-list install: sign in first")
    if actor == "Mi":
        if probe.inject:
            return Expect("any", why="inject-only machine on the inject route")
        return Expect("deny", DENY, "a machine holding only machine:inject reaches nothing else")
    if actor == "M":
        if probe.machine == "write":
            return Expect("any", why="a machine writes into any existing thread")
        if probe.machine == "read" and owner == "A0":
            return Expect("any", why="a machine reads the protected Admin's")
        return Expect("deny", DENY, "a machine never reads another profile's data")
    if owner != actor:
        if probe.admin_surface and actor in ("A0", "A2"):
            return Expect("any", why="an Admin management surface: leaks are still judged")
        return Expect("deny", DENY, f"{actor} may not touch {owner}'s {probe.kind}")
    perms = actor_permissions(actor)
    if probe.perm and probe.perm not in perms:
        return Expect("deny", (403, 404), f"{actor} lacks {probe.perm}")
    return Expect("allow" if probe.own == "ok" else "any", why="own resource")


def request(inst: Install, actor: str, probe: Probe, res: Res):
    spec = probe.build(inst, res)
    return inst.client(actor).request(**spec)


def judge(inst: Install, actor: str, owner: str, probe: Probe, res: Res) -> list[tuple[str, bool]]:
    """Run one cell. Returns ``(failure, is_positive)`` pairs (empty = pass); a positive failure
    is an own-resource call that was refused or came back without its sentinel."""
    exp = expected(inst, actor, owner, probe)
    try:
        r = request(inst, actor, probe, res)
    except Exception as e:                                   # a crash is a failure, not a skip
        return [(f"{probe.name}: raised {type(e).__name__}: {str(e)[:160]}", False)]
    body = r.content.decode("utf-8", "replace")
    tag = f"[{actor} -> {owner}'s {probe.kind}] {probe.method} {probe.path} ({probe.name}): HTTP {r.status_code}"
    out: list[tuple[str, bool]] = []
    if r.status_code >= 500 and not (probe.own_5xx_ok and exp.outcome != "deny"):
        out.append((f"{tag}: server error", False))
    lk = leaks(inst, actor, body)
    if lk:
        out.append((f"{tag}: body leaks {lk[:3]}", False))
    if exp.outcome == "deny":
        if r.status_code not in exp.statuses:
            out.append((f"{tag}: expected one of {exp.statuses} ({exp.why})", False))
        if res.sentinel in body:
            out.append((f"{tag}: denied but the response contains the resource sentinel", False))
    elif exp.outcome == "allow":
        if not (200 <= r.status_code < 300):
            out.append((f"{tag}: expected 2xx ({exp.why})", True))
        elif probe.reveals and res.sentinel not in body:
            out.append((f"{tag}: allowed but the response lacks the resource sentinel", True))
    return out


def run_kind(inst: Install, actor: str, kind: str) -> tuple[list[str], list[str]]:
    """Run every probe of ``kind`` for ``actor`` against every owner (foreign first, own last).

    Returns ``(failures, deferred_failures)``."""
    fails: list[str] = []
    deferred: list[str] = []
    probes = sorted_probes(kind)
    mine = actor if actor in OWNERS else None
    owners = [o for o in OWNERS if o != mine] + ([mine] if mine else [])
    for owner in owners:
        res = inst.get(kind, owner)
        if res is None:
            continue
        for probe in probes:
            msgs = judge(inst, actor, owner, probe, res)
            for m, positive in msgs:
                if probe.deferred and positive:
                    deferred.append(f"{m} <deferred: {probe.deferred}>")
                else:
                    fails.append(m)
    fails.extend(f"integrity: {p}" for p in foreign_intact(inst, actor))
    return fails, deferred


def conclude(fails: list[str], deferred: list[str]) -> None:
    """Fail with every cell listed, or xfail when only another package's declared cells failed."""
    if fails:
        raise AssertionError(f"{len(fails)} cell(s) failed:\n  " + "\n  ".join(fails)
                             + (f"\n  (+{len(deferred)} deferred)" if deferred else ""))
    if deferred:
        pytest.xfail("deferred cells (not WP-CORE): " + "; ".join(deferred[:3]))


# --------------------------------------------------------------------------- #
# Small WebSocket helpers shared by the socket modules
# --------------------------------------------------------------------------- #


def drain(ws) -> list[dict]:
    """Every frame queued on ``ws`` up to a ping sentinel."""
    ws.send_json({"type": "ping"})
    got = []
    while True:
        f = ws.receive_json()
        if f.get("type") == "pong":
            return got
        got.append(f)


def broadcast(inst: Install, frame: dict) -> None:
    inst.run(lambda: main.manager.broadcast(frame))


def ids_in(frames: list[dict], ftype: str) -> set[str]:
    return {b["id"] for f in frames if f.get("type") == ftype for b in f.get("bots", []) if isinstance(b, dict)}


def run_async(coro) -> Any:
    return asyncio.run(coro)


def json_or_none(r) -> Any:
    try:
        return r.json()
    except ValueError:
        return None
