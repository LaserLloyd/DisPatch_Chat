#!/usr/bin/env python3
"""Drive mode end-to-end on a SCRATCH DisPatch instance (never the live one).

    cd backend
    uv run --group voice python tests/e2e_voice_roundtrip.py            # wire test
    uv run --group voice python tests/e2e_voice_roundtrip.py --browser  # + headless Chromium fake mic

What it does:

1. Starts a stub OpenAI-compatible LLM on a free loopback port (canned reply,
   ``--llm-delay`` seconds of fake "thinking"), so the round trip needs no
   agent, no gateway, no API key and no rig.
2. Writes a throwaway data dir with ONE API bot pointed at that stub, and
   starts ``uvicorn app.main:app`` on ``--port`` (default 8798) with
   ``DISPATCH_DATA_DIR=<tmp>`` and ``DISPATCH_VOICE=1``. No PIN is set, which
   DisPatch treats as unlocked (the PIN/cookie gate is covered by
   tests/test_voice_ws_route.py).
3. Makes the driver's voice with the local TTS engine (a different built-in
   voice), unless ``--wav`` is given (16 kHz mono 16-bit).
4. Wire mode: opens /ws/voice/<thread>, streams the WAV in REAL TIME (20 ms
   frames) + silence, and records eot / transcript / first reply audio. Then
   speaks again while the reply is "playing" and times the barge-in.
   Browser mode: Chromium with --use-fake-device-for-media-stream and
   --use-file-for-fake-audio-capture=<wav> opens the app, presses the 🎙
   button and waits for the transcript and the reply audio in the Drive view.
5. Checks the thread through the HTTP API: the transcript is a user row with
   metadata.voice, and the bot's reply follows it.

Prints a JSON summary (latencies in ms) and exits non-zero on any failure.
Needs the voice models (python -m app.voice.download) and the integration
patch applied to main.py.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))       # `python tests/e2e_voice_roundtrip.py` from backend/
REPLY = ("You have two things this afternoon. A dentist appointment at three, and a call "
         "with the school at four thirty. Want me to remind you before each one?")
SAY_1 = "Hey, what's on my calendar for this afternoon?"
SAY_2 = "Actually, wait, stop. Just tell me the first one."


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Stub LLM
# --------------------------------------------------------------------------- #

def start_stub_llm(delay: float) -> tuple[int, list]:
    seen: list = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json(200, {"object": "list", "data": [{"id": "stub", "object": "model"}]})

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            seen.append(req)
            time.sleep(delay)
            self._json(200, {"id": "x", "object": "chat.completion", "model": "stub",
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": REPLY}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, seen


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

def make_speech(text: str, out: Path) -> np.ndarray:
    """The driver's voice: the configured local TTS engine, in a different
    built-in voice than the bot uses, resampled to 16 kHz."""
    from app.voice.config import load_settings
    from app.voice.tts import build_engine
    eng = build_engine(load_settings())
    pcm, sr = eng.synth(text, "am_michael")
    a = pcm.astype(np.float32) / 32768.0
    n = int(len(a) * 16000 / sr)
    y = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    write_wav(out, y, 16000)
    return y


def write_wav(path: Path, x: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "--wav must be 16 kHz mono 16-bit"
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

def start_server(port: int, data: Path, llm_port: int) -> subprocess.Popen:
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.yaml").write_text(json.dumps({"bots": [{
        "id": "voicetest", "name": "Voice Test", "emoji": "🎙", "order": 0,
        "visible": True, "safe": False,
        "api": {"provider": "openai", "base_url": f"http://127.0.0.1:{llm_port}/v1",
                "model": "stub", "api_key": "not-a-key"},
    }]}, ensure_ascii=False))          # JSON is YAML; ascii escapes of an emoji
                                        # would parse as lone surrogates
    os.chmod(data / "config.yaml", 0o600)
    env = dict(os.environ, DISPATCH_DATA_DIR=str(data), DISPATCH_VOICE="1",
               DISPATCH_MIRROR="0", DISPATCH_GATEWAY_WS="0",
               OPENCLAW_BIN="/nonexistent/openclaw")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND, env=env, stdout=open(data / "server.log", "wb"), stderr=subprocess.STDOUT)
    import httpx
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/voice/status", timeout=1).status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit(f"server did not come up; see {data / 'server.log'}")


# --------------------------------------------------------------------------- #
# Wire test
# --------------------------------------------------------------------------- #

async def wire_test(base: str, tid: str, speech1: np.ndarray, speech2: np.ndarray, out: Path) -> dict:
    import websockets
    url = base.replace("http", "ws") + f"/ws/voice/{tid}"
    res: dict = {"events": []}
    reply_pcm: list[bytes] = []
    t0 = time.monotonic()

    def ms(t):
        return round((t - t0) * 1000)

    async with websockets.connect(url, max_size=None, open_timeout=120) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        await ws.send(json.dumps({"type": "start", "patience": "normal"}))
        pending_hdr = None
        marks: dict = {}

        async def reader():
            nonlocal pending_hdr
            async for m in ws:
                now = time.monotonic()
                if isinstance(m, bytes):
                    if pending_hdr and pending_hdr.get("kind") == "reply":
                        reply_pcm.append(m)
                        marks.setdefault("first_reply_audio", now)
                        marks["last_reply_audio"] = now
                    if pending_hdr and pending_hdr.get("kind") == "ack":
                        marks.setdefault("ack_audio", now)
                    pending_hdr = None
                    continue
                f = json.loads(m)
                res["events"].append({"t_ms": ms(now), **{k: v for k, v in f.items() if k != "type"}, "type": f["type"]})
                if f["type"] == "audio":
                    pending_hdr = f
                elif f["type"] in ("eot", "transcript", "barge_in"):
                    marks.setdefault(f["type"], now)
                    if f["type"] == "transcript":
                        res.setdefault("transcripts", []).append(f["text"])
                elif f["type"] == "metrics":
                    res.setdefault("server_metrics", []).append(f)

        rt = asyncio.create_task(reader())

        async def stream(x: np.ndarray, trailing_s: float):
            pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
            silence = b"\x00\x00" * int(16000 * trailing_s)
            data = pcm + silence
            step = 640                                   # 20 ms
            nxt = time.monotonic()
            for i in range(0, len(data), step):
                await ws.send(data[i:i + step])
                nxt += 0.02
                await asyncio.sleep(max(0, nxt - time.monotonic()))

        speech_end_1 = None
        t_start = time.monotonic()
        await stream(speech1, 0.0)
        speech_end_1 = time.monotonic()
        silence_task = asyncio.create_task(stream(np.zeros(16000 * 6, np.float32), 0))
        for _ in range(300):                             # wait for reply audio
            if "first_reply_audio" in marks:
                break
            await asyncio.sleep(0.05)
        silence_task.cancel()
        await asyncio.sleep(0.3)
        # Barge-in: speak while the reply is still "playing" (we never send
        # playback_done, so the server believes the client is still playing).
        speech2_start = time.monotonic()
        await stream(speech2, 1.0)
        await asyncio.sleep(3.0)
        rt.cancel()

    res["timeline_ms"] = {k: ms(v) for k, v in sorted(marks.items(), key=lambda kv: kv[1])}
    res["speech1_ms"] = {"start": ms(t_start), "end": ms(speech_end_1)}
    if "eot" in marks:
        res["latency_ms"] = {
            "speech_end_to_eot": round((marks["eot"] - speech_end_1) * 1000),
            "speech_end_to_transcript": round((marks.get("transcript", marks["eot"]) - speech_end_1) * 1000),
            "speech_end_to_ack_audio": round((marks["ack_audio"] - speech_end_1) * 1000) if "ack_audio" in marks else None,
            "speech_end_to_first_reply_audio": round((marks["first_reply_audio"] - speech_end_1) * 1000) if "first_reply_audio" in marks else None,
        }
    if "barge_in" in marks:
        res["latency_ms"]["speech2_onset_to_barge_in"] = round((marks["barge_in"] - speech2_start) * 1000)
    if reply_pcm:
        a = np.frombuffer(b"".join(reply_pcm), "<i2")
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(a.tobytes())
        res["reply_audio_s"] = round(len(a) / 24000, 2)
    return res


def build_voice(base: str, clip: Path) -> dict:
    """POST the clip to the Voices API (consent: synthetic), set it as the
    default, and wait for its fillers. Returns timings."""
    import httpx
    t0 = time.monotonic()
    with clip.open("rb") as f:
        r = httpx.post(base + "/api/voices", timeout=300,
                       data={"name": "E2E host", "consent": "synthetic",
                             "consent_note": "e2e test clip"},
                       files={"file": (clip.name, f, "audio/wav")})
    r.raise_for_status()
    prof = r.json()
    built = time.monotonic() - t0
    httpx.patch(base + f"/api/voices/{prof['id']}", json={"default": True}, timeout=30).raise_for_status()
    for _ in range(240):                     # fillers render in the background
        p = httpx.get(base + f"/api/voices/{prof['id']}", timeout=30).json()
        if p.get("fillers") == "ready":
            break
        time.sleep(0.5)
    return {"id": prof["id"], "build_s": round(built, 2),
            "fillers_wait_s": round(time.monotonic() - t0 - built, 2),
            "profile_keys": sorted(p.keys())}


# --------------------------------------------------------------------------- #
# Browser test (headless Chromium, fake mic)
# --------------------------------------------------------------------------- #

def browser_test(base: str, tid: str, wav: Path, shots: Path) -> dict:
    from playwright.sync_api import sync_playwright
    out: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
        page = b.new_page(viewport={"width": 1280, "height": 900})
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
        # The popout view opens exactly one conversation (and keeps the header
        # with the 🎙 button), which is also how a phone in a car mount would
        # be used.
        page.goto(base + f"/?popout=1&bot=voicetest&thread={tid}")
        page.wait_for_selector("#drive-btn:not([hidden])", timeout=30000)
        page.screenshot(path=str(shots / "drive-header.png"))
        t0 = time.monotonic()
        page.click("#drive-btn")
        page.wait_for_selector(".drive-overlay", timeout=10000)
        # Locators, not wait_for_function: the app's CSP (rightly) has no
        # 'unsafe-eval', and Playwright's string predicates need it.
        import re
        page.locator(".drive-you").filter(has_text=re.compile(r"\w{3}")).wait_for(timeout=60000)
        out["transcript_ui"] = page.inner_text(".drive-you")
        out["click_to_transcript_ms"] = round((time.monotonic() - t0) * 1000)
        page.locator(".drive-them").filter(has_text=re.compile(r"\w{3}")).wait_for(timeout=60000)
        out["reply_ui"] = page.inner_text(".drive-them")
        out["click_to_reply_text_ms"] = round((time.monotonic() - t0) * 1000)
        out["state"] = page.get_attribute(".drive-overlay", "data-state")
        page.screenshot(path=str(shots / "drive-view.png"))
        page.click(".drive-exit")
        out["console_errors"] = [line for line in logs if line.startswith("error")]
        b.close()
    return out


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--data", type=Path, default=None, help="scratch data dir (default: a new temp dir)")
    ap.add_argument("--wav", type=Path, default=None, help="16 kHz mono 16-bit driver speech")
    ap.add_argument("--llm-delay", type=float, default=1.5, help="stub LLM 'thinking' seconds")
    ap.add_argument("--browser", action="store_true", help="also run the headless Chromium fake-mic test")
    ap.add_argument("--voice-clip", type=Path, default=None,
                    help="build a cloned-voice profile from this clip through /api/voices (needs "
                         "dispatch-voice) and make it the default, so replies use the cloned voice")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="dispatch-voice-e2e-"))
    data = args.data or (work / "data")
    llm_port, llm_seen = start_stub_llm(args.llm_delay)
    base = f"http://127.0.0.1:{args.port}"
    if args.wav:
        s1 = read_wav16k(args.wav)
        wav1 = args.wav
    else:
        wav1 = work / "driver1.wav"
        s1 = make_speech(SAY_1, wav1)
    s2 = make_speech(SAY_2, work / "driver2.wav")
    proc = start_server(args.port, data, llm_port)
    ok = True
    summary: dict = {"work_dir": str(work)}
    try:
        import httpx
        st = httpx.get(base + "/api/voice/status").json()
        summary["status"] = st
        if args.voice_clip:
            summary["voice_profile"] = build_voice(base, args.voice_clip)
        r = httpx.post(base + "/api/threads", json={"bot_id": "voicetest"})
        r.raise_for_status()
        tid = r.json()["id"]
        summary["wire"] = asyncio.run(wire_test(base, tid, s1, s2, work / "reply.wav"))
        msgs = httpx.get(base + f"/api/threads/{tid}/messages").json()
        rows = msgs["messages"] if isinstance(msgs, dict) else msgs
        users = [m for m in rows if m["role"] == "user"]
        bots = [m for m in rows if m["role"] == "assistant"]
        summary["thread"] = {"user_rows": [(m["content"], (m.get("metadata") or {}).get("voice")) for m in users],
                             "assistant_rows": len(bots)}
        summary["llm_saw_voice_hint"] = any("[Voice mode]" in json.dumps(r) for r in llm_seen)
        checks = {
            "transcript_heard": bool(summary["wire"].get("transcripts")) and "calendar" in summary["wire"]["transcripts"][0].lower(),
            "voice_row_persisted": any(v for _, v in summary["thread"]["user_rows"]),
            "assistant_replied": summary["thread"]["assistant_rows"] >= 1,
            "reply_audio_received": summary["wire"].get("reply_audio_s", 0) > 1,
            "barge_in_seen": "barge_in" in summary["wire"]["timeline_ms"],
            "persona_hint_sent": summary["llm_saw_voice_hint"],
        }
        if args.browser:
            th2 = httpx.post(base + "/api/threads", json={"bot_id": "voicetest"}).json()["id"]
            summary["browser_thread"] = th2
            # Chrome LOOPS the fake-mic file: without a long tail of silence the
            # "driver" never stops talking, and every reply is merged/barged.
            mic = work / "driver1-mic.wav"
            write_wav(mic, np.concatenate([np.zeros(16000, np.float32), s1,
                                           np.zeros(16000 * 40, np.float32)]), 16000)
            summary["browser"] = browser_test(base, th2, mic, work)
            checks["browser_transcript"] = "calendar" in summary["browser"].get("transcript_ui", "").lower()
            checks["browser_reply"] = len(summary["browser"].get("reply_ui", "")) > 3
        summary["checks"] = checks
        ok = all(checks.values())
    except Exception as e:
        summary["error"] = repr(e)
        ok = False
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(10)
    summary["ok"] = ok
    print(json.dumps(summary, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
