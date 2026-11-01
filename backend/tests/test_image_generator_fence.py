"""WP3 (Civitai plan): the server fences. Civitai is studio-only (never a chat-picture maker,
never the saved default, never reachable from the image-job routes or the provider Test), kids'
bots can never draw, and the "standard generators" switch gates every API-backed picture path.

Run: cd backend && uv run pytest tests/test_image_generator_fence.py
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from app import (
    advisor,
    auth,
    config,
    image_jobs,
    image_providers,
    invariants,
    main,
    quicksetup,
    settings_store,
    starter_packs,
)
from tests.test_image_jobs import _messages, _png, _say, _thread
from tests.test_image_jobs import env as _jobs_env
from tests.test_image_providers import Fake, _drive, _minimax_ok, _save


@pytest.fixture
def env(_jobs_env, monkeypatch):
    """The image-jobs fixture with NO MCP server: only the API path."""
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, clawforge_url="", image_jobs_enabled=False, image_jobs_flag="auto"))
    monkeypatch.setattr(image_providers, "backend", image_providers.ApiImages())
    yield _jobs_env
    monkeypatch.setattr(image_providers, "transport", None)


def _counting(monkeypatch, handler=None) -> Fake:
    fake = Fake(handler or _minimax_ok)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(fake))
    return fake


def _hand_write_civitai():
    """What a hand edit / restored backup could leave: image.yaml naming Civitai."""
    image_providers.config_path().write_text("provider: civitai\n", encoding="utf-8")


# 1 -------------------------------------------------------------------------- #

def test_a_hand_written_civitai_default_reaches_nothing(env, monkeypatch):
    fake = _counting(monkeypatch)
    _hand_write_civitai()
    c = env()
    tid = _thread(c)
    _say(c, tid, "Look [[pic:a lighthouse]] there.")
    rows = _messages(c, tid)
    assert "[[pic" not in rows[0]["content"]
    assert not [m for m in rows if (m.get("metadata") or {}).get("kind") == "image_job"]
    assert len(rows) == 2 and "needs an image generator" in rows[1]["content"]
    _drive()
    assert fake.requests == []

    r = c.post("/api/image-jobs", json={"bot_id": "main", "thread_id": tid, "prompt": "a lighthouse"})
    assert r.status_code == 503, r.text
    assert advisor.pictures_available() is False
    with pytest.raises(image_jobs.ImageJobError):
        asyncio.run(image_providers.ApiImages().enqueue(image_jobs.ImageSpec(prompt="x", backend="api")))
    assert fake.requests == []


def test_i13_reports_a_studio_only_default(env):
    assert not [v for v in invariants.check() if v.code == "I13"]
    _hand_write_civitai()
    (v,) = [v for v in invariants.check() if v.code == "I13"]
    assert v.subject == "image.yaml"
    assert v.detail == "the default picture maker names a studio-only provider"
    assert "I13" in invariants.INVARIANTS
    assert invariants.report()["by_invariant"]["I13"] == 1


def test_i13_on_the_live_route(world):
    owner = world.create_owner(world.client())
    r = owner.get("/api/health/invariants")
    assert r.status_code == 200 and r.json()["by_invariant"]["I13"] == 0
    _hand_write_civitai()
    body = owner.get("/api/health/invariants").json()
    assert body["by_invariant"]["I13"] == 1
    assert any(v["code"] == "I13" and v["subject"] == "image.yaml" for v in body["violations"])


# 2 -------------------------------------------------------------------------- #

def test_civitai_cannot_be_saved_tested_or_listed(world, monkeypatch):
    fake = _counting(monkeypatch)
    owner = world.create_owner(world.client())
    r = owner.put("/api/llm/image", json={"provider": "civitai"})
    assert r.status_code == 422, r.text
    r = owner.post("/api/llm/image/test", json={"provider": "civitai"})
    assert r.status_code == 422, r.text
    r = owner.get("/api/llm/image")
    assert r.status_code == 200
    assert "civitai" not in [p["id"] for p in r.json()["presets"]]
    assert fake.requests == []


# 3 -------------------------------------------------------------------------- #

def test_no_quicksetup_plan_picks_a_studio_only_preset():
    for pid, plan in quicksetup.PLANS.items():
        if plan.image_preset:
            assert not image_providers.PRESET_BY_ID[plan.image_preset].studio_only, pid


# 4 -------------------------------------------------------------------------- #

def test_no_shipped_kids_bot_can_draw(env, monkeypatch):
    for pack_id in [p["id"] for p in starter_packs.list_packs()]:
        for ch in starter_packs.get_pack(pack_id)["characters"]:
            assert not getattr(ch, "image_jobs", False), (pack_id, ch.id)
    fake = _counting(monkeypatch)
    _save("minimax", api_key="mm")
    starter_packs.install("animal_friends")
    config._invalidate_bots_cache()
    kids = [b for b in config.load_bots() if b.id.startswith("kids_")]
    assert kids and not any(b.image_jobs for b in kids)
    c = env()
    tid = _thread(c, kids[0].id)
    _say(c, tid, "Hi [[pic:an owl]] there.")
    rows = _messages(c, tid)
    assert not [m for m in rows if (m.get("metadata") or {}).get("kind") == "image_job"]
    _drive()
    assert fake.requests == []


# 5, 6 ----------------------------------------------------------------------- #

@pytest.fixture
def gated(world, monkeypatch):
    """A claimed install: the Owner, a User (Uma), a second Admin (Ada); household bot `pic` may draw."""
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, clawforge_url="", image_jobs_enabled=False, image_jobs_flag="auto"))
    monkeypatch.setattr(image_providers, "backend", image_providers.ApiImages())
    monkeypatch.setattr(main, "_shutting_down", False)
    fake = _counting(monkeypatch)
    owner = world.create_owner(world.client())
    _save("minimax", api_key="mm")
    from agents_world import sibling
    from app import accounts
    base = next(b for b in config.load_bots() if b.id == "main")
    config.upsert_bot(replace(base, id="pic", name="Pic", image_jobs=True, scope="household",
                              owner_profile="", template="", give_to=None, agent="",
                              advisor={"providers": []}))
    config._invalidate_bots_cache()
    out = {"world": world, "owner": owner, "fake": fake}
    for i, (name, role, kw) in enumerate((("Uma", "user", {"pin": "5555"}),
                                          ("Ada", "admin", {"password": "pw-pass-1234"}))):  # scrub-ok: throwaway test password
        p = world.person(owner, name, role, **kw)
        c = sibling(world, owner, (f"203.0.113.{200 + i}", 49300 + i))
        assert world.login(c, profile_id=p["id"], **kw).status_code == 200
        out[name] = c
        accounts.update_profile(p["id"], household_bots=["pic"])
    cfg = auth.load()
    cfg.api_token = "fence-machine-token"  # scrub-ok: throwaway test fixture
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": cfg.api_token})
    out["machine"] = machine
    yield out
    monkeypatch.setattr(image_providers, "transport", None)


def _job(c, tid):
    return c.post("/api/image-jobs", json={"bot_id": "pic", "thread_id": tid, "prompt": "a lighthouse"})


def _new_thread(c):
    r = c.post("/api/threads", json={"bot_id": "pic"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_the_standard_switch_gates_markers_and_the_job_route(gated):
    uma, owner, fake = gated["Uma"], gated["owner"], gated["fake"]
    settings_store.set_many({"connections.image_access.standard_user": False})
    tid = _new_thread(uma)
    r = _job(uma, tid)
    assert r.status_code == 403, r.text
    _say(gated["machine"], tid, "Look [[pic:a lighthouse]] there.")
    rows = _messages(uma, tid)
    assert not [m for m in rows if (m.get("metadata") or {}).get("kind") == "image_job"]
    assert any("may not use the image generators" in m["content"] for m in rows)
    _drive()
    assert fake.requests == []

    settings_store.set_many({"connections.image_access.standard_user": True})
    r = _job(uma, tid)
    assert r.status_code == 202, r.text
    _drive()
    assert len(fake.requests) == 1


def test_a_clawforge_backed_bot_ignores_the_standard_switch(gated, monkeypatch):
    settings_store.set_many({"connections.image_access.standard_user": False})
    monkeypatch.setattr(main, "SETTINGS", replace(
        main.SETTINGS, clawforge_url="http://rig.invalid:8700/mcp", image_jobs_enabled=True))
    monkeypatch.setattr(image_providers, "load_config", lambda: {})     # automatic = the rig
    uma = gated["Uma"]
    assert main._choose_image_backend("") == "clawforge"
    tid = _new_thread(uma)
    assert asyncio.run(main._thread_owner_may_generate(tid, "standard")) is False
    r = _job(uma, tid)
    assert r.status_code == 202, r.text         # the gate only fences the API backend
    assert gated["fake"].requests == []


def test_provider_test_needs_the_standard_switch(gated):
    ada, owner, fake = gated["Ada"], gated["owner"], gated["fake"]
    settings_store.set_many({"connections.image_access.standard_admin": False})
    r = ada.post("/api/llm/image/test", json={"provider": "minimax", "api_key": "mm"})
    assert r.status_code == 403, r.text
    assert fake.requests == []
    r = owner.post("/api/llm/image/test", json={"provider": "minimax", "api_key": "mm"})
    assert r.status_code == 200, r.text
    assert len(fake.requests) == 1
