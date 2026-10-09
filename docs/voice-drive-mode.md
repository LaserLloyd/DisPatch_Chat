# Drive mode (hands-free voice)

Drive mode lets you talk to a DisPatch thread with your hands on the wheel. You
speak, DisPatch works out when you have finished and turns your words into
text, then sends them through the thread's **normal** send path. The bot's
reply is spoken back sentence by sentence while it is still being written.
Transcripts land in the thread like typed messages (tagged `metadata.voice`),
so any bot type works: gateway agents, API bots and advisors.

**Voice is optional.** It is off until you turn it on in **Settings → Voice**
(see [Turning voice on](#turning-voice-on)), and its advanced pieces (voice
cloning, the optional built-in voices, tuning and diagnostics) are separate
opt-in [voice tools](#voice-tools). An install that never turns it on loads
no speech models and shows no microphone button.

All speech processing runs on the DisPatch host's **CPU**. Only the bot's own
LLM call leaves the machine. There is no cloud speech service and no GPU, and
Drive mode keeps working while a GPU rig is busy or offline.

| Stage | Engine | Licence | Notes |
|---|---|---|---|
| Voice activity | Silero VAD (ONNX) | MIT | 32 ms frames, ~0.2 ms each |
| End of turn | Smart Turn v3.2 (ONNX) + silence fallback | BSD-2 | ~100 ms per check; hears intonation |
| Turn merge | in-house | — | keeps listening after a turn ends (below) |
| Speech-to-text | Parakeet TDT 0.6B v2 int8 via `onnx-asr` | CC-BY-4.0 / MIT | whole utterance, RTF ≈ 0.06–0.09 |
| Voice (default) | Chatterbox-Turbo ONNX q4 via the separate [`dispatch-voice`](#cloned-voices-dispatch-voice) package | MIT, ungated | cloned voices, plus a **stock synthetic voice** when none is cloned; a short first sentence takes about 2–4 s to synthesise on the reference CPU (Ryzen AI 9 365: 2.1 s for "Hey, what's up?", 4.0 s for a nine-word sample, engine warm), plus a one-off ~4.5 s engine load that the start-up prewarm absorbs |
| Voice (optional plugin) | Kokoro-82M **fp32** via the separately installed [`dispatch-voice-kokoro`](#optional-kokoro-voices-dispatch-voice-kokoro) plugin | plugin **GPL-3.0-or-later** (Apache-2.0 weights; GPL-3.0 phonemizer/espeak-ng) | built-in voices and a fast fallback; RTF ≈ 0.3 (fp32) vs ≈ 1.9 (int8), see below |
| Optional | "HQ" engine over OpenAI-compatible `/v1/audio/speech`; MiniMax T2A | — | opt-in; HQ always falls back locally |

## Licence

**The core voice feature is MIT-compatible.** Everything DisPatch installs or
downloads for Drive mode (the `voice` dependency group, `app.voice.download`
and the `dispatch-voice` package) is permissively licensed: MIT, BSD,
Apache-2.0 and CC-BY-4.0 model weights. soundfile's bundled libsndfile is
LGPL-2.1, loaded dynamically and replaceable. Installing Drive mode does not
change DisPatch's MIT licence. Attribution for the models and libraries is in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

**The Kokoro built-in voices are an optional GPL-3.0 plugin, installed
separately.** Kokoro's runtime converts text to sound units with phonemizer
and espeak-ng. Both are GPL-3.0-or-later, and both load into the Python
process. So the Kokoro engine lives in its own package, `dispatch-voice-kokoro`
(GPL-3.0-or-later), which DisPatch never imports by name. Install it only if
you want those voices. Once it is installed, your running DisPatch is a
combined work with GPL-3 code. That is fine for private use. If you
redistribute DisPatch together with the plugin (a Docker image, an appliance),
you must do so under GPL-3 terms.

The TTS engine sits behind one interface (`app/voice/tts.py`):

```python
async for frame in speak(text_chunks, profile_id, engine=...):   # PCM16LE mono frames
    ...
```

**Kokoro (plugin): use fp32, not int8.** Measured on the reference box (Ryzen AI 9 365,
AVX-512 VNNI, onnxruntime CPU, 2026-10-10), median of 3 runs, voice `af_heart`:

| Model | Threads | "Sure, you have two things this afternoon." | 21-word sentence | "Got it." | median RTF |
|---|---|---|---|---|---|
| int8 (`kokoro-v1.0.int8.onnx`, 88 MB) | 8 | 3.71 s | 8.07 s | 2.05 s | 1.89 |
| fp32 (`kokoro-v1.0.onnx`, 310 MB) | 8 | 0.63 s | 1.38 s | 0.33 s | 0.32 |
| int8 | 4 | 3.67 s | 7.78 s | 1.90 s | 1.83 |
| fp32 | 4 | 0.95 s | 2.09 s | 0.48 s | 0.48 |

int8 is slower than real time (its ConvInteger kernels are slow on x86 CPUs)
and does not improve with threads. The plugin's downloader therefore fetches
fp32, and the plugin picks fp32 whenever the file is present
(`DISPATCH_VOICE_KOKORO_MODEL=auto|fp32|int8`). The earlier "0.3–0.9 s per
short sentence" figure only holds for fp32.

Each engine declares `sample_rate`, `sample_format` (`pcm_s16le`), `channels`
(1) and `streaming`. `DISPATCH_VOICE_TTS=auto` picks the cloned-voice engine
when `dispatch-voice` is installed.

- **Without the Kokoro plugin** (the default install): Chatterbox speaks
  everything. A bot with no voice of its own uses the default profile, else
  the **stock voice** (`stock-host`, built from a bundled clip of fully
  synthetic speech, not a real person). A saved `builtin:` voice from an
  earlier install degrades to the same default instead of failing.
- **With the Kokoro plugin**: Kokoro sits behind Chatterbox, exactly as in
  2.1. It speaks when no voice profile is chosen, when a built-in voice is
  chosen, and when the cloned engine fails before producing audio. Without
  `dispatch-voice`, Kokoro alone is used.
- **Neither installed:** `/api/voice/status` lists "a text-to-speech engine"
  under `missing` and says what to install. Drive mode stays off and nothing
  crashes.

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

**Settings → Voice** has the everyday settings (the gear's *All Drive
settings* row opens it). The tab sits next to Device and appears only on an
unlocked device; a locked or Safe Mode device never shows it. It opens with
the [on/off switch](#turning-voice-on). While voice is off it shows only the
switch and, when something is missing on the server, what turning it on needs
and the commands that install it. Once voice is on:

| Group | Setting | Stored |
|---|---|---|
| Drive mode | Voice (Drive mode) on/off | the server (`voice.yaml`) |
| Voice | Default voice (with ▶ to hear a sample), speaking speed (only with built-in voices installed), Manage voices (only while the Voice cloning tool is on) | the server (`voice.yaml`) |
| Listening | Pause before replying, Interrupt by talking, Keep listening after a reply (off = push-to-talk), Sounds | this device |
| Replies | Reply length, Ask a follow-up question | this device |
| This device | Keep the screen on, Microphone, Speaker (desktop browsers that can choose one), Show what was heard | this device |

Per-device settings are one `localStorage` entry (`dispatch-drive`; a 2.1.0
`dispatch-drive-patience` value is carried over) and travel to the server on
the `start` frame. **Per-bot voice** is the **Voice** pill on each card in
Settings → Bots; it waits for Save like Safe / React / Pool.

Engine status, timing, the thinking level and a quick test moved out of this
tab into the **Voice lab** tool; adding and managing voices into **Voice
cloning**; the optional plugin's voices into **Built-in voices**. See
[Voice tools](#voice-tools).

## Turning voice on

Voice has one server-wide switch, **Settings → Voice → Voice (Drive mode)**.
It is stored as `enabled` in `<data>/voice.yaml` and applies live, without a
restart:

- **Off** (the default on a fresh install, even when every package and model
  is present): no microphone button, `/ws/voice` refuses an unlocked caller
  with 1013 and the reason "Voice is turned off in Settings › Voice" (the
  Drive screen says so in the driver's language), and no speech model is
  loaded at start-up. Turning it off while someone is driving ends that drive
  with the same 1013 and **releases the loaded models** (STT, TTS, Smart Turn,
  the Chatterbox sessions and every render cache), so the memory goes back.
  What a third-party plugin keeps in its own module globals is up to it.
- **On**: the start-up prewarm runs at once in the background (and at every
  later start-up), the microphone button appears, and Drive works as soon as
  the models are loaded. If something is missing, the tab lists it with the
  command that fixes it.

`DISPATCH_VOICE` overrides the switch: `1` forces voice on (the switch is
shown on and locked), `0` forces it off (the switch is hidden and the tab
says the server's settings turned it off). Unset or `auto` leaves it to the
switch. `PATCH /api/voice/settings {"enabled": …}` answers 409 while the
environment decides.

**Upgrading an install that already uses voice.** Before this switch,
`DISPATCH_VOICE=auto` meant "on whenever the models are present". Now it
means "on when the switch says so", and a `voice.yaml` without `enabled`
reads as off. Either turn it on once in Settings → Voice, or add one line to
`<data>/voice.yaml` (any other keys stay as they are):

```yaml
enabled: true
```

A hand edit of `enabled` has the same live effect as the switch: the server
re-reads `voice.yaml` when its modification time moves (on the next status
request or voice socket, and in any case within 5 s), then ends open drives
and releases the models (off) or starts the prewarm (on). No restart needed.
Saving from the app keeps only the keys it knows; comments in the file are
not preserved.

## Voice tools

The advanced pieces are three **builtin tools** in DisPatch's Tools framework
(Settings → Tools, the desktop Tools popup, the phone's Tools row). Unlike
the other builtins they are **opt-in**: a tool is on only when
`<data>/tools.yaml` has its row with `enabled: true`; no row (or no
`tools.yaml`) means it is simply not there, and its routes answer 404. They
are unlocked-tier only and never listed or served for Safe Mode. Switch them
on in **Settings → Tools** or in the file:

```yaml
tools:
  - {id: voice-cloning, kind: builtin, enabled: true}
  - {id: voice-builtin, kind: builtin, enabled: true}
  - {id: voice-lab, kind: builtin, enabled: true}
```

A tool whose package is missing still opens, and says what is missing and
how to install it (Settings → Tools marks it "Not available on this install").

| Tool | What it does | Needs |
|---|---|---|
| **Voice cloning** (`voice-cloning`) | The Voices manager: your cloned voices and the stock voice (▶, set as default, rename and delete for your own voices; the stock voice has a **Stock** badge and cannot be renamed or deleted), the built-in voices when the plugin is installed, and **Add a voice** (record with a script to read, or upload; quality checks; name; whose voice it is). Also owns the `/api/voices` API, which answers 404 while the tool is off. Voices already made keep speaking in Drive mode either way. | `dispatch-voice` (MIT) |
| **Built-in voices** (`voice-builtin`) | Status of the optional `dispatch-voice-kokoro` plugin (**GPL-3.0-or-later, installed separately, never shipped with DisPatch**): not installed / installed but not enabled / models missing / ready, with the commands for each; when ready, its voice list with ▶ samples and *Set as default*. | the plugin |
| **Voice lab** (`voice-lab`) | Engine status and anything missing; which engines are in use (cloned + built-in fallback, cloned only, stock only, built-in, cloud); whether the speech models are loaded; **Turn-taking**: the keep-talking (merge) window 0.5–4 s and the hard end of turn 0.8–3 s, and the thinking level of voice turns, all server-wide and applied from the next drive, with *Reset* back to the server's defaults; **Show timings on the Drive screen** (this device); the **cloud and rig engines** (MiniMax, the HQ engine) as status only, with the environment variables to set (keys and addresses never go through the app); and a **quick test**: record 3 s, see the transcript, hear it said back in the default voice, with the recognition, first-sound and whole-reply times. | DisPatch itself |

## Install the optional pieces

| Piece | Licence | How |
|---|---|---|
| Speech models (VAD, Smart Turn, Parakeet) and the voice deps | permissive | `cd backend && uv sync --group voice && uv run python -m app.voice.download` |
| [`dispatch-voice`](https://github.com/LaserLloyd/DisPatch_Voice) (cloned voices + the stock voice) | MIT | in `backend/`: `uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice" dispatch-voice fetch-models`, then run DisPatch with the same `--with` (`uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice" uvicorn app.main:app …`) |
| `dispatch-voice-kokoro` (built-in voices + fast fallback) | GPL-3.0-or-later | an optional plugin, distributed separately (not published with DisPatch): in its folder `uv run dispatch-voice-kokoro fetch-models`, then run DisPatch with `DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup` and the plugin in its environment (`uv run --group voice --with /path/to/dispatch-voice-kokoro …`) |

Then restart DisPatch and turn voice on. Details below in [Install](#install).

## Who can use it

**Unlocked sessions only.**

- The voice socket, `/api/voice/*` (status, settings, preview, the Voice lab
  routes) and the whole Voices API (`/api/voices/*`) refuse Safe Mode, locked
  devices and decoy sessions. The voice tools are never listed for them, and
  `#tool=voice-lab` does nothing on a locked device. The
  routes use `_require_operator`: a real unlocked session, or no PIN at all.
  Both prefixes are also on the middleware's Safe-Mode block list.
- Threads belonging to a Safe-Mode (`safe: true`) bot get no voice.
- The WebSocket gate runs inline before `accept()`, because HTTP middleware
  never sees a websocket. Order: Origin == Host → a live `lc_session` (when a
  PIN is set) → feature on (the switch, or `DISPATCH_VOICE`) → the thread exists and its bot is not safe →
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
uv sync --group voice                 # onnx-asr + the cloned-voice engine's runtime deps (all permissive)
uv run python -m app.voice.download   # VAD, Smart Turn, Parakeet (~680 MB)
# the voice: dispatch-voice (MIT), published at github.com/LaserLloyd/DisPatch_Voice
uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice" \
  dispatch-voice fetch-models         # Chatterbox-Turbo ONNX q4 (~690 MB, pinned) + builds the stock voice
# then run DisPatch with the same --with, so dispatch-voice is in its environment:
uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice" \
  uvicorn app.main:app --host 127.0.0.1 --port 8765
```

The speech models go to `DISPATCH_SPEECH_MODELS`, which defaults to
`<data dir>/speech-models`. Chatterbox's models go to its own
`~/.local/share/dispatch-voice/models`, which dispatch-voice's
`DISPATCH_VOICE_MODELS` overrides. The stock voice's tensors are cached there
too (`stock/stock-host.npz`). An install that fetched its models before the
stock voice existed builds it on first use or at the start-up prewarm (about
a second once the engine is loaded).

`dispatch-voice` is **not** a dependency of this project, so a fresh clone
locks and installs without it. DisPatch finds it like any installed package:
`--with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice"`
on `uv run` (as above; repeat it on every `uv run`, since `uv sync` does not
keep it), or `uv pip install` it into a virtualenv you manage yourself. To run
a **local checkout** instead, set `DISPATCH_VOICE_PKG` to the directory that
contains its `dispatch_voice` package (its `src/`): it goes first on the
import path, so it wins over an installed copy. The `voice` group already
carries the package's runtime dependencies; under `uv run`, add `--group
voice` so the group stays installed.

Restart DisPatch and turn voice on in **Settings → Voice** (or add
`enabled: true` to `<data>/voice.yaml`). With `DISPATCH_VOICE=auto` (the
default) the microphone button appears once the switch is on and everything
is present.

### Optional: Kokoro voices (dispatch-voice-kokoro)

The Kokoro built-in voices (Heart, Michael, Emma …) and the fast local
fallback are a **separate, optional plugin licensed GPL-3.0-or-later** (see
[Licence](#licence)). To add them:

```bash
# from the plugin's checkout
uv run dispatch-voice-kokoro fetch-models          # fp32 (~340 MB with voices); --int8 for ~120 MB
# run DisPatch with the plugin in its environment and enabled
cd backend
DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup \
  uv run --group voice --with /path/to/dispatch-voice-kokoro uvicorn app.main:app …
```

The plugin looks for its files in `DISPATCH_VOICE_KOKORO_DIR`, else in
`$DISPATCH_SPEECH_MODELS/kokoro` (where DisPatch 2.1 put them, so an existing
install keeps its files), else in `~/.local/share/dispatch-voice-models/kokoro`.

**Start-up prewarm.** When voice is on (the switch, or `DISPATCH_VOICE=1`), DisPatch loads Parakeet, Smart Turn,
Silero and the TTS engine in a background thread at start-up, runs one
throwaway synthesis (and loads Chatterbox when a default voice profile
exists, any non-safe bot names a profile, or Chatterbox is the last-resort
engine, which builds the stock voice if needed), then renders the default
voice's acknowledgements and error lines. A cloned voice's error lines are
rendered only when a turn actually fails: pre-rendering them held the
Chatterbox lock for ~6 s right when the first reply needed it. The first Drive
session after a restart (every backend deploy restarts the app) therefore
gets `ready` in well under a second and its first reply does not pay the
~4.5 s Chatterbox load. `DISPATCH_VOICE_PREWARM=0` turns it off (the test
suite does).

## Cloned voices (dispatch-voice)

Voices are managed in the opt-in **Voice cloning** tool
(`frontend/static/js/voices.js`, hosted by `js/voice-tools.js`, on the JSON
API below; Settings → Voice links to it with *Manage voices* while the tool
is on). dispatch-voice's own standalone page at `/api/voices/ui` belongs to
the same API, so inside DisPatch it answers only while the tool is on (404
otherwise; the app never links to it). For command-line use without
DisPatch, `dispatch-voice serve` serves it on its own. Either way you can:

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

**Stock voice.** dispatch-voice ships one short reference clip of fully
synthetic speech (Kokoro-82M output, Apache-2.0 weights; not a real person) and
turns it into an ordinary profile, `stock-host` ("Stock voice (synthetic)",
consent "synthetic"). It is never made the default by itself; it speaks only
when there is no other voice. You can pick it as the default like any other
profile.

**Per-bot voice.** A bot's `voice:` key in `config.yaml` names its profile,
or a built-in voice as `builtin:<id>` when the Kokoro plugin is installed
(e.g. `builtin:am_michael`). Empty means the default voice. An unknown id, or
a `builtin:` id with the plugin gone, falls back to the default without
putting the cloned engine on its failure cooldown. The Bots tab sets
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

- `GET /api/voice/status` — `enabled` (Drive is offered), `switch` (the
  on/off switch as it stands, env override included), `forced` (`"on"`,
  `"off"` or `null`), `ready`, `missing`, `stt`, `tts`,
  `smart_turn`, `patience`, `patience_presets`, `speed`,
  `voice_turn_thinking`, `default_voice` (a profile id, `builtin:<id>`, or
  `""` when nothing can speak yet), `builtin_voices [{id, name, lang,
  default?}]` (**empty** without a built-in voice plugin), `profiles [{id,
  name}]` (includes `stock-host` once it exists), `warm` (prewarm done),
  `extensions`, `voices_ui`, `merge_window_s`, `hard_silence_s`,
  `stock_voice` (`stock-host` while dispatch-voice is mounted, else `null`),
  `plugins {cloning: {installed, ready, missing}, builtin: {installed,
  package, ready, missing, licence}}` and `tools` (which voice tools are on).
  The 2.1 field `kokoro_model` is gone.
- `GET /api/voice/settings` → `{enabled, forced, default_voice, speed,
  voice_turn_thinking, merge_window_s, hard_silence_s}`.
- `PATCH /api/voice/settings` with any subset. `enabled` true/false (409 while
  `DISPATCH_VOICE` decides; applied live, see [Turning voice
  on](#turning-voice-on)), `merge_window_s` 0.5–4.0, `hard_silence_s` 0.8–3.0,
  `reset: [...]` (any of `merge_window_s`, `hard_silence_s`,
  `voice_turn_thinking`: back to the server default), `speed` 0.8–1.3 (built-in
  voices; the cloned and stock voices ignore it), `voice_turn_thinking` one of
  `off|low|medium`, `default_voice` a profile id (it becomes the profile
  store's default) or `builtin:<id>` (clears the profile default, so the
  built-in voice speaks). A `builtin:` id with no plugin installed is a 422.
  Anything else is a 422. Saved to
  `<data>/voice.yaml` (0600) and applied live: the next turn and the next
  sentence use it, no restart.
- `GET /api/voice/preview?voice=<profile id | builtin:<id>>&text=<≤120 chars>`
  → `audio/wav` ("Hi, this is how I'll sound in Drive mode." by default),
  cached per voice and speed. A built-in voice is rendered by its provider.
  503 while voice is off.
- `GET /api/voice/lab` (Voice lab tool on, else 404) — the switch and
  readiness, the engines in use and their fallback, whether models are
  loaded and the prewarm state, MiniMax (`selected`, `key_set`, model, voice;
  never the key) and the HQ engine (`configured`, host only, model, timeout),
  the thinking level and the two timings with their default and range.
- `POST /api/voice/lab/test` (Voice lab tool on, else 404) — body: 0.5–10 s of
  16 kHz mono PCM16. 503 while voice is off or not ready, 409 while another
  test runs, 413 over 10 s. Returns `{transcript, said, heard_ms, load_ms,
  stt_ms, tts_first_ms, tts_total_ms, spoken_ms, audio_wav_b64}`.

## Extension seam

`app/voice/registry.py` lets a package add TTS engines, a profile resolver, an
ack provider, a persona provider, a built-in voice provider and routers. The
routers are always mounted behind the operator dependency. Load a package with
`DISPATCH_VOICE_EXTENSIONS=pkg.module:setup`; `setup(api)` gets
`api.api_version` (2 since the licence split, 3 since the voice tools).

- `api.register_engine(name, factory, role="primary"|"fallback",
  missing=fn, sample_rate=int)`. A ready `fallback` engine with the same
  sample rate goes behind the primary one; with no primary it speaks alone.
  `missing(settings)` feeds `/api/voice/status`.
- `api.set_builtin_voices_provider(list_fn, preview=fn, info=dict)`: the
  `builtin:<id>` list for Settings and the preview renderer. `info` (API 3)
  is how the Built-in voices tool describes the provider: `{name, licence,
  models: [{where, cmd}]}` with `where` one of `plugin`, `env`, `backend`.
  DisPatch hard-codes no plugin; with no provider registered, the tool
  suggests the documented one (`dispatch-voice-kokoro`, GPL-3.0-or-later)
  and its install steps (`routes.SUGGESTED_BUILTIN`).
- `api.add_release_hook(fn)` (API 3): called when voice is switched off,
  after DisPatch dropped its own engines, so a plugin frees the models it
  caches itself. The Kokoro plugin registers one.
- Optional engine attributes: `cache_tag(profile_id)` opts in to the disk
  cache under `<data>/voice-cache/<tag>/` (only for deterministic output),
  `apply_settings(settings)` receives voice/speed changes live, `warm()` runs
  at the start-up prewarm. A cache tag must be a plain name
  (`[A-Za-z0-9][A-Za-z0-9._-]{0,99}`); only the 8 most recently written tag
  directories are kept.

`dispatch-voice` is wired from `app/voice/dv.py`; the Kokoro plugin uses this
seam and nothing else.

## Configuration

| Variable | Default | |
|---|---|---|
| `DISPATCH_VOICE` | `auto` | `auto` = the Settings → Voice switch decides (off until turned on); `1` forces on; `0` (or anything else) forces off |
| `DISPATCH_SPEECH_MODELS` | `<data>/speech-models` | VAD, Smart Turn, Parakeet (and `kokoro/` for the plugin) |
| `DISPATCH_VOICE_STT` | `parakeet` | speech-to-text engine (Parakeet is the only built-in one) |
| `DISPATCH_VOICE_TTS` | `auto` | `auto`, `chatterbox`, `minimax` (needs `MINIMAX_API_KEY`), or a registered name (`kokoro` with the plugin) |
| `DISPATCH_VOICE_BUILTIN_VOICE` | provider default | built-in voice when no profile applies (alias: `DISPATCH_VOICE_KOKORO_VOICE`) |
| `DISPATCH_VOICE_SPEED` | `1.05` | speaking rate of built-in voices (alias: `DISPATCH_VOICE_KOKORO_SPEED`) |
| `DISPATCH_VOICE_KOKORO_MODEL` / `_KOKORO_DIR` | `auto` / `$DISPATCH_SPEECH_MODELS/kokoro` | read by the Kokoro plugin only |
| `DISPATCH_VOICE_PREWARM` | `1` | load the speech models in the background at start-up |
| `DISPATCH_VOICE_MINIMAX_MODEL` / `_VOICE` / `_BASE_URL` | `speech-2.8-turbo` / `Wise_Woman` / `https://api.minimax.io` | the MiniMax cloud engine (`DISPATCH_VOICE_TTS=minimax`) |
| `DISPATCH_VOICE_HQ_URL` / `_MODEL` / `_TIMEOUT` | off / – / 4 s | optional remote engine. Any failure falls back for 2 min |
| `DISPATCH_VOICE_MERGE_WINDOW` | `1.8` | seconds, × patience |
| `DISPATCH_VOICE_PATIENCE` | `1.0` | default multiplier |
| `DISPATCH_VOICE_HARD_SILENCE` / `_SILENCE` | `1.4` / `0.8` | end-of-turn fallbacks with / without Smart Turn |
| `DISPATCH_VOICE_BARGE_ABORT` | `1` | barge-in also aborts the agent turn |
| `DISPATCH_VOICE_THINKING` | `off` | per-turn thinking level for gateway voice turns |
| `DISPATCH_VOICE_THREADS` / `_TTS_THREADS` | `6` / `8` | onnxruntime threads for VAD/STT and for a local TTS plugin |
| `DISPATCH_VOICE_EXTENSIONS` | – | `module:setup` list |
| `DISPATCH_VOICE_PKG` | – | optional override: a local dispatch-voice checkout's `src/` (wins over an installed `dispatch-voice`; not needed when the package is installed) |

`<data>/voice.yaml` (written by `PATCH /api/voice/settings`, keys
`enabled`, `builtin_voice`, `speed`, `voice_turn_thinking`,
`merge_window_s`, `hard_silence_s`) holds the switch and overrides
`DISPATCH_VOICE_BUILTIN_VOICE`, `_SPEED`, `_THINKING`, `_MERGE_WINDOW` and
`_HARD_SILENCE`; the other variables
are install decisions and stay environment-only. A 2.1 `voice.yaml`
(`kokoro_voice`, `kokoro_speed`) is still read, and the next save rewrites it
with the new names.

Fixed limits: one utterance is cut at 30 s; a merged utterance never grows
past 4,000 characters; one unlocked session may hold two voice sockets at once
(a third replaces the oldest, which is closed with 4001 and a "moved to
another tab or device" note). On an install **without a PIN** there is no
session, so the count is per browser: drive.js sends a random device id
(`?d=`, kept in `localStorage['dispatch-drive-device']`), and one family
member's drive never ends another's; a client that sends none is counted on
its own. `undo` works for 8 s and only until the answer starts (once a reply
sentence is queued or the turn is answered, `undo_expired`; the Drive screen
hides Undo on the first reply audio);
a turn with nothing spoken after 120 s is reported as `timeout`; a reference
clip upload is refused over 20 MB before it is read. Acknowledgements and
error lines of an engine that opts in (the Kokoro plugin) are cached under
`<data>/voice-cache/`. A dispatch-voice profile (a cloned voice or the stock
voice) uses its own fillers, rendered once by dispatch-voice and stored in
the profile; DisPatch never renders a second set for it. Samples (▶) and the
Voice lab test wait until no reply sentence is being synthesised, one at a
time. A drive slides the session's idle window on real audio and control
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
| `backend/tests/test_voice_dv.py` | dispatch-voice adapter: panel 403 when locked, profile resolution, fallback (no cooldown for a built-in or unknown id), fillers, default profile, the stock voice as last resort, a saved `builtin:` voice with no plugin (skips without the package; `DISPATCH_VOICE_PKG` is enough) |
| `backend/tests/test_voice_engine_split.py` | the licence split: no provider (empty list, clear `missing`, 422 not a crash), engine roles (fallback pairing, sample rates, readiness), a fake plugin through `DISPATCH_VOICE_EXTENSIONS` (list, default, preview, live settings), the 2.1 setting aliases, no `kokoro` import in `app/` |
| `backend/tests/test_voice_drive_v2.py` | the v2 contract with fakes: ping/pong, ready, start fields and re-send, undo, cancel, `turn_error` (agent error, nothing speakable, timeout, TTS failure), one answer per question (M3/M4), silent-socket lock, heartbeat, eviction, rate limit, echo with digits, persona variants |
| `backend/tests/test_voice_drive_v2_app.py` | the real app: `/api/voice/settings` (persist, validate, live), `/api/voice/preview`, status fields, per-bot voice on the Bots save, the abort hook (API/advisor cancel, queued turns, agent seat), the downloader (no Kokoro), background renders yielding to replies, opt-in disk cache, prewarm |
| `backend/tests/test_voice_ws_route.py` | the real app (skips until main.py is wired): locked, cross-origin, safe-bot and feature-off refusals on `/ws/voice`; 403 on `/api/voice*` when locked; a spoken turn persisted as a voice row with the reply spoken |
| `frontend/tests/drive.test.js` | the Drive view's pure half: reconnect rules (1008/1013/1011/4001/network), heartbeat, tap meaning and hint per state, thinking phases, mic pre-buffer, undo window, heard-id tracking, error wording, player gen/drop, device settings (defaults, junk, 2.1.0 migration, listeners), the `start` payload, voice labels, the Voices manager's build gate, and the mic worklet's anti-alias filter at 48 and 44.1 kHz |
| `backend/tests/e2e_voice_roundtrip.py` | scratch-instance round trip with a stub LLM, over the raw socket in real time; reports connect→`ready`, end of speech→first reply audio, and speech onset→`duck`/`barge_in`. `--browser` adds headless Chromium with a fake microphone (`uv run --group voice --with playwright …`, `--chromium <path>` for a system Chromium); `--voice-clip` builds a cloned voice through `/api/voices` first; `--wait-warm` waits for the start-up prewarm |

Run the backend voice tests with the optional package and the real models so
nothing skips:

```bash
cd backend
DISPATCH_SPEECH_MODELS=~/path/to/speech-models \
  uv run --group voice --with "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice" pytest -q tests/test_voice_*.py
# or, with a local checkout: DISPATCH_VOICE_PKG=~/path/to/dispatch-voice/src uv run --group voice pytest …
```

The Kokoro plugin has its own tests, including one that runs DisPatch and the
plugin together (see the plugin's README).
