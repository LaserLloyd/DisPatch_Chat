"""The ``/ws/voice/{thread_id}`` endpoint, minus the app-specific lookups.

main.py owns the route decorator and passes in the few things only it knows
(the auth module, the cookie name, how to find a thread's bot, how to submit a
message). Everything else — the gate order, the frame tap, model loading —
lives here, where it can be tested without importing the whole app.

THE GATE (all of it runs before ``accept``; a refused handshake never gets a
socket):

1. Feature on (``DISPATCH_VOICE``) and models present.
2. CSWSH: a present Origin must positively equal Host — the same rule /ws
   applies. HTTP middleware does not run for the websocket scope, which is
   why this is inline and not left to the app's middleware.
3. Unlocked tier only. With a PIN set, the ``lc_session`` cookie must name a
   live full session. A Safe-Mode / locked device is refused outright — there
   is no "redacted" voice mode. (With NO PIN configured the whole app is
   unlocked by definition, and voice follows it.)
4. The thread exists and its bot is not a Safe-Mode (``safe``) bot. Family-safe
   bots get no voice unless the owner changes this rule.

During the session the cookie's session is re-checked about once a second; an
expired or revoked session gets a ``locked`` frame and the socket closes.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request

from .config import VoiceSettings, load_settings

log = logging.getLogger("dispatch.voice")

#: Close code for "not allowed" (policy violation), same as /ws uses.
POLICY = 1008
#: Close code for "the server can't do voice right now" (feature off/missing).
UNAVAILABLE = 1013


@dataclass
class GateResult:
    ok: bool
    code: int = POLICY
    reason: str = ""
    token: str | None = None


def origin_ok(headers) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True          # non-browser client; CSWSH is a browser-only vector
    origin_host = urlparse(origin).netloc
    host = headers.get("host", "")
    return bool(origin_host) and bool(host) and origin_host == host


async def check_gate(ws, thread_id: str, *, settings: VoiceSettings, auth,
                     cookie_name: str,
                     thread_bot: Callable[[str], Awaitable[object | None]]) -> GateResult:
    """Decide whether this handshake may become a voice session.

    ``auth`` is app.auth (``load()`` → cfg with ``pin_set``; ``get_session``).
    ``thread_bot(thread_id)`` returns the thread's bot config (with ``.safe``)
    or None when the thread or bot does not exist.
    """
    if not settings.enabled:
        return GateResult(False, UNAVAILABLE, "voice disabled")
    if not origin_ok(ws.headers):
        return GateResult(False, POLICY, "cross-origin")
    cfg = auth.load()
    token = None
    if cfg.pin_set:
        session = auth.get_session(ws.cookies.get(cookie_name))
        if session is None:
            return GateResult(False, POLICY, "locked")
        token = session.token
    bot = await thread_bot(thread_id)
    if bot is None:
        return GateResult(False, POLICY, "no such thread")
    if getattr(bot, "safe", False):
        return GateResult(False, POLICY, "safe bot")
    if settings.missing():
        return GateResult(False, UNAVAILABLE, "voice models missing")
    return GateResult(True, token=token)


class FrameTap:
    """A duck-typed WebSocket that the app's ConnectionManager can broadcast to.

    Registering it with ``manager.connect(tap, decoy=False, token=<session>)``
    means the voice session sees exactly the frames an unlocked browser tab of
    the same session would — including the manager's own Safe-Mode redaction
    the instant that session lapses. Sends never block: frames go straight to
    the session's bounded queue.
    """

    def __init__(self, on_frame: Callable[[dict], None]) -> None:
        self._on_frame = on_frame

    async def accept(self) -> None:  # ConnectionManager.connect calls this
        return None

    async def send_json(self, frame: dict) -> None:
        try:
            self._on_frame(frame)
        except Exception:
            log.exception("voice tap: frame handler failed")

    async def close(self, code: int = 1000) -> None:
        return None


async def warm_models(settings: VoiceSettings):
    """Load (once per process) and return (stt_engine, tts_engine)."""
    from . import stt, tts
    return await asyncio.gather(asyncio.to_thread(stt.shared_engine, settings),
                                asyncio.to_thread(tts.shared_engine, settings))


async def serve_voice(ws, thread_id: str, *, auth, cookie_name: str, manager,
                      thread_bot: Callable[[str], Awaitable[object | None]],
                      submit: Callable[[str, str], Awaitable[str | None]],
                      abort: Callable[[str], Awaitable[None]] | None = None,
                      retract: Callable[[str, str], Awaitable[None]] | None = None,
                      replies_after=None,
                      settings: VoiceSettings | None = None,
                      engines: tuple | None = None,
                      detector=None,
                      voice_for_bot: Callable[[str | None], tuple[str | None, list]] | None = None) -> None:
    """The whole endpoint body. ``engines``/``detector``/``voice_for_bot``
    are test seams; production picks the bot's assigned voice profile."""
    settings = settings or load_settings()
    gate = await check_gate(ws, thread_id, settings=settings, auth=auth,
                            cookie_name=cookie_name, thread_bot=thread_bot)
    if not gate.ok:
        log.info("voice: refused %s (%s)", thread_id, gate.reason)
        await ws.close(code=gate.code)
        return

    token = gate.token
    slot = token or ""
    if _OPEN.get(slot, 0) >= MAX_SOCKETS_PER_SESSION:
        log.info("voice: refused %s (session already has %d voice sockets)",
                 thread_id, _OPEN[slot])
        await _close_unavailable(ws)
        return
    _OPEN[slot] = _OPEN.get(slot, 0) + 1
    try:
        await _serve_gated(ws, thread_id, token=token, auth=auth, manager=manager,
                           thread_bot=thread_bot, submit=submit, abort=abort,
                           retract=retract, replies_after=replies_after,
                           settings=settings, engines=engines, detector=detector,
                           voice_for_bot=voice_for_bot)
    finally:
        _OPEN[slot] -= 1
        if _OPEN[slot] <= 0:
            _OPEN.pop(slot, None)


