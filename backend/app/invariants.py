"""The data invariants: I1-I8 of the 4.2 ownership model (PLAN-v2 section 7), I9-I12 from 4.3.

One owner per bot, live templates, no profile sharing a bot or a chat with another. The rules
are enforced where bots and copies are made; this module is the second line: it reads the
roster, the profiles, the thread owners and the kit state, and reports every place the data
disagrees with the rules - a hand edit of config.yaml, a restored backup, a half-finished
migration. It only reads and never repairs, never raises into its caller, and says ids, never
names, chat titles or text (the report is numbers and ids, safe for a machine caller).

=====  =========================================================================================
I1     a template has no threads (nobody chats with a template)
I2     every thread's bot is owned by the thread's owner (a NULL owner is the protected Admin),
       or no longer exists (retired ids such as an old bot stay readable); a household bot with
       threads is only tolerated on an install with no protected Admin yet
I3     an agent / app / legacy-api bot is personal to the protected Admin (or household while the
       install has no protected Admin) and never a template
I4     every ``template`` a copy names exists, is a household bot and is not itself a copy
I5     at most one linked copy per (profile, template), and every profile a template is given to
       has a copy or a recorded deletion of it
I6     ``give_to`` names live profiles
I7     every copy holds its kit's chats on record (skipped while a kit sync is pending)
I8     no personal bot id appears in any profile's ``household_bots``
I9     (4.3) no two connections share type + base_url + key fingerprint (report-only)
I10    (4.3) every enabled agent schedule names a live owner profile and an agent it may use
I11    (4.3) every OIDC link names a live profile; the protected Admin keeps a local credential
I12    (4.3) the default profile (when the lock lands on one) exists, is enabled, is not an Admin
       and has no PIN or password
=====  =========================================================================================

``check()`` is the whole thing; :func:`run_at_start` logs and audits what it finds (the server
calls it after the first kit sync); ``GET /api/health/invariants`` serves it (invariants_api.py).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("local-chat.invariants")

#: code -> one-line statement (the report carries these so a reader needs no other document).
INVARIANTS: dict[str, str] = {
    "I1": "a template has no threads",
    "I2": "every thread's bot is its owner's own bot, or no longer exists",
    "I3": "agent and app bots are personal to the protected Admin and never templates",
    "I4": "every template a copy names exists, is a household bot and is not a copy",
    "I5": "one linked copy per profile and template; every entitled profile has one or a deletion on record",
    "I6": "give_to names live profiles",
    "I7": "every copy has its kit chats on record",
    "I8": "no personal bot id in any household_bots list",
    "I9": "no two connections with the same type, address and key fingerprint",
    "I10": "every enabled schedule names a live owner profile and an agent it may use",
    "I11": "every sign-in link names a live profile; the protected Admin keeps a local credential",
    "I12": "the default profile is enabled, not an Admin, and has no PIN or password",
}


@dataclass(frozen=True)
class Violation:
    code: str
    subject: str            # an id (bot, profile, "bot@profile"); never a name
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "subject": self.subject, "detail": self.detail}


@dataclass
class World:
    """Everything the invariants read, as plain data (so a test can seed any violation)."""
    bots: list[Any] = field(default_factory=list)                 # config.Bot
    profiles: list[Any] = field(default_factory=list)             # accounts.Profile
    admin: str | None = None                                      # the protected Admin's id
    threads: Counter = field(default_factory=Counter)             # (bot_id, owner_id|None) -> n
    delivered: dict[str, set[str]] = field(default_factory=dict)  # copy id -> kit chat ids on record
    removed: dict[str, set[str]] = field(default_factory=dict)    # profile id -> template ids removed
    expected: dict[str, set[str]] = field(default_factory=dict)   # copy id -> kit chat ids it should hold
    kinds: dict[str, str] = field(default_factory=dict)           # bot id -> backend kind
    sync_pending: bool = False
    #: 4.3: True for :func:`snapshot` (the live data). I9-I12 read their own stores
    #: (connections, the agents DB, oidc_links, security.yaml) only when ``live`` is set, or take
    #: seeded rows from ``extra`` ("connections" / "schedules" / "oidc_links" / "lock_target")
    #: so a test can build any case.
    live: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Reading the live data
# --------------------------------------------------------------------------- #


def snapshot(*, sync_pending: bool = False) -> World:
    """The live world. Reads only; a table that is not there yet reads as empty."""
    from . import accounts, characters, config, kits, ownership
    w = World(sync_pending=sync_pending, live=True)
    w.bots = config.load_bots()
    w.profiles = accounts.list_profiles()
    w.admin = ownership.protected_admin_id()
    w.kinds = {b.id: characters.backend_kind(b) for b in w.bots}
    try:
        with accounts.connect() as con:
            rows = con.execute("SELECT bot_id, owner_profile_id, COUNT(*) FROM threads "
                               "GROUP BY bot_id, owner_profile_id").fetchall()
        for bot, owner, n in rows:
            w.threads[(bot, owner)] += n
    except sqlite3.Error:
        log.debug("invariants: no threads table yet", exc_info=True)
    state = kits._read_state()
    w.delivered = {cid: set(v) for cid, v in (state.get("delivered") or {}).items() if isinstance(v, dict)}
    w.removed = {pid: {str(x) for x in v} for pid, v in (state.get("removed") or {}).items()
                 if isinstance(v, list)}
    by_id = {p.id: p for p in w.profiles}
    for b in w.bots:
        if b.scope != "personal" or not b.template or b.owner_profile not in by_id:
            continue
        kit = kits.get_kit(b.template)
        if kit is not None:
            w.expected[b.id] = {c.id for c in kits.chats_for(kit, by_id[b.owner_profile].role)}
    return w


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def _templates(w: World) -> dict[str, Any]:
    return {b.id: b for b in w.bots if b.scope == "household" and not b.template and b.give_to is not None}


def _i1(w: World) -> list[Violation]:
    tpls = _templates(w)
    out = []
    for (bot, _owner), n in sorted(w.threads.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if bot in tpls:
            out.append(Violation("I1", bot, f"{n} thread(s) on a template"))
    return out


def _i2(w: World) -> list[Violation]:
    by_id = {b.id: b for b in w.bots}
    tpls = _templates(w)
    out = []
    for (bot, owner), n in sorted(w.threads.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        b = by_id.get(bot)
        if b is None or bot in tpls:
            continue                    # retired id (readable) / a template (I1 says so)
        who = owner or w.admin
        if b.scope == "personal":
            if who != b.owner_profile:
                out.append(Violation("I2", f"{bot}@{who or 'nobody'}",
                                     f"{n} thread(s) of a profile that does not own this bot"))
        elif w.admin is not None:
            out.append(Violation("I2", f"{bot}@{who or 'nobody'}",
                                 f"{n} thread(s) on a household bot that no profile owns"))
    return out


def _i3(w: World) -> list[Violation]:
    out = []
    for b in w.bots:
        if w.kinds.get(b.id, "direct") == "direct" or b.template:
            continue
        if b.give_to is not None:
            out.append(Violation("I3", b.id, "an agent/app bot is a template"))
        elif w.admin is not None and not (b.scope == "personal" and b.owner_profile == w.admin):
            out.append(Violation("I3", b.id, "an agent/app bot that is not the protected Admin's"))
        elif w.admin is None and b.scope == "personal":
            out.append(Violation("I3", b.id, "a personal agent/app bot on an install with no protected Admin"))
    return out


def _i4(w: World) -> list[Violation]:
    by_id = {b.id: b for b in w.bots}
    out = []
    for b in w.bots:
        if not b.template:
            continue
        t = by_id.get(b.template)
        if t is None:
            out.append(Violation("I4", b.id, f"template {b.template!r} does not exist"))
        elif t.template:
            out.append(Violation("I4", b.id, f"template {b.template!r} is itself a copy"))
        elif t.scope != "household" or t.give_to is None:
            out.append(Violation("I4", b.id, f"{b.template!r} is not a household template"))
    return out


def _i5(w: World) -> list[Violation]:
    out = []
    counts = Counter((b.owner_profile, b.template) for b in w.bots if b.template and b.scope == "personal")
    for (pid, tid), n in sorted(counts.items()):
        if n > 1:
            out.append(Violation("I5", f"{tid}@{pid}", f"{n} copies of one template in one profile"))
    from . import characters
    for t in _templates(w).values():
        for p in w.profiles:
            if not characters.entitled(p, t) or counts.get((p.id, t.id)):
                continue
            if t.id not in w.removed.get(p.id, set()):
                out.append(Violation("I5", f"{t.id}@{p.id}", "given to this profile but no copy and no deletion on record"))
    return out


def _i6(w: World) -> list[Violation]:
    live = {p.id for p in w.profiles}
    out = []
    for t in _templates(w).values():
        if isinstance(t.give_to, list):
            for pid in t.give_to:
                if pid not in live:
                    out.append(Violation("I6", f"{t.id}@{pid}", "give_to names a profile that does not exist"))
    return out


def _i7(w: World) -> list[Violation]:
    from . import firstrun
    if w.sync_pending or not firstrun.guides_enabled():
        return []                       # no kit chat is ever posted with the guides switched off
    out = []
    for cid, want in sorted(w.expected.items()):
        missing = want - w.delivered.get(cid, set())
        if missing:
            out.append(Violation("I7", cid, f"{len(missing)} kit chat(s) not on record"))
    return out


def _i8(w: World) -> list[Violation]:
    personal = {b.id for b in w.bots if b.scope == "personal"}
    out = []
    for p in w.profiles:
        if isinstance(p.household_bots, list):
            for bid in p.household_bots:
                if bid in personal:
                    out.append(Violation("I8", f"{bid}@{p.id}", "a personal bot id in household_bots"))
    return out


# ---- 4.3 A3 I9 begin
def _i9_rows(w: World) -> list[tuple[str, str, str, str]]:
    """(id, type, address, key fingerprint) per connection that holds a key. The live
    connections (read-only) when ``w.live``, else ``w.extra["connections"]``: Connection
    objects or dicts ``{id, type, base_url, key | fingerprint}`` (a test seeds any case)."""
    from . import connections
    if w.live:
        src: list[Any] = connections.list_all_ro()
    else:
        src = list(w.extra.get("connections") or [])
    rows = []
    for c in src:
        if isinstance(c, dict):
            fp = str(c.get("fingerprint") or "") or connections.key_fingerprint(str(c.get("key") or ""))
            cid, ctype, url = str(c.get("id") or ""), str(c.get("type") or ""), str(c.get("base_url") or "")
        else:
            if not c.credential_ref:
                continue
            # the resolved key (a stored secret or the variable's value): two references to
            # one account match whichever way each holds it; a variable that is not set keeps
            # its name as the identity
            fp = connections.key_fingerprint(connections.credential(c)) or (
                "e:" + connections.env_of(c) if connections.env_of(c) else "")
            cid, ctype, url = c.id, c.type, c.base_url
        if fp:
            rows.append((cid, ctype, url.strip().rstrip("/"), fp))
    return rows


def _i9(w: World) -> list[Violation]:
    """Two connections with the same type, address and key: one account stored twice (a 3.0
    adoption re-run after a crash did that before 4.3). Report-only; the subject is the ids,
    never a key or its fingerprint."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for cid, ctype, url, fp in _i9_rows(w):
        groups.setdefault((ctype, url, fp), []).append(cid)
    out = []
    for ids in sorted(groups.values()):
        if len(ids) > 1:
            out.append(Violation("I9", ",".join(sorted(ids)),
                                 f"{len(ids)} connections share type, address and key"))
    return out
