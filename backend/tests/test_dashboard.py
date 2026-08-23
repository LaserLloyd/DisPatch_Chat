"""Operator-dashboard tests: the auth gate (including without the middleware),
collect()'s never-raise contract under broken probes, and the findings matrix.

Same hermetic style as the rest of tests/: throwaway data dir + DB, nothing
touches the live data dir, ~/.openclaw, or any real binary.
Run: cd backend && uv run pytest tests/test_dashboard.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import auth, config, dashboard, dashboard_routes, main
from app.database import Database
from app.ws import manager

DASH_ROUTES = [
    ("GET", "/api/dashboard"),
    ("GET", "/api/dashboard/deep"),
    ("GET", "/api/dashboard/logs?lines=10"),
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _mount_once(app) -> None:
    """Mount the router on main.app the way main.py's one-liner will.

    Idempotent: the suite builds a client per test and including the same
    router twice would stack duplicate routes.
    """
    if any(getattr(r, "path", "") == "/api/dashboard" for r in app.router.routes):
        return
    app.include_router(dashboard_routes.router)


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    """Isolated data dir + DB + security.yaml, with the dashboard router
    mounted. Yields a client factory (default peer = remote/LAN)."""
    for name, sub in [("DATA_DIR", ""), ("CONFIG_PATH", "config.yaml"),
                      ("MEDIA_DIR", "media"), ("FILES_DIR", "files"),
                      ("LOG_DIR", "logs"), ("BACKUP_DIR", "backups")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    auth._fail_count = 0
    auth._fail_until = 0.0
    config._invalidate_bots_cache()
    config.ensure_dirs()

    # Module-level caches are global; a body (or a walk) cached by one test must
    # never satisfy the next test's differently-arranged box.
    dashboard_routes._summary_cache.update(ts=0.0, body=None)
    dashboard._du_cache.update(ts=0.0, value=None)
    dashboard._du_walking = False
    dashboard._agent_version_cache.clear()
    dashboard._agent_version_fail_cache.clear()
    dashboard._gateway_probe.update(ts=0.0, ok=None)
    dashboard._observed_bind.update(host=None, port=None)
    dashboard._listeners_cache.update(ts=0.0, port=None, value=None)
    dashboard._cpu_prev = None

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    _mount_once(main.app)

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


@pytest.fixture
def live_db(tmp_path):
    """A connected Database for direct collect() calls (no HTTP)."""
    db = Database(tmp_path / "direct.db")
    asyncio.run(db.connect())
    yield db
    asyncio.run(db.close())


def _unlock(client: TestClient, pin: str = "1234") -> None:
    auth.set_pin(pin)
    r = client.post("/api/auth/unlock", json={"pin": pin})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The gate: full-session only, and fail-closed without the middleware
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method,path", DASH_ROUTES)
def test_locked_session_refused(dash_env, method, path):
    client = dash_env()
    auth.set_pin("1234")                     # PIN set, never unlocked -> Safe Mode
    assert client.request(method, path).status_code == 403


@pytest.mark.parametrize("method,path", DASH_ROUTES)
def test_full_session_allowed(dash_env, method, path):
    client = dash_env()
    _unlock(client)
    r = client.request(method, path)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("method,path", DASH_ROUTES)
def test_no_pin_is_open_like_the_rest_of_the_app(dash_env, method, path):
    """With no lock configured the whole app is open; the dashboard matches that
    (and says so, loudly, in its own findings)."""
    client = dash_env()
    assert client.request(method, path).status_code == 200


@pytest.mark.parametrize("method,path", DASH_ROUTES)
def test_expired_session_falls_back_to_refused(dash_env, method, path):
    client = dash_env()
    _unlock(client)
    auth.revoke_all()                        # idle timeout / restart equivalent
    assert client.request(method, path).status_code == 403


@pytest.mark.parametrize("method,path", DASH_ROUTES)
def test_fails_closed_without_the_auth_middleware(dash_env, method, path):
    """The whole point of the router-level dependency: mounted on a bare app
    with no auth_gate middleware at all, a sessionless caller is still refused.
    A future edit that drops the middleware (or adds the prefix to the open
    list) must not silently expose the host's health, log tail and paths."""
    dash_env()                                # isolate paths only
    auth.set_pin("1234")
    bare = FastAPI()
    bare.include_router(dashboard_routes.router)
    with TestClient(bare) as client:
        assert client.request(method, path).status_code == 403


def test_every_route_on_the_router_carries_the_gate(dash_env):
    """Structural: the gate is a router-level dependency, so a route added here
    later cannot forget it."""
    dash_env()
    for route in dashboard_routes.router.routes:
        names = [d.dependency for d in getattr(route, "dependencies", [])]
        assert dashboard_routes._require_operator in names, route.path


def test_deep_check_is_refused_while_one_is_running(dash_env):
    """A second deep check gets 409 rather than queueing — a jumpy click must
    not stack full database scans."""
    client = dash_env()
    _unlock(client)
    assert dashboard_routes._deep_gate.try_acquire() is True
    try:
        assert client.get("/api/dashboard/deep").status_code == 409
    finally:
        dashboard_routes._deep_gate.release()


def test_deep_check_actually_holds_the_gate(dash_env, monkeypatch):
    """…and the refusal above is meaningful: the scan really does run inside
    the gate, so the two tests together cover the contract."""
    client = dash_env()
    _unlock(client)
    seen = {}
    real = dashboard.collect

    async def spy(db, *, deep=False):
        seen["locked"] = dashboard_routes._deep_gate.locked()
        return await real(db, deep=deep)

    monkeypatch.setattr(dashboard, "collect", spy)
    assert client.get("/api/dashboard/deep").status_code == 200
    assert seen["locked"] is True
    assert dashboard_routes._deep_gate.locked() is False      # released after


def test_two_concurrent_deep_checks_cannot_both_pass_the_gate(dash_env, monkeypatch):
    """The contract is "concurrent deep check → 409", and `if lock.locked()`
    followed by `async with lock` did not deliver it: both callers could see the
    gate free and the second one silently QUEUED a full database scan behind the
    first. Two genuinely concurrent calls; exactly one must be refused."""
    dash_env()
    started = 0

    async def slow_collect(db, *, deep=False):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)               # the window the old code lost
        return {"deep": deep, "findings": [], "status": "ok", "counts": {}}

    monkeypatch.setattr(dashboard, "collect", slow_collect)

    async def drive():
        return await asyncio.gather(dashboard_routes.dashboard_deep(),
                                    dashboard_routes.dashboard_deep(),
                                    return_exceptions=True)

    results = asyncio.run(drive())
    refused = [r for r in results
               if isinstance(r, HTTPException) and r.status_code == 409]
    served = [r for r in results if not isinstance(r, BaseException)]
    assert len(refused) == 1, results
    assert len(served) == 1, results
    assert started == 1                          # the second one never ran
    assert dashboard_routes._deep_gate.locked() is False


