"""4.3 WP-A14 (H-1): the host tier.

Since 3.0 every Admin holds ``admin_system``, so a gate on it admits a second Admin too -
including the routes whose own notes say "code execution on the host". 4.3 adds
``Requirement.host``: such a route needs the protected Admin (``Principal.is_host``; an open
install counts; machines never) and answers 403 ``host_owner_only`` otherwise.

(a) gate: a second Admin is refused the harness / local viewer / MailForge routes, the Owner is not,
    an open install is not; (b) a second Admin's typed ``[[media:/etc/hosts|x]]`` is stripped, the
    Owner's is ingested; (c) the classification lint: every ``admin_system`` row is classified here.

Run: cd backend && uv run pytest tests/test_i43_host_tier.py --basetemp ~/.cache/cfp/42/tmp-a14
"""
from __future__ import annotations

import pytest

from app import accounts, config, main, policy, route_policy, settings_api, settings_schema
from app.contracts.permissions import MachineScope, Permission, Role

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}
ADMIN_PW = "second-admin-pw1"   # scrub-ok: throwaway test fixture
LAN2 = ("203.0.113.61", 50061)

# --------------------------------------------------------------------------- #
# (c) The classification table. EVERY route_policy row whose permission is admin_system is
# listed here as "host" (the Owner only) or "admin" (every Admin), with the reason. A new
# admin_system row fails test_every_admin_system_row_is_classified until somebody decides.
# Integrators: append one line per new row. A row a 4.3 WP already declared host=True with a
# note is accepted without a line (host is the tightest tier), but a line here is still best.
# --------------------------------------------------------------------------- #
HOST, ADMIN = "host", "admin"
_HARNESS = "code execution on the host (the coding harness)"
_LOCAL = "the local file viewer: arbitrary bytes off the host"
_BACKUP = "household data, encrypted; every Admin runs the household"
_MAINT = "household data: storage, retention, the daily maintenance task"
CLASSIFIED: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/api/health/invariants"): (ADMIN, "ids and counts only"),
    ("POST", "/api/recover/transcript"): (HOST, "reads OpenClaw session stores outside the data folder"),
    ("GET", "/api/dashboard"): (ADMIN, "redacted health"),
    ("GET", "/api/dashboard/deep"): (ADMIN, "redacted health"),
    ("GET", "/api/dashboard/logs"): (ADMIN, "redacted logs"),
    ("GET", "/api/local/roots"): (HOST, _LOCAL),
    ("GET", "/api/local/stat"): (HOST, _LOCAL),
    ("GET", "/api/local/ls"): (HOST, _LOCAL),
    ("PUT", "/api/local/config"): (HOST, _LOCAL),
    ("GET", "/local/file/{path:path}"): (HOST, _LOCAL),
    ("GET", "/api/mail/status"): (HOST, "its launch URL carries MailForge's one-time key"),
    ("GET", "/api/practice/{path:path}"): (HOST, "a proxy onto another host service"),
    ("POST", "/api/practice/{path:path}"): (HOST, "a proxy onto another host service"),
    ("GET", "/api/practice-links/{client_id}"): (HOST, "a proxy onto another host service"),
    ("PUT", "/api/practice-links/{client_id}"): (HOST, "a proxy onto another host service"),
    ("GET", "/api/harness/status"): (HOST, _HARNESS),
    ("POST", "/api/harness/start"): (HOST, _HARNESS),
    ("POST", "/api/harness/stop"): (HOST, _HARNESS),
    ("POST", "/api/harness/restart"): (HOST, _HARNESS),
    ("GET", "/api/harness/models"): (HOST, _HARNESS),
    ("POST", "/api/harness/model"): (HOST, _HARNESS),
    ("GET", "/api/harness/jobs"): (HOST, _HARNESS),
    ("GET", "/api/harness/jobs/{job_id}"): (HOST, _HARNESS),
    ("POST", "/api/harness/jobs"): (HOST, _HARNESS),
    ("POST", "/api/harness/jobs/cancel"): (HOST, _HARNESS),
    ("GET", "/api/harness/sessions"): (HOST, _HARNESS),
    ("POST", "/api/harness/sessions"): (HOST, _HARNESS),
    ("GET", "/api/harness/sessions/{sid}"): (HOST, _HARNESS),
    ("POST", "/api/harness/sessions/{sid}/stop"): (HOST, _HARNESS),
    ("POST", "/api/harness/sessions/{sid}/dismiss"): (HOST, _HARNESS),
    ("POST", "/api/wizard/network"): (ADMIN, "network exposure is household control (maintainer 2026-10-19)"),
    ("GET", "/api/backups"): (ADMIN, _BACKUP),
    ("POST", "/api/backups"): (ADMIN, _BACKUP),
    ("GET", "/api/backups/pending"): (ADMIN, _BACKUP),
    ("DELETE", "/api/backups/pending"): (ADMIN, _BACKUP),
    ("POST", "/api/backups/{backup_id}/verify"): (ADMIN, _BACKUP),
    ("POST", "/api/backups/{backup_id}/restore"): (ADMIN, _BACKUP),
    ("DELETE", "/api/backups/{backup_id}"): (ADMIN, _BACKUP),
    ("POST", "/api/support-bundle"): (ADMIN, "redacted zip for a bug report"),
    ("GET", "/api/maintenance/status"): (ADMIN, _MAINT),
    ("GET", "/api/maintenance/storage"): (ADMIN, _MAINT),
    ("POST", "/api/maintenance/storage/refresh"): (ADMIN, _MAINT),
    ("POST", "/api/maintenance/run"): (ADMIN, _MAINT),
    ("GET", "/api/maintenance/images/preview"): (ADMIN, _MAINT),
    ("GET", "/api/maintenance/retention"): (ADMIN, _MAINT),
    ("PUT", "/api/maintenance/retention"): (ADMIN, _MAINT),
    ("POST", "/api/maintenance/images/restore"): (ADMIN, _MAINT),
}


