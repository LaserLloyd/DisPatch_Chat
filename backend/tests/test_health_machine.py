"""/api/health: who is allowed to see the degraded-state detail.

The detailed body carries the signals that say the app is quietly broken —
`avatar_snapshots_missing` (chats are showing the wrong face),
`reaction_fire_failures_24h`, `gateway_ws.truncation_unrepaired` (replies are
being delivered cut short), backup freshness. Those exist to be MONITORED, and
a monitor holds no session: it is a cron job or a smoke script on the box.

Gating them on a session alone therefore made every one of them invisible to
the only caller whose job is to notice, which is this codebase's recurring
failure mode — a fault that reports nothing looks exactly like health.

So the rule is the same three-part machine test the auth gate uses: loopback
socket, no proxy header in front, no browser fingerprint. These tests pin all
four corners of it, because the value of the widening is entirely in the
corners it does NOT widen.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.database import Database

# A browser stamps these on fetch/XHR; agents, curl and cron do not.
BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}

# Fields that are operator detail, not liveness.
DETAIL_KEYS = {"data_dir", "clients", "last_backup_ok", "gateway_ok",
               "avatar_snapshots_missing", "reaction_fire_failures_24h",
               "image_job_failures_24h"}


def _client(tmp_path, monkeypatch, host: str):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    config._invalidate_bots_cache()
    # Never let the health probe open a real socket to the gateway.
    monkeypatch.setattr(main, "_gateway_probe", {"ts": 1e18, "ok": True})
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    return temp_db, TestClient(main.app, client=(host, 50000))


@pytest.fixture
def on_box(tmp_path, monkeypatch):
    """A caller whose socket peer is this machine."""
    temp_db, tc = _client(tmp_path, monkeypatch, "127.0.0.1")
    with tc as c:
        yield c
    asyncio.run(temp_db.close())


@pytest.fixture
def off_box(tmp_path, monkeypatch):
    """A caller from somewhere else on the network."""
    temp_db, tc = _client(tmp_path, monkeypatch, "203.0.113.5")
    with tc as c:
        yield c
    asyncio.run(temp_db.close())


def test_liveness_is_always_answered_without_a_session(on_box):
    """The container healthcheck runs before anyone has ever unlocked."""
    auth.set_pin("1234")
    body = on_box.get("/api/health").json()
    assert body["status"] == "ok"
    assert "db_integrity_ok" in body


def test_on_box_machine_sees_the_degraded_state_detail(on_box):
    auth.set_pin("1234")                      # locked; nobody has unlocked
    body = on_box.get("/api/health").json()
    missing = DETAIL_KEYS - set(body)
    assert not missing, f"a monitor on this box could not see {sorted(missing)}"
    # The specific signal that started this: a lost avatar snapshot store was
    # only discoverable by a human reading the journal.
    assert body["avatar_snapshots_missing"] == 0
    assert "gateway_ws" in body


def test_a_locked_browser_tab_on_this_box_still_gets_the_short_body(on_box):
    """The whole risk of the machine branch: this box's OWN tab after the idle
    auto-lock connects over loopback too. It must stay on the two-field body —
    the data directory and client count are reconnaissance for whoever picked
    up the tablet."""
    auth.set_pin("1234")
    body = on_box.get("/api/health", headers=BROWSER).json()
    assert set(body) == {"status", "db_integrity_ok"}, body


def test_a_remote_machine_gets_the_short_body(off_box):
    """Bound on 0.0.0.0, this endpoint is reachable from the whole network.
    Being a machine is not the qualification — being ON THE BOX is."""
    auth.set_pin("1234")
    body = off_box.get("/api/health").json()
    assert set(body) == {"status", "db_integrity_ok"}, body


def test_a_proxied_caller_is_not_treated_as_on_box(on_box):
    """A reverse proxy (a tailnet Serve terminating TLS) forwards from
    127.0.0.1, so the socket peer lies. Any forwarding header means the request
    did not originate here."""
    auth.set_pin("1234")
    for header in ("X-Forwarded-For", "Forwarded", "Tailscale-Headers-Info"):
        body = on_box.get("/api/health", headers={header: "203.0.113.7"}).json()
        assert set(body) == {"status", "db_integrity_ok"}, (header, body)


def test_a_full_session_still_sees_everything(on_box):
    auth.set_pin("1234")
    assert on_box.post("/api/auth/unlock", json={"pin": "1234"}).status_code == 200
    body = on_box.get("/api/health", headers=BROWSER).json()
    assert DETAIL_KEYS <= set(body)


def test_with_no_pin_configured_the_app_is_open_by_design(off_box):
    """Unchanged behaviour: no PIN means no lock, for anyone."""
    assert auth.load().pin_set is False
    body = off_box.get("/api/health").json()
    assert DETAIL_KEYS <= set(body)


# --------------------------------------------------------------------------- #
# A background loop that dies must not report as health
# --------------------------------------------------------------------------- #


def test_background_loops_are_reported_and_a_live_one_is_not_stale(on_box):
    body = on_box.get("/api/health").json()
    loops = body["background_loops"]
    assert "session_purge" in loops, loops
    beat = loops["session_purge"]
    assert beat["stale"] is False and beat["period_s"] > 0
    assert body["status"] == "ok"


def test_a_stale_loop_degrades_the_top_level_status(on_box, monkeypatch):
    """The loops used to die on the first exception with nothing to show for
    it: sessions stopped being purged, snapshots stopped being taken, and
    /api/health kept answering "ok"."""
    import time as _time

    monkeypatch.setitem(main._loop_beats, "session_purge",
                        (_time.time() - 100_000, 300))
    body = on_box.get("/api/health").json()
    assert body["background_loops"]["session_purge"]["stale"] is True
    # Visible WITHOUT a session too — the short body is what a monitor reads.
    assert body["status"] == "degraded"
    auth.set_pin("1234")
    short = on_box.get("/api/health", headers=BROWSER).json()
    assert short == {"status": "degraded", "db_integrity_ok": short["db_integrity_ok"]}


def test_a_loop_that_keeps_running_after_a_failed_pass(on_box, monkeypatch):
    """One bad pass must not end the loop (it used to unwind the whole task)."""
    calls: list[int] = []

    def _boom():
        calls.append(1)
        raise RuntimeError("transient")

    monkeypatch.setattr(main.auth, "purge_expired", _boom)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", _fast_sleep)
    asyncio.run(main._session_purge_loop())
    assert len(calls) == 3, "the loop stopped at the first failing pass"


def test_backup_staleness_is_visible_even_though_last_backup_ok_latches(on_box,
                                                                       monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "_last_backup_ok", True)      # latched success
    monkeypatch.setattr(main, "SETTINGS",
                        replace(main.SETTINGS, backup_interval=3600))
    monkeypatch.setattr(main, "_last_backup_at",
                        (datetime.now() - timedelta(days=7)).isoformat())
    body = on_box.get("/api/health").json()
    assert body["last_backup_ok"] is True     # additive: the old field is intact
    assert body["backup_stale"] is True
    assert body["backup_age_s"] > 3600 * 2
    assert body["status"] == "degraded"