def test_the_deep_gate_is_released_when_the_scan_blows_up(dash_env, monkeypatch):
    """A failed deep check must not wedge the button for the process lifetime."""
    dash_env()

    async def boom(db, *, deep=False):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dashboard, "collect", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(dashboard_routes.dashboard_deep())
    assert dashboard_routes._deep_gate.locked() is False


def test_responses_are_not_cacheable(dash_env):
    client = dash_env()
    _unlock(client)
    r = client.get("/api/dashboard")
    assert r.headers.get("cache-control") == "no-store"


def test_log_line_bounds_are_enforced(dash_env):
    client = dash_env()
    _unlock(client)
    assert client.get("/api/dashboard/logs?lines=0").status_code == 422
    assert client.get(
        f"/api/dashboard/logs?lines={dashboard.LOG_TAIL_MAX_LINES + 1}"
    ).status_code == 422


# --------------------------------------------------------------------------- #
# Payload shape
# --------------------------------------------------------------------------- #


def test_summary_payload_shape(dash_env):
    client = dash_env()
    _unlock(client)
    body = client.get("/api/dashboard").json()
    for key in ("generated_at", "status", "summary", "counts", "took_ms", "process",
                "storage", "database", "connections", "agent", "clock", "findings"):
        assert key in body, key
    assert body["status"] in ("ok", "warn", "fail")
    assert body["deep"] is False
    ids = [f["id"] for f in body["findings"]]
    assert len(ids) == len(set(ids)), f"duplicate finding ids: {ids}"
    for f in body["findings"]:
        assert set(f) == {"id", "level", "title", "detail", "fix"}
        assert f["level"] in ("ok", "warn", "fail")
        assert f["title"] and f["detail"]


def test_deep_payload_adds_the_expensive_checks(dash_env):
    client = dash_env()
    _unlock(client)
    body = client.get("/api/dashboard/deep").json()
    assert body["deep"] is True
    assert body["database"]["check"]["kind"] == "integrity_check"
    assert body["database"]["check"]["ok"] is True
    assert body["database"]["messages"] == 0        # deep-only row counts


def test_connections_report_counts_but_never_identity(dash_env, monkeypatch):
    """Tier split without leaking who. The payload is counts and flags only —
    no addresses, tokens or user agents may appear in it."""
    dash_env()
    monkeypatch.setattr(manager, "pin_set", lambda: False)
    monkeypatch.setattr(manager, "is_session_live", None)
    fake = {object(): {"decoy": True, "token": None},
            object(): {"decoy": False, "token": None},
            object(): {"decoy": False, "token": None}}
    monkeypatch.setattr(manager, "_conns", fake)

    conns = asyncio.run(dashboard._connections())
    assert set(conns) == {"total", "full", "limited", "tiers_available"}
    assert conns == {"total": 3, "full": 2, "limited": 1, "tiers_available": True}


def test_connections_degrade_when_the_manager_changes(monkeypatch):
    """If the connection manager's internals ever move, we report the total and
    admit the split is unavailable — we do not guess or explode."""
    monkeypatch.setattr(manager, "_conns", "not-a-dict", raising=False)
    conns = asyncio.run(dashboard._connections())
    assert conns["tiers_available"] is False
    assert conns["full"] is None and conns["limited"] is None


# --------------------------------------------------------------------------- #
# collect() never raises, whatever the box is doing
# --------------------------------------------------------------------------- #


def test_collect_survives_a_missing_agent_binary(dash_env, live_db, monkeypatch):
    dash_env()
    monkeypatch.setattr(config, "SETTINGS",
                        replace(config.SETTINGS, openclaw_bin="/nonexistent/bin/openclaw"))
    body = asyncio.run(dashboard.collect(live_db))
    agent = _find(body, "agent.missing")
    assert agent["level"] == "warn"
    assert body["agent"]["present"] is False
    assert body["agent"]["optional"] is True


def test_collect_treats_no_agent_backend_as_supported(dash_env, live_db, monkeypatch):
    """The open-source build ships without one — that is a configuration, not a
    fault, and must not paint the banner amber."""
    dash_env()
    monkeypatch.setattr(config, "SETTINGS", replace(config.SETTINGS, openclaw_bin=""))
    body = asyncio.run(dashboard.collect(live_db))
    assert _find(body, "agent.none")["level"] == "ok"


def test_collect_survives_an_agent_binary_that_hangs(dash_env, live_db, monkeypatch,
                                                     tmp_path):
    """A --version that never returns must cost us the timeout, not the page."""
    dash_env()
    fake = tmp_path / "hangs"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(config, "SETTINGS",
                        replace(config.SETTINGS, openclaw_bin=str(fake)))
    monkeypatch.setattr(dashboard, "AGENT_VERSION_TIMEOUT_S", 0.3)
    body = asyncio.run(dashboard.collect(live_db))
    assert body["agent"]["version"] is None
    assert "timed out" in (body["agent"]["version_error"] or "")
    assert _find(body, "agent.ok")["level"] == "ok"      # present + executable


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_collect_survives_an_unreadable_data_dir(dash_env, live_db, monkeypatch,
                                                 tmp_path):
    dash_env()
    walled = tmp_path / "walled"
    walled.mkdir()
    (walled / "media").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", walled)
    monkeypatch.setattr(config, "MEDIA_DIR", walled / "media")
    monkeypatch.setattr(config, "FILES_DIR", walled / "files")
    monkeypatch.setattr(config, "BACKUP_DIR", walled / "backups")
    walled.chmod(0o000)
    try:
        body = asyncio.run(dashboard.collect(live_db))     # must not raise
    finally:
        walled.chmod(0o755)
    assert body["status"] == "fail"
    assert _find(body, "storage.not_writable")["level"] == "fail"


def test_collect_survives_a_disconnected_database(dash_env, tmp_path):
    """Every probe is independent: a dead DB costs one fail finding, not the
    page. This is the state the dashboard exists for."""
    dash_env()
    never = Database(tmp_path / "never-connected.db")
    body = asyncio.run(dashboard.collect(never))
    assert body["status"] == "fail"
    probe = _find(body, "probe.database")
    assert probe["level"] == "fail" and probe["detail"]
    assert body["process"]["pid"] == os.getpid()          # other probes still ran


def test_collect_reports_a_corrupt_database(dash_env, tmp_path):
    dash_env()
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xde\xad\xbe\xef" * 512)
    db = Database(bad)
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        asyncio.run(db.connect())                        # sqlite rejects it
    try:
        body = asyncio.run(dashboard.collect(db, deep=True))
    finally:
        # A half-open aiosqlite connection owns a NON-daemon worker thread; not
        # closing it hangs interpreter exit long after pytest reports.
        with contextlib.suppress(Exception):
            asyncio.run(db.close())
    assert body["status"] == "fail"
    assert _find(body, "probe.database")["level"] == "fail"


