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
3. Feature on: the Settings › Voice switch (``enabled`` in voice.yaml), or
   ``DISPATCH_VOICE=1``; ``DISPATCH_VOICE=0`` is off whatever the switch says.
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

from .config import (
    HARD_SILENCE_MAX,
    HARD_SILENCE_MIN,
    MERGE_WINDOW_MAX,
    MERGE_WINDOW_MIN,
    SPEED_MAX,
    SPEED_MIN,
    THINKING_LEVELS,
    VoiceSettings,
    load_settings,
)

log = logging.getLogger("dispatch.voice")

#: Close code for "not allowed" (policy violation), same as /ws uses.
POLICY = 1008
#: Close code for "the server can't do voice right now" (feature off/missing).
UNAVAILABLE = 1013
#: Close code for "a newer voice socket of the same session took over".
REPLACED = 4001

#: Close reasons for a voice socket refused (or ended) because voice is off.
#: The client shows its own localised words for them (drive.js matches the
#: start); they are also what a log line or a non-browser client reads.
OFF_SETTINGS = "Voice is turned off in Settings › Voice"
OFF_SERVER = "Voice is turned off on this server (DISPATCH_VOICE)"


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
    if not settings.switched_on:
        reason = OFF_SERVER if settings.forced == "off" else OFF_SETTINGS
        return GateResult(False, UNAVAILABLE, reason, token)
    bot = await thread_bot(thread_id)
    if bot is None:
        return GateResult(False, POLICY, "no such thread")
    if getattr(bot, "safe", False):
        return GateResult(False, POLICY, "safe bot")
    if await asyncio.to_thread(settings.missing):      # stat + find_spec: off the loop
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


_DEVICE_RE = __import__("re").compile(r"\A[A-Za-z0-9-]{8,64}\Z")


def slot_key(ws, token: str | None) -> str:
    """Whose voice sockets this one counts against.

    With a PIN: the unlocked session. Without one there is no session, and
    one shared list let a third family member's drive end the first one
    (review M3), so the browser's Drive device id (``?d=``, drive.js) keys
    it; a client that sends none gets a list of its own (never shared)."""
    if token:
        return token
    try:
        dev = ws.query_params.get("d") or ""
    except Exception:
        dev = ""
    if _DEVICE_RE.match(dev):
        return f"device:{dev}"
    return f"conn:{id(ws)}"


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
    if settings is None:
        ensure_switch_watch()
        settings = await asyncio.to_thread(load_settings)
        await sync_switch(settings)
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
    key = slot_key(ws, token)
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
    """Where this voice's renders are kept across restarts, or None.

    Disk caching is opt-in per engine (``cache_tag``): only an engine whose
    output is a pure function of (model, voice, speed, text), such as the
    optional Kokoro plugin's built-in voices, returns a tag. Cloned voices
    keep their own pre-rendered fillers in the profile store."""
    from pathlib import Path

    from . import tts as _tts
    tag = _tts.cache_tag(tts_engine, profile_id)
    if not tag or not _CACHE_TAG_RE.match(tag):
        return None
    from .config import _data_dir
    return Path(_data_dir()) / "voice-cache" / tag


#: A plugin's cache tag becomes a directory name: plain characters only.
_CACHE_TAG_RE = __import__("re").compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
#: Tag directories kept under <data>/voice-cache (each speed step or voice
#: change makes a new one; the least recently written go first).
CACHE_KEEP = 8


def _prune_cache(root) -> None:
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return
    import shutil
    for d in dirs[CACHE_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)


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
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                _prune_cache(d.parent)
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
    if _profile_speaks(tts_engine, pid):
        # A dispatch-voice profile (a cloned voice, or the stock voice) has
        # its own fillers, rendered ONCE by dispatch-voice into the profile
        # store and persisted there. While they are still being made, no acks
        # this session: rendering a second set here put up to ~25 Chatterbox
        # synths on every restart (review M2).
        return pid, []
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


