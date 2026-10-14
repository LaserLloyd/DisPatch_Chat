"""2.3 platform round, bots side: the Atlas default roster, the first-run guide
threads, the bot management routes and "Connect an AI" -> a direct-API bot.

Hermetic like the rest of tests/: data dir, config and DB are throwaway.
Run: cd backend && uv run pytest tests/test_platform_bots.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import advisor, auth, config, firstrun, llm_api, main
from app.database import Database


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir + DB. `real_defaults=True` swaps the suite's test
    roster for the roster the product really ships."""
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
    main._delivered.clear()
    main._thread_bot.clear()
    clients: list[TestClient] = []
    dbs: list[Database] = []

    def make(real_defaults: bool = False, *, fresh_db: bool = True) -> TestClient:
        if real_defaults:
            monkeypatch.setattr(config, "DEFAULT_BOTS", config.shipped_default_bots())
        config._invalidate_bots_cache()
        if fresh_db or not dbs:
            d = Database(tmp_path / "chats.db")
            dbs.append(d)
            monkeypatch.setattr(main, "db", d)
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    make.tmp_path = tmp_path
    yield make
    for c in clients:
        c.__exit__(None, None, None)
    for d in dbs:
        asyncio.run(d.close())


def _threads(client: TestClient, bot="atlas"):
    return client.get("/api/threads", params={"bot_id": bot}).json()["threads"]


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def test_the_shipped_roster_is_atlas_alone():
    (b,) = config.shipped_default_bots()
    assert (b.id, b.name, b.emoji, b.order) == ("atlas", "Atlas", "🌍", 0)
    assert b.visible and b.safe and not b.reactions and not b.avatar_pool
    assert b.model_hint == "Direct API"
    assert b.advisor == {"providers": [], "tools": True}


def test_a_fresh_install_materialises_atlas_and_lists_it(env):
    c = env(real_defaults=True)
    written = yaml.safe_load((env.tmp_path / "config.yaml").read_text())
    assert written["bots"][0]["id"] == "atlas"      # (the Job Board app adds its own)
    assert written["bots"][0]["advisor"] == {"providers": [], "tools": True}
    ids = [b["id"] for b in c.get("/api/bots").json()["bots"]]
    assert ids == ["atlas"]
    row = next(b for b in c.get("/api/bots/all").json()["bots"] if b["id"] == "atlas")
    assert (row["backend"], row["tools"], row["connected"]) == ("direct", True, False)


def test_an_existing_config_is_never_rewritten(env):
    (env.tmp_path / "config.yaml").write_text(
        "bots:\n  - id: mine\n    name: Mine\n    safe: true\n")
    c = env(real_defaults=True)
    ids = [b["id"] for b in c.get("/api/bots/all").json()["bots"]]
    assert "mine" in ids and "atlas" not in ids     # (an app may add its own bot)
    assert config.get_bot("mine").safe is True


# --------------------------------------------------------------------------- #
# The turn on a bot with no provider
# --------------------------------------------------------------------------- #


def test_a_turn_on_an_unconnected_bot_says_so_in_the_thread(env, monkeypatch):
    env(real_defaults=True)
    delivered, frames = [], []

    async def deliver(thread_id, text, **kw):
        delivered.append((text, kw.get("metadata") or {}))
        return SimpleNamespace(id="m1")

    async def noop(*a, **kw):
        return None

    statuses = []

    async def set_status(tid, status):
        statuses.append(status)

    async def list_messages(tid, limit):
        return [], False

    async def broadcast(f):
        frames.append(f)

    monkeypatch.setattr(advisor, "_hooks", advisor.Hooks(
        list_messages=list_messages, deliver=deliver, set_status=set_status,
        broadcast=broadcast, thread_update=noop,
        open_stream=lambda t, p: None, close_stream=lambda t, p: True,
        sanitize_delta=lambda s: s, ask_agent=noop, threads_since=noop,
        data_dir=env.tmp_path))
    asyncio.run(advisor.run_turn("t1", "atlas", "hello"))
    (text, meta), = delivered
    assert text == ("Atlas isn't connected to an AI yet. "
                    "Open Settings → AI models to add a key.")
    assert meta["notice"]["level"] == "warn" and meta["not_connected"] is True
    assert not [f for f in frames if f["type"] == "error"]
    # The thread is back to idle, not left "thinking" with Send disabled.
    assert statuses and statuses[-1] == "idle"


