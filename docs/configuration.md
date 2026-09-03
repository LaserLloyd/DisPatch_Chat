# Configuration

Two places, with a clear split:

- **Environment variables** — where things live, what ports, what limits.
  Read once at startup. Full annotated list in [`.env.example`](../.env.example).
- **`config.yaml`** in your data directory — the bot roster and per-bot
  settings. Created on first run and **hot-reloaded**, so you can edit it while
  the app is running.

Credentials live in neither. They are set through the app and stored hashed.

## Where your data lives

```
DISPATCH_DATA_DIR=~/.local/share/local-chat     # the default
```

The default follows the XDG spec (`$XDG_DATA_HOME/local-chat`, falling back to
`~/.local/share/local-chat`). The Docker images set it to `/data` and mount a
volume there, so in a container this is already handled.

Every setting also answers to a legacy `LOCAL_CHAT_` prefix — the project was
called local-chat first, and existing service units still use those names.
`DISPATCH_` wins where both are set. Only the `DISPATCH_` names are documented.

Everything is under here — database, media, file blobs, backups, config,
generated assets. Back it up by copying the directory; move hosts by moving it;
delete it and DisPatch Chat starts fresh. There is no hidden state elsewhere.

```
data/
├── chats.db             # SQLite (WAL): messages, threads, users, sessions
├── chats.db-wal         # write-ahead log; folded in on clean shutdown
├── media/               # inline images and video
├── files/               # file-server attachments
├── backups/             # rotating snapshots, integrity-checked
├── logs/
├── config.yaml          # bot roster (hot-reloaded)
└── .instance.lock       # single-process guard; ignore it
```

## Network

```
DISPATCH_HOST=127.0.0.1     # the default
DISPATCH_PORT=8765
```

Loopback is the default deliberately: the app ships with no credential set, so
a wider default would expose every message on a machine with a port forward
before the operator had finished reading this page. Bind wider only when something in front is
terminating TLS — see [remote-access.md](remote-access.md). If you bind to
`0.0.0.0` with no account configured, the app warns loudly at startup and the
dashboard raises a finding, because at that point everyone on the network has
full access.

**Do not pass `--workers`.** Sessions, WebSocket connections, rate limiters and
background loops are in-process state. The app takes an exclusive lock on its
data directory and refuses to start twice against it, so this fails loudly
rather than producing an app that half-works.

## Limits

**Every numeric setting is a plain integer.** There are no `20GB` / `6h`
suffixes anywhere — a suffixed value is a startup crash, not a rounding error.
Sizes are bytes, durations are seconds.

A server-wide ceiling across all stored blobs (chat media + file server):

```
DISPATCH_FILES_TOTAL_MAX=21474836480    # bytes; 20 GiB, the default. 0 disables
```

The per-file ceiling is fixed at 4 GiB and is not configurable.

Daily per-device budgets for the limited (no-password) tier. These are
availability guards — they bound what an unauthenticated device on your network
can spend:

```
DISPATCH_DECOY_UPLOAD_QUOTA=209715200   # bytes uploaded per day
DISPATCH_DECOY_TURN_QUOTA=200           # messages sent
DISPATCH_DECOY_THREAD_QUOTA=50          # conversations created
```

The message budget is the one to think about: each message to a bot spawns an
agent turn, which may cost real money. In-memory by design — they reset on
restart, which is fine for a coarse guard.

## Agents

```
OPENCLAW_BIN=openclaw            # path to the agent CLI; unset/missing = no
                                 # agent backend (fully supported)
DISPATCH_AGENT_TIMEOUT=900       # seconds per turn
DISPATCH_MAX_CONCURRENCY=3
```

`OPENCLAW_BIN` is the one setting here that does **not** take the `DISPATCH_`
prefix — it is read straight from the environment under that exact name,
because it is the agent runtime's own variable. Use an absolute path under
systemd: a unit does not source your shell profile.

`DISPATCH_MAX_CONCURRENCY` caps simultaneous agent turns. Keep it low if the
agent runs a local model — three concurrent turns against one GPU is usually
three slow turns. See [agents.md](agents.md).

## Direct LLM providers

There are no environment variables for these. A bot connected through **Connect
an AI** (Settings → 🔌 AI models) is configured in `config.yaml` in the data directory,
under its own `api` block — provider, base URL, model, and either a stored key
or the *name* of an environment variable holding one:

