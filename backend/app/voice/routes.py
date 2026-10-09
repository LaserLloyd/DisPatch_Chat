"""The ``/ws/voice/{thread_id}`` endpoint, minus the app-specific lookups.

main.py owns the route decorator and passes in the few things only it knows
(the auth module, the cookie name, how to find a thread's bot, how to submit a
message). Everything else — the gate order, the frame tap, model loading —
lives here, where it can be tested without importing the whole app.

THE GATE (all of it runs before ``accept``; a refused handshake never gets a
socket):

1. CSWSH: a present Origin must positively equal Host — the same rule /ws
   applies. HTTP middleware does not run for the websocket scope, which is
   why this is inline and not left to the app's middleware.
2. Unlocked tier only. With a PIN set, the ``lc_session`` cookie must name a
   live full session. A Safe-Mode / locked device is refused outright — there
   is no "redacted" voice mode. (With NO PIN configured the whole app is
   unlocked by definition, and voice follows it.)
3. Feature on (``DISPATCH_VOICE``).
4. The thread exists and its bot is not a Safe-Mode (``safe``) bot. Family-safe
   bots get no voice unless the owner changes this rule.
5. Models present.

Refusals at 1, 2 and 4 close with 1008 BEFORE accept. "Unavailable" (3, 5, a
model load failure) is only ever told to a caller that passed 1 and 2, and it
is accepted first and then closed with 1013, because a close before accept
reaches a browser as a bare 1006 and the client must tell "voice isn't set up"
from a network drop.

After the gate: accept, then load the models (instant when the start-up
prewarm already ran), then ``ready``. One unlocked session holds at most
``MAX_SOCKETS_PER_SESSION`` voice sockets; a newer one REPLACES the oldest,
which is closed with 4001 "replaced" (a half-open phone socket must never lock
the driver out — review H2).

During the session the cookie's session is re-checked on every frame (at most
once a second) and on a timer when the socket is silent; an expired or revoked
session gets a ``locked`` frame and the socket closes with 1008.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import threading
import wave
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from .config import SPEED_MAX, SPEED_MIN, THINKING_LEVELS, VoiceSettings, load_settings

log = logging.getLogger("dispatch.voice")

#: Close code for "not allowed" (policy violation), same as /ws uses.
POLICY = 1008
#: Close code for "the server can't do voice right now" (feature off/missing).
UNAVAILABLE = 1013
#: Close code for "a newer voice socket of the same session took over".
REPLACED = 4001


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
    if not origin_ok(ws.headers):
        return GateResult(False, POLICY, "cross-origin")
    cfg = auth.load()
    token = None
    if cfg.pin_set:
        session = auth.get_session(ws.cookies.get(cookie_name))
        if session is None:
            return GateResult(False, POLICY, "locked")
        token = session.token
    # Only an authorised caller learns that voice is off (and gets the 1013
    # an accept-then-close can carry); everyone else got 1008 above.
    if not settings.enabled:
        return GateResult(False, UNAVAILABLE, "voice disabled", token)
    bot = await thread_bot(thread_id)
    if bot is None:
        return GateResult(False, POLICY, "no such thread")
    if getattr(bot, "safe", False):
        return GateResult(False, POLICY, "safe bot")
    if settings.missing():
        return GateResult(False, UNAVAILABLE, "voice models missing", token)
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


@dataclass
class _Slot:
    """One open voice socket of an unlocked session."""
    ws: object
    session: object | None = None
    evicted: bool = False

    async def evict(self) -> None:
        self.evicted = True
        if self.session is not None:
            self.session.end(REPLACED, "replaced")
            # Wake run() even when the client is gone (half-open): the close
            # frame below may never be answered.
        with contextlib.suppress(Exception):
            await self.ws.close(code=REPLACED, reason="replaced")


#: Concurrent voice sockets one unlocked session (or the PIN-less install) may
#: hold. Each one loads the CPU with VAD + STT + TTS. A newer socket replaces
#: the oldest (4001), so a dead one never locks the driver out.
MAX_SOCKETS_PER_SESSION = 2
_OPEN: dict[str, list[_Slot]] = {}


def _open_count(slot: str) -> int:
    v = _OPEN.get(slot)
    return len(v) if isinstance(v, list) else int(v or 0)


async def serve_voice(ws, thread_id: str, *, auth, cookie_name: str, manager,
                      thread_bot: Callable[[str], Awaitable[object | None]],
                      submit: Callable[..., Awaitable[str | None]],
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
        if gate.code == UNAVAILABLE:
            await _close_unavailable(ws, gate.reason)      # authorised: accept first
        else:
            await ws.close(code=gate.code)                 # never a socket
        return

    token = gate.token
    key = token or ""
    slots = _OPEN.setdefault(key, [])
    if not isinstance(slots, list):          # tests may seed a bare count
        slots = _OPEN[key] = [_Slot(None, evicted=True) for _ in range(int(slots))]
    while len(slots) >= MAX_SOCKETS_PER_SESSION:
        old = slots.pop(0)
        log.info("voice: %s replaces an older voice socket of the same session", thread_id)
        await old.evict()
    me = _Slot(ws)
    slots.append(me)
    try:
        await _serve_gated(ws, thread_id, token=token, auth=auth, manager=manager,
                           thread_bot=thread_bot, submit=submit, abort=abort,
                           retract=retract, replies_after=replies_after,
                           settings=settings, engines=engines, detector=detector,
                           voice_for_bot=voice_for_bot, slot=me)
    finally:
        cur = _OPEN.get(key)
        if isinstance(cur, list):
            with contextlib.suppress(ValueError):
                cur.remove(me)
            if not cur:
                _OPEN.pop(key, None)


async def _close_unavailable(ws, reason: str = "") -> None:
    """Refuse an ALREADY-AUTHORISED caller with 1013. Accepted first: a close
    before accept reaches a browser as a bare 1006, and the client needs the
    real code to say "Voice isn't set up on this server" instead of retrying.
    Gate refusals (1008) stay pre-accept — an unauthorised caller never gets a
    socket."""
    with contextlib.suppress(Exception):
        await ws.accept()
    with contextlib.suppress(Exception):
        await ws.close(code=UNAVAILABLE, reason=(reason or "voice unavailable")[:120])


async def _serve_gated(ws, thread_id: str, *, token, auth, manager, thread_bot,
                       submit, abort, retract, replies_after, settings, engines,
                       detector, voice_for_bot, slot: _Slot | None = None) -> None:
    from .session import VoiceHost, VoiceSession

    def session_live() -> bool:
        cfg = auth.load()
        if token is None:
            return not cfg.pin_set          # opened with no PIN; a PIN since = locked
        return auth.get_session(token) is not None

    # Accept FIRST (the gate passed): the client gets an open socket at once
    # and buffers the mic until `ready`. With the start-up prewarm the loads
    # below are cache hits; on a cold process they take a few seconds.
    await ws.accept()
    if engines is None:
        try:
            engines = await warm_models(settings)
        except Exception:
            log.exception("voice: model load failed")
            with contextlib.suppress(Exception):
                await ws.close(code=UNAVAILABLE, reason="voice models failed to load")
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
    if slot is not None and slot.evicted:
        return                               # replaced while loading

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
                           tts_engine=tts_engine, host=host, profile_id=profile_id, acks=acks,
                           notice=lambda text: notice_pcm(tts_engine, profile_id, text))
    if slot is not None:
        slot.session = session
    tap = FrameTap(session.on_app_frame)
    await manager.connect(tap, decoy=False, token=token)
    try:
        await session.run()
    except Exception as e:
        if type(e).__name__ != "WebSocketDisconnect":
            log.exception("voice: session error")
    finally:
        await manager.disconnect(tap)
        code, reason = session.close_code
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=reason or None)


#: Short acknowledgements, rendered once per voice per process in the
#: background. The list objects are shared with sessions and fill in place, so
#: no session ever waits for them; an extension's pre-rendered acks win.
ACK_LINES = ("Mm-hm.", "Right.", "Okay, sure.", "Got it.", "Let me see.", "One sec.")
_ACKS: dict[str, list] = {}
_ACKS_LOCK = __import__("threading").Lock()


def _cache_dir(settings: VoiceSettings, tts_engine, profile_id: str | None):
    """Where a BUILT-IN voice's renders are kept across restarts, or None.

    Only the built-in (Kokoro) voice is cached on disk: its output is a pure
    function of (model, voice, speed, text). Cloned voices keep their own
    pre-rendered fillers in the profile store."""
    from pathlib import Path

    from . import tts as _tts
    k = _tts.kokoro_of(tts_engine)
    if k is None:
        return None
    if profile_id and not _tts.builtin_id(profile_id):
        return None                        # a cloned profile, not Kokoro
    voice = _tts.builtin_id(profile_id) or k.voice
    from .config import _data_dir
    tag = f"kokoro-{settings.kokoro_variant}-{voice}-{k.speed:.3f}"
    return Path(_data_dir()) / "voice-cache" / tag


def _render_cached(settings, tts_engine, profile_id, text: str, *, wait_idle: bool):
    """int16 PCM of ``text`` in this voice: disk cache → synthesis."""
    from . import tts as _tts
    d = _cache_dir(settings, tts_engine, profile_id)
    f = None
    if d is not None:
        f = d / (hashlib.sha256(text.encode()).hexdigest()[:24] + ".npy")
        with contextlib.suppress(Exception):
            return np.load(f).astype("<i2")
    if wait_idle:
        _tts.REPLY_ACTIVITY.wait_idle()
    pcm, _ = tts_engine.synth(text, profile_id)
    pcm = np.asarray(pcm, "<i2")
    if f is not None and len(pcm):
        with contextlib.suppress(Exception):
            d.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp.npy")
            np.save(tmp, pcm)
            tmp.replace(f)
    return pcm


def _voice_for_bot(settings: VoiceSettings, tts_engine, bot_id: str | None):
    """(profile_id for the TTS engine, acknowledgement clips) for this bot.

    Acks render in the background, one line at a time and only while no
    reply is being synthesised (review M2), from the disk cache when the
    built-in voice already said them once."""
    from . import registry
    pid = registry.resolve_profile(bot_id)
    ext = registry.acks_for(pid)
    if ext:
        # A cloned voice's error lines render on demand (~2 s, only when a
        # turn actually fails): pre-rendering them held the Chatterbox lock
        # for ~6 s right when the first reply wanted it.
        return pid, ext
    k = _cache_dir(settings, tts_engine, pid)
    key = f"{pid or ''}|{k or ''}"
    with _ACKS_LOCK:
        acks = _ACKS.get(key)
        if acks is not None:
            return pid, acks
        acks = _ACKS[key] = []

    def render():
        for line in ACK_LINES:
            try:
                acks.append(_render_cached(settings, tts_engine, pid, line, wait_idle=True))
            except Exception:
                log.exception("voice: ack render failed")
                with _ACKS_LOCK:
                    _ACKS.pop(key, None)       # try again next session
                return
        _render_notices(tts_engine, pid)

    threading.Thread(target=render, name="voice-acks", daemon=True).start()
    return pid, acks


def _render_notices(tts_engine, pid) -> None:
    for line in _NOTICE_LINES():
        with contextlib.suppress(Exception):
            notice_pcm(tts_engine, pid, line, wait_idle=True)


def _NOTICE_LINES():
    from .session import NOTICE_LINES
    return list(NOTICE_LINES.values())


_NOTICES: OrderedDict[tuple, np.ndarray] = OrderedDict()
_NOTICES_LOCK = threading.Lock()


def notice_pcm(tts_engine, profile_id: str | None, text: str, *, wait_idle: bool = False):
    """A spoken error line ("Sorry, that didn't go through.") in the
    session's voice: memory → disk (built-in voice) → synthesis."""
    settings = load_settings()
    key = (id(tts_engine), profile_id or "", str(_cache_dir(settings, tts_engine, profile_id)), text)
    with _NOTICES_LOCK:
        if key in _NOTICES:
            return _NOTICES[key]
    pcm = _render_cached(settings, tts_engine, profile_id, text, wait_idle=wait_idle)
    with _NOTICES_LOCK:
        _NOTICES[key] = pcm
        while len(_NOTICES) > 64:
            _NOTICES.popitem(last=False)
    return pcm


