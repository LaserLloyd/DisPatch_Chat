"""2.3.7 integrations: the "Works with DisPatch" catalog, the setup-guide
thread, and "Set up everything with one provider".

No test reaches a real API: every provider call goes to an httpx MockTransport.
"""
from __future__ import annotations

import base64
import json
import re

import httpx
import pytest

from app import catalog, config, firstrun, image_providers, llm_api, quicksetup
from tests.test_image_jobs import _png
from tests.test_platform_bots import _threads, env

# --------------------------------------------------------------------------- #
# The catalog is data, and public-clean
# --------------------------------------------------------------------------- #

#: Repositories verified public (unauthenticated 200) on 2026-10-14.
PUBLIC_REPOS = {
    "https://github.com/LaserLloyd/StudioForge",
    "https://github.com/LaserLloyd/DisPatch_Voice",
    "https://github.com/LaserLloyd/MailForge",
    "https://github.com/LaserLloyd/CrucibleForge",
    "https://github.com/LaserLloyd/ThemeForge",
    "https://github.com/LaserLloyd/InfoForge",      # 200 on 2026-10-18
    "https://github.com/openclaw/openclaw",
}


def test_every_catalog_entry_is_well_formed_and_links_only_public_repos():
    entries = catalog.entries()
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)) and len(ids) >= 10
    for e in entries:
        assert e["summary"] and e["connects"], e["id"]
        if e["repo"]:
            assert e["repo"] in PUBLIC_REPOS, e["repo"]
    raw = catalog.CATALOG_PATH.read_text()
    # The private image server is described, never linked or addressed.
    assert "ClawForge" in raw and "github.com/LaserLloyd/ClawForge" not in raw
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", raw)
    entry = next(e for e in entries if e["name"] == "SpeechForge")
    assert not entry["repo"] and entry["repo_note"]
    info = next(e for e in entries if e["name"] == "InfoForge")
    assert info["repo"] == "https://github.com/LaserLloyd/InfoForge" and not info["repo_note"]


def test_the_setup_guide_follows_the_catalog():
    guide = (firstrun.GUIDE_DIR / "set-up-add-ons.md").read_text()
    for e in catalog.entries():
        if e["guide"] and e["id"] not in ("ai-provider", "image-api"):
            assert e["name"] in guide, e["id"]
        if e["guide"] and e["repo"]:
            assert e["repo"] in guide, e["id"]
    # The recommended order.
    order = ["An AI provider", "Image generation", "StudioForge", "OpenClaw",
             "DisPatch Voice", "InfoForge", "MailForge"]
    pos = [guide.index(f"## {i}. {name}") for i, name in enumerate(order, 1)]
    assert pos == sorted(pos)
    assert "one key" in guide.lower()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", guide.replace("127.0.0.1", ""))


def test_the_catalog_route_reports_a_fresh_install(env):
    c = env(real_defaults=True)
    rows = {r["id"]: r for r in c.get("/api/plugins/catalog").json()["entries"]}
    assert rows["ai-provider"]["status"] == "not_configured"
    assert rows["image-api"]["status"] == "not_configured"
    assert rows["themeforge"]["status"] == "bundled"
    assert rows["lookups"]["status"] == "connected"
    assert rows["mailforge"]["status"] in ("connected", "not_configured")
    assert all(r["docs"].startswith(catalog.DOCS_URL + "#") for r in rows.values())
    # After an image provider is saved, the card says so.
    image_providers.save_config({"provider": "minimax", "api_key": "secret-k"})
    body = c.get("/api/plugins/catalog").text
    assert "secret-k" not in body
    rows = {r["id"]: r for r in json.loads(body)["entries"]}
    assert rows["image-api"]["status"] == "connected"
    assert rows["image-api"]["detail"] == "MiniMax (image-01)"


def test_a_probe_that_raises_reads_as_unknown():
    def boom(_):
        raise RuntimeError("x")
    rows = catalog.resolve({"chat": boom})
    assert next(r for r in rows if r["id"] == "ai-provider")["status"] == "unknown"


# --------------------------------------------------------------------------- #
# Posting a guide on an existing install
# --------------------------------------------------------------------------- #


def test_the_guide_can_be_posted_once_on_demand(env, monkeypatch):
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "0")      # an "existing" install
    c = env(real_defaults=True)
    assert _threads(c) == []
    r = c.post("/api/guide/seed?name=set-up-add-ons")
    assert r.status_code == 200, r.text
    (t,) = _threads(c)
    assert t["title"] == "Set up add-ons" and t["id"] == r.json()["thread_id"]
    assert c.post("/api/guide/seed?name=set-up-add-ons").status_code == 409
    assert c.post("/api/guide/seed?name=set-up-add-ons&force=true").status_code == 200
    assert c.post("/api/guide/seed?name=nope").status_code == 404


# --------------------------------------------------------------------------- #
# One key, every capability
# --------------------------------------------------------------------------- #


def _b64png():
    return base64.b64encode(_png()).decode()


def _fake(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=read_timeout,
                                 follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    monkeypatch.setattr(image_providers, "transport", transport)
    monkeypatch.setattr(quicksetup, "transport", transport)
    return seen


def _provider_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "m1"}]})
    if path.endswith("/image_generation"):
        return httpx.Response(200, json={"data": {"image_base64": [_b64png()]},
                                         "base_resp": {"status_code": 0}})
    if path.endswith("/images/generations"):
        return httpx.Response(200, json={"data": [{"b64_json": _b64png()}]})
    if path.endswith("/embeddings"):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    return httpx.Response(404)