```yaml
  api:
    provider: openai
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: OPENAI_API_KEY     # or api_key: sk-… , stored in this file
```

`config.yaml` is written `0600` because it can hold credentials. Full reference,
including every provider's defaults and the key-storage trade-off:
[llm-providers.md](llm-providers.md).

## Backups

```
DISPATCH_BACKUP_INTERVAL=21600   # seconds between snapshots (6 h, the default)
                                 # 0 disables the loop; manual export still works
DISPATCH_BACKUP_KEEP=12          # how many rotated snapshots to keep (default)
```

Rotating online snapshots (`VACUUM INTO`, so they are consistent), integrity
checked, kept in `data/backups/`.

**These are snapshots of the database only.** Media and file blobs are not in
them. A real backup copies the whole data directory, off this machine, and you
have restored one at least once.

## The bot roster

`config.yaml`, hot-reloaded:

```yaml
bots:
  - id: assistant
    name: Assistant
    emoji: "✨"
    avatar: assistant.png
    order: 0
    visible: true
    safe: false          # true = also reachable from Safe Mode (no PIN)
```

`safe` is the access flag, and it is server-enforced: a bot without it is
invisible *and* unreachable to a Safe-Mode session, not merely hidden. Turning
it on is the deliberate decision — it means anyone who can reach the port,
with no credential, can spend agent turns on that bot.