# --------------------------------------------------------------------------- #
# Start-up prewarm (review H5)
# --------------------------------------------------------------------------- #

_PREWARM: dict[str, object] = {"state": "idle"}


def _profile_to_warm(registry, tts) -> str | None:
    """A cloned voice some bot will actually speak with: the default profile,
    else the first bot's own profile. None = only built-in voices in use."""
    pid = registry.resolve_profile(None)
    if pid and not tts.builtin_id(pid):
        return pid
    try:
        from app import config
        for bot in config.load_bots():
            if getattr(bot, "safe", False):
                continue
            pid = registry.resolve_profile(bot.id)
            if pid and not tts.builtin_id(pid):
                return pid
    except Exception:
        log.exception("voice: looking for a voice profile to warm failed")
    return None


def prewarm(settings: VoiceSettings | None = None) -> bool:
    """Load every speech model and run one throwaway synthesis, so the first
    Drive session after a restart accepts fast and its first reply does not
    pay the engine load (~4.5 s for the cloned voice). Blocking: main.py runs
    it in a background thread at start-up. Returns False when voice is off."""
    settings = settings or load_settings()
    if not settings.enabled or not settings.prewarm or settings.missing():
        _PREWARM["state"] = "skipped"
        return False
    import time
    t0 = time.monotonic()
    _PREWARM["state"] = "running"
    try:
        from . import registry, stt, tts, vad_turn
        stt_engine = stt.shared_engine(settings)
        eng = tts.shared_engine(settings)
        vad_turn._shared_smart_turn(settings)
        vad_turn.build_detector(settings)          # Silero + ORT kernels
        stt_engine.transcribe(np.zeros(16000, np.float32))
        k = tts.kokoro_of(eng)
        if k is not None:
            k.synth("Okay.")
        pid = _profile_to_warm(registry, tts)
        if pid:
            try:
                from . import dv
                dv.store().engine.warm()
                eng.synth("Okay.", pid)
            except Exception:
                log.exception("voice: cloned-voice warm-up failed")
        _voice_for_bot(settings, eng, None)        # default acks, in the background
        _PREWARM["state"] = "done"
        _PREWARM["seconds"] = round(time.monotonic() - t0, 1)
        log.info("voice: speech models warm in %.1f s", time.monotonic() - t0)
        return True
    except Exception:
        _PREWARM["state"] = "failed"
        log.exception("voice: prewarm failed; models will load on the first session")
        return False


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
        settings = load_settings()
        st = settings.status()
        st["extensions"] = list(_LOADED)
        st["voices_ui"] = "/api/voices/ui" if "dispatch-voice" in _LOADED else None
        st.update(await asyncio.to_thread(_voice_choices, settings))
        st["warm"] = _PREWARM.get("state") == "done"
        return st

    @r.get("/settings")
    async def get_voice_settings():
        settings = load_settings()
        ch = await asyncio.to_thread(_voice_choices, settings)
        return {"default_voice": ch["default_voice"], "speed": settings.kokoro_speed,
                "voice_turn_thinking": settings.voice_turn_thinking or "off"}

    @r.patch("/settings")
    async def patch_voice_settings(body: dict = Body(...)):
        settings = load_settings()
        unknown = set(body) - {"default_voice", "speed", "voice_turn_thinking"}
        if unknown:
            raise HTTPException(422, f"Unknown setting: {sorted(unknown)[0]}")
        update: dict = {}
        if "speed" in body:
            v = body["speed"]
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not SPEED_MIN <= float(v) <= SPEED_MAX):
                raise HTTPException(422, f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
            update["kokoro_speed"] = round(float(v), 3)
        if "voice_turn_thinking" in body:
            v = body["voice_turn_thinking"]
            if v not in THINKING_LEVELS:
                raise HTTPException(422, f"voice_turn_thinking must be one of {', '.join(THINKING_LEVELS)}")
            update["voice_turn_thinking"] = v
        profile_default = _NOCHANGE
        if "default_voice" in body:
            v = body["default_voice"]
            kind = await asyncio.to_thread(classify_voice, settings, v)
            if kind is None or v == "":
                raise HTTPException(422, "Unknown voice")
            if kind == "builtin":
                from .tts import builtin_id
                update["kokoro_voice"] = builtin_id(v)
                profile_default = None              # the built-in voice speaks
            else:
                profile_default = v
        from . import config as _vc
        from . import tts as _tts
        if profile_default is not _NOCHANGE:
            try:
                from . import dv
                if dv.available():
                    await asyncio.to_thread(dv.set_default_profile, profile_default)
            except Exception:
                log.exception("voice: could not set the default voice profile")
                raise HTTPException(500, "Could not save the default voice")
        if update:
            await asyncio.to_thread(_vc.save_prefs, update)
        fresh = load_settings()
        _tts.apply_live(fresh)
        ch = await asyncio.to_thread(_voice_choices, fresh)
        return {"default_voice": ch["default_voice"], "speed": fresh.kokoro_speed,
                "voice_turn_thinking": fresh.voice_turn_thinking or "off"}

    @r.get("/preview")
    async def voice_preview(voice: str, text: str | None = None):
        settings = load_settings()
        if text is not None and len(text) > PREVIEW_MAX_CHARS:
            raise HTTPException(422, f"Keep the sample under {PREVIEW_MAX_CHARS} characters")
        line = (text or "").strip() or PREVIEW_TEXT
        kind = await asyncio.to_thread(classify_voice, settings, voice)
        if kind is None or not voice:
            raise HTTPException(422, "Unknown voice")
        if not settings.enabled or settings.missing():
            raise HTTPException(503, "Voice isn't set up on this server")
        wav = await asyncio.to_thread(_preview_wav, settings, voice, line)
        return Response(wav, media_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=300"})

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
_NOCHANGE = object()

PREVIEW_TEXT = "Hi, this is how I'll sound in Drive mode."
PREVIEW_MAX_CHARS = 120
_PREVIEWS: OrderedDict[tuple, bytes] = OrderedDict()
_PREVIEWS_LOCK = threading.Lock()


def _dv():
    """The dispatch-voice adapter when it is mounted, else None."""
    if "dispatch-voice" not in _LOADED:
        return None
    try:
        from . import dv
        return dv if dv.available() else None
    except Exception:
        return None


def classify_voice(settings: VoiceSettings, voice) -> str | None:
    """"builtin" | "profile" for a known voice id, None for anything else.
    "" is "use the default" (only meaningful per bot)."""
    from .tts import builtin_id, builtin_voices
    if not isinstance(voice, str) or len(voice) > 80:
        return None
    if voice == "":
        return "default"
    b = builtin_id(voice)
    if b:
        return "builtin" if any(v["id"] == voice for v in builtin_voices(settings)) else None
    dv = _dv()
    if dv is not None and dv.profile_exists(voice):
        return "profile"
    return None


def _voice_choices(settings: VoiceSettings) -> dict:
    """What the Settings rows render from: default voice, built-ins, profiles."""
    from .tts import BUILTIN, builtin_voices
    dv = _dv()
    profiles: list[dict] = []
    default = None
    if dv is not None:
        try:
            profiles = dv.profiles()
            default = dv.default_profile()
        except Exception:
            log.exception("voice: listing voice profiles failed")
    return {"default_voice": default or f"{BUILTIN}{settings.kokoro_voice}",
            "builtin_voices": builtin_voices(settings),
            "profiles": profiles}


def _preview_wav(settings: VoiceSettings, voice: str, text: str) -> bytes:
    from . import tts
    eng = tts.shared_engine(settings)
    key = (voice, round(settings.kokoro_speed, 3), settings.kokoro_variant, text)
    with _PREVIEWS_LOCK:
        if key in _PREVIEWS:
            _PREVIEWS.move_to_end(key)
            return _PREVIEWS[key]
    pcm, rate = eng.synth(text, voice)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate or eng.sample_rate))
        w.writeframes(np.asarray(pcm, "<i2").tobytes())
    data = buf.getvalue()
    with _PREVIEWS_LOCK:
        _PREVIEWS[key] = data
        while len(_PREVIEWS) > 32:
            _PREVIEWS.popitem(last=False)
    return data
