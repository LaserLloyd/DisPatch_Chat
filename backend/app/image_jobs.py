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

A fourth, ``comfy_status()``, is asked only "can you render right now?" — see
:class:`RigReadiness`. Every submit is tagged with ``client: "dispatch"`` so
the rig can attribute the work and resolve its own ``"last"`` per client, and
every job is followed by ``job_id``, never by ``"last"``, which is rig-wide
and races with whoever else is generating.

Refusals arrive coded (``structuredContent.error.code``) and the code, not the
prose, decides what happens next: some are worth waiting out, ``gpu_leased``
means the rig is BOOKED by somebody else, and a ``benchmark`` booking is one
to stand down from rather than sit through. Payloads carry a
``schema_version`` that bumps only when a field moves; it is recorded and
reported so that day is visible rather than mysterious.

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
from typing import Any, ClassVar

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
#: After a transport failure, how long every call fails fast (retryable)
#: before the next real probe. Shorter than the worker tick × 4 so the health
#: loop-beat never sees a stall, longer than one tick so a down rig costs one
#: connect timeout per window rather than one per job per tick.
RIG_BACKOFF_S = 15.0

#: Transport failures that happened BEFORE the request was sent, so repeating
#: one cannot have doubled anything. Everything else — a read timeout, a reset
#: mid-answer — leaves the question of whether the rig acted on it open.
_SAFE_TO_REPEAT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

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
                 code: str = "", retry_after_s: float = 0.0,
                 facts: dict | None = None, uncertain: bool = False):
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
        # How long the rig says to wait, when it says. Advisory: this pipeline
        # has a deadline of its own and the sweep is what actually retries.
        self.retry_after_s = float(retry_after_s or 0.0)
        # Whatever the rig attached to the refusal (a lease holder, its kind,
        # an expiry). Used for wording, never for a decision that matters more
        # than the code does.
        self.facts = facts or {}
        # The request LEFT this box and the answer did not come back, so we do
        # not know whether the rig acted on it. Only meaningful for a call that
        # starts work: repeating a poll is free, repeating a submit is a second
        # render on a contended GPU that nothing will ever poll or cancel.
        self.uncertain = uncertain

    @property
    def reservation(self) -> str:
        """"bench-runner (benchmark)" for a `gpu_leased` refusal, else ""."""
        if self.code != GPU_LEASED:
            return ""
        who = str(self.facts.get("holder_family")
                  or self.facts.get("holder") or "somebody else")
        kind = str(self.facts.get("kind") or "")
        return f"{who} ({kind})" if kind else who


#: A submit refused because the card is inside somebody else's GPU lease
#: (ClawForge2 2.2.1, 2026-09-04). Not a failure of ours and not a shortage:
#: the rig is BOOKED, and the honest answer to a human is "come back later".
GPU_LEASED = "gpu_leased"
#: Our own client tag has too many renders in flight. A refusal, not a hold —
#: the rig returns at once and owns no work, so the wait is ours to serve.
CLIENT_QUOTA = "client_quota"
#: The render started, moved, and then went quiet past the rig's ceiling.
#: Distinct from a job that never moved at all, and worth ONE more attempt:
#: a stall is usually a wedged sampler, not a graph that cannot run.
STALLED = "stalled"

#: Refusal codes the rig says are worth another attempt before the deadline.
#: Anything else — a workflow that does not exist, a graph ComfyUI rejected, a
#: GPU that is the wrong generation — will refuse identically in a minute, and
#: retrying it only delays the visible failure.
RETRYABLE_CODES = frozenset({
    "insufficient_vram",        # a co-tenant is holding the card; it frees up
    "backend_unavailable",      # ComfyUI was starting or briefly down
    "captioner_unavailable",    # the captioner's LLM backend was busy
    CLIENT_QUOTA,               # our own queue is full; it drains
})

#: Lease kinds we do NOT wait out. A benchmark books every card for hours and
#: the rig's own advice is to stand down entirely rather than retry into it —
#: so the placeholder becomes an honest ending now instead of a spinner that
#: runs the full deadline and fails anyway.
STAND_DOWN_LEASE_KINDS = frozenset({"benchmark"})


