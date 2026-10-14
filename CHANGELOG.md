# Changelog

All notable changes to DisPatch Chat are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.3.3] — 2026-10-14

### Fixed

- **The reaction pool's daily run never mints into a full shelf.** Each mood's
  quota (fresh images plus the ones used since the last run) is capped at the
  room left under its target, so a full shelf costs nothing and a used image
  is replaced the next morning. The target (`per_mood`) is the outage buffer:
  how many unused images stay on hand per mood if the image server is away.
  Until now a full 32-mood shelf with a daily add of 10 rendered about 470
  images a day, retiring an unused one for each.

## [2.3.2] — 2026-10-14

A review pass over 2.3.0/2.3.1.

### Fixed

- The AI models panel's **system prompt** now reaches a direct-API bot
  (`advisor.persona`); a blank field never clears a saved one.
- **Lookups on/off keeps a hand-picked list**: switching a bot's lookups off and
  on again restores the list it had (`advisor.tools_picked`).
- **Test connection** re-tests a connected bot without re-typing its key, and
  only credential-shaped environment names are ever read for a probe.
- Safe-Mode turns: the locked prompt no longer tells the model to answer from
  its own knowledge ahead of the lookups it has.
- Agent ids no longer accept a dot; Settings → Plugins shows the restart hint
  exactly when the server says a restart is pending; an add-in asking for a
  Settings tab before the host exists gets a clear error.
- Docs: the locked-tools default and the add-on tools rule read as the code
  behaves; README names the current release.

## [2.3.1] — 2026-10-14

### Changed

- **Extension websockets accept a `safe` bot's thread.** The voice add-on can
  now serve a bot that locked devices may also talk to (Atlas, a Wikipedia
  bot). The gate is unchanged where it matters: Origin == Host and a live
  unlocked session — a Safe-Mode device never reaches an extension socket.
  Pairs with DisPatch Voice 1.3.1.

## [2.3.0] — 2026-10-14

DisPatch becomes a platform: a default bot that only needs a key, chats that
explain the app on first run, and a Settings dialog that reads in the order you
use it. Voice stays an add-on; tools can now be add-ons too.

### Added

- **Atlas, the default bot.** A fresh install starts with Atlas, a direct-API
  assistant with lookups (Safe-Mode safe), instead of the two agent-CLI bots.
  On an empty database DisPatch seeds two chats on him: "Meet Atlas" and "How
  DisPatch works".
- **Connect an AI connects a bot.** Settings → AI models writes the provider,
  model and key into the chosen bot's `advisor.providers` (Atlas by default, any
  direct-API bot, or a new one), with a Lookups switch. The previous behaviour
  stays available as the legacy `api:` backend.
- **Bots tab editing.** Rename a bot, change its emoji and model hint, switch
  lookups, add a direct-API or agent bot, remove a bot (its threads are kept).
- **Quick actions** (desktop): ChatForge's twelve canned prompts as chips on an
  empty thread and a composer pill that applies one to your next message, for
  every backend. Override or hide them in `quick-actions.yaml`.
- **Plugins tab** in Settings: installed add-ons with an install-wide on/off
  switch (`extensions.yaml`), apps and tools (the old Tools tab), and a switch
  for the built-in lookups (`livetools: {enabled}` in `tools.yaml`).
- **Add-in Settings tabs.** An extension may create a Settings tab of its own
  with any new pane id.
- **`register_live_tools`** on the extension context: a package adds lookups to
  the live-tools registry, usable by the per-bot `tools` and `tools_locked`
  lists.
- **Web Push notifications** (Settings → Device → Push notifications): a
  lock-screen ping when a bot replies, a picture is ready, or a machine post
  asks for one (`/api/inject` `notify`). Pings carry a name-only title and the
  thread id, never message text. Unlocked, remembered devices only; a device
  that is locked, forgotten or expires loses its subscription. Needs the
  https address. New dependency: `pywebpush`.
- `api:` bots stream for real, retry a 429/502/503/504 once before the first
  token, and accept `extra_body`, `max_tokens` and `read_timeout_s`; new
  `minimax` preset.
- **Advisor live tools** (`advisor.tools`): OpenAI tool calling over the
  eight read-only lookups ported from ChatForge (`app/livetools`): web and
  news search (DuckDuckGo), a page fetch (SSRF-hardened), weather
  (Open-Meteo), Wikipedia, exchange rates (ECB), the time and a calculator.
  Each call that ran shows as a collapsible notice row before the answer.
  `tools_locked` names what a Safe-Mode device may trigger (default: all of
  them). New dependency: `ddgs`.
- **Advisor lookup** (`advisor.lookup`): the turn runs `infoforge search
  --json --text` itself and hands the articles to the model as reference
  notes, so a Wikipedia bot answers in about two seconds with a citation.
- The advisor's date line now carries the local time and zone.

### Changed

- **Settings order** is now Bots, Theme, Device, Voice, Plugins, AI models,
  Reactions, Health, Security.
- The **Tools tab is folded into Plugins**; the Tools popup is unchanged.
- **Default roster:** Atlas replaces the `quick` and `smart` agent bots. Existing
  installs keep their own `config.yaml` roster.
- Safe-Mode lookups default to the whole set (`tools_locked` narrows them).
- The reaction pool's daily add defaults to 2 images per mood (was 10: on a full
  shelf that was ~320 renders a day to replace a handful, each retiring an unused
  image).
- **An advisor may be a Safe-Mode bot.** `safe: true` sticks (it used to be
  forced off silently, including from the Bot Manager). What a locked device
  may make it do is decided per turn: a send, retry or resend over a Safe-Mode
  socket runs the turn locked — no research or handoff marker is acted on, the
  prompt says so, and only `tools_locked` are offered.