def _profile_speaks(tts_engine, pid) -> bool:
    """Will a dispatch-voice profile speak for ``pid``? A profile id that is
    not a built-in voice, or no id at all on an engine whose last resort is
    dispatch-voice (the stock voice)."""
    from . import tts as _tts
    if pid and not _tts.builtin_id(pid):
        return _profiles_installed()
    if not pid:
        return _speaks_stock(tts_engine)
    return False


def _profiles_installed() -> bool:
    """A profile extension (dispatch-voice) is mounted and owns fillers."""
    from . import registry
    return registry._ack_provider is not None


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
#: Bumped by release_models(). A prewarm that started before a release drops
#: what it loaded when it finishes, so "off" stays off.
_GEN = [0]


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


def _speaks_stock(eng) -> bool:
    """True when dispatch-voice is the last-resort engine of this chain."""
    try:
        from . import dv
    except Exception:
        return False
    return any(isinstance(e, dv.ChatterboxTTS) and not e.has_fallback for e in _walk(eng))


def _walk(eng):
    from . import tts
    return tts.walk(eng)


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
    gen = _GEN[0]
    _PREWARM["state"] = "running"
    try:
        from . import registry, stt, tts, vad_turn
        stt_engine = stt.shared_engine(settings)
        eng = tts.shared_engine(settings)
        vad_turn._shared_smart_turn(settings)
        vad_turn.build_detector(settings)          # Silero + ORT kernels
        stt_engine.transcribe(np.zeros(16000, np.float32))
        for e in tts.walk(eng):
            warm = getattr(e, "warm", None)
            if callable(warm):
                try:
                    warm()
                except Exception:
                    log.exception("voice: %s warm-up failed", type(e).__name__)
        pid = _profile_to_warm(registry, tts)
        if pid or _speaks_stock(eng):
            # A cloned voice some bot uses, or the stock voice that speaks
            # when no built-in voice plugin is installed (this also builds
            # the stock profile on an install that predates it).
            try:
                from . import dv
                dv.store().engine.warm()
                eng.synth("Okay.", pid)
            except Exception:
                log.exception("voice: cloned-voice warm-up failed")
        if gen != _GEN[0]:
            # Switched off (release_models ran) while we were loading: give
            # back what this run loaded after the release.
            release_models()
            return False
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
# The switch, applied live
# --------------------------------------------------------------------------- #

def release_models() -> None:
    """Drop every loaded speech model and render cache, so the memory goes
    back when voice is switched off. The next session (or prewarm) loads them
    again. What a plugin keeps in its own module globals is the plugin's."""
    import gc

    from . import stt, tts, vad_turn
    _GEN[0] += 1
    with stt._ENGINES_LOCK:
        stt._ENGINES.clear()
    with tts._ENGINES_LOCK:
        tts._ENGINES.clear()
    with vad_turn._SMART_TURN_LOCK:
        vad_turn._SMART_TURN.clear()
    with _ACKS_LOCK:
        _ACKS.clear()
    with _NOTICES_LOCK:
        _NOTICES.clear()
    with _PREVIEWS_LOCK:
        _PREVIEWS.clear()
    try:
        from . import dv
        dv.release()
    except Exception:
        log.debug("voice: no dispatch-voice engine to release", exc_info=True)
    from . import registry
    registry.run_release_hooks()          # plugins drop what they cache themselves
    _PREWARM.clear()
    _PREWARM["state"] = "idle"
    gc.collect()
    _trim_heap()
    log.info("voice: speech models released (voice switched off)")


def _trim_heap() -> None:
    """Hand freed heap back to the OS. glibc keeps what the ONNX sessions
    freed in its arenas, so without this the process stayed ~700 MB fatter
    after a release and grew by a full model set on every off/on cycle
    (measured 2026-10-10). A no-op where there is no glibc."""
    try:
        import ctypes
        import ctypes.util
        name = ctypes.util.find_library("c")
        if name:
            ctypes.CDLL(name).malloc_trim(0)
    except Exception:
        log.debug("voice: malloc_trim unavailable", exc_info=True)


