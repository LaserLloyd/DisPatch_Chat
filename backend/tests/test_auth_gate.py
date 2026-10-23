"""Security-gate regression tests: the signed-out middleware matrix,
media redaction, the loopback/api_token split on the inbound endpoints, and
the fail-closed handling of a corrupt security.yaml.

Same hermetic style as the rest of tests/: throwaway DB + monkeypatched data
dirs, nothing touches the live data dir or ~/.openclaw.
Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app import auth, avatar_snapshots, config, main, policy
from app.database import Database

SAFE_BOTS = {"alpha", "beta", "Atlas"}     # finalized two-tier roster

# The smallest valid PNG. snapshot_pair() checks the magic bytes, so a
# placeholder string will not do.
_ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Isolated data dir + DB (mirrors the other suites' fixtures). Yields a
    factory so tests can open clients with different peer addresses
    (default 'testclient' = remote/LAN; ('127.0.0.1', …) = loopback)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()

    clients: list[TestClient] = []

    def make_client(client_addr=("testclient", 50000)) -> TestClient:
        c = TestClient(main.app, client=client_addr)
        c.__enter__()
        clients.append(c)
        return c

    yield make_client

    for c in clients:
        c.__exit__(None, None, None)
    asyncio.run(temp_db.close())


def _set_api_token(token: str) -> None:
    cfg = auth.load()
    cfg.api_token = token
    auth._write(cfg)
    auth._bust_cache()


# --------------------------------------------------------------------------- #
# Signed-out 401/403 matrix (PIN set, no session)
# --------------------------------------------------------------------------- #

SIGNED_OUT_BLOCKED = [
    ("GET", "/api/media?path=/etc/hosts"),
    # GET /api/files moved to the INBOUND set 2026-08-14 (same migration as
    # the avatar routes below): agents need the list to map an upload to its
    # blob on disk. Browser-403 and remote-401 are pinned in
    # test_inbound_thread_mgmt.py; downloads stay blocked for a signed-out caller here.
    ("GET", "/api/files/some-id/download"),
    ("GET", "/api/files/some-id/raw"),
    ("GET", "/api/export"),
    ("GET", "/api/search?q=x"),
    ("GET", "/api/recover/transcript"),
    ("GET", "/api/openclaw/sessions?bot_id=main"),
    ("GET", "/api/bots/all"),
    # Bot management (add / edit / remove): operator-only, every method.
    ("POST", "/api/bots"),
    ("PATCH", "/api/bots/alpha"),
    ("DELETE", "/api/bots/alpha"),
    # Code-execution surfaces. Each route has its own gate; these entries are
    # the structural belt-and-braces in the middleware.
    ("GET", "/api/harness/status"),
    ("POST", "/api/harness/jobs"),
    # Local Viewer — reads arbitrary bytes off the host's disk. The router has
    # its own gate too (localview._require_full_access); these rows pin the
    # middleware half, and note that the routes are deliberately absent from
    # _is_inbound so an API key does not open them either.
    ("GET", "/local/file/tmp/x.txt"),
    ("GET", "/api/local/stat?path=/tmp"),
    ("GET", "/api/local/ls?path=/tmp"),
    ("GET", "/api/local/roots"),
    ("PUT", "/api/local/config"),
    # Tools (tools.yaml). Refresh + the write path are barred in the
    # middleware; a tool page / status for a tool that is not `safe: true`
    # (here: no such tool at all) is refused by the handler with the same 403.
    ("PUT", "/api/tools"),
    ("POST", "/api/tools/x/refresh"),
    ("GET", "/tools/x/"),
    ("GET", "/api/tools/x/status"),
    # Settings -> Plugins (app/plugins_api.py): operator only, reads included.
    ("GET", "/api/plugins"),
    ("PUT", "/api/plugins/extensions/voice"),
    ("PUT", "/api/plugins/livetools"),
    ("GET", "/static/avatars/nova.png"),          # non-safe bot's avatar
    # Apps (docs/design/2026-09-25-apps.md). An unknown app id is not on the
    # machine surface, so a sessionless caller is plainly signed out; a known,
    # non-safe app's STATIC page is never a machine surface either.
    ("GET", "/api/apps"),
    ("GET", "/api/apps/x/y"),
    ("POST", "/api/apps/x/y"),
    ("GET", "/apps/x/"),
    ("GET", "/apps/jobboard/"),
    ("GET", "/apps/jobboard/board.js"),
    # The three avatar routes that used to live here moved to the INBOUND set
    # when on-box agents were given avatar management (they were documented in
    # OPENCLAW.md as available and actually returned 403). A remote caller is
    # still refused — 401 from the API-key check instead of 403 — and a locked
    # BROWSER on loopback is still refused 403. Both asserted below, in
    # test_avatar_routes_are_agent_only_not_browser_or_remote.
]


@pytest.mark.parametrize("method,path", SIGNED_OUT_BLOCKED)
def test_signed_out_blocked_matrix(gate_env, method, path):
    client = gate_env()
    auth.set_pin("1234")                 # PIN set, never signed in -> signed out
    r = client.request(method, path)
    assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


def test_unlocked_session_reaches_gated_paths(gate_env):
    client = gate_env()
    auth.set_pin("1234")
    r = client.post("/api/auth/unlock", json={"pin": "1234"})
    assert r.status_code == 200, r.text
    assert client.get("/api/bots/all").status_code == 200
    assert client.get("/api/export").status_code == 200
    assert client.get("/api/threads", params={"bot_id": "main"}).status_code == 200


# --------------------------------------------------------------------------- #
# Inbound endpoints: loopback exempt, remote requires a configured token
# --------------------------------------------------------------------------- #


def test_remote_inject_refused_when_no_token_configured(gate_env):
    client = gate_env()                          # peer = 'testclient' (remote)
    auth.set_pin("1234")                         # api_token stays null
    r = client.post("/api/inject", json={"bot_id": "alpha", "content": "hi"})
    assert r.status_code == 401, r.text


def test_remote_inject_401_without_or_with_wrong_token(gate_env):
    client = gate_env()
    auth.set_pin("1234")
    _set_api_token("tok-123")
    assert client.post("/api/inject", json={"bot_id": "alpha", "content": "x"}).status_code == 401
    r = client.post("/api/inject", headers={"X-API-Key": "wrong"},
                    json={"bot_id": "alpha", "content": "x"})
    assert r.status_code == 401
    r = client.post("/api/inject", headers={"X-API-Key": "tok-123"},
                    json={"bot_id": "alpha", "content": "x"})
    assert r.status_code == 200


def test_loopback_inject_exempt_from_token(gate_env):
    client = gate_env(client_addr=("127.0.0.1", 40000))
    auth.set_pin("1234")                         # no token configured
    r = client.post("/api/inject", json={"bot_id": "alpha", "content": "local cron"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# security.yaml: fail closed on corruption; atomic 0600 writes
# --------------------------------------------------------------------------- #


def test_corrupt_security_yaml_fails_closed(gate_env):
    client = gate_env()
    auth.set_pin("1234")
    # Corrupt the file (invalid YAML). The gate must stay locked — and the
    # correct PIN must no longer mint a session (no fail-open window).
    auth.SECURITY_PATH.write_text("pin: [unclosed\n\t:::")
    auth._bust_cache()
    cfg = auth.load()
    assert cfg.pin_set is True
    assert auth.verify_pin("1234") is False
    assert client.get("/api/bots/all").status_code in (401, 403)
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 401
    # Recovery codes can't bypass the lockdown either.
    assert auth.verify_recovery("LC-AAAA-AAAA") is False
    # Deleting the file is still the documented "remove the lock" path.
    auth.SECURITY_PATH.unlink()
    auth._bust_cache()
    assert client.get("/api/bots/all").status_code == 200


def test_lockdown_revokes_sessions_already_minted(gate_env):
    """Fail-closed has to include the devices that are ALREADY unlocked.

    Lockdown made a NEW session impossible but left every existing one alive,
    so the state entered because the security config can no longer be trusted
    still had full-access devices walking around in it. The plaintext-PIN
    reset path has always cleared both; this one now matches."""
    client = gate_env()
    auth.set_pin("1234")
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    assert client.get("/api/bots/all").status_code == 200      # unlocked now
    assert auth._sessions, "precondition: a live session exists"

    auth.SECURITY_PATH.write_text("pin: [unclosed\n\t:::")
    auth._bust_cache()
    auth.load()                                   # trips the lockdown branch
    assert auth._sessions == {}, "an already-unlocked device kept full access"
    assert not auth.TRUSTED_PATH.exists()
    assert client.get("/api/bots/all").status_code in (401, 403)


def test_empty_security_yaml_fails_closed(gate_env):
    gate_env()
    auth.set_pin("1234")
    auth.SECURITY_PATH.write_text("")            # truncated / emptied file
    auth._bust_cache()
    assert auth.load().pin_set is True
    assert auth.verify_pin("1234") is False


def test_security_yaml_written_0600(gate_env):
    gate_env()
    auth.set_pin("1234")
    if os.name == "nt":
        # Windows has no mode bits to assert; check the DACL: no "everyone/users" read.
        from app import fsperm
        assert fsperm.others_can_read(auth.SECURITY_PATH) is False
        return
    mode = stat.S_IMODE(auth.SECURITY_PATH.stat().st_mode)
    assert mode == 0o600, oct(mode)


# --------------------------------------------------------------------------- #
# Avatar gate is structural: sibling/backup copies can never leak (DP-AUTH-1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [
    "/static/avatars.backup-20260715-165147/nova.png",   # the live leak, 2026-08-01
    "/static/avatars.backup-x/alpha.svg",                # even a safe FILENAME
    "/static/AVATARS.old/nova.png",                      # case games
    "/static/avatars/backup-20260708/nova.png",          # nested under the live dir
    "/static/avatars/backup-20260708/alpha.svg",         # nested + safe basename
])
def test_signed_out_avatar_backup_paths_all_403(gate_env, path):
    """Stale avatars.backup-<date>/ copies under /static/ used to serve every
    non-safe bot's face to a sessionless client, because the gate matched only
    the literal /static/avatars/ prefix. The rule is structural now: anything
    avatar-shaped is gated, and only a safe bot's file directly under the live
    directory may serve without a session."""
    client = gate_env()
    auth.set_pin("1234")
    assert client.get(path).status_code in (401, 403), path


def test_signed_out_live_avatar_still_serves(gate_env):
    """The tightening must not cost a signed-out device the pictures it may render."""
    client = gate_env()
    auth.set_pin("1234")
    assert client.get("/static/avatars/alpha.svg").status_code != 403


def test_unlocked_session_reaches_avatar_statics(gate_env):
    client = gate_env()
    auth.set_pin("1234")
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    assert client.get("/static/avatars/nova.png").status_code != 403


# --------------------------------------------------------------------------- #
# API docs are disabled outright (DP-AUTH-2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_api_docs_routes_do_not_exist(gate_env, path):
    """FastAPI's docs would hand a sessionless LAN caller the complete route +
    model inventory (harness, inject, recovery…) on a 0.0.0.0 bind. They are
    disabled in this deployment — for locked AND unlocked alike."""
    client = gate_env()
    auth.set_pin("1234")
    assert client.get(path).status_code == 404          # signed out: not there
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    assert client.get(path).status_code == 404          # full session: still not


# --------------------------------------------------------------------------- #
# Inbound writes: the machine exemption never extends to a browser (DP-AUTH-3)
# --------------------------------------------------------------------------- #

# What a real browser tab stamps on fetch/XHR (and agents/curl/cron never send).
BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}


def test_locked_browser_on_loopback_cannot_inject(gate_env):
    """The idle auto-lock signs THIS box's own tab out. The inbound
    write endpoints' loopback exemption is for MACHINES — a sessionless browser
    must not be able to post content (or `:react:` markers) into any thread."""
    tab = gate_env(client_addr=("127.0.0.1", 51000))
    auth.set_pin("1234")
    r = tab.post("/api/inject", headers=BROWSER,
                 json={"bot_id": "alpha", "content": "hi"})
    assert r.status_code in (401, 403), r.text
    assert tab.post("/api/daily", headers=BROWSER,
                    json={"bot_id": "alpha"}).status_code in (401, 403)


def test_locked_browser_on_loopback_cannot_post_thread_message(gate_env):
    # An on-box AGENT (no browser headers) keeps the exemption it needs…
    agent = gate_env(client_addr=("127.0.0.1", 51001))
    auth.set_pin("1234")
    r = agent.post("/api/daily", json={"bot_id": "alpha"})
    assert r.status_code == 200, r.text
    tid = r.json()["thread"]["id"]
    assert agent.post(f"/api/threads/{tid}/messages",
                      json={"content": "from cron"}).status_code == 200
    # …while the same box's locked tab is refused on the same route.
    tab = agent  # same client, now sending browser headers and no session
    assert tab.post(f"/api/threads/{tid}/messages", headers=BROWSER,
                    json={"content": "from a locked tab"}).status_code in (401, 403)


def test_unlocked_browser_may_still_use_inbound_writes(gate_env):
    """A signed-in session (a person driving the app) is not signed out — the
    browser re-check must not lock out legitimate session-carrying tabs."""
    client = gate_env(client_addr=("127.0.0.1", 51002))
    auth.set_pin("1234")
    assert client.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    r = client.post("/api/inject", headers=BROWSER,
                    json={"bot_id": "alpha", "content": "hello"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The KDF cost is lowered for the suite. These two tests are why that is safe.
# --------------------------------------------------------------------------- #

def test_shipped_kdf_cost_is_not_weakened():
    """The DEFAULT must stay at production strength.

    conftest drops PBKDF2_ITERATIONS to 1,000 for every other test — 414 hashes
    at 47ms was 19.4 seconds of a suite proving only that PBKDF2 is slow, which
    is its job. The risk that buys is that someone later lowers the SHIPPED
    value and no test notices. This reads the default with the env override
    absent, so it fails if the floor ever moves.
    """
    import importlib
    import os

    from app import auth

    saved = os.environ.pop("DISPATCH_PBKDF2_ITERATIONS", None)
    try:
        fresh = importlib.reload(auth)
        assert fresh.PBKDF2_ITERATIONS >= 200_000, (
            f"shipped KDF cost dropped to {fresh.PBKDF2_ITERATIONS}")
    finally:
        if saved is not None:
            os.environ["DISPATCH_PBKDF2_ITERATIONS"] = saved
        importlib.reload(auth)


def test_pin_round_trips_at_full_production_cost(tmp_path, monkeypatch):
    """One end-to-end set/verify at the real cost, so the cheap-KDF fixture can
    never hide a mismatch between hashing and verification."""
    from app import auth

    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 200_000)
    monkeypatch.setattr(auth.SecurityConfig.__dataclass_fields__["iterations"],
                        "default", 200_000)
    auth._cache = None

    auth.set_pin("13571357")
    cfg = auth.load()
    assert cfg.iterations >= 200_000, "a real PIN was hashed at test strength"
    assert auth.verify_pin("13571357"), "full-cost hash did not verify"
    assert not auth.verify_pin("99999999")


# --------------------------------------------------------------------------- #
# Avatar management is session-exempt for ON-BOX AGENTS only
# --------------------------------------------------------------------------- #

AVATAR_ROUTES = [
    ("GET", "/api/bots/main/avatar"),
    ("GET", "/api/bots/alpha/avatar/full"),
    ("GET", "/api/bots/main/avatar/history"),
    ("POST", "/api/bots/alpha/avatar"),
    ("POST", "/api/bots/alpha/avatar/restore"),
]


@pytest.mark.parametrize("method,path", AVATAR_ROUTES)
def test_avatar_routes_refuse_a_remote_caller_with_no_token(gate_env, method, path):
    """Remote stays closed. The exemption is for processes on this box.

    The status moved 403 -> 401 (the API-key path, fail-closed when no token is
    configured) and that is the only change: both refuse.
    """
    client = gate_env()                       # 'testclient' peer = remote/LAN
    auth.set_pin("1234")
    r = client.request(method, path)
    assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path", AVATAR_ROUTES)
def test_avatar_routes_refuse_a_locked_browser_on_loopback(gate_env, method, path):
    """The hazard the inbound exemption creates, closed.

    The idle auto-lock signs THIS box's own tab out, and that tab is
    on loopback — so the exemption that lets an agent through would have let
    the locked tab through with it, and any page able to reach 127.0.0.1.
    Browsers stamp Sec-Fetch-*; agents and curl do not.
    """
    client = gate_env(("127.0.0.1", 51000))
    auth.set_pin("1234")
    headers = {
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Origin": "http://127.0.0.1:8765",
    }
    # A VALID body, deliberately: FastAPI validates before the handler runs, so
    # a POST with no multipart file returns 422 and never reaches the guard —
    # a test that accepted that would be asserting nothing.
    kwargs = {"headers": headers}
    if method == "POST" and path.endswith("/avatar"):
        kwargs["files"] = {"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")}
    elif method == "POST":
        kwargs["json"] = {"snapshot_id": "deadbeefdeadbeef.png"}

    r = client.request(method, path, **kwargs)
    assert r.status_code in (401, 403), f"locked browser reached {method} {path} -> {r.status_code}"


def test_an_on_box_agent_CAN_read_and_restore_an_avatar(gate_env):
    """The point of the change: what a curl-shaped caller on loopback may do.

    Not asserting 200 on every route — a restore with no snapshot is a
    legitimate 404 — only that none of them is refused by the GATE.
    """
    client = gate_env(("127.0.0.1", 51000))   # loopback, no browser headers
    auth.set_pin("1234")
    for method, path in AVATAR_ROUTES:
        r = client.request(method, path)
        assert r.status_code not in (401, 403), (
            f"agent refused at {method} {path} -> {r.status_code}")


def test_history_full_res_refuses_a_locked_browser_even_for_a_safe_bot(gate_env):
    """The avatar-history IMAGE route is inbound-allowlisted, so the middleware
    blocklist is skipped and its own guard is the only defense. `?full=1` must
    refuse a locked browser even for a safe bot — the same rule the sibling
    full-res routes enforce. Regression: it served safe bots' untouched
    originals to a locked device. The thumbnail (no ?full=1) stays visible."""
    client = gate_env(("127.0.0.1", 51000))
    auth.set_pin("1234")
    headers = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
               "Origin": "http://127.0.0.1:8765"}
    # A machine caller (no browser headers) creates a snapshot to list.
    main_bot = config.load_bots()[0].id
    # Register a safe bot with an avatar + a thread so history has an entry.
    # (Reuse the machine path: loopback, no browser headers = agent.)
    tid = client.post("/api/threads", json={"bot_id": main_bot}).json()["id"]

    # Seed the snapshot EXPLICITLY rather than hoping thread creation captured
    # one. In this hermetic environment the bot has no avatar file on disk, so
    # it never did — and the test used to `return` at that point, which made it
    # pass without ever exercising the gate it exists for. A test that skips
    # itself when the fixture is empty is a test that never runs.
    face = _ONE_PIXEL_PNG
    sid = avatar_snapshots.snapshot_pair(face, face + b"full")
    assert sid, "could not seed an avatar snapshot"
    assert asyncio.run(main.db.set_thread_avatar(tid, sid, explicit=True)), \
        "could not pin the seeded snapshot to a thread"

    hist = client.get(f"/api/bots/{main_bot}/avatar/history").json()
    assert hist.get("avatars"), "the seeded snapshot did not reach the history"
    sid = hist["avatars"][0]["id"]
    r = client.get(f"/api/bots/{main_bot}/avatar/history/{sid}?full=1", headers=headers)
    assert r.status_code in (401, 403), f"locked browser pulled full-res history: {r.status_code}"


# --------------------------------------------------------------------------- #
# Daily budgets are per DEVICE, not per proxy
# --------------------------------------------------------------------------- #


def test_quota_ip_ignores_forwarding_headers_from_a_remote_peer(monkeypatch):
    """A bucket is a fresh ALLOWANCE, so trusting a client-settable header
    meant N forged values bought N daily quotas — and the locked page can set
    X-Forwarded-For itself with a same-origin fetch(). Only the socket peer
    counts unless the peer is loopback (where Tailscale Serve lands)."""
    remote = "192.0.2.77"
    assert policy.quota_ip({}, remote) == remote
    assert policy.quota_ip({"x-forwarded-for": "203.0.113.9"}, remote) == remote
    assert policy.quota_ip({"forwarded": "for=203.0.113.9"}, remote) == remote
    assert policy.quota_ip({"x-real-ip": "203.0.113.9"}, remote) == remote
    # Forged values must all land in the SAME bucket — one quota, not many.
    keys = {policy.quota_ip({"x-forwarded-for": f"203.0.113.{i}"}, remote)
            for i in range(10)}
    assert keys == {remote}, "forged XFF values minted extra quota buckets"


def test_quota_ip_still_splits_the_household_behind_local_serve(monkeypatch):
    """Tailscale Serve is the only fronting layer and it connects from
    loopback; there, one shared household bucket would be the worse failure."""
    proxy = "127.0.0.1"
    assert policy.quota_ip({}, proxy) == proxy                     # direct
    assert policy.quota_ip({"x-forwarded-for": "203.0.113.9"}, proxy) == "203.0.113.9"
    # Rightmost entry only: that is the hop the trusted proxy itself appended;
    # everything to its left is client-supplied and would let a caller pick
    # its own quota bucket.
    assert policy.quota_ip(
        {"x-forwarded-for": "203.0.113.9, 192.0.2.77"}, proxy) == "192.0.2.77"
    # IPv6, bracketed and with a port.
    assert policy.quota_ip({"x-forwarded-for": "[2001:db8::5]:443"}, proxy) == "2001:db8::5"
    # Junk falls back to the peer rather than minting an arbitrary key.
    assert policy.quota_ip({"x-forwarded-for": "not-an-ip"}, proxy) == proxy
    # No forwarding evidence at all: nothing to read.
    assert policy.quota_ip({"x-real-ip": "203.0.113.9"}, proxy) == proxy


# --------------------------------------------------------------------------- #
# Apps: a signed-out BROWSER is refused on a real app's API at both prefixes
# (a sessionless non-browser caller is a machine there — 401 remote, allowed
# on loopback — which apps/jobboard/tests/test_jobboard_api.py pins).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/jobs"), ("POST", "/api/jobs/find"), ("GET", "/api/jobs/months"),
    ("GET", "/api/apps/jobboard"), ("POST", "/api/apps/jobboard/abc/vote"),
])
def test_signed_out_browser_is_refused_on_app_api(gate_env, method, path):
    client = gate_env(("127.0.0.1", 50000))
    auth.set_pin("1234")
    r = client.request(method, path, headers={"origin": "http://127.0.0.1:8765"},
                       **({"json": {}} if method == "POST" else {}))
    assert r.status_code in (401, 403), (method, path, r.status_code)
    assert r.json()["error"]["code"] == "signin_required"