Other per-bot fields: `color` (pins the letter-block avatar colour),
`reactions` (may this bot fire reaction images — off for every shipped bot),
`reaction_autopilot`, `avatar_pool`, `image_jobs` (may this bot ask for a
generated picture — also off for every shipped bot), `image_workflow` (the
image-server workflow its pictures use when a request names none; empty means
the server's own default), and an `api:` block for a
direct LLM provider. See [agents.md](agents.md) and [llm-providers.md](llm-providers.md).

## Posting in from outside

```bash
curl -X POST http://127.0.0.1:8765/api/inject \
  -H 'Content-Type: application/json' \
  -d '{"bot_id": "assistant", "content": "Backup finished."}'
```

The field is `content` (`text` is accepted as an alias, because every other
chat transport calls it that — an empty message is refused rather than posted
as a blank bubble). Loopback needs no credential; remote callers need
`X-API-Key`. Use `thread_id` to target a specific conversation, or `bot_id`
(case-insensitive; the canonical roster id is used) to find-or-create that
bot's daily thread.

## Feature switches

```
DISPATCH_TERMINAL=0     # host shell in the browser
DISPATCH_MIRROR=0       # import an agent runner's own conversations
```

The terminal additionally needs to be told *which* CLI to run. There is no
default binary — an unset `DISPATCH_TERMINAL_BIN` leaves the feature reporting
"not configured" rather than guessing at somebody's install layout:

```
DISPATCH_TERMINAL_BIN=          # name resolved on PATH, or an absolute path
DISPATCH_TERMINAL_PATH=         # optional: extra PATH entries, colon-separated
DISPATCH_TERMINAL_CONFIG=       # optional: TOML paths the model picker reads
```

The CLI itself is external and not distributed with this project.

**Read the defaults carefully: in the code both are ON (`1`).** They are
host-install features, so what actually turns them off for most people is the
shipping configuration rather than the code — the container image and
`.env.example` set both to `0`, and the system unit sets the terminal to `0`.
On a bare-metal install started by hand they are on.

**Both the terminal and the harness stay unavailable (403, and the terminal socket
refuses) until a PIN is set** — they run code, and with no credential there is no
unlocked session for the gate to check. `DISPATCH_TERMINAL` gives an authenticated admin an interactive shell on the
host, running as the app's user. It is exactly as dangerous as it sounds. It and
the harness pane below are gated on a fully-unlocked session — which means
**setting a PIN is a precondition, not an afterthought**: with no credential
configured there is no unlocked session for the gate to check. Set the PIN
before you turn either of them on.

Only `0`, `false`, `False` and empty read as off. `no` and `off` read as **on**;
write `0` when you mean off.

### Local viewer

The local viewer opens a file, folder or static site from the **host's disk**
inside the app, for a fully-unlocked session only. It is **off until you give it
a root**: with no roots configured every route answers
`404 {"detail": "Local viewer is off — add a root in Settings"}`.

Configure it from **Settings → Device → Local viewer**, which writes
`<data>/local-viewer.yaml` (0600, atomic):

```yaml
roots:                      # absolute or ~-prefixed directories; empty = OFF
  - ~/Projects
  - /srv/reports
deny:                       # extra always-denied prefixes or globs, merged
  - ~/Projects/private      #   with the built-in list (below)
show_hidden: false          # dot-components BELOW a root are refused unless true
max_text_bytes: 2000000     # in-app text render cap; larger files download only
```

A malformed file turns the feature **off** and logs one error — it does not
guess. Changes are picked up without a restart (the file is mtime-cached).

For containers and images, where there is no Settings pane on first boot, the
roots can be seeded from the environment. It is only read when
`local-viewer.yaml` is **absent**; once the file exists, the file wins:

```
DISPATCH_VIEWER_ROOTS=/srv/reports:/srv/site    # os.pathsep-separated
```

**What it will never serve, whatever you put in `roots` or `deny`:** `~/.ssh`,
`~/.gnupg`, `~/.config/secrets`, `~/.openclaw/{secrets,agents}`,
`~/.openclaw/gateway.systemd.env`, `~/.config/systemd`, `/etc`, `/proc`, `/sys`,
`/dev`, this app's own `security.yaml`, `trusted-devices.yaml`,
`RECOVERY-CODE.txt`, `chats.db*`, `backups/` and `local-viewer.yaml` — plus any
file matching `*.env .env* *.pem *.key *.p12 *.pfx id_* *.kdbx *.gpg *.asc
known_hosts authorized_keys *.sqlite *.db`. Denied paths, hidden paths and paths
outside every root all answer the same `403 Not served by the local viewer`, so
the surface cannot be used to probe the disk. Refusals are logged and counted as
`viewer_denied_24h` in `/api/health`.

The routes are **browser-only on purpose**: they are not on the machine-inbound
surface, so an `X-API-Key` holder (an on-box cron, a GPU rig) gets Safe Mode
here even on loopback. See [security.md](security.md).

### Agent image jobs

```
DISPATCH_CLAWFORGE_URL=         # the image server's MCP endpoint, e.g.
                                #   http://192.0.2.5:8700/mcp
DISPATCH_CLAWFORGE_FILES_URL=   # optional; defaults to /files/ on the same host
DISPATCH_CALLBACK_BASE=         # optional; DisPatch's own origin AS THE RIG
                                #   SEES IT, e.g. http://192.0.2.37:8765
DISPATCH_IMAGE_JOBS=auto        # auto (default) | 1 | 0
```

Lets a bot ask for a generated picture without its turn hanging behind the
render: `POST /api/image-jobs` returns immediately, a placeholder message
appears in the thread, and DisPatch rewrites that message into the picture (or
into a visible failure line) when the render lands. There is deliberately **no
default endpoint** — the address of a GPU box is site configuration — and
`auto` therefore means "on iff `DISPATCH_CLAWFORGE_URL` is set".

Two switches have to agree before anything happens: this one, and
`image_jobs: true` on the individual bot in `config.yaml`. It is off for every
shipped bot, for the same reason reactions are — it spends shared GPU time and
puts a picture into a family conversation.

**`DISPATCH_CALLBACK_BASE` is an optimisation, not a requirement.** Set it and
each submitted job carries a `callback_url` plus a per-job token, so the image
server can say "this one is finished" instead of DisPatch noticing on its next
poll — terminal transitions land in about a second rather than within five. It
cannot be derived, because it is what *the image server* has to dial to reach
this box; leave it empty (the default) and the worker polls exactly as before.
The callback route (`POST /api/image-jobs/<job_id>/callback`) authenticates on
that per-job token alone, **discards the request body**, and re-polls the rig
itself, so a forged call can at worst cost one wasted poll. The safety net is
unchanged either way: every job still has a ten-minute deadline, and the sweep
still runs on its own cadence.

Jobs submitted through a thread go to the image server's **interactive** queue
band (`priority: 1`); `POST /api/image-jobs` accepts an explicit `priority` of
`1`, `2` or `3` if a caller knows its picture is not urgent. Reaction- and
avatar-pool refills submit at `3` (background), so a shelf top-up never puts a
waiting family member behind it.

A render can also end as **cancelled** — DisPatch withdraws one that has passed
its deadline or whose thread was deleted, and an operator interrupting the job
on the rig produces the same thing. That is its own visible ending ("✋ The
image was cancelled on the rig."), not counted as a failure in `/api/health`.

**When the image server cannot be reached**, the placeholder says so —
"🖼️ Waiting for the image rig (ConnectError)…" — and goes back to the plain
placeholder the moment the server answers again; the job itself keeps its
ten-minute deadline. DisPatch keeps one MCP session open to the server and
reuses it (a server that forgets the session gets one fresh handshake and the
call repeated), and a server that refuses connections is left alone for
fifteen seconds before the next attempt, so a rack of queued jobs fails fast
instead of each waiting out its own connect timeout. `GET /api/health` reports
this as `image_rig`: `{"reachable": true|false|null, "unreachable_for_s": …,
"last_error": "…"}` (`null` = image jobs are not configured).

The expected server speaks MCP over streamable HTTP and offers
`generate_image` (with `wait: false`), `get_job`, `cancel_job` and a
`/files/<path>` route;
[ClawForge](https://github.com/) is the reference implementation. Nothing else
is assumed: DisPatch validates the returned bytes are a real image over 10 KB
before any of it reaches a chat window.

Both routes are on the machine surface (loopback free, remote needs the API
key) and are refused to a locked browser tab. See
[agents.md](agents.md#asking-for-a-generated-image) for the agent-facing side.

### DeepSeek Harness (`dsh`)

```
DISPATCH_HARNESS=auto           # auto (default) | 1 | 0
DISPATCH_HARNESS_UNIT=dsh-web.service
DISPATCH_HARNESS_PORT=3080
DSH_HOME=~/.dsh                 # dsh's own home (settings.yaml, .credentials.yaml)
```

A second coding-agent engine next to the terminal, for
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(`npm i -g @deepseek-ai/dsh`). `auto` turns the pane on only when a `dsh`
binary is found at boot, so an install without it never grows a stray
sidebar entry. The pane is **admin-only, like the terminal** — a headless job
is code execution — and Safe Mode never sees it or its state frames.

What it does:

* **Embeds the `dsh web` UI** (loopback only; the frame renders when DisPatch
  itself is opened on the host — remote tabs get an explanation instead) and
  **controls its systemd `--user` unit** (`DISPATCH_HARNESS_UNIT`, expected to
  bind `127.0.0.1:DISPATCH_HARNESS_PORT`). A sample unit:

  ```ini
  [Service]
  WorkingDirectory=%h
  Environment=DSH_HOME=%h/.dsh
  # `dsh` on PATH, or the absolute path your install put it at — a user unit
  # does not read your shell profile, so PATH is whatever the unit says.
  ExecStart=/usr/local/bin/dsh web --host 127.0.0.1 --port 3080
  Restart=on-failure
  [Install]
  WantedBy=default.target
  ```

* **Default-model switch** — rewrites `agent-default-model` in
  `$DSH_HOME/settings.yaml`, which dsh hot-reloads for the next new session
  (Web UI and headless alike). The picker lists dsh's DeepSeek route plus every
  `llm-pi-ai.providers.*` route in that file, so a local OpenAI-compatible
  server appears once you add it there. Credentials never pass through
  DisPatch: dsh resolves `apiKeyEnv` references from its own
  `.credentials.yaml`.
* **Headless jobs** — `POST /api/harness/jobs {task, cwd?}` runs
  `dsh --profile headless "<task>"` (fixed argv, no shell; `cwd` must be an
  existing directory under `$HOME`), one at a time, and keeps a short in-memory
  history with the final answer. `harness_state` WS frames announce start/end.

Routes: `GET /api/harness/status`, `POST /api/harness/{start,stop,restart}`,
`GET /api/harness/models`, `POST /api/harness/model`, `GET|POST
/api/harness/jobs`, `POST /api/harness/jobs/cancel`, `GET
/api/harness/jobs/<id>`. All 403 in Safe Mode; 404 when disabled.

## Everything else the code reads

Rarely-touched settings, listed so that the documented set and the set the code
actually reads are the same set.

| Variable | Default | What it does |
|---|---|---|
| `DISPATCH_DATA_DIR` | XDG path | Where all state lives (see the top of this page). |
| `DISPATCH_GREETING` | `0` | Show a canned greeting in a brand-new thread (no model call). |
| `DISPATCH_LOG_FILE` | unset | Also write the log to this file, and tell the dashboard's log viewer where to tail. |
| `DISPATCH_PUBLIC_URL` | unset | The address people type to reach this install, scheme included. Declares that something in front terminates TLS. |
| `DISPATCH_BEHIND_TLS` | unset | The same declaration without naming a URL. Any value but `0`/`false`/`no` means yes. |
| `DISPATCH_MIRROR_POLL` | `4` | Mirror poll cadence, seconds. |
| `DISPATCH_MIRROR_IDLE_MAX` | `60` | Ceiling the idle mirror poll ramps up to, seconds. |
| `DISPATCH_MIRROR_HORIZON_H` | `72` | A transcript already older than this when first seen is tailed from EOF, not imported. |
| `DISPATCH_MIRROR_KINDS` | `webchat,main` | Which session kinds to mirror. `other` adds scripted/watchdog sessions. |
| `DISPATCH_GATEWAY_WS` | unset (off) | Native agent-gateway WebSocket transport: unset/`0` off, `shadow` connects and logs what it *would* deliver, `1` live. |
| `DISPATCH_TURN_TRANSPORT` | `auto` | How a turn is *sent* (the WS transport above is how the reply comes back): `auto` = over the gateway socket when one is connected, else spawn `openclaw agent`; `1` = socket only (a turn with no socket fails instead of quietly costing ~1.1 s more); `0` = the per-turn CLI spawn. |
| `OPENCLAW_GATEWAY_URL` | `ws://127.0.0.1:18789` | Where that transport connects. |
| `OPENCLAW_GATEWAY_TOKEN` | unset | Its auth token. There is **no** loopback exemption on the gateway side. |
| `DISPATCH_HARNESS` | `auto` | Coding-agent harness pane: `auto` = on iff a `dsh` binary is found at boot, `1` forces on, `0` off + 404. |
| `DISPATCH_HARNESS_UNIT` | `dsh-web.service` | The systemd `--user` unit that runs `dsh web`. |
| `DISPATCH_HARNESS_PORT` | `3080` | The loopback port that unit binds. |
| `DSH_HOME` | `~/.dsh` | `dsh`'s own home — read straight from the environment under that name, because it is `dsh`'s variable, not ours. |
| `DISPATCH_PBKDF2_ITERATIONS` | `200000` | PBKDF2 rounds for the PIN hash. Lower it only on hardware that genuinely cannot afford the default, and know what you are trading. |
| `TMPDIR` | system | Where multipart uploads spool. Keep it on real disk on the data volume — on a tmpfs a 4 GiB upload is a 4 GiB RAM allocation. |

### Optional image-host integration

These drive the nightly top-up of the reaction and avatar pools. Every one of
them is optional; with no CLI configured the pools simply never refill on
their own. All accept both spellings (`DISPATCH_` preferred, `LOCAL_CHAT_`
legacy), like everything else in this file.

| Variable | Default | What it does |
|---|---|---|
| `DISPATCH_IMAGE_CLI` | the image CLI named by `DISPATCH_IMAGE_CLI` | Path to the image-generation CLI used to top up the pools. |
| `DISPATCH_IMAGE_CLI_TIMEOUT` | `300` | Seconds one generation may take (a cold model load is slow). |
| `DISPATCH_GPU_CLI` | unset (guard off) | A GPU-host companion CLI that answers `status --json`, `models settings --json -- <id>` and `models unload --json -- <id>`. Only needed when the image host's GPUs are shared with an LLM server. Never guessed from PATH — set it explicitly. |
| `DISPATCH_MINT_MIN_FREE_GB` | `10` | Free VRAM (GiB) the image host must have before a refill is attempted; below it the refill backs off instead of tight-looping refused calls. |
| `DISPATCH_POOL_FREE_VRAM` | `0` | `1` lets the guard unload non-pinned, idle LLM models on the image host to make room. Leave at `0` when those GPUs belong to someone else — the headroom check and back-off still run, nothing is evicted. |
| `DISPATCH_GLYPH_FONTS` / `DISPATCH_TEXT_FONTS` | built-in search path | `:`-separated font files for the starter reaction cards (pictograms / captions). Overrides the built-in Linux/macOS list; Pillow's bitmap font is the last resort. |
| `DISPATCH_DSH_BIN` | unset | Explicit path to `dsh` for the coding-agent harness when it is installed outside the service's `PATH` (a user-local npm prefix, say). |

Both integrations are entirely optional: with neither CLI present the pools
simply never refill themselves, and nothing else changes.