async def end_all_sessions(code: int = UNAVAILABLE, reason: str = OFF_SETTINGS) -> int:
    """End every open Drive session (voice was switched off). Returns how many."""
    n = 0
    for slots in list(_OPEN.values()):
        if not isinstance(slots, list):
            continue
        for slot in list(slots):
            if slot.evicted:
                continue
            slot.evicted = True
            n += 1
            if slot.session is not None:
                slot.session.end(code, reason)
            with contextlib.suppress(Exception):
                await slot.ws.close(code=code, reason=reason)
    return n


async def apply_switch(was_on: bool, settings: VoiceSettings) -> None:
    """The switch moved (or a PATCH saved it): make the running process match.

    on → off: open Drive sessions end with 1013 and the speech models are
    released (no restart). off → on: the start-up prewarm runs now, in the
    background, so the first drive is fast."""
    now_on = settings.switched_on
    if was_on and not now_on:
        n = await end_all_sessions(UNAVAILABLE, OFF_SETTINGS)
        if n:
            log.info("voice: switched off; ended %d Drive session(s)", n)
        await asyncio.to_thread(release_models)
    elif now_on and not was_on and settings.enabled and settings.prewarm:
        threading.Thread(target=prewarm, args=(settings,), name="voice-prewarm",
                         daemon=True).start()


#: The switch as this process last applied it (None = not looked yet).
_APPLIED: dict[str, bool | None] = {"on": None}
_WATCH: dict[str, object] = {}
#: How often a running server re-reads voice.yaml for a hand-edited switch.
SWITCH_WATCH_S = 5.0


async def sync_switch(settings: VoiceSettings | None = None) -> None:
    """Apply a switch change that did not come through PATCH (someone edited
    ``enabled`` in voice.yaml by hand): the same live effects as the app's
    switch — off ends drives and releases the models, on prewarms. Cheap:
    voice.yaml is re-read only when its mtime moved (config.load_prefs)."""
    settings = settings or load_settings()
    now = settings.switched_on
    was = _APPLIED["on"]
    _APPLIED["on"] = now
    if was is None or was == now:
        return
    log.info("voice: the switch changed in voice.yaml (%s → %s); applying it",
             "on" if was else "off", "on" if now else "off")
    await apply_switch(was, settings)


def ensure_switch_watch() -> None:
    """Start (once, on the running loop) the background check that makes a
    hand edit of voice.yaml apply within SWITCH_WATCH_S, open drives
    included. Called from the status route and the voice socket."""
    task = _WATCH.get("task")
    if task is not None and not task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def watch():
        while True:
            await asyncio.sleep(SWITCH_WATCH_S)
            try:
                await sync_switch(await asyncio.to_thread(load_settings))
            except Exception:
                log.exception("voice: applying a voice.yaml switch change failed")

    _WATCH["task"] = loop.create_task(watch(), name="voice-switch-watch")


def tool_switch(tool_id: str) -> bool:
    """Is this opt-in voice tool switched on in tools.yaml? (No entry = off.)"""
    try:
        from app import tools
        return tools.builtin_enabled(tool_id) is True
    except Exception:
        return False


def require_tool(tool_id: str):
    """A dependency: 404 unless ``tool_id`` is on in tools.yaml, the same
    answer a builtin pane's routes give once the manifest switches it off."""
    def dep() -> None:
        if not tool_switch(tool_id):
            raise HTTPException(404, "Not found")
    return dep


# --------------------------------------------------------------------------- #
# HTTP: the status probe + extension mounting
# --------------------------------------------------------------------------- #

