"""Agent-fired image jobs: a placeholder now, the picture when it lands.

WHY THIS EXISTS
---------------
A bot that wants to show a picture has, until now, had exactly one option: run
an image generator itself, wait for it, and only then say anything. On a shared
rig a render is 10 s when the GPUs are idle and several minutes when they are
not, so the agent's whole turn hangs behind it — and when the rig refuses (no
free VRAM, a workflow that will not fit), the turn either dies or, worse,
finishes with a cheerful sentence and no image at all.

This module inverts that. The agent makes **one** call
(``POST /api/image-jobs``) and is done in milliseconds. DisPatch persists a
real message in the thread that says the picture is coming, drives the image
server itself in the background, and then **edits that same message** into the
finished picture. If the render fails or runs out of time, the same message
becomes a visible ``⚠️ image failed: <reason>`` line.

The load-bearing property is that **failure never looks like pending forever**.
Every job has a hard deadline, every terminal state rewrites the placeholder,
and a job left mid-flight by a restart is resumed from the database or failed
out loudly at boot — there is no state in which the row simply stops changing.

The other property is that no model is in the loop after the first call.
Everything below the endpoint is deterministic plumbing: enqueue, poll, fetch,
validate, ingest, edit. A model cannot forget a step of it.

SHAPE OF THE BACKEND
--------------------
The image server is an **MCP server over streamable HTTP** — the same surface
`doxy-pics` drives, and the same one the reaction and avatar pools use to
refill themselves. Three calls matter:

* ``generate_image(prompt, workflow, wait=false)`` → ``{"job_id": "..."}``
  immediately, without waiting for the render. (It can still refuse
  synchronously — "not enough free VRAM" — and that refusal is the job's
  failure reason, delivered within seconds.)
* ``get_job(job_id)`` → ``{"state", "done", "error", "files_rel": [...]}``.
* ``GET <files_url>/<files_rel[0]>`` → the actual bytes.

This module is pure: no FastAPI, no database, no broadcasting. It knows how to
talk to the rig and how to tell a real image from an error page. The routes,
the placeholder message and the worker loop compose it in main.py, which is
where the HTTP error mapping and the Safe-Mode rules already live.

CONFIGURATION
-------------
``DISPATCH_CLAWFORGE_URL`` is the MCP endpoint (e.g.
``http://192.0.2.5:8700/mcp``). There is deliberately **no default**: the
address of somebody's GPU box is site configuration, not something to ship in
a public repository, and "auto" mode keys off exactly this — the feature is on
when an operator has pointed it at a server and off otherwise.
``DISPATCH_CLAWFORGE_FILES_URL`` defaults to ``/files/`` on the same origin.
``DISPATCH_CALLBACK_BASE`` is DisPatch's own origin as the rig sees it; set, it
arms a completion callback per job, which only makes terminal transitions
arrive sooner. Unset (the default) the worker is pure polling and behaves
exactly as it did before callbacks existed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import openclaw_text

log = logging.getLogger("local-chat.image_jobs")

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

#: How long one MCP call may take. Enqueue and poll are both meant to answer in
#: well under a second; a rig that is thinking about it for a minute is a rig
#: that is not going to answer, and the job's own deadline governs the retry.
MCP_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 10.0

#: Fetching the finished image. Generous — this is a multi-megabyte PNG over a
#: home network, and by this point the render has already succeeded.
FETCH_TIMEOUT_S = 180.0

#: Refuse to buffer more than this from the rig. A render is a few MB; anything
#: at this scale is a misconfiguration pointed at the wrong URL.
FETCH_MAX_BYTES = 64 * 1024 * 1024

#: Below this an "image" is an error page, a 28-byte JSON blob, or a truncated
#: transfer — all three have been seen in the wild on this exact pipeline.
MIN_IMAGE_BYTES = 10 * 1024

#: File magic we accept. The rig's workflows emit PNG; JPEG and WebP are here
#: because a workflow that saves one should not fail validation for it.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"RIFF", ".webp"),          # RIFF....WEBP; checked further below
)


class ImageJobError(Exception):
    """A failure with a reason fit to show a human in the chat.

    Every path that can fail raises this with a short, already-trimmed message,
    because that message is what ends up in the ``⚠️ image failed: …`` line —
    not a traceback, and not a 4 KB wall of rig log.
    """

    def __init__(self, message: str, *, retryable: bool = False,
                 code: str = ""):
        super().__init__(message)
        self.message = _clip(message)
        # Retryable failures are transport-shaped (the rig briefly unreachable)
        # or a refusal the rig itself says is temporary; the worker keeps
        # polling until the deadline instead of giving up on the first blip.
        # Everything else is terminal on the spot.
        self.retryable = retryable
        # The rig's machine-readable reason, when it gave one. Kept alongside
        # the prose rather than instead of it: the sentence is what a human
        # reads in the thread, the code is what the worker decides on.
        self.code = code


#: Refusal codes the rig says are worth another attempt before the deadline.
#: Anything else — a workflow that does not exist, a graph ComfyUI rejected, a
#: GPU that is the wrong generation — will refuse identically in a minute, and
#: retrying it only delays the visible failure.
RETRYABLE_CODES = frozenset({
    "insufficient_vram",        # a co-tenant is holding the card; it frees up
    "backend_unavailable",      # ComfyUI was starting or briefly down
    "captioner_unavailable",    # the captioner's LLM backend was busy
})


def _clip(text: str, limit: int = 300) -> str:
    """One line, bounded. Rig errors arrive as paragraphs with log tails."""
    s = " ".join((text or "").split())
    return s[: limit - 1] + "…" if len(s) > limit else s


# --------------------------------------------------------------------------- #
# MCP over streamable HTTP
# --------------------------------------------------------------------------- #


def _sse_json(body: str) -> dict:
    """The JSON-RPC object out of either a plain body or an SSE stream.

    Streamable-HTTP MCP servers answer either way depending on the request and
    their mood; the same client has to read both. A `data:` line that does not
    parse is skipped rather than fatal — servers interleave comments and
    keep-alives.
    """
    body = (body or "").strip()
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    for line in body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk:
                continue
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return {}


@dataclass
class ClawForge:
    """A minimal MCP client for one image server.

    Deliberately stateless between calls: it re-initialises a session for every
    tool call. That is one extra round trip on loopback/LAN and it means a rig
    restart, a session timeout or a server upgrade can never leave this process
    holding a dead handle — which, for something that polls every few seconds
    for ten minutes, is the failure mode that actually matters.
    """

    url: str
    files_url: str = ""
    client_name: str = "dispatch"

    def __post_init__(self) -> None:
        self.url = (self.url or "").strip()
        if not self.files_url:
            # …/mcp -> …/files/ . The two are served from the same origin by
            # every deployment of this server we know of.
            base = re.sub(r"/mcp/?$", "", self.url)
            self.files_url = f"{base.rstrip('/')}/files/" if base else ""

    # -- plumbing ---------------------------------------------------------- #

    def _http(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
        )

    async def call(self, tool: str, args: dict, *,
                   timeout: float = MCP_TIMEOUT_S) -> dict:
        """One ``tools/call``. Returns the raw MCP result object."""
        if not self.url:
            raise ImageJobError("no image server configured")
        headers = {"content-type": "application/json",
                   "accept": "application/json, text/event-stream"}
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": self.client_name, "version": "1"}},
        }
        try:
            async with self._http(timeout) as http:
                r = await http.post(self.url, json=init, headers=headers)
                r.raise_for_status()
                sid = r.headers.get("mcp-session-id")
                body = _sse_json(r.text)
                if body.get("error") or not sid:
                    raise ImageJobError(
                        f"image server handshake failed: "
                        f"{body.get('error') or 'no session id'}", retryable=True)
                headers["mcp-session-id"] = sid
                await http.post(self.url, headers=headers,
                                json={"jsonrpc": "2.0",
                                      "method": "notifications/initialized"})
                r = await http.post(
                    self.url, headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}})
                r.raise_for_status()
                body = _sse_json(r.text)
        except ImageJobError:
            raise
        except httpx.HTTPError as e:
            # Unreachable / timed out / 5xx: the rig may be restarting. Worth
            # another poll before the deadline, so mark it retryable.
            raise ImageJobError(f"image server unreachable: {type(e).__name__}",
                                retryable=True)
        if body.get("error"):
            raise ImageJobError(f"image server refused {tool}: "
                                f"{_error_text(body['error'])}")
        return body.get("result") or {}

    # -- tool wrappers ----------------------------------------------------- #

    async def _tool_json(self, tool: str, args: dict, *,
                         timeout: float = MCP_TIMEOUT_S) -> dict:
        """``tools/call`` whose result is the JSON object in the first text block.

        A tool-level failure (``isError``) is plain prose, not JSON, and it is
        the single most useful string in this whole module — it is the rig
        telling you *why*, in a sentence ("Not enough free VRAM … short by
        15.9 GB"). Raise it verbatim rather than flattening every failure to
        "no image returned".

        The retry decision comes from ``structuredContent.error.code``, not
        from the prose: the text carries the SDK's own "Error executing tool
        <name>: " prefix in front of the ``[code]``, so matching it is exactly
        the kind of parse that reads fine and silently stops working.
        """
        res = await self.call(tool, args, timeout=timeout)
        blocks = [b for b in (res.get("content") or []) if isinstance(b, dict)]
        text = " ".join((b.get("text") or "") for b in blocks).strip()
        if res.get("isError"):
            code = _error_code(res)
            raise ImageJobError(text or f"{tool} failed (no reason given)",
                                code=code, retryable=code in RETRYABLE_CODES)
        for b in blocks:
            t = (b.get("text") or "").strip()
            if t.startswith("{"):
                try:
                    return json.loads(t)
                except json.JSONDecodeError:
                    continue
        return {}

    async def enqueue(self, spec: ImageSpec, *, callback_url: str = "",
                      callback_token: str = "") -> EnqueueResult:
        """Start a render WITHOUT waiting for it.

        ``wait=false`` is what makes the whole feature possible: it returns a
        job id in about a second even when the queue is deep. Two answers are
        legitimate and both are handled — a job id (the normal case) and, on a
        completely idle rig, the finished file straight away.

        A ``callback_url`` asks the rig to POST once when the job reaches a
        terminal state. It is an optimisation on top of the poll, never a
        replacement: the rig makes three attempts and then gives up with a log
        line, so the sweep stays the thing that makes a lost job impossible.
        """
        args: dict[str, Any] = {"prompt": spec.prompt, "wait": False,
                                "client": self.client_name,
                                "priority": spec.priority}
        if callback_url:
            args["callback_url"] = callback_url
            if callback_token:
                args["callback_token"] = callback_token
        if spec.workflow:
            args["workflow"] = spec.workflow
        if spec.negative:
            args["negative_prompt"] = spec.negative
        if spec.ratio:
            args["aspect_ratio"] = spec.ratio
        if spec.width and spec.height:
            args["width"], args["height"] = spec.width, spec.height
        res = await self._tool_json("generate_image", args)
        if res.get("error"):
            raise ImageJobError(str(res["error"]))
        rel = _first_rel(res)
        job_id = res.get("job_id")
        if not job_id and not rel:
            raise ImageJobError(
                f"image server queued nothing: "
                f"{res.get('note') or res.get('state') or 'no job id'}")
        return EnqueueResult(job_id=job_id or "", files_rel=rel,
                             seed=_first_seed(res))

    async def poll(self, job_id: str) -> PollResult:
        """One ``get_job``. Terminal states are reported, not raised."""
        res = await self._tool_json("get_job", {"job_id": job_id})
        rel = _first_rel(res)
        err = str(res.get("error") or "").strip()
        state = str(res.get("state") or "").strip() or "unknown"
        done = bool(rel) or bool(res.get("done")) or state in ("failed", "cancelled")
        if state in ("failed", "cancelled") and not err:
            err = str(res.get("note") or f"render {state}")
        return PollResult(state=state, done=done, files_rel=rel,
                          error=_clip(err), seed=_first_seed(res),
                          progress=_progress(res))

    async def cancel(self, job_id: str) -> dict:
        """Withdraw a render from the rig. Best effort by contract.

        Cancelling a job that already finished is a NORMAL reply on this rig
        (``cancelled: false`` and a note), not an error — which is what makes
        it safe to call from the timeout path without racing the completion.
        Callers still treat every outcome, including a raise, as advisory: the
        job is failed locally either way.
        """
        return await self._tool_json(
            "cancel_job", {"job_id": job_id, "client": self.client_name})

    async def fetch(self, files_rel: str) -> bytes:
        """Pull the finished image and prove it is one.

        Validation is not paranoia: this pipeline's characteristic failure is a
        28-byte error JSON saved with a .png name, which renders as a broken
        image in every chat window in the house and looks exactly like a bug in
        DisPatch. Bytes that are not an image never become a message.
        """
        if not self.files_url:
            raise ImageJobError("no file endpoint for the image server")
        url = self.files_url.rstrip("/") + "/" + files_rel.lstrip("/")
        try:
            async with self._http(FETCH_TIMEOUT_S) as http:
                async with http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > FETCH_MAX_BYTES:
                            raise ImageJobError("image is implausibly large — "
                                                "refusing to buffer it")
                        chunks.append(chunk)
        except ImageJobError:
            raise
        except httpx.HTTPError as e:
            raise ImageJobError(f"could not fetch the image: {type(e).__name__}",
                                retryable=True)
        data = b"".join(chunks)
        ok, why = validate_image(data)
        if not ok:
            raise ImageJobError(f"the rig returned something that is not an image ({why})")
        return data


def _error_text(err: Any) -> str:
    if isinstance(err, dict):
        return _clip(str(err.get("message") or err))
    return _clip(str(err))


def _first_rel(res: dict) -> str:
    rels = res.get("files_rel")
    if isinstance(rels, list) and rels:
        first = rels[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return ""


def _error_code(res: dict) -> str:
    """``structuredContent.error.code`` off an ``isError`` result, or "".

    Defensive at every level: a server that has not shipped structured errors
    yet, or one that answers with a differently-shaped block, must degrade to
    "no code" (and therefore to the pre-existing terminal-on-refusal
    behaviour) rather than raising inside the error path.
    """
    sc = res.get("structuredContent")
    err = sc.get("error") if isinstance(sc, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return str(code).strip()[:64] if isinstance(code, str) else ""


def _progress(res: dict) -> dict | None:
    """``{step, steps, percent}`` off a poll, or None.

    Stage progress, not job progress: a workflow with two samplers runs 0→100
    twice and a finished job reports nothing at all. Stored and shown as a
    number for exactly that reason — anything derived from it (an ETA, a
    monotonic bar) would be wrong the moment a second sampler starts.
    """
    p = res.get("progress")
    if not isinstance(p, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("step", "steps"):
        v = p.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100_000:
            out[key] = v
    pct = p.get("percent")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        out["percent"] = round(max(0.0, min(100.0, float(pct))), 1)
    return out or None


def _first_seed(res: dict) -> int | None:
    seeds = res.get("seeds")
    if isinstance(seeds, list) and seeds and isinstance(seeds[0], int):
        return seeds[0]
    seed = res.get("seed")
    return seed if isinstance(seed, int) else None


def validate_image(data: bytes) -> tuple[bool, str]:
    """(ok, why-not). Magic bytes first, then a size floor."""
    if not data:
        return False, "empty response"
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            if kind == ".webp" and data[8:12] != b"WEBP":
                continue
            break
    else:
        head = data[:16].decode("utf-8", "replace").strip()
        return False, f"not an image (starts {head!r})"
    if len(data) < MIN_IMAGE_BYTES:
        return False, f"only {len(data)} bytes"
    return True, ""


def image_suffix(data: bytes, files_rel: str = "") -> str:
    """The extension to store the bytes under — magic first, name as a hint."""
    for magic, kind in _MAGIC:
        if data.startswith(magic) and not (kind == ".webp" and data[8:12] != b"WEBP"):
            return kind
    suffix = files_rel.rsplit(".", 1)[-1].lower() if "." in files_rel else ""
    return f".{suffix}" if suffix.isalnum() and len(suffix) <= 4 else ".png"


# --------------------------------------------------------------------------- #
# Job specification
# --------------------------------------------------------------------------- #

#: Hard ceiling on one job, from the moment the placeholder is written. Ten
#: minutes is well past a cold model load on a busy rig and well short of "the
#: family scrolls past a spinner from this morning".
DEADLINE_S = 600

#: How often the worker asks the rig about a job in flight.
POLL_INTERVAL_S = 5.0

#: Fires per bot per rolling minute. A render occupies a GPU for tens of
#: seconds, so this is about protecting the rig (and the thread) from a model
#: in a loop, not about protecting the endpoint.
RATE_LIMIT = 3
RATE_WINDOW_S = 60.0

MAX_PROMPT_CHARS = 2000
MAX_CAPTION_CHARS = 200

_RATIO_RE = re.compile(r"^\d{1,2}:\d{1,2}$")


@dataclass
class ImageSpec:
    """What to render. Validated at the edge so the worker never sees junk."""

    prompt: str
    workflow: str = ""
    ratio: str = ""
    width: int | None = None
    height: int | None = None
    negative: str = ""
    caption: str = ""
    #: Queue band on the rig: 1 interactive, 2 normal, 3 background. Defaults
    #: to 1 because every job this class describes has a placeholder message
    #: sitting in a thread with somebody looking at it; the pool refills, which
    #: nobody is waiting on, go through the image CLI and pass 3 there.
    priority: int = 1

    def __post_init__(self) -> None:
        self.prompt = (self.prompt or "").strip()
        if not self.prompt:
            raise ValueError("prompt is required")
        if len(self.prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt is longer than {MAX_PROMPT_CHARS} characters")
        self.workflow = (self.workflow or "").strip()[:80]
        self.negative = (self.negative or "").strip()[:MAX_PROMPT_CHARS]
        self.caption = (self.caption or "").strip()[:MAX_CAPTION_CHARS]
        self.ratio = (self.ratio or "").strip()
        if self.ratio and not _RATIO_RE.match(self.ratio):
            raise ValueError("ratio must look like 3:2")
        for name in ("width", "height"):
            v = getattr(self, name)
            if v is not None and not (64 <= int(v) <= 4096):
                raise ValueError(f"{name} must be between 64 and 4096")
        if (self.width is None) != (self.height is None):
            raise ValueError("give both width and height, or neither")
        try:
            self.priority = int(self.priority)
        except (TypeError, ValueError):
            raise ValueError("priority must be 1, 2 or 3")
        if self.priority not in (1, 2, 3):
            raise ValueError("priority must be 1, 2 or 3")

    def to_json(self) -> str:
        return json.dumps({"prompt": self.prompt, "workflow": self.workflow,
                           "ratio": self.ratio, "width": self.width,
                           "height": self.height, "negative": self.negative,
                           "caption": self.caption,
                           "priority": self.priority}, sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> ImageSpec:
        d = json.loads(blob or "{}")
        return cls(prompt=d.get("prompt", ""), workflow=d.get("workflow", ""),
                   ratio=d.get("ratio", ""), width=d.get("width"),
                   height=d.get("height"), negative=d.get("negative", ""),
                   caption=d.get("caption", ""),
                   # A row written before priority existed reads back as 1,
                   # which is what it was submitted as in every practical
                   # sense: it was a chat request with somebody waiting.
                   priority=d.get("priority", 1))


@dataclass
class EnqueueResult:
    job_id: str
    files_rel: str = ""          # set when an idle rig answered immediately
    seed: int | None = None


@dataclass
class PollResult:
    state: str
    done: bool
    files_rel: str = ""
    error: str = ""
    seed: int | None = None
    progress: dict | None = None


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


@dataclass
class RateLimiter:
    """Fixed-window-per-key limiter, deliberately tiny and in-process.

    Same shape as the reaction limiter next door: a refusal here is final (the
    caller is told, and does not queue), and a restart clears the window —
    which is correct, because a restart also clears every job that could have
    been racing.
    """

    limit: int = RATE_LIMIT
    window_s: float = RATE_WINDOW_S
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str, *, now: float | None = None) -> str | None:
        t = now if now is not None else time.monotonic()
        recent = [x for x in self._hits.get(key, []) if t - x < self.window_s]
        if len(recent) >= self.limit:
            wait = int(self.window_s - (t - recent[0])) + 1
            self._hits[key] = recent
            return (f"Too many image requests — {self.limit} per "
                    f"{int(self.window_s)}s. Try again in {wait}s.")
        recent.append(t)
        self._hits[key] = recent
        return None

    def refund(self, key: str) -> None:
        hits = self._hits.get(key)
        if hits:
            hits.pop()

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------- #
# Job states
#
# The split between QUEUED and RUNNING exists so the resume-after-restart path
# can tell "we wrote the placeholder but never got a job id out of the rig"
# (unrecoverable — nothing to poll) apart from "the rig has it, keep polling"
# (recoverable).
#
# CANCELLED is a THIRD terminal outcome, not a flavour of failure: the rig
# reports it when the render was withdrawn — by our own timeout path, by a
# thread being deleted under it, or by an operator pressing Interrupt in a
# ComfyUI tab on the rig. It can therefore arrive without DisPatch asking for
# it, and a worker that only branches on "failed" would read it as still
# pending and wait out the whole deadline.
# --------------------------------------------------------------------------- #

QUEUED = "queued"       # placeholder written, not yet accepted by the rig
RUNNING = "running"     # the rig has a job id for it
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
OPEN_STATES = (QUEUED, RUNNING)
TERMINAL_STATES = (DONE, FAILED, CANCELLED)


# --------------------------------------------------------------------------- #
# Failure counter
#
# Same shape (and the same reasoning) as the reaction fire-failure counter:
# /api/health exposes the COUNT so an operator watching the box can see image
# jobs failing without the prompts, rig errors and bot names leaking into a
# route a locked device can reach. The details stay in the journal.
# --------------------------------------------------------------------------- #

_FAILURES: list[dict] = []
_FAILURES_MAX = 200


def note_failure(job_id: str, reason: str, *, bot: str = "") -> None:
    _FAILURES.append({"at": time.time(), "job_id": str(job_id)[:32],
                      "reason": str(reason)[:200], "bot": str(bot)[:60]})
    del _FAILURES[:-_FAILURES_MAX]


def failure_stats(window_s: float = 24 * 3600.0) -> dict:
    cutoff = time.time() - window_s
    recent = [f for f in _FAILURES if f["at"] >= cutoff]
    return {"failures_24h": len(recent), "recent": recent[-10:]}


def reset_failures() -> None:
    _FAILURES.clear()


# --------------------------------------------------------------------------- #
# Inline markers — how an agent asks for a picture from inside a reply
#
# `POST /api/image-jobs` is the explicit path; this is the one a model actually
# takes, because writing a marker costs it no tool call and no extra turn. The
# marker is stripped at the persist chokepoint, so the syntax never reaches a
# chat bubble, and the model gets nothing back — the placeholder it produces is
# a separate message that the worker rewrites into the picture.
# --------------------------------------------------------------------------- #

#: `[[pic:a blue teapot]]` or `[[pic:a blue teapot|Tea, at last]]`.
#: Single line and non-greedy: a marker may not span a paragraph, and the first
#: `]]` closes it, so two markers on one line are two markers rather than one
#: swallowing everything between them.
PIC_MARKER_RE = re.compile(r"\[\[pic:([^\n]*?)\]\]", re.IGNORECASE)

#: Same ceiling as reaction markers, for the same reason: a model in a loop
#: must not be able to occupy the rig for the length of one reply.
MAX_PIC_MARKERS_PER_MESSAGE = 2


def parse_pic_marker(body: str) -> tuple[str, str]:
    """``(prompt, caption)`` out of one marker body.

    Split on the FIRST `|`: a prompt may not contain one, a caption may. The
    other way round, a caption with a pipe in it would silently move half of
    itself into the prompt and render something nobody asked for.
    """
    prompt, sep, caption = body.partition("|")
    return prompt.strip(), (caption.strip() if sep else "")


def extract_pic_markers(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Strip ``[[pic:…]]`` markers out of text.

    Returns ``(clean_text, [(prompt, caption), …])``, at most
    :data:`MAX_PIC_MARKERS_PER_MESSAGE` of them; a marker with an empty prompt
    is removed and reported to nobody, exactly like an unknown reaction id —
    leaving the syntax in the bubble would be worse than dropping the request.

    CODE IS SKIPPED, for the reason the reaction markers learned it: a quoted
    `[[pic:…]]` is an agent DESCRIBING the syntax, and firing it both corrupts
    the sentence and spends rig time on documentation.
    """
    if not content or "[[pic:" not in content.lower():
        return content, []
    found: list[tuple[str, str]] = []
    dropped = 0

    def _sub(m: re.Match) -> str:
        nonlocal dropped
        prompt, caption = parse_pic_marker(m.group(1))
        if not prompt:
            dropped += 1
        elif len(found) < MAX_PIC_MARKERS_PER_MESSAGE:
            found.append((prompt, caption))
        else:
            dropped += 1
        return ""

    def _clean(segment: str) -> str:
        # Tidy PER SEGMENT so the whitespace collapse never reaches the code
        # regions the walk copied through verbatim.
        segment = PIC_MARKER_RE.sub(_sub, segment)
        segment = re.sub(r"[ \t]{2,}", " ", segment)
        return re.sub(r"\n{3,}", "\n\n", segment)

    cleaned = openclaw_text.sub_outside_code(content, _clean).strip()
    if dropped:
        log.info("dropped %d [[pic:…]] marker(s) — empty prompt or over the "
                 "%d-per-message cap", dropped, MAX_PIC_MARKERS_PER_MESSAGE)
    return cleaned, found


def strip_pic_markers(content: str) -> str:
    """The marker-free text, with no side effects and no logging.

    Byte-identical to ``extract_pic_markers(content)[0]``. Dedup keys need
    exactly this: a canonical key must mirror every transform persisting
    applies, and computing one must not log.
    """
    if not content or "[[pic:" not in content.lower():
        return content

    def _clean(segment: str) -> str:
        segment = PIC_MARKER_RE.sub("", segment)
        segment = re.sub(r"[ \t]{2,}", " ", segment)
        return re.sub(r"\n{3,}", "\n\n", segment)

    return openclaw_text.sub_outside_code(content, _clean).strip()