def test_digest_and_status_survive_an_unconnected_advisor(env):
    c = env(real_defaults=True)
    assert c.get("/api/advisor/status").status_code == 200
    r = c.post("/api/advisor/digest")
    assert r.status_code == 200
    assert r.json()["results"][0]["skipped"] == "not connected"


# --------------------------------------------------------------------------- #
# First-run seed
# --------------------------------------------------------------------------- #


def test_a_fresh_install_seeds_the_guide_threads_once(env):
    c = env(real_defaults=True)
    ts = _threads(c)
    assert sorted(t["title"] for t in ts) == ["How DisPatch works", "Meet Atlas",
                                              "Set up add-ons"]
    assert ts[0]["title"] == "Meet Atlas"            # newest = top of the list
    for t in ts:
        msgs = c.get(f"/api/threads/{t['id']}/messages").json()
        msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["metadata"]["seeded"] is True
    assert (env.tmp_path / firstrun.FLAG_NAME).exists()
    meet = firstrun.GUIDE_DIR.joinpath("meet-atlas.md").read_text().strip()
    ids = {t["title"]: t["id"] for t in ts}
    body = c.get(f"/api/threads/{ids['Meet Atlas']}/messages").json()
    body = body["messages"] if isinstance(body, dict) else body
    assert body[0]["content"] == meet


def test_a_second_boot_seeds_nothing(env):
    c = env(real_defaults=True)
    assert len(_threads(c)) == 3
    c.__exit__(None, None, None)
    c2 = env(real_defaults=True, fresh_db=False)
    assert len(_threads(c2)) == 3


def test_deleting_every_thread_does_not_bring_the_tour_back(env):
    c = env(real_defaults=True)
    for t in _threads(c):
        assert c.delete(f"/api/threads/{t['id']}").status_code in (200, 204)
    c.__exit__(None, None, None)
    c2 = env(real_defaults=True, fresh_db=False)
    assert _threads(c2) == []


def test_an_install_with_threads_never_seeds(env, monkeypatch):
    # An existing install: boot once with the seed off and make a thread…
    with monkeypatch.context() as m:
        m.setenv("DISPATCH_FIRSTRUN", "0")
        c = env(real_defaults=True)
        tid = c.post("/api/threads", json={"bot_id": "atlas", "title": "Old chat"}).json()["id"]
        assert tid
        c.__exit__(None, None, None)
    assert not (env.tmp_path / firstrun.FLAG_NAME).exists()
    # …then upgrade to a build that seeds: nothing is added.
    c2 = env(real_defaults=True, fresh_db=False)
    assert len(_threads(c2)) == 1
    # …and the flag is written so deleting that thread later is final too.
    assert (env.tmp_path / firstrun.FLAG_NAME).exists()


def test_no_atlas_on_the_roster_means_no_seed(env):
    (env.tmp_path / "config.yaml").write_text("bots:\n  - id: mine\n    name: Mine\n")
    c = env(real_defaults=True)
    assert _threads(c, "mine") == []


def test_the_seed_can_be_switched_off(env, monkeypatch):
    monkeypatch.setenv("DISPATCH_FIRSTRUN", "0")
    c = env(real_defaults=True)
    assert _threads(c) == []


def test_seeded_text_goes_through_the_normal_persist_path(env, monkeypatch):
    """A guide with a picture and a media marker is processed like any reply:
    the Markdown image survives and the file marker is ingested."""
    guide = env.tmp_path / "guide"
    guide.mkdir()
    pic = env.tmp_path / "shot.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (guide / "meet-atlas.md").write_text(
        "Hello ![Settings](/static/guide/settings.png)\n\n"
        f"[[media:{pic}|a shot]]\n")
    (guide / "how-dispatch-works.md").write_text("Tour.")
    monkeypatch.setattr(firstrun, "GUIDE_DIR", guide)
    c = env(real_defaults=True)
    ids = {t["title"]: t["id"] for t in _threads(c)}
    body = c.get(f"/api/threads/{ids['Meet Atlas']}/messages").json()
    body = body["messages"] if isinstance(body, dict) else body
    content = body[0]["content"]
    assert "![Settings](/static/guide/settings.png)" in content
    assert str(pic) not in content and "[[media:/media/" in content


