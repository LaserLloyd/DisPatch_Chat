"""The File Server and chat attachments per owner (4.0, BUILD-PLAN-v2 §1.11 / WP-DEL).

* upload: ``files_upload`` (every role); a chat attachment: ``attach_files``;
* list / download / delete a File Server file: ``files_read`` (User, Admin) and the owner -
  another profile's file is a 404 for everyone, Admins included; a Basic profile uploads only;
* a chat attachment is its owner's to download (``use_chat``) and delete (``manage_history``);
* a machine lists the protected Admin's files only; the 200 MB/day machine guard stays;
* the wipe-by-date button touches the caller's own files only;
* Reset deletes the profile's File Server files too and counts them.

Run: cd backend && uv run pytest tests/test_fileserver_owner.py
"""
from __future__ import annotations

import pytest
import yaml

from app import accounts, auth, config, main, ownership, profiles_service

PROV = {"providers": [{"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1"}]}
TOKEN = "fileserver-owner-token-1"  # scrub-ok: throwaway test fixture


@pytest.fixture
def house(world):
    owner = world.create_owner(world.client())
    doc = yaml.safe_load(config.CONFIG_PATH.read_text()) or {}
    doc.setdefault("bots", []).append({"id": "chatty", "name": "Chatty", "advisor": PROV})
    config.CONFIG_PATH.write_text(yaml.safe_dump(doc))
    config._invalidate_bots_cache()
    from agents_world import sibling
    people = {}
    for i, (name, role) in enumerate((("Uma", "user"), ("Una", "user"), ("Bea", "basic"),
                                       ("Ada", "admin"))):
        extra = {"household_bots": ["chatty"]} if role == "basic" else {}
        pw = "admin-pass-22" if role == "admin" else None
        p = world.person(owner, name, role, pin=None if pw else f"{i + 3}{i + 3}{i + 3}{i + 3}",
                         password=pw, **extra)
        c = sibling(world, owner, (f"203.0.113.{170 + i}", 49100 + i))
        r = (world.login(c, profile_id=p["id"], password=pw) if pw
             else world.login(c, profile_id=p["id"], pin=f"{i + 3}{i + 3}{i + 3}{i + 3}"))
        assert r.status_code == 200, r.text
        people[name] = (c, p["id"])
    cfg = auth.load()
    cfg.api_token = TOKEN
    cfg.machine_loopback_trust = False
    auth._write(cfg)
    auth._bust_cache()
    machine = world.client(world.LOOPBACK, origin=False)
    machine.headers.update({"X-API-Key": TOKEN})
    ownership.forget_all()
    return {"owner": owner, "machine": machine, "people": people, "world": world}