def test_collect_deep_flags_a_damaged_database(dash_env, tmp_path, monkeypatch):
    """A DB that opens but fails integrity_check reports as a failed check
    rather than a probe error."""
    dash_env()
    db = Database(tmp_path / "damaged.db")
    asyncio.run(db.connect())
    try:
        monkeypatch.setattr(dashboard, "_deep_integrity_sync",
                            lambda path: (False, "row 3 missing from index idx_x"))
        body = asyncio.run(dashboard.collect(db, deep=True))
    finally:
        asyncio.run(db.close())
    finding = _find(body, "db.integrity")
    assert finding["level"] == "fail"
    assert "idx_x" in finding["detail"]
    assert finding["fix"]


def test_collect_survives_a_probe_that_times_out(dash_env, live_db, monkeypatch):
    dash_env()

    async def _slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(dashboard, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(dashboard, "_process", _slow)
    body = asyncio.run(dashboard.collect(live_db))
    assert _find(body, "probe.process")["level"] == "fail"
    assert "timed out" in _find(body, "probe.process")["detail"]


def test_collect_survives_findings_blowing_up(dash_env, live_db, monkeypatch):
    """Even the finding builder is wrapped: the page renders something honest
    rather than a 500."""
    dash_env()

    def _boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dashboard, "_build_findings", _boom)
    body = asyncio.run(dashboard.collect(live_db))
    assert body["status"] == "fail"
    assert _find(body, "probe.findings")["level"] == "fail"


# --------------------------------------------------------------------------- #
# Findings logic — the pure part, one condition at a time
# --------------------------------------------------------------------------- #


def _find(body_or_findings, fid: str) -> dict:
    findings = (body_or_findings["findings"] if isinstance(body_or_findings, dict)
                else body_or_findings)
    for f in findings:
        if f["id"] == fid:
            return f
    raise AssertionError(f"no finding {fid!r} in {[f['id'] for f in findings]}")


def _has(findings, fid: str) -> bool:
    return any(f["id"] == fid for f in findings)


def _bind(**over) -> dict:
    """A CONFIRMED loopback-only bind, as _bind_state() reports one.

    Tests that want the unverified path (nothing observed, configured value
    only) pass ``process={"bind": None}`` — the builder then re-derives it from
    config.SETTINGS, which is the fallback that must carry a caveat.
    """
    base = {"host": "127.0.0.1", "port": 8765, "source": "observed",
            "verified": True, "kind": "loopback", "reachable": False,
            "listeners": ["127.0.0.1"], "configured_host": "127.0.0.1",
            "configured_port": 8765, "caveat": "",
            "why": "the only listener on port 8765 is 127.0.0.1"}
    base.update(over)
    return base