def test_the_guide_directory_is_served_as_static(env):
    c = env(real_defaults=True)
    r = c.get("/static/guide/welcome.png")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# Bot routes
# --------------------------------------------------------------------------- #


def test_create_a_direct_bot_derives_its_id(env):
    c = env(real_defaults=True)
    r = c.post("/api/bots", json={"name": "My Helper", "emoji": "🤖"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["id"] == "my-helper" and row["name"] == "My Helper"
    assert (row["backend"], row["tools"], row["connected"]) == ("direct", True, False)
    assert config.get_bot("my-helper").advisor == {"providers": [], "tools": True}
    # same name again -> a different, unused id
    assert c.post("/api/bots", json={"name": "My Helper"}).json()["id"] == "my-helper-2"
    assert any(b["id"] == "my-helper" for b in c.get("/api/bots/all").json()["bots"])


def test_create_an_agent_bot(env):
    c = env(real_defaults=True)
    r = c.post("/api/bots", json={"id": "worker", "name": "Worker",
                                  "backend": "agent", "agent": "oc-worker",
                                  "model_hint": "agent", "safe": False, "visible": False})
    assert r.status_code == 201, r.text
    row = r.json()
    assert (row["backend"], row["agent"], row["visible"]) == ("agent", "oc-worker", False)
    assert config.get_bot("worker").advisor is None


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "x" * 41}, {"name": "ok", "emoji": "x" * 9},
    {"name": "ok", "model_hint": "x" * 61}, {"name": "ok", "id": "Bad Id"},
    {"name": "ok", "id": "x" * 33}, {"name": "ok", "surprise": 1},
    {"name": "ok", "backend": "carrier-pigeon"},
    {"name": "ok", "backend": "agent", "tools": True},
    {"name": "ok", "backend": "direct", "agent": "a"},
    # the Bots tab (and its hint text) allow letters, digits, - and _ only
    {"name": "ok", "backend": "agent", "agent": "a.b"},
])
def test_create_rejects_bad_bodies(env, body):
    c = env(real_defaults=True)
    assert c.post("/api/bots", json=body).status_code == 422


def test_create_with_a_taken_id_is_409(env):
    c = env(real_defaults=True)
    assert c.post("/api/bots", json={"id": "atlas", "name": "Again"}).status_code == 409


def test_patch_changes_only_what_it_names(env):
    c = env(real_defaults=True)
    r = c.patch("/api/bots/atlas", json={"name": "Globe", "emoji": "🧭",
                                         "model_hint": "mine", "visible": False,
                                         "safe": False, "reactions": True,
                                         "avatar_pool": True, "tools": False})
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["name"], row["emoji"], row["model_hint"], row["visible"],
            row["safe"], row["reactions"], row["avatar_pool"], row["tools"]) == (
        "Globe", "🧭", "mine", False, False, True, True, False)
    b = config.get_bot("atlas")
    assert b.advisor == {"providers": [], "tools": False}
    r = c.patch("/api/bots/atlas", json={"name": "Atlas"})
    assert r.json()["name"] == "Atlas" and r.json()["emoji"] == "🧭"


def test_patch_keeps_a_hand_picked_tool_list_when_lookups_stay_on(env):
    c = env(real_defaults=True)
    config.upsert_bot(config.Bot(id="atlas", name="Atlas", advisor={
        "providers": [], "tools": ["weather", "calculator"], "owner": "Sam"}))
    c.patch("/api/bots/atlas", json={"tools": True})
    assert config.get_bot("atlas").advisor["tools"] == ["weather", "calculator"]
    c.patch("/api/bots/atlas", json={"tools": False})
    adv = config.get_bot("atlas").advisor
    assert adv["tools"] is False and adv["owner"] == "Sam"
    # The list is parked, not lost: on again brings it back, not "every tool".
    assert adv["tools_picked"] == ["weather", "calculator"]
    assert c.patch("/api/bots/atlas", json={"tools": False}).status_code == 200   # idempotent
    assert config.get_bot("atlas").advisor["tools_picked"] == ["weather", "calculator"]
    c.patch("/api/bots/atlas", json={"tools": True})
    adv = config.get_bot("atlas").advisor
    assert adv["tools"] == ["weather", "calculator"] and "tools_picked" not in adv
    # A plain on/off bot just flips.
    config.upsert_bot(config.Bot(id="atlas", name="Atlas", advisor={"providers": [], "tools": True}))
    c.patch("/api/bots/atlas", json={"tools": False})
    assert config.get_bot("atlas").advisor == {"providers": [], "tools": False}
    c.patch("/api/bots/atlas", json={"tools": True})
    assert config.get_bot("atlas").advisor == {"providers": [], "tools": True}


