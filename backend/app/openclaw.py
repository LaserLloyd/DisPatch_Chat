"""OpenClaw CLI integration.

Sends a message to an OpenClaw agent by spawning `openclaw agent ... --json`
and parsing the response. We deliberately do NOT pass `--deliver`, so replies
come back to us only — they are not re-sent to any messaging channel.

The JSON contract (observed on OpenClaw 2026.6.1, re-verified on 2026.6.6):

    {
      "runId": "...",
      "status": "ok",                 # "ok" | "error"
      "summary": "completed",
      "result": {
        "payloads": [ { "text": "...", "mediaUrl": null }, ... ],
        "meta": {
          "durationMs": 3161,
          "aborted": false,
          "finalAssistantVisibleText": "...",
          "finalAssistantRawText": "...",
          "agentMeta": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "sessionId": "...",
            "usage": { "total": 15934, ... }
          }
        }
      }
    }

The extractor is defensive: it prefers `payloads`, then visible/raw text, then a
range of legacy shapes, so it keeps working if the CLI output evolves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import SETTINGS

OPENCLAW_AGENTS_DIR = Path.home() / ".openclaw" / "agents"

# Hard ceiling on a single CLI invocation's stdout/stderr. The payload is one
# JSON document, so unlike the harness we cannot keep a tail — a truncated
# body would not parse. What we can do is refuse to buffer without limit:
# `communicate()` alone will hold in memory whatever the child writes, so a
# runaway or hostile CLI turns a chat reply into an OOM. Crossing the ceiling
# kills the process and reports a normal agent failure.
AGENT_OUTPUT_MAX = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class AgentOutputTooLarge(Exception):
    """Internal marker: the CLI wrote past AGENT_OUTPUT_MAX."""


async def _read_capped(stream, limit: int) -> bytes:
    """Read a pipe to EOF, raising once more than ``limit`` bytes arrive."""
    buf = bytearray()
    while True:
        try:
            chunk = await stream.read(_READ_CHUNK)
        except (ValueError, OSError):            # pipe torn down under us
            break
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise AgentOutputTooLarge()
    return bytes(buf)


def _kill_tree(proc) -> None:
    """SIGKILL the CLI *and anything it spawned*.

    The wrapper forks a native child that inherits the pipes, so killing only
    the direct child leaves the write ends open — asyncio then never sees the
    pipes disconnect and `proc.wait()` waits forever. The process gets its own
    session (``start_new_session``) precisely so the group is ours to sweep.
    Called BEFORE the process is reaped, while its pid is still ours.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _communicate_capped(proc, limit: int | None = None) -> tuple[bytes, bytes]:
    """`proc.communicate()` with a total-bytes ceiling per stream.

    On overflow the process group is killed and the SIBLING reader cancelled
    rather than awaited: a grandchild holding the pipes would otherwise keep
    them open and hang us exactly where the ceiling was supposed to help.
    """
    limit = AGENT_OUTPUT_MAX if limit is None else limit
    tasks = [asyncio.ensure_future(_read_capped(s, limit))
             for s in (proc.stdout, proc.stderr)]
    try:
        out, err = await asyncio.gather(*tasks)
    except AgentOutputTooLarge:
        for t in tasks:
            t.cancel()
        _kill_tree(proc)
        await asyncio.gather(*tasks, return_exceptions=True)
        # The group is dead, but the pipes still hold whatever it managed to
        # write, and a paused (unread) transport never sees EOF — so it never
        # reports the process as finished and proc.wait() would sit there.
        # One short bounded drain lets both pipes close.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(
                proc.stdout.read(), proc.stderr.read(),
                return_exceptions=True), timeout=5)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    await proc.wait()
    return out, err

# Custom-message lines whose narration is real assistant text worth delivering.
# When an agent yields control to wait on a subagent (sessions_yield), its
# "here's what I found / waiting for X" summary is written ONLY as a
# custom_message transcript line — it is not a normal assistant message and is
# not returned in the CLI's --json payload, so without this the watcher /
# reconciler / follower all skip it and the narration is lost from the chat.
_NARRATION_CUSTOM_TYPES = {"openclaw.sessions_yield"}


