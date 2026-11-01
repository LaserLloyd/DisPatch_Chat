"""4.3 (#13): the generic CLI adapter supervises a real subprocess (a fake CLI, never a model).

(a) a run of the fake CLI reaches ``succeeded`` with text + tool events, through the SAME
    normalizer as the Claude Agent; cancel kills the whole process group (grandchild included).
(g) only a SAVED preset runs: a free-form command in the agent spec, or an unknown preset id,
    is refused before anything starts; a preset must name an absolute executable.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from app.agents.cli_adapter import GenericCliBackend, cli_to_wire, validate_presets
from app.agents.events import ErrorCode
from app.agents.interface import AgentBackendError, AgentSpec, TaskContext
from app.agents.secrets import MemorySecretProvider

pytestmark = pytest.mark.timeout(60)

FAKE = Path(__file__).parent / "fixtures" / "fake_agent_cli.py"


def preset(**over):
    p = {"id": "fake", "label": "Fake CLI", "kind": "generic", "executable": sys.executable,
         "args": ["-I", str(FAKE)]}
    p.update(over)
    return p


def backend(presets=None):
    be = GenericCliBackend(presets=lambda: presets if presets is not None else [preset()])
    be.INTERRUPT_GRACE_S = 0.3
    be.KILL_GRACE_S = 1.0
    return be


def spec(**opts):
    return AgentSpec(backend="cli", tool_allowlist=["Read"],
                     backend_options={"preset": "fake", **opts})


async def drain(be, tid, timeout=30.0):
    out = []

    async def go():
        async for ev in be.events(tid):
            out.append(ev)
            if ev.type == "result":
                return
    await asyncio.wait_for(go(), timeout)
    return out


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


async def test_a_the_fake_cli_runs_to_completion_with_text_and_tool_events(tmp_path):
    be = backend()
    tid = await be.start_task(spec(), "scenario:ok", TaskContext(data_dir=tmp_path, task_id="t_cli_ok",
                                                                 secrets=MemorySecretProvider()))
    evs = await drain(be, tid)
    types = [e.type for e in evs]
    assert types[0] == "status" and "text_delta" in types
    call = next(e for e in evs if e.type == "tool_call")
    assert (call.name, call.call_id, call.input) == ("Read", "tu_1", {"file_path": "README.md"})
    res = next(e for e in evs if e.type == "tool_result")
    assert res.call_id == "tu_1" and res.ok and "hello from the file" in res.output
    final = evs[-1]
    assert final.state.value == "succeeded" and final.text == "The file says hello."
    assert final.session_id is None and final.resumable is False, \
        "a generic preset without resume_args is not resumable"
    info = be.task_info(tid)
    assert info.cost_usd == pytest.approx(0.0123) and info.tokens == 160
    # streamed deltas are not doubled by the final assistant block
    text = "".join(e.text for e in evs if e.type == "text_delta")
    assert text.count("Let me look.") == 1
    await be.aclose()


async def test_a_a_preset_with_resume_args_continues_the_session(tmp_path):
    be = backend(presets=[preset(resume_args=["--resume", "{session_id}"])])
    tid = await be.start_task(spec(), "first", TaskContext(data_dir=tmp_path, task_id="t_cli_res"))
    evs = await drain(be, tid)
    assert evs[-1].resumable is True and evs[-1].session_id == "cli-sess-1"
    after = evs[-1].seq
    await be.resume(tid, "second")
    evs2 = []

    async def go():
        async for ev in be.events(tid, after_seq=after):
            evs2.append(ev)
            if ev.type == "result":
                return
    await asyncio.wait_for(go(), 30)
    assert evs2[-1].state.value == "succeeded" and evs2[-1].text == "echo: second"
    await be.aclose()


async def test_a_the_prompt_goes_over_stdin_not_argv(tmp_path):
    be = backend()
    tid = await be.start_task(spec(), "hello there", TaskContext(data_dir=tmp_path, task_id="t_cli_echo"))
    evs = await drain(be, tid)
    assert evs[-1].text == "echo: hello there"
    await be.aclose()


async def test_a_an_exit_without_a_result_fails_with_the_stderr_tail(tmp_path):
    be = backend()
    tid = await be.start_task(spec(), "scenario:noresult", TaskContext(data_dir=tmp_path, task_id="t_cli_bad"))
    evs = await drain(be, tid)
    err = next(e for e in evs if e.type == "error")
    assert err.code == ErrorCode.backend_error and "exit code 3" in err.message
    assert "something broke" in (err.detail or "")
    assert evs[-1].state.value == "failed"
    await be.aclose()


async def test_a_cancel_kills_the_whole_process_group(tmp_path):
    be = backend()
    tid = await be.start_task(spec(), "scenario:hang", TaskContext(data_dir=tmp_path, task_id="t_cli_hang"))
    pids_file = tmp_path / "agents" / tid / "home" / "pids.txt"
    end = time.monotonic() + 20
    while not pids_file.exists() or not pids_file.read_text().strip():
        assert time.monotonic() < end, "the fake CLI never started"
        await asyncio.sleep(0.05)
    leader, grandchild = (int(x) for x in pids_file.read_text().split())
    assert alive(leader) and alive(grandchild)
    await be.cancel(tid)
    end = time.monotonic() + 10
    while (alive(leader) or alive(grandchild)) and time.monotonic() < end:
        await asyncio.sleep(0.05)
    assert not alive(leader), "the CLI itself survived cancel"
    assert not alive(grandchild), "a process the CLI started survived cancel (group not killed)"
    info = be.task_info(tid)
    assert info.state.value == "cancelled"
    await be.aclose()


async def test_g_a_command_in_the_agent_spec_is_refused(tmp_path):
    be = backend()
    for bad in ({"argv": ["/bin/sh", "-c", "id"]}, {"executable": "/bin/sh"}, {"command": "rm -rf /"},
                {"args": ["-c", "id"]}):
        with pytest.raises(AgentBackendError) as ei:
            await be.start_task(spec(**bad), "x", TaskContext(data_dir=tmp_path))
        assert ei.value.code == ErrorCode.policy_violation
    assert not list((tmp_path / "agents").glob("*")) if (tmp_path / "agents").exists() else True


async def test_g_an_unsaved_preset_is_refused(tmp_path):
    be = backend(presets=[preset(id="other")])
    with pytest.raises(AgentBackendError) as ei:
        await be.start_task(spec(), "x", TaskContext(data_dir=tmp_path))
    assert ei.value.code == ErrorCode.policy_violation
    be2 = backend(presets=[])
    with pytest.raises(AgentBackendError):
        await be2.start_task(spec(), "x", TaskContext(data_dir=tmp_path))


def test_g_presets_need_an_absolute_executable_and_safe_fields(tmp_path):
    ok = validate_presets([preset()])
    assert ok[0]["executable"] == sys.executable
    for bad in (preset(executable="python3"), preset(executable=str(tmp_path / "missing")),
                preset(id="Bad Id"), preset(kind="shell"), preset(args="-c id"),
                preset(env_secrets={"PATH": "x"}), preset(env_secrets={"LD_PRELOAD": "x"}),
                preset(kind="claude"), preset(resume_args=["--resume"]), {**preset(), "shell": True}):
        with pytest.raises(ValueError):
            validate_presets([bad])
    with pytest.raises(ValueError):
        validate_presets([preset(), preset()])                    # duplicate id


def test_the_stream_json_maps_onto_the_sdk_wire_shapes():
    assert cli_to_wire({"type": "system", "subtype": "init", "session_id": "s",
                        "mcp_servers": [{"name": "x", "status": "failed"}]}) == {
        "kind": "system", "subtype": "init",
        "data": {"session_id": "s", "mcp_server_errors": [{"name": "x", "status": "failed"}],
                 "plugin_errors": []}}
    assert cli_to_wire({"type": "result", "session_id": "s", "result": "r"}, keep_session=False) == {
        "kind": "result", "result": "r"}
    assert cli_to_wire({"type": "something_new"}) is None