def _rows(r):
    return {x["id"]: x["status"] for x in r.json()["rows"]}


def test_one_minimax_key_sets_up_chat_and_pictures(env, monkeypatch):
    seen = _fake(monkeypatch, _provider_handler)
    c = env(real_defaults=True)
    r = c.post("/api/llm/quicksetup", json={"provider": "minimax", "api_key": "mm-one"})
    assert r.status_code == 200, r.text
    assert "mm-one" not in r.text
    assert _rows(r) == {"chat": "ok", "lookups": "ok", "images": "ok",
                        "embeddings": "absent", "voice": "absent"}
    atlas = config.get_bot("atlas")
    (entry,) = atlas.advisor["providers"]
    assert entry["provider"] == "minimax" and entry["model"] == "MiniMax-M3"
    assert entry["api_key"] == "mm-one"
    # Stored ONCE: the image config has no key of its own and borrows Atlas's.
    img = image_providers.load_config()
    assert img["provider"] == "minimax" and not img["api_key"]
    assert image_providers.resolve().key_source == "shared"
    # Exactly one picture was generated, and every call went to one host.
    assert sum(1 for q in seen if q.url.path.endswith("/image_generation")) == 1
    assert {q.url.host for q in seen} == {"api.minimax.io"}


def test_openai_also_gets_embeddings(env, monkeypatch):
    seen = _fake(monkeypatch, _provider_handler)
    c = env(real_defaults=True)
    r = c.post("/api/llm/quicksetup", json={"provider": "openai", "api_key": "sk-one"})
    assert _rows(r)["embeddings"] == "ok", r.text
    emb = config.get_bot("atlas").advisor["embeddings"]
    assert emb["model"] == "text-embedding-3-small"
    assert emb["base_url"] == "https://api.openai.com/v1"
    assert {q.url.host for q in seen} == {"api.openai.com"}


def test_a_failed_capability_is_marked_not_hidden(env, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(403, json={"error": {"message": "no images on this plan"}})
        return _provider_handler(request)
    _fake(monkeypatch, handler)
    c = env(real_defaults=True)
    r = c.post("/api/llm/quicksetup", json={"provider": "xai", "api_key": "x"})
    rows = {x["id"]: x for x in r.json()["rows"]}
    assert rows["chat"]["status"] == "ok"
    assert rows["images"]["status"] == "failed"
    assert "rejected the API key" in rows["images"]["detail"]
    assert rows["embeddings"]["status"] == "absent"


def test_without_tests_nothing_leaves_the_box(env, monkeypatch):
    seen = _fake(monkeypatch, _provider_handler)
    c = env(real_defaults=True)
    r = c.post("/api/llm/quicksetup", json={"provider": "minimax", "api_key": "k",
                                            "test": False})
    assert set(_rows(r).values()) <= {"skipped", "ok", "absent"}
    assert seen == []


@pytest.mark.parametrize("body", [{"provider": "nope", "api_key": "k"},
                                  {"provider": "minimax"}])
def test_bad_requests_are_400(env, body, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    c = env(real_defaults=True)
    assert c.post("/api/llm/quicksetup", json=body).status_code == 400


# --------------------------------------------------------------------------- #
# A direct-API bot with Pictures on learns the marker only when it can work
# --------------------------------------------------------------------------- #


def test_picture_rules_reach_the_prompt_only_when_they_can_work(monkeypatch):
    from dataclasses import replace

    from app import advisor
    bot = config.Bot(id="a", name="A", image_jobs=True,
                     advisor={"providers": [{"provider": "openai", "model": "m"}],
                              "tools": False})
    cfg = advisor.AdvisorConfig.from_bot(bot)
    assert cfg.pictures is True

    def prompt(c):
        return advisor.build_system_prompt(c, "", [], "Monday")

    monkeypatch.setattr(advisor, "pictures_available", lambda: False)
    assert advisor.PICTURE_RULES not in prompt(cfg)
    monkeypatch.setattr(advisor, "pictures_available", lambda: True)
    assert advisor.PICTURE_RULES in prompt(cfg)
    assert advisor.PICTURE_RULES not in prompt(cfg.locked_down())
    assert advisor.PICTURE_RULES not in prompt(replace(cfg, pictures=False))
    own = replace(cfg, persona="I draw with [[pic:…]] when asked.")
    assert advisor.PICTURE_RULES not in prompt(own)


def test_the_bots_tab_can_switch_pictures(env):
    c = env(real_defaults=True)
    r = c.put("/api/bots/order", json={"bots": [{"id": "atlas", "order": 0,
                                                  "image_jobs": True}]})
    assert r.status_code == 200, r.text
    assert config.get_bot("atlas").image_jobs is True
    r = c.patch("/api/bots/atlas", json={"image_jobs": False})
    assert r.status_code == 200, r.text
    assert config.get_bot("atlas").image_jobs is False


def test_every_catalog_entry_has_a_section_in_the_docs():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / "docs" / "integrations.md").read_text()
    for e in catalog.entries():
        assert f'<a id="{e["id"]}"></a>' in doc, e["id"]
    assert "github.com/LaserLloyd/ClawForge" not in doc
