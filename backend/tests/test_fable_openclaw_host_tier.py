"""4.3 Fable (the owner's decision): the OpenClaw session routes are host tier.

``GET /api/openclaw/sessions``, ``GET /api/openclaw/transcript`` and ``POST /api/openclaw/import``
list and read OpenClaw session stores outside the data folder, exactly like
``POST /api/recover/transcript`` (``admin_system`` + host). A second (non-protected) Admin now
gets 403 ``host_owner_only``; the Owner gets what it got before (here: 400 for an unknown bot,
which the handler answers after the gate); a machine key is refused as before (no scope).

Run: cd backend && uv run pytest tests/test_fable_openclaw_host_tier.py
"""
from __future__ import annotations

from app import accounts, auth, route_policy
from app.contracts.permissions import Permission

ADMIN_PW = "second-admin-pw2"   # scrub-ok: throwaway test fixture
TOKEN = "fable-pin-machine-token-0123456789"   # scrub-ok: throwaway test fixture
ROUTES = [("GET", "/api/openclaw/sessions", {"params": {"bot_id": "nope"}}),
          ("GET", "/api/openclaw/transcript", {"params": {"bot_id": "nope", "session_key": "a:b:c"}}),
          ("POST", "/api/openclaw/import", {"json": {"bot_id": "nope", "session_key": "a:b:c"}})]


def test_the_three_rows_match_recover_transcript():
    rows = route_policy.declared()
    ref = rows[("POST", "/api/recover/transcript")]
    for m, path, _ in ROUTES:
        req = rows[(m, path)]
        assert req.permission == Permission.ADMIN_SYSTEM.value and req.host is True, (m, path, req)
        assert not req.machine and not req.public and not req.scoped, (m, path)
        assert req.note == ref.note, (m, path)


def _install(world):
    from agents_world import sibling
    base = world.create_owner(world.client())
    world.person(base, "Ann", "admin", password=ADMIN_PW)
    ann_id = next(p.id for p in accounts.list_profiles() if p.name == "Ann")
    ann = sibling(world, base, ("203.0.113.62", 50062))
    assert world.login(ann, profile_id=ann_id, password=ADMIN_PW).status_code == 200
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    return base, ann, machine


def test_second_admin_is_refused_the_owner_and_machine_are_unchanged(world):
    base, ann, machine = _install(world)
    for m, path, kw in ROUTES:
        r = ann.request(m, path, **kw)
        assert r.status_code == 403, (path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "host_owner_only", (path, r.text)
        o = base.request(m, path, **kw)
        assert o.status_code == 400, (path, o.status_code, o.text)      # unknown bot, past the gate
        k = machine.request(m, path, **kw)
        assert k.status_code in (401, 403), (path, k.status_code, k.text)
