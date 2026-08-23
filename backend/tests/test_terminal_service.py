"""Real-PTY unit tests for the terminal session manager. Uses `cat` (an
injectable stand-in for the coding CLI) so spawn → write → echo → stop is exercised
end to end without depending on any CLI being installed. Run: cd backend && uv
run pytest.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from app import terminal
from app.terminal import (
    OptionsValidationError,
    TerminalBusyError,
    TerminalSession,
)

CAT = shutil.which("cat") or "/bin/cat"


async def _wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_spawn_write_echo_stop():
    sess = TerminalSession(binary=CAT)
    seen = bytearray()
    sess.attach(seen.extend)

    st = await sess.start()
    assert st["state"] == "running"
    assert st["pid"] and st["started_at"]

    # cat echoes its input back on the PTY (the tty echo + the program's own
    # copy) — either way our bytes come back.
    sess.write(b"hello-pty\n")
    assert await _wait_for(lambda: b"hello-pty" in bytes(seen)), bytes(seen)

    # Scrollback replays for a late attacher.
    late = bytearray()
    replay = sess.attach(late.extend)
    assert b"hello-pty" in replay

    st = await sess.stop()
    assert st["state"] == "exited"
    assert st["exit_code"] is not None


async def test_restart_gives_fresh_scrollback():
    sess = TerminalSession(binary=CAT)
    sess.attach(lambda d: None)
    await sess.start()
    sess.write(b"first\n")
    assert await _wait_for(lambda: b"first" in bytes(sess._scrollback))
    await sess.restart()
    # Fresh process → scrollback cleared of the old output.
    assert b"first" not in bytes(sess._scrollback)
    assert sess.status()["state"] == "running"
    await sess.stop()


async def test_state_hook_fires_on_transitions():
    sess = TerminalSession(binary=CAT)
    states = []
    sess.add_state_hook(lambda s: states.append(s["state"]))
    await sess.start()
    await sess.stop()
    assert "running" in states
    assert "exited" in states


async def test_control_ops_are_exclusive():
    sess = TerminalSession(binary=CAT)

    async def _hold():
        async with sess._exclusive():
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0.05)
    with pytest.raises(TerminalBusyError):
        await sess.start()
    await holder


async def test_write_when_stopped_raises():
    sess = TerminalSession(binary=CAT)
    from app.terminal import TerminalError
    with pytest.raises(TerminalError):
        sess.write(b"x")


async def test_exit_is_reaped_without_respawn():
    # `false` exits immediately; the reap task must record exited + exit_code
    # and never respawn.
    sess = TerminalSession(binary=shutil.which("false") or "/bin/false")
    sess.attach(lambda d: None)
    await sess.start()
    assert await _wait_for(lambda: sess.status()["state"] == "exited")
    assert sess.status()["state"] == "exited"
    assert sess.status()["exit_code"] not in (None, 0)


def test_argv_reflects_options():
    sess = TerminalSession(binary="codecli")
    assert sess._argv("codecli") == ["codecli"]
    sess.set_options(yolo=True)
    assert sess._argv("codecli") == ["codecli", "--yolo"]
    sess.set_options(model="deepseek-pro/deepseek-v4-pro")
    assert sess._argv("codecli") == ["codecli", "--yolo", "--model", "deepseek-pro/deepseek-v4-pro"]
    sess.set_options(yolo=False, model=None)
    assert sess._argv("codecli") == ["codecli"]


def test_argv_reflects_resume_mode():
    # resume flag comes first, then yolo, then model — a fixed flag per mode.
    sess = TerminalSession(binary="codecli")
    assert sess._argv("codecli") == ["codecli"]        # default 'none'
    sess.set_options(resume="continue")
    assert sess._argv("codecli") == ["codecli", "-c"]
    sess.set_options(resume="resume")
    assert sess._argv("codecli") == ["codecli", "--resume"]
    sess.set_options(resume="copy")
    assert sess._argv("codecli") == ["codecli", "--copy"]
    # combined with yolo + model, and ordering is resume > yolo > model.
    sess.set_options(resume="continue", yolo=True, model="mimo-pro")
    assert sess._argv("codecli") == ["codecli", "-c", "--yolo", "--model", "mimo-pro"]
    # clear back to a fresh session.
    sess.set_options(resume=None)
    assert sess._argv("codecli") == ["codecli", "--yolo", "--model", "mimo-pro"]


def test_set_options_validation():
    sess = TerminalSession(binary="codecli")
    with pytest.raises(OptionsValidationError):
        sess.set_options(model="bad model!")
    with pytest.raises(OptionsValidationError):
        sess.set_options(yolo="nope")
    with pytest.raises(OptionsValidationError):
        sess.set_options(resume="bogus")
    with pytest.raises(OptionsValidationError):
        sess.set_options(resume=42)
    # model/resume unchanged by an omitted arg vs cleared by explicit None.
    sess.set_options(model="mimo-pro", resume="copy")
    sess.set_options(yolo=True)
    assert sess.get_options()["model"] == "mimo-pro"
    assert sess.get_options()["resume"] == "copy"


async def test_yolo_option_applied_at_spawn():
    # `env` prints its argv-independent environment; use `printenv`-free check:
    # spawn `echo` so argv is observable via the reap being clean. Simpler: use
    # a wrapper that echoes its args. `sh -c 'echo "$@"' _ ...` isn't our argv
    # shape, so assert via _active_options instead (records the real spawn set).
    sess = TerminalSession(binary=shutil.which("true") or "/bin/true")
    sess.attach(lambda d: None)
    sess.set_options(yolo=True, model="mimo-pro")
    await sess.start()
    assert sess.status()["options"] == {"yolo": True, "model": "mimo-pro", "resume": "none"}
    assert await _wait_for(lambda: sess.status()["state"] == "exited")


# --------------------------------------------------------------------------- #
# Group sweeps must not killpg a recycled pid
# --------------------------------------------------------------------------- #


def test_sweep_group_uses_killpg_only_while_the_leader_lives(monkeypatch):
    """A blanket killpg is safe only until the leader is reaped; afterwards the
    pid (and so the group id) can belong to somebody else, so the sweep names
    the group's current members instead."""
    import signal as _signal

    class _Proc:
        pid = 424242
        returncode = None

    proc = _Proc()
    killpg: list[tuple] = []
    monkeypatch.setattr(terminal.os, "killpg",
                        lambda pid, sig: killpg.append((pid, sig)))

    terminal.TerminalSession._sweep_group(proc, _signal.SIGKILL)
    assert killpg == [(424242, _signal.SIGKILL)]

    # Reaped: no killpg at all, only the members we can still see in the group.
    killpg.clear()
    proc.returncode = 0
    killed: list[tuple] = []
    monkeypatch.setattr(terminal, "_group_members", lambda pgid: [9001, 9002])
    monkeypatch.setattr(terminal.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    terminal.TerminalSession._sweep_group(proc, _signal.SIGKILL)
    assert killpg == [], "killpg fired on a pid that is no longer ours"
    assert killed == [(9001, _signal.SIGKILL), (9002, _signal.SIGKILL)]


def test_group_members_never_reports_our_own_process():
    import os as _os
    assert _os.getpid() not in terminal._group_members(_os.getpgid(0))
