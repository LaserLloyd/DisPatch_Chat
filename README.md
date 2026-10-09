<div align="center">

<img src="docs/assets/hero.svg" alt="DisPatch Chat — a self-hosted chat app for your household and your AI agents. MIT-licensed, one process, one SQLite file, no cloud." width="100%" />

**A self-hosted chat app for you, your household, and your AI agents.**

Runs on your hardware. Your conversations stay in a SQLite file you can read,
back up, and delete. No account, no cloud, no telemetry.

**Current release: 2.1.0** (October 2026) · [what changed](#whats-new-in-21)

[Quick start](#quick-start) · [Bring your own AI](#bring-your-own-ai) ·
[Deployment](#deployment) · [Security](#security) ·
[Documentation](docs/) · [Changelog](CHANGELOG.md) ·
[Contributing](CONTRIBUTING.md)

</div>

---

## What it is

DisPatch Chat is a small, fast application you host yourself. It looks and feels
like a normal messaging app — threads, avatars, markdown, file sharing, search,
a phone-installable PWA — and it can talk to AI agents running on your own
machine as well as to the other people in your household.

It is deliberately **one process and one SQLite file**. No Redis, no Postgres,
no message broker, no build step. It runs comfortably on a Raspberry Pi.

**Not** a Slack replacement. **Not** end-to-end encrypted (see
[Security](#security) for exactly what that means). It is a private chat you own.

### The shape of it, honestly

A whole household can use one install, and everyone who unlocks it sees the same
threads, so people really can talk to each other in here. That works, and some
families use it that way.

But it was built first as a **front door to an AI backend** — originally an
agent runtime like [OpenClaw](docs/agents.md) running on your own machine — that
happens to look and feel like the messaging app everyone already knows. The
bots are participants in the threads, not a feature bolted onto a chat app.
If what you want is a private person-to-person messenger, there are better
tools; if what you want is for your family to be able to talk to the thing
running on the box in the cupboard, this is the one.

You no longer need an agent stack for that: **Connect an AI** (below) points a
bot at a model provider's API directly. The agent path is still what you want
if you need a thing that can *use tools and act on the machine*, and it is
still the one that has to run on the same host.

It is also **single-account today**. One PIN, one shared view of everything.
Per-person accounts with separate identities, roles and per-conversation access
are designed in detail and not built — the design is in
[docs/design/multi-user.md](docs/design/multi-user.md), including the migration
that keeps an existing install's history.

## Screenshots

A demo install with three fictional assistants — the same app on a desktop and
on a phone, in a few of the ten themes.

<table>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/desktop-glacier.png"
           alt="DisPatch Chat on a desktop browser in the default Glacier theme: a rail of three letter-block bot avatars on the left, the planner bot's thread list, and an open thread where it answers with a day-trip plan as a bulleted list and a sortable comparison table." />
    </td>
    <td width="50%">
      <img src="docs/screenshots/desktop-midnight-gold.png"
           alt="The same app in the Midnight Gold theme, showing a reply with a console code block that has a language label, a wrap toggle and a copy button." />
    </td>
  </tr>
  <tr>
    <td align="center"><em>Glacier (default) — markdown and sortable tables</em></td>
    <td align="center"><em>Midnight Gold — code blocks</em></td>
  </tr>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/desktop-checklist.png"
           alt="A reply in the Forest theme containing an interactive checklist table: four household chores with a checkbox per row and sortable Task, Who and When columns." />
    </td>
    <td width="50%">
      <img src="docs/screenshots/settings-theme.png"
           alt="Settings on the Theme tab: a grid of live miniature previews for the six core themes, a More themes row with four more, and a check mark on the active one." />
    </td>
  </tr>
  <tr>
    <td align="center"><em>Checklist tables — ticks sync to every device</em></td>
    <td align="center"><em>Settings → Theme — ten skins, live previews</em></td>
  </tr>
</table>

<table>
  <tr>
    <td width="33%">
      <img src="docs/screenshots/mobile-chats.png"
           alt="DisPatch Chat at phone width showing one bot's thread list under a Today heading, with previews and relative timestamps, and a bottom tab bar for Bots, Chats and Messages." />
    </td>
    <td width="33%">
      <img src="docs/screenshots/mobile-thread.png"
           alt="DisPatch Chat at phone width showing an open conversation: a numbered list and a shopping list rendered as markdown, a Copy button and a menu under each message, and the composer pinned above the tab bar." />
    </td>
    <td width="33%">
      <img src="docs/screenshots/bot-manager.png"
           alt="The Settings dialog on the Bots tab, listing four bots with drag handles, Safe, React and Pool toggles, a Change photo button and a per-bot visibility switch." />
    </td>
  </tr>
  <tr>
    <td align="center"><em>Phone — thread list</em></td>
    <td align="center"><em>Phone — conversation</em></td>
    <td align="center"><em>Settings — the bot roster</em></td>
  </tr>
</table>

## Features

**Chat**
- Threads per conversation, with pinning, archiving, rename and full-text search
- Markdown with syntax-highlighted code blocks, copy buttons, and collapsible JSON
- Image, video and arbitrary file attachments with inline previews
- Live streaming replies, a status line naming what the run is doing, a
  collapsible "what the agent is doing" panel, and a Stop button
- Regenerate (with the alternates kept), edit-and-rerun, quote/reply, thumbs
  feedback, and dropping a message from the context
- Per-thread model and thinking-level override, with a context meter
- Interactive checklist tables whose ticks persist and sync across devices
- Drafts per thread, a persisted outbox and offline reading — installs to a
  phone home screen as a PWA

**Images & media delivery**
- Agent- and script-posted pictures arrive as an instant placeholder in the
  thread that resolves into the image when the render lands — failures turn
  the placeholder into a visible warning, never silence
- Per-bot avatar pools and one-shot reaction overlays, topped up automatically
- Every thumbnail opens its full-resolution original in the lightbox

**Tools & apps (plugins)**
- Embed your own projects as switchable modules: a static directory, a URL, or
  a trusted app package with its own backend routes, UI and bot
  (`apps/<id>/`, [docs/apps.md](docs/apps.md))
- Tools open full-page inside the app — or in a new browser window for pages
  that refuse framing — from one Tools button in the rail

**Access control**
- A PIN unlocks full access; sessions idle-expire, and a device can be trusted
  for a sliding window if you choose
- A **limited tier** that can read and send without the PIN — for a family
  tablet on the kitchen counter — restricted to the conversations you allow,
  with all media stripped server-side and daily per-device budgets
- **One-way file drop**: a locked device can send you a file without being able
  to browse or retrieve anything

**Operations**
- Built-in **host dashboard**: health, storage, database integrity, backup
  status, and a checklist that tells you what is misconfigured and how to fix it
- Automatic rotating database backups with integrity verification
- Crash recovery that reconciles interrupted conversations rather than losing them
- **Local viewer**: tap a path in a message and the file opens inside the app —
  an HTML page or a whole static site, a PDF, markdown, a log, an image, a
  video, a folder listing. The server reads the bytes, so it works from a phone
  as well as the desktop. It serves nothing until you name a folder, and never
  serves secrets, dotfiles or system paths; unlocked sessions only

**Interface**
- Eight languages — English, العربية, Deutsch, Español, Français, 日本語,
  Português, 中文 — including full right-to-left layout
- Ten built-in themes — Glacier (default), Midnight Gold, Forest, Paper,
  Daylight, Classic Purple, Electric Yellow, Night Red and two LaserLloyd
  skins — picked per device from Settings → Theme, each
  with a live preview. A theme is one fixed skin; there is no separate
  light/dark switch
- Keyboard-navigable, screen-reader labelled, WCAG AA contrast in every theme

**AI (optional)**
- **Bring your own model provider**: point a bot at OpenAI, Anthropic, LM
  Studio, Ollama or anything OpenAI-compatible, from a panel in the app. No
  agent runtime, no install — see below
- Or use a full **agent backend**: ships with an adapter for
  [OpenClaw](docs/agents.md). Turns go over the gateway's WebSocket when it is
  connected (falling back to spawning its CLI), replies stream in live, and an
  in-flight table plus a transcript sweep make sure an accepted run's reply
  lands even after a dropped socket or a restart. The CLI and the transcripts
  are host-filesystem operations, so *that* backend has to live on the same
  machine — the seam that would make an agent
  runtime on another host pluggable is designed and not built
  ([docs/design/agent-backend.md](docs/design/agent-backend.md))
- Agents reply into threads like any other participant, and can push proactive
  messages (a morning briefing, a finished job, an alert)
- **Entirely optional** — DisPatch is a perfectly good human-to-human chat with
  neither configured

## What's new in 2.1

- **Advisor bots.** A bot with an `advisor:` block answers straight from a model
  API with real token streaming and a cloud-then-local fallback chain, knows you
  from a folder of Markdown notes (keyword plus optional embedding search), and
  hands work to your agents: research runs automatically, actions wait for a
  Send tap. Reports are saved back into the notes and a nightly digest keeps
  its memory current. See [docs/advisor.md](docs/advisor.md).
- **Drive mode.** Talk to a thread hands-free: DisPatch hears when you have
  finished, transcribes on the CPU, sends it like a typed message, and reads
  the reply back as it streams. Barge-in, echo rejection and a patience
  setting for drivers who pause mid-thought. Optional: install the `voice`
  dependency group and download the models; cloned voices come from the
  separate `dispatch-voice` package. The Kokoro fallback voice uses a GPL-3.0
  phonemizer at runtime (not vendored here). Unlocked tier only. See
  [docs/voice-drive-mode.md](docs/voice-drive-mode.md).

## What's new in 2.0

2.0.0 is the first tagged release and rolls up everything since the August
1.0.0 tree. The headlines (the full list is in the [changelog](CHANGELOG.md)):

- **Replies arrive live and are never dropped.** Turns go over the gateway's
  open socket (about a second faster per reply), text streams as the model
  writes it, a Stop button aborts a run, and every accepted run is settled even
  across a restart.
- **A real conversation toolkit.** Regenerate with alternates, edit-and-rerun,
  quote/reply, thumbs, per-thread model and thinking overrides, a context meter,
  drafts and an offline outbox.
- **Tools and apps.** Embed your own projects as switchable panels — a static
  folder, a URL, or a trusted app package with its own backend, UI and bot —
  from one Tools button. The Job Board ships as the first app.
- **Ten themes** from a shared theme package, replacing the light/dark switch.
- **Local viewer, image jobs, checklist tables, calmer message rows**, and
  machine notices that collapse to one muted line.
- **Removed:** the coding-terminal pane. The
  licence is **MIT** (it was AGPL-3.0 before 2026-08-30).

## Why there is a PIN

An agent backend is not a chatbot. It runs real commands on the machine it lives
on, with your credentials, because that is the entire point of having one.

Which means a child with full access could ask it, perfectly politely, to
replace your entire website with pictures of ducks. It would. It would do a
thorough job, and it would tell you cheerfully when it was finished. Nothing in
that request looks different to an agent from "summarise this email", and by the
time you read the message the ducks are live.

So the two tiers are not about keeping the family out of the chat. They are
about which half of the app is behind a door:

<div align="center">
  <img src="docs/assets/tiers.svg" width="100%"
       alt="Both tiers funnel through one server-side gate. A kitchen tablet with no PIN reaches Safe Mode: read and send to safe characters only, media stripped server-side, a one-way file drop, daily per-device budgets, and 403 on every mutating endpoint. A phone with the PIN entered reaches everything: full-power agents, files, settings, deletion, the host dashboard, the tools and the coding pane." />
</div>

**Safe Mode — no PIN.** What a kitchen tablet or a kid's phone gets. Read and
send, but only to the characters you marked safe, with media stripped
server-side, per-device daily budgets, and a one-way file drop (they can send
you a file; they cannot browse or retrieve anything). Every mutating endpoint
answers 403. The app does not even hint that it unlocks.

**Unlocked — with the PIN.** Everything: the full-power agents, files, settings,
deletion, the host dashboard, and the DeepSeek Harness pane (when `dsh` is
installed) if you turned it on.

The split is enforced on the server, not in the interface — a locked device that
crafts the request by hand gets the same 403 as one that presses the button.
That is the invariant to protect if you contribute: new endpoints inherit the
gate, and a permission problem is never fixed by loosening it.

## Quick start

```bash
git clone https://github.com/LaserLloyd/DisPatch_Chat.git
cd DisPatch_Chat
cp .env.example .env          # optional: every setting in it has a working default
docker compose up -d
```

If you forked, or are running your own copy, one line repoints every reference
in the tree at your own fork — image labels, the in-app source link, issue links:

```bash
grep -rlE 'LaserLloyd/DisPatch_Chat|ghcr.io/laserlloyd/' --exclude-dir=.git . \
  | xargs sed -i -e 's|LaserLloyd/DisPatch_Chat|myuser/DisPatch_Chat|g' -e 's|ghcr.io/laserlloyd/|ghcr.io/myuser/|g'
```

There is no secret to generate and no required setting. An empty `.env` starts a
usable server; it builds the image from the checkout — no registry account
needed — and the file is where you put a data path, a port, or an agent backend
when you want one.

Open <http://localhost:8765>, then set a PIN under **Settings → Security**.
Do it now: **the compose file publishes the port on `0.0.0.0`**, so until you
set a PIN, everyone on your network has full access. Set `BIND_ADDR=127.0.0.1`
in `.env` if you would rather it were this machine only while you look around.
(A bare `uvicorn` process with no container is the other way round — it binds
loopback unless you tell it not to.)

Not using Docker? See [docs/deploy-bare-metal.md](docs/deploy-bare-metal.md).

## Bring your own AI

DisPatch will not answer you out of the box, because it ships with no model and
talks to no cloud until you tell it to. Making it answer takes about a minute
and needs no agent stack:

1. Open **⚙ Settings → 🔌 AI models** (a fresh install also offers it as a
   card in the empty chat area).
2. Pick a provider, paste a key if it needs one, press **Test connection**, pick
   a model from the list it comes back with, press **Save & chat**.

That is it. The provider becomes a bot in your sidebar with its own threads and
history, and it works like every other conversation in the app.

Supported out of the box: **LM Studio** and **Ollama** (on your own machine, no
key, nothing leaves the box), **OpenAI**, **Anthropic**, **DeepSeek**, **Groq**,
**OpenRouter**, **Mistral**, **xAI** and **Together** — plus **Custom**, which
is anything speaking OpenAI's `/chat/completions`, so llama.cpp, vLLM, LocalAI
and most corporate gateways work too.

Two honest caveats:

- **Your key is stored in `config.yaml` in the data directory.** Every writer
  chmods that file to `0600`, so other users on the box cannot read it — but it
  is not encrypted, and anyone with your account, root, or a copy of your
  backups has it. If you would rather it were not on disk at all, name an
  environment variable instead and DisPatch reads the key from there; the
  environment always wins over the file.
- **This is a conversational assistant, not an agent.** No tools, no file
  access, nothing that can act on your machine. For that you want an
  [agent backend](docs/agents.md), which is a bigger commitment and has to run
  on the same host.

Full setup notes, per provider: [docs/llm-providers.md](docs/llm-providers.md).

## Deployment

| Platform | Guide | Notes |
|---|---|---|
| Docker / Compose | [docs/deploy-docker.md](docs/deploy-docker.md) | Recommended. amd64 + arm64 |
| Raspberry Pi | [docs/deploy-raspberry-pi.md](docs/deploy-raspberry-pi.md) | Pi 4/5. Read the SD-card section |
| Windows | [docs/deploy-windows.md](docs/deploy-windows.md) | Docker Desktop or WSL2 |
| Bare Linux | [docs/deploy-bare-metal.md](docs/deploy-bare-metal.md) | systemd unit, system or user |

### Reaching it from outside your house

DisPatch does not depend on any commercial mesh VPN. The supported ladder, in
order of what most people should pick first, is in
[docs/remote-access.md](docs/remote-access.md):

1. **Reverse proxy with a real certificate** (Caddy + Let's Encrypt DNS-01).
   TLS terminates on *your* hardware.
2. **Behind CGNAT?** A cheap VPS forwarding raw TCP to a WireGuard tunnel. Your
   traffic stays encrypted end-to-end past the VPS — it only sees ciphertext.
3. **LAN only.** Perfectly valid. The docs cover getting real certificates for a
   private network so browsers stop complaining.

The public hostname is the same in all three, which matters: passkeys are bound
to it permanently, so you can change how you reach the box without invalidating
every device.

## Security

Read [SECURITY.md](SECURITY.md) for the reporting process and
[docs/security.md](docs/security.md) for the hardening guide. The short version
of what you are trusting:

- **Your host is the trust boundary.** Anyone with shell access, or read access
  to the data directory, can read everything. The database is not encrypted at
  rest — use full-disk encryption if you need that.
- **Messages are not end-to-end encrypted**, and cannot be while a server stores
  history and hands text to agent backends. Transport is TLS; storage is your
  filesystem. Anyone claiming otherwise about an app shaped like this is
  selling something.
- **It is currently single-account, not multi-user.** One PIN grants full
  access; there are no per-person accounts yet. Everyone who unlocks sees
  everything. See the roadmap below.
- **Privacy mode** lets a device hold nothing locally: no offline cache, no
  saved settings, no persistent session. See
  [docs/security.md](docs/security.md#privacy-mode).

## Your data

Everything lives in one directory (`~/.local/share/local-chat` by default,
`/data` in the container images, `DISPATCH_DATA_DIR` to move it):

```
data/
├── chats.db                      # SQLite — messages, threads, users, sessions
├── media/                        # inline images and video
├── files/                        # file-server attachments
├── avatars/                      # avatar pools drawn by new threads
├── reactions/                    # reaction images: pack, per-bot pools, spent
├── backups/                      # rotating snapshots, integrity-checked
├── logs/
├── config.yaml                   # your bot roster and settings
├── reactions.yaml                # the named, reusable reaction pack
├── reaction-prompts-<bot>.yaml   # per-bot image prompt bank
└── .instance.lock                # single-process guard; ignore it
```

Back it up by copying the directory. Migrate hosts by moving it. Delete it and
DisPatch starts fresh. There is no hidden state anywhere else, and nothing is
transmitted off the machine unless you configure an agent backend that does so.

## Development

```bash
cd backend
uv sync                 # Python 3.12+
uv run pytest           # the test suite
uv run uvicorn app.main:app --reload --port 8765
```

The frontend has **no build step** — it is ES modules and CSS served as-is.
Edit a file, reload the page. Cache busting is two moves, not one: bump the
asset's own `?v=N` counter where it is referenced, and bump the `CACHE` constant
in `sw.js` to invalidate the shell.

Before opening a pull request:

```bash
python3 scripts/scrub_check.py      # no private data may enter the repo
python3 scripts/check-locales.py    # translations complete, consistent and safe
cd backend && uv run pytest -q
```

## Roadmap

Designed in detail and not yet built. Both designs are in the repository because
they are the next things to happen and are worth reviewing before they are:

- **[Per-person accounts](docs/design/multi-user.md)** — real users, roles, and
  per-conversation access, replacing the single shared PIN. Includes the
  migration that preserves an existing install's history.
- **[Password + TOTP, and passkeys](docs/design/remote-access-and-auth.md)** —
  proper authentication with recovery codes, plus the honest account of where
  passkeys help and where they cause self-hosting pain.

Until those land, treat DisPatch as a private chat for one household with one
shared credential. That is what it is today, and it is good at it.

## Translating

Every string lives in `frontend/static/locales/<lang>.json`. Adding a language
is one file and one line — no code. See [docs/i18n.md](docs/i18n.md).
Translation contributions are very welcome and are the easiest way to help.

## License

Copyright (c) 2026 Laser Lloyd. <!-- scrub-ok: the copyright holder is named on purpose — MIT is meaningless without an attributable notice, and this line mirrors LICENSE. -->

[MIT](LICENSE). You can run, modify, share and sell this freely — keep the
copyright notice and the licence text with it, and it comes with no warranty.

The vendored third-party libraries in `frontend/static/vendor/` keep their own
licences (MIT, BSD-3-Clause, Apache-2.0/MPL-2.0) — inventory and full licence
texts in
[frontend/static/vendor/README.md](frontend/static/vendor/README.md).