def test_connect_writes_the_system_prompt_as_the_persona_and_never_clears_it(env):
    c = env(real_defaults=True)
    base = {"provider": "openai", "model": "gpt-4o", "api_key": "sk-1"}
    r = c.post("/api/llm/connect", json={**base, "system_prompt": "  You are Globe.  "})
    assert r.status_code == 200, r.text
    assert config.get_bot("atlas").advisor["persona"] == "You are Globe."
    # 2.3.6: the full-access row carries it (the Bots tab's Personality editor
    # and the Connect field show it); the public roster never does.
    assert r.json()["persona"] == "You are Globe."
    assert "You are Globe" not in c.get("/api/bots").text
    # A blank field on a re-save keeps the persona.
    assert c.post("/api/llm/connect", json={**base, "system_prompt": ""}).status_code == 200
    assert config.get_bot("atlas").advisor["persona"] == "You are Globe."
    # A new bot made through connect gets it too; no field = no key.
    c.post("/api/llm/connect", json={**base, "target": "globe", "system_prompt": "Terse."})
    assert config.get_bot("globe").advisor["persona"] == "Terse."
    c.post("/api/llm/connect", json={**base, "target": "plain"})
    assert "persona" not in config.get_bot("plain").advisor


def test_the_probe_reads_only_credential_shaped_or_stored_env_names(env, monkeypatch):
    """The named variable's value goes to whatever base URL the form says, so
    the probe never reads PATH/HOME/... on an operator's say-so: a name must
    end like a credential, or be one a bot already stores for that provider."""
    env(real_defaults=True)
    for name, val in (("PATH_LIKE", "p"), ("MY_API_KEY", "sk-env"), ("DISPATCH_API_KEY", "no"),
                      ("HOUSE_AUTH", "sk-stored"), ("A", "short")):
        monkeypatch.setenv(name, val)
    probe = main._stored_probe_key
    assert probe({"provider": "openai", "api_key_env": "MY_API_KEY"}) == "sk-env"
    assert probe({"provider": "openai", "api_key_env": "PATH_LIKE"}) == ""
    assert probe({"provider": "openai", "api_key_env": "DISPATCH_API_KEY"}) == ""
    assert probe({"provider": "openai", "api_key_env": "A"}) == ""
    assert probe({"provider": "openai", "api_key_env": "HOUSE_AUTH"}) == ""
    # ...unless the targeted bot already stores that name for this provider.
    config.upsert_bot(config.Bot(id="atlas", name="Atlas", advisor={
        "providers": [{"provider": "openai", "model": "m", "api_key_env": "HOUSE_AUTH"}]}))
    assert probe({"provider": "openai", "api_key_env": "HOUSE_AUTH", "target": "atlas"}) == "sk-stored"
    assert probe({"provider": "deepseek", "api_key_env": "HOUSE_AUTH", "target": "atlas"}) == ""
    # No env name: the stored entry's own key resolution.
    assert probe({"provider": "openai", "target": "atlas"}) == "sk-stored"


def test_patch_errors(env):
    c = env(real_defaults=True)
    c.post("/api/bots", json={"id": "worker", "name": "W", "backend": "agent"})
    assert c.patch("/api/bots/nope", json={"name": "x"}).status_code == 404
    assert c.patch("/api/bots/atlas", json={"color": "red"}).status_code == 422
    assert c.patch("/api/bots/atlas", json={}).status_code == 422
    assert c.patch("/api/bots/atlas", json={"name": None}).status_code == 422
    assert c.patch("/api/bots/atlas", json={"name": "   "}).status_code == 422
    assert c.patch("/api/bots/worker", json={"tools": True}).status_code == 422


