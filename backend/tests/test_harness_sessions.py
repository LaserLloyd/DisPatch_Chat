"""Tests for live DeepSeek Harness sessions (`app/harness_sessions.py`).

Nothing here touches the real dsh, systemd, or ~/.dsh: the runner is given a
FAKE dsh binary that writes a session log the same way dsh does (appended zstd
frames of JSONL under `$DSH_HOME/sessions/<slug>/session-<id>/`), so launch,
live tailing, stop and disappear are all exercised for real.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, config, harness, harness_sessions, main
from app.database import Database

FAKE_DSH = '''#!{python}
"""A stand-in for `dsh --profile headless <task>`: writes a live session log."""
import json, os, pathlib, subprocess, sys, time

home = pathlib.Path(os.environ["DSH_HOME"])
cwd = pathlib.Path.cwd()
slug = "-" + str(cwd).replace("/", "-") + "-"
d = home / "sessions" / slug / "session-fake"
d.mkdir(parents=True, exist_ok=True)
log = d / "session.v3.jsonl.zstd"


def emit(ev):
    payload = (json.dumps(ev) + "\\n").encode()
    frame = subprocess.run(["zstd", "-q", "-c"], input=payload,
                           stdout=subprocess.PIPE, check=True).stdout
    with open(log, "ab") as fh:
        fh.write(frame)


long = "long" in " ".join(sys.argv)
emit({{"type": "session/title", "seq": 1, "data": {{"title": "fake run"}}}})
emit({{"type": "tool/call", "seq": 2,
       "data": {{"name": "write", "arguments": "{{\\"file_path\\": \\"x\\"}}"}}}})
time.sleep(0.1)
emit({{"type": "assistant/message", "seq": 3,
       "data": {{"message": {{"role": "assistant",
                             "content": [{{"type": "text", "text": "working"}}]}}}}}})
if long:
    for i in range(120):
        time.sleep(0.25)
        emit({{"type": "step/start", "seq": 10 + i,
               "data": {{"turn": 1, "step": i + 2}}}})
else:
    emit({{"type": "turn/end", "seq": 99, "data": {{"reason": {{"kind": "completed"}}}}}})
'''


@pytest.fixture
def fake_dsh(tmp_path):
    p = tmp_path / "fake-dsh"
    p.write_text(FAKE_DSH.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def shared_home(tmp_path, monkeypatch):
    """A throwaway ~/.dsh with a settings file and a credentials file."""
    home = tmp_path / "dsh-home"
    home.mkdir()
    (home / "settings.yaml").write_text(
        "agent-default-model:\n  provider: deepseek-official\n  model: deepseek-v4-flash\n"
        "llm-deepseek:\n  models:\n    - id: deepseek-v4-flash\n")
    (home / ".credentials.yaml").write_text("{}\n")
    monkeypatch.setattr(harness, "dsh_home", lambda: home)
    monkeypatch.setenv("DSH_HOME", str(home))
    return home


async def _await_until(pred, timeout=6.0):
    """Poll a predicate on the CURRENT loop (tests call this from inside
    asyncio.run; a sync wrapper would trip 'cannot be called from a running
    event loop')."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


def _wait(pred, timeout=6.0):
    """Sync wrapper for the route tests, which are not async."""
    return asyncio.run(_await_until(pred, timeout))


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #

def _ev(**kw):
    return json.dumps(kw)


def test_project_tool_call_and_result():
    call = harness_sessions.project(_ev(
        type="tool/call", seq=2, data={"name": "write", "arguments": '{"a":1}'}))
    assert call["k"] == "tool" and call["name"] == "write" and '{"a":1}' in call["args"]

    ok = harness_sessions.project(_ev(type="tool/result", seq=3, data={"message": {
        "content": [{"type": "tool-result", "content": [{"type": "text", "text": "done"}]}]}}))
    assert ok["k"] == "result" and ok["ok"] is True and ok["text"] == "done"

    bad = harness_sessions.project(_ev(type="tool/result", seq=4, data={"message": {
        "content": [{"type": "tool-result", "isError": True,
                     "content": [{"type": "text", "text": "boom"}]}]}}))
    assert bad["ok"] is False


def test_project_assistant_message_joins_text_blocks():
    item = harness_sessions.project(_ev(type="assistant/message", seq=5, data={"message": {
        "content": [{"type": "reasoning", "text": "hmm"},
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"}]}}))
    assert item["k"] == "say" and item["text"] == "line one\nline two"


def test_project_ignores_machinery_and_reasoning_only():
    assert harness_sessions.project("not json") is None
    assert harness_sessions.project(_ev(type="assistant/chunk", data={})) is None
    # A reasoning-only assistant message has nothing to render.
    assert harness_sessions.project(_ev(type="assistant/message", data={"message": {
        "content": [{"type": "reasoning", "text": "thinking"}]}})) is None
    assert harness_sessions.project(_ev(type="request/header",
                                        data={"header": {"system": "x" * 999}})) is None


def test_project_title_and_turn_end():
    assert harness_sessions.project(_ev(type="session/title",
                                        data={"title": "Fix the thing"}))["text"] == "Fix the thing"
    end = harness_sessions.project(_ev(type="turn/end", data={"reason": {"kind": "completed"}}))
    assert end["k"] == "end" and end["reason"] == "completed"


def test_project_clips_giant_fields():
    big = "x" * 9000
    item = harness_sessions.project(_ev(type="assistant/message", data={"message": {
        "content": [{"type": "text", "text": big}]}}))
    assert len(item["text"]) <= 4000


def test_project_strips_reasoning_and_drops_injected_scaffold():
    item = harness_sessions.project(_ev(type="assistant/message", data={"message": {
        "content": [{"type": "text", "text": "<think>secret plan</think>\nAnswer."}]}}))
    assert item["text"] == "Answer."
    # dsh injects its own runtime-context block as a user message; the card
    # already carries the task, so those are not projected at all.
    assert harness_sessions.project(_ev(type="user/message", data={"content": [
        {"type": "text", "text": "Current runtime context. workspace-write."}]})) is None
    # A title that is nothing but reasoning is dropped too.
    assert harness_sessions.project(_ev(type="session/title",
                                        data={"title": "<think>hmm</think>"})) is None


# --------------------------------------------------------------------------- #
# Model selector parsing
# --------------------------------------------------------------------------- #

def test_split_model():
    assert harness_sessions.split_model("minimax/MiniMax-M3") == ("minimax", "MiniMax-M3")
    assert harness_sessions.split_model(None) == (None, None)
    assert harness_sessions.split_model("") == (None, None)
    assert harness_sessions.split_model({"provider": "minimax", "model": "M3"}) == ("minimax", "M3")
    for bad in ("justamodel", "provider/", "/model"):
        with pytest.raises(harness.ValidationError):
            harness_sessions.split_model(bad)


# --------------------------------------------------------------------------- #
# Runner lifecycle (fake dsh)
# --------------------------------------------------------------------------- #

def test_launch_tails_the_log_live_then_exits(tmp_path, fake_dsh, shared_home):
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("quick task", work)
        assert s["state"] == "running" and s["pid"]
        assert s["title"] == "quick task"          # task's first line until the log says otherwise

        deadline = asyncio.get_running_loop().time() + 8
        seen = []
        while asyncio.get_running_loop().time() < deadline:
            d = r.session(s["id"], after=0)
            if d is None:
                break
            seen = d["events"]
            if d["state"] in ("exited", "failed") and len(seen) >= 4:
                break
            await asyncio.sleep(0.15)

        kinds = [e["k"] for e in seen]
        assert "title" in kinds and "tool" in kinds and "say" in kinds, kinds
        assert [e["n"] for e in seen] == list(range(1, len(seen) + 1)), "n must be 1..N"
        final = r.session(s["id"], after=0)
        assert final["state"] == "exited" and final["exit_code"] == 0
        assert final["title"] == "fake run", "the log's own title should win"
        await r.shutdown()

    asyncio.run(go())


def test_after_returns_only_new_events(tmp_path, fake_dsh, shared_home):
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("quick task", work)
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            d = r.session(s["id"], after=0)
            if d and len(d["events"]) >= 3:
                break
            await asyncio.sleep(0.15)
        first = r.session(s["id"], after=0)
        n = first["next"]
        assert n == len(first["events"])
        # Nothing new yet → empty, and `next` stays put.
        empty = r.session(s["id"], after=n)
        assert empty["events"] == [] and empty["next"] == n
        await r.shutdown()

    asyncio.run(go())


def test_stop_removes_the_session_and_kills_the_process(tmp_path, fake_dsh, shared_home):
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("a long job", work)
        assert r.status()["running"] == 1
        assert await _await_until(lambda: (r.session(s["id"]) or {}).get("pid") is not None)
        await r.stop(s["id"])
        # Gone from the list the instant it is stopped…
        assert r.session(s["id"]) is None
        assert r.status() == {"sessions": [], "running": 0,
                              "limit": harness_sessions.SESSION_MAX}
        # …and the scratch home goes with it.
        await asyncio.sleep(0.6)
        assert not (tmp_path / "root" / s["id"]).exists()
        await r.shutdown()

    asyncio.run(go())


def test_dismiss_removes_a_finished_session(tmp_path, fake_dsh, shared_home):
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("quick task", work)
        assert await _await_until(lambda: (r.session(s["id"]) or {}).get("state") == "exited")
        assert r.status()["running"] == 0
        await r.dismiss(s["id"])
        assert r.session(s["id"]) is None
        await r.shutdown()

    asyncio.run(go())


def test_dismiss_refuses_a_running_session(tmp_path, fake_dsh, shared_home):
    """Clearing a live session would hide a process nobody can stop."""
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("a long job", work)
        with pytest.raises(harness.HarnessBusyError):
            await r.dismiss(s["id"])
        assert r.session(s["id"]) is not None, "a refused dismiss must not remove it"
        await r.stop(s["id"])
        assert r.session(s["id"]) is None
        await r.shutdown()

    asyncio.run(go())


def test_session_limit_is_enforced(tmp_path, fake_dsh, shared_home, monkeypatch):
    monkeypatch.setattr(harness_sessions, "SESSION_MAX", 1)
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        await r.launch("a long job one", work)
        with pytest.raises(harness.HarnessBusyError):
            await r.launch("a long job two", work)
        await r.shutdown()

    asyncio.run(go())


def test_scratch_home_pins_the_model_without_touching_shared_settings(
        tmp_path, fake_dsh, shared_home):
    work = tmp_path / "work"
    work.mkdir()
    r = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")

    async def go():
        s = await r.launch("quick task", work, model="minimax/MiniMax-M3")
        home = tmp_path / "root" / s["id"]
        import yaml
        data = yaml.safe_load((home / "settings.yaml").read_text())
        assert data["agent-default-model"] == {"provider": "minimax", "model": "MiniMax-M3"}
        # The provider ROUTES must come along, or dsh says NO_ADAPTER.
        assert "llm-deepseek" in data
        # Credentials are a symlink to the one 0600 file, never a copy.
        link = home / ".credentials.yaml"
        assert link.is_symlink() and link.resolve() == (shared_home / ".credentials.yaml").resolve()
        # …and the shared file is untouched.
        assert "minimax" not in (shared_home / "settings.yaml").read_text()
        await r.shutdown()

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@pytest.fixture
def route_client(tmp_path, monkeypatch, fake_dsh):
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

    temp_db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", temp_db)
    monkeypatch.setattr(main, "SETTINGS", replace(config.SETTINGS, harness_enabled=True))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))

    runner = harness_sessions.SessionRunner(binary=str(fake_dsh), root=tmp_path / "root")
    monkeypatch.setattr(harness_sessions, "runner", runner)
    # `validate_cwd` insists the working directory is under $HOME, and the
    # test's tmp dir is not — re-base it on tmp_path, keeping the real logic.
    _real_validate_cwd = harness.validate_cwd
    monkeypatch.setattr(harness, "validate_cwd",
                        lambda cwd=None: _real_validate_cwd(cwd, home=tmp_path))

    with TestClient(main.app) as client:
        yield client, runner

    asyncio.run(temp_db.close())


def _unlock(client, pin="1234"):
    auth.set_pin(pin)
    assert client.post("/api/auth/unlock", json={"pin": pin}).status_code == 200


def test_sessions_routes_launch_live_and_stop(route_client, tmp_path):
    client, runner = route_client
    _unlock(client)
    work = tmp_path / "work"
    work.mkdir()

    r = client.post("/api/harness/sessions",
                    json={"task": "a long job", "cwd": str(work)})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    assert client.get("/api/harness/sessions").json()["running"] == 1
    deadline = _wait(lambda: bool(
        client.get(f"/api/harness/sessions/{sid}").json().get("events")))
    assert deadline, "no events were projected from the live log"

    detail = client.get(f"/api/harness/sessions/{sid}").json()
    assert detail["active"] is True and detail["cwd"] == str(work)

    stopped = client.post(f"/api/harness/sessions/{sid}/stop")
    assert stopped.status_code == 200
    assert client.get(f"/api/harness/sessions/{sid}").status_code == 404
    assert client.get("/api/harness/sessions").json()["sessions"] == []


def test_sessions_routes_reject_bad_input(route_client):
    client, _ = route_client
    _unlock(client)
    assert client.post("/api/harness/sessions", json={"task": ""}).status_code == 422
    assert client.post("/api/harness/sessions",
                       json={"task": "--dump-default-config"}).status_code == 422
    assert client.post("/api/harness/sessions",
                       json={"task": "x", "model": "noslash"}).status_code == 422
    assert client.post("/api/harness/sessions/ deadbeef/stop").status_code in (404, 405)
    assert client.post("/api/harness/sessions/nope/stop").status_code == 404
