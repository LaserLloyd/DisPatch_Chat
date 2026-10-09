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
| Voice (fallback / built-in) | Kokoro-82M **fp32** via `kokoro-onnx` (int8 if that is all there is) | Apache-2.0 weights; **GPL-3.0** phonemizer/espeak-ng at runtime | built-in voices; RTF ≈ 0.3 (fp32) vs ≈ 1.9 (int8), see below |
| Optional | "HQ" engine over OpenAI-compatible `/v1/audio/speech`; MiniMax T2A | — | opt-in; HQ always falls back locally |

The TTS engine sits behind one interface (`app/voice/tts.py`):

```python
async for frame in speak(text_chunks, profile_id, engine=...):   # PCM16LE mono frames
    ...
```

**Kokoro: use fp32, not int8.** Measured on the reference box (Ryzen AI 9 365,
AVX-512 VNNI, onnxruntime CPU, 2026-10-10), median of 3 runs, voice `af_heart`:

| Model | Threads | "Sure, you have two things this afternoon." | 21-word sentence | "Got it." | median RTF |
|---|---|---|---|---|---|
| int8 (`kokoro-v1.0.int8.onnx`, 88 MB) | 8 | 3.71 s | 8.07 s | 2.05 s | 1.89 |
| fp32 (`kokoro-v1.0.onnx`, 310 MB) | 8 | 0.63 s | 1.38 s | 0.33 s | 0.32 |
| int8 | 4 | 3.67 s | 7.78 s | 1.90 s | 1.83 |
| fp32 | 4 | 0.95 s | 2.09 s | 0.48 s | 0.48 |

int8 is slower than real time (its ConvInteger kernels are slow on x86 CPUs)
and does not improve with threads. The downloader therefore fetches fp32, and
the server picks fp32 whenever the file is present
(`DISPATCH_VOICE_KOKORO_MODEL=auto|fp32|int8`). The earlier "0.3–0.9 s per
short sentence" figure only holds for fp32.

Each engine declares `sample_rate`, `sample_format` (`pcm_s16le`), `channels`
(1) and `streaming`. `DISPATCH_VOICE_TTS=auto` picks the cloned-voice engine
when `dispatch-voice` is installed, with Kokoro behind it. Kokoro speaks only
when no voice profile exists yet or the cloned engine fails before producing
audio. Without `dispatch-voice`, Kokoro alone is used.

## Using Drive mode

Open a chat and tap the **microphone** in its header (unlocked devices only,
and only when the server reports voice on). The Drive screen is built for a
glance:

- **The bot's picture in a ring** says what is happening. A green dot on a
  calm ring: listening. Green ring that grows with your voice: hearing you.
  Amber arcs going round: working on it (after 4 s the word under it counts
  the seconds). Ring in the accent colour, moving with the reply: speaking.
  Grey dashed: paused. Red dashed: no connection, retrying. Solid red: Drive
  mode stopped. With reduced motion turned on in the system, the colours and
  dash patterns stay and the movement goes.
- **The line under the state word says what a tap on the picture does**:
  pause, listen again, interrupt the reply, or cancel a turn that is taking
  too long ("Still working — tap to cancel" after 25 s).
- **Start talking after the tone.** Words you say while it is still
  connecting are kept (up to 10 s) and sent the moment the server can hear.
- **Undo** appears for 8 s after DisPatch shows what it heard. It takes the
  message back before the bot answers. On a keyboard: `U`.
- **Errors appear on the screen and as a short falling tone**, never only as
  a pop-up: what happened and what to do ("That didn't send. Say it again.").
- **Pause / Resume** and **End drive** are the two big buttons at the bottom.
  The phone's Back button and `Esc` also end the drive; `Space` pauses.
- When the screen turns off, Drive pauses itself (a hidden page loses the
  microphone) and says "Paused while the screen was off" when you come back.

**The gear** opens a sheet with the settings you might change on the road:
the voice, *Pause before replying* (Short / Normal / Long — choose Long if it
keeps cutting you off), *Interrupt by talking* (turn off in a noisy car),
*Reply length* and the microphone. Changes apply at once.