def test_delete_removes_the_bot_but_keeps_its_threads(env):
    c = env(real_defaults=True)
    c.post("/api/bots", json={"name": "Temp"})
    tid = c.post("/api/threads", json={"bot_id": "temp"}).json()["id"]
    r = c.delete("/api/bots/temp")
    assert r.status_code == 200 and r.json() == {"removed": "temp"}
    assert config.get_bot("temp") is None
    assert c.get(f"/api/threads/{tid}/messages").status_code == 200   # still there
    assert c.delete("/api/bots/temp").status_code == 404


def test_delete_refuses_an_app_bot_and_the_last_bot(env, monkeypatch):
    c = env(real_defaults=True)
    c.post("/api/bots", json={"id": "appbot", "name": "App"})
    with monkeypatch.context() as m:
        m.setattr(main.apps_loader, "owns_bot", lambda i: i == "appbot")
        assert c.delete("/api/bots/appbot").status_code == 409
    assert config.get_bot("appbot") is not None
    only = [b.id for b in config.load_bots() if b.id != "appbot"]
    for bid in only:
        if bid != "jobboard":          # the Job Board app owns its bot
            assert c.delete(f"/api/bots/{bid}").status_code == 200
    with pytest.raises(ValueError):    # config refuses to empty the roster
        config._write_bots([config._bot_entry(config.get_bot("appbot"))])
        config.remove_bot("appbot")
    assert config.get_bot("appbot") is not None


def test_bots_all_rows_carry_backend_tools_connected_and_no_secret(env):
    c = env(real_defaults=True)
    c.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o-mini",
                                     "api_key": "sk-SECRET-1"})
    c.post("/api/bots", json={"id": "w", "name": "W", "backend": "agent"})
    c.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "lmstudio",
                                     "model": "m"})
    rows = {b["id"]: b for b in c.get("/api/bots/all").json()["bots"]}
    assert rows["atlas"]["backend"] == "direct" and rows["atlas"]["connected"] is True
    assert rows["w"]["backend"] == "agent" and rows["w"]["connected"] is False
    assert rows["llm-lmstudio"]["backend"] == "legacy-api"
    assert "sk-SECRET-1" not in c.get("/api/bots/all").text


# --------------------------------------------------------------------------- #
# The gate: operator only, fail closed
# --------------------------------------------------------------------------- #

WRITE_ROUTES = [
    ("POST", "/api/bots", {"name": "x"}),
    ("PATCH", "/api/bots/atlas", {"name": "x"}),
    ("DELETE", "/api/bots/atlas", None),
]


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_a_locked_device_cannot_manage_bots(env, method, path, body):
    c = env(real_defaults=True)
    c.post("/api/auth/setup", json={"new_pin": "1234"})
    c.cookies.clear()                       # no session -> Safe Mode
    r = c.request(method, path, json=body)
    assert r.status_code == 403
    assert "unlock" in r.text.lower()
    assert config.get_bot("atlas").name == "Atlas"
    assert config.get_bot("x") is None


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_an_unlocked_session_can(env, method, path, body):
    c = env(real_defaults=True)
    c.post("/api/auth/setup", json={"new_pin": "1234"})
    c.post("/api/bots", json={"id": "spare", "name": "Spare"})   # so delete is not the last
    r = c.request(method, path, json=body)
    assert r.status_code in (200, 201), r.text


# --------------------------------------------------------------------------- #
# Connect an AI -> a direct-API bot
# --------------------------------------------------------------------------- #


def test_connect_writes_the_provider_into_atlas(env):
    c = env(real_defaults=True)
    r = c.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o",
                                         "api_key": "sk-SECRET-2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "sk-SECRET-2" not in r.text
    assert body["id"] == "atlas" and body["connected"] is True
    assert body["bot"]["id"] == "atlas"
    assert (body["provider"], body["model"], body["backend"]) == ("openai", "gpt-4o", "direct")
    adv = config.get_bot("atlas").advisor
    assert adv["tools"] is True
    assert adv["providers"] == [{"provider": "openai", "base_url": "https://api.openai.com/v1",
                                 "model": "gpt-4o", "api_key": "sk-SECRET-2"}]
    assert oct((env.tmp_path / "config.yaml").stat().st_mode & 0o777) == "0o600"


