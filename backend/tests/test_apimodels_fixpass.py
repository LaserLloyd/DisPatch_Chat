"""Fix-pass for the available-models round: SSRF guard on refresh, image-only refresh,
default_model side effect, H3 chat deadline, catalog hygiene (mock transport only)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import httpx
import pytest

from app import config, connections, image_jobs, image_providers, llm_api, main, model_catalog
from tests.test_model_catalog import KEY, _mock_chat, _transport, clients, run


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(model_catalog, "_last", {})
    monkeypatch.setattr(image_providers, "transport", None)
    for n in ("MINIMAX_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "CIVITAI_API_KEY"):
        monkeypatch.delenv(n, raising=False)


def test_m1_custom_preset_at_metadata_address_makes_no_request(monkeypatch):
    image_providers.save_config({"provider": "custom", "base_url": "http://169.254.169.254/v1",
                                 "model": "m", "api_key": KEY})
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "x"}]}))
    e = run(model_catalog.refresh_image("custom"))
    assert seen == [] and e["error"] and e["source"] == "documented"


def test_m3_refresh_does_not_fill_default_model(monkeypatch):
    _mock_chat(monkeypatch, ("text-embedding-3-small", "gpt-4o"))
    c = connections.create(type="openai", api_key=KEY)
    assert not connections.get(c.id).default_model
    run(model_catalog.refresh_connection(c))
    got = connections.get(c.id)
    assert sorted(got.models) == ["gpt-4o", "text-embedding-3-small"] and not got.default_model
    assert got.models_checked_at and got.last_test
    # the Test button keeps today's behaviour
    c2 = connections.create(type="openai", api_key=KEY)
    run(connections.test_and_record(c2))
    assert connections.get(c2.id).default_model


def test_m3_bulk_refresh_does_not_fill_default_model(monkeypatch):
    _mock_chat(monkeypatch, ("m1", "m2"))
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    c = connections.create(type="openai", api_key=KEY)
    run(model_catalog.refresh_all())
    assert not connections.get(c.id).default_model


def test_m3_image_scope_touches_no_chat_connection(clients, monkeypatch):
    owner, member = clients
    seen_chat = _mock_chat(monkeypatch)
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    c = connections.create(type="openai", api_key=KEY)
    before = connections.get(c.id).last_test
    r = owner.post("/api/connections/models/refresh?scope=image")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["connections"] == {} and set(j["image"]) == set(image_providers.PRESET_BY_ID)
    assert seen_chat == [] and connections.get(c.id).last_test == before
    assert member.post("/api/connections/models/refresh?scope=image").status_code == 403


def test_l1_l8_note_and_source(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"models": [{"id": "grok-imagine-image"}]}))
    run(model_catalog.refresh_image("xai"))
    im = model_catalog.image_models("xai")
    # a live list is authoritative: documented ids are NOT merged into it
    assert im["models"] == ["grok-imagine-image"]
    assert im["source"] == "endpoint" and im["note"] == "" and im["checked"] == "2026-11-11"
    # an empty live list falls back to the documented one
    monkeypatch.setattr(model_catalog, "_last", {})
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"models": []}))
    run(model_catalog.refresh_image("xai"))
    im = model_catalog.image_models("xai")
    assert im["source"] == "documented" and "grok-imagine-image-2.0" in im["models"]
    mm = model_catalog.image_models("minimax")
    assert mm["source"] == "documented" and "no image models" in mm["note"]


def test_l6_a_dripping_server_is_cut_at_the_total_deadline(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setattr(model_catalog, "TIMEOUT_S", 0.2)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.1)
                yield b" "
    monkeypatch.setattr(image_providers, "transport",
                        httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow())))
    import time
    s = time.monotonic()
    e = run(model_catalog.refresh_image("openai"))
    assert time.monotonic() - s < 2 and e["source"] == "documented" and e["error"]


def test_l7_concurrent_clicks_pass_the_cooldown_once(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "gpt-image-1"}]}))

    async def both():
        return await asyncio.gather(model_catalog.refresh_image("openai"),
                                    model_catalog.refresh_image("openai"))
    a, b = run(both())
    assert len(seen) == 1 and sorted(bool(x.get("cached")) for x in (a, b)) == [False, True]


def test_l8_a_stale_tmp_file_does_not_keep_loose_permissions():
    p = model_catalog._path()
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text("old")
    os.chmod(tmp, 0o666)
    model_catalog._store("image", "openai", {"models": []})
    assert p.stat().st_mode & 0o777 == 0o600


def test_provider_rows_read_the_catalog_file_once(monkeypatch):
    reads = []
    real = model_catalog._read
    monkeypatch.setattr(model_catalog, "_read", lambda: reads.append(1) or real())
    cat = model_catalog.catalog()
    for p in image_providers.PRESETS:
        model_catalog.image_models(p.id, cat)
    assert len(reads) == 1


# ---- M4 -------------------------------------------------------------------- #

import itertools  # noqa: E402

_ids = itertools.count()


def _job(model_cfg, age_s):
    image_providers.save_config(model_cfg)
    spec = image_jobs.ImageSpec(prompt="a lighthouse", backend="api")
    created = (datetime.now() - timedelta(seconds=age_s)).isoformat()
    return {"id": f"j-{next(_ids)}", "spec": spec.to_json(), "created_at": created}


def test_m4_h3_api_job_outlives_its_request():
    h3 = {"provider": "minimax", "api_key": KEY, "model": "MiniMax-H3-Image"}
    assert main._image_job_expired(_job(h3, image_jobs.DEADLINE_S + 60)) is False
    assert main._image_job_expired(
        _job(h3, image_providers.H3_TIMEOUT_S + 130)) is True


def test_m4_other_models_keep_the_normal_deadline():
    old = {"provider": "minimax", "api_key": KEY, "model": "image-01"}
    assert main._image_job_expired(_job(old, image_jobs.DEADLINE_S + 60)) is True
    assert main._image_job_expired(_job(old, 30)) is False


def test_m4_cancelled_h3_request_is_logged_without_key_or_prompt(monkeypatch, caplog):
    image_providers.save_config({"provider": "minimax", "api_key": KEY, "model": "MiniMax-H3-Image"})
    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(image_providers, "transport", httpx.MockTransport(hang))

    async def go():
        res = image_providers.resolve()
        spec = image_jobs.ImageSpec(prompt="secret prompt words", backend="api")
        t = asyncio.ensure_future(image_providers.generate_batch(res, spec))
        await started.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    with caplog.at_level(logging.WARNING, logger="local-chat.image_providers"):
        run(go())
    text = caplog.text
    assert "MiniMax-H3-Image" in text and "minimax" in text.lower()
    assert KEY not in text and "secret prompt" not in text


# ---- M-B: a failed refresh does not start the cooldown ----------------------- #

def test_mb_failed_image_refresh_can_be_retried_at_once(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    state = {"ok": False}
    seen = _transport(monkeypatch, lambda r: httpx.Response(
        200, json={"data": [{"id": "gpt-image-1"}]}) if state["ok"] else httpx.Response(401))
    a = run(model_catalog.refresh_image("openai"))
    assert a["error"]
    state["ok"] = True
    b = run(model_catalog.refresh_image("openai"))
    assert len(seen) == 2 and not b.get("cached") and b["error"] is None and b["source"] == "endpoint"


def test_mb_failed_connection_refresh_can_be_retried_at_once(monkeypatch):
    state = {"ok": False}
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [{"id": "m1"}]}) if state["ok"] else httpx.Response(401)

    monkeypatch.setattr(llm_api, "http_client", lambda read_timeout=llm_api.READ_TIMEOUT_S: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=read_timeout, follow_redirects=False))
    c = connections.create(type="openai", api_key=KEY)
    a = run(model_catalog.refresh_connection(c))
    assert a["error"]
    state["ok"] = True
    n = len(seen)
    b = run(model_catalog.refresh_connection(c))
    assert len(seen) > n and not b.get("cached") and b["error"] is None


# ---- L-1: the H3 deadline is decided once per job ---------------------------- #

def test_l1_deadline_resolves_once_per_job(monkeypatch):
    h3 = {"provider": "minimax", "api_key": KEY, "model": "MiniMax-H3-Image"}
    job = _job(h3, 30)
    calls = []
    real = image_providers.resolve
    monkeypatch.setattr(image_providers, "resolve", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(5):
        assert main._image_job_deadline_s(job) == image_providers.H3_TIMEOUT_S + 120
    assert len(calls) == 1


# ---- L-2: the bulk audit row names its scope --------------------------------- #

def test_l2_bulk_audit_names_the_scope(clients, monkeypatch):
    from app import connections_api
    owner, _ = clients
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    rows = []
    monkeypatch.setattr(connections_api, "audit", lambda req, kind, target, detail="": rows.append((kind, detail)))
    assert owner.post("/api/connections/models/refresh?scope=image").status_code == 200
    assert owner.post("/api/connections/models/refresh").status_code == 200
    assert "scope=image" in rows[0][1] and "scope=all" in rows[1][1]


# ---- L-9: a machine key cannot refresh --------------------------------------- #

def test_l9_machine_key_is_refused_on_both_refresh_routes(world, monkeypatch):
    from app import auth
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    world.create_owner(world.client())
    cfg = auth.load()
    cfg.api_token = "machine-token-0123456789"      # scrub-ok: test token
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": "machine-token-0123456789"})   # scrub-ok: test token
    for path in ("/api/connections/models/refresh", "/api/apps/imagestudio/models/refresh"):
        assert machine.post(path).status_code in (401, 403), path
    assert seen == []
