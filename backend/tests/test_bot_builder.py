"""AI build (WP-AIB): the meta-prompt, JSON validation with one repair retry, the route.

A fake provider only: ``llm_api.complete`` is replaced, so nothing here opens a socket."""
from __future__ import annotations

import json
import logging
import re

import pytest

from app import accounts, bot_builder, bot_builder_api, config, llm_api
from app.config import Bot

GOOD = {"name": "Quill", "emoji": "🪶", "description": "Edits your writing.",
        "persona": "You are Quill, an editor who tightens prose for {user}. Be exact and brief."}


def good(**over):
    return json.dumps({**GOOD, **over})


class Fake:
    """Stands in for ``llm_api.complete``; replies come from a queue."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, cfg, history):
        self.calls.append((cfg.system_prompt, history[-1]["content"]))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return llm_api.Reply(text=reply, model=cfg.model, provider=cfg.provider.id)


@pytest.fixture
def fake(monkeypatch):
    f = Fake(good())
    monkeypatch.setattr(llm_api, "complete", f)
    return f


@pytest.fixture
def household_atlas(monkeypatch):
    atlas = Bot(id="atlas", name="Atlas", advisor={"providers": [
        {"provider": "lmstudio", "base_url": "http://127.0.0.1:9/v1", "model": "fake-model"}]})
    monkeypatch.setattr(config, "load_bots", lambda: [atlas])
    return atlas


# --------------------------------------------------------------------------- #
# Pure logic: template, parsing, validation, repair
# --------------------------------------------------------------------------- #

def test_meta_prompt_golden():
    prompt = bot_builder.first_prompt("A pirate who explains tides.")
    assert prompt.rstrip().endswith("A pirate who explains tides.")
    for key in ('"name"', '"emoji"', '"description"', '"persona"'):
        assert key in prompt
    assert "{{" not in prompt and "---REPAIR---" not in prompt
    assert "at most 3000 characters" in prompt           # stays under the editor's 4000 limit
    assert bot_builder.PERSONA_MAX == 4000


def test_meta_prompt_has_no_restriction_or_agent_language():
    text = bot_builder.TEMPLATE_PATH.read_text(encoding="utf-8").lower()
    banned = ["safe", "restrict", "refus", "forbid", "inappropriate", "appropriate", "family",
              "kid", "child", "minor", "moderat", "never ", "must not", "worker", "agent",
              "command", "shell", "execute", "tool"]
    assert [w for w in banned if w in text] == []


def test_repair_prompt_names_the_problem_and_keeps_the_description():
    p = bot_builder.repair_prompt("A pirate.", 'the "name" key is missing')
    assert 'the "name" key is missing' in p and p.rstrip().endswith("A pirate.")
    assert "{{" not in p


@pytest.mark.parametrize("raw", [
    good(),
    "```json\n" + good() + "\n```",
    "Here you go:\n" + good() + "\nEnjoy!",
    "<think>hmm</think>" + good(),
])
def test_validate_accepts_common_wrappers(raw):
    d = bot_builder.validate(raw)
    assert (d.name, d.emoji) == ("Quill", "🪶") and d.persona.startswith("You are Quill")


@pytest.mark.parametrize("raw,needle", [
    ("not json at all", "JSON"),
    ("[1, 2]", "JSON"),
    (json.dumps({"emoji": "x", "persona": "p" * 50}), '"name"'),
    (good(name="   "), '"name" is empty'),
    (good(name="N" * 41), "limit is 40"),
    (good(persona=""), '"persona" is empty'),
    (good(persona="short"), "too short"),
    (good(persona="p" * 4001), "limit is 4000"),
    (good(emoji=5), "must be a string"),
])
def test_validate_rejects(raw, needle):
    with pytest.raises(bot_builder.DraftInvalid) as e:
        bot_builder.validate(raw)
    assert needle in str(e.value)


def test_validate_drops_unknown_keys_and_defaults_optionals():
    d = bot_builder.validate(json.dumps({"name": "A", "persona": "You are A, a calm guide." * 3, "x": 1}))
    assert d.to_dict().keys() == {"name", "emoji", "description", "persona"} and d.emoji == ""


@pytest.mark.anyio
async def test_one_repair_retry_then_success():
    seen = []

    async def complete(system, user):
        seen.append(user)
        return "oops" if len(seen) == 1 else good()

    d = await bot_builder.build_draft("An editor.", complete)
    assert d.name == "Quill" and len(seen) == 2
    assert "could not be used" in seen[1] and "An editor." in seen[1]


@pytest.mark.anyio
async def test_two_bad_replies_fail_without_a_third_call():
    calls = []

    async def complete(system, user):
        calls.append(user)
        return "SECRET-REPLY nope"

    with pytest.raises(bot_builder.DraftFailed) as e:
        await bot_builder.build_draft("An editor.", complete)
    assert len(calls) == 2 and "SECRET-REPLY" not in str(e.value)


@pytest.mark.anyio
async def test_provider_errors_propagate():
    async def complete(system, user):
        raise llm_api.ApiError("down")

    with pytest.raises(llm_api.ApiError):
        await bot_builder.build_draft("x", complete)


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

@pytest.fixture
def people(world):
    owner = world.create_owner()
    world.person(owner, "Una", "user", pin="1357")
    world.person(owner, "Bea", "basic")
    out = {"admin": owner}
    for key, name, pin in (("user", "Una", "1357"), ("basic", "Bea", None)):
        c = world.client()
        r = world.login(c, name=name, pin=pin)
        assert r.status_code == 200, r.text
        out[key] = c
    return out


URL = "/api/me/characters/draft"


def test_route_end_to_end_with_a_fake_provider(people, fake, household_atlas):
    r = people["user"].post(URL, json={"description": "An editor who tightens my writing."})
    assert r.status_code == 200, r.text
    assert r.json() == {"draft": GOOD}
    system, user = fake.calls[0]
    assert system == bot_builder.SYSTEM_PROMPT and "An editor who tightens my writing." in user
    fake.replies.append(good())
    assert people["admin"].post(URL, json={"description": "x"}).status_code == 200


def test_route_repairs_once(people, household_atlas, monkeypatch):
    f = Fake("garbage", good())
    monkeypatch.setattr(llm_api, "complete", f)
    r = people["user"].post(URL, json={"description": "An editor."})
    assert r.status_code == 200 and len(f.calls) == 2


def test_route_reports_an_unusable_draft(people, household_atlas, monkeypatch):
    f = Fake("garbage", "still garbage")
    monkeypatch.setattr(llm_api, "complete", f)
    r = people["user"].post(URL, json={"description": "An editor."})
    assert r.status_code == 502 and r.json()["error"]["code"] == "draft_failed"
    assert "garbage" not in r.text


def test_route_provider_failure_is_a_502(people, household_atlas, monkeypatch):
    monkeypatch.setattr(llm_api, "complete", Fake(llm_api.ApiError("Rate limited")))
    r = people["user"].post(URL, json={"description": "An editor."})
    assert r.status_code == 502 and "Rate limited" in r.json()["detail"]


def test_route_without_a_connection_is_503(people, monkeypatch):
    monkeypatch.setattr(config, "load_bots", lambda: [Bot(id="plain", name="Plain")])
    r = people["user"].post(URL, json={"description": "An editor."})
    assert r.status_code == 503 and r.json()["error"]["code"] == "draft_unavailable"


def test_permission_basic_is_refused_and_no_provider_call(people, fake, household_atlas):
    r = people["basic"].post(URL, json={"description": "An editor."})
    assert r.status_code == 403 and fake.calls == []


def test_permission_unsigned_is_refused(world, fake, household_atlas):
    world.create_owner()
    r = world.client().post(URL, json={"description": "An editor."})
    assert r.status_code in (401, 403) and fake.calls == []


def test_admin_can_switch_the_permission_off_per_profile(world, fake, household_atlas):
    owner = world.create_owner()
    person = world.person(owner, "Una", "user", pin="1357")
    r = owner.patch(f"/api/people/{person['id']}", json={"denies": ["create_characters"]})
    assert r.status_code == 200, r.text
    c = world.client()
    assert world.login(c, name="Una", pin="1357").status_code == 200
    assert c.post(URL, json={"description": "x"}).status_code == 403


@pytest.mark.parametrize("body", [{}, {"description": ""}, {"description": "   "},
                                  {"description": "x" * 1001}, {"description": "x", "extra": 1}])
def test_route_validates_input(people, fake, household_atlas, body):
    assert people["user"].post(URL, json=body).status_code == 422 and fake.calls == []


def test_route_is_declared_with_the_right_permission():
    from app import route_policy
    reqs = [req for ms, path, req in route_policy.ROUTES if path == URL and "POST" in ms]
    assert len(reqs) == 1 and str(getattr(reqs[0].permission, "value", reqs[0].permission)) == "create_characters"


def test_provider_chain_honours_the_allowed_connections(monkeypatch):
    a = Bot(id="atlas", name="Atlas", advisor={"providers": [
        {"provider": "lmstudio", "model": "m1", "connection": "c1"},
        {"provider": "lmstudio", "model": "m2", "connection": "c2"}]})
    other = Bot(id="aaa", name="Aaa", advisor={"providers": [{"provider": "lmstudio", "model": "z"}]})
    mine = Bot(id="p1", name="Mine", scope="personal", owner_profile="u",
               advisor={"providers": [{"provider": "lmstudio", "model": "private"}]})
    monkeypatch.setattr(config, "load_bots", lambda: [other, mine, a])
    pr = type("P", (), {"profile": object()})()
    monkeypatch.setattr(bot_builder_api.backends_policy, "allowed_backends", lambda p: None)
    assert [e["model"] for e in bot_builder_api.provider_chain(pr)] == ["m1", "m2"]   # atlas first
    monkeypatch.setattr(bot_builder_api.backends_policy, "allowed_backends", lambda p: ["c2"])
    assert [e["model"] for e in bot_builder_api.provider_chain(pr)] == ["m2"]


# --------------------------------------------------------------------------- #
# The draft is never logged
# --------------------------------------------------------------------------- #

def test_no_log_sentinel(people, household_atlas, monkeypatch, caplog, capfd):
    sentinel_in, sentinel_out = "SENTINEL-DESC-7f3a", "SENTINEL-DRAFT-91bc"
    monkeypatch.setattr(llm_api, "complete", Fake(
        good(persona=f"You are Quill, {sentinel_out} for {{user}}. Be exact and brief."), "bad", "bad"))
    caplog.set_level(logging.DEBUG)
    r = people["user"].post(URL, json={"description": f"An editor {sentinel_in}."})
    assert r.status_code == 200 and sentinel_out in r.text
    # a failing draft too
    monkeypatch.setattr(llm_api, "complete", Fake(f"bad {sentinel_out}", f"worse {sentinel_out}"))
    r = people["user"].post(URL, json={"description": f"An editor {sentinel_in}."})
    assert r.status_code == 502
    out, err = capfd.readouterr()
    haystack = caplog.text + out + err
    assert sentinel_in not in haystack and sentinel_out not in haystack
    rows = accounts.recent_activity(500) if hasattr(accounts, "recent_activity") else []
    assert sentinel_in not in json.dumps(rows, default=str)
    assert re.search(r"SENTINEL", json.dumps(rows, default=str)) is None