def _facts(**over) -> dict:
    """A healthy box; each test perturbs exactly one thing."""
    base = {
        "process": {"root": False, "uid": 1000, "user": "dispatch", "container": None,
                    "uptime_seconds": 7200, "pid": 1234, "bind": _bind()},
        "storage": {
            "data_dir": "/data", "writable": True,
            "disk": {"total_bytes": 100 * 1024**3, "used_bytes": 40 * 1024**3,
                     "free_bytes": 60 * 1024**3, "free_ratio": 0.6},
            "blob_bytes": 1024**3, "cap_bytes": 20 * 1024**3, "cap_ratio": 0.05,
        },
        "database": {
            "journal_mode": "wal",
            "check": {"kind": "quick_check", "ok": True, "detail": "ok"},
            "backup": {"dir": "/data/backups", "count": 3, "corrupt_count": 0,
                       "last_backup_epoch": int(time.time()) - 600,
                       "last_backup_at": "now-ish", "last_backup_ok": True,
                       "interval_seconds": 21600, "keep": 12},
        },
        "agent": {"configured": True, "present": True, "executable": True,
                  "bin": "openclaw", "path": "/usr/bin/openclaw", "version": "1.2.3"},
        "clock": {"now": "2026-08-03T00:00:00+00:00", "timezone": "UTC",
                  "jump_seconds": 0.0, "future_data_seconds": 0.0},
        "errors": {},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


@pytest.fixture
def clean_security(tmp_path, monkeypatch):
    """No PIN, no token, loopback bind — the neutral starting point for the
    security findings (each test moves one dial)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    monkeypatch.setattr(config, "SETTINGS", replace(config.SETTINGS, host="127.0.0.1"))
    # BOTH spellings of every setting these findings read: config.env() answers
    # to the documented DISPATCH_ name and the legacy LOCAL_CHAT_ one, so a
    # stray value under either would decide the test.
    for name in ("PUBLIC_URL", "BEHIND_TLS", "FILES_TOTAL_MAX", "LOG_FILE"):
        monkeypatch.delenv(f"DISPATCH_{name}", raising=False)
        monkeypatch.delenv(f"LOCAL_CHAT_{name}", raising=False)
    # The observed-socket stash is module state: an HTTP test earlier in the
    # session must not decide what "the bind" is for a pure-findings test.
    dashboard._observed_bind.update(host=None, port=None)
    dashboard._listeners_cache.update(ts=0.0, port=None, value=None)
    auth._sessions.clear()
    auth._cache = None
    yield monkeypatch


def test_no_pin_on_a_public_bind_is_a_failure(clean_security):
    clean_security.setattr(config, "SETTINGS", replace(config.SETTINGS, host="0.0.0.0"))
    f = _find(dashboard._build_findings(**_facts(process={"bind": None})), "auth.no_pin")
    assert f["level"] == "fail"
    assert "PIN" in f["fix"]


def test_no_pin_on_an_observed_public_socket_is_a_failure(clean_security):
    """Even with a loopback CONFIGURED: the socket we are answering on wins."""
    facts = _facts(process={"bind": _bind(host="192.0.2.7", kind="reachable",
                                          reachable=True, listeners=["0.0.0.0"],
                                          why="the kernel reports 0.0.0.0 listening "
                                              "on port 8765")})
    f = _find(dashboard._build_findings(**facts), "auth.no_pin")
    assert f["level"] == "fail"
    assert "192.0.2.7" in f["detail"]


def test_no_pin_on_a_verified_loopback_bind_is_fine(clean_security):
    """The green verdict survives — but only when the bind was CONFIRMED."""
    f = _find(dashboard._build_findings(**_facts()), "auth.no_pin")
    assert f["level"] == "ok"
    assert "only listener" in f["detail"]


def test_no_pin_on_an_unverified_loopback_bind_only_warns(clean_security):
    """The dangerous green. config.SETTINGS.host is what was CONFIGURED and
    nothing in the app binds it (every launcher passes uvicorn its own --host),
    so "only reachable from this machine" must not be claimed from it."""
    findings = dashboard._build_findings(**_facts(process={"bind": None}))
    f = _find(findings, "auth.no_pin")
    assert f["level"] == "warn"
    assert "could not be confirmed" in f["title"]
    assert "unverified" in f["detail"]
    # …and the TLS verdict, which reads the same value, degrades with it.
    tls = _find(findings, "net.tls")
    assert tls["level"] == "warn"
    assert "could not be confirmed" in tls["title"]


def test_pin_set_reports_the_lock(clean_security):
    auth.set_pin("1234")
    findings = dashboard._build_findings(**_facts())
    assert _find(findings, "auth.lock")["level"] == "ok"
    assert not _has(findings, "auth.no_pin")


def test_missing_tls_on_a_reachable_bind_warns(clean_security):
    clean_security.setattr(config, "SETTINGS", replace(config.SETTINGS, host="0.0.0.0"))
    f = _find(dashboard._build_findings(**_facts(process={"bind": None})), "net.tls")
    assert f["level"] == "warn"
    assert f["title"] == "Served without TLS"


def test_no_tls_needed_on_a_verified_loopback_bind(clean_security):
    assert _find(dashboard._build_findings(**_facts()), "net.tls")["level"] == "ok"


@pytest.mark.parametrize("var", ["DISPATCH_BEHIND_TLS", "LOCAL_CHAT_BEHIND_TLS"])
def test_declared_tls_terminator_clears_the_warning(clean_security, var):
    """Both spellings work: the DISPATCH_ name is the documented one, and it did
    nothing at all while this read os.environ["LOCAL_CHAT_BEHIND_TLS"] directly."""
    clean_security.setattr(config, "SETTINGS", replace(config.SETTINGS, host="0.0.0.0"))
    clean_security.setenv(var, "1")
    assert _find(dashboard._build_findings(**_facts(process={"bind": None})),
                 "net.tls")["level"] == "ok"


@pytest.mark.parametrize("var", ["DISPATCH_PUBLIC_URL", "LOCAL_CHAT_PUBLIC_URL"])
def test_an_https_public_url_declares_tls(clean_security, var):
    clean_security.setattr(config, "SETTINGS", replace(config.SETTINGS, host="0.0.0.0"))
    clean_security.setenv(var, "https://chat.example.org")
    assert _find(dashboard._build_findings(**_facts(process={"bind": None})),
                 "net.tls")["level"] == "ok"


def test_placeholder_api_token_is_a_failure(clean_security):
    cfg = auth.load()
    cfg.api_token = "changeme"
    auth._write(cfg)
    auth._bust_cache()
    f = _find(dashboard._build_findings(**_facts()), "auth.api_token_default")
    assert f["level"] == "fail"


def test_short_api_token_warns(clean_security):
    cfg = auth.load()
    cfg.api_token = "abc123"
    auth._write(cfg)
    auth._bust_cache()
    assert _find(dashboard._build_findings(**_facts()),
                 "auth.api_token_weak")["level"] == "warn"


def test_strong_api_token_is_ok(clean_security):
    cfg = auth.load()
    cfg.api_token = "S6t8xQ2m-longenough-random-value"
    auth._write(cfg)
    auth._bust_cache()
    assert _find(dashboard._build_findings(**_facts()), "auth.api_token")["level"] == "ok"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_world_readable_security_file_warns(clean_security):
    auth.set_pin("1234")
    auth.SECURITY_PATH.chmod(0o644)
    assert _find(dashboard._build_findings(**_facts()),
                 "auth.security_file_mode")["level"] == "warn"


def test_root_outside_a_container_warns(clean_security):
    f = _find(dashboard._build_findings(**_facts(process={"root": True, "uid": 0})),
              "process.root")
    assert f["level"] == "warn"


def test_root_inside_a_container_is_accepted(clean_security):
    f = _find(dashboard._build_findings(
        **_facts(process={"root": True, "uid": 0, "container": "docker"})), "process.root")
    assert f["level"] == "ok"
    assert f["fix"]                                  # still offers the hardening step


@pytest.mark.parametrize("free,ratio,fid,level", [
    (60 * 1024**3, 0.60, "storage.disk", "ok"),
    (700 * 1024**2, 0.30, "storage.disk_low", "warn"),
    (50 * 1024**2, 0.30, "storage.disk_full", "fail"),
    (40 * 1024**3, 0.015, "storage.disk_full", "fail"),      # ratio, not absolute
    (5 * 1024**3, 0.05, "storage.disk_low", "warn"),         # ratio, not absolute
])
def test_disk_thresholds(clean_security, free, ratio, fid, level):
    facts = _facts(storage={"disk": {"total_bytes": 100 * 1024**3,
                                     "used_bytes": 100 * 1024**3 - free,
                                     "free_bytes": free, "free_ratio": ratio}})
    assert _find(dashboard._build_findings(**facts), fid)["level"] == level


def test_unreadable_disk_is_a_failure(clean_security):
    facts = _facts(storage={"disk": {"error": "PermissionError"}})
    assert _find(dashboard._build_findings(**facts), "storage.disk")["level"] == "fail"


def test_unwritable_data_dir_is_a_failure(clean_security):
    facts = _facts(storage={"writable": False})
    assert _find(dashboard._build_findings(**facts),
                 "storage.not_writable")["level"] == "fail"


@pytest.mark.parametrize("ratio,fid,level", [
    (0.05, "storage.cap", "ok"),
    (0.85, "storage.cap_near", "warn"),
    (1.0, "storage.cap_reached", "fail"),
    (1.4, "storage.cap_reached", "fail"),
])
def test_blob_cap_thresholds(clean_security, ratio, fid, level):
    facts = _facts(storage={"cap_ratio": ratio,
                            "blob_bytes": int(20 * 1024**3 * ratio)})
    assert _find(dashboard._build_findings(**facts), fid)["level"] == level


def test_non_wal_journal_mode_warns(clean_security):
    facts = _facts(database={"journal_mode": "delete"})
    assert _find(dashboard._build_findings(**facts),
                 "db.journal_mode")["level"] == "warn"


def test_failed_integrity_check_is_a_failure(clean_security):
    facts = _facts(database={"check": {"kind": "quick_check", "ok": False,
                                       "detail": "malformed"}})
    f = _find(dashboard._build_findings(**facts), "db.integrity")
    assert f["level"] == "fail" and "backups" in f["fix"]


def test_backups_disabled_warns(clean_security):
    facts = _facts(database={"backup": {"interval_seconds": 0,
                                        "last_backup_epoch": None}})
    assert _find(dashboard._build_findings(**facts),
                 "backup.disabled")["level"] == "warn"


def test_backups_never_run_is_quiet_right_after_boot(clean_security):
    facts = _facts(process={"uptime_seconds": 30},
                   database={"backup": {"last_backup_epoch": None,
                                        "interval_seconds": 21600}})
    assert _find(dashboard._build_findings(**facts),
                 "backup.never_run")["level"] == "ok"


def test_backups_never_run_warns_once_it_has_had_time(clean_security):
    facts = _facts(process={"uptime_seconds": 6 * 3600},
                   database={"backup": {"last_backup_epoch": None,
                                        "interval_seconds": 21600}})
    assert _find(dashboard._build_findings(**facts),
                 "backup.never_run")["level"] == "warn"


def test_failed_backup_is_a_failure(clean_security):
    facts = _facts(database={"backup": {"last_backup_ok": False, "corrupt_count": 1,
                                        "last_backup_epoch": int(time.time()) - 60,
                                        "last_backup_at": "recent",
                                        "interval_seconds": 21600}})
    assert _find(dashboard._build_findings(**facts), "backup.failed")["level"] == "fail"


def test_stale_backups_warn(clean_security):
    facts = _facts(database={"backup": {
        "last_backup_epoch": int(time.time()) - 5 * 24 * 3600,
        "last_backup_ok": True, "interval_seconds": 21600, "count": 2}})
    assert _find(dashboard._build_findings(**facts), "backup.stale")["level"] == "warn"


def test_unreadable_backup_dir_warns(clean_security):
    facts = _facts(database={"backup": {"error": "PermissionError: backups"}})
    assert _find(dashboard._build_findings(**facts),
                 "backup.unreadable")["level"] == "warn"


@pytest.mark.parametrize("future,jump,fid,level", [
    (0, 0, "time.clock", "ok"),
    (600, 0, "time.clock_skew", "warn"),
    (0, 900, "time.clock_skew", "warn"),
    (7200, 0, "time.clock_skew", "fail"),
])
def test_clock_skew_levels(clean_security, future, jump, fid, level):
    facts = _facts(clock={"future_data_seconds": future, "jump_seconds": jump})
    assert _find(dashboard._build_findings(**facts), fid)["level"] == level


def test_probe_errors_become_failures(clean_security):
    findings = dashboard._build_findings(**_facts(errors={"storage": "PermissionError: /data"}))
    f = _find(findings, "probe.storage")
    assert f["level"] == "fail" and "/data" in f["detail"]


def test_agent_not_executable_warns(clean_security):
    facts = _facts(agent={"executable": False, "path": "/usr/bin/openclaw"})
    assert _find(dashboard._build_findings(**facts),
                 "agent.not_executable")["level"] == "warn"


def test_overall_status_is_the_worst_finding(clean_security):
    ok_only = dashboard._build_findings(**_facts())
    assert dashboard._worst(f["level"] for f in ok_only) == "ok"
    warned = dashboard._build_findings(**_facts(database={"journal_mode": "delete"}))
    assert dashboard._worst(f["level"] for f in warned) == "warn"
    failed = dashboard._build_findings(**_facts(storage={"writable": False}))
    assert dashboard._worst(f["level"] for f in failed) == "fail"


# --------------------------------------------------------------------------- #
# Storage: the cap the uploader actually enforces, one walk at a time, and
# reactions counted separately
# --------------------------------------------------------------------------- #


def _cap_env(monkeypatch, **values) -> None:
    for name in ("DISPATCH_FILES_TOTAL_MAX", "LOCAL_CHAT_FILES_TOTAL_MAX"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_cap_honours_the_legacy_env_name(monkeypatch):
    """main.FILES_TOTAL_MAX reads config.env(), which answers to BOTH names. A
    dashboard reading os.environ["DISPATCH_FILES_TOTAL_MAX"] only reported the
    20GB default — "well under the cap" — on an install where the uploader was
    enforcing the legacy value and returning 507."""
    _cap_env(monkeypatch, LOCAL_CHAT_FILES_TOTAL_MAX="12345")
    assert dashboard._cap_bytes() == 12345
    assert dashboard._cap_bytes() == int(config.env("FILES_TOTAL_MAX"))


def test_the_cap_prefers_the_documented_name(monkeypatch):
    _cap_env(monkeypatch, DISPATCH_FILES_TOTAL_MAX="999",
             LOCAL_CHAT_FILES_TOTAL_MAX="12345")
    assert dashboard._cap_bytes() == 999


def test_an_unparseable_cap_falls_back_to_the_default(monkeypatch):
    _cap_env(monkeypatch, DISPATCH_FILES_TOTAL_MAX="twenty gigs")
    assert dashboard._cap_bytes() == 20 * 1024 * 1024 * 1024


def test_a_second_walk_is_not_spawned_while_one_is_in_flight(dash_env, tmp_path,
                                                             monkeypatch):
    """asyncio.wait_for cancels the AWAIT, not the thread behind to_thread, and
    the cache is only written when a walk COMPLETES — so a walk slow enough to
    blow the probe budget used to get another walker every 5s poll until the
    default executor (8 workers) was full and every to_thread in the app
    starved. A walk already running must serve the stale reading instead."""
    dash_env()
    walked: list = []
    release = threading.Event()

    def slow_walk(root, budget):
        walked.append(str(root))
        release.wait(5.0)
        return {"bytes": 1, "files": 1, "complete": True}

    monkeypatch.setattr(dashboard, "_dir_usage", slow_walk)
    db_path = tmp_path / "chats.db"
    first: dict = {}
    worker = threading.Thread(
        target=lambda: first.update(dashboard._storage_sync(db_path, fresh=False)))
    worker.start()
    try:
        deadline = time.monotonic() + 5.0
        while not walked and time.monotonic() < deadline:
            time.sleep(0.01)
        assert walked, "the first walk never started"
        during = len(walked)

        # The poll that lands while that walk is still running.
        out = dashboard._storage_sync(db_path, fresh=False)
        assert out["measuring"] is True
        assert out["blob_bytes"] is None            # not measured, not "0"
        assert out["cap_ratio"] is None             # so no cap verdict is claimed
        assert len(walked) == during, "a second walker was spawned"
    finally:
        release.set()
        worker.join(10)
    assert first["measuring"] is False               # the real walk still landed
    assert dashboard._du_walking is False            # and the guard was released


def test_an_in_flight_walk_serves_the_previous_reading(dash_env, tmp_path,
                                                       monkeypatch):
    """With something cached, the poll that arrives mid-walk gets the last good
    numbers marked stale — never a blank card."""
    dash_env()
    dashboard._du_cache.update(
        ts=time.monotonic() - dashboard.DU_TTL_S - 1,
        value={"media": {"bytes": 7, "files": 1, "complete": True},
               "files": {"bytes": 3, "files": 1, "complete": True},
               "backups": {"bytes": 0, "files": 0, "complete": True},
               "reactions": {"bytes": 5, "files": 1, "complete": True},
               "blob_bytes": 10, "complete": True, "blob_complete": True,
               "cached": False, "measuring": False})
    monkeypatch.setattr(dashboard, "_du_claim", lambda: False)
    out = dashboard._storage_sync(tmp_path / "chats.db", fresh=False)
    assert out["blob_bytes"] == 10
    assert out["cached"] is True and out["measuring"] is True


def test_reactions_are_reported_but_kept_out_of_the_cap_maths(dash_env, tmp_path):
    """Spent reaction images are kept forever by design, so this is the
    fastest-growing directory on the box — it has to be visible. It is NOT part
    of blob_bytes: the cap main.py enforces governs media/ and files/ only."""
    dash_env()
    # Measured as a delta: startup seeds the built-in reaction pack, and this
    # test is about where the bytes are ATTRIBUTED, not how many there are.
    before = dashboard._storage_sync(tmp_path / "chats.db", fresh=True)["reactions"]

    (config.MEDIA_DIR / "a.png").write_bytes(b"x" * 10)
    (config.FILES_DIR / "b.bin").write_bytes(b"y" * 20)
    mood = config.REACTIONS_DIR / "moods" / "happy"
    mood.mkdir(parents=True, exist_ok=True)
    (mood / "one.png").write_bytes(b"z" * 500)
    (config.REACTIONS_DIR / "spent" / "happy").mkdir(parents=True, exist_ok=True)
    (config.REACTIONS_DIR / "spent" / "happy" / "old.png").write_bytes(b"z" * 700)

    out = dashboard._storage_sync(tmp_path / "chats.db", fresh=True)
    assert out["reactions"]["bytes"] == before["bytes"] + 1200
    assert out["reactions"]["files"] == before["files"] + 2
    assert out["blob_bytes"] == 30                   # media + files ONLY
    assert out["media"]["bytes"] == 10 and out["files"]["bytes"] == 20


def test_a_truncated_walk_never_claims_you_are_under_the_cap(clean_security):
    """The walk stops at a shared entry budget, and truncation can only
    UNDERCOUNT — so "well under the cap" is exactly the verdict those numbers
    cannot support."""
    facts = _facts(storage={"blob_complete": False, "cap_ratio": 0.05})
    f = _find(dashboard._build_findings(**facts), "storage.cap")
    assert f["level"] == "ok"
    assert "not fully measured" in f["title"]
    assert f["detail"].startswith("At least ")
    assert "deep check" in f["fix"]


def test_a_truncated_walk_still_reports_a_cap_it_has_already_blown(clean_security):
    """…while an over-cap reading stays true, because the real total is higher."""
    facts = _facts(storage={"blob_complete": False, "cap_ratio": 1.2,
                            "blob_bytes": 24 * 1024**3})
    f = _find(dashboard._build_findings(**facts), "storage.cap_reached")
    assert f["level"] == "fail"
    assert "at least" in f["detail"]


def test_a_complete_walk_still_says_you_are_under_the_cap(clean_security):
    f = _find(dashboard._build_findings(**_facts(storage={"blob_complete": True})),
              "storage.cap")
    assert f["title"] == "Storage well under the cap"


def test_measuring_storage_emits_no_cap_verdict_at_all(clean_security):
    facts = _facts(storage={"cap_ratio": None, "blob_bytes": None, "measuring": True})
    findings = dashboard._build_findings(**facts)
    for fid in ("storage.cap", "storage.cap_near", "storage.cap_reached"):
        assert not _has(findings, fid), fid


# --------------------------------------------------------------------------- #
# Backups: a file that is not a database is not a backup
# --------------------------------------------------------------------------- #


def _real_snapshot(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()


def test_a_zero_byte_snapshot_is_not_counted_as_a_backup(dash_env):
    """A failed VACUUM INTO leaves a 0-byte chats-<stamp>.db behind. Counting it
    made the page report "Backups are current" on a box where every single
    backup was failing — the worst lie this module can tell."""
    dash_env()
    (config.BACKUP_DIR / "chats-20260804-020000.db").write_bytes(b"")
    state = dashboard._backup_state_sync()
    assert state["count"] == 0
    assert state["unusable_count"] == 1
    assert state["last_backup_epoch"] is None

    findings = dashboard._build_findings(
        **_facts(database={"backup": state}, process={"uptime_seconds": 6 * 3600}))
    assert not _has(findings, "backup.ok")
    bad = _find(findings, "backup.bad_snapshot")
    assert bad["level"] == "warn" and bad["fix"]
    assert _find(findings, "backup.never_run")["level"] == "warn"


def test_a_snapshot_without_a_sqlite_header_is_not_counted(dash_env):
    """Non-empty is not enough: a partial write is still not restorable."""
    dash_env()
    (config.BACKUP_DIR / "chats-20260804-030000.db").write_bytes(b"not a database")
    state = dashboard._backup_state_sync()
    assert state["count"] == 0 and state["unusable_count"] == 1


def test_a_real_snapshot_still_counts(dash_env):
    dash_env()
    _real_snapshot(config.BACKUP_DIR / "chats-20260804-040000.db")
    state = dashboard._backup_state_sync()
    assert state["count"] == 1
    assert state["unusable_count"] == 0
    assert state["last_backup_epoch"] is not None
    findings = dashboard._build_findings(**_facts(database={"backup": state}))
    assert _find(findings, "backup.ok")["level"] == "ok"
    assert not _has(findings, "backup.bad_snapshot")


def test_a_good_snapshot_beside_a_failed_one_reports_both(dash_env):
    """The good one is still the backup you can restore; the empty one still
    means the last attempt died. Both facts get said."""
    dash_env()
    _real_snapshot(config.BACKUP_DIR / "chats-20260804-040000.db")
    (config.BACKUP_DIR / "chats-20260804-100000.db").write_bytes(b"")
    state = dashboard._backup_state_sync()
    assert state["count"] == 1 and state["unusable_count"] == 1
    findings = dashboard._build_findings(**_facts(database={"backup": state}))
    assert _find(findings, "backup.ok")["level"] == "ok"
    assert _find(findings, "backup.bad_snapshot")["level"] == "warn"


def test_corrupt_snapshots_are_still_classified_separately(dash_env):
    dash_env()
    _real_snapshot(config.BACKUP_DIR / "chats-20260804-040000.db")
    (config.BACKUP_DIR / "chats-20260804-050000.db.corrupt").write_bytes(b"junk")
    state = dashboard._backup_state_sync()
    assert state["count"] == 1
    assert state["corrupt_count"] == 1
    assert state["unusable_count"] == 0
    assert state["last_backup_ok"] is False          # newest attempt failed


# --------------------------------------------------------------------------- #
# Agent backend: the CLI and the gateway fail separately
# --------------------------------------------------------------------------- #


def test_a_failing_agent_version_is_not_respawned_every_poll(dash_env, tmp_path,
                                                             monkeypatch):
    """Only successes were cached, so a broken or hanging CLI cost a spawn + a
    3s timeout + a 1s reap on EVERY 5s poll — a process storm aimed at a box
    that is already unwell."""
    dash_env()
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\necho nope >&2\nexit 3\n")
    broken.chmod(0o755)

    spawns = 0
    real_exec = asyncio.create_subprocess_exec

    async def counting(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting)
    key = (str(broken), 1.0, 2)
    first = asyncio.run(dashboard._agent_version(str(broken), key))
    second = asyncio.run(dashboard._agent_version(str(broken), key))
    assert first[0] is None and second[0] is None
    assert first[1] == second[1] and first[1]
    assert spawns == 1

    # …and the cache is SHORT: a fixed CLI must show up on the page in a minute,
    # not at the next restart.
    dashboard._agent_version_fail_cache[key] = (time.monotonic() - 1, "stale")
    asyncio.run(dashboard._agent_version(str(broken), key))
    assert spawns == 2


def test_a_hanging_agent_version_is_cached_too(dash_env, tmp_path, monkeypatch):
    dash_env()
    hangs = tmp_path / "hangs"
    hangs.write_text("#!/bin/sh\nsleep 30\n")
    hangs.chmod(0o755)
    monkeypatch.setattr(dashboard, "AGENT_VERSION_TIMEOUT_S", 0.3)
    key = (str(hangs), 1.0, 2)
    started = time.monotonic()
    assert asyncio.run(dashboard._agent_version(str(hangs), key))[0] is None
    first_cost = time.monotonic() - started
    started = time.monotonic()
    assert asyncio.run(dashboard._agent_version(str(hangs), key))[0] is None
    assert time.monotonic() - started < first_cost / 2      # served from cache


def test_a_dead_gateway_is_its_own_finding(clean_security):
    """"Agent CLI available" was reassurance handed out for exactly the symptom
    a dead gateway produces: messages stored, nothing ever replies."""
    facts = _facts(agent={"gateway": {"host": "127.0.0.1", "port": 18789,
                                      "reachable": False}})
    findings = dashboard._build_findings(**facts)
    assert _find(findings, "agent.ok")["level"] == "ok"          # the CLI IS there
    gw = _find(findings, "agent.gateway")
    assert gw["level"] == "warn" and gw["fix"]
    assert "18789" in gw["detail"]


def test_a_live_gateway_reports_ok(clean_security):
    facts = _facts(agent={"gateway": {"host": "127.0.0.1", "port": 18789,
                                      "reachable": True}})
    assert _find(dashboard._build_findings(**facts), "agent.gateway")["level"] == "ok"


def test_no_agent_backend_means_no_gateway_finding(clean_security):
    facts = _facts(agent={"configured": False, "gateway": None})
    assert not _has(dashboard._build_findings(**facts), "agent.gateway")


def test_the_gateway_probe_answers_from_a_real_socket(dash_env, monkeypatch):
    """The probe itself: a listener answers, a closed port does not. Bound on an
    ephemeral port — never the app's own."""
    dash_env()

    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(dashboard, "GATEWAY_PORT", port)
        dashboard._gateway_probe.update(ts=0.0, ok=None)
        try:
            assert await dashboard._gateway_reachable() is True
        finally:
            server.close()
            await server.wait_closed()
        dashboard._gateway_probe.update(ts=0.0, ok=None)
        assert await dashboard._gateway_reachable() is False

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Bind: what we know vs what was configured
# --------------------------------------------------------------------------- #

# One real row of each, captured from /proc on a box serving DisPatch.
_PROC_TCP = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt\n"
    "   0: 0100007F:2255 00000000:0000 0A 00000000:00000000 00:00000000 00000000\n"
    "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00000000 00000000\n"
    "   2: 0100007F:2255 0100007F:C001 01 00000000:00000000 00:00000000 00000000\n"
)
_PROC_TCP6 = (
    "  sl  local_address                         remote_address  st\n"
    "   0: 00000000000000000000000001000000:2255 000000000000000"
    "00000000000000000:0000 0A 00000000:00000000 00:00000000 00000000\n"
)