# ---- 4.3 A3 I9 end


# ---- 4.3 A8 I10 begin
def _i10(w: World) -> list[Violation]:
    """Every ENABLED schedule names a live owner profile and an existing agent that profile may
    use (enabled, published to it - or it is an Admin - and holding the agent's permission).
    Live data is read through a read-only connection; a test seeds ``w.extra["schedules"]`` and
    ``w.extra["agents"]`` instead."""
    if w.live:
        from . import accounts
        from .agents import schedule_store
        path = accounts.db_path()
        if not path.exists():
            return []
        rows = schedule_store.read_only_enabled(str(path))
        agents = _agents_read_only(str(path)) if rows else {}
    else:
        rows = [r for r in (w.extra.get("schedules") or []) if r.get("enabled", True)]
        agents = {a["id"]: a for a in (w.extra.get("agents") or [])}
    from . import policy
    from .contracts.permissions import Permission
    by_id = {p.id: p for p in w.profiles}
    out = []
    for r in rows:
        sid, owner = str(r.get("id")), r.get("owner_profile")
        prof = by_id.get(owner)
        if prof is None:
            out.append(Violation("I10", sid, "enabled schedule's owner profile does not exist"))
            continue
        agent = agents.get(r.get("agent_id"))
        if agent is None:
            out.append(Violation("I10", sid, "enabled schedule names an agent that does not exist"))
            continue
        pr = policy.profile_principal(prof, via="invariant")
        pub = agent.get("published_to") or []
        need = (agent.get("spec") or {}).get("required_permission") or Permission.DISPATCH_AGENTS.value
        # A switched-off agent is a household-wide pause, not a broken schedule (the loop skips it).
        usable = (pr.is_admin or "*" in pub or prof.id in pub) \
            and pr.can(need) and pr.can(Permission.DISPATCH_AGENTS)
        if not usable:
            out.append(Violation("I10", sid, "enabled schedule's owner may not use its agent"))
    return out


