"""Unit tests for app.harness — settings.yaml catalog/default-model round trip,
task/cwd validation, and the headless JobRunner against a fake `dsh` binary
(a tiny shell script; never the real harness). Run: cd backend && uv run pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap
from pathlib import Path

import pytest
import yaml

from app import harness

# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #

def test_discover_models_missing_file_yields_shipped_defaults(tmp_path):
    out = harness.discover_models(tmp_path / "nope.yaml")
    assert out["current"] is None
    ds = out["providers"][0]
    assert ds["id"] == "deepseek-official"
    assert [m["id"] for m in ds["models"]] == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_discover_models_reads_deepseek_and_pi_ai_routes(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(textwrap.dedent("""
        agent-default-model: {provider: buildpc, model: local/foo}
        llm-deepseek:
          models:
            - id: deepseek-v4-pro
              name: Pro
        llm-pi-ai:
          providers:
            buildpc:
              displayName: Acme Inference
              api: openai-completions
              baseURL: http://rig:1234/v1
              models:
                - id: local/foo
                - {id: local/bar, name: Bar}
    """))
    out = harness.discover_models(p)
    assert out["current"] == {"provider": "buildpc", "model": "local/foo"}
    by_id = {pr["id"]: pr for pr in out["providers"]}
    assert [m["id"] for m in by_id["deepseek-official"]["models"]] == ["deepseek-v4-pro"]
    assert by_id["buildpc"]["name"] == "Acme Inference"
    assert [(m["id"], m["name"]) for m in by_id["buildpc"]["models"]] == \
        [("local/foo", "local/foo"), ("local/bar", "Bar")]


def test_discover_models_lists_any_configured_route_verbatim(tmp_path, monkeypatch):
    """The picker is driven by settings.yaml, not by a built-in model list: a
    route nobody wrote code for (here MiniMax, whose ids are mixed-case) shows
    up, with its ids preserved character for character."""
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(tmp_path / "no-catalog"))
    p = tmp_path / "settings.yaml"
    p.write_text(textwrap.dedent("""
        llm-pi-ai:
          providers:
            minimax:
              displayName: MiniMax (cloud)
              apiKeyEnv: MINIMAX_API_KEY
              api: openai-completions
              baseURL: https://api.minimax.io/v1
              models:
                - id: MiniMax-M3
                  name: MiniMax M3
                - id: MiniMax-M2.7-highspeed
    """))
    by_id = {pr["id"]: pr for pr in harness.discover_models(p)["providers"]}
    mm = by_id["minimax"]
    assert mm["name"] == "MiniMax (cloud)"
    assert [(m["id"], m["name"]) for m in mm["models"]] == [
        ("MiniMax-M3", "MiniMax M3"),
        ("MiniMax-M2.7-highspeed", "MiniMax-M2.7-highspeed"),
    ]
    # And that exact id survives a write + re-read (no lowercasing anywhere).
    assert harness.set_default_model("minimax", "MiniMax-M3", p) == \
        {"provider": "minimax", "model": "MiniMax-M3"}
    assert harness.discover_models(p)["current"] == \
        {"provider": "minimax", "model": "MiniMax-M3"}
    assert "MiniMax-M3" in p.read_text()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def _fake_pi_ai_catalog(root: Path) -> Path:
    """A stand-in for @earendil-works/pi-ai's bundled provider data."""
    prov = root / "providers"
    (prov / "data").mkdir(parents=True)
    (prov / "data" / "minimax.json").write_text(json.dumps({
        "anthropic-messages": {
            "MiniMax-M2.7": {"id": "MiniMax-M2.7", "name": "MiniMax-M2.7"},
            "MiniMax-M3": {"id": "MiniMax-M3", "name": "MiniMax-M3"},
        }
    }))
    (prov / "minimax.js").write_text(
        'export function minimaxProvider() { return createProvider({\n'
        '  id: "minimax",\n  name: "MiniMax",\n  baseUrl: "https://api.minimax.io/anthropic",\n});}\n')
    return prov