async def _close_unavailable(ws) -> None:
    """Refuse an ALREADY-AUTHORISED caller with 1013. Accepted first: a close
    before accept reaches a browser as a bare 1006, and the client's
    "stop after N unavailable" rule (drive.js reconnectDecision) needs the
    real code. Gate refusals (1008, and anything before the auth check) stay
    pre-accept — an unauthorised caller never gets a socket."""
    with contextlib.suppress(Exception):
        await ws.accept()
    await ws.close(code=UNAVAILABLE)


#: Concurrent voice sockets one unlocked session (or the PIN-less install) may
#: hold. Each one loads the CPU with VAD + STT + TTS; two covers a reconnect
#: overlapping the socket it replaces.
MAX_SOCKETS_PER_SESSION = 2
_OPEN: dict[str, int] = {}


async def _serve_gated(ws, thread_id: str, *, token, auth, manager, thread_bot,
                       submit, abort, retract, replies_after, settings, engines,
                       detector, voice_for_bot) -> None:
    from .session import VoiceHost, VoiceSession

    def session_live() -> bool:
        cfg = auth.load()
        if token is None:
            return not cfg.pin_set          # opened with no PIN; a PIN since = locked
        return auth.get_session(token) is not None

    if engines is None:
        try:
            engines = await warm_models(settings)
        except Exception:
            log.exception("voice: model load failed")
            await _close_unavailable(ws)
            return
    stt_engine, tts_engine = engines
    if detector is None:
        from .vad_turn import build_detector
        detector = await asyncio.to_thread(build_detector, settings)

    bot = await thread_bot(thread_id)
    try:
        profile_id, acks = await asyncio.to_thread(
            voice_for_bot or (lambda b: _voice_for_bot(settings, tts_engine, b)),
            getattr(bot, "id", None))
    except Exception:
        log.exception("voice: voice profile lookup failed; using the default voice")
        profile_id, acks = None, []

    await ws.accept()
    def touch() -> None:
        # The main /ws slides the idle window on every real action; a drive
        # sends no /ws actions for as long as it lasts, so without this the
        # session idle-locked mid-sentence.
        slide = getattr(auth, "touch_session", None)
        if token is not None and slide is not None:
            slide(token)

    host = VoiceHost(submit=submit, session_live=session_live, abort=abort,
                     retract=retract, replies_after=replies_after, touch=touch)
    session = VoiceSession(ws, thread_id=thread_id, settings=settings,
                           detector=detector, stt_engine=stt_engine,
                           tts_engine=tts_engine, host=host, profile_id=profile_id, acks=acks)
    tap = FrameTap(session.on_app_frame)
    await manager.connect(tap, decoy=False, token=token)
    try:
        await session.run()
    except Exception as e:
        if type(e).__name__ != "WebSocketDisconnect":
            log.exception("voice: session error")
    finally:
        await manager.disconnect(tap)
        with contextlib.suppress(Exception):
            await ws.close()