**Settings → Device → Drive mode** has everything else:

| Group | Setting | Stored |
|---|---|---|
| Voice | Default voice (with ▶ to hear a sample), speaking speed (built-in voices), Manage voices | the server (`voice.yaml`) |
| Listening | Pause before replying, Interrupt by talking, Keep listening after a reply (off = push-to-talk), Sounds | this device |
| Replies | Reply length, Ask a follow-up question | this device |
| This device | Keep the screen on, Microphone, Speaker (desktop browsers that can choose one), Show what was heard | this device |
| Advanced | which speech engine is in use, anything missing on the server, thinking during voice turns | the server |

Device settings are one `localStorage` entry (`dispatch-drive`; a 2.1.0
`dispatch-drive-patience` value is carried over) and travel to the server on
the `start` frame. **Per-bot voice** is the **Voice** pill on each card in
Settings → Bots; it waits for Save like Safe / React / Pool.

**Manage voices** opens the Voices manager inside Settings: your cloned
voices (play a sample, set as default, rename, delete), the built-in voices,
and **Add a voice** — record now (with a script to read) or upload a clip,
see the quality checks, name it, and say whose voice it is (required).

## Who can use it

**Unlocked sessions only.**

- The voice socket, `/api/voice/status` and the whole Voices panel
  (`/api/voices/*`) refuse Safe Mode, locked devices and decoy sessions. The
  routes use `_require_operator`: a real unlocked session, or no PIN at all.
  Both prefixes are also on the middleware's Safe-Mode block list.
- Threads belonging to a Safe-Mode (`safe: true`) bot get no voice.
- The WebSocket gate runs inline before `accept()`, because HTTP middleware
  never sees a websocket. Order: Origin == Host → a live `lc_session` (when a
  PIN is set) → feature on → the thread exists and its bot is not safe →
  models present. Every refusal of an unauthorised or cross-origin caller,
  an unknown thread or a safe bot closes with **1008 before accept**: no
  socket. "Voice is off / models missing / models failed to load" is only
  ever told to a caller that passed the Origin and session checks, and it is
  **accepted, then closed with 1013** and a reason, so the browser sees 1013
  rather than 1006 and can say "Voice isn't set up on this server".
- The session is re-checked on every frame (at most once a second) and by a
  timer every 15 s while the socket is silent (paused). If it has lapsed, the
  server sends `locked` and closes with 1008.

**The microphone needs a secure context.** Browsers grant the microphone only
to `https://` pages or `localhost`. A phone must therefore open DisPatch
through an HTTPS address, such as a reverse proxy or tailnet HTTPS.

## Install