def test_discover_models_falls_back_to_pi_ai_catalog_when_models_omitted(tmp_path, monkeypatch):
    """A route may legally omit `models:` — the adapter then serves pi-ai's
    installed catalog for it. Without this the picker showed an EMPTY minimax
    provider (the frontend skips model-less providers), so M3 was unreachable
    from a perfectly valid two-line route."""
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(_fake_pi_ai_catalog(tmp_path / "pi")))
    p = tmp_path / "settings.yaml"
    p.write_text("llm-pi-ai:\n  providers:\n    minimax:\n      apiKeyEnv: MINIMAX_API_KEY\n")
    mm = {pr["id"]: pr for pr in harness.discover_models(p)["providers"]}["minimax"]
    assert mm["from_catalog"] is True
    assert mm["name"] == "MiniMax"                 # pi-ai's own display name
    assert [m["id"] for m in mm["models"]] == ["MiniMax-M2.7", "MiniMax-M3"]


def test_pi_ai_catalog_route_key_cannot_escape_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(_fake_pi_ai_catalog(tmp_path / "pi")))
    assert harness.catalog_models("minimax")       # sanity: the fixture is live
    for bad in ("../../etc/passwd", "/etc/passwd", "..", "min imax", "", None):
        assert harness.catalog_models(bad) == []
        assert harness.catalog_display_name(bad) is None
    assert harness.catalog_models("no-such-provider") == []


def test_pi_ai_catalog_absent_or_broken_degrades_quietly(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(tmp_path / "gone"))
    assert harness.pi_ai_catalog_dir() is None
    assert harness.catalog_models("minimax") == []
    prov = _fake_pi_ai_catalog(tmp_path / "pi")
    (prov / "data" / "minimax.json").write_text("{not json")
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(prov))
    assert harness.catalog_models("minimax") == []
    p = tmp_path / "settings.yaml"
    p.write_text("llm-pi-ai:\n  providers:\n    minimax: {}\n")
    mm = {pr["id"]: pr for pr in harness.discover_models(p)["providers"]}["minimax"]
    assert mm["models"] == [] and mm["from_catalog"] is False


def test_explicit_models_list_replaces_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(_fake_pi_ai_catalog(tmp_path / "pi")))
    p = tmp_path / "settings.yaml"
    p.write_text("llm-pi-ai:\n  providers:\n    minimax:\n      models: [{id: MiniMax-M3}]\n")
    mm = {pr["id"]: pr for pr in harness.discover_models(p)["providers"]}["minimax"]
    assert [m["id"] for m in mm["models"]] == ["MiniMax-M3"] and mm["from_catalog"] is False


def test_model_overrides_rename_catalog_entries(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.PI_AI_CATALOG_DIR_ENV, str(_fake_pi_ai_catalog(tmp_path / "pi")))
    p = tmp_path / "settings.yaml"
    p.write_text(textwrap.dedent("""
        llm-pi-ai:
          providers:
            minimax:
              modelOverrides:
                MiniMax-M3: {name: M3 (1M ctx)}
    """))
    mm = {pr["id"]: pr for pr in harness.discover_models(p)["providers"]}["minimax"]
    assert {m["id"]: m["name"] for m in mm["models"]}["MiniMax-M3"] == "M3 (1M ctx)"


