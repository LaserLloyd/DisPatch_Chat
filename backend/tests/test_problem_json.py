"""RFC 9457 problem documents on the machine-inbound API.

The point is a stable `code` a model can branch on. The constraint is that
NOTHING already relying on `detail` may change — the frontend, the agent
skills, and the tests all read that key, and a wire-format change that buys
agents a slug at the cost of breaking the browser is a bad trade.

So these tests pin both halves: the machine gets the envelope, the browser gets
exactly what it always got, and `detail` is byte-identical in both.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main, problem
from app.database import Database

BROWSER = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    config._invalidate_bots_cache()
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    with TestClient(main.app, client=("127.0.0.1", 50000)) as c:
        yield c
    asyncio.run(temp_db.close())


def test_a_machine_gets_a_problem_document(client):
    r = client.get("/api/threads/no-such-thread-id")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(problem.MEDIA_TYPE)
    body = r.json()
    assert body["status"] == 404
    assert body["code"] == "thread_not_found"
    assert body["type"] == "/problems/thread_not_found"
    assert "detail" in body


def test_detail_is_unchanged_for_everyone(client):
    """The backward-compatibility promise, stated as an equality."""
    machine = client.get("/api/threads/no-such-thread-id")
    browser = client.get("/api/threads/no-such-thread-id", headers=BROWSER)
    assert machine.json()["detail"] == browser.json()["detail"]


def test_a_browser_still_gets_the_plain_body(client):
    r = client.get("/api/threads/no-such-thread-id", headers=BROWSER)
    assert r.headers["content-type"].startswith("application/json")
    assert set(r.json()) == {"detail"}, "the browser's error body changed shape"


def test_a_refusal_carries_a_code_the_prose_cannot_give(client):
    """A Safe-Mode refusal and a missing thread are both "no" to a model that
    can only read prose. They are different codes."""
    auth.set_pin("1234")
    r = client.post("/api/inject", json={"bot_id": "main", "content": "hi"},
                    headers=BROWSER)
    assert r.status_code == 403
    # Browser-shaped: plain body, as promised.
    assert set(r.json()) == {"detail"}
    assert problem.code_for(403, "Unlock for full access") == "safe_mode"
    assert problem.code_for(404, "thread not found") == "thread_not_found"


def test_validation_errors_keep_the_pydantic_detail(client):
    r = client.post("/api/inject", json={"bot_id": "main"})     # no content
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], list) and body["detail"], (
        "the pydantic error list was replaced rather than wrapped")


def test_an_unmapped_message_degrades_to_a_status_code_not_a_guess(client):
    assert problem.code_for(409, "something nobody mapped") == "conflict"
    assert problem.code_for(418, "teapot") == "http_418"


def test_only_the_machine_surface_is_affected(client):
    """A route that is not machine-inbound keeps the old body even for a
    non-browser caller — the change is scoped, not global."""
    auth.set_pin("1234")
    # Retrieval is deliberately OFF the machine surface (the file drop is
    # one-way), so this route is the clean negative case.
    r = client.get("/api/files/nope/download")
    assert r.status_code >= 400
    assert r.headers["content-type"].startswith("application/json")
    assert not r.headers["content-type"].startswith(problem.MEDIA_TYPE)


def test_the_openapi_schema_documents_what_a_route_refuses(client):
    """It is served to an on-box machine, and it says more than "200 OK"."""
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    responses = r.json()["paths"]["/api/inject"]["post"]["responses"]
    assert {"401", "403", "404", "422"} <= set(responses), sorted(responses)
    assert problem.MEDIA_TYPE in responses["403"]["content"]


def test_the_schema_is_not_handed_to_a_stranger(tmp_path, monkeypatch):
    """The reason /openapi.json is off by default: on 0.0.0.0 it is a complete
    inventory of the unlocked feature surface."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(auth, "SECURITY_PATH", tmp_path / "security.yaml")
    monkeypatch.setattr(auth, "RECOVERY_PATH", tmp_path / "RECOVERY-CODE.txt")
    auth._sessions.clear()
    auth._cache = None
    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    with TestClient(main.app, client=("203.0.113.5", 50000)) as c:
        auth.set_pin("1234")
        assert c.get("/api/openapi.json").status_code == 403
    asyncio.run(temp_db.close())