class AgentError(Exception):
    """Generic failure talking to an OpenClaw agent."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class AgentTimeout(AgentError):
    pass


class AgentNotFound(AgentError):
    """The `openclaw` binary could not be found / executed."""


class GatewayUnavailable(AgentError):
    """The gateway rejected the task at the door — down, starting or draining
    for a restart. The turn never started, so retrying cannot double-run it.
    """


# Signatures the CLI emits when the gateway refused the task outright (it is
# restarting/draining, or simply not up). Deliberately narrow: an error that
# merely CONTAINS one of these strings mid-turn still raises plain AgentError,
# because a started turn must never be blindly retried.
_GATEWAY_DOWN_RE = re.compile(
    r"GatewayDrainingError|Gateway is draining"
    r"|ECONNREFUSED|ECONNRESET before hello"
    r"|gateway (?:is )?not (?:running|reachable|available)"
    r"|errorCode[\"':= ]+UNAVAILABLE",
    re.I,
)


def _friendly_gateway_down(bot_id: str, detail: str) -> GatewayUnavailable:
    return GatewayUnavailable(
        f"The agent gateway is down or restarting, so {bot_id} can't answer "
        f"right now. Your message is saved — try again in a minute.",
        detail=detail[:800],
    )


@dataclass
class AgentPayload:
    text: str
    media_url: str | None = None
    sub: bool = False    # intermediate/working output -> collapsed in the UI


@dataclass
class AgentReply:
    payloads: list[AgentPayload]
    metadata: dict = field(default_factory=dict)


def session_key_for(bot_id: str, thread_id: str) -> str:
    """OpenClaw session-key convention — 1:1 with a chat thread."""
    return f"agent:{bot_id}:{thread_id}"


def resolve_session_file(bot_id: str, session_key: str) -> Path | None:
    """Find the live OpenClaw session transcript (.jsonl) for a session key.

    The gateway keeps a per-agent index (sessions.json) mapping session keys
    to session ids; the transcript is appended in real time during a turn,
    which is what powers the live "working" panel in the UI.

    Case handling: the gateway LOWERCASES the entire session key when it
    persists the index (a thread for a bot called `Assistant` is stored under
    `agent:assistant:daily-assistant-2026-06-14`), but callers build the key
    from the mixed-case bot id + thread id
    (`agent:Assistant:daily-Assistant-2026-06-14`). A
    case-sensitive `data.get()` therefore misses every mixed-case bot, which
    silently disables the live progress panel AND the post-turn follow-up
    follower for those bots (their late/intermediate replies never reach the
    UI). We match the exact key first, then fall back to a lowercase compare.
    """
    for cand in (bot_id, bot_id.lower()):
        d = OPENCLAW_AGENTS_DIR / cand / "sessions"
        idx = d / "sessions.json"
        if not idx.is_file():
            continue
        try:
            data = json.loads(idx.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ent = data.get(session_key)
        if not (isinstance(ent, dict) and ent.get("sessionId")):
            want = session_key.lower()
            ent = next(
                (v for k, v in data.items()
                 if k.lower() == want and isinstance(v, dict) and v.get("sessionId")),
                None,
            )
        if isinstance(ent, dict) and ent.get("sessionId"):
            f = d / f"{ent['sessionId']}.jsonl"
            if f.is_file():
                return f
    return None


def session_file_by_id(bot_id: str, session_id: str) -> Path | None:
    """Resolve a transcript directly from a known sessionId — race-free.

    The CLI reply metadata carries the exact ``sessionId`` (see _parse_reply), so
    the post-turn reconciler/follower can build the transcript path WITHOUT going
    through the sessions.json index, which can lag a write for a brand-new session
    (the race that otherwise silently disables all transcript-based delivery).
    """
    if not session_id:
        return None
    for cand in (bot_id, bot_id.lower()):
        f = OPENCLAW_AGENTS_DIR / cand / "sessions" / f"{session_id}.jsonl"
        if f.is_file():
            return f
    return None


def _session_kind(key: str) -> str:
    """Classify an OpenClaw session key for the session browser."""
    parts = key.split(":")
    if len(parts) >= 3:
        tag = parts[2]
        if tag in ("dashboard", "subagent", "cron"):
            return tag
        if tag.startswith("daily-"):
            return "daily"
        if tag == parts[1]:          # agent:main:main — the agent's own session
            return "main"
        return "thread"              # agent:<bot>:<thread-uuid> — a DisPatch thread
    return "other"


_UUID_TAG_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def mirror_kind(session_key: str) -> str:
    """Classify a session key for the gateway-chat mirror.

    ``webchat`` — a bare UUID thread tag (`agent:<bot>:<uuid>`): either a
    Control-UI webchat thread or a native DisPatch thread; the mirror separates
    the two by checking the DisPatch DB. ``main`` — the agent's own session
    (`agent:main:main`, and `agent:<bot>:main` for non-main agents). Everything
    else (`wd-*` watchdog one-shots, `explicit:*`, `acp:*`, scripted tags) is
    ``other`` and not mirrored by default.
    """
    parts = session_key.split(":")
    if len(parts) < 3:
        return "other"
    tag = parts[2]
    if tag in ("dashboard", "subagent", "cron"):
        return tag
    if tag.startswith("daily-"):
        return "daily"
    if tag == parts[1] or tag == "main":
        return "main"
    if len(parts) == 3 and _UUID_TAG_RE.match(tag):
        return "webchat"
    return "other"


def list_agent_sessions(bot_id: str) -> list[dict]:
    """All OpenClaw sessions for an agent (from sessions.json), with transcript
    file stats. Surfaces conversations DisPatch never created — cron runs, the
    agent's own session, subagent transcripts, dashboard sessions — so nothing the
    agent has ever said is unreachable. Newest-activity first."""
    out: dict[str, dict] = {}
    seen_files: set[str] = set()
    for cand in (bot_id, bot_id.lower()):
        d = OPENCLAW_AGENTS_DIR / cand / "sessions"
        idx = d / "sessions.json"
        if not idx.is_file():
            continue
        try:
            data = json.loads(idx.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for key, ent in data.items():
            sid = ent.get("sessionId") if isinstance(ent, dict) else None
            if not sid:
                continue
            f = d / f"{sid}.jsonl"
            if not f.is_file() or str(f) in seen_files:
                continue
            seen_files.add(str(f))
            try:
                st = f.stat()
                mtime, size = st.st_mtime, st.st_size
            except OSError:
                mtime, size = 0.0, 0
            out[key] = {
                "session_key": key,
                "session_id": sid,
                "kind": _session_kind(key),
                "mtime": mtime,
                "size": size,
            }
    return sorted(out.values(), key=lambda s: s["mtime"], reverse=True)


def read_transcript_items(path: Path, *, include_all: bool = True) -> list[dict]:
    """Parse a whole transcript into display items for the raw viewer.

    With ``include_all`` this returns EVERYTHING — user turns, assistant text,
    thinking, tool calls + their results, and narration custom_messages — i.e. the
    full record including the parts the normal delivery funnel deliberately drops
    (tool calls, thinking, subagent chatter). Each item: {idx, kind, role, text,
    name, ts}. ``kind`` ∈ {user, text, thinking, tool, tool_result, note}.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return transcript_items_from_bytes(raw, include_all=include_all)