def test_proc_listeners_are_parsed_for_the_right_port():
    # 0x2255 == 8789; only the LISTEN row on that port counts, not the
    # ESTABLISHED one and not the other port's listener.
    assert dashboard._parse_listeners(_PROC_TCP, 0x2255, False) == ["127.0.0.1"]
    assert dashboard._parse_listeners(_PROC_TCP, 0x1F90, False) == ["0.0.0.0"]
    assert dashboard._parse_listeners(_PROC_TCP, 9999, False) == []
    assert dashboard._parse_listeners(_PROC_TCP6, 0x2255, True) == ["::1"]


def test_a_bind_we_cannot_classify_is_ignored(dash_env):
    """scope["server"] is a hostname under the test client and a filesystem path
    on a unix socket — neither says anything about network reach, and a value we
    cannot reason about is worse than admitting we don't know."""
    dash_env()
    for server in (None, ("testserver", 80), ("/run/dispatch.sock", None), ()):
        dashboard.note_bound_socket(server)
        assert dashboard._observed_bind["host"] is None, server
    dashboard.note_bound_socket(("127.0.0.1", 8777))
    assert dashboard._observed_bind == {"host": "127.0.0.1", "port": 8777}


def test_the_configured_bind_is_reported_as_unverified(dash_env, monkeypatch):
    dash_env()
    monkeypatch.setattr(config, "SETTINGS", replace(config.SETTINGS, host="127.0.0.1",
                                                    port=8765))
    bind = dashboard._bind_state(scan=False)
    assert bind["source"] == "configured"
    assert bind["verified"] is False
    assert bind["caveat"] == "configured, unverified"