def _admin_system_rows():
    """(method, path, requirement) for every declared surface that needs admin_system."""
    out = []
    for methods, path, req in route_policy.ROUTES:
        for m in methods:
            out.append((m, path, req))
    for prefix, req in route_policy.MOUNTS:
        out.append(("MOUNT", prefix, req))
    for path, req in route_policy.WS_ROUTES.items():
        out.append(("WS", path, req))
    for action, req in route_policy.WS_ACTION_PERMISSIONS.items():
        out.append(("WSACTION", action, req))
    return out


def test_every_admin_system_row_is_classified():
    missing, wrong = [], []
    for m, path, req in _admin_system_rows():
        if req.permission != Permission.ADMIN_SYSTEM.value:
            continue
        row = CLASSIFIED.get((m, path))
        if row is None:
            # A 4.3 WP's own host row (A4 reveal, A8 CLI presets, A13 OIDC ...) is accepted when
            # it already says host=True and gives its reason; anything else must be decided here.
            if req.host and req.note:
                continue
            missing.append(f"{m} {path}")
            continue
        tier, why = row
        assert why.strip(), (m, path)
        if (tier == HOST) != req.host:
            wrong.append(f"{m} {path}: table says {tier}, route_policy host={req.host}")
    assert not missing, ("admin_system rows with no host/admin decision (add a line to "
                         f"CLASSIFIED in this file): {missing}")
    assert not wrong, wrong


def test_the_table_names_no_stale_route():
    live = {(m, p) for m, p, _r in _admin_system_rows()}
    assert not [k for k in CLASSIFIED if k not in live]


def test_a_host_row_is_never_public_scoped_or_machine_callable():
    for m, path, req in _admin_system_rows():
        if req.host:
            assert not req.public and not req.scoped and not req.machine, (m, path)
            assert req.permission or req.authenticated, (m, path)


def test_the_lint_fails_on_an_unclassified_row(monkeypatch):
    """Proof the lint has teeth: a new admin_system row nobody classified is reported."""
    monkeypatch.setattr(route_policy, "ROUTES", [
        *route_policy.ROUTES, (("POST",), "/api/new/thing", policy.R(Permission.ADMIN_SYSTEM))])
    with pytest.raises(AssertionError, match="/api/new/thing"):
        test_every_admin_system_row_is_classified()


def test_the_lint_accepts_a_declared_host_row_from_a_4_3_anchor(monkeypatch):
    monkeypatch.setattr(route_policy, "ROUTES", [
        *route_policy.ROUTES,
        (("POST",), "/api/new/reveal", policy.R(Permission.ADMIN_SYSTEM, host=True, note="shows a secret"))])
    test_every_admin_system_row_is_classified()


# --------------------------------------------------------------------------- #
# (a) the pure decision
# --------------------------------------------------------------------------- #


