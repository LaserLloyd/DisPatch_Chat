"""DeepSeek Harness (`dsh`) sessions: several runs at once, watchable while
they go, stoppable — and gone when you stop them.

The pane's Jobs tab (`harness.JobRunner`) runs ONE task at a time and only
surfaces the final answer once the process has ended. This module is the other
half: concurrent runs, each visible while it is still working.

Three facts about dsh shape the design (verified by hand against 0.1.5-rc.1 on
this box, 2026-09-16):

  * `--profile headless` writes its progress to a session log under
    ``$DSH_HOME/sessions/<cwd-slug>/session-<uuid>/`` and it is written
    INCREMENTALLY. That file is the only live signal there is: stdout stays
    silent until the turn ends. Measured while a `sleep 30` tool call ran:
    11,727 → 14,731 → 17,198 bytes, each poll decoding to valid JSONL.
  * The log's FILENAME is version-dependent — ``session.jsonl.zstd`` on
    0.1.1-rc.2, ``session.v3.jsonl.zstd`` (plus ``session.lock``) on
    0.1.5-rc.1. Discovery therefore globs ``*.jsonl.zstd`` and never hardcodes
    the name, or an `dsh` upgrade silently reports every session as log-less.
  * There is no resume and no mid-flight steering. A session is exactly one
    process, so stopping it is the only correction primitive — which is why a
    stopped session is REMOVED rather than kept as history.

Each session gets its own scratch ``DSH_HOME``. That buys two things: a
per-session model can be set without ever writing the shared
``~/.dsh/settings.yaml`` (the file's writer is a lock-free read-modify-write
that strips its comments — see the `dsh` skill), and log discovery becomes
exact, because the scratch home holds exactly one session. The scratch home is
a COPY of the shared settings (so every provider route comes with it) with only
``agent-default-model`` replaced, plus a SYMLINK to the shared credentials
file — the secret is never copied, read, or logged.

Command whitelist stays as strict as `harness.py`: the only binaries ever
invoked are the resolved `dsh` binary and `zstd` (for decoding the log). The
task is a single argv element, never a shell string.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, harness

log = logging.getLogger("local-chat.harness_sessions")


class SessionNotFound(harness.HarnessError):
    """A session id that is not (or is no longer) in the live list. Mapped to
    404 by the route layer — an unknown id is not a gateway failure."""

# How many sessions may be in flight at once. The dsh skill is explicit that
# concurrency is fine as long as each run has its own home and working copy;
# the ceiling here is about a phone-sized UI and about not filling the box with
# node processes, not about dsh.
SESSION_MAX = 4
# Finished sessions stay readable (the final answer is in the event stream)
# until dismissed, but the list is not a history: prune the oldest beyond this.
FINISHED_KEEP = 20
# Projected events kept per session. The raw log stays on disk until the
# session is forgotten; this bounds what a reconnect has to ship.
EVENT_MAX = 500
# Log poll. The writer flushes per event, so 1 s is well inside "feels live".
TAIL_INTERVAL = 1.0
# SIGTERM, then this long before SIGKILL. Headless dsh is a node process and
# exits on SIGTERM in about a second, so this is a backstop, not the norm.
STOP_GRACE = 5.0
# A single decode is bounded; a session log is JSONL, not a video.
_DECODE_TIMEOUT = 15.0

_ZSTD = shutil.which("zstd") or "/usr/bin/zstd"


def _zstd_cat(path: Path) -> bytes:
    """Decode a (possibly still-being-written) session log.

    A partial file is the normal case, not the exception — that is the whole
    point of reading it mid-run — so the decoded stdout is used even when zstd
    exits non-zero. stdout carries every frame it managed to read; treating a
    non-zero exit as "no data" would make a live session look empty.
    """
    try:
        proc = subprocess.run([_ZSTD, "-dc", str(path)], capture_output=True,
                              timeout=_DECODE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("session log decode failed for %s: %s", path, exc)
        return b""
    return proc.stdout or b""


def _text_of(content) -> str:
    """Join the text blocks of a dsh message `content` array."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p)


