"""The image server in Image Studio: the Owner's allowlist, the gallery, the render.

The image server is not an API provider and it is not a role switch: only the
Owner may use it, and after that only the people the Owner ticked by name.

Run: cd backend && uv run pytest tests/test_image_server_access.py
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app import (accounts, image_access, image_jobs, policy, settings_api,
                 settings_schema, settings_store)
from privacy_fixtures import _png

BASE = "/api/apps/imagestudio"
KEY = image_access.SERVER_PROFILES_KEY
SERVER_URL = "http://image-server.test/mcp"
LAN = ("203.0.113.90", 43100)


def _studio():
    mod = sys.modules.get("dispatch_app_imagestudio")
    assert mod is not None, "the Image Studio app is not mounted"
    return mod


@pytest.fixture
def studio(world, monkeypatch):
    """The Image Studio module, with the image server's address in place."""
    mod = _studio()
    monkeypatch.setattr(mod, "SETTINGS", SimpleNamespace(image_server_url=SERVER_URL))
    return mod


@pytest.fixture
def people(world, studio):
    owner = world.create_owner(world.client())
    ids = {"owner": accounts.get_protected_admin().id}
    for name, role, kw in (("Ada", "admin", {"password": "pw-pass-1234"}),  # scrub-ok: throwaway test password
                           ("Ula", "user", {"pin": "1357"}),
                           ("Bo", "basic", {"pin": "2468"})):
        ids[name] = world.person(owner, name, role, **kw)["id"]
    return {"world": world, "owner": owner, "ids": ids}


def _principal(name):
    return policy.profile_principal(accounts.get_by_name(name))


def _login(world, name, **kw):
    c = world.client(LAN)
    assert world.login(c, name=name, **kw).status_code == 200
    return c


# --------------------------------------------------------------- the allowlist


def test_the_owner_may_always_use_the_image_server(people):
    owner = policy.profile_principal(accounts.get_protected_admin())
    assert image_access.allows_server(owner) is True


def test_nobody_else_may_until_the_owner_names_them(people):
    for name in ("Ada", "Ula", "Bo"):
        assert image_access.allows_server(_principal(name)) is False
    settings_store.set_many({KEY: [people["ids"]["Ada"]]})
    assert image_access.allows_server(_principal("Ada")) is True
    assert image_access.allows_server(_principal("Ula")) is False
    settings_store.set_many({KEY: [people["ids"]["Ula"]]})
    assert image_access.allows_server(_principal("Ula")) is True
    assert image_access.allows_server(_principal("Ada")) is False


def test_a_basic_profile_cannot_be_granted(people):
    settings_store.set_many({KEY: [people["ids"]["Bo"]]})
    assert image_access.allows_server(_principal("Bo")) is False
    assert image_access.server_profiles() == []


def test_the_shared_sign_in_profile_cannot_be_granted(people, monkeypatch):
    uid = people["ids"]["Ula"]
    monkeypatch.setattr(policy, "is_lock_target",
                        lambda prof, cfg=None: bool(prof) and prof.id == uid)
    settings_store.set_many({KEY: [uid]})
    assert image_access.allows_server(_principal("Ula")) is False


def test_a_machine_and_a_signed_out_caller_are_refused(people):
    assert image_access.allows_server(policy.machine_principal("api_token")) is False
    assert image_access.allows_server(policy.nobody()) is False
    assert image_access.allows_server(None) is False


def test_the_list_is_sanitised_on_the_way_in_and_on_the_way_out(people):
    ids = people["ids"]
    r = people["owner"].patch("/api/settings", json={"set": {KEY: [ids["Bo"], ids["Ada"],
                                                                  ids["Ada"], "nope"]}})
    assert r.status_code == 200, r.text
    assert settings_store.get(KEY) == [ids["Ada"]]
    assert image_access.sanitize_server_profiles([ids["Bo"], 7, None, ids["Ada"]]) == [ids["Ada"]]
    assert len(image_access.sanitize_server_profiles([f"x{i}" for i in range(200)])) <= 64