### Fixed

- Re-connecting an API bot no longer drops its other fields.
- The instance-lock file handle is closed at shutdown (ResourceWarning).

## [2.2.0] — 2026-10-11

Voice chat leaves DisPatch and becomes an add-on: **DisPatch Voice**, a
separate MIT package (github.com/LaserLloyd/DisPatch_Voice). DisPatch itself
ships no speech code and no speech dependency any more; it gains a small,
stable **extension host** that the add-on (and any later one) plugs into.
Guide: [docs/extensions.md](docs/extensions.md).

### Upgrading an install that used voice

- Install the add-on into DisPatch's environment and fetch its models once —
  the Settings → Voice tab shows the exact commands, and
  [docs/extensions.md](docs/extensions.md#voice) has every install mode. With
  `uv run` as the service's start line: add
  `--with-editable /path/to/a-release-checkout-of-DisPatch_Voice` to it (a `git
  worktree` at the release tag — never the checkout you develop in), plus
  `--offline` (boot must never need the network), `--ws-max-size 262144` (the
  voice socket's frame cap; the main `/ws` never sends more than a
  65,536-character message) and `--ws-ping-timeout 60`; better, start it
  through `scripts/local-chat-launch`. Warm the overlay online once before the
  restart.
- The `voice` dependency group is still declared, **empty**, so a 2.1 unit
  drop-in that runs `uv run --group voice …` keeps booting (without the group
  uv exits 2 and the restart loop takes the app down). It is removed in 2.3.0.
- `DISPATCH_VOICE_PKG=<src dir>` (the 2.1 way to find dispatch-voice) is
  honoured for 2.2.x only, logged as deprecated.
- `<data>/voice.yaml`, `<data>/voices/`, the speech-model directories and each
  bot's `voice` keep their names and paths: the add-on reads them as they are.
- `tools.yaml` rows for the voice tools are kept untouched while the add-on is
  missing (see *dormant rows* below) and work again the moment it is back.

### Added

- **Extension host** (`backend/app/extensions.py`). Installed add-ons are found
  through the `dispatch.extensions` entry point group (or
  `DISPATCH_EXTENSIONS=off | module:attr,…`), version-checked against
  `EXT_API = 1` before any of their code beyond the metadata is imported, and
  set up with an `ExtensionContext`: thread turns (submit, speculate, abort
  with or without keeping the spoken part, retract, replies after, heard
  notes), the frame stream of one thread (bounded, never inside a broadcast,
  with a snapshot of any reply still streaming), a gated thread websocket,
  static assets, tools rows, bot-field validators and lifecycle hooks. A
  failing add-on is a state, never a failed boot: its routes are rolled back.
- **Every gate stays in DisPatch.** `/ws/<name>/{thread}` runs Origin==Host →
  an unlocked session → the thread and a non-Safe-Mode bot → per-session
  slots (the oldest is replaced, 4001) → a server-wide cap (`busy`, 1013), all
  before `accept`; refusals are 1008 pre-accept. A name no extension provides
  runs the same gate and answers 1013 "No extension provides …" instead of a
  bare 1006 a client would retry (so does a handler that never accepts). A
  replaced socket is closed in the background, so a phone reconnecting from a
  half-open link is never held up by its own stale socket. An add-on's HTTP
  routes (API routes only — no mounts) all carry the
  operator dependency, live under `/api/<id>`, join the Safe-Mode block list,
  never join the machine-inbound surface, and may declare body ceilings that
  are enforced before the body is read.
- `GET /api/extensions` (operator, or an authenticated on-box machine — the
  deploy check): names, versions, states (`ok` / `incompatible` / `failed` /
  `disabled`), the assets hash — never a path; `detail` for operators only,
  with the home directory shown as `~`. `/api/auth/status.features.extensions`
  counts the `ok` ones, so a fresh install costs no extra request.
- `/ext/<id>/<path>`: an add-on's frontend files, operator only (403 for a
  locked device), an allowlist of asset types (no HTML, SVG or XML document
  is ever served: 404), `nosniff`, a deny-all CSP, `immutable` caching when
  `?v=` matches the content hash and `no-cache` + ETag otherwise. The service
  worker never caches `/ext/`.
- **Frontend loader and SDK** (`js/extensions.js`, `js/ext-sdk.js`): an
  add-on's strings are merged under the shell's (`i18n.mergeBundle` — it can
  never override a shell key), its CSS loaded, its module imported and
  handshaken, then mounted with a frozen `shell` object: the chat-header
  action slot, a Settings pane, a control on each Bots-tab row, rail tiles and
  a native tool pane, Back-button entries that stack (Settings opened over an
  add-on's overlay takes its own Back, so Back never hangs up a call that
  Settings covered). Everything an add-on registered is torn down when the
  device locks or the add-on fails.
- **Settings → Voice without the add-on** (`js/voice-absent.js`): what voice
  chat is, and the three steps to add it (install the latest 1.x release —
  the add-on's moving `v1` tag — fetch the models, restart and **Check
  again**, which mounts it without a reload), with copyable commands, the
  Docker and `uv run` variants and the optional Kokoro plugin; or, when it is
  installed but cannot run, why (incompatible, failed with the server's
  reason, disabled by `DISPATCH_EXTENSIONS`). Eight languages.
- Bots: `voice_rp` (how role-play text is read aloud) joins `voice` as a bot
  field DisPatch stores and the add-on validates. Without the add-on a save
  may only keep a value or clear it.
