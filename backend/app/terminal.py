"""Coding terminal: a single server-side PTY session running an interactive
coding CLI, mirrored to any number of attached WebSocket clients.

This module stays pure/testable (stdlib pty +
asyncio, no FastAPI here); routes in main.py compose these primitives, own the
HTTP error mapping and the WS broadcasts.

Command whitelist (non-negotiable): the only binary ever spawned is the one
named by DISPATCH_TERMINAL_BIN, resolved once at import. There is no default —
an unset variable leaves the feature off rather than guessing at a binary. argv
is exactly [binary] — no route parameter ever reaches argv; client input only
travels through the PTY master fd as terminal keystrokes.

The session is in-memory only: it dies with the DisPatch process (every restart
is a fresh session) and is never auto-respawned — Start/Restart are the only
paths that spawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import shutil
import signal
import struct
import termios
import time
from collections.abc import Callable
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

log = logging.getLogger("local-chat.terminal")


def _group_members(pgid: int) -> list[int]:
    """Pids currently in process group ``pgid`` (Linux /proc; [] elsewhere).

    Used instead of a blanket ``killpg`` once the group LEADER has been
    reaped: at that moment its pid can be recycled, and killpg on a recycled
    pid would signal a stranger's whole process group. Naming the survivors
    individually keeps the sweep to processes that are demonstrably in our
    group right now.
    """
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out                      # not Linux — caller falls back
    me = os.getpid()
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            if os.getpgid(pid) == pgid:
                out.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return out


# The coding CLI to spawn: a bare name resolved on PATH, or an absolute path.
# Deliberately no default — a hardcoded fallback would both guess at somebody
# else's install layout and bake one vendor's binary name into the product.
TERMINAL_BIN = os.environ.get("DISPATCH_TERMINAL_BIN", "").strip()

# Optional TOML the model picker reads, newest-wins, for a CLI that keeps its
# providers in a config file. Colon-separated, like PATH. Unset = no picker.
TERMINAL_CONFIG_PATHS = [
    Path(p).expanduser()
    for p in os.environ.get("DISPATCH_TERMINAL_CONFIG", "").split(":")
    if p.strip()
]

SCROLLBACK_MAX = 256 * 1024   # bytes of raw output replayed on (re)attach
_STOP_GRACE = 5.0             # seconds between SIGTERM and SIGKILL
_READ_CHUNK = 65536

# A model name lands in argv (never a shell) — keep it a tight token anyway.
# \A..\Z (not ^..$) so a trailing newline can't slip through, and no leading '-'
# so a value can't masquerade as a flag.
_MODEL_RE = re.compile(r"\A(?!-)[A-Za-z0-9._/-]{1,64}\Z")

# Resume mode -> the spawn flag it maps to. `none` starts a fresh session; the
# rest are the CLI's own interactive session controls:
#   continue (-c)   resume the latest session
#   resume (--resume)  the CLI shows an in-TUI picker to choose one
#   copy (--copy)   continue the latest session in a duplicate branch
# Validated to this exact set; the value only ever reaches argv as a fixed flag
# (never the raw string), so it can't masquerade as anything else.
_RESUME_FLAGS = {
    "none": [],
    "continue": ["-c"],
    "resume": ["--resume"],
    "copy": ["--copy"],
}


class TerminalError(Exception):
    """A terminal op failed. str(e) is safe to show the caller (no tracebacks)."""


class TerminalBusyError(TerminalError):
    """Another control op is already in flight."""

    def __init__(self):
        super().__init__("operation in progress")


class OptionsValidationError(TerminalError):
    """An incoming spawn-option payload failed validation."""


def _resolve_binary() -> str:
    """The configured CLI, as an absolute path. Raises if none is configured or
    it is not on PATH — the caller turns that into a 'not configured' state
    rather than spawning something unexpected."""
    if not TERMINAL_BIN:
        raise TerminalError(
            "no coding CLI configured — set DISPATCH_TERMINAL_BIN to its name or path")
    resolved = shutil.which(TERMINAL_BIN) if os.sep not in TERMINAL_BIN else TERMINAL_BIN
    if not resolved or not os.access(resolved, os.X_OK):
        raise TerminalError(f"configured coding CLI is not executable: {TERMINAL_BIN}")
    return resolved


def discover_models() -> dict:
    """Best-effort parse of the CLI's config for the model picker. Returns
    {models: [...], current_default: str|None}; empty list if unreadable.

    Model names are collected from every [[providers]].models entry (both the
    plain name and the provider-qualified `provider/model` form, since
    default_model uses the qualified form). Never raises — a malformed config
    just yields an empty list."""
    models: list[str] = []
    current_default: str | None = None
    if tomllib is None:
        return {"models": [], "current_default": None}
    for path in TERMINAL_CONFIG_PATHS:
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            continue
        if current_default is None and isinstance(data.get("default_model"), str):
            current_default = data["default_model"]
        for prov in (data.get("providers") or []):
            if not isinstance(prov, dict):
                continue
            pname = prov.get("name")
            for m in (prov.get("models") or []):
                if not isinstance(m, str):
                    continue
                if m not in models:
                    models.append(m)
                if isinstance(pname, str):
                    q = f"{pname}/{m}"
                    if q not in models:
                        models.append(q)
    return {"models": models, "current_default": current_default}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The service runs with TMPDIR pointed at the data dir (20-tmpdir.conf) so
    # UPLOAD spooling lands on real disk — that's an app concern, not the
    # terminal's. Inherited by the PTY child it makes every build/test tool
    # (node compile cache, pytest tmpdirs, …) dump byproducts into the data
    # dir, which then grow unbounded (342MB / 13.8k files by 2026-08-01).
    # Children get the system default temp dir instead.
    env.pop("TMPDIR", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Extra PATH entries for the spawned CLI, so it can find its own sibling
    # tools when they live outside the service's PATH. Colon-separated, appended
    # in order, duplicates skipped. Unset = inherit the service PATH unchanged.
    extra = [e for e in os.environ.get("DISPATCH_TERMINAL_PATH", "").split(":") if e.strip()]
    if extra:
        path = env.get("PATH", "")
        seen = path.split(":") if path else []
        for entry in extra:
            if entry not in seen:
                seen.append(entry)
        env["PATH"] = ":".join(x for x in seen if x)
    return env


def _set_ctty():
    # Child-side (after setsid via start_new_session): make the PTY slave on
    # stdin the controlling terminal so line-discipline signals (Ctrl-C etc.)
    # reach the foreground process group.
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class TerminalSession:
    """One PTY-backed child process, N mirrored viewers.

    Output callbacks are plain sync callables invoked on the event loop thread
    (the master fd is drained via loop.add_reader) — attachers that need to do
    async work should enqueue, never block. State hooks fire on every
    running/stopped/exited transition with the status() dict.
    """

    def __init__(self, binary: str | None = None):
        self._binary = binary                 # injectable for tests; None = configured CLI
        self._proc: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None
        self._state = "stopped"               # 'stopped' | 'running' | 'exited'
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._scrollback = bytearray()
        self._outputs: list[Callable[[bytes], None]] = []
        self._state_hooks: list[Callable[[dict], None]] = []
        self._lock = asyncio.Lock()
        self._reap_task: asyncio.Task | None = None
        # Last requested viewport. A fresh PTY has a 0x0 winsize; a TUI that
        # initializes against 0 columns can wedge unrecoverably, so every
        # spawn presets the PTY to the last known (or a sane default) size
        # BEFORE the child starts. Clients still resize() to their real dims.
        self._winsize = (80, 24)              # (cols, rows)
        # Spawn-time options, applied on the NEXT start/restart. In-memory only
        # (reset on reboot, like the session itself). _active_options records
        # what the currently running process was actually spawned with, so the
        # UI can flag "restart to apply" when they diverge.
        self._options = {"yolo": False, "model": None, "resume": "none"}
        self._active_options: dict | None = None

    # ------------------------------------------------------------------ #
    # Exclusive control-op lock — concurrent ops get a 409, not a queue
    # ------------------------------------------------------------------ #

    @contextlib.asynccontextmanager
    async def _exclusive(self):
        if self._lock.locked():
            raise TerminalBusyError()
        async with self._lock:
            yield

    # ------------------------------------------------------------------ #
    # Status / attach
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        return {
            "state": self._state,
            "pid": self._proc.pid if (self._proc and self._state == "running") else None,
            "started_at": self._started_at,
            "exit_code": self._exit_code,
            "options": dict(self._options),
            "pending_options": self._options_pending(),
        }

    def _options_pending(self) -> bool:
        """True when a running process was spawned with options that differ
        from the currently-set ones (so a restart would change behaviour).
        Only meaningful while running; a stopped/exited session applies the
        current options on its next start, so nothing is 'pending'."""
        if self._state != "running" or self._active_options is None:
            return False
        return self._active_options != self._options

    def get_options(self) -> dict:
        return dict(self._options)

    def set_options(self, yolo: bool | None = None, model=..., resume=...) -> dict:
        """Update spawn options (applied on next start/restart). `model`/`resume`
        use an `...` sentinel so an explicit None means 'clear back to the
        default' while an omitted arg leaves the value unchanged."""
        if yolo is not None:
            if not isinstance(yolo, bool):
                raise OptionsValidationError("yolo must be a boolean")
            self._options["yolo"] = yolo
        if model is not ...:
            if model in (None, ""):
                self._options["model"] = None
            elif isinstance(model, str) and _MODEL_RE.match(model):
                self._options["model"] = model
            else:
                raise OptionsValidationError("model must match ^[A-Za-z0-9._/-]{1,64}$")
        if resume is not ...:
            if resume in (None, ""):
                self._options["resume"] = "none"
            elif isinstance(resume, str) and resume in _RESUME_FLAGS:
                self._options["resume"] = resume
            else:
                raise OptionsValidationError(
                    "resume must be one of: " + ", ".join(_RESUME_FLAGS))
        return dict(self._options)

    def _argv(self, binary: str) -> list[str]:
        argv = [binary]
        argv += _RESUME_FLAGS.get(self._options.get("resume") or "none", [])
        if self._options.get("yolo"):
            argv.append("--yolo")
        model = self._options.get("model")
        if model:
            argv += ["--model", model]
        return argv

    def attach(self, callback: Callable[[bytes], None]) -> bytes:
        """Register an output mirror; returns the scrollback to replay first."""
        if callback not in self._outputs:
            self._outputs.append(callback)
        return bytes(self._scrollback)

    def detach(self, callback: Callable[[bytes], None]) -> None:
        with contextlib.suppress(ValueError):
            self._outputs.remove(callback)

    def add_state_hook(self, hook: Callable[[dict], None]) -> None:
        if hook not in self._state_hooks:   # idempotent: lifespan may re-run in tests
            self._state_hooks.append(hook)

    def remove_state_hook(self, hook: Callable[[dict], None]) -> None:
        with contextlib.suppress(ValueError):
            self._state_hooks.remove(hook)

    def _notify_state(self) -> None:
        st = self.status()
        for hook in list(self._state_hooks):
            try:
                hook(st)
            except Exception:
                log.exception("terminal state hook failed")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> dict:
        async with self._exclusive():
            if self._state != "running":
                await self._spawn()
        return self.status()

    async def stop(self) -> dict:
        async with self._exclusive():
            await self._do_stop()
        return self.status()

    async def restart(self) -> dict:
        async with self._exclusive():
            await self._do_stop()
            await self._spawn()
        return self.status()

    async def shutdown(self) -> None:
        """Best-effort teardown for lifespan exit (no lock, no broadcasts)."""
        self._state_hooks.clear()
        self._outputs.clear()
        with contextlib.suppress(Exception):
            await self._do_stop()

    async def _spawn(self) -> None:
        binary = self._binary or _resolve_binary()
        master, slave = pty.openpty()
        # Preset the winsize before exec — never let the child see 0x0.
        cols, rows = self._winsize
        with contextlib.suppress(OSError):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        argv = self._argv(binary)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave, stdout=slave, stderr=slave,
                cwd=str(Path.home()), env=_child_env(),
                start_new_session=True, preexec_fn=_set_ctty,
            )
        except OSError as e:
            os.close(master)
            os.close(slave)
            raise TerminalError(f"failed to launch {os.path.basename(binary)}: {e}") from e
        os.close(slave)
        os.set_blocking(master, False)
        self._proc = proc
        self._master_fd = master
        self._state = "running"
        self._started_at = time.time()
        self._exit_code = None
        self._active_options = dict(self._options)   # what THIS process ran with
        self._scrollback.clear()   # fresh process, fresh screen
        asyncio.get_running_loop().add_reader(master, self._on_readable)
        self._reap_task = asyncio.get_running_loop().create_task(self._reap(proc))
        log.info("terminal session started: %s (pid %s)", binary, proc.pid)
        self._notify_state()

    async def _do_stop(self) -> None:
        proc = self._proc
        if proc is None or self._state != "running":
            return
        # Signal the whole process group, not just the direct child: the CLI
        # is a node wrapper that execs a native binary; killing only the
        # wrapper orphans that child (start_new_session makes proc the group
        # leader, so killpg reaches every descendant in the group).
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE)
        except TimeoutError:
            self._signal_group(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
        # The reap task owns the running→exited transition (state, exit_code,
        # fd cleanup, notifications) — wait for it so callers observe the
        # final state, not a half-stopped one.
        if self._reap_task is not None:
            with contextlib.suppress(Exception):
                await self._reap_task
        # Final sweep: a native CLI child may ignore SIGTERM, and the
        # wrapper exiting promptly means the grace timeout above never
        # escalates. The wrapper has been REAPED by now, so its pid — and
        # therefore the group id — is no longer reliably ours: sweep the
        # group's current members by pid rather than killpg'ing the number.
        self._sweep_group(proc, signal.SIGKILL)

    @staticmethod
    def _sweep_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal our group's survivors AFTER the leader has been reaped.

        A blanket ``killpg`` is only safe while the leader is unreaped — once
        it is gone its pid can be recycled, and the group id would then point
        at somebody else. This names the pids that are in the group at this
        instant instead.
        """
        if proc.returncode is None:          # leader still ours: group id is safe
            TerminalSession._signal_group(proc, sig)
            return
        for pid in _group_members(proc.pid):
            with contextlib.suppress(OSError):
                os.kill(pid, sig)

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        # start_new_session=True makes proc the leader of its own process
        # group, so pgid == proc.pid — valid for killpg even after the leader
        # itself has been reaped (surviving members keep the group alive).
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass                            # whole group already gone
        except (PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if self._proc is not proc:      # superseded by a restart
            return
        self._exit_code = code
        self._state = "exited"
        self._close_master()
        log.info("terminal session exited (code %s)", code)
        # No auto-respawn, ever: the operator restarts explicitly.
        self._notify_state()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #

    def _on_readable(self) -> None:
        fd = self._master_fd
        if fd is None:
            return
        try:
            data = os.read(fd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""      # EIO: slave side gone — the reap task finishes up
        if not data:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(fd)
            return
        self._scrollback.extend(data)
        if len(self._scrollback) > SCROLLBACK_MAX:
            del self._scrollback[: len(self._scrollback) - SCROLLBACK_MAX]
        for cb in list(self._outputs):
            try:
                cb(data)
            except Exception:
                log.exception("terminal output callback failed")

    def write(self, data: bytes) -> None:
        if self._master_fd is None or self._state != "running":
            raise TerminalError("session is not running")
        try:
            os.write(self._master_fd, data)
        except BlockingIOError:
            pass    # PTY input buffer full — drop rather than block the loop
        except OSError as e:
            raise TerminalError(f"write failed: {e}") from e

    def resize(self, cols: int, rows: int) -> None:
        cols = max(2, min(int(cols), 500))
        rows = max(2, min(int(rows), 300))
        self._winsize = (cols, rows)   # remembered across restarts
        if self._master_fd is None:
            return
        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsz)

    def _close_master(self) -> None:
        fd = self._master_fd
        if fd is None:
            return
        self._master_fd = None
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


# The one live session (in-memory only; dies with the process).
session = TerminalSession()