@pytest.fixture
def people(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    accounts.reset_for_tests()
    owner = accounts.create_profile("Owner", Role.ADMIN, protected=True)
    ann = accounts.create_profile("Ann", Role.ADMIN)
    uma = accounts.create_profile("Uma", Role.USER)
    yield {k: policy.profile_principal(accounts.get_profile(p.id))
           for k, p in (("owner", owner), ("ann", ann), ("uma", uma))}
    accounts.reset_for_tests()


HOST_REQ = policy.R(Permission.ADMIN_SYSTEM, host=True)


def test_is_host_is_the_protected_admin_or_an_open_install(people):
    assert people["owner"].is_host
    assert not people["ann"].is_host and people["ann"].is_admin
    assert not people["uma"].is_host
    assert policy.open_principal().is_host
    assert not policy.machine_principal("api_token", [s.value for s in MachineScope]).is_host
    assert not policy.nobody().is_host


def test_authorize_host_route(people):
    assert policy.authorize(people["owner"], HOST_REQ).allowed
    assert policy.authorize(policy.open_principal(), HOST_REQ).allowed
    d = policy.authorize(people["ann"], HOST_REQ)
    assert (d.allowed, d.status, d.code) == (False, 403, "host_owner_only")
    assert d.detail == policy.HOST_DETAIL
    # a User keeps today's refusal (it never held admin_system)
    d = policy.authorize(people["uma"], HOST_REQ)
    assert (d.allowed, d.code) == (False, "permission_denied")
    # the same row without host=True still admits every Admin (household tier unchanged)
    assert policy.authorize(people["ann"], policy.R(Permission.ADMIN_SYSTEM)).allowed


def test_authorize_host_never_admits_a_machine_or_a_scoped_non_holder(people):
    every = [s.value for s in MachineScope]
    m = policy.machine_principal("api_token", every)
    req = policy.R(Permission.ADMIN_SYSTEM, host=True, machine=[MachineScope.HEALTH])
    assert not policy.authorize(m, req).allowed
    assert policy.authorize(m, policy.R(Permission.ADMIN_SYSTEM, machine=[MachineScope.HEALTH])).allowed
    scoped = policy.R(Permission.ADMIN_SYSTEM, host=True, scoped=True)
    assert not policy.authorize(people["uma"], scoped).allowed
    assert not policy.authorize(people["ann"], scoped).allowed
    assert policy.authorize(people["owner"], scoped).allowed


def test_harness_frames_reach_host_principals_only(people):
    """INT-1: the coding harness's live frames (jobs, services, sessions on this computer) follow
    the host tier: the Owner and an open install get them, a second Admin and a machine do not."""
    every = [s.value for s in MachineScope]
    for t in ("harness_state", "harness_sessions"):
        frame = {"type": t, "jobs": []}
        assert main.scope_frame_for_principal(frame, people["owner"]) is not None, t
        assert main.scope_frame_for_principal(frame, policy.open_principal()) is not None, t
        assert main.scope_frame_for_principal(frame, people["ann"]) is None, t
        assert main.scope_frame_for_principal(frame, people["uma"]) is None, t
        assert main.scope_frame_for_principal(frame, policy.machine_principal("api_token", every)) is None, t
    # a second Admin's socket keeps its other frames
    assert main.scope_frame_for_principal({"type": "pong"}, people["ann"]) is not None


def test_the_error_code_is_in_the_catalog():
    from app.contracts import errors
    env = errors.envelope("host_owner_only", message=policy.HOST_DETAIL)
    assert env["code"] == "host_owner_only"


# --------------------------------------------------------------------------- #
# (a) over HTTP: second Admin vs Owner vs open install
# --------------------------------------------------------------------------- #


def _install(world):
    from agents_world import sibling
    base = world.create_owner(world.client())
    world.person(base, "Ann", "admin", password=ADMIN_PW)
    ann_id = next(p.id for p in accounts.list_profiles() if p.name == "Ann")
    ann = sibling(world, base, LAN2)
    r = world.login(ann, profile_id=ann_id, password=ADMIN_PW)
    assert r.status_code == 200, r.text
    return base, ann, ann_id


HOST_PROBES = [("GET", "/api/harness/status"), ("GET", "/api/local/roots"), ("GET", "/api/mail/status"),
               ("GET", "/api/harness/jobs"), ("GET", "/api/practice-links/c1"),
               ("POST", "/api/recover/transcript")]


def _call(c, m, path):
    return c.request(m, path, json={"all": True}) if m == "POST" else c.request(m, path)


def test_second_admin_is_refused_every_host_route_the_owner_is_not(world):
    base, ann, _ = _install(world)
    for m, path in HOST_PROBES:
        r = _call(ann, m, path)
        assert r.status_code == 403, (path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "host_owner_only", (path, r.text)
        o = _call(base, m, path)
        assert o.status_code == 200, (path, o.status_code, o.text)      # the Owner: as today
    # household administration stays every Admin's
    assert ann.get("/api/dashboard").status_code == 200
    assert ann.get("/api/maintenance/status").status_code == 200
    assert ann.get("/api/health/invariants").status_code == 200


def test_open_install_reaches_host_routes(world, monkeypatch):
    monkeypatch.setattr(policy, "allow_open_install", lambda *a, **k: True)
    c = world.client()
    assert not policy.claimed()
    r = c.get("/api/local/roots")
    assert r.status_code == 200, r.text


def test_local_viewer_second_lock_refuses_a_second_admin(people):
    """The handler's own lock (a router mounted with no gate) agrees with the gate."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app import localview
    for who, ok in (("owner", True), ("ann", False)):
        req = SimpleNamespace(state=SimpleNamespace(policy=policy.Policy(people[who])))
        if ok:
            localview._require_full_access(req)
        else:
            with pytest.raises(HTTPException) as ei:
                localview._require_full_access(req)
            assert ei.value.status_code == 403 and ei.value.detail == policy.HOST_DETAIL


def test_auth_status_features_hide_host_panes_from_a_second_admin(world, monkeypatch):
    for fn in ("harness_available", "mail_available", "practice_available"):
        monkeypatch.setattr(main, fn, lambda: True)
    base, ann, _ = _install(world)
    fo = base.get("/api/auth/status").json()["features"]
    fa = ann.get("/api/auth/status").json()["features"]
    assert fo["harness"] and fo["mail"] and fo["practice"]
    assert not fa["harness"] and not fa["mail"] and not fa["practice"]


def test_host_settings_are_the_owners(world):
    base, ann, _ = _install(world)
    for key in sorted(settings_api.HOST_SETTING_KEYS):
        assert settings_schema.get_def(key) is not None, key
    r = ann.patch("/api/settings", json={"set": {"tools.openclaw_bin": "/usr/bin/true"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_owner_only", r.text
    r = ann.patch("/api/settings", json={"reset": ["connections.image_cli"]})
    assert r.status_code == 403, r.text
    # an Admin keeps the household host-control settings
    r = ann.patch("/api/settings", json={"set": {"system.backup_keep": 7}})
    assert r.status_code == 200, r.text
    r = base.patch("/api/settings", json={"set": {"tools.openclaw_bin": "/usr/bin/true"}})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# (b) a typed local-path media directive
# --------------------------------------------------------------------------- #


def _send_media(world, monkeypatch, who: str) -> tuple[str, list[str]]:
    """Send ``hello [[media:/etc/hosts|x]]`` as ``who`` (owner | ann); returns (stored text,
    texts the ingest saw)."""
    import asyncio
    seen: list[str] = []

    def fake_ingest(text):
        seen.append(text)
        return text.replace("[[media:/etc/hosts|x]]", "[[media:/media/copied.txt|x]]")

    async def no_turn(*a, **k):
        return None

    monkeypatch.setattr(main, "_ingest_content_media", fake_ingest)
    monkeypatch.setattr(main, "_turn_for_socket", no_turn)
    base = world.create_owner(world.client())
    owner_id = accounts.get_protected_admin().id
    pid = owner_id
    if who == "ann":
        world.person(base, "Ann", "admin", password=ADMIN_PW)
        pid = next(p.id for p in accounts.list_profiles() if p.name == "Ann")
        assert world.login(base, profile_id=pid, password=ADMIN_PW).status_code == 200
    config.upsert_bot(config.Bot(id=f"own{who}", name=f"Own {who}", advisor=dict(PROV),
                                 scope="personal", owner_profile=pid))
    config._invalidate_bots_cache()
    r = base.post("/api/threads", json={"bot_id": f"own{who}"})
    assert r.status_code in (200, 201), r.text
    tid = r.json()["id"]
    with base.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "send", "thread_id": tid, "text": "hello [[media:/etc/hosts|x]]",
                      "client_msg_id": f"cm-{who}"})
        for _ in range(50):
            f = ws.receive_json()
            if f.get("type") == "ack" and f.get("client_msg_id") == f"cm-{who}":
                break
    msgs = base.portal.call(lambda: world.db.dump_messages(tid)) if hasattr(base, "portal") \
        else asyncio.run(world.db.dump_messages(tid))
    users = [m.content for m in msgs if m.role == "user"]
    assert users, msgs
    return users[-1], seen


def test_second_admin_local_media_directive_is_stripped(world, monkeypatch):
    text, seen = _send_media(world, monkeypatch, "ann")
    assert "/etc/hosts" not in text and "[[media:" not in text
    assert text.strip() == "hello"
    assert not seen


def test_owner_local_media_directive_is_ingested(world, monkeypatch):
    text, seen = _send_media(world, monkeypatch, "owner")
    assert seen and "/etc/hosts" in seen[0]
    assert "[[media:/media/copied.txt|x]]" in text
