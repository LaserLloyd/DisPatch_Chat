# Changelog

All notable changes to DisPatch Chat are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Interactive checklist tables**: a ```` ```checklist ```` fenced block posts a
  markdown table that renders as a checkable widget — sortable columns, a
  checkbox per row (completed rows group at the bottom in check order), and
  state persisted on the message so it survives a reload and syncs across
  devices.

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