def test_the_kernel_listener_table_confirms_a_loopback_bind(dash_env, monkeypatch):
    """The only way this page gets to say "contained" at all."""
    dash_env()
    dashboard.note_bound_socket(("127.0.0.1", 8777))
    monkeypatch.setattr(dashboard, "_listening_addrs", lambda port: ["127.0.0.1"])
    bind = dashboard._bind_state()
    assert bind["verified"] is True and bind["kind"] == "loopback"
    assert bind["reachable"] is False and bind["caveat"] == ""


def test_a_wider_listener_beats_a_loopback_observation(dash_env, monkeypatch):
    """The asymmetry that makes the scan worth doing: a socket bound to 0.0.0.0
    reports 127.0.0.1 as the local address of a request that arrived over
    loopback, so the observation ALONE would happily have said "contained"."""
    dash_env()
    dashboard.note_bound_socket(("127.0.0.1", 8777))
    monkeypatch.setattr(dashboard, "_listening_addrs", lambda port: ["0.0.0.0"])
    bind = dashboard._bind_state()
    assert bind["reachable"] is True and bind["verified"] is True
    assert "0.0.0.0" in bind["why"]


def test_an_observed_public_socket_is_reachable_without_a_scan(dash_env):
    dash_env()
    dashboard.note_bound_socket(("192.0.2.7", 8766))
    bind = dashboard._bind_state(scan=False)
    assert bind["source"] == "observed"
    assert bind["host"] == "192.0.2.7" and bind["port"] == 8766
    assert bind["reachable"] is True and bind["verified"] is True