def _drop(c, name: str, body: bytes = b"hello") -> dict:
    r = c.post("/api/files", files={"file": (name, body, "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()


def _attach(c, name: str) -> dict:
    r = c.post("/api/upload", files={"file": (name, b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_user_lists_downloads_and_deletes_only_their_own_files(house):
    uma, _ = house["people"]["Uma"]
    una, _ = house["people"]["Una"]
    mine = _drop(uma, "SENT-UMA.txt")
    theirs = _drop(una, "SENT-UNA.txt")
    lst = uma.get("/api/files")
    assert lst.status_code == 200
    assert "SENT-UMA" in lst.text and "SENT-UNA" not in lst.text
    assert uma.get(f"/api/files/{mine['id']}/download").status_code == 200
    for path in (f"/api/files/{theirs['id']}/download", f"/api/files/{theirs['id']}/raw"):
        r = uma.get(path)
        assert r.status_code == 404 and "SENT-UNA" not in r.text
    assert uma.delete(f"/api/files/{theirs['id']}").status_code == 404
    assert una.get(f"/api/files/{theirs['id']}/download").status_code == 200    # still there
    assert uma.delete(f"/api/files/{mine['id']}").status_code == 200


def test_an_admin_cannot_open_another_profiles_file(house):
    uma, _ = house["people"]["Uma"]
    ada, _ = house["people"]["Ada"]
    f = _drop(uma, "SENT-UMA-PRIVATE.txt")
    for c in (house["owner"], ada):
        assert "SENT-UMA-PRIVATE" not in c.get("/api/files").text
        assert c.get(f"/api/files/{f['id']}/download").status_code == 404
        assert c.delete(f"/api/files/{f['id']}").status_code == 404
    own = _drop(house["owner"], "SENT-OWNER.txt")
    assert house["owner"].get(f"/api/files/{own['id']}/download").status_code == 200
    assert ada.get(f"/api/files/{own['id']}/download").status_code == 404


def test_a_basic_profile_uploads_but_never_reads_the_file_server(house):
    bea, _ = house["people"]["Bea"]
    f = _drop(bea, "SENT-BEA.txt")
    assert bea.get("/api/files").status_code == 403
    for path in (f"/api/files/{f['id']}/download", f"/api/files/{f['id']}/raw"):
        r = bea.get(path)
        assert r.status_code == 403 and "SENT-BEA" not in r.text
    assert bea.delete(f"/api/files/{f['id']}").status_code == 403
    r = bea.post("/api/drop", files={"file": ("SENT-BEA-DROP.txt", b"x", "text/plain")})
    assert r.status_code == 200, r.text


def test_a_chat_attachment_is_its_owners_to_download_and_delete(house):
    bea, _ = house["people"]["Bea"]
    uma, _ = house["people"]["Uma"]
    a = _attach(bea, "SENT-BEA-DOC.pdf")
    assert bea.get(f"/api/files/{a['id']}/download").status_code == 200
    assert uma.get(f"/api/files/{a['id']}/download").status_code == 404
    assert house["owner"].get(f"/api/files/{a['id']}/download").status_code == 404
    assert bea.delete(f"/api/files/{a['id']}").status_code == 200


def test_the_machine_lists_the_protected_admins_files_only(house):
    uma, _ = house["people"]["Uma"]
    _drop(uma, "SENT-UMA-M.txt")
    _drop(house["owner"], "SENT-OWNER-M.txt")
    r = house["machine"].get("/api/files")
    assert r.status_code == 200
    assert "SENT-OWNER-M" in r.text and "SENT-UMA-M" not in r.text


def test_the_machine_upload_guard_is_a_machine_limit_only(house, monkeypatch):
    monkeypatch.setattr(main, "MACHINE_UPLOAD_QUOTA", 64)
    big = b"x" * 100
    r = house["machine"].post("/api/upload", files={"file": ("a.pdf", big, "application/pdf")})
    assert r.status_code == 429
    uma, _ = house["people"]["Uma"]
    r = uma.post("/api/upload", files={"file": ("a.pdf", big, "application/pdf")})
    assert r.status_code == 200, r.text          # a person has no daily limit
    assert accounts.usage_get("machine", "upload_bytes") <= 64


def test_wipe_by_date_touches_only_the_callers_files(house):
    uma, _ = house["people"]["Uma"]
    una, _ = house["people"]["Una"]
    _drop(uma, "SENT-UMA-W.txt")
    keep = _drop(una, "SENT-UNA-W.txt")
    r = uma.post("/api/files/wipe", json={"before": "2999-01-01T00:00:00+00:00"})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert una.get(f"/api/files/{keep['id']}/download").status_code == 200


def test_reset_deletes_the_profiles_files_and_counts_them(house):
    una, una_id = house["people"]["Una"]
    uma, _ = house["people"]["Uma"]
    gone = _drop(una, "SENT-UNA-R.txt")
    kept = _drop(uma, "SENT-UMA-R.txt")
    prev = profiles_service.reset_preview(una_id)
    assert prev["counts"]["files"] == 1
    profiles_service.reset_profile(una_id, prev["token"])
    assert uma.get(f"/api/files/{kept['id']}/download").status_code == 200
    assert una.get(f"/api/files/{gone['id']}/download").status_code == 404