- Docker: `--build-arg VOICE=1` (or `VOICE=1` in `.env`) builds the image with
  the add-on.
- `scripts/local-chat-launch`: the recommended start command for a `uv run`
  service. It runs DisPatch with the add-on overlay offline after proving, in a
  throwaway process, that the overlay resolves and its host package and native
  libraries import, and falls back to DisPatch without it when they do not (a
  native crash of that probe included), so a cold cache or a broken wheel costs
  voice chat, not the app.
- **The chat shows what a voice-chat listener did not hear.** When someone talks
  over a spoken reply, the part they never heard is drawn muted behind a mic-off
  mark titled "Not heard in voice chat" (`metadata.voice_heard.upto`, cut at a
  markdown-safe point); a reply stored only up to the cut says "Cut off in voice
  chat" (`js/voice-heard.js`, eight languages).
- `shell.activity()` (extension SDK, additive): an extension whose use makes no
  touches — a voice chat — re-arms the shell's idle auto-lock. Before, DisPatch
  locked itself in the middle of a hands-free conversation.
- `BotView.owner` / `BotView.has_persona` (additive): how an advisor's persona
  names its user, and whether a bot has a persona of its own — names and a flag,
  never prompt text.

### Security and robustness (final review)

- An extension route that can change something (any method but GET, HEAD,
  OPTIONS) refuses a cross-origin browser request: a present `Origin` must equal
  the `Host` and `Sec-Fetch-Site` must not be cross-site or same-site. A
  raw-body or multipart POST is a CORS "simple" request, so another service on
  the same host name could drive one with the unlocked cookie (a cross-origin
  `POST /api/voice/lab/test` loaded the speech models).
- The thread websocket's server-wide cap holds while a reconnect is evicting
  its predecessor (another session's handshake in that second was admitted:
  three open with a cap of two).
- An extension `setup` that has not returned after 60 s is abandoned (failed,
  routes rolled back, context sealed) instead of keeping the port unbound for
  ever; a start-up that never settles is counted in `<data>/ext/.booting`, and
  after two in a row DisPatch starts without extensions (Settings → Voice says
  why) — the next restart once it has run a minute tries them again.
- `/ext/` assets are cached `private, immutable` (operator-only resources).
- `DISPATCH_EXT_BOOT_MARKER=0` turns the crash-loop marker off; the test suite
  does, so a run with the add-on installed never counts "failed starts" in a
  real data directory.

### Changed

- **Instructions for a spoken turn** reach the model in its own slot: the
  system prompt of advisor and API bots, a short note appended at the END of
  what a gateway agent reads — never prepended to the user's words (which is
  what made bots answer "Hands-free mode, got it").
- **Dormant tools rows.** A `tools.yaml` builtin row whose id no installed
  extension provides is kept verbatim through load and save, never listed and
  logged once. Before, one such row invalidated the whole manifest.
- The user-facing name is **Voice chat** (it was Drive mode).
- Keyboard focus on a language/voice `<select>` shows the app's focus ring (the
  accent border alone was fainter than the resting border on the default
  palette).