def transcript_items_from_bytes(raw: bytes, *, include_all: bool = True) -> list[dict]:
    """Parse transcript JSONL bytes into display items (see read_transcript_items).

    Split out so the gateway mirror can parse just the bytes appended since its
    last offset — same parsing rules as a whole-file read, no duplication.

    Each item also carries ``uid`` — "<line>.<block>", its POSITION IN THESE
    BYTES. Unlike ``idx``, which counts only the items a given ``include_all``
    chose to emit, uid names the same block whatever the filter drops, so two
    readers of one transcript agree on what they have each already seen. It is
    file-relative for the whole-file reads :func:`read_transcript_items` does;
    for the mirror's incremental slices it is offset-relative and means nothing
    outside that slice.
    """
    items: list[dict] = []
    idx = 0
    for ln, line in enumerate(raw.split(b"\n")):
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        ts = e.get("ts") or e.get("timestamp") or ""
        if e.get("type") == "custom_message" and e.get("customType") in _NARRATION_CUSTOM_TYPES:
            det = e.get("details") if isinstance(e.get("details"), dict) else {}
            txt = (det.get("message") or e.get("content") or "").strip()
            if txt:
                items.append({"idx": idx, "uid": f"{ln}.0", "kind": "note",
                              "role": "assistant", "text": txt, "name": "",
                              "ts": ts}); idx += 1
            continue
        if e.get("type") != "message":
            continue
        m = e.get("message") or {}
        role = m.get("role")
        content = m.get("content")
        if role == "user" and include_all:
            text = _blocks_text(content)
            if text:
                items.append({"idx": idx, "uid": f"{ln}.0", "kind": "user",
                              "role": "user", "text": text, "name": "",
                              "ts": ts}); idx += 1
            continue
        if role == "toolResult" and include_all:
            text = _blocks_text(content)
            if text:
                items.append({"idx": idx, "uid": f"{ln}.0", "kind": "tool_result",
                              "role": "tool", "text": text[:4000], "name": "",
                              "ts": ts}); idx += 1
            continue
        if role != "assistant" or not isinstance(content, list):
            continue
        for b_i, b in enumerate(content):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                t = (b.get("text") or "").strip()
                if t:
                    items.append({"idx": idx, "uid": f"{ln}.{b_i}", "kind": "text",
                                  "role": "assistant", "text": t, "name": "",
                                  "ts": ts}); idx += 1
            elif bt == "thinking" and include_all:
                t = (b.get("thinking") or "").strip()
                if t:
                    items.append({"idx": idx, "uid": f"{ln}.{b_i}", "kind": "thinking",
                                  "role": "assistant", "text": t, "name": "",
                                  "ts": ts}); idx += 1
            elif bt == "toolCall" and include_all:
                name = b.get("name") or b.get("toolName") or "tool"
                args = b.get("arguments") or b.get("input") or {}
                try:
                    detail = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    detail = str(args)
                items.append({"idx": idx, "uid": f"{ln}.{b_i}", "kind": "tool",
                              "role": "assistant", "text": detail[:2000],
                              "name": str(name), "ts": ts}); idx += 1
    return items