def _agents_read_only(path: str) -> dict[str, dict[str, Any]]:
    import json
    from pathlib import Path
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute("SELECT id, spec, published_to, disabled FROM agents").fetchall()
        except sqlite3.OperationalError:
            return {}
        out = {}
        for r in rows:
            def j(t, d):
                try:
                    v = json.loads(t or "")
                except ValueError:
                    return d
                return v if isinstance(v, type(d)) else d
            out[r["id"]] = {"id": r["id"], "spec": j(r["spec"], {}),
                            "published_to": j(r["published_to"], []), "disabled": bool(r["disabled"])}
        return out
    finally:
        con.close()
# ---- 4.3 A8 I10 end


# ---- 4.3 A13 I11 begin
def _i11(w: World) -> list[Violation]:
    """OIDC links naming dead profiles; a linked protected Admin without a local credential.

    Rows: ``accounts.list_oidc_links()`` when ``w.live``, else ``w.extra["oidc_links"]`` (dicts
    or ``accounts.OidcLink``). The protected Admin's local credential is its password or its PIN
    (the profile row mirrors the install PIN)."""
    if w.live:
        from . import accounts
        rows = [x.to_dict() for x in accounts.list_oidc_links()]
    else:
        rows = [x if isinstance(x, dict) else x.to_dict() for x in (w.extra.get("oidc_links") or [])]
    by_id = {p.id: p for p in w.profiles}
    out: list[Violation] = []
    linked: set[str] = set()
    for r in rows:
        pid = str(r.get("profile_id") or "")
        if pid not in by_id:
            out.append(Violation("I11", pid or "?", "a sign-in link names a profile that does not exist"))
        else:
            linked.add(pid)
    admin = by_id.get(w.admin) if w.admin else next((p for p in w.profiles if p.protected), None)
    if admin is not None and admin.id in linked and not (admin.has_pin or admin.has_password):
        out.append(Violation("I11", admin.id, "the protected Admin has a sign-in link but no PIN or password"))
    return out