- A bot whose avatar file is missing (a roster entry without one gets
  `default.png`; an app bot's face that ships nowhere) is drawn with its letter
  instead of requesting a picture that 404s on every render.
- Service worker cache `local-chat-v149`.

### Removed

- The in-tree voice code: `backend/app/voice/`, `drive.js`,
  `voice-worklet.js`, `voice.css`, `js/drive-settings.js`, `js/voices.js`,
  `js/voice-tools.js`, their tests and 306 locale keys per language — all of it
  moved, unchanged, to DisPatch Voice. The voice section of
  `THIRD_PARTY_NOTICES.md` moved with it. `/api/voices` is gone (the add-on
  serves its voice profiles under `/api/voice/profiles`).

### Voice work after 2.1.0, never released in DisPatch

The notes below describe voice changes made after 2.1.0 that were never part
of a DisPatch release. They ship in DisPatch Voice 1.0.0, together with the
code; they are kept here as history.

#### Changed

- **Voice is optional, with an on/off switch.** Settings → Voice opens with
  *Voice (Drive mode)*, a server-wide switch stored as `enabled` in
  `<data>/voice.yaml` and applied live. It is **off on a fresh install**, even
  when every package and model is present: no microphone button, `/ws/voice`
  refuses with 1013 "Voice is turned off in Settings › Voice", and no speech
  model is loaded at start-up. Turning it off ends open drives and releases
  the loaded models; turning it on prewarms them in the background.
  `DISPATCH_VOICE=1` / `0` still force it on / off (the switch is then locked
  or hidden, and the API answers 409). **Upgrading an install that uses voice:
  add `enabled: true` to `<data>/voice.yaml`** (or flip the switch once);
  `DISPATCH_VOICE=auto` no longer means "on whenever the models are present".
  While voice is off the tab shows only the switch and what turning it on
  needs, with the commands.
- **Settings → Voice holds everyday settings only:** Voice (default voice with
  preview; speaking speed only when built-in voices are installed), Listening,
  Replies, This device. The Advanced group and the Voices manager left the
  tab; *Manage voices* appears only while the Voice cloning tool is on.
- **Voice tools.** The advanced pieces are three opt-in builtin tools, off
  unless `tools.yaml` has their row with `enabled: true` (Settings → Tools can
  switch them), unlocked tier only and never in Safe Mode: **Voice cloning**
  (the Voices manager; the `/api/voices` API now answers 404 while it is off),
  **Built-in voices** (status, install steps and voice list of the optional
  GPL-3.0 `dispatch-voice-kokoro` plugin) and **Voice lab** (engine status,
  turn-merge window and hard end of turn, thinking level, cloud/rig engine
  status, *Show timings on the Drive screen*, and a record-3-seconds quick
  test with latency numbers). A tool whose package is missing opens with
  "not installed" and how to install it. New routes `GET /api/voice/lab` and
  `POST /api/voice/lab/test`; new settings keys `enabled`, `merge_window_s`,
  `hard_silence_s` and `reset`.
- **dispatch-voice is published on its own** at
  github.com/LaserLloyd/DisPatch_Voice (MIT). DisPatch finds it when it is
  simply installed (`uv run --group voice --with "dispatch-voice @
  git+https://github.com/LaserLloyd/DisPatch_Voice" …`); `DISPATCH_VOICE_PKG`
  is now only the override for a local checkout and wins over an installed
  copy. Every setup step in the app and the docs uses that command; the Kokoro
  plugin stays a separately distributed optional GPL-3.0 plugin.
- **Review fixes (voice).** Undo is refused once the answer has started
  (it used to delete the question and orphan a fast API/advisor reply), and
  the Drive screen hides it on the first reply audio. A cloned or stock voice
  no longer renders a second set of acknowledgements on every start-up (up to
  ~25 Chatterbox synths). Without a PIN, voice sockets are counted per browser
  (`?d=` device id), so a third family member no longer ends the first drive.
  A hand edit of `enabled` in voice.yaml applies live within 5 s. Switching
  off returns the memory (`malloc_trim`; measured 1.5 GB → 0.16 GB, steady
  across off/on cycles) and calls plugins' release hooks. Previews and the
  lab test wait for idle and run one at a time; a provider's preview failure
  is a JSON 503. Extension API 3: a built-in voices provider describes itself
  (name, licence, steps), so the Built-in voices tool hard-codes no plugin;
  `add_release_hook`. Cache tags are validated and pruned to 8.
- The stock voice shows as "Stock voice" with a **Stock** badge and cannot be
  renamed or deleted in the app; a server with no default voice yet shows
  "Choose a voice" instead of pretending the first one is chosen. The engine
  line tells cloned voices with a built-in fallback, cloned only, and stock
  only apart.
- **Voice licence split.** Drive mode's core install is now entirely
  permissive, so it never changes DisPatch's MIT licence. The Kokoro built-in
  voices moved out into a separately installed, optional plugin,
  **dispatch-voice-kokoro** (GPL-3.0-or-later), because Kokoro's phonemizer
  and espeak-ng are GPL-3.0 and load into the process. `kokoro-onnx` (with
  phonemizer and espeakng-loader) left the `voice` dependency group, and so
  did the unused `soxr`. `python -m app.voice.download` no longer fetches
  Kokoro, so the core download is ~680 MB. The plugin has its own
  `fetch-models [--int8]` and still finds files in
  `$DISPATCH_SPEECH_MODELS/kokoro`. Enable it with
  `DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup`; with it installed
  everything behaves as in 2.1.
- **Default voice without the plugin:** Chatterbox via dispatch-voice speaks
  everything. When no voice is cloned, it uses dispatch-voice's new **stock
  voice** (`stock-host`, built from a bundled clip of synthetic speech, not a
  real person). A `builtin:` voice saved earlier degrades to the default voice
  instead of failing. With no TTS engine at all, `/api/voice/status` says what
  to install and nothing crashes.
- **Engine-neutral extension API (version 2).** `register_engine` takes
  `role="primary"|"fallback"`, `missing=` and `sample_rate=`; new
  `set_builtin_voices_provider(list, preview=)`; engines opt in to the disk
  cache with `cache_tag()` and receive live settings with `apply_settings()`.
  DisPatch no longer names Kokoro anywhere in code.
- **Setting names.** `DISPATCH_VOICE_BUILTIN_VOICE` / `DISPATCH_VOICE_SPEED`
  and `builtin_voice` / `speed` in `voice.yaml` replace the `KOKORO_` names,
  which still work as aliases. The next save rewrites a 2.1 `voice.yaml`.
  `/api/voice/status` drops `kokoro_model`; `builtin_voices` is empty without
  a provider, and `default_voice` can be `""`.

- **Drive mode v2.** The Drive screen follows the active theme and shows the
  bot's avatar inside a state ring, a hint for what a tap does, End drive and
  Pause buttons, live captions with the current sentence highlighted, a
  thinking timer with cancel, and Undo for a misheard sentence. Errors show on
  the Drive screen and failed turns are spoken.
- **Drive settings in the app.** A quick sheet (gear on the Drive screen) and
  a Voice tab in Settings: voice with preview, speaking
  speed, pause before replying, interrupt by talking, reply length, follow-up
  questions, microphone, sounds, screen-on. Each bot can have its own voice
  (Bots tab). Voices are recorded, uploaded, tested and managed inside
  the app (now the Voice cloning tool, above).
- **Protocol.** `start` takes `barge_in`, `reply_style`, `follow_up` and can be
  re-sent mid-drive; new `ping`/`pong`, `ready`, `undo`, `cancel`, `duck`,
  `turn_error` frames; `GET/PATCH /api/voice/settings`, `GET /api/voice/preview`.

#### Fixed

- Drive mode: echo of the reply no longer cuts it off (talking over a reply
  ducks it until the words are confirmed); a dropped phone no longer locks
  Drive mode out (newest socket replaces the oldest, heartbeat); a server
  without speech models says so instead of reconnecting forever; first words
  are no longer lost; speech models load at startup; built-in voice uses the
  fp32 Kokoro model (about 6x faster on CPU than int8; this raised that
  download from the ~810 MB stated for 2.1.0 to ~1 GB, until the licence split
  above moved Kokoro into the plugin).

#### Added

- `THIRD_PARTY_NOTICES.md`: attribution for the Drive-mode models (Parakeet
  CC-BY-4.0 incl. the istupakov ONNX conversion, Silero VAD, Smart Turn,
  Chatterbox) and the runtime libraries of the `voice` group.

## [2.1.0] — 2026-10-09

### Added

- **Advisor bots.** A bot with an `advisor:` block in `config.yaml` answers
  directly from a model API with real token streaming, through an ordered
  provider chain (cloud first, local second; a provider that fails before its
  first word falls through, and a reasoning-only reply does too). Per-provider
  `max_tokens`, `extra_body` (for example to disable thinking) and
  `read_timeout_s`; `<think>` blocks are hidden from the stream.
- **Knowledge corpus.** A directory of Markdown files is the truth; a SQLite
  FTS5 index with optional embeddings from any OpenAI-compatible `/embeddings`
  endpoint gives hybrid ranking. `_profile.md` is always in the prompt, with
  `profile_fallback` files when it is absent.
- **Delegation to agents.** `[[research:<agent>|brief]]` dispatches at once;
  `[[handoff:<agent>|task]]` shows a card with Send and Dismiss and needs a full
  unlocked session, so an agent can never approve its own handoff. Reports are
  saved to `research/` and `actions/` in the corpus and posted into the thread.
  Requests are tracked in `advisor.db` (proposed, running, done, failed,
  dismissed); requests running at a restart are marked failed.
- **Nightly digest.** `POST /api/advisor/digest?day=YYYY-MM-DD[&force=true]`
  writes `conversations/<date>-<bot>.md`; `GET /api/advisor/status` reports
  providers, index and recent requests. Both are machine-inbound. See
  [docs/advisor.md](docs/advisor.md).
- **Drive mode (hands-free voice).** A 🎙 button on a thread opens a voice view:
  speak, and DisPatch detects the end of your turn (Silero VAD + Smart Turn,
  with a turn-merge window for mid-sentence pauses), transcribes it on the CPU
  (Parakeet), sends it through the normal send path tagged `metadata.voice`,
  and speaks the reply sentence by sentence while it streams. Barge-in, echo
  rejection, spoken acknowledgements, Wake Lock and Media Session keys.
  Unlocked tier and non-safe bots only. New endpoints: `/ws/voice/{thread_id}`
  (inline Origin + session gate), `GET /api/voice/status`, and — when the
  optional [`dispatch-voice`](docs/voice-drive-mode.md#cloned-voices-dispatch-voice)
  package is present (`DISPATCH_VOICE_PKG`) — the Voices panel at
  `/api/voices` for cloning a voice from a short clip. Optional `voice`
  dependency group (`uv sync --group voice`, then
  `uv run python -m app.voice.download` for ~810 MB of pinned, sha256-checked
  models, with the int8 Kokoro build this release fetched). Configured by `DISPATCH_VOICE` (`auto` by default: on only when the
  deps and models are present) and the `DISPATCH_VOICE_*` variables. Licence
  note: the Kokoro fallback voice pulls in a **GPL-3.0** phonemizer chain
  (phonemizer + espeak-ng) at runtime through the `voice` group; nothing GPL is
  vendored in this repository. A bot's `voice:` picks its profile. See
  [docs/voice-drive-mode.md](docs/voice-drive-mode.md).

### Changed

- **Default prompt follows the tier.** A direct-API ("Connect an AI") bot with
  no `system_prompt` of its own is no longer told it is in a family chat
  unless it is a Safe-Mode (`safe: true`) bot; unlocked-only bots get a neutral
  default.
- `llm_api` gained `resolve_api`, resolving a provider entry without a bot.
- `config.yaml` bots gain an optional `advisor:` block, omitted when unset;
  admin output redacts its provider and embeddings keys. They also gain an
  optional `voice:` (Drive-mode profile id), shown in admin output only.
- Drive mode is optional at import time: if `app/voice` cannot be imported the
  app starts without it (logged) and `/ws/voice` answers 1013.

## [2.0.0] — 2026-10-09

The first tagged release. It rolls up everything since the 1.0.0 source tree
of August 2026: a socket-first, drop-proof reply path with live streaming, a
conversation toolkit, tools and app packages, ten shared themes, and the
licence change to MIT. Breaking for operators: the coding-terminal pane and its
`DISPATCH_TERMINAL*` switches are gone, and the light/dark switch is replaced
by palettes (a stored `data-theme` choice is not carried over).

### Added

- **Tools and apps.** A Tools group in the rail (one Tools button with a popup
  menu on desktop, a tile grid and bottom sheet on the phone), full-page tool
  panes and **Settings → Tools**. Tools are manifest-driven (`tools.yaml`): a
  static folder, a URL, or a builtin, each switchable, and opening framed or in
  a new window (`open: frame|window`) for pages that refuse framing. **Trusted
  app packages** (`apps/<id>/`) add their own backend routes, static UI, bot
  and thread hook through `AppContext` and the app SDK; the **Job Board** is
  the first one. See [docs/apps.md](docs/apps.md).
- **Conversation toolkit.** Regenerate keeps the alternates, a message can be
  edited and re-run or dropped from the context, quote/reply, thumbs feedback,
  and a per-thread **model and thinking-level override** with a context meter
  — all on one shared turn seam (`TurnOptions`).
- **Drafts, a persisted outbox and offline reading.** An unsent draft is kept
  per thread, a message sent while offline waits in an outbox that survives a
  reload, and threads already opened stay readable offline.
- **Calmer message rows.** Copy and a ⋯ menu, nothing else on screen; machine
  events carry `metadata.notice` and collapse to one muted line.
- **Today / Older section headers** in the thread list, on desktop and phone.
- **Mood-driven header face and a scene backdrop** for bots that have them.
- **Custom link buttons** on the rail, and thin-stroke SVG rail icons.
- **DeepSeek Harness live sessions** — launch, watch and stop a `dsh` run from
  the harness pane (operator-only).
- **StudioForge control panel** as a builtin tool when the LLM rig is present.
- **Inline `[[pic:prompt|caption]]` marker** — a bot's reply can fire an image
  job that lands in place of a placeholder.
- **Clients site links** (practice integration): editable live/repo/notes links
  per client, stored in the data directory.
- **Live reply streaming.** A reply paints as the gateway produces it: the
  OpenClaw `chat` delta events (per-session `sessions.messages.subscribe`)
  drive `stream_start` / `stream_chunk` / `stream_done` frames, coalesced to
  ~100 ms, and the streaming row swaps to the persisted message on
  `stream_done`. Text appears as soon as the model emits it instead of after
  the run ends. Providers that hand OpenClaw a single block (DeepSeek's
  reasoning models do) still land in one paint — the gateway, not DisPatch,
  decides the granularity.
- **Turn phases.** A status line under the composer names what the run is
  doing (preparing context, loading model, writing, using *tool*, finishing)
  from `turn_status` frames (`turn.phase_*` locale keys, all eight languages).
- **Stop.** A ⏹ button beside Send aborts the in-flight run
  (`{type:"abort"}` over the socket, `POST /api/threads/{id}/abort`);
  unlocked sessions only — Safe Mode gets 403.
- **Optimistic send.** The sent bubble paints immediately and is reconciled
  with the server row when it lands (`provisional_id`).

- **Health → "Live transport" card.** The host dashboard shows the gateway
  socket state, replies delivered live vs backfilled, unrepaired truncation,
  `messageSeq` gaps, turns sent over the socket vs the CLI, image-rig
  reachability and 24 h image-job failures — the `/api/health` counters the
  socket turn transport exposes, so "which path did that reply take" is
  answerable from the app.
- **Command palette reaches every rail surface.** Ctrl/⌘-K offers Settings,
  the Health dashboard and the File Server when unlocked, and "Send a file"
  in Safe Mode.

- **Interactive checklist tables**: a ```` ```checklist ```` fenced block posts a
  markdown table that renders as a checkable widget — sortable columns, a
  checkbox per row (completed rows group at the bottom in check order), and
  state persisted on the message so it survives a reload and syncs across
  devices.
- **Image jobs: the placeholder says when the image server is unreachable.**
  A job whose rig cannot be reached shows "🖼️ Waiting for the image rig
  (ConnectError)…" and reverts to the normal placeholder when the rig answers
  again; `/api/health` gains `image_rig` (`reachable`, `unreachable_for_s`,
  `last_error`).

- **Local viewer.** An unlocked operator taps a path in a message and the
  thing opens inside the app, full-screen: an HTML page or a whole static site
  folder, a PDF, markdown, a log, an image, a video, a directory listing. The
  server reads the bytes, so it works from any device that can reach DisPatch
  — a phone on the sofa opens a file on the host with no filesystem access of
  its own. Three ways in: a bare path in a message (`/var/log/x.log`,
  `~/reports/`), the `[[view:/abs/path|label]]` directive an agent can write,
  and a markdown link whose href is a local path. A gear-rail link button can
  point at a local path (or open an http(s) URL in the viewer instead of a
  tab), and the command palette gains **"Open a local file…"** — type a path,
  and the last eight you opened are offered as rows of their own
  (`dispatch-viewer-recent`, per device and on the privacy wipe list).
  Settings → Device gains a **Local viewer** section that edits the served
  folders: add or remove a root, toggle "show hidden files", each change saved
  straight to the server (`PUT /api/local/config`) like every other preference
  in that pane. Unlocked sessions only — the section is not built in Safe
  Mode.

  It serves **nothing** until you name a folder in Settings → Device, and then
  only what resolves inside it. `~/.ssh`, `~/.config/secrets`, `/etc`, `/proc`,
  the database, the security files, and anything matching `*.env *.pem *.key
  id_* known_hosts …` are refused whatever the roots say; dotfiles below a root
  are refused unless you turn them on. Safe Mode never sees any of it — the
  affordance is not rendered and every route answers 403 — and `/api/health`
  counts refusals as `viewer_denied_24h`. Framed pages run under a sandbox
  with an opaque origin: their own scripts and relative assets work, `fetch()`
  from inside them does not.

### Changed

- **No dropped replies.** Every run the gateway accepts is registered in an
  in-flight table and settled with `agent.wait`, so a reply whose live delta
  stream was cut (socket blip, restart mid-run) still lands as a message —
  recovered on startup for runs that were in flight when the server went
  down. `/api/health` carries `inflight_runs`, `dropped_local`, `tick_closes`
  and `transcript_backstop`.

- **Turn dispatch over the already-open gateway socket.** When the native
  gateway transport (`DISPATCH_GATEWAY_WS=1`) is connected, a user turn is
  sent as a gateway `agent` request on that socket instead of spawning
  `openclaw agent` per message — about 1.1 s of fixed pre-model overhead
  gone from every reply (measured 2259 → 1115 ms). `DISPATCH_TURN_TRANSPORT`
  = `auto` (default: socket when connected, CLI otherwise) / `1` (socket only,
  a turn with no socket fails instead of silently paying the CLI cost) / `0`
  (the old per-turn CLI spawn). Only a run the gateway never accepted is
  retried; an accepted run is never re-sent.

- **Image jobs: faster and cheaper on the wire.** One MCP session is reused
  across calls (a forgotten session is rebuilt once and the call repeated); a
  submit or a rig callback wakes the worker instead of waiting for the next
  5-second tick; the sweep advances up to three jobs at once; an unreachable
  rig trips a 15-second breaker so queued jobs fail fast rather than each
  waiting out a connect timeout in series.

- **Appearance is a palette, not a light/dark pair.** `theme.css` first shipped six
  complete skins selected by `data-palette` on `<html>`: **Glacier** (the
  default), Midnight Gold, Forest, Paper, Daylight and Classic Purple (today's
  purple, frozen to the dark values it shipped). Each block redefines the full
  thematic token set with flat values, so a palette can no longer inherit half
  its colours from the other skin, and each declares the `color-scheme` its
  surfaces need. The rail button and the Cmd/Ctrl-K "Change theme" action open
  **Settings → Theme**: a grid of live miniature previews (the card carries its
  own `data-palette`, so a preview is painted by the real token block, never by
  a copy of the values) with radio-group arrow-key handling. The pick is per
  device, applied by the no-FOUC `<head>` script before first paint, and stored
  as `dispatch-palette` — on privacy mode's wipe list.
  `frontend/tests/theme-palettes.test.js` pins the palette ids across
  `theme.css`, `js/theme.js` and `index.html`, and fails if any palette omits a
  token or redefines a structural one.
  Since 2026-09-22 the palettes come from a shared theme package vendored as
  the drop-in folder `static/ui-theme/` — **ten themes** (adding Electric
  Yellow, Night Red and two LaserLloyd skins), with a "More themes" row.
- **Licence: MIT** (relicensed from AGPL-3.0 on 2026-08-30).
- **Text responses are gzipped** and the thread-list preview is bounded.
- **A failed startup exits** instead of lingering as a zombie process.

### Removed

- **The light/dark theme switch.** `data-theme`, the `light-dark()` pairs in
  `theme.css`, `js/theme.js`'s toggle and the `theme.dark` / `theme.light`
  locale keys are gone — Light and Dark as *choices* are replaced by the Paper
  and Daylight palettes above. `light-dark()` still appears in `app.css` and
  `dashboard.css` for two narrow cases (minimal-avatar name tints, host
  dashboard severities); those resolve per palette through each block's
  `color-scheme`. The lock face carries `data-palette="purple"` so it stays
  dark on a light palette, and the rail's theme shortcut is hidden in Safe Mode
  with the rest of the admin surface (Settings is where a palette is chosen
  now, and Settings is full-session only — the language picker was already
  behind the same door).

- **The coding-terminal pane.** The server-side PTY (`backend/app/terminal.py`,
  `/api/terminal/*`, `WS /ws/terminal`, the `terminal_state` frame), its
  sidebar row, action bar, find box and model picker, the `terminal.*` locale
  namespace in all eight languages, the `DISPATCH_TERMINAL*` /
  `LOCAL_CHAT_TERMINAL` switches and the vendored terminal-emulator bundle
  (six files, 317KB) are gone. The pane existed to host one operator-side
  CLI that is no longer part of the stack; the DeepSeek Harness pane is the
  coding-agent surface. Nothing loosens: the harness and StudioForge routes
  keep their own full-session gates, and the `/api/terminal` prefix is now
  simply unrouted (404) rather than decoy-blocked (403). `features.terminal`
  no longer appears in `/api/auth/status`.

### Fixed

- **First-run "Connect an AI" hero on a socket-only host.** `features.agent`
  keyed off the `openclaw` CLI alone, so a host whose turns run over the
  gateway socket greeted an unlocked admin with the onboarding banner. It now
  reports true when either the CLI or a connected gateway socket can answer a
  turn (`test_agent_operability.py`).
- **Thread `⋯` menu glyphs.** Pin/Rename/Archive/Delete carry an icon like
  Sync and Transcript already did; the Bots pane's "no prompt bank — refills
  generate nothing" now reads "no prompt bank yet — add prompts to enable
  refills".

- **Phantom `messageSeq` gaps no longer trigger a backfill per reply.** The
  gateway folds tool-result rows into the assistant message, so every
  "gap of 2" the router repaired was phantom (32 of 32 in the logs). Gap
  repair is now deferred and coalesced (`GAP_BACKFILL_DELAY_S`), the range
  excludes the just-delivered message, and the health counters count
  deliveries that actually landed rather than offers. Side effect fixed with
  it: the backfill used to deliver the revealing reply first as a `followup`,
  which is what was suppressing one bot's reaction markers.
- **`backup_age_s` in `/api/health` could go negative** — the backup stamp was
  naive local time compared against UTC; it is now `datetime.now(UTC)`.
- **Thread list: a picture-only newest message previews as its caption** (or
  a 🖼️ glyph) instead of "No messages yet", and the row repaints when an
  image job's placeholder is rewritten — it used to sit on "Generating an
  image…" after the picture had landed.
- **Image jobs: a callback and the sweep can no longer advance one job
  twice** (a duplicate enqueue was possible when both arrived together).
- **Image jobs: a file name returned by the image server is checked** for
  `..`, empty segments and embedded schemes before it is fetched.
- **Reaction and avatar ids are anchored with `\A…\Z`**: a trailing newline
  in a `bot_id` or mood name passed `^…$` and became a path component.
- The `image_jobs` loop reported a 20-second period to `/api/health`'s
  loop beats while ticking every 5.
- **The app's own pages now send `Content-Security-Policy: frame-ancestors
  'self'` as a header.** `index.html` said that was the server's job and the
  server never did it, so nothing stopped another site from framing DisPatch.

## [1.0.0] — 2026-08-26

The source tree distributed as a zip in August 2026 (never tagged). The history below is the work that produced it, grouped by
theme rather than by commit.

### Added

- **Chat**: threads with pinning, archiving, rename and full-text search;
  markdown with syntax-highlighted code blocks, copy buttons and collapsible
  JSON; image, video and file attachments with inline previews; streaming
  replies with a collapsible "what the agent is doing" panel; an installable,
  offline-capable PWA.
- **Two access tiers**: a PIN unlocks everything, while a limited no-PIN tier
  can read and send to the bots you mark safe, with media stripped server-side,
  per-device daily budgets and a one-way file drop. The split is enforced on the
  server, never in the interface.
- **Remembered devices**: opt-in, sliding-window trust for a device, stored as
  SHA-256 token hashes only.
- **No-Image Mode**: a device setting that omits every picture — chat media,
  avatars, reactions — and, more importantly, downloads none of them. Available
  on the lock screen, so it works before unlocking.
- **Privacy mode**: a device that holds nothing locally — no offline cache, no
  saved settings, no persistent session.
- **Direct LLM providers**: point a bot at OpenAI, Anthropic, DeepSeek, Groq,
  OpenRouter, Mistral, xAI, Together, LM Studio, Ollama or anything
  OpenAI-compatible, from a panel in the app. No agent runtime, no install.
- **Agent backend**: an adapter that spawns an agent CLI and tails its session
  transcripts, plus a native gateway WebSocket transport with session-scoped
  deduplication, truncation repair and gap backfill. Agents reply into threads
  like any other participant and can push proactive messages.
- **Gateway chat mirror**: an agent runtime's own conversations are tailed into
  DisPatch threads, both directions, with gap-filling for replies that arrive
  after the follower has stopped watching.
- **Coding-agent panes**: an admin-only server-side terminal, and a second
  engine next to it for the DeepSeek Harness (`dsh`) with service control, a
  model switch and one-at-a-time headless jobs.
- **Reaction images**: a named reusable pack plus per-bot one-shot mood pools
  where the filesystem is the manifest; agent-only firing, a visible refusal row
  when a fire is rejected, registry↔disk healing, and a server-side autopilot
  that fires a mood when a bot's reply carries no marker.
- **Avatar pools**: burn-on-use face/full pairs, so a new thread draws a face of
  its own and keeps wearing it; a thread's own snapshot is shown in its header
  and on every message inside it.
- **Host dashboard**: health, storage, database integrity, backup status and a
  findings checklist that says what is misconfigured and how to fix it.
- **Backups**: rotating online snapshots taken with `VACUUM INTO` on a private
  connection, integrity-verified, with crash recovery that reconciles
  interrupted conversations rather than losing them.
- **Internationalisation**: eight languages including full right-to-left layout,
  a locale validator that doubles as an XSS boundary, and no build step.
- **Deployment**: multi-arch container images, a compose stack with an optional
  Caddy profile, system and user systemd units, and guides for Docker, bare
  metal, Raspberry Pi and Windows.
- **Repository tooling**: a scrubber that fails the build on private data (as a
  pre-commit, commit-message and pre-push hook, and in CI), a locale checker, and
  a CI matrix covering both supported Python versions.

### Changed

- Renamed the product to DisPatch Chat. Every setting now answers to both the
  `DISPATCH_` and the legacy `LOCAL_CHAT_` prefix, so existing service units
  keep working; only the `DISPATCH_` names are documented.
- The agent-facing API accepts a case-insensitive `bot_id`, takes `text` as an
  alias for `content`, and exposes thread management to machine callers without
  ever taking the machine branch for a browser-shaped request.
- Markdown rendering and text sanitisation were brought to parity with the
  agent runtime's own renderer; markdown tables now scroll sideways instead of
  collapsing their columns.
- The test suite was restructured to be strictly hermetic — every test gets a
  throwaway database and monkeypatched data directories — and runs several times
  faster as a result.
- Ruff configuration that describes the codebase, with a clean run.

### Fixed

- Message identity is scoped by session: an 8-hex-character upstream id collides
  often enough at household volume to silently drop a reply.
- A reply cut short in transit is repaired rather than delivered truncated, and
  an unrepaired truncation is surfaced as a stop-the-line signal.
- A delegated answer can no longer be lost to a missed follow-up window; a turn
  that errors leaves a visible placeholder instead of silence.
- Reaction markers inside code blocks are quoted, not fired; quoted runtime
  delimiters no longer eat the message.
- Two hazard handlers that were dead on arrival — a deadlock, and a field read
  that always returned nothing — were fixed, along with a live render crash and
  a batch of seam bugs found in review.
- The database backup no longer loses a race with the app's own reads.
- The left rail opens the chat rather than the lightbox; every thumbnail opens
  its own full-resolution original, including images an agent sent.
- Tests no longer write into a live install.

### Security

- Fail-closed authentication throughout: WebSocket routes carry inline auth and
  an `Origin`/`Host` check, mutating endpoints answer 403 to a limited-tier
  session, and the file server is a one-way drop with no download path.
- The scrubber gained more pattern classes and now scans commit messages and
  pushes, not just the working tree.

[Unreleased]: https://github.com/LaserLloyd/DisPatch_Chat/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LaserLloyd/DisPatch_Chat/releases/tag/v1.0.0