def _retry_policy(code: str, facts: dict) -> tuple[bool, float]:
    """``(retryable, retry_after_s)`` for one coded refusal.

    ``gpu_leased`` is the one code whose answer depends on the payload rather
    than the code: a render or agent lease clears in minutes and is worth
    waiting for, a benchmark lease is a promise to its holder that will still
    be standing when our deadline runs out.
    """
    wait = _positive_float(facts.get("retry_after_s"))
    if code == GPU_LEASED:
        kind = str(facts.get("kind") or "").strip().lower()
        return kind not in STAND_DOWN_LEASE_KINDS, wait
    return code in RETRYABLE_CODES, wait


def _positive_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


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


#: The rig's closed vocabulary for "why not" (ClawForge2 2.2.1). Kept here so
#: an unlisted reason is visibly new rather than quietly mistaken for one of
#: ours — `unknown` is OURS, for a probe that could not be made at all.
CAN_RENDER_REASONS = frozenset({
    "backend_reachable",        # ComfyUI answered and the card is ours to use
    "startable",                # not running, but the next job starts it
    "unmanaged_backend_down",   # nothing answering and ClawForge may not start it
    "comfyui_path_invalid",
    "circuit_open",             # crash-loop breaker tripped on the rig
    "backoff",                  # cooling down after an early exit
    "port_taken",
    "no_suitable_gpu",
    "cards_leased",             # every candidate card is in a foreign lease
})

#: Reasons that mean "submitting now would be refused or wasted". Everything
#: else — including `startable`, which is a cold start and not an outage, and
#: any reason we do not recognise — lets the submit go ahead.
NOT_READY_REASONS = frozenset({
    "unmanaged_backend_down", "comfyui_path_invalid", "circuit_open",
    "backoff", "port_taken", "no_suitable_gpu", "cards_leased",
})

#: How long one readiness answer is reused. Long enough that a sweep over many
#: open jobs costs ONE `comfy_status`, short enough that a lease clearing is
#: noticed within a tick or two of it happening.
READINESS_TTL_S = 20.0


@dataclass
class RigReadiness:
    """``can_render`` and why, off the rig's own ``comfy_status``.

    This replaces reading ``comfy.running``, which was never the question:
    ClawForge unloads ComfyUI's models when idle and starts the backend on the
    next job, so `running: false` is routinely a rig that renders fine (the
    live rig said exactly that while this was written — `running: false`,
    `can_render: true`). The rig now answers the real question itself, and the
    predicate fails OPEN on its side too: an inconclusive probe is `true`.

    So does this one. ``can_render`` is True whenever we could not ask, with
    ``reason`` left at "unknown" — the submit then goes ahead and the rig's own
    refusal is the authority, exactly as it was before this existed.
    """

    can_render: bool = True
    reason: str = "unknown"
    detail: str = ""
    checked_at: float = 0.0
    #: Who is holding the cards and what kind of work it is, when the reason
    #: is `cards_leased`. The same two facts the `[gpu_leased]` refusal
    #: carries, read from `leases.foreign` — so the pre-flight and the submit
    #: refusal reach the SAME verdict about a benchmark instead of the
    #: pre-flight parking a job the submit would have ended honestly.
    lease_holder: str = ""
    lease_kind: str = ""

    @property
    def known(self) -> bool:
        return self.reason != "unknown"

    @property
    def stand_down(self) -> bool:
        """A booking not worth sitting through (see STAND_DOWN_LEASE_KINDS)."""
        return self.leased and self.lease_kind in STAND_DOWN_LEASE_KINDS

    @property
    def reservation(self) -> str:
        who = self.lease_holder or "another job"
        return f"{who} ({self.lease_kind})" if self.lease_kind else who

    @property
    def blocked(self) -> bool:
        """True only when the rig named a reason we know means "not now"."""
        return not self.can_render and self.reason in NOT_READY_REASONS

    @property
    def leased(self) -> bool:
        return self.reason == "cards_leased"

    def as_dict(self) -> dict:
        """The health-safe half: the verdict, the closed-vocabulary reason and
        its age. `detail` is deliberately NOT here — it is a free sentence
        written by the rig and has been seen to name the rig's own ComfyUI
        URL, and /api/health is readable by a locked device. It goes to the
        journal instead, which is where the rest of this module's specifics
        already live."""
        return {"can_render": self.can_render, "reason": self.reason,
                "age_s": (int(time.monotonic() - self.checked_at)
                          if self.checked_at else None)}