def test_connect_carries_the_minimax_thinking_switch(env):
    c = env(real_defaults=True)
    c.post("/api/llm/connect", json={"provider": "minimax", "api_key_env": "MM_KEY_X",
                                     "tools": False, "max_tokens": 2048})
    # no model sent -> the preset's; env var not set but a key is not required
    # to exist at save time only when the env var is present
    c2 = c.post("/api/llm/connect", json={"provider": "minimax", "api_key": "k"})
    assert c2.status_code == 200, c2.text
    (e,) = config.get_bot("atlas").advisor["providers"]
    assert e["model"] == "MiniMax-M3"
    assert e["extra_body"] == {"thinking": {"type": "disabled"}, "reasoning_split": True}


def test_connect_anthropic_makes_an_entry_the_advisor_accepts(env):
    c = env(real_defaults=True)
    r = c.post("/api/llm/connect", json={"provider": "anthropic", "api_key": "k",
                                         "tools": False})
    assert r.status_code == 200, r.text
    bot = config.get_bot("atlas")
    cfg = advisor.AdvisorConfig.from_bot(bot)
    res = llm_api.resolve_api(cfg.providers[0], bot.name, bot.id)
    assert res.provider.kind == "anthropic" and res.model == "claude-opus-5"
    assert bot.advisor["tools"] is False


def test_connect_a_new_bot_by_target(env):
    c = env(real_defaults=True)
    r = c.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m",
                                         "target": "local", "name": "Local"})
    assert r.status_code == 200, r.text
    b = config.get_bot("local")
    assert (b.name, b.safe, b.reactions, b.model_hint) == ("Local", False, False, "m")
    assert b.advisor["providers"][0]["provider"] == "lmstudio"
    assert c.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m",
                                            "target": "Bad Id!"}).status_code == 400


def test_reconnect_keeps_the_fallback_chain_behind_the_first_provider(env):
    c = env(real_defaults=True)
    config.upsert_bot(config.Bot(id="atlas", name="Atlas", advisor={"tools": True, "providers": [
        {"provider": "openai", "model": "gpt-4o", "api_key": "KEEP", "base_url": "https://api.openai.com/v1"},
        {"provider": "custom", "model": "local-27b", "base_url": "http://127.0.0.1:1234/v1", "read_timeout_s": 180},
    ]}))
    r = c.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o-mini"})
    assert r.status_code == 200, r.text
    provs = config.get_bot("atlas").advisor["providers"]
    assert [p["model"] for p in provs] == ["gpt-4o-mini", "local-27b"]
    assert provs[1]["read_timeout_s"] == 180 and provs[0]["api_key"] == "KEEP"


def test_reconnect_keeps_the_key_and_every_other_field(env):
    c = env(real_defaults=True)
    config.upsert_bot(config.Bot(
        id="atlas", name="Atlas", avatar="a.png", color="#123456", safe=True,
        reactions=True, avatar_pool=True, image_jobs=True, image_workflow="wf",
        voice="v", voice_rp="r", reaction_autopilot=True,
        advisor={"owner": "Sam", "knowledge_dir": "/k", "agents": ["w"], "tools": True,
                 "providers": [{"provider": "openai", "model": "gpt-4o", "api_key": "KEEP",
                                "base_url": "https://api.openai.com/v1",
                                "max_tokens": 777}]}))
    r = c.post("/api/llm/connect", json={"provider": "openai", "model": "gpt-4o-mini"})
    assert r.status_code == 200, r.text
    b = config.get_bot("atlas")
    assert b.advisor["providers"][0]["api_key"] == "KEEP"
    assert b.advisor["providers"][0]["max_tokens"] == 777
    assert b.advisor["providers"][0]["model"] == "gpt-4o-mini"
    assert (b.advisor["owner"], b.advisor["knowledge_dir"], b.advisor["agents"]) == (
        "Sam", "/k", ["w"])
    assert (b.avatar, b.color, b.safe, b.reactions, b.avatar_pool, b.image_jobs,
            b.image_workflow, b.voice, b.voice_rp, b.reaction_autopilot) == (
        "a.png", "#123456", True, True, True, True, "wf", "v", "r", True)
    # Switching provider must not carry the old key across.
    assert c.post("/api/llm/connect", json={"provider": "deepseek"}).status_code == 400