def mount_http(app, require_full) -> list[str]:
    """Called once by main.py at import time.

    * ``GET /api/voice/status`` — is Drive mode on (the frontend shows the
      button only when it is), the switch, and what is missing. Unlocked
      tier only, like every route here.
    * ``GET/PATCH /api/voice/settings`` — the switch and the server-level
      preferences (voice.yaml), applied live.
    * ``GET /api/voice/lab`` + ``POST /api/voice/lab/test`` — the Voice lab
      tool; 404 unless ``voice-lab`` is on in tools.yaml.
    * ``dispatch-voice`` when installed (app/voice/dv.py): the Voices API at
      ``/api/voices`` (+ ``/api/voices/ui``) and the cloned-voice engine. The
      API answers 404 unless the ``voice-cloning`` tool is on; the engine
      speaks either way.
    * Every extension in ``DISPATCH_VOICE_EXTENSIONS`` (registry.py), mounted
      behind the same unlocked-tier gate.
    """
    from . import registry

    def gate(request: Request) -> None:
        require_full(request)

    r = APIRouter(prefix="/api/voice", dependencies=[Depends(gate)])

    @r.get("/status")
    async def voice_status():
        ensure_switch_watch()
        settings = await asyncio.to_thread(load_settings)
        await sync_switch(settings)
        st = await asyncio.to_thread(settings.status)
        st["extensions"] = list(_LOADED)
        st["voices_ui"] = "/api/voices/ui" if "dispatch-voice" in _LOADED else None
        st.update(await asyncio.to_thread(_voice_choices, settings))
        st["warm"] = _PREWARM.get("state") == "done"
        st["stock_voice"] = _stock_id()
        st["plugins"] = await asyncio.to_thread(_plugins, settings)
        st["tools"] = await asyncio.to_thread(lambda: {tid: tool_switch(tid) for tid in VOICE_TOOLS})
        return st

    @r.get("/settings")
    async def get_voice_settings():
        settings = load_settings()
        ch = await asyncio.to_thread(_voice_choices, settings)
        return _settings_out(settings, ch)

    @r.patch("/settings")
    async def patch_voice_settings(body: dict = Body(...)):
        settings = load_settings()
        unknown = set(body) - SETTING_KEYS
        if unknown:
            raise HTTPException(422, f"Unknown setting: {sorted(unknown)[0]}")
        update: dict = {}
        if "enabled" in body:
            v = body["enabled"]
            if not isinstance(v, bool):
                raise HTTPException(422, "enabled must be true or false")
            if settings.forced is not None and v != settings.switched_on:
                raise HTTPException(409, "DISPATCH_VOICE is set on the server, so the "
                                         "switch can't change it")
            if settings.forced is None:
                update["enabled"] = v
        if "speed" in body:
            update["speed"] = _ranged(body["speed"], "speed", SPEED_MIN, SPEED_MAX)
        if "merge_window_s" in body:
            update["merge_window_s"] = _ranged(body["merge_window_s"], "merge_window_s",
                                               MERGE_WINDOW_MIN, MERGE_WINDOW_MAX)
        if "hard_silence_s" in body:
            update["hard_silence_s"] = _ranged(body["hard_silence_s"], "hard_silence_s",
                                               HARD_SILENCE_MIN, HARD_SILENCE_MAX)
        if "voice_turn_thinking" in body:
            v = body["voice_turn_thinking"]
            if v not in THINKING_LEVELS:
                raise HTTPException(422, f"voice_turn_thinking must be one of {', '.join(THINKING_LEVELS)}")
            update["voice_turn_thinking"] = v
        reset: list[str] = []
        if "reset" in body:
            v = body["reset"]
            if (not isinstance(v, list) or not all(isinstance(k, str) for k in v)
                    or not set(v) <= RESETTABLE):
                raise HTTPException(422, "reset takes a list of: " + ", ".join(sorted(RESETTABLE)))
            reset = [k for k in v if k not in update]
        profile_default = _NOCHANGE
        if "default_voice" in body:
            v = body["default_voice"]
            kind = await asyncio.to_thread(classify_voice, settings, v)
            if kind is None or v == "":
                raise HTTPException(422, "Unknown voice")
            if kind == "builtin":
                from .tts import builtin_id
                update["builtin_voice"] = builtin_id(v)
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
        was_on = settings.switched_on
        if update:
            await asyncio.to_thread(_vc.save_prefs, update)
        if reset:
            await asyncio.to_thread(_vc.forget_prefs, reset)
        fresh = load_settings()
        _tts.apply_live(fresh)
        await apply_switch(was_on, fresh)
        _APPLIED["on"] = fresh.switched_on
        ch = await asyncio.to_thread(_voice_choices, fresh)
        return _settings_out(fresh, ch)

    @r.get("/preview")
    async def voice_preview(voice: str, text: str | None = None):
        settings = load_settings()
        if text is not None and len(text) > PREVIEW_MAX_CHARS:
            raise HTTPException(422, f"Keep the sample under {PREVIEW_MAX_CHARS} characters")
        line = (text or "").strip() or PREVIEW_TEXT
        kind = await asyncio.to_thread(classify_voice, settings, voice)
        if kind is None or not voice:
            raise HTTPException(422, "Unknown voice")
        if not settings.enabled or await asyncio.to_thread(settings.missing):
            raise HTTPException(503, "Voice isn't set up on this server")
        try:
            wav = await asyncio.to_thread(_preview_wav, settings, voice, line)
        except PreviewBusy:
            raise HTTPException(503, "The voice engine is busy; try the sample again") from None
        except Exception as e:
            # A provider's renderer failing is the engine's problem, not a
            # server crash: say so in a JSON 503 like the rest of the API.
            log.exception("voice: preview of %s failed", voice)
            raise HTTPException(503, f"Couldn't play a sample of that voice ({type(e).__name__})") from None
        return Response(wav, media_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=300"})

    lab = [Depends(require_tool("voice-lab"))]

    @r.get("/lab", dependencies=lab)
    async def voice_lab():
        settings = load_settings()
        return await asyncio.to_thread(_lab_info, settings)

    @r.post("/lab/test", dependencies=lab)
    async def voice_lab_test(request: Request):
        settings = load_settings()
        if not settings.enabled or settings.missing():
            raise HTTPException(503, "Turn voice on (and finish setting it up) to run a test")
        size = request.headers.get("content-length")
        if size is not None and (not size.isdigit() or int(size) > LAB_TEST_MAX_BYTES):
            raise HTTPException(413, f"Keep the test under {LAB_TEST_MAX_S} seconds")
        raw = await request.body()
        if len(raw) > LAB_TEST_MAX_BYTES:
            raise HTTPException(413, f"Keep the test under {LAB_TEST_MAX_S} seconds")
        if len(raw) % 2 or len(raw) < LAB_TEST_MIN_BYTES:
            raise HTTPException(422, "Send at least half a second of 16 kHz mono PCM16")
        if not _LAB_LOCK.acquire(blocking=False):
            raise HTTPException(409, "A test is already running")
        try:
            return await _lab_test(settings, raw)
        finally:
            _LAB_LOCK.release()

    app.include_router(r)
    loaded: list[str] = []
    try:
        from . import dv
        if dv.install(app, require_full, extra=[Depends(require_tool("voice-cloning"))]):
            loaded.append("dispatch-voice")
    except Exception:
        log.exception("voice: dispatch-voice failed to mount; no cloned or stock voice")
    loaded += registry.mount_extensions(app, require_full)
    _LOADED[:] = loaded
    return list(_LOADED)


#: The opt-in voice tools (tools.yaml ``kind: builtin``; no entry = off).
VOICE_TOOLS = ("voice-cloning", "voice-builtin", "voice-lab")
#: Every key PATCH /api/voice/settings accepts.
SETTING_KEYS = frozenset({"enabled", "default_voice", "speed", "voice_turn_thinking",
                          "merge_window_s", "hard_silence_s", "reset"})
#: Keys ``reset`` may put back to the server's default (the Voice lab's).
RESETTABLE = frozenset({"voice_turn_thinking", "merge_window_s", "hard_silence_s"})

LAB_TEST_MAX_S = 10
LAB_TEST_MAX_BYTES = 16000 * 2 * LAB_TEST_MAX_S
LAB_TEST_MIN_BYTES = 16000            # half a second
LAB_TEST_FALLBACK = "I didn't catch anything that time. Try again a little closer to the microphone."
_LAB_LOCK = threading.Lock()


def _ranged(v, name: str, lo: float, hi: float) -> float:
    if (not isinstance(v, (int, float)) or isinstance(v, bool)
            or not lo <= float(v) <= hi):
        raise HTTPException(422, f"{name} must be between {lo} and {hi}")
    return round(float(v), 3)


def _settings_out(settings: VoiceSettings, ch: dict) -> dict:
    return {"enabled": settings.switched_on, "forced": settings.forced,
            "default_voice": ch["default_voice"], "speed": settings.speed,
            "voice_turn_thinking": settings.voice_turn_thinking or "off",
            "merge_window_s": settings.merge_window_s,
            "hard_silence_s": settings.turn_hard_silence_s}


def _stock_id() -> str | None:
    if "dispatch-voice" not in _LOADED:
        return None
    try:
        from . import dv
        return dv.stock_id()
    except Exception:
        return None


#: What the Built-in voices tool suggests installing while NO provider is
#: registered: the documented optional plugin. Once a provider registers, it
#: describes itself (registry ``info``) and this is not used.
SUGGESTED_BUILTIN = {
    "name": "dispatch-voice-kokoro",
    "module": "dispatch_voice_kokoro",
    "licence": "GPL-3.0-or-later",
    "install": [
        {"where": "plugin", "cmd": "uv run dispatch-voice-kokoro fetch-models"},
        {"where": "env", "cmd": "DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup"},
        {"where": "backend", "cmd": "uv run --group voice --with /path/to/dispatch-voice-kokoro uvicorn app.main:app"},
    ],
    "enable": [{"where": "env", "cmd": "DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup"}],
}


def _plugins(settings: VoiceSettings) -> dict:
    """The optional pieces, for Settings and the voice tools: installed?
    ready? what is missing? No paths, no keys. A registered built-in voice
    provider names itself (``registry.builtin_info``)."""
    import importlib.util

    from . import registry
    from .config import _dispatch_voice_installed
    try:
        dv_installed = _dispatch_voice_installed()
    except Exception:
        dv_installed = False
    mounted = "dispatch-voice" in _LOADED
    builtin_missing: list[str] = []
    for spec in list(registry._engines.values()):
        if spec.name != "chatterbox":
            builtin_missing += spec.missing_for(settings)
    has = registry.has_builtin_voices()
    info = registry.builtin_info()
    sug = SUGGESTED_BUILTIN
    try:
        sug_present = importlib.util.find_spec(sug["module"]) is not None
    except Exception:
        sug_present = False
    return {
        "cloning": {"installed": dv_installed, "ready": mounted,
                    "missing": [] if mounted or not dv_installed
                    else ["dispatch-voice models (run `dispatch-voice fetch-models`)"]},
        "builtin": {"installed": has,
                    # Not registered, but the suggested plugin is importable:
                    # it only needs enabling.
                    "package": has or sug_present,
                    "ready": has and not builtin_missing,
                    "missing": builtin_missing,
                    "name": info.get("name") if has else None,
                    "licence": info.get("licence") if has else None,
                    "models": info.get("models", []) if has else [],
                    "suggested": {k: sug[k] for k in ("name", "licence", "install", "enable")}},
    }


def _lab_info(settings: VoiceSettings) -> dict:
    """Everything the Voice lab shows. Safe for an unlocked client: no key
    values, no file paths; the HQ address is reduced to its host."""
    import os
    from urllib.parse import urlsplit

    from . import config as _vc
    from . import registry, stt, tts
    try:
        resolved = (tts._auto_engine(settings, registry)
                    if settings.tts_engine == "auto" else settings.tts_engine)
    except Exception:
        resolved = None
    fb = None
    if resolved and registry.engine_spec(resolved) is not None:
        spec = registry.fallback_for(settings, resolved, registry.engine_spec(resolved).sample_rate)
        fb = spec.name if spec else None
    with stt._ENGINES_LOCK:
        stt_loaded = bool(stt._ENGINES)
    with tts._ENGINES_LOCK:
        tts_loaded = bool(tts._ENGINES)
    hq_host = ""
    if settings.hq_url:
        try:
            hq_host = urlsplit(settings.hq_url).netloc
        except ValueError:
            hq_host = ""
    miss = settings.missing()
    return {
        "switch": settings.switched_on, "forced": settings.forced,
        "enabled": settings.enabled, "ready": not miss, "missing": miss,
        "stt": settings.stt_engine, "smart_turn": settings.smart_turn_path.exists(),
        "tts": settings.tts_engine, "tts_resolved": resolved, "fallback": fb,
        "engines": registry.engine_names(),
        "loaded": stt_loaded or tts_loaded,
        "warm": _PREWARM.get("state", "idle"), "warm_seconds": _PREWARM.get("seconds"),
        "minimax": {"selected": resolved == "minimax",
                    "key_set": bool(os.environ.get("MINIMAX_API_KEY")),
                    "model": settings.minimax_model, "voice": settings.minimax_voice},
        "hq": {"configured": bool(settings.hq_url), "host": hq_host,
               "model": settings.hq_model, "timeout_s": settings.hq_timeout_s},
        "thinking": settings.voice_turn_thinking or "off",
        "thinking_levels": list(THINKING_LEVELS),
        "timings": {
            "merge_window_s": {"value": settings.merge_window_s,
                               "default": _vc._float("VOICE_MERGE_WINDOW", 1.8),
                               "min": MERGE_WINDOW_MIN, "max": MERGE_WINDOW_MAX},
            "hard_silence_s": {"value": settings.turn_hard_silence_s,
                               "default": _vc._float("VOICE_HARD_SILENCE", 1.4),
                               "min": HARD_SILENCE_MIN, "max": HARD_SILENCE_MAX},
        },
    }


async def _lab_test(settings: VoiceSettings, raw: bytes) -> dict:
    """Hear ``raw`` (16 kHz mono PCM16), then say it back: the same engines a
    Drive session uses, timed. Returns the transcript, the spoken audio as a
    base64 WAV and the latency of each step."""
    import base64
    import time

    from . import registry
    from .tts import REPLY_ACTIVITY, speakable
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    t0 = time.monotonic()
    stt_engine, tts_engine = await warm_models(settings)
    load_ms = round((time.monotonic() - t0) * 1000)
    t1 = time.monotonic()
    text = (await asyncio.to_thread(stt_engine.transcribe, audio) or "").strip()
    stt_ms = round((time.monotonic() - t1) * 1000)
    say = speakable(text) or LAB_TEST_FALLBACK
    pid = await asyncio.to_thread(registry.resolve_profile, None)

    def speak_timed():
        # Never ahead of someone's reply sentence on the engine (review L1).
        REPLY_ACTIVITY.wait_idle(timeout=20)
        t = time.monotonic()
        first = None
        parts = []
        for chunk in tts_engine.stream(say, pid):
            if first is None:
                first = time.monotonic() - t
            parts.append(np.asarray(chunk, "<i2"))
        pcm = np.concatenate(parts) if parts else np.zeros(0, "<i2")
        return pcm, first, time.monotonic() - t

    pcm, first_s, total_s = await asyncio.to_thread(speak_timed)
    rate = int(getattr(tts_engine, "sample_rate", 24000) or 24000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return {
        "transcript": text, "said": say,
        "heard_ms": round(len(audio) / 16),
        "load_ms": load_ms, "stt_ms": stt_ms,
        "tts_first_ms": round(first_s * 1000) if first_s is not None else None,
        "tts_total_ms": round(total_s * 1000),
        "spoken_ms": round(len(pcm) * 1000 / rate),
        "audio_wav_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


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


def default_builtin(settings: VoiceSettings, builtins: list[dict]) -> str | None:
    """The built-in voice that speaks when no profile applies: the saved
    choice when the provider still lists it, else the provider's default,
    else its first voice. None without a provider."""
    if not builtins:
        return None
    from .tts import BUILTIN
    want = f"{BUILTIN}{settings.builtin_voice}" if settings.builtin_voice else ""
    ids = [v["id"] for v in builtins]
    if want in ids:
        return want
    return next((v["id"] for v in builtins if v.get("default")), ids[0])


def _voice_choices(settings: VoiceSettings) -> dict:
    """What the Settings rows render from: default voice, built-ins, profiles.

    ``default_voice`` is the voice a bot with no voice of its own speaks in:
    the default cloned profile, else the plugin's built-in voice, else (no
    built-in voice plugin) dispatch-voice's stock synthetic voice, else ""."""
    from .tts import builtin_voices
    dv = _dv()
    builtins = builtin_voices(settings)
    profiles: list[dict] = []
    default = None
    if dv is not None:
        try:
            if not builtins:
                dv.stock_profile(build=False)       # list it once it can exist
            profiles = dv.profiles()
            default = dv.default_profile()
        except Exception:
            log.exception("voice: listing voice profiles failed")
    if not default:
        default = default_builtin(settings, builtins)
    if not default and dv is not None:
        try:
            default = dv.stock_profile(build=False)
        except Exception:
            default = None
    return {"default_voice": default or "",
            "builtin_voices": builtins,
            "profiles": profiles}


class PreviewBusy(Exception):
    """Another sample is rendering, or a drive kept the engine busy too long."""


#: One sample renders at a time, and only between reply sentences: a preview
#: tapped during someone's drive must not delay their next sentence by a
#: whole synth (review L1).
_PREVIEW_SEM = threading.BoundedSemaphore(1)
PREVIEW_WAIT_S = 20.0


def _preview_wav(settings: VoiceSettings, voice: str, text: str) -> bytes:
    from . import tts
    key = (voice, round(settings.speed, 3), text)
    with _PREVIEWS_LOCK:
        if key in _PREVIEWS:
            _PREVIEWS.move_to_end(key)
            return _PREVIEWS[key]
    if not _PREVIEW_SEM.acquire(timeout=PREVIEW_WAIT_S):
        raise PreviewBusy()
    try:
        tts.REPLY_ACTIVITY.wait_idle(timeout=PREVIEW_WAIT_S)
        return _render_preview(settings, voice, text, key)
    finally:
        _PREVIEW_SEM.release()


def _render_preview(settings: VoiceSettings, voice: str, text: str, key: tuple) -> bytes:
    from . import registry, tts
    preview = registry.builtin_preview() if tts.builtin_id(voice) else None
    if preview is not None:
        # The provider renders its own voices, whatever engine is primary.
        pcm, rate = preview(settings, voice, text)
        eng = None
    else:
        eng = tts.shared_engine(settings)
        pcm, rate = eng.synth(text, voice)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate or (eng.sample_rate if eng is not None else 24000)))
        w.writeframes(np.asarray(pcm, "<i2").tobytes())
    data = buf.getvalue()
    with _PREVIEWS_LOCK:
        _PREVIEWS[key] = data
        while len(_PREVIEWS) > 32:
            _PREVIEWS.popitem(last=False)
    return data