def _clip(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _strip_reasoning(text: str) -> str:
    """Drop `<think>…</think>` blocks.

    A model whose reasoning leaks into the text block (rather than arriving as
    its own `reasoning` block) would otherwise put its scratchpad in the live
    view. Mirrors what the gateway's own text scrubber does upstream.
    """
    if "<think" not in text:
        return text
    return _THINK_RE.sub("", text).strip()


_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def project(raw: str) -> dict | None:
    """One raw dsh event → one compact, renderable item (or None to ignore).

    The raw stream is mostly machinery — `request/header` carries the whole
    system prompt, `reasoning-chunks` is a delta array, `assistant/chunk` is
    token-by-token. A live view that shipped those would be unreadable and
    would flood the socket, so only the events that answer "what is it doing"
    survive.
    """
    try:
        ev = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    kind = ev.get("type")
    data = ev.get("data")
    if not isinstance(data, dict):
        data = {}

    if kind == "session/title":
        title = _strip_reasoning(_clip(data.get("title") or "", 200)).strip()
        # dsh emits the fallback title first and an LLM title second; the LLM
        # one sometimes arrives with its <think> block still attached. An empty
        # or reasoning-only title is dropped rather than shown.
        if not title or title.startswith("<think"):
            return None
        return {"k": "title", "text": title}
    if kind == "user/message":
        # Not projected: the card already shows the task, and dsh injects its
        # own scaffolding here (a "Current runtime context" block that is
        # hundreds of lines the operator never wrote).
        return None
    if kind == "request/context":
        return {"k": "model", "provider": data.get("provider"),
                "model": data.get("model")}
    if kind == "turn/start":
        return {"k": "turn", "turn": data.get("turn")}
    if kind == "step/start":
        return {"k": "step", "turn": data.get("turn"), "step": data.get("step")}
    if kind == "tool/call":
        return {"k": "tool", "name": str(data.get("name") or "tool"),
                "args": _clip(data.get("arguments") or "", 600)}
    if kind == "tool/result":
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        first = content[0] if isinstance(content, list) and content else {}
        if not isinstance(first, dict):
            first = {}
        return {"k": "result",
                "ok": not bool(first.get("isError") or first.get("error")),
                "text": _clip(_text_of(first.get("content")), 600)}
    if kind == "assistant/message":
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        text = _strip_reasoning(_text_of(content))
        if not text.strip():
            return None                      # reasoning-only step; nothing to show
        return {"k": "say", "text": _clip(text, 4000)}
    if kind == "turn/end":
        reason = data.get("reason") or {}
        return {"k": "end",
                "reason": str((reason or {}).get("kind") or "ended")}
    return None


@dataclass
class DshSession:
    """One `dsh --profile headless` run, plus everything we know about it."""

    id: str
    task: str
    cwd: str
    home: Path
    provider: str | None = None
    model: str | None = None
    state: str = "starting"       # starting|running|stopping|exited|failed
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    exit_code: int | None = None
    pid: int | None = None
    title: str | None = None
    log_path: str | None = None
    error: str = ""
    events: list[dict] = field(default_factory=list)
    # internal bookkeeping — not part of the wire shape
    _decoded: int = 0
    _size: int = -1

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "stopping")

    def summary(self, after: int | None = None) -> dict:
        out = {
            "id": self.id,
            "task": self.task,
            "cwd": self.cwd,
            "state": self.state,
            "active": self.active,
            "provider": self.provider,
            "model": self.model,
            "title": self.title or _clip(self.task.splitlines()[0] if self.task else "", 80),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round((self.ended_at or time.time()) - self.started_at, 1),
            "error": self.error,
            "has_log": self.log_path is not None,
        }
        if after is not None:
            # `n` is a 1-based index into the projected stream, so a client can
            # hold the highest one it has and ask for exactly what it missed.
            new = [e for e in self.events if e.get("n", 0) > after]
            out["events"] = new
            out["next"] = new[-1]["n"] if new else after
        return out