def test_a_second_admin_cannot_change_the_list(people):
    c = _login(people["world"], "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    r = c.patch("/api/settings", json={"set": {KEY: [people["ids"]["Ada"]]}})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "host_owner_only"


def test_the_schema_row_and_the_host_tier(people):
    d = settings_schema.get_def(KEY)
    assert d is not None and d.type == "list" and d.default == []
    assert d.owner_only is True
    assert d.ui_section == "connections#images"
    assert KEY in settings_api.HOST_SETTING_KEYS
    assert KEY in settings_api.IMAGE_OWNER_KEYS


def test_the_generator_matrix_is_untouched():
    assert image_access.GENERATORS == ("standard", "civitai")
    assert "workflows" not in image_access.GENERATORS


def test_only_the_owner_may_read_the_people_who_may_be_named(people):
    world = people["world"]
    c = _login(world, "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    r = c.get(f"{BASE}/server-access")
    assert r.status_code == 403, r.text
    r = people["owner"].get(f"{BASE}/server-access")
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()["profiles"]}
    assert "Ada" in names and "Ula" in names
    assert "Bo" not in names, "a Basic profile is never offered a box"


# ------------------------------------------------------------------- the gallery


def test_the_gallery_is_refused_for_a_profile_not_on_the_list(people, studio, monkeypatch):
    calls = []

    async def no_network(path):
        calls.append(path)
        raise AssertionError("the network must not be touched")

    monkeypatch.setattr(studio, "_gallery_get", no_network)
    monkeypatch.setattr(studio, "_gallery_bytes", no_network)
    c = _login(people["world"], "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    assert c.get(f"{BASE}/workflows").status_code == 403
    assert c.get(f"{BASE}/workflows/thumb",
                 params={"u": "/ui/api/image?f=x.png"}).status_code == 403
    assert calls == []


def test_the_thumb_proxy_refuses_a_path_that_is_not_a_picture(people, studio, monkeypatch):
    async def no_network(path):
        raise AssertionError("the network must not be touched")

    monkeypatch.setattr(studio, "_gallery_bytes", no_network)
    for bad in ("https://example.com/x.png", "/ui/api/image/../../etc/passwd",
                "/etc/passwd", "//example.com/x.png", "ui/api/image?f=x.png",
                "/ui/api/image?f=%2e%2e/%2e%2e/etc/passwd",
                "/files/%2e%2e/%2e%2e/etc/passwd",
                "/ui/api/imageevil"):
        r = people["owner"].get(f"{BASE}/workflows/thumb", params={"u": bad})
        assert r.status_code == 422, (bad, r.text)


def test_the_thumb_proxy_checks_the_access_before_the_network(people, studio, monkeypatch):
    calls = []

    async def fake(path):
        calls.append(path)
        return _png(), "image/png"

    monkeypatch.setattr(studio, "_gallery_bytes", fake)
    c = _login(people["world"], "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    r = c.get(f"{BASE}/workflows/thumb", params={"u": "/ui/api/image?f=x.png"})
    assert r.status_code == 403, r.text
    assert calls == []


def test_the_thumb_proxy_passes_a_picture_through(people, studio, monkeypatch):
    calls = []

    async def fake(path):
        calls.append(path)
        return _png(), "image/png"

    monkeypatch.setattr(studio, "_gallery_bytes", fake)
    r = people["owner"].get(f"{BASE}/workflows/thumb",
                             params={"u": "/ui/api/image?f=_previews/x.png"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert calls == ["/ui/api/image?f=_previews/x.png"]


def test_the_gallery_merges_the_two_lists_by_name(people, studio, monkeypatch):
    async def fake_get(path):
        if path.endswith("/meta"):
            return {"workflows": [{"name": "wf-a", "display_name": "Workflow A"},
                                  {"name": "wf-b", "display_name": "Workflow B"}]}
        return {"previews": [{"workflow": "wf-a", "display_name": "Workflow A",
                               "state": "ready", "stale": True,
                               "thumb": "/ui/api/image?f=_previews/wf-a.png"}]}

    monkeypatch.setattr(studio, "_gallery_get", fake_get)
    r = people["owner"].get(f"{BASE}/workflows")
    assert r.status_code == 200, r.text
    rows = {w["name"]: w for w in r.json()["workflows"]}
    assert set(rows) == {"wf-a", "wf-b"}
    assert rows["wf-a"]["display_name"] == "Workflow A"
    assert rows["wf-a"]["state"] == "ready"
    assert rows["wf-a"]["stale"] is True
    assert rows["wf-a"]["thumb"] == "/ui/api/image?f=_previews/wf-a.png"
    assert rows["wf-b"]["thumb"] == ""


def test_the_owner_sees_the_image_server_provider_and_nobody_else_does(people, studio):
    r = people["owner"].get(f"{BASE}/state")
    assert r.status_code == 200, r.text
    assert "workflows" in [p["id"] for p in r.json()["providers"]]
    c = _login(people["world"], "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    r = c.get(f"{BASE}/state")
    assert "workflows" not in [p["id"] for p in r.json()["providers"]]


# --------------------------------------------------------------------- the render


class FakeServer:
    """The image server, as a fake: no network, and a record of every call."""

    def __init__(self, *, blocked: bool = False, error=None):
        self.calls: list[str] = []
        self.blocked = blocked
        self.error = error

    async def readiness(self):
        self.calls.append("readiness")
        if self.blocked:
            return image_jobs.RigReadiness(can_render=False, reason="no_suitable_gpu",
                                           detail="the cards are busy")
        return image_jobs.RigReadiness()

    async def enqueue(self, spec, **kw):
        self.calls.append("enqueue")
        if self.error is not None:
            raise self.error
        return image_jobs.EnqueueResult(job_id="job-1")

    async def poll(self, job_id):
        self.calls.append("poll")
        return image_jobs.PollResult(state="done", done=True, files_rel="a.png")

    async def cancel(self, job_id):
        self.calls.append("cancel")
        return {}

    async def fetch(self, files_rel):
        self.calls.append("fetch")
        return _png()


def _gen(c, **over):
    body = {"prompt": "a lighthouse at dusk", "provider": "workflows",
            "model": "my-workflow", "ratio": "1:1", "n": 1}
    body.update(over)
    return c.post(f"{BASE}/generate", json=body)


def test_one_render_submits_once(people, studio, monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(studio, "_server", lambda: fake)
    r = _gen(people["owner"])
    assert r.status_code == 200, r.text
    assert fake.calls == ["readiness", "enqueue", "poll", "fetch"]
    row = r.json()["images"][0]
    assert row["provider"] == "workflows"
    assert row["model"] == "my-workflow"


def test_an_uncertain_submit_is_never_tried_again(people, studio, monkeypatch):
    fake = FakeServer(error=image_jobs.ImageJobError("the rig did not answer", uncertain=True))
    monkeypatch.setattr(studio, "_server", lambda: fake)
    r = _gen(people["owner"])
    assert r.status_code == 502, r.text
    assert fake.calls.count("enqueue") == 1


def test_a_blocked_readiness_never_enqueues(people, studio, monkeypatch):
    fake = FakeServer(blocked=True)
    monkeypatch.setattr(studio, "_server", lambda: fake)
    r = _gen(people["owner"])
    assert r.status_code == 409, r.text
    assert "enqueue" not in fake.calls


def test_a_bad_workflow_name_and_a_reference_picture_are_refused(people, studio, monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(studio, "_server", lambda: fake)
    assert _gen(people["owner"], model="bad/name").status_code == 422
    assert _gen(people["owner"], model="x" * 81).status_code == 422
    assert _gen(people["owner"], reference={"kind": "media", "path": "/media/x.png"}).status_code == 422
    assert fake.calls == []


def test_a_profile_not_on_the_list_cannot_render(people, studio, monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(studio, "_server", lambda: fake)
    c = _login(people["world"], "Ada", password="pw-pass-1234")  # scrub-ok: throwaway test password
    r = _gen(c)
    assert r.status_code == 403, r.text
    assert fake.calls == []