@dataclass
class ClawForge:
    """A minimal MCP client for one image server.

    One MCP session, reused across calls. The server we talk to still speaks
    the session-ful transport (a ``tools/call`` with no ``Mcp-Session-Id`` is a
    400 "Missing session ID"), so a session must exist — but there is no
    reason to build a fresh one per call, and on a LAN the handshake is two
    round trips that dwarf the call itself. The thing that made the old
    per-call handshake attractive — never being left holding a dead handle
    across a rig restart — is kept a different way: a session-level refusal
    (400/404) throws the handle away and the call is repeated ONCE on a new
    one. A poll that runs every few seconds for ten minutes therefore
    survives a rig restart with one extra round trip, not a stale session.

    The second thing this owns is a rig-unreachable breaker. Without it a
    sweep over N open jobs against a rig that is down pays N connect timeouts
    IN SERIES (10 s each) every tick — which is how one dead renderer turned
    into a worker that could not keep up with its own placeholders. After a
    transport failure every call for the next ``RIG_BACKOFF_S`` fails fast
    (retryable, so nothing is marked failed); the first call after the window
    is the probe. ``status()`` reports the breaker so /api/health can say
    "the rig is unreachable since …" instead of a bare failure count.
    """

    url: str
    files_url: str = ""
    client_name: str = "dispatch"
    #: Test seam: an ``httpx`` transport to talk to instead of the network.
    transport: Any = None

    _sid: str | None = field(default=None, init=False, repr=False)
    _client: Any = field(default=None, init=False, repr=False)
    _unreachable_until: float = field(default=0.0, init=False, repr=False)
    _unreachable_since: float = field(default=0.0, init=False, repr=False)
    _last_error: str = field(default="", init=False, repr=False)
    #: Sessions started, for tests and for the log line that proves reuse.
    handshakes: int = field(default=0, init=False, repr=False)
    #: The rig's payload contract version, last seen. 0 until it says.
    schema_version: int = field(default=0, init=False, repr=False)
    #: Last answer to "would a job submitted right now run?", and when.
    _readiness: RigReadiness | None = field(default=None, init=False, repr=False)
    #: JSON-RPC request ids. One counter per client, never reused, because the
    #: sweep makes up to `_IMAGE_JOB_CONCURRENCY` calls at once ON ONE SESSION
    #: and the id is the ONLY thing that tells two in-flight answers apart.
    #: See `_next_id` for what a shared id actually did.
    _rpc_id: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.url = (self.url or "").strip()
        if not self.files_url:
            # …/mcp -> …/files/ . The two are served from the same origin by
            # every deployment of this server we know of.
            base = re.sub(r"/mcp/?$", "", self.url)
            self.files_url = f"{base.rstrip('/')}/files/" if base else ""

    # -- plumbing ---------------------------------------------------------- #

    def _http(self) -> httpx.AsyncClient:
        """The one long-lived client (keep-alive + the session id live here)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                transport=self.transport,
                timeout=httpx.Timeout(MCP_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._sid = None

    @staticmethod
    def _timeout(seconds: float) -> httpx.Timeout:
        return httpx.Timeout(seconds, connect=CONNECT_TIMEOUT_S)

    def _next_id(self) -> int:
        """A fresh JSON-RPC id. Never a constant, and never reused.

        This is not hygiene, it is the fix for a wedge measured live against
        ClawForge2 2.2.1 on 2026-09-04. Every ``tools/call`` used to be sent as
        ``"id": 2``, and the sweep sends up to three at once over ONE MCP
        session. The rig matches an answer to a request BY ID, so identical
        ids collide: with three concurrent calls, one coroutine received
        another's answer (a `comfy_status` came back holding a `get_job`
        refusal — proven by body length and by the echoed id) and a second
        connection was closed with its answer unread.

        Inside DisPatch that surfaced as a permanent hang, not an error: the
        sweep never returned, `/api/health` reported `image_jobs` stale, and
        every open render sat at its last percentage until the app was
        restarted. Reproduced twice, then reproduced away by this counter —
        duplicate ids cross-deliver and hang, unique ids answer all three
        correctly.

        The worse outcome it also closes is silent: a cross-delivered poll
        could have handed one job another's ``files_rel``, i.e. the wrong
        picture into the wrong family thread.
        """
        self._rpc_id += 1
        return self._rpc_id

    # Breaker ----------------------------------------------------------------

    def _note_unreachable(self, why: str) -> None:
        now = time.monotonic()
        if not self._unreachable_since:
            self._unreachable_since = now
            log.warning("image server unreachable (%s); backing off %ss between "
                        "probes", why, int(RIG_BACKOFF_S))
        self._unreachable_until = now + RIG_BACKOFF_S
        self._last_error = why

    def _note_reachable(self) -> None:
        if self._unreachable_since:
            log.info("image server reachable again after %ds",
                     int(time.monotonic() - self._unreachable_since))
        self._unreachable_since = 0.0
        self._unreachable_until = 0.0
        self._last_error = ""

    def status(self) -> dict:
        """For /api/health: is the rig answering, and if not, since when.

        Synchronous on purpose — the health route must not make a rig call to
        answer. The readiness block is therefore the LAST one the worker took,
        with its age, rather than a fresh probe.
        """
        down = bool(self._unreachable_since)
        out = {
            "reachable": not down,
            "unreachable_for_s": (int(time.monotonic() - self._unreachable_since)
                                  if down else 0),
            "last_error": self._last_error,
            "session": bool(self._sid),
            # Zero until the rig has told us. It bumps only on a breaking
            # change, so a number that is not 1 is worth a look.
            "schema_version": self.schema_version,
        }
        if self._readiness is not None:
            out["readiness"] = self._readiness.as_dict()
        return out

    def _check_breaker(self) -> None:
        if self._unreachable_until and time.monotonic() < self._unreachable_until:
            raise ImageJobError(
                f"image server unreachable: {self._last_error} (backing off)",
                retryable=True)

    # Session ----------------------------------------------------------------

    _HEADERS: ClassVar[dict[str, str]] = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream"}

    async def _initialize(self, timeout: float) -> str:
        http = self._http()
        init = {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": self.client_name, "version": "2"}},
        }
        r = await http.post(self.url, json=init, headers=self._HEADERS,
                            timeout=self._timeout(timeout))
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        body = _sse_json(r.text)
        if body.get("error") or not sid:
            raise ImageJobError(
                f"image server handshake failed: "
                f"{body.get('error') or 'no session id'}", retryable=True)
        await http.post(self.url, headers={**self._HEADERS, "mcp-session-id": sid},
                        json={"jsonrpc": "2.0",
                              "method": "notifications/initialized"},
                        timeout=self._timeout(timeout))
        self._sid = sid
        self.handshakes += 1
        return sid

    async def call(self, tool: str, args: dict, *,
                   timeout: float = MCP_TIMEOUT_S) -> dict:
        """One ``tools/call``. Returns the raw MCP result object."""
        if not self.url:
            raise ImageJobError("no image server configured")
        self._check_breaker()
        try:
            body: dict = {}
            for attempt in (0, 1):
                sid = self._sid or await self._initialize(timeout)
                # A NEW id per attempt as well as per call: a retried request
                # is a new request on the wire, and the answer to the first one
                # may still be in flight.
                rpc_id = self._next_id()
                req = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}
                r = await self._http().post(
                    self.url, json=req,
                    headers={**self._HEADERS, "mcp-session-id": sid},
                    timeout=self._timeout(timeout))
                if r.status_code in (400, 404) and attempt == 0:
                    # The server no longer knows our session (it restarted, or
                    # expired us). Not a transport failure: the rig is up. Once.
                    log.info("image server dropped session; re-initialising")
                    self._sid = None
                    continue
                r.raise_for_status()
                body = _sse_json(r.text)
                _check_rpc_id(tool, rpc_id, body)
                break
        except ImageJobError:
            raise
        except httpx.TransportError as e:
            # Unreachable / timed out: the rig may be restarting. Worth another
            # poll before the deadline, so mark it retryable — and trip the
            # breaker so the other open jobs do not each pay the same timeout.
            #
            # `uncertain` splits the two shapes that look alike here. A CONNECT
            # failure means the request never left: repeating it is free. A
            # read timeout or a reset means it did leave and the answer was
            # lost, and repeating a SUBMIT then means two renders for one
            # placeholder — one of which nothing will ever poll or cancel.
            why = type(e).__name__
            self._note_unreachable(why)
            self._sid = None
            raise ImageJobError(f"image server unreachable: {why}",
                                retryable=True,
                                uncertain=not isinstance(e, _SAFE_TO_REPEAT))
        except httpx.HTTPStatusError as e:
            # A status, not a blip. 4xx will say exactly the same thing in ten
            # minutes' time, and reporting it as "timed out after 10 minutes"
            # — which is what retrying it to the deadline did — names the
            # wrong cause, late. 408/429 are the two that mean "later".
            status = e.response.status_code
            later = status in (408, 429) or status >= 500
            if later:
                self._note_unreachable(f"HTTP {status}")
            raise ImageJobError(f"image server returned HTTP {status}",
                                retryable=later, uncertain=later)
        except httpx.HTTPError as e:
            raise ImageJobError(f"image server unreachable: {type(e).__name__}",
                                retryable=True, uncertain=True)
        self._note_reachable()
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
            # "Error executing tool generate_image: " is the MCP SDK talking to
            # a developer, and this string's destination is a family chat
            # bubble. Drop the prefix; keep every word the rig itself wrote.
            text = _SDK_ERROR_PREFIX_RE.sub("", text, count=1)
            code = _error_code(res)
            facts = _error_facts(res)
            retryable, wait = _retry_policy(code, facts)
            raise ImageJobError(text or f"{tool} failed (no reason given)",
                                code=code, retryable=retryable,
                                retry_after_s=wait, facts=facts)
        for b in blocks:
            t = (b.get("text") or "").strip()
            if t.startswith("{"):
                try:
                    payload = json.loads(t)
                except json.JSONDecodeError:
                    continue
                self._note_schema_version(tool, payload)
                return payload
        return {}

    def _note_schema_version(self, tool: str, payload: dict) -> None:
        """Record the rig's ``schema_version``, and say so when it moves.

        ClawForge2 2.2.1 stamps it on ``status()``, ``list_jobs``, ``get_job``
        and every generation result, and bumps it ONLY when a field moves, is
        removed or changes type — adding one does not. So a bump is exactly the
        event that breaks a parser in this file, and the whole value of the
        field is that it is visible BEFORE the breakage: the log line and
        ``status()`` are what make "the rig moved a field" a thing an operator
        reads rather than a thing they debug.
        """
        seen = payload.get("schema_version")
        if not isinstance(seen, int) or isinstance(seen, bool):
            return
        if self.schema_version and seen != self.schema_version:
            log.warning("image server schema_version moved %s -> %s (on %s) — "
                        "a field has moved, been removed or changed type; "
                        "check app/image_jobs.py against the rig's release note",
                        self.schema_version, seen, tool)
        self.schema_version = seen

    async def readiness(self, *, max_age_s: float = READINESS_TTL_S
                        ) -> RigReadiness:
        """"Would a job submitted right now be accepted and run?"

        One cached ``comfy_status`` — deliberately WITHOUT
        ``include_recent_jobs``, which is opt-in since 2.2.1 and was 73% of the
        response: this asks three fields' worth of question and should not pull
        the rig's job history across the network to answer it.

        Never raises. A rig that will not answer leaves the previous answer
        standing (or an "unknown" one), because the alternative — treating an
        unreachable rig as "cannot render" — would fail jobs the breaker
        already handles better, one retryable blip at a time.
        """
        cached = self._readiness
        if (cached is not None
                and time.monotonic() - cached.checked_at < max_age_s):
            return cached
        try:
            res = await self._tool_json("comfy_status", {}, timeout=20.0)
        except ImageJobError as e:
            log.debug("readiness probe failed (%s) — assuming the rig can render",
                      e.message)
            return cached or RigReadiness(checked_at=time.monotonic())
        can = res.get("can_render")
        reason = str(res.get("can_render_reason") or "").strip()
        if not isinstance(can, bool) or not reason:
            # A rig that predates `can_render` (or a shape we do not know).
            # Not an outage, and emphatically not a reason to stop submitting.
            return RigReadiness(checked_at=time.monotonic())
        if reason not in CAN_RENDER_REASONS:
            # New vocabulary. Say so once per probe and treat it as inconclusive
            # rather than guessing which side of the gate it belongs on.
            log.info("image server reported an unfamiliar can_render_reason %r "
                     "— treating it as inconclusive", reason[:60])
        holder, kind = _foreign_lease(res)
        ready = RigReadiness(can_render=can, reason=reason,
                             detail=_clip(str(res.get("can_render_detail") or "")),
                             checked_at=time.monotonic(),
                             lease_holder=holder, lease_kind=kind)
        if cached is None or cached.reason != ready.reason:
            log.info("image server can_render=%s (%s): %s",
                     ready.can_render, ready.reason, ready.detail or "—")
        self._readiness = ready
        return ready

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
        """One ``get_job``. Terminal states are reported, not raised.

        ``get_job`` takes no ``client`` argument (checked against the live
        tool schema on 2.2.1) — and does not need one: a job id is exact, and
        preferring it to ``"last"`` is the whole point of tagging the submit.
        """
        try:
            res = await self._tool_json("get_job", {"job_id": job_id})
        except ImageJobError as e:
            # The rig only remembers job ids for the life of ITS process, and
            # it gets restarted — by its maintainers, by the watchdog, by
            # a VRAM self-heal. Every job in flight then fails at
            # once, and what the reader used to get was the rig's developer
            # sentence about which tools mint job ids, clipped mid-word.
            #
            # The discriminator is the id: the refusal quotes back the very id
            # we asked about. That is a fact, not a wording match. (The code is
            # `tool_error` on ClawForge2 2.2.1, not the documented
            # `job_not_found` — probed live 2026-09-04, reported to the rig.)
            if e.code in ("job_not_found", "tool_error") and job_id in str(e):
                raise ImageJobError(
                    "the image server restarted and lost this render",
                    code="job_lost") from None
            raise
        rel = _first_rel(res)
        err = str(res.get("error") or "").strip()
        state = str(res.get("state") or "").strip() or "unknown"
        done = bool(rel) or bool(res.get("done")) or state in ("failed", "cancelled")
        if state in ("failed", "cancelled") and not err:
            err = str(res.get("note") or f"render {state}")
        return PollResult(state=state, done=done, files_rel=rel,
                          error=_clip(err), seed=_first_seed(res),
                          progress=_progress(res), code=_job_error_code(res, err))

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
        parts = files_rel.replace("\\", "/").split("/")
        if ".." in parts or "" in parts[1:] or "://" in files_rel:
            # The path came from the rig's own answer, but a rig that has been
            # replaced by something hostile must not be able to walk us off
            # its /files/ tree or onto another host.
            raise ImageJobError("the rig named a file outside its files area")
        url = self.files_url.rstrip("/") + "/" + files_rel.lstrip("/")
        try:
            async with self._http().stream(
                    "GET", url, timeout=self._timeout(FETCH_TIMEOUT_S)) as resp:
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


def _check_rpc_id(tool: str, sent: int, body: dict) -> None:
    """Refuse an answer that is not the answer to the question we asked.

    The id is the only thing distinguishing two in-flight calls on one MCP
    session, so a mismatch means somebody else's result is in our hands. The
    honest move is to treat that as a transport failure and poll again —
    NEVER to act on it, because the payload this pipeline reads off a poll is
    ``files_rel``, and acting on another job's ``files_rel`` puts the wrong
    picture in the wrong thread.

    Retryable on purpose: a mismatch is a race, and the next poll is clean.
    Tolerant of a server that omits the id (some send only a result) — this
    guards against the WRONG id, not against a missing one.
    """
    got = body.get("id")
    if got is None or got == sent:
        return
    log.warning("image server answered %s with id %r, not %r — discarding it "
                "rather than acting on another call's result", tool, got, sent)
    raise ImageJobError("the image server's answers crossed over; retrying",
                        retryable=True)


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


def _foreign_lease(status: dict) -> tuple[str, str]:
    """``(holder_family, kind)`` of the first lease somebody ELSE is holding.

    ``leases.foreign`` already excludes ours BY LEASE ID (not by holder name),
    so everything in it is genuinely somebody else's. The distinction the rig
    is careful about is kept here too: ``[]`` means "asked, nothing standing",
    ``null`` means "could not ask" — both leave us with no facts, and neither
    is an occasion to invent one.
    """
    leases = status.get("leases")
    foreign = leases.get("foreign") if isinstance(leases, dict) else None
    if not isinstance(foreign, list):
        return "", ""
    for lease in foreign:
        if not isinstance(lease, dict):
            continue
        holder = str(lease.get("holder_family") or lease.get("holder") or "")
        kind = str(lease.get("kind") or "").strip().lower()
        if holder or kind:
            return _clip(holder, 60), kind[:32]
    return "", ""


#: Fields the rig attaches to a coded refusal that are worth keeping. A lease
#: refusal carries who holds it, what family they are, what KIND of work it is
#: (the field the retry decision turns on) and when it ends.
_FACT_KEYS = ("holder", "holder_family", "kind", "expires_at", "expires_in_s",
              "reason", "retry_after_s", "devices")


def _error_facts(res: dict) -> dict:
    """The scalars the rig hung on a refusal, from wherever it hung them.

    ``structuredContent.error`` is the contract; whether the lease block sits
    flat on it or nested under a key (``lease``, and StudioForge's sibling
    envelope nests under its own name) is not something this file should be
    brittle about. One level of nesting is searched, a flat key wins over a
    nested one, and anything that is not a scalar is dropped — nothing here is
    ever executed or trusted, it only chooses wording and one retry decision.
    """
    sc = res.get("structuredContent")
    err = sc.get("error") if isinstance(sc, dict) else None
    if not isinstance(err, dict):
        return {}
    out: dict[str, Any] = {}
    for source in ([v for v in err.values() if isinstance(v, dict)] + [err]):
        for key in _FACT_KEYS:
            v = source.get(key)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[key] = v if not isinstance(v, str) else _clip(v, 120)
    return out


#: The MCP SDK's own wrapper around a tool failure. Stripped before the rig's
#: sentence is shown to anyone: it names internal tool names and helps nobody
#: reading a chat thread.
_SDK_ERROR_PREFIX_RE = re.compile(r"^Error executing tool [A-Za-z0-9_]+:\s*")

#: A coded job failure reads ``[stalled] the render moved and then …``. This
#: is a prose parse and is deliberately the SECOND choice: an explicit
#: ``error_code`` on the payload wins. It is safe where the same parse on an
#: ``isError`` block is not, because a job's stored error string carries no
#: SDK "Error executing tool …:" prefix — the bracket is the first thing in it.
_JOB_CODE_RE = re.compile(r"^\[([a-z][a-z0-9_]{1,40})\]")


def _job_error_code(res: dict, error_text: str = "") -> str:
    for key in ("error_code", "code"):
        v = res.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:64]
    m = _JOB_CODE_RE.match((error_text or "").strip())
    return m.group(1) if m else ""


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

#: See ImageSpec.__post_init__ — a caption is prose, and prose does not need
#: the two characters that close a media directive early.
_CAPTION_BRACKETS_RE = re.compile(r"[\[\]]")


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
        # Square brackets are REMOVED from the caption, not rejected, and it
        # happens here so that every path — the inline marker, the endpoint,
        # a spec read back off a row — is covered by one rule.
        #
        # The caption's destiny is a `[[media:<path>|<caption>]]` directive
        # whose grammar cannot express a `]` inside the caption group. A
        # caption with one in it made the finished directive fail to match,
        # which made the delivery test fail, which DELETED a rendered image
        # and told the family "the image could not be stored" — a correct
        # picture destroyed, blamed on the disk, by a regex. `[[pic:a bar
        # chart|panel 5] of 6]]` is all it took, and a bot writing "panel 5]"
        # has done nothing wrong. Neither bracket survives; a caption is prose.
        self.caption = _CAPTION_BRACKETS_RE.sub(
            "", (self.caption or "")).strip()[:MAX_CAPTION_CHARS]
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
    #: The rig's code for a failed render, when it gave one — `stalled` is the
    #: one this pipeline branches on (a job that moved and then went quiet is
    #: worth one more attempt; a job that never moved is not).
    code: str = ""


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


#: Markers that asked for a picture and got none (invalid, over the per-message
#: cap, rate limited). A SEPARATE number from the failures above, because they
#: are a different problem with a different owner: a failure is the rig going
#: wrong, a drop is a bot writing markers the pipeline cannot honour. Same
#: count-only rule — the reasons stay in the journal and the chat sub row.
_MARKER_DROPS: list[dict] = []


def note_marker_drop(reason: str) -> None:
    _MARKER_DROPS.append({"at": time.time(), "reason": str(reason)[:200]})
    del _MARKER_DROPS[:-_FAILURES_MAX]


def marker_drop_stats(window_s: float = 24 * 3600.0) -> dict:
    cutoff = time.time() - window_s
    return {"drops_24h": len([d for d in _MARKER_DROPS if d["at"] >= cutoff])}


def reset_marker_drops() -> None:
    _MARKER_DROPS.clear()


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
#:
#: Whitespace around `pic` is tolerated because `[[ pic:a cat]]` is a shape
#: models actually produce and there is nothing else it could mean — refusing
#: it only ever meant printing the syntax at the family instead of a picture.
PIC_MARKER_RE = re.compile(r"\[\[\s*pic\s*:([^\n]*?)\]\]", re.IGNORECASE)

#: The same opening, used as a cheap "is there anything to do here" test.
_PIC_HINT_RE = re.compile(r"\[\[\s*pic\s*:", re.IGNORECASE)

#: An opening that never closes on its own line: a reply cut off by a token
#: cap, a marker the model wrapped across a newline, a nested one whose tail
#: was eaten. The lookahead is what keeps this from touching a COMPLETE
#: marker, so a documented `[[pic:example]]` in backticks still survives.
#:
#: Applied to the whole text, code regions included, and unconditionally —
#: every guard above may decide not to render, but none of them is a reason to
#: print internal syntax into a family thread. (A complete marker inside an
#: UNCLOSED ``` fence is the one case left standing: it is indistinguishable
#: from documentation, and it renders as code rather than as prose.)
_PIC_REMNANT_RE = re.compile(r"\[\[\s*pic\s*:(?![^\n]*\]\])[^\n]*",
                             re.IGNORECASE)

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

    A marker that never closes is a different animal and is removed EVERYWHERE
    (see :data:`_PIC_REMNANT_RE`): it renders nothing, it is never
    documentation, and leaving it produced the worst outcome available —
    `here you go [[pic:a blue teapot` sitting in the family thread, on the
    locked tablets too, with no picture.
    """
    if not content or not _PIC_HINT_RE.search(content):
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

    walked = openclaw_text.sub_outside_code(content, _clean)
    if _PIC_REMNANT_RE.search(walked):
        log.info("stripped an unterminated [[pic: marker from a reply")
    cleaned = _strip_remnants(walked)
    if dropped:
        log.info("dropped %d [[pic:…]] marker(s) — empty prompt or over the "
                 "%d-per-message cap", dropped, MAX_PIC_MARKERS_PER_MESSAGE)
    return cleaned, found