def test_discover_models_malformed_file_never_raises(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("- just\n- a list\n")
    out = harness.discover_models(p)
    assert out["current"] is None
    assert out["providers"][0]["id"] == "deepseek-official"


def test_set_default_model_round_trip_preserves_other_sections(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("llm-pi-ai:\n  providers:\n    x:\n      baseURL: http://x/v1\n      models: [{id: m1}]\n")
    sel = harness.set_default_model("x", "m1", p)
    assert sel == {"provider": "x", "model": "m1"}
    data = yaml.safe_load(p.read_text())
    assert data["agent-default-model"] == {"provider": "x", "model": "m1"}
    assert data["llm-pi-ai"]["providers"]["x"]["baseURL"] == "http://x/v1"
    # 0600, and no leftover temp file.
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not (tmp_path / "settings.yaml.tmp").exists()
    # And the reader agrees.
    assert harness.discover_models(p)["current"] == sel


def test_set_default_model_creates_file_when_absent(tmp_path):
    p = tmp_path / "sub" / "settings.yaml"
    harness.set_default_model("deepseek-official", "deepseek-v4-pro", p)
    assert yaml.safe_load(p.read_text())["agent-default-model"]["model"] == "deepseek-v4-pro"


@pytest.mark.parametrize("prov,model", [
    ("", "m"), ("p", ""), ("-p", "m"), ("p", "-m"), ("p q", "m"), ("p", "m\n"),
    (None, "m"), ("p", 3), ("p", "x" * 129),
])
def test_set_default_model_rejects_bad_ids(tmp_path, prov, model):
    with pytest.raises(harness.ValidationError):
        harness.set_default_model(prov, model, tmp_path / "s.yaml")


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def test_validate_task():
    assert harness.validate_task("  do it  ") == "do it"
    for bad in ("", "   ", None, 5, "x\x00y", "a" * (harness.TASK_MAX_CHARS + 1)):
        with pytest.raises(harness.ValidationError):
            harness.validate_task(bad)


def test_validate_task_rejects_a_flag_shaped_task():
    """The task is argv[3]; a leading dash makes dsh parse it as an OPTION
    (`--dump-default-config` was verified to run), so it is refused."""
    for bad in ("--dump-default-config", "  --help", "-v", "--profile=web"):
        with pytest.raises(harness.ValidationError):
            harness.validate_task(bad)
    # A dash INSIDE the task is ordinary prose and stays allowed.
    assert harness.validate_task("rename foo --bar to baz") == "rename foo --bar to baz"


def test_validate_cwd_home_and_children(tmp_path):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    assert harness.validate_cwd(None, home) == home.resolve()
    assert harness.validate_cwd("", home) == home.resolve()
    assert harness.validate_cwd(str(home / "proj"), home) == (home / "proj").resolve()
    # relative → under home
    assert harness.validate_cwd("proj", home) == (home / "proj").resolve()


def test_validate_cwd_rejects_escapes(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir(); outside.mkdir()
    (home / "link").symlink_to(outside)
    (home / "file").write_text("x")
    for bad in (str(outside), str(home / "link"), str(home / ".." / "outside"),
                str(home / "missing"), str(home / "file"), "x\x00", 12):
        with pytest.raises(harness.ValidationError):
            harness.validate_cwd(bad, home)


# --------------------------------------------------------------------------- #
# JobRunner against a fake dsh
# --------------------------------------------------------------------------- #

def _fake_dsh(tmp_path: Path, body: str) -> str:
    p = tmp_path / "dsh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


async def _wait_until(pred, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_job_runner_runs_fixed_argv_and_captures_output(tmp_path):
    # Echo the argv so the test can assert the exact shape: --profile headless <task>.
    binary = _fake_dsh(tmp_path, 'printf "%s|" "$@"; pwd; echo err >&2; exit 0\n')
    (tmp_path / "ws").mkdir()

    async def go():
        r = harness.JobRunner(binary=binary)
        seen = []
        r.add_state_hook(lambda st: seen.append(st["running"]))
        job = await r.submit("say hi; rm -rf /", tmp_path / "ws")
        assert job["state"] in ("queued", "running")
        assert await _wait_until(lambda: r.job(job["id"])["state"] == "done")
        j = r.job(job["id"])
        assert j["exit_code"] == 0
        # The task reached argv as ONE element (the shell metacharacters are inert).
        assert j["output"].startswith("--profile|headless|say hi; rm -rf /|")
        assert str((tmp_path / "ws").resolve()) in j["output"]
        assert j["error"].strip() == "err"
        assert seen == [True, False]
        assert r.status()["running"] is False
        assert r.status()["history"][0]["id"] == job["id"]
        assert "output" not in r.status()["history"][0]     # summaries omit bodies
    asyncio.run(go())


def test_job_runner_nonzero_exit_is_failed(tmp_path):
    binary = _fake_dsh(tmp_path, 'echo nope; exit 1\n')

    async def go():
        r = harness.JobRunner(binary=binary)
        job = await r.submit("x", tmp_path)
        assert await _wait_until(lambda: r.job(job["id"])["state"] == "failed")
        assert r.job(job["id"])["exit_code"] == 1
    asyncio.run(go())


def test_job_runner_one_at_a_time_and_cancel(tmp_path):
    binary = _fake_dsh(tmp_path, 'sleep 30\n')

    async def go():
        r = harness.JobRunner(binary=binary)
        job = await r.submit("long", tmp_path)
        assert await _wait_until(lambda: r.status()["running"])
        with pytest.raises(harness.HarnessBusyError):
            await r.submit("another", tmp_path)
        await r.cancel()
        assert await _wait_until(lambda: r.job(job["id"])["state"] == "cancelled")
        assert r.status()["running"] is False
        # After cancel a new job is accepted again.
        job2 = await r.submit("again", tmp_path)
        assert job2["id"] == job["id"] + 1
        assert await _wait_until(lambda: r.job(job2["id"])["state"] == "running")
        await r.cancel()
        assert await _wait_until(lambda: r.job(job2["id"])["state"] == "cancelled")
    asyncio.run(go())


def test_job_runner_timeout(tmp_path):
    binary = _fake_dsh(tmp_path, 'sleep 30\n')

    async def go():
        r = harness.JobRunner(binary=binary, timeout=0.3)
        job = await r.submit("slow", tmp_path)
        assert await _wait_until(lambda: r.job(job["id"])["state"] == "timeout")
        assert r.status()["running"] is False
    asyncio.run(go())


def test_job_runner_bounds_a_flooding_job(tmp_path, monkeypatch):
    """A job that never stops printing must not be buffered whole in RAM.

    `communicate()` held everything the child wrote before the truncation to
    JOB_OUTPUT_MAX ran, so a runaway job was an OOM rather than a big log.
    Now only the tail is kept and the process is killed at the hard ceiling.
    """
    monkeypatch.setattr(harness, "JOB_OUTPUT_HARD_MAX", 256 * 1024)
    monkeypatch.setattr(harness, "JOB_OUTPUT_MAX", 4096)
    # yes(1) floods forever: without a ceiling this never terminates.
    binary = _fake_dsh(tmp_path, 'yes AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n')

    async def go():
        r = harness.JobRunner(binary=binary, timeout=30)
        job = await r.submit("flood", tmp_path)
        assert await _wait_until(
            lambda: r.job(job["id"])["state"] not in ("queued", "running"),
            timeout=20)
        j = r.job(job["id"])
        assert len(j["output"]) <= 4096, "the whole flood was buffered"
        assert "output ceiling reached" in j["error"]
        assert r.status()["running"] is False
    asyncio.run(go())


def test_job_runner_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "resolve_binary", lambda: None)

    async def go():
        r = harness.JobRunner()
        with pytest.raises(harness.HarnessError):
            await r.submit("x", tmp_path)
    asyncio.run(go())


def test_job_env_strips_tmpdir_and_appends_configured_path(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/somewhere")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DISPATCH_HARNESS_PATH", "/opt/node/bin")
    env = harness._job_env()
    assert "TMPDIR" not in env
    assert env["PATH"].split(":")[-1] == "/opt/node/bin"
    assert env["DSH_HOME"]


def test_job_env_leaves_path_alone_when_unconfigured(monkeypatch):
    """Unset DISPATCH_HARNESS_PATH must not invent an entry — the old code
    appended a hardcoded home directory here."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("DISPATCH_HARNESS_PATH", raising=False)
    assert harness._job_env()["PATH"] == "/usr/bin"


# --------------------------------------------------------------------------- #
# set_default_model: the shared settings.yaml must survive being written to
# --------------------------------------------------------------------------- #

SETTINGS_WITH_KNOWLEDGE = """\
# DeepSeek Harness (dsh) user settings — $DSH_HOME/settings.yaml
# Hot-reloads: model/provider changes apply on the NEXT request.
agent-default-model:
  provider: minimax
  model: MiniMax-M3
  reasoningEffort: high
llm-pi-ai:
  providers:
    buildpc:
      displayName: Local LLM server
      models:
        # Curated to the models actually wired into the agent fleet, not the
        # rig's whole disk. JoyFox is deliberately absent: 0/5 on tool calls,
        # and dsh is a tool-using agent.
        - id: some/model
          name: Some Model
"""


def _write_settings(tmp_path, text=SETTINGS_WITH_KNOWLEDGE):
    p = tmp_path / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_set_default_model_preserves_comments(tmp_path):
    """A model switch must not delete the file's documentation.

    Regression: the PyYAML round-trip erased the curated-catalog note and the
    'JoyFox is deliberately absent' warning — knowledge written down to stop
    someone repeating a known mistake.
    """
    p = _write_settings(tmp_path)
    harness.set_default_model("deepseek-official", "deepseek-v4-flash", path=p)
    out = p.read_text(encoding="utf-8")
    assert "JoyFox is deliberately absent" in out
    assert "Curated to the models actually wired" in out
    assert "Hot-reloads: model/provider changes apply" in out
    data = yaml.safe_load(out)
    assert data["agent-default-model"]["provider"] == "deepseek-official"
    assert data["agent-default-model"]["model"] == "deepseek-v4-flash"


def test_set_default_model_keeps_sibling_keys_in_the_block(tmp_path):
    p = _write_settings(tmp_path)
    harness.set_default_model("deepseek-official", "deepseek-v4-pro", path=p)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["agent-default-model"]["reasoningEffort"] == "high"
    # and the rest of the document is untouched
    assert data["llm-pi-ai"]["providers"]["buildpc"]["displayName"] == "Local LLM server"


def test_set_default_model_appends_when_block_absent(tmp_path):
    p = _write_settings(tmp_path, "# just a comment\nllm-deepseek:\n  reasoningEffort: high\n")
    harness.set_default_model("minimax", "MiniMax-M3", path=p)
    out = p.read_text(encoding="utf-8")
    assert "# just a comment" in out
    data = yaml.safe_load(out)
    assert data["agent-default-model"] == {"provider": "minimax", "model": "MiniMax-M3"}
    assert data["llm-deepseek"]["reasoningEffort"] == "high"


def test_set_default_model_is_0600(tmp_path):
    p = _write_settings(tmp_path)
    harness.set_default_model("minimax", "MiniMax-M3", path=p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_set_default_model_concurrent_writes_never_corrupt(tmp_path):
    """Concurrent writers may race for last-write, but must not interleave.

    The old implementation took no lock and used a fixed .tmp path. Every
    surviving file must still parse and name one of the models written.
    """
    import threading
    p = _write_settings(tmp_path)
    models = [f"model-{i}" for i in range(12)]
    errors = []

    def worker(m):
        try:
            harness.set_default_model("deepseek-official", m, path=p)
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(m,)) for m in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["agent-default-model"]["model"] in models
    assert data["agent-default-model"]["reasoningEffort"] == "high"
    assert "JoyFox is deliberately absent" in p.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp")), "temp files left behind"


def test_set_default_model_still_validates(tmp_path):
    p = _write_settings(tmp_path)
    with pytest.raises(harness.ValidationError):
        harness.set_default_model("bad provider!", "m", path=p)
    with pytest.raises(harness.ValidationError):
        harness.set_default_model("ok", "bad model!", path=p)
    assert "JoyFox is deliberately absent" in p.read_text(encoding="utf-8")