# ---- 4.3 A13 I11 end


# ---- 4.3 Fable I12 begin
def _i12(w: World) -> list[Violation]:
    """The shared default profile every unsigned device and every lock lands on: it must exist,
    be enabled, not be an Admin and hold no PIN or password (``auth.check_lock_target`` and the
    ``accounts.update_profile`` guard enforce it; this catches a hand edit or a restored backup).

    The lock target: ``auth.load()`` when ``w.live``, else ``w.extra["lock_target"]``
    (``{"target": ..., "default_profile": ...}``). Nothing to check on the profile list."""
    if w.live:
        from . import auth
        cfg = auth.load()
        target, pid = cfg.lock_target, cfg.default_profile
    else:
        lt = w.extra.get("lock_target") or {}
        target, pid = lt.get("target"), lt.get("default_profile")
    if target != "default_profile" or not pid:
        return []
    prof = next((p for p in w.profiles if p.id == pid), None)
    if prof is None:
        return [Violation("I12", pid, "the default profile does not exist")]
    from .contracts.permissions import Role
    out: list[Violation] = []
    if prof.disabled:
        out.append(Violation("I12", pid, "the default profile is disabled"))
    if prof.role is Role.ADMIN:
        out.append(Violation("I12", pid, "the default profile is an Admin"))
    if prof.has_pin or prof.has_password:
        out.append(Violation("I12", pid, "the default profile has a PIN or password"))
    return out