def test_connect_needs_a_key_and_refuses_agent_and_legacy_bots(env):
    c = env(real_defaults=True)
    assert c.post("/api/llm/connect", json={"provider": "openai"}).status_code == 400
    c.post("/api/bots", json={"id": "w", "name": "W", "backend": "agent"})
    r = c.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m",
                                         "target": "w"})
    assert r.status_code == 400 and "not a direct-API" in r.text
    assert c.post("/api/llm/connect", json={"backend": "weird", "provider": "lmstudio",
                                            "model": "m"}).status_code == 422
    assert c.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m",
                                            "tools": "yes"}).status_code == 422


def test_legacy_connect_no_longer_drops_the_other_fields(env):
    c = env(real_defaults=True)
    c.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "lmstudio",
                                     "model": "m"})
    b = config.get_bot("llm-lmstudio")
    config.upsert_bot(__import__("dataclasses").replace(
        b, avatar_pool=True, image_jobs=True, voice="v", reaction_autopilot=True,
        image_ratio="2:3"))
    c.post("/api/llm/connect", json={"backend": "legacy-api", "provider": "lmstudio",
                                     "model": "m2"})
    b = config.get_bot("llm-lmstudio")
    assert (b.avatar_pool, b.image_jobs, b.voice, b.reaction_autopilot,
            b.image_ratio, b.api["model"]) == (True, True, "v", True, "2:3", "m2")