# --------------------------------------------------------------------------- #
# Log tail
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("var", ["DISPATCH_LOG_FILE", "LOCAL_CHAT_LOG_FILE"])
def test_the_log_path_honours_both_env_names(dash_env, tmp_path, monkeypatch, var):
    """DISPATCH_LOG_FILE is the documented name and did nothing whatsoever while
    this read os.environ["LOCAL_CHAT_LOG_FILE"] directly."""
    dash_env()
    for name in ("DISPATCH_LOG_FILE", "LOCAL_CHAT_LOG_FILE"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "elsewhere.log"
    monkeypatch.setenv(var, str(target))
    assert dashboard.log_path() == target


def test_the_log_path_prefers_the_documented_name(dash_env, tmp_path, monkeypatch):
    dash_env()
    monkeypatch.setenv("DISPATCH_LOG_FILE", str(tmp_path / "new.log"))
    monkeypatch.setenv("LOCAL_CHAT_LOG_FILE", str(tmp_path / "legacy.log"))
    assert dashboard.log_path() == tmp_path / "new.log"


def test_missing_log_file_explains_where_the_logs_are(dash_env, monkeypatch):
    dash_env()
    monkeypatch.delenv("LOCAL_CHAT_LOG_FILE", raising=False)
    monkeypatch.delenv("DISPATCH_LOG_FILE", raising=False)
    body = asyncio.run(dashboard.tail_log(50))
    assert body["available"] is False
    assert "journalctl" in body["reason"]
    assert body["lines"] == []


def test_log_tail_returns_the_last_lines(dash_env, tmp_path, monkeypatch):
    dash_env()
    log_file = tmp_path / "app.log"
    log_file.write_text("".join(f"line {i}\n" for i in range(5000)))
    monkeypatch.setenv("LOCAL_CHAT_LOG_FILE", str(log_file))
    body = asyncio.run(dashboard.tail_log(10))
    assert body["available"] is True
    assert body["lines"][-1] == "line 4999"
    assert len(body["lines"]) == 10
    assert body["size_bytes"] == log_file.stat().st_size


def test_log_tail_handles_a_directory_at_the_path(dash_env, tmp_path, monkeypatch):
    dash_env()
    monkeypatch.setenv("LOCAL_CHAT_LOG_FILE", str(tmp_path))
    body = asyncio.run(dashboard.tail_log(10))
    assert body["available"] is False and body["lines"] == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores file permissions")
def test_log_tail_handles_an_unreadable_file(dash_env, tmp_path, monkeypatch):
    dash_env()
    log_file = tmp_path / "secret.log"
    log_file.write_text("nope\n")
    log_file.chmod(0o000)
    monkeypatch.setenv("LOCAL_CHAT_LOG_FILE", str(log_file))
    try:
        body = asyncio.run(dashboard.tail_log(10))
    finally:
        log_file.chmod(0o600)
    assert body["available"] is False and "cannot read" in body["reason"]


def test_log_route_serves_the_tail(dash_env, tmp_path, monkeypatch):
    client = dash_env()
    _unlock(client)
    log_file = tmp_path / "route.log"
    log_file.write_text("alpha\nbeta\ngamma\n")
    monkeypatch.setenv("LOCAL_CHAT_LOG_FILE", str(log_file))
    body = client.get("/api/dashboard/logs?lines=2").json()
    assert body["lines"] == ["beta", "gamma"]


# --------------------------------------------------------------------------- #
# The docs are part of the contract
# --------------------------------------------------------------------------- #


def test_every_finding_id_is_documented(clean_security):
    """An operator who sees an id on the page must be able to look it up. This
    walks the conditions the builder can emit and checks docs/dashboard.md
    mentions each one."""
    doc = (Path(__file__).resolve().parents[2] / "docs" / "dashboard.md")
    assert doc.exists(), "docs/dashboard.md is missing"
    text = doc.read_text()

    scenarios = [
        _facts(),
        _facts(process={"root": True, "uid": 0}),
        _facts(process={"root": True, "uid": 0, "container": "docker"}),
        _facts(storage={"disk": {"error": "boom"}}),
        _facts(storage={"disk": {"total_bytes": 100, "used_bytes": 99,
                                 "free_bytes": 1, "free_ratio": 0.01}}),
        _facts(storage={"disk": {"total_bytes": 100 * 1024**3, "used_bytes": 0,
                                 "free_bytes": 700 * 1024**2, "free_ratio": 0.3}}),
        _facts(storage={"writable": False}),
        _facts(storage={"cap_ratio": 0.9}),
        _facts(storage={"cap_ratio": 1.2}),
        _facts(database={"journal_mode": "delete"}),
        _facts(database={"check": {"kind": "quick_check", "ok": False, "detail": "x"}}),
        _facts(database={"backup": {"interval_seconds": 0, "last_backup_epoch": None}}),
        _facts(database={"backup": {"last_backup_epoch": None, "interval_seconds": 1}}),
        _facts(database={"backup": {"last_backup_ok": False, "corrupt_count": 1,
                                    "last_backup_epoch": int(time.time()),
                                    "interval_seconds": 21600}}),
        _facts(database={"backup": {"last_backup_epoch": 0, "last_backup_ok": True,
                                    "interval_seconds": 21600}}),
        _facts(database={"backup": {"error": "nope"}}),
        _facts(agent={"configured": False}),
        _facts(agent={"api_bots": 2}),
        _facts(agent={"present": False}),
        _facts(agent={"executable": False}),
        _facts(agent={"gateway": {"host": "127.0.0.1", "port": 18789,
                                  "reachable": False}}),
        _facts(database={"backup": {"unusable_count": 2, "dir": "/data/backups",
                                    "unusable_newest_at": "2026-08-04T02:00:00+00:00",
                                    "last_backup_epoch": None,
                                    "interval_seconds": 21600}}),
        _facts(storage={"blob_complete": False, "cap_ratio": 0.05}),
        _facts(clock={"future_data_seconds": 600}),
        _facts(clock={"future_data_seconds": 7200}),
        _facts(errors={"storage": "boom"}),
    ]
    ids = set()
    for facts in scenarios:
        ids.update(f["id"] for f in dashboard._build_findings(**facts))
    # The security block varies with auth state, so cover its other branches too.
    ids.update({"auth.no_pin", "auth.lock", "auth.api_token",
                "auth.api_token_default", "auth.api_token_weak",
                "auth.security_file_mode", "net.tls"})

    undocumented = sorted(i for i in ids
                          if not re.search(rf"(?<![\w.]){re.escape(i)}(?![\w.])", text))
    missing = [i for i in undocumented if i not in _AWAITING_DOCS]
    assert not missing, f"findings missing from docs/dashboard.md: {missing}"


# Ids this module emits that docs/dashboard.md does not describe YET. They were
# added by the dashboard-defect pass, which does not own docs/ — the doc entries
# land with the change that does, and this set must shrink to empty then. Every
# OTHER id stays strictly enforced above, which is the point of keeping the list
# explicit rather than loosening the assertion.
_AWAITING_DOCS = set()  # every finding id is documented in docs/dashboard.md