# ---- 4.3 Fable I12 end


_CHECKS = (_i1, _i2, _i3, _i4, _i5, _i6, _i7, _i8, _i9, _i10, _i11, _i12)


def check(world: World | None = None, *, sync_pending: bool = False) -> list[Violation]:
    """Every violation of I1-I12 in ``world`` (default: the live data), in invariant order."""
    w = world if world is not None else snapshot(sync_pending=sync_pending)
    out: list[Violation] = []
    for fn in _CHECKS:
        if fn in (_i9, _i10, _i11, _i12):
            # 4.3 checks read stores of their own; one that cannot be read reports nothing
            # rather than hiding I1-I8 (the check never raises into its caller).
            try:
                out.extend(fn(w))
            except Exception:
                log.warning("invariant %s could not be checked (ignored)", fn.__name__, exc_info=True)
            continue
        out.extend(fn(w))
    return out


def report(*, sync_pending: bool = False) -> dict[str, Any]:
    """The JSON body of ``GET /api/health/invariants``: ids and counts only."""
    vs = check(sync_pending=sync_pending)
    by = Counter(v.code for v in vs)
    return {"ok": not vs, "count": len(vs), "by_invariant": {c: by.get(c, 0) for c in INVARIANTS},
            "invariants": dict(INVARIANTS), "sync_pending": bool(sync_pending),
            "violations": [v.to_dict() for v in vs[:200]]}


def run_at_start(*, sync_pending: bool = False) -> list[Violation]:
    """Check once and say so: a warning per violation (at most 20 lines) and one audit-log row
    per invariant that fired. Never raises - a data problem must not stop the server."""
    try:
        vs = check(sync_pending=sync_pending)
    except Exception:
        log.warning("the invariant check itself failed (ignored)", exc_info=True)
        return []
    if not vs:
        log.info("invariants: I1-I12 hold")
        return vs
    for v in vs[:20]:
        log.warning("invariant %s violated: %s - %s", v.code, v.subject, v.detail)
    if len(vs) > 20:
        log.warning("invariants: %d more violation(s) not listed", len(vs) - 20)
    try:
        from . import accounts
        for code, n in sorted(Counter(v.code for v in vs).items()):
            accounts.log_activity("invariant_violated", subject=code,
                                  detail=f"{n} violation(s) of {code}: {INVARIANTS[code]}",
                                  category="audit")
    except Exception:
        log.warning("could not write the invariant audit rows", exc_info=True)
    return vs