#: Short acknowledgements, rendered once per voice per process in the
#: background. The list objects are shared with sessions and fill in place, so
#: no session ever waits for them; an extension's pre-rendered acks win.
ACK_LINES = ("Mm-hm.", "Right.", "Okay, sure.", "Got it.", "Let me see.", "One sec.")
_ACKS: dict[str, list] = {}
_ACKS_LOCK = __import__("threading").Lock()


def _voice_for_bot(settings: VoiceSettings, tts_engine, bot_id: str | None):
    """(profile_id for the TTS engine, acknowledgement clips) for this bot."""
    from . import registry
    pid = registry.resolve_profile(bot_id)
    ext = registry.acks_for(pid)
    if ext:
        return pid, ext
    key = pid or ""
    with _ACKS_LOCK:
        acks = _ACKS.get(key)
        if acks is not None:
            return pid, acks
        acks = _ACKS[key] = []

    def render():
        for line in ACK_LINES:
            try:
                pcm, _ = tts_engine.synth(line, pid)
                acks.append(pcm)
            except Exception:
                log.exception("voice: ack render failed")
                return

    import threading
    threading.Thread(target=render, name="voice-acks", daemon=True).start()
    return pid, acks


# --------------------------------------------------------------------------- #
# HTTP: the status probe + extension mounting
# --------------------------------------------------------------------------- #

def mount_http(app, require_full) -> list[str]:
    """Called once by main.py at import time.

    * ``GET /api/voice/status`` — is Drive mode on (the frontend shows the
      button only when it is) and what is missing. Unlocked tier only.
    * ``dispatch-voice`` when installed (app/voice/dv.py): the Voices panel
      at ``/api/voices`` (+ ``/api/voices/ui``) and the cloned-voice engine.
    * Every extension in ``DISPATCH_VOICE_EXTENSIONS`` (registry.py), mounted
      behind the same unlocked-tier gate.
    """
    from . import registry

    def gate(request: Request) -> None:
        require_full(request)

    r = APIRouter(prefix="/api/voice", dependencies=[Depends(gate)])

    @r.get("/status")
    async def voice_status():
        st = load_settings().status()
        st["extensions"] = list(_LOADED)
        st["voices_ui"] = "/api/voices/ui" if "dispatch-voice" in _LOADED else None
        return st

    app.include_router(r)
    loaded: list[str] = []
    try:
        from . import dv
        if dv.install(app, require_full):
            loaded.append("dispatch-voice")
    except Exception:
        log.exception("voice: dispatch-voice failed to mount; built-in voice only")
    loaded += registry.mount_extensions(app, require_full)
    _LOADED[:] = loaded
    return list(_LOADED)


_LOADED: list[str] = []
