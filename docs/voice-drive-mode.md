# Drive mode (hands-free voice)

Drive mode lets you talk to a DisPatch thread with your hands on the wheel. You
speak, DisPatch works out when you have finished and turns your words into
text, then sends them through the thread's **normal** send path. The bot's
reply is spoken back sentence by sentence while it is still being written.
Transcripts land in the thread like typed messages (tagged `metadata.voice`),
so any bot type works: gateway agents, API bots and advisors.

All speech processing runs on the DisPatch host's **CPU**. Only the bot's own
LLM call leaves the machine. There is no cloud speech service and no GPU, and
Drive mode keeps working while a GPU rig is busy or offline.

| Stage | Engine | Licence | Notes |
|---|---|---|---|
| Voice activity | Silero VAD (ONNX) | MIT | 32 ms frames, ~0.2 ms each |
| End of turn | Smart Turn v3.2 (ONNX) + silence fallback | BSD-2 | ~100 ms per check; hears intonation |
| Turn merge | in-house | — | keeps listening after a turn ends (below) |
| Speech-to-text | Parakeet TDT 0.6B v2 int8 via `onnx-asr` | CC-BY-4.0 / MIT | whole utterance, RTF ≈ 0.06–0.09 |
| Voice (default) | Chatterbox-Turbo ONNX q4 via the separate [`dispatch-voice`](#cloned-voices-dispatch-voice) package | MIT, ungated | cloned voices; ~2.5 s to synthesise a short first sentence on an 8-core laptop CPU |
| Voice (fallback) | Kokoro-82M int8 via `kokoro-onnx` | Apache-2.0 weights; **GPL-3.0** phonemizer/espeak-ng at runtime | built-in voices; ~0.3–0.9 s per short sentence |
| Optional | "HQ" engine over OpenAI-compatible `/v1/audio/speech`; MiniMax T2A | — | opt-in; HQ always falls back locally |

The TTS engine sits behind one interface (`app/voice/tts.py`):

```python
async for frame in speak(text_chunks, profile_id, engine=...):   # PCM16LE mono frames
    ...
```

Each engine declares `sample_rate`, `sample_format` (`pcm_s16le`), `channels`
(1) and `streaming`. `DISPATCH_VOICE_TTS=auto` picks the cloned-voice engine
when `dispatch-voice` is installed, with Kokoro behind it. Kokoro speaks only
when no voice profile exists yet or the cloned engine fails before producing
audio. Without `dispatch-voice`, Kokoro alone is used.

## Who can use it

**Unlocked sessions only.**

- The voice socket, `/api/voice/status` and the whole Voices panel
  (`/api/voices/*`) refuse Safe Mode, locked devices and decoy sessions. The
  routes use `_require_operator`: a real unlocked session, or no PIN at all.
  Both prefixes are also on the middleware's Safe-Mode block list.
- Threads belonging to a Safe-Mode (`safe: true`) bot get no voice.
- The WebSocket gate runs inline before `accept()`, because HTTP middleware
  never sees a websocket. It requires Origin == Host, a live `lc_session`, and
  a non-safe bot.
- The session is re-checked about once a second. If it has lapsed, the socket
  closes.

**The microphone needs a secure context.** Browsers grant the microphone only
to `https://` pages or `localhost`. A phone must therefore open DisPatch
through an HTTPS address, such as a reverse proxy or tailnet HTTPS.

## Install

```bash
cd backend
uv sync --group voice                 # onnx-asr, kokoro-onnx + the cloned-voice engine's runtime deps
uv run python -m app.voice.download   # VAD, Smart Turn, Parakeet, Kokoro (~810 MB)
# optional cloned voices: check out dispatch-voice, then from its directory:
#   uv run dispatch-voice fetch-models   # Chatterbox-Turbo ONNX q4 (~690 MB, pinned revision)
```

The speech models go to `DISPATCH_SPEECH_MODELS`, which defaults to
`<data dir>/speech-models`. Chatterbox's models go to its own
`~/.local/share/dispatch-voice/models`, which dispatch-voice's
`DISPATCH_VOICE_MODELS` overrides.

`dispatch-voice` is **not** a dependency of this project, so a fresh clone
locks and installs without it. To enable cloned voices, set
`DISPATCH_VOICE_PKG` to the directory that contains its `dispatch_voice`
package (its `src/`); DisPatch imports it from there when present. The
`voice` group already carries that package's runtime dependencies. If you
run DisPatch under `uv run`, add `--group voice` so the group stays installed.

Restart DisPatch. With `DISPATCH_VOICE=auto` (the default), the 🎙 button
appears once everything is present.

## Cloned voices (dispatch-voice)

The Voices panel is at `/api/voices/ui`, linked from **Settings → Device →
Drive mode → Voices** while unlocked. In the panel you can:

- upload or record a 10–20 s clip, and run the quality checks;
- give consent: "my own voice", "explicit permission" or "synthetic";
- build the profile and audition it;
- choose the default voice.

Profiles live in `<data dir>/voices/<id>/`. They are recordings of a person,
so:

- they are never in the repo;
- they are on no deploy or sync allowlist;
- `.gitignore`, `.dockerignore` and `scripts/scrub_check.py` refuse `voices/`,
  `*.npz` and `speech-models/`.

**Per-bot voice.** A bot's `voice:` key in `config.yaml` names its profile.
Empty means the default profile. An unknown id falls back to the default.

**Fillers.** Each profile has pre-rendered fillers ("Mm-hmm.", "Right." …).
Drive mode plays one in the same voice the moment your transcript is sent,
while the agent works.

The ProfileStore and its engine load lazily, so a box that never opens Drive
mode or the panel pays nothing at startup.

## The voice persona

Every voice turn prepends an instruction for the agent
(`TurnOptions.voice_hint`). It is added in the same chokepoint as quotes,
`_compose_agent_text`, and the stored user row stays clean. The default asks
for a warm "podcast host":

- one to three spoken sentences;
- a follow-up question most turns;
- no lists, URLs, code or markdown. The reply says "I've put that in the
  thread" instead.

Gateway turns also run with `TurnOptions.thinking = "off"`
(`DISPATCH_VOICE_THINKING`), because hidden reasoning before the first word is
the largest latency cost. API and advisor bots ignore the per-turn thinking
override, so set `extra_body` in their own config.

## Turn-taking, turn merge and patience

Smart Turn sometimes calls a mid-sentence pause finished. In a road-noise
simulation it did so in 4 of 10 runs. Drive mode handles that in three ways:

- After a turn ends it **keeps listening** for `merge_window`
  (1.8 s × patience). The reply is **held**, not spoken, during that window.
- If you carry on and the new piece has real words:
  1. the early turn is **aborted**;
  2. its user row is **retracted**;
  3. the merged sentence is sent once.
- A cough or an empty transcript just releases the hold.

**Patience** (Quick 0.7 / Normal 1.0 / Patient 1.5) scales the merge window
and the hard-silence fallback. It is set per device in the Drive view.

**Barge-in.** If you start talking while the bot speaks, playback stops and
the turn is aborted. Only a gateway (agent) turn can be aborted: a direct-API
or advisor bot's reply is silenced but keeps generating, and still lands in
the thread. `speaking` lasts until the whole reply has been synthesised and
played, so the reply's own echo between two sentences never counts as a
barge-in. While audio is playing, 0.3 s of sustained speech is
required, so residual echo does not count. The browser asks for
`echoCancellation`. A transcript that matches what the bot just said is
dropped as echo.

## Protocol (`/ws/voice/{thread_id}`)

Client → server:

- binary frames: PCM16LE mono 16 kHz (the AudioWorklet sends 20 ms frames);
- JSON: `start {after, patience}`, `pause`, `stop_playback`,
  `playback_done {gen}`, `ping`.

Server → client:

- `ready {sample_rate_out, sample_format, streaming_tts, …}`;
- `state`, `eot`, `transcript {text, merged}`, `echo_ignored`, `ack`;
- `audio {gen, seq, chunk, sample_rate, text, message_id, kind}`, immediately
  followed by one binary PCM16 chunk;
- `barge_in {gen}`, `metrics`, `locked`, `error`.

Reply frames are read off the app's own broadcast through a non-blocking tap
registered with the connection manager under the same session token. On
reconnect, `start.after` makes the server speak up to three replies that
arrived while the link was down.

## Extension seam

`app/voice/registry.py` lets a package add TTS engines, a profile resolver, an
ack provider, a persona provider and routers. The routers are always mounted
behind the operator dependency. Load a package with
`DISPATCH_VOICE_EXTENSIONS=pkg.module:setup`. `dispatch-voice` is wired the
same way, from `app/voice/dv.py`.

## Configuration

| Variable | Default | |
|---|---|---|
| `DISPATCH_VOICE` | `auto` | `auto` = on iff deps + models present; `1`; `0` |
| `DISPATCH_SPEECH_MODELS` | `<data>/speech-models` | VAD, Smart Turn, Parakeet, Kokoro |
| `DISPATCH_VOICE_STT` | `parakeet` | speech-to-text engine (Parakeet is the only built-in one) |
| `DISPATCH_VOICE_TTS` | `auto` | `auto`, `chatterbox`, `kokoro`, `minimax` (needs `MINIMAX_API_KEY`), or a registered name |
| `DISPATCH_VOICE_KOKORO_VOICE` | `af_heart` | fallback voice |
| `DISPATCH_VOICE_KOKORO_SPEED` | `1.05` | Kokoro speaking rate |
| `DISPATCH_VOICE_MINIMAX_MODEL` / `_VOICE` / `_BASE_URL` | `speech-2.8-turbo` / `Wise_Woman` / `https://api.minimax.io` | the MiniMax cloud engine (`DISPATCH_VOICE_TTS=minimax`) |
| `DISPATCH_VOICE_HQ_URL` / `_MODEL` / `_TIMEOUT` | off / – / 4 s | optional remote engine. Any failure falls back for 2 min |
| `DISPATCH_VOICE_MERGE_WINDOW` | `1.8` | seconds, × patience |
| `DISPATCH_VOICE_PATIENCE` | `1.0` | default multiplier |
| `DISPATCH_VOICE_HARD_SILENCE` / `_SILENCE` | `1.4` / `0.8` | end-of-turn fallbacks with / without Smart Turn |
| `DISPATCH_VOICE_BARGE_ABORT` | `1` | barge-in also aborts the agent turn |
| `DISPATCH_VOICE_THINKING` | `off` | per-turn thinking level for gateway voice turns |
| `DISPATCH_VOICE_THREADS` / `_TTS_THREADS` | `6` / `8` | onnxruntime threads for VAD/STT and for Kokoro |
| `DISPATCH_VOICE_EXTENSIONS` | – | `module:setup` list |
| `DISPATCH_VOICE_PKG` | – | directory holding the optional `dispatch_voice` package (cloned voices) |

Fixed limits: one utterance is cut at 30 s; a merged utterance never grows
past 4,000 characters; one unlocked session may hold two voice sockets at once
(a third is refused with 1013, and the Drive view stops reconnecting after
three 1013s in a row); a reference clip upload is refused over 20 MB before it
is read. A drive slides the session's idle window on real audio and control
frames (about every 30 s), so a long drive does not lock mid-sentence.

`python -m app.voice.download` fetches pinned revisions and checks every file
against its sha256; `--verify` re-checks the files already on disk.

## Phones, cars and what the web platform allows

- **Screen locked or app backgrounded:** Android Chrome stops delivering
  microphone audio to a hidden page. Drive mode holds a Screen Wake Lock so
  the screen stays on, and re-acquires it on return. It cannot listen with the
  screen locked; that needs a native wrapper with a foreground service.
- **Steering-wheel / Bluetooth play-pause:** a Media Session is registered,
  kept active by a silent looping `<audio>` element. Play/pause toggles
  listening. Whether a car forwards those buttons to a web page is
  device-specific.
- **Android Auto** does not project PWAs. Audio reaches the car over Bluetooth
  like any media. Which microphone `getUserMedia` uses (car or phone) depends
  on the phone and the car.

## Tests

| Test | Covers |
|---|---|
| `backend/tests/test_voice_vad_turn.py` | endpointing on synthetic audio; numpy Whisper features (match `transformers` to 3e-7) |
| `backend/tests/test_voice_tts.py` | speakable filter, sentence splitter, the `speak()` seam |
| `backend/tests/test_voice_session.py` | the session with fakes: turn merge, barge-in, echo, acks, resume, the gate, idle-window touch, speaking-until-drained, worker failure, socket cap |
| `backend/tests/test_voice_registry.py` | extension seam: routers forced behind the gate, engine selection, fail-soft hooks |
| `backend/tests/test_voice_dv.py` | dispatch-voice adapter: panel 403 when locked, profile resolution, fallback, fillers (skips without the package) |
| `backend/tests/test_voice_ws_route.py` | the real app (skips until main.py is wired): locked, cross-origin, safe-bot and feature-off refusals on `/ws/voice`; 403 on `/api/voice*` when locked; a spoken turn persisted as a voice row with the reply spoken |
| `frontend/tests/drive.test.js` | pure helpers of the Drive view |
| `backend/tests/e2e_voice_roundtrip.py` | scratch-instance round trip with a stub LLM, over the raw socket in real time. `--browser` adds headless Chromium with a fake microphone; `--voice-clip` builds a cloned voice through `/api/voices` first |
