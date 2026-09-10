# Changelog

All notable changes to DisPatch Chat are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Removed

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
  which is what was suppressing Bits' reaction markers.
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

## [1.0.0] — unreleased

First public release. The history below is the work that produced it, grouped by
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

[Unreleased]: https://github.com/LaserLloyd/dispatch-chat/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LaserLloyd/dispatch-chat/releases/tag/v1.0.0