def test_probe_takes_the_connect_payload_and_reuses_the_stored_key(env, monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    def factory(read_timeout: float = llm_api.READ_TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 timeout=read_timeout, follow_redirects=False)
    monkeypatch.setattr(llm_api, "http_client", factory)
    c = env(real_defaults=True)
    payload = {"provider": "openai", "model": "gpt-4o", "api_key": "sk-typed",
               "target": "atlas", "backend": "direct", "tools": True}
    r = c.post("/api/llm/test", json=payload)
    assert r.json() == {"ok": True, "models": ["gpt-4o"]}
    assert seen["auth"] == "Bearer sk-typed"
    c.post("/api/llm/connect", json=payload)
    # Re-test from the panel: the form has no key, the bot does.
    r = c.post("/api/llm/test", json={**payload, "api_key": ""})
    assert r.json()["ok"] is True and seen["auth"] == "Bearer sk-typed"
    # A different provider never borrows it.
    r = c.post("/api/llm/test", json={**payload, "api_key": "", "provider": "deepseek"})
    assert r.json()["ok"] is False


def test_a_connected_atlas_streams_through_the_chain(env, monkeypatch):
    """End to end through the advisor: once connected, a turn goes to the
    configured provider (no 'not connected' notice)."""
    c = env(real_defaults=True)
    c.post("/api/llm/connect", json={"provider": "openai", "api_key": "k", "tools": False})
    delivered = []

    async def fake_stream(res, entry, messages, on_text):
        await on_text("Hi there")
        return "Hi there"

    async def deliver(thread_id, text, **kw):
        delivered.append((text, kw.get("metadata") or {}))
        return SimpleNamespace(id="m")

    async def noop(*a, **kw):
        return None

    async def list_messages(tid, limit):
        return [], False

    monkeypatch.setattr(advisor, "_stream_openai", fake_stream)
    monkeypatch.setattr(advisor, "_hooks", advisor.Hooks(
        list_messages=list_messages, deliver=deliver, set_status=noop, broadcast=noop,
        thread_update=noop, open_stream=lambda t, p: None, close_stream=lambda t, p: True,
        sanitize_delta=lambda s: s, ask_agent=noop, threads_since=noop,
        data_dir=env.tmp_path))
    asyncio.run(advisor.run_turn("t1", "atlas", "hello"))
    assert delivered and delivered[0][0] == "Hi there"


def test_a_connected_direct_bot_counts_for_the_first_run_card(env):
    c = env(real_defaults=True)
    assert c.get("/api/auth/status").json()["features"]["api_bots"] == 0
    c.post("/api/llm/connect", json={"provider": "lmstudio", "model": "m"})
    assert c.get("/api/auth/status").json()["features"]["api_bots"] == 1
    rows = {b["id"]: b for b in c.get("/api/bots/all").json()["bots"]}
    assert rows["atlas"]["connected"] is True
    c.post("/api/bots", json={"id": "w", "name": "W", "backend": "agent"})
    assert {b["id"]: b for b in c.get("/api/bots/all").json()["bots"]}["w"]["connected"] is False
    assert config.api_bot_count() == 1


# --------------------------------------------------------------------------- #
# Shipped default avatar
# --------------------------------------------------------------------------- #


@pytest.fixture
def avatar_dir(env, monkeypatch):
    d = env.tmp_path / "avatars"
    monkeypatch.setattr(config, "AVATAR_DIR", d)
    return d


def test_a_fresh_install_gets_the_atlas_avatar_set(env, avatar_dir):
    c = env(real_defaults=True)
    for name in firstrun.DEFAULT_AVATARS:
        assert (avatar_dir / name).read_bytes() == (firstrun.AVATAR_SRC_DIR / name).read_bytes()
    assert not list(avatar_dir.glob(".*partial"))
    row = next(b for b in c.get("/api/bots").json()["bots"] if b["id"] == "atlas")
    assert "atlas-face.png" in row["avatar_url"]
    full = c.get("/api/bots/atlas/avatar/full")
    thumb = c.get("/api/bots/atlas/avatar/thumb")
    assert full.status_code == 200 and thumb.status_code == 200
    assert full.content == (avatar_dir / "atlas-full.png").read_bytes()
    assert thumb.content == (avatar_dir / "atlas-thumb.png").read_bytes()


def test_the_shipped_avatar_files_are_the_documented_sizes():
    from PIL import Image
    sizes = {n: Image.open(firstrun.AVATAR_SRC_DIR / n).size for n in firstrun.DEFAULT_AVATARS}
    assert sizes == {"atlas-face.png": (512, 512), "atlas-full.png": (1096, 1536),
                     "atlas-thumb.png": (128, 128)}


def test_an_existing_atlas_avatar_is_never_overwritten(env, avatar_dir):
    avatar_dir.mkdir(exist_ok=True)
    (avatar_dir / "atlas-face.png").write_bytes(b"mine")
    env(real_defaults=True)
    assert (avatar_dir / "atlas-face.png").read_bytes() == b"mine"
    # the set stays whole: no shipped full/thumb beside somebody else's face
    assert not (avatar_dir / "atlas-full.png").exists()
    assert not (avatar_dir / "atlas-thumb.png").exists()


def test_an_existing_full_or_thumb_is_kept_when_the_face_is_missing(avatar_dir):
    avatar_dir.mkdir(exist_ok=True)
    (avatar_dir / "atlas-full.png").write_bytes(b"mine")
    assert firstrun.install_default_avatars() == ["atlas-face.png", "atlas-thumb.png"]
    assert (avatar_dir / "atlas-full.png").read_bytes() == b"mine"


def test_a_deleted_avatar_comes_back_even_after_the_first_run(env, avatar_dir):
    env(real_defaults=True)
    for p in avatar_dir.iterdir():
        p.unlink()
    assert (env.tmp_path / firstrun.FLAG_NAME).exists()
    assert firstrun.install_default_avatars() == list(firstrun.DEFAULT_AVATARS)


def test_a_missing_source_file_fails_safe(env, avatar_dir, monkeypatch, tmp_path, caplog):
    src = tmp_path / "empty-src"
    src.mkdir()
    monkeypatch.setattr(firstrun, "AVATAR_SRC_DIR", src)
    with caplog.at_level("WARNING", logger="local-chat.firstrun"):
        c = env(real_defaults=True)                 # boots anyway
    assert c.get("/api/bots").status_code == 200
    assert "could not install default avatar" in caplog.text
    assert not list(avatar_dir.iterdir()) # no half-written leftovers