def _blocks_text(content) -> str:
    """Join the text of a message's content blocks (string or block list)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") in (None, "text") and b.get("text"):
                out.append(b["text"])
            elif b.get("type") == "toolResult" and b.get("content"):
                out.append(str(b.get("content")))
    return "\n".join(out).strip()


def _with_source(base: str | None, idx: int, item: dict) -> dict:
    """Stamp a transcript item with its stable identity.

    `idx` disambiguates multiple text blocks written under one transcript
    entry — without it two blocks from the same line collide and the second is
    dropped as a duplicate, which is a LOSS rather than the duplicate it looks
    like.
    """
    if base:
        item["source_id"] = f"{base}:{idx}"
    return item


def parse_progress_line(line: str) -> list[dict]:
    """Extract progress items (thinking / tool calls / text) from one
    transcript line. Returns [] for anything that isn't agent activity.

    Each item carries `source_id` — "<sessionId>:<seq>[:<idx>]" — a STABLE
    identity for that transcript entry. It is what makes re-reading a
    transcript safe: content comparison could only ever check the trailing
    assistant run, so a re-scan from offset 0 replayed everything above the
    last user message into the chat (49 rows in one second, 41 duplicates).
    Identity does not care how far back the message was, and does not confuse
    a legitimately repeated line ("Done.") with a repeat of the same line.
    """
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return []
    _sid = e.get("sessionId")
    _seq = e.get("seq")
    _base = f"{_sid}:{_seq}" if _sid is not None and _seq is not None else None
    # Yield-narration: an agent waiting on a subagent records its progress
    # summary as a custom_message, not a normal assistant message. Surface the
    # clean narration (details.message — NOT content, which appends a
    # "[Context: ... sessions_yield ...]" boilerplate line). Delivered as a real
    # assistant text item; the downstream NO_REPLY/empty filter + dedup guard it.
    #
    # When the agent calls sessions_yield with no arguments, the gateway fills in
    # a default "Turn yielded." — that is system noise, not agent speech. Filter
    # it so it doesn't show up as a chat message (bug: it was delivered verbatim).
    if e.get("type") == "custom_message" and e.get("customType") in _NARRATION_CUSTOM_TYPES:
        details = e.get("details") if isinstance(e.get("details"), dict) else {}
        text = details.get("message") or e.get("content") or ""
        text = text.strip() if isinstance(text, str) else ""
        if text and text != "Turn yielded.":
            return [_with_source(_base, 0, {"kind": "text", "text": text[:300], "full_text": text})]
        return []
    if e.get("type") != "message":
        return []
    m = e.get("message") or {}
    if m.get("role") != "assistant":
        return []
    items: list[dict] = []
    content = m.get("content")
    if not isinstance(content, list):
        return []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            t = (b.get("thinking") or "").strip()
            if t:
                items.append({"kind": "thinking", "text": t[:300]})
        elif bt == "toolCall":
            name = b.get("name") or b.get("toolName") or "tool"
            args = b.get("arguments") or b.get("input") or {}
            try:
                detail = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                detail = str(args)
            items.append({"kind": "tool", "name": str(name), "text": detail[:300]})
        elif bt == "text":
            t = (b.get("text") or "").strip()
            if t:
                # text kept in full too: the post-turn follower delivers these
                # as real messages, not just progress snippets.
                items.append(_with_source(_base, len(items),
                                          {"kind": "text", "text": t[:300], "full_text": t}))
    return items


# Agent ids the gateway will accept: it normalizes to lowercase and replaces
# anything outside [a-z0-9_-]. The CLI does this to `--agent` before sending,
# so doing it here keeps the two transports addressing the same agent — a
# mixed-case id like `DS_Flash` otherwise risks resolving differently from the
# session key, and "unknown agent" is not a failure worth discovering in the
# family chat.
_AGENT_ID_INVALID_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_agent_id(bot_id: str) -> str:
    v = _AGENT_ID_INVALID_RE.sub("-", (bot_id or "").strip().lower())
    return v.strip("-") or "main"


async def send_via_gateway(
    client,
    bot_id: str,
    session_key: str,
    message: str,
    timeout: int | None = None,
    run_id: str | None = None,
) -> AgentReply:
    """Dispatch a turn over the gateway socket DisPatch already holds open.

    Identical request to the one `openclaw agent --json` makes, and the reply
    payload has the identical shape — `_parse_reply` is shared, deliberately,
    so the two transports cannot drift into disagreeing about what an agent
    said. What it skips is the Node process in between: on this box that
    wrapper measured 2.3 s of boot-and-connect before the run started, on every
    turn, which is the single largest fixed cost on the reply path.

    `deliver` stays False for the same reason the CLI is never given
    `--deliver`: replies come back to us and are not re-sent to any channel.
    """
    from . import gateway_ws
    timeout = timeout or SETTINGS.agent_timeout
    params = {
        "message": message,
        "agentId": normalize_agent_id(bot_id),
        "sessionKey": session_key,
        "deliver": False,
        "timeout": timeout,
        # A stable per-attempt key: the gateway dedups on it, so a resend can
        # never start a second run of the same turn. It is ALSO the run id the
        # gateway then uses on every `chat` delta and on `agent.wait`, which is
        # why the caller may supply it — a run you cannot name is a run you
        # cannot follow, stream, or ask about after a disconnect.
        "idempotencyKey": run_id or uuid.uuid4().hex,
        "cleanupBundleMcpOnRunEnd": True,
    }
    try:
        payload = await client.call_agent(params, timeout=timeout + 15)
    except gateway_ws.GatewayRunRefused as e:
        # Nothing ran. Retryable, and the caller's backoff already knows how.
        raise _friendly_gateway_down(bot_id, str(e))
    except gateway_ws.GatewayRunTimeout:
        raise AgentTimeout(
            f"{bot_id} took too long to respond (>{timeout}s).",
            detail="agent timeout (gateway transport)")
    except (gateway_ws.GatewayDisconnected, ConnectionError) as e:
        # The run was accepted, so it may still be underway on the gateway and
        # its reply will arrive over the session subscription. Retrying would
        # double-run it, so this is a plain AgentError, never a retryable one.
        raise AgentError(
            f"Lost the connection to the gateway while {bot_id} was answering.",
            detail=str(e)[:200])
    if not isinstance(payload, dict):
        raise AgentError(f"{bot_id} returned an unreadable response.",
                         detail=repr(payload)[:200])
    return _parse_reply(payload, bot_id, "")


def cli_available() -> bool:
    bin_ = SETTINGS.openclaw_bin
    if os.path.isabs(bin_):
        return os.path.exists(bin_) and os.access(bin_, os.X_OK)
    return shutil.which(bin_) is not None


async def send_to_agent(
    bot_id: str,
    session_key: str,
    message: str,
    timeout: int | None = None,
) -> AgentReply:
    timeout = timeout or SETTINGS.agent_timeout
    # Pass the message via a temp file (--message-file) instead of an argv element:
    # a single argv string is capped at MAX_ARG_STRLEN (~128KB) regardless of the
    # total ARG_MAX, so a large message (e.g. an inlined [[doc:...]] ref) crashed
    # the turn with OSError E2BIG ("Argument list too long"). A file has no limit.
    msg_path: str | None = None
    try:
        # Create + write INSIDE the try so a disk-full/IO failure mid-write is
        # surfaced as an AgentError (not a raw 500) and the temp file is always
        # unlinked by the finally below instead of leaking.
        try:
            msg_fd, msg_path = tempfile.mkstemp(prefix="dispatch-msg-", suffix=".txt")
            with os.fdopen(msg_fd, "w", encoding="utf-8") as mf:
                mf.write(message)
        except OSError as e:
            raise AgentError(
                f"Could not prepare the message for {bot_id} (disk error).",
                detail=str(e)[:200],
            )
        cmd = [
            SETTINGS.openclaw_bin, "agent",
            "--agent", bot_id,
            "--message-file", msg_path,
            "--session-key", session_key,
            "--json",
            "--timeout", str(timeout),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
                # Its own process group, so a runaway CLI can be swept whole
                # (see _kill_tree) instead of leaving a pipe-holding orphan.
                start_new_session=True,
            )
        except FileNotFoundError:
            raise AgentNotFound(
                "OpenClaw not available. Is it installed and on PATH?",
                detail=f"binary: {SETTINGS.openclaw_bin}",
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                _communicate_capped(proc), timeout=timeout + 15
            )
        except AgentOutputTooLarge:
            raise AgentError(
                f"{bot_id} produced an unreadably large reply.",
                detail=f"output passed {AGENT_OUTPUT_MAX // (1024 * 1024)}MB "
                       "and the process was stopped",
            )
        except TimeoutError:
            _kill_tree(proc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise AgentTimeout(
                f"{bot_id} took too long to respond (>{timeout}s).",
                detail="agent timeout",
            )

        stdout = stdout_b.decode("utf-8", "replace").strip()
        stderr = stderr_b.decode("utf-8", "replace").strip()

        # Try to parse JSON even on non-zero exit (the CLI may emit error JSON).
        parsed: dict | None = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None

        if proc.returncode != 0 and parsed is None:
            err_text = stderr or stdout
            if _GATEWAY_DOWN_RE.search(err_text):
                raise _friendly_gateway_down(bot_id, err_text)
            raise AgentError(
                f"{bot_id} failed (exit {proc.returncode}).",
                detail=err_text[:800],
            )

        if parsed is None:
            # Non-JSON but exit 0 — treat raw stdout as the reply.
            if not stdout:
                raise AgentError(f"{bot_id} returned an empty response.", detail=stderr[:800])
            return AgentReply(payloads=[AgentPayload(text=stdout)], metadata={"raw": True})

        return _parse_reply(parsed, bot_id, stderr)
    finally:
        if msg_path is not None:
            try:
                os.unlink(msg_path)
            except OSError:
                pass


def _parse_reply(parsed: dict, bot_id: str, stderr: str) -> AgentReply:
    status = str(parsed.get("status", "")).lower()
    result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    usage = agent_meta.get("usage") if isinstance(agent_meta.get("usage"), dict) else {}

    aborted = bool(meta.get("aborted"))
    if status == "error" or aborted:
        summary = parsed.get("summary") or meta.get("stopReason") or "agent error"
        detail = (str(summary) + (f" — {stderr}" if stderr else ""))[:800]
        # An error reply whose whole story is "gateway draining/unreachable"
        # means the task was refused before it started — retryable.
        if not aborted and _GATEWAY_DOWN_RE.search(detail):
            raise _friendly_gateway_down(bot_id, detail)
        raise AgentError(f"{bot_id} reported an error.", detail=detail)

    metadata = {
        "model": agent_meta.get("model"),
        "provider": agent_meta.get("provider"),
        "tokens": usage.get("total"),
        "duration_ms": meta.get("durationMs"),
        "session_id": agent_meta.get("sessionId"),
        "run_id": parsed.get("runId"),
        "stop_reason": meta.get("stopReason"),
    }
    # Drop empty metadata keys.
    metadata = {k: v for k, v in metadata.items() if v is not None}

    # No placeholder when the turn produced no visible text (silent tool turn):
    # agents often deliver the real reply moments later via /api/inject, and a
    # "*(no reply)*" bubble immediately followed by the actual answer reads as
    # a glitch. An empty payload list simply posts nothing.
    payloads = _extract_payloads(parsed, result, meta)
    if not payloads:
        # The turn had no VISIBLE text, but raw text (tool chatter / working
        # notes) may exist. Surface it as a sub message — the UI shows those
        # in a collapsed "working" box — so nothing the agent said is lost.
        raw = meta.get("finalAssistantRawText")
        if isinstance(raw, str) and raw.strip():
            payloads = [AgentPayload(text=raw.strip(), sub=True)]
    return AgentReply(payloads=payloads, metadata=metadata)


def _extract_payloads(parsed: dict, result: dict, meta: dict) -> list[AgentPayload]:
    out: list[AgentPayload] = []

    # 1. Canonical: result.payloads[] = [{text, mediaUrl}]
    raw_payloads = result.get("payloads")
    if isinstance(raw_payloads, list):
        for p in raw_payloads:
            if not isinstance(p, dict):
                continue
            text = (p.get("text") or "").strip()
            media = p.get("mediaUrl") or p.get("media_url")
            # The gateway marks its own mid-turn tool-error notices
            # ("⚠️ 🛠️ Exec failed: …") with this metadata flag; they are
            # runtime narration, not agent speech — collapse them as "sub".
            # (_deliver_assistant_text also demotes by text prefix, which
            # covers transcript-derived paths where the flag is absent.)
            p_meta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
            warn = bool(p_meta.get("nonTerminalToolErrorWarning"))
            if text or media:
                out.append(AgentPayload(text=text, media_url=media, sub=warn))
        if out:
            return out

    # 2. Visible/raw assistant text from meta.
    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        txt = meta.get(key)
        if isinstance(txt, str) and txt.strip():
            return [AgentPayload(text=txt.strip())]

    # 3. Legacy / alternate shapes.
    for container in (parsed, result):
        for key in ("reply", "text", "message", "output"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return [AgentPayload(text=val.strip())]
        content = container.get("content")
        if isinstance(content, str) and content.strip():
            return [AgentPayload(text=content.strip())]
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text")
            ]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                return [AgentPayload(text=joined)]

    return out
