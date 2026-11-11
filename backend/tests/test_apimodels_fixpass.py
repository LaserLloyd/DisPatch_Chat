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
from tests.test_model_catalog import KEY, _mock_chat, _transport, clients, run  # noqa: F401


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


def test_m3_image_scope_touches_no_chat_connection(clients, monkeypatch):  # noqa: F811
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


def test_l1_l2_note_and_source(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"models": [{"id": "grok-imagine-image"}]}))
    run(model_catalog.refresh_image("xai"))
    im = model_catalog.image_models("xai")
    assert "grok-imagine-image-2.0" in im["models"]          # documented id merged
    assert im["source"] == "mixed" and im["note"] == ""      # not the docs-only sentence
    assert im["checked"] == "2026-11-11"
    # a custom list has no documented ids: stays endpoint
    image_providers.save_config({"provider": "custom", "base_url": "https://pics.example.org/v1",
                                 "model": "m", "api_key": KEY})
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "a"}]}))
    run(model_catalog.refresh_image("custom"))
    assert model_catalog.image_models("custom")["source"] == "endpoint"
    # minimax stays documented with its note
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
    import importlib.util  # noqa: F401
    reads = []
    real = model_catalog._read
    monkeypatch.setattr(model_catalog, "_read", lambda: reads.append(1) or real())
    cat = model_catalog.catalog()
    for p in image_providers.PRESETS:
        model_catalog.image_models(p.id, cat)
    assert len(reads) == 1


# ---- M4 -------------------------------------------------------------------- #

def _job(model_cfg, age_s):
    image_providers.save_config(model_cfg)
    spec = image_jobs.ImageSpec(prompt="a lighthouse", backend="api")
    created = (datetime.now() - timedelta(seconds=age_s)).isoformat()
    return {"id": "j1", "spec": spec.to_json(), "created_at": created}


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