```bash
cd backend
uv sync --group voice                 # onnx-asr, kokoro-onnx + the cloned-voice engine's runtime deps
uv run python -m app.voice.download   # VAD, Smart Turn, Parakeet, Kokoro fp32 (~1 GB)
#   --kokoro-int8 fetches the 90 MB int8 Kokoro instead (~6x slower on x86)
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

**Start-up prewarm.** When voice is on, DisPatch loads Parakeet, Smart Turn,
Silero and the TTS engine in a background thread at start-up, runs one
throwaway synthesis (and loads Chatterbox when a default voice profile
exists, or any non-safe bot names a profile), then renders the built-in
voice's acknowledgements and error lines. A cloned voice's error lines are
rendered only when a turn actually fails: pre-rendering them held the
Chatterbox lock for ~6 s right when the first reply needed it. The first Drive
session after a restart (every backend deploy restarts the app) therefore
gets `ready` in well under a second and its first reply does not pay the
~4.5 s Chatterbox load. `DISPATCH_VOICE_PREWARM=0` turns it off (the test
suite does).

## Cloned voices (dispatch-voice)

Voices are managed in **Settings → Device → Drive mode → Manage voices**
(`frontend/static/js/voices.js`, on the JSON API below). dispatch-voice's own
standalone page at `/api/voices/ui` is still served, for `dispatch-voice
serve` and command-line use, but the app no longer links to it. Either way
you can:

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

**Per-bot voice.** A bot's `voice:` key in `config.yaml` names its profile,
or a built-in voice as `builtin:<kokoro id>` (e.g. `builtin:am_michael`).
Empty means the default voice. An unknown id falls back to the default
without putting the cloned engine on its failure cooldown. The Bots tab sets
it through `PUT /api/bots/order` (`voice` per bot: `""`, a profile id or a
known `builtin:` id, anything else is a 422; an absent key leaves it alone).
`voice` appears only in that full-access response and in `GET /api/bots/all`,
never in the `bots` broadcast.

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

- one to three spoken sentences (`start.reply_style`: `short` = one, two at
  most; `longer` = a short paragraph of five or six);
- a follow-up question most turns (`start.follow_up: false` = no closing
  question unless one is needed);
- no lists, URLs, code or markdown. The reply says "I've put that in the
  thread" instead.

A persona extension's text is used as it is; a non-default reply length or
follow-up choice is appended to it as one extra line.

Gateway turns also run with `TurnOptions.thinking = "off"`
(`DISPATCH_VOICE_THINKING`, or `voice_turn_thinking` in Settings), because hidden reasoning before the first word is
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

**Barge-in is two-step (echo-safe).** If the mic hears 0.35 s of sustained
speech while the bot speaks, the server only **ducks**: it sends
`duck {on: true}` (the client lowers or pauses playback) and holds further
reply audio. Synthesis carries on. When that utterance ends it is
transcribed:

- it matches what the bot just said (our own echo: ≥ 80 % of its words, digits
  ignored because STT writes "at 3" for a spoken "at three") → `echo_ignored`,
  `duck {on: false}`, and playback carries on where it paused;
- it is real words → `barge_in {gen}` (drop queued audio), the turn is
  stopped, and the new words are sent as the next message.

Measured with the reply fed back into the mic at -10 dB and -20 dB (no AEC):
before, the echo killed the reply 0.4–0.6 s into the first sentence; now
every sentence of the reply is spoken. `start.barge_in: false` makes the mic
deaf while the bot speaks (noisy car).

**Stopping a turn.** Barge-in, merge, `undo` and `cancel` all stop the turn
the same way (`main._voice_abort`): every voice turn still queued behind the
thread lock is cancelled before it runs, and the running one is stopped as
its backend allows (API and advisor turns: the task is cancelled; gateway
turns: the gateway's abort, by the bot's agent seat). The session also mutes
anything the abandoned turn still produces, even if it arrives after the next
question (it attributes each `thinking: started` run to the oldest voice turn
whose user row has echoed). A new question asked while the last one is still
being worked on replaces it: one answer, never two.

**Failures are spoken.** An agent error, a reply with nothing speakable
(only pictures, reactions or emoji, or a deduplicated reply), a turn with no
answer after 120 s, or a TTS failure sends `turn_error`, plays a short line
in the session voice ("Sorry, that didn't go through.", "Nothing to read out.
It's in the chat.", "Still working on that. It'll be in the chat.") and
returns to `listening`. Drive mode never sits on "Thinking".

**Merge hold.** While the merge window holds a reply, its first sentence is
already being synthesised; only the sending waits.

**A short first piece.** Synthesis time grows with length (the cloned voice
has a fixed ~1.2 s per piece plus roughly real time), and the driver waits for
the first piece only. So the first piece is cut at a clause end (`,` `;` `:`
`—`) within 48 characters when there is one, else within 80, and a long first
sentence of a whole (non-streamed) reply is split the same way. Later
sentences are untouched.

**A deduplicated reply.** DisPatch drops an assistant message identical to a
very recent one in the thread. If that happens to a voice turn's answer (a
stub LLM repeating itself, or the same question twice in a row), nothing new
reaches the thread and the driver hears "Nothing to read out. It's in the
chat."

## Protocol (`/ws/voice/{thread_id}`)

The full wire reference is the docstring of `app/voice/session.py`; the
binding client/server contract for v2 is summarised here.

**Connect.** Gate (above) → accept → models (instant after the prewarm) →
`ready`. The client buffers mic audio until `ready` (cap 10 s) and then
flushes it, so words spoken while connecting are not lost.

**Sockets per session.** One unlocked session (or the PIN-less install) holds
at most two voice sockets. A newer one **replaces the oldest**, which is
closed with **4001 "replaced"** (the client closes quietly and does not
reconnect). A phone whose old socket went half-open in a tunnel can therefore
always reconnect at once. A client that has sent a `ping` and then stays
silent for 30 s is dropped (1001).

Client → server:

- binary frames: PCM16LE mono 16 kHz (the AudioWorklet sends 20 ms frames);
- `start {after?, patience, barge_in, reply_style, follow_up}` — begin or
  resume; may be re-sent at any time to change settings without resetting
  the conversation (the backlog for `after` is read once per value). Unknown
  values fall back to the defaults silently;
- `ping {t}` (every 10 s) → `pong {t}` at once;
- `undo` — within 8 s of the last transcript: the turn is stopped, its user
  row is deleted, its reply muted; `undone {message_id}` then
  `state listening`. Later: `error {code: "undo_expired"}`;
- `cancel` — stop the pending or running turn and playback; `state listening`;
- `pause`, `stop_playback` as before; `playback_done {gen, seq?}` — the
  optional `seq` (last audio seq played) lets the server ignore a drain report
  for an ack that finished before the reply started;
- `start.after_ids` (optional, ≤ 8 heard reply ids, newest first): when
  `after` is a provisional stream id the server never stored, resume after the
  first of these that exists.

Server → client:

- `ready {sample_rate_out, sample_format, streaming_tts, barge_in, reply_style, follow_up, …}`;
- `state`, `eot`, `transcript {text, merged, message_id}`, `echo_ignored`, `ack`;
- `audio {gen, seq, chunk, sample_rate, text, message_id, kind}`, immediately
  followed by one binary PCM16 chunk; `kind` is `reply`, `ack` or `notice`
  (a spoken error line);
- `duck {on, gen}` (see barge-in), `barge_in {gen}`;
- `turn_error {message_id, reason, detail?}` with reason `agent_failed`,
  `no_speech_in_reply`, `timeout` or `tts_failed`;
- `undone {message_id}`, `pong {t}`, `metrics`, `locked`;
- `error {code, message}` with code `undo_expired`, `stt_failed`, `bad_frame`,
  `rate_limited` or `unavailable`. Control frames have a budget of 40, refilled
  at 10 per second; past it they are dropped with one `rate_limited` every 2 s.

Close codes: 1008 refused/locked, 1013 voice unavailable (after accept, with a
reason), 4001 replaced, 1001 heartbeat lost, 1011 internal error. The client
stops on 1008 and 1013 (the reason on the overlay; Try again for 1013), closes
quietly on 4001, and treats 1001, 1011 (after a notice saying what happened)
and a bare network drop alike: reconnect with backoff for up to 60 s, then
Stopped with Try again.

Reply frames are read off the app's own broadcast through a non-blocking tap
registered with the connection manager under the same session token. On
reconnect, `start.after` makes the server speak up to three replies that
arrived while the link was down.

## HTTP (unlocked only; Safe Mode 403)

All under `/api/voice`, behind `_require_operator` like `/api/voice/status`,
and on the middleware's Safe-Mode block list.

- `GET /api/voice/status` — `enabled`, `ready`, `missing`, `stt`, `tts`,
  `kokoro_model`, `smart_turn`, `patience`, `patience_presets`, `speed`,
  `voice_turn_thinking`, `default_voice` (a profile id or `builtin:<id>`),
  `builtin_voices [{id, name, lang}]` (American and British English),
  `profiles [{id, name}]`, `warm` (prewarm done), `extensions`, `voices_ui`.
- `GET /api/voice/settings` → `{default_voice, speed, voice_turn_thinking}`.
- `PATCH /api/voice/settings` with any subset. `speed` 0.8–1.3 (the built-in
  voice; the cloned engine ignores it), `voice_turn_thinking` one of
  `off|low|medium`, `default_voice` a profile id (it becomes the profile
  store's default) or `builtin:<id>` (clears the profile default, so the
  built-in voice speaks). Anything else is a 422. Saved to
  `<data>/voice.yaml` (0600) and applied live: the next turn and the next
  sentence use it, no restart.
- `GET /api/voice/preview?voice=<profile id | builtin:<id>>&text=<≤120 chars>`
  → `audio/wav` ("Hi, this is how I'll sound in Drive mode." by default),
  cached per voice and speed.

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
| `DISPATCH_VOICE_KOKORO_MODEL` | `auto` | `auto` (fp32 if present, else int8), `fp32`, `int8` |
| `DISPATCH_VOICE_PREWARM` | `1` | load the speech models in the background at start-up |
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

`<data>/voice.yaml` (written by `PATCH /api/voice/settings`) overrides
`DISPATCH_VOICE_KOKORO_VOICE`, `_KOKORO_SPEED` and `_THINKING`; the other
variables are install decisions and stay environment-only.

Fixed limits: one utterance is cut at 30 s; a merged utterance never grows
past 4,000 characters; one unlocked session may hold two voice sockets at once
(a third replaces the oldest, which is closed with 4001); `undo` works for 8 s;
a turn with nothing spoken after 120 s is reported as `timeout`; a reference
clip upload is refused over 20 MB before it is read. Built-in-voice
acknowledgements and error lines are cached under `<data>/voice-cache/`. A drive slides the session's idle window on real audio and control
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
| `backend/tests/test_voice_dv.py` | dispatch-voice adapter: panel 403 when locked, profile resolution, fallback (no cooldown for a built-in or unknown id), fillers, default profile (skips without the package; `DISPATCH_VOICE_PKG` is enough) |
| `backend/tests/test_voice_drive_v2.py` | the v2 contract with fakes: ping/pong, ready, start fields and re-send, undo, cancel, `turn_error` (agent error, nothing speakable, timeout, TTS failure), one answer per question (M3/M4), silent-socket lock, heartbeat, eviction, rate limit, echo with digits, persona variants |
| `backend/tests/test_voice_drive_v2_app.py` | the real app: `/api/voice/settings` (persist, validate, live), `/api/voice/preview`, status fields, per-bot voice on the Bots save, the abort hook (API/advisor cancel, queued turns, agent seat), Kokoro model choice, the downloader, background renders yielding to replies, disk cache, prewarm |
| `backend/tests/test_voice_ws_route.py` | the real app (skips until main.py is wired): locked, cross-origin, safe-bot and feature-off refusals on `/ws/voice`; 403 on `/api/voice*` when locked; a spoken turn persisted as a voice row with the reply spoken |
| `frontend/tests/drive.test.js` | the Drive view's pure half: reconnect rules (1008/1013/1011/4001/network), heartbeat, tap meaning and hint per state, thinking phases, mic pre-buffer, undo window, heard-id tracking, error wording, player gen/drop, device settings (defaults, junk, 2.1.0 migration, listeners), the `start` payload, voice labels, the Voices manager's build gate, and the mic worklet's anti-alias filter at 48 and 44.1 kHz |
| `backend/tests/e2e_voice_roundtrip.py` | scratch-instance round trip with a stub LLM, over the raw socket in real time; reports connect→`ready`, end of speech→first reply audio, and speech onset→`duck`/`barge_in`. `--browser` adds headless Chromium with a fake microphone (`uv run --group voice --with playwright …`, `--chromium <path>` for a system Chromium); `--voice-clip` builds a cloned voice through `/api/voices` first; `--wait-warm` waits for the start-up prewarm |

Run the backend voice tests with the optional package and the real models so
nothing skips:

```bash
cd backend
DISPATCH_VOICE_PKG=~/path/to/dispatch-voice/src DISPATCH_SPEECH_MODELS=~/path/to/speech-models \
  uv run --group voice pytest -q tests/test_voice_*.py
```