def _strip_remnants(text: str) -> str:
    """Remove unterminated `[[pic:` openings and tidy what they leave.

    Shared by both entry points below so that ``strip_pic_markers`` stays
    byte-identical to ``extract_pic_markers(...)[0]`` — the property the dedup
    key depends on, and the one that once re-posted a reply five times when it
    slipped. Pure and silent for that reason — the caller does the logging.
    """
    if not _PIC_REMNANT_RE.search(text):
        return text.strip()
    text = _PIC_REMNANT_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # A remnant eaten off the end of a line leaves the space in front of it
    # behind ("before [[pic:x" -> "before "), which is invisible in a bubble
    # and very visible in a dedup key.
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_pic_markers(content: str) -> str:
    """The marker-free text, with no side effects and no logging.

    Byte-identical to ``extract_pic_markers(content)[0]``. Dedup keys need
    exactly this: a canonical key must mirror every transform persisting
    applies, and computing one must not log.
    """
    if not content or not _PIC_HINT_RE.search(content):
        return content

    def _clean(segment: str) -> str:
        segment = PIC_MARKER_RE.sub("", segment)
        segment = re.sub(r"[ \t]{2,}", " ", segment)
        return re.sub(r"\n{3,}", "\n\n", segment)

    return _strip_remnants(openclaw_text.sub_outside_code(content, _clean))