class SessionRunner:
    """Every live dsh session, in memory. Dies with the process, by design:
    a session is a process, and DisPatch restarting does not resurrect one."""

    def __init__(self, binary: str | None = None, root: Path | None = None):
        self._binary = binary                  # injectable for tests
        self._root = root
        self._sessions: dict[str, DshSession] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._hooks: list = []
        self._lock = asyncio.Lock()

    # -- hooks --------------------------------------------------------------
    def add_state_hook(self, hook) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)

    def remove_state_hook(self, hook) -> None:
        with contextlib.suppress(ValueError):
            self._hooks.remove(hook)

    def _notify(self) -> None:
        st = self.status()
        for h in list(self._hooks):
            try:
                h(st)
            except Exception:
                log.exception("harness session hook failed")

    # -- reads --------------------------------------------------------------
    def status(self) -> dict:
        """The list frame. Ordered newest first, active sessions at the top."""
        items = sorted(self._sessions.values(),
                       key=lambda s: (not s.active, -s.started_at))
        return {
            "sessions": [s.summary() for s in items],
            "running": sum(1 for s in self._sessions.values() if s.active),
            "limit": SESSION_MAX,
        }

    def session(self, sid: str, after: int = 0) -> dict | None:
        s = self._sessions.get(sid)
        return s.summary(after=after) if s is not None else None

    # -- scratch home -------------------------------------------------------
    def _root_dir(self) -> Path:
        base = self._root if self._root is not None else (config.DATA_DIR / "harness-sessions")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _make_home(self, sid: str, provider: str | None, model: str | None) -> Path:
        """A per-session DSH_HOME: shared settings copied, model replaced,
        credentials symlinked.

        Copying rather than writing the shared file is the whole point — the
        shared `settings.yaml` is what the human-facing Model select owns, and
        a round-trip through PyYAML would strip its comments. This copy is
        disposable, so losing comments here is free.
        """
        home = self._root_dir() / sid
        home.mkdir(parents=True, exist_ok=True)
        shared = harness.dsh_home()
        src = shared / "settings.yaml"
        dst = home / "settings.yaml"
        try:
            dst.write_bytes(src.read_bytes()) if src.is_file() else dst.write_text("")
        except OSError as exc:
            log.warning("could not seed scratch settings for session %s: %s", sid, exc)
        if provider and model:
            try:
                data = yaml.safe_load(dst.read_text(encoding="utf-8")) or {}
                if not isinstance(data, dict):
                    data = {}
                data["agent-default-model"] = {"provider": provider, "model": model}
                dst.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            except (OSError, yaml.YAMLError) as exc:
                log.warning("could not pin model for session %s: %s", sid, exc)
        # A symlink, not a copy: the secret stays in one 0600 file and is
        # never read into this process.
        creds = shared / ".credentials.yaml"
        link = home / ".credentials.yaml"
        if creds.is_file() and not link.exists():
            with contextlib.suppress(OSError):
                link.symlink_to(creds)
        return home

    def _forget(self, sid: str) -> None:
        """Take a session out of the visible list.

        Deliberately does NOT cancel the supervisor or delete the scratch home:
        the supervisor already holds its reference, and it is the thing that
        reaps the process and then removes the home. Cancelling it here would
        leave the subprocess transport unclosed, and deleting the home here
        would pull the directory out from under a process still writing to it.
        """
        self._sessions.pop(sid, None)
        self._procs.pop(sid, None)

    def _prune(self) -> None:
        finished = [s for s in self._sessions.values() if not s.active]
        if len(finished) <= FINISHED_KEEP:
            return
        finished.sort(key=lambda s: s.started_at)
        for s in finished[: len(finished) - FINISHED_KEEP]:
            self._forget(s.id)

    # -- lifecycle ----------------------------------------------------------
    async def launch(self, task: str, cwd: Path, model: str | None = None) -> dict:
        binary = self._binary or harness.resolve_binary()
        if binary is None:
            raise harness.HarnessError(
                "dsh is not installed (npm i -g @deepseek-ai/dsh)")
        provider, model_id = split_model(model)
        async with self._lock:
            if sum(1 for s in self._sessions.values() if s.active) >= SESSION_MAX:
                raise harness.HarnessBusyError(
                    f"{SESSION_MAX} sessions are already running — stop one first")
            sid = secrets.token_hex(4)
            home = await asyncio.to_thread(self._make_home, sid, provider, model_id)
            s = DshSession(id=sid, task=task, cwd=str(cwd), home=home,
                           provider=provider, model=model_id)
            self._sessions[sid] = s
            env = dict(os.environ)
            env.pop("TMPDIR", None)          # keep byproducts out of the data dir
            env["DSH_HOME"] = str(home)
            extra = [e for e in os.environ.get("DISPATCH_HARNESS_PATH", "").split(":")
                     if e.strip()]
            if extra:
                seen = (env.get("PATH") or "").split(":")
                for entry in extra:
                    if entry not in seen:
                        seen.append(entry)
                env["PATH"] = ":".join(x for x in seen if x)
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "--profile", "headless", task,
                    cwd=str(cwd), env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                s.state = "failed"
                s.error = f"failed to launch dsh: {exc}"
                s.ended_at = time.time()
                self._notify()
                raise harness.HarnessError(s.error) from exc
            s.pid = proc.pid
            s.state = "running"
            self._procs[sid] = proc
            self._tasks[sid] = asyncio.get_running_loop().create_task(
                self._supervise(s, proc))
        self._prune()
        log.info("harness session %s started (pid %s, cwd %s, model %s)",
                 sid, s.pid, s.cwd, f"{provider}/{model_id}" if provider else "default")
        self._notify()
        return s.summary()

    async def stop(self, sid: str) -> dict:
        s = self._sessions.get(sid)
        if s is None:
            raise SessionNotFound("no such session")
        proc = self._procs.get(sid)
        if s.active and proc is not None:
            s.state = "stopping"
            self._signal(proc, signal.SIGTERM)
            self._escalate(sid, proc)
        # The row disappears the moment it is stopped; the supervisor keeps the
        # reference it already holds and finishes the reaping + cleanup behind
        # us, so a stop is instant from the operator's side.
        self._forget(sid)
        log.info("harness session %s stopped by operator", sid)
        self._notify()
        return {"ok": True, "id": sid}

    async def dismiss(self, sid: str) -> dict:
        s = self._sessions.get(sid)
        if s is None:
            raise SessionNotFound("no such session")
        # Dismiss only ever clears a FINISHED session. Dropping a running one
        # from the list would leave a live process nobody can see or stop —
        # the supervisor would still reap it eventually, but until then the
        # operator has lost the controls for it.
        if s.active:
            raise harness.HarnessBusyError("session is still running — stop it first")
        self._forget(sid)
        self._notify()
        return {"ok": True, "id": sid}

    async def shutdown(self) -> None:
        """Kill every child and wait for its supervisor, so no process and no
        subprocess transport outlives the event loop (a leaked transport
        closes noisily at interpreter exit)."""
        self._hooks.clear()
        for sid, s in list(self._sessions.items()):
            proc = self._procs.get(sid)
            if s.active and proc is not None:
                self._signal(proc, signal.SIGKILL)
        for task in list(self._tasks.values()):
            if task.done():
                continue
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=STOP_GRACE)
        for s in list(self._sessions.values()):
            with contextlib.suppress(OSError):
                shutil.rmtree(s.home, ignore_errors=True)
        self._sessions.clear()
        self._procs.clear()
        self._tasks.clear()

    # -- process handling ---------------------------------------------------
    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Same care as JobRunner: only killpg while the leader is unreaped,
        because after that its pid — and so the group id — can be recycled."""
        harness.JobRunner._sweep_group(proc, sig)

    def _escalate(self, sid: str, proc: asyncio.subprocess.Process) -> None:
        async def _later():
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE)
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(Exception):
                    self._signal(proc, signal.SIGKILL)
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_later())

    async def _supervise(self, s: DshSession,
                         proc: asyncio.subprocess.Process) -> None:
        tail = asyncio.get_running_loop().create_task(self._tail(s))
        try:
            _, err = await asyncio.gather(
                harness.JobRunner._drain(proc.stdout, lambda: None),
                harness.JobRunner._drain(proc.stderr, lambda: None),
            )
            self._signal(proc, signal.SIGKILL)   # sweep any orphaned children
            await proc.wait()
            # The writer flushes per event but the last frame can land a beat
            # after the process exits; one settle pass keeps the final answer
            # from being the one thing the live view misses.
            await asyncio.sleep(0.25)
            await self._read_log(s)
            s.exit_code = proc.returncode
            s.ended_at = time.time()
            if s.state == "stopping":
                s.state = "stopped"
            elif proc.returncode == 0:
                s.state = "exited"
            else:
                s.state = "failed"
                if not s.error:
                    s.error = err.decode("utf-8", "replace").strip()[-2000:]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("harness session %s supervisor failed", s.id)
            s.state = "failed"
            s.ended_at = s.ended_at or time.time()
        finally:
            tail.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await tail
            # Only the runner owns this entry; a stop() already forgot it.
            self._tasks.pop(s.id, None)
            with contextlib.suppress(OSError):
                shutil.rmtree(s.home, ignore_errors=True)
            if self._sessions.get(s.id) is s:
                log.info("harness session %s %s (rc=%s, %.1fs)",
                         s.id, s.state, s.exit_code,
                         (s.ended_at or time.time()) - s.started_at)
                self._notify()

    # -- live log -----------------------------------------------------------
    def _find_log(self, s: DshSession) -> Path | None:
        """The session's log file, whatever this dsh version calls it."""
        try:
            for p in sorted((s.home / "sessions").rglob("*.jsonl.zstd")):
                return p
        except OSError:
            return None
        return None

    async def _read_log(self, s: DshSession) -> bool:
        """Project whatever is new in the log. Returns True if it grew."""
        path = Path(s.log_path) if s.log_path else self._find_log(s)
        if path is None:
            return False
        s.log_path = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == s._size:
            return False
        data = await asyncio.to_thread(_zstd_cat, path)
        s._size = size
        if not data:
            return False
        lines = data.splitlines()
        fresh = lines[s._decoded:]
        if not fresh:
            return False
        s._decoded = len(lines)
        for raw in fresh:
            item = project(raw.decode("utf-8", "replace"))
            if item is None:
                continue
            item["n"] = len(s.events) + 1
            s.events.append(item)
            if item["k"] == "title" and not s.title:
                s.title = item["text"]
        if len(s.events) > EVENT_MAX:
            del s.events[: len(s.events) - EVENT_MAX]
        return True

    async def _tail(self, s: DshSession) -> None:
        """Poll the log. Cheap when nothing changed — the size check is the
        gate, so an idle session costs one stat() per second."""
        while True:
            with contextlib.suppress(Exception):
                await self._read_log(s)
            await asyncio.sleep(TAIL_INTERVAL)


def split_model(model) -> tuple[str | None, str | None]:
    """'provider/model' → (provider, model). None/'' → (None, None).

    Deliberately permissive: a typo is not rejected here, because dsh's own
    error ("NO_ADAPTER: no adapter registered for provider …") is written into
    the session's log within seconds and shows up in the live view, which
    teaches the operator more than a 422 would.
    """
    if not model:
        return None, None
    if isinstance(model, dict):
        provider = str(model.get("provider") or "").strip()
        mid = str(model.get("model") or "").strip()
    else:
        text = str(model).strip()
        if "/" not in text:
            raise harness.ValidationError("model must be 'provider/model'")
        provider, _, mid = text.partition("/")
        provider, mid = provider.strip(), mid.strip()
    if not provider or not mid:
        raise harness.ValidationError("model must be 'provider/model'")
    return provider, mid


# The one live runner (in-memory only; dies with the process).
runner = SessionRunner()
