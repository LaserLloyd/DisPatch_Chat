# Extensions — installed add-ons

An **extension** is a separately installed Python package that plugs a whole
feature into DisPatch: backend routes, a gated websocket, its own frontend
(loaded into the shell), Settings panes, rail tools and per-bot controls.
DisPatch ships the host and nothing else; the feature lives in the package.

Today there is one: **[DisPatch Voice](https://github.com/LaserLloyd/DisPatch_Voice)**
(MIT), which is all of voice chat — speech recognition, turn-taking, voices,
the voice chat screen and its settings. DisPatch installs and runs fully
without it.

How an extension differs from the other two ways of adding to DisPatch:

| | [Tools](configuration.md#tools-data_dirtoolsyaml) | [Apps](apps.md) | Extensions |
|---|---|---|---|
| What | a sandboxed page (static folder or URL) | a trusted package in `apps/<id>/` | a trusted package installed into DisPatch's Python environment |
| Runs code in the server | no | yes (`build(ctx)`) | yes (`setup(ctx)`) |
| Found by | `tools.yaml` | the `apps/` folder (or a trusted data-dir row) | the `dispatch.extensions` entry point group |
| Frontend | framed page | framed page + optional thread hook | modules mounted **into** the shell's DOM through a frozen `shell` object |

- [Settings tabs and tool packages](#settings-tabs-and-tool-packages)
- [Voice chat (DisPatch Voice)](#voice)
- [Discovery and the handshake](#discovery-and-the-handshake)
- [Writing an extension: the backend](#writing-an-extension-the-backend)
- [Writing an extension: the frontend](#writing-an-extension-the-frontend)
- [Security model](#security-model)
- [Versioning](#versioning)

---

## Settings tabs and tool packages

An extension can add a tab of its own to Settings: ask the shell for a Settings
pane with a new id (lowercase letters, digits and hyphens) and a title, and the
tab and its pane are created for you, in an **Add-ons** group after the household
sections. Ids the shell already knows (the sections and every tab id earlier versions
had) keep their place and open the section that holds those controls now. A package can also add lookups for the bots to use, by
registering tool specs on the extension context (`register_live_tools`). Settings →
Plugins & Apps lists every installed add-on and lets you switch one off (the choice is
kept in `extensions.yaml`, see [configuration.md](configuration.md)). The
sections below, [Writing an extension](#writing-an-extension-the-backend), cover
the API.

## Manifests, permissions and approval (3.0)

Every way of adding a tool - a directory or URL row in `tools.yaml`, an `apps/<id>/` package, an add-on's
`dispatch.extensions` entry point - is described by one **Tool manifest**
(`docs/contracts/manifests/tool-plugin.schema.json`: `id`, `name`, `version`, `publisher`, `entry`, the `permission` a
profile needs to use it, `permissions_requested`, a `settings` fragment, `min_app_version`). `app/tool_manifests.py`
reads the existing sources as they are and produces the manifests; nothing about how an add-on or app is written
changes. `GET /api/plugins/manifests` returns them with the permissions each one asks for, in plain words:

| Id | Means |
|---|---|
| `http_routes` / `websocket` / `ui` | adds its own web API, live sockets, pages and scripts |
| `live_tools` / `panes` / `character_fields` | gives characters tools, adds rail tiles, stores its own settings on characters (a voice) |
| `background_work` / `large_uploads` | runs at start-up and shutdown, accepts large uploads |
| `run_code` / `character` / `thread_hook` | an app package runs inside the server, adds a character, adds a panel to its chats |
| `network:<host>` / `env:<NAME>` | contacts that host / reads that setting |

**Approval.** A plugin that was running when 3.0 first started is approved automatically (`source: upgrade`; the
migration step `007_plugin_approvals` lists them). One that appears later must be approved by someone with
`manage_plugins`, who sees what it asks for: turning it on with `PUT /api/plugins/extensions/{id}` answers
`409 plugin_needs_approval` with the permission list until the request carries `"approve": [<that list>]`. Approval
is recorded with who gave it; switching a plugin off never needs one. (Loading an installed add-on is still the
extension host's decision; approval gates the Plugins switch, and the audit log shows who approved what.)

**Settings.** An app's `env:` names, and any `settings` a manifest declares, appear in Settings -> Plugins & Apps under
`tools.<id>.` (secrets are write-only) and reach the app through `ctx.env`, whether the value came from the
environment or from Settings. An add-on registers its own rows with `settings_schema.register(...)` and writes values
with `settings_store.writer("<namespace>")` - see [settings.md](settings.md#settings_storewriternamespace---for-add-ons).

## Voice

Voice chat lets you talk to a thread hands-free: DisPatch listens, works out
when you have finished, sends what you said like a typed message and speaks
the bot's reply back sentence by sentence. Everything runs on the server's
CPU; nothing you say leaves the machine. It understands and speaks English
only for now. Since 2.3.4 it works on a locked (Safe Mode) device too, on the
bots that device can chat with (the `safe` ones), and every turn it sends runs
with the same locked rules as a typed Safe-Mode message. Voice settings stay
unlocked-only.

**Settings → Voice** tells you which state you are in. Without the add-on it
explains what voice chat is and shows the install command below, ready to copy,
with a **Check again** button that mounts the add-on without a page reload once
you have restarted DisPatch. With it, that page is the whole setup: the switch,
the model download (a progress bar and Cancel, no terminal), the providers, the
microphone test and the voice of each character. [voice.md](voice.md) counts the
steps from a fresh Docker install and lists what is left.

### 1. Install it

On the DisPatch host, in `backend/` (where DisPatch's `.venv` lives):

```bash
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
```

`v1` is a **moving tag**: it always names the latest 1.x release, so a fresh
install gets the current add-on. For a reproducible install pin an exact
release instead (`…DisPatch_Voice@v1.0.0`).

> **`uv sync` removes the add-on again** (it makes the venv match DisPatch's
> lock file exactly). Re-run the line above after a `uv sync`;
> `.venv/bin/dispatch-voice doctor` tells you when that happened.

**A service that starts DisPatch with `uv run`** (the systemd units in
[deploy-bare-metal.md](deploy-bare-metal.md) do): add the add-on to that line
instead — `uv sync` then leaves it alone, and an editable checkout is live on
the next restart. That is also its risk: **point the overlay at a release
checkout that nobody develops in**, never at the checkout you work in —
whatever is in that folder goes live at the next restart, and a dependency a
work-in-progress adds makes the offline overlay fail to resolve:

```bash
git -C /path/to/DisPatch_Voice worktree add --detach /path/to/voice-release v1.0.0
# a later release: git -C /path/to/voice-release checkout --detach v1.1.0, warm, restart
```


```bash
uv run --offline --with-editable /path/to/DisPatch_Voice \
  uvicorn app.main:app --host 0.0.0.0 --port 8765 --ws-max-size 262144
```

Two flags are load-bearing:

- `--offline`: a boot must never need the network. Without it, a reboot with
  the network down makes uv re-resolve the overlay for seconds and exit 2 —
  and the unit's restart loop keeps the whole app down. Because of it, **warm
  the overlay online once** after every add-on update, before the restart:
  `uv run --with-editable /path/to/DisPatch_Voice python -c "import dispatch_voice"`
  (in `backend/`). `dispatch-voice doctor` reports "overlay resolvable
  offline: yes/no". Never `uv cache clean` on a box that runs this way.
- `--ws-max-size 262144`: the frame cap for every websocket. The voice socket
  never needs more, and the chat's own `/ws` never sends more than a
  65,536-character message (≤ 196 KB of UTF-8), so nothing legitimate is cut.

**Recommended service command:** `scripts/local-chat-launch` runs that line,
but first proves, in a throwaway process, that the overlay resolves offline and
that the add-on's host package and its native libraries import (numpy,
onnxruntime). When that fails — a cold cache, a broken wheel, even a native
crash of the probe — it starts DisPatch **without** the add-on and says why in
the journal: voice chat is lost, the app is not.

```
WorkingDirectory=/path/to/dispatch/backend
ExecStart=/path/to/dispatch/scripts/local-chat-launch --uv /path/to/uv \
    --with-editable /path/to/voice-release -- \
    uvicorn app.main:app --host 0.0.0.0 --port 8765 --no-access-log \
    --ws-ping-timeout 60 --ws-max-size 262144
```

`--ws-ping-timeout 60` lets a 20–60 s cellular stall be judged by voice chat's
own heartbeat rules instead of uvicorn's 20 s keepalive. `dispatch-voice doctor
--unit <unit>` checks the add-on with the unit's own settings.

**Docker**: build the image with the add-on. `VOICE=1` also installs `ffmpeg`
and `libopus0` in the image (the add-on shells out to them: cloning a voice
decodes the clip you upload; the low-bandwidth link uses Opus). The models are
never baked in: the add-on's page downloads them into the data volume.

```bash
VOICE=1 docker compose build && docker compose up -d
```

`--build-arg VOICE_REF=` picks the add-on's ref: a full ref (`refs/tags/v1.6.0`,
`refs/heads/<branch>`) or a bare tag name (`v1.6.0`). The default is `refs/heads/main`
of the add-on, which carries the 1.6.0 line (the `v1` tag still names 1.5.0, which
the host no longer supports); it moves to a release tag when 1.6.0 ships. The
add-on is two packages from one repository (`dispatch-voice` and its engines,
`forge-voice-core`); the Dockerfile installs both from the same ref or directory
and the build fails unless `import dispatch_voice, forge_voice_core` works. To build from a local
copy (CI, an air-gapped host, a branch nobody pushed yet) hand the Dockerfile the
directory as a named build context; export it clean first (`git archive`), the
whole directory is sent:

```bash
git -C /path/to/DisPatch_Voice archive HEAD | tar -x -C /tmp/voice-src
docker build --build-arg VOICE=1 --build-arg VOICE_SRC=local \
  --build-context voice-src=/tmp/voice-src -t dispatch:voice .
```

### 2. Restart DisPatch

Restart the service (or, in Docker, nothing: the image already has it) so the
entry point is discovered. Everything after this is Settings → Voice.

### 3. Turn voice chat on and download the models

Open **Settings → Voice**, press **Check again** if the install page is still
showing, switch voice on and press **Download** under *Models*: about 1.4 GB,
once (speech recognition 631 MB, the voice 688 MB, turn detection 11 MB), with a
progress bar and **Cancel**. Speech works as soon as it finishes; no restart is
needed to hear a reply. A headless server can do the same from a shell with
`.venv/bin/dispatch-voice fetch-models`.

The files land in **`<data>/models`** (or `DISPATCH_SPEECH_MODELS`), in a `speech/`
and a `chatterbox/` subfolder. Before 3.0 that folder was `<data>/speech-models`; at
start DisPatch moves it across once (never overwriting; see
[the models folder](#the-models-folder)).

The journal shows `ext voice: DisPatch Voice 1.x.y ok (api 1)`,
and `curl -s 127.0.0.1:8765/api/extensions` lists `voice` as `ok` (loopback is
the machine tier for that one route). `/api/voice/*` needs a signed-in session (the reads below are any voice user's,
the rest `manage_plugins`): with a PIN set, `curl` it with a session cookie, e.g.
`curl -s -c jar -H 'Origin: http://127.0.0.1:8765' -H 'Content-Type: application/json'
-d '{"pin":"…"}' 127.0.0.1:8765/api/auth/unlock` and then `curl -s -b jar
127.0.0.1:8765/api/voice/status`.

### Optional: Kokoro built-in voices

A separate plugin of the add-on under **GPL-3.0-or-later** (its phonemizer
and espeak-ng are GPL-3.0 and load in process). Installing it makes the running
DisPatch process a combined work with GPL-3 code — fine for private use; a
redistribution that includes it must follow GPL-3 terms.

```bash
uv pip install --python .venv/bin/python /path/to/dispatch-voice-kokoro
# and in the service environment:
DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup
```

### Removing it, or turning it off

Uninstall the package (or drop the `--with-editable`) and restart: Settings →
Voice shows the install page again. `DISPATCH_EXTENSIONS=off` keeps it
installed but unloaded (Settings → Voice says so). Your `voice.yaml`, recorded
voices (`<data>/voices/`), each bot's `voice` and the voice tools' rows in
`tools.yaml` are kept untouched — they work again the moment the add-on is
back (a `tools.yaml` builtin row no extension provides is a *dormant* row:
kept, not listed, logged once).

### Upgrading from DisPatch 2.1

2.1 shipped voice in-tree, run with `uv run --group voice` and
`DISPATCH_VOICE_PKG=<dispatch-voice/src>`. In 2.2 the `voice` dependency group
is still declared but **empty**, so a 2.1 unit line keeps booting (without the
group uv refuses to start). `DISPATCH_VOICE_PKG` is honoured for 2.2.x only
(logged as deprecated). Move to one of the install modes above; both go away
in 2.3.0.

---

## Discovery and the handshake

At startup DisPatch looks for extensions, in this order:

- `DISPATCH_EXTENSIONS=off` — nothing is imported; installed ones list as
  `disabled`.
- `DISPATCH_EXTENSIONS=module:attr[,module:attr]` — exactly those (a
  checkout that is not installed: put its `src/` on `PYTHONPATH`).
- otherwise every entry point in the **`dispatch.extensions`** group.

For each one the **metadata** is read — the add-on's top-level module must be
cheap (no numpy, no models):

```python
# src/dispatch_voice/__init__.py
EXTENSION = {
    "id": "voice",                        # [a-z][a-z0-9-]{1,31}, unique
    "name": "DisPatch Voice",
    "version": "1.0.0",                   # informational, never compared
    "api": [1, 1],                        # the EXT_API range it accepts
    "setup": "dispatch_voice.host:setup", # imported only after the handshake
    "env": ["DISPATCH_SPEECH_MODELS", "DISPATCH_VOICE_MODELS"],  # copied into ctx.env
}
# pyproject.toml
# [project.entry-points."dispatch.extensions"]
# voice = "dispatch_voice:EXTENSION"
```

If `api[0] <= EXT_API <= api[1]` (DisPatch 2.2 has `EXT_API = 1`), `setup(ctx)`
is imported and called. Every outcome is a **state**, never a failed boot:

| State | Meaning |
|---|---|
| `ok` | mounted |
| `incompatible` | the API range excludes this DisPatch — `detail` says which side to update |
| `failed` | importing or running `setup` raised — everything it had registered is rolled back; `detail` is `Type: message`, the traceback is in the journal |
| `disabled` | installed, but `DISPATCH_EXTENSIONS` leaves it out, or the operator switched it off in Settings → Plugins & Apps (`<DATA_DIR>/extensions.yaml`) |

A second extension claiming an id that is taken is `failed`.

`GET /api/extensions` (operator, or an authenticated on-box machine such as a
loopback `curl` — that is how a deploy is verified) returns
`{extensions: [{id, name, version, api, state, source, assets_version, entry,
css, locales, tools, detail?}], mode, api}` — never a filesystem path or a
`module:attr` string; `detail` only for operators, ≤ 200 characters with the
home directory shown as `~`. `GET /api/auth/status` carries
`features.extensions`, the number of `ok` ones.

---

## Writing an extension: the backend

`setup(ctx)` runs once, synchronously, during startup. Registration methods
work only inside it (the context is sealed afterwards). **Keep it fast**: it
only registers things — load models and warm caches in
`on_startup(fn, thread=True)`. A `setup` that has not returned after 60 s is
abandoned (the extension is `failed`, its routes rolled back, its context
sealed so a late registration raises) and DisPatch boots without it.

```python
def setup(ctx):
    ctx.serve_static(STATIC, entry="index.js", css=["voice.css"], locales=True)
    ctx.include_router(router, prefix="/api/voice",
                       body_limits={"/api/voice/profiles": 20 * 2**20})
    ctx.thread_websocket("voice", serve, max_total=3)
    ctx.set_bot_field_validator("voice", check_voice)     # raise ValueError → 422
    ctx.register_tools([...])                              # rows read by attribute
    ctx.on_startup(prewarm, thread=True)
    ctx.on_shutdown(release)
```

**Context** (`app/extensions.py ExtensionContext`): `api_version`,
`app_version`, `ext_id`, `log` (`local-chat.ext.<id>`), `env` (only the names
you declared), `bots` (`get(id)` / `all()` → `BotView {id, name, safe, voice,
voice_rp, backend, owner, has_persona}` — never a provider block or a prompt;
`owner` is how an advisor's persona names its user, `has_persona` is False for
an API bot on DisPatch's default system prompt and None for a gateway agent;
both added in 2.2.0, read them with `getattr`), `paths` (`ExtPaths`: your files
by name — `voice_yaml`, `acks_yaml`, `voices_dir`, `cache_dir`, `models_dir`,
and `state_dir` = `<data>/ext/<id>/`, owner-only — never the data directory itself),
`secrets` and `settings_writer(namespace)` ([host services](#host-services-folders-credentials-settings)),
`db()`, `is_operator(request)`, `require_operator(request)`,
`bot_backend(bot_id)`, `socket_count(name)`, `touch_session(token)`,
`counters` (host-side counters such as `speculation_stale`).

> **`db()` writes need an owner (ownership groundwork, Unreleased).** The
> database `db()` returns now takes a required keyword-only owner on the calls
> that create a record: `create_thread(..., owner_profile_id=)`,
> `add_file(..., owner_profile_id=)`, `adopt_file(..., owner_profile_id=)` and
> `find_or_create_daily_thread(..., owner=)`. Omitting it is a `TypeError`.
> Pass the profile the record belongs to; for a machine-made record (an
> extension's own drop, a daily thread) pass the Owner's id
> (`app.ownership.owner_id()`), or `None` on an install without an Owner yet.
> `EXT_API` stays 1: the voice add-on never calls these, and nothing else in
> the extension API changed shape.

**Turns** — `submit_turn` runs under the same rules as a typed message. It
raises `PermissionError` (no row is stored, no turn runs) when the thread's
socket belongs to a caller who may not start this turn: a Restricted caller
on a character it cannot see or out of budget, or a profile without
`dispatch_agents` on a character whose turns run an agent on the host. The
thread socket itself refuses that last caller after accept with 1008 and a
reason, so a client stops instead of retrying.

**HTTP** — `include_router(router, prefix, body_limits=)`: the prefix must be
`/api/<id>` or below it. Every route gets the host's extension dependency
(`extensions.require_extension_route`): the requirement the route table declares
for that path (`route_policy.extension_requirement`, listed in
`docs/contracts/routes.json` under `extension_routes`) — `manage_plugins` by
default, `use_voice` (Member, Admin, Owner; never a Restricted caller) for the
reads a voice user needs: `GET /api/voice/status`, `GET /api/voice/preview` and
`GET /api/voice/profiles` (2026-10-19, "Members should have voice"). Never the
machine-inbound tier. The prefix joins the Safe-Mode block list and is kept
off the machine-inbound surface, and `body_limits` (exact path → bytes) are
enforced from `Content-Length` before the body is read. A request that can
change something (anything but GET, HEAD and OPTIONS) must also come from
DisPatch's own page: a present `Origin` must equal the `Host` and
`Sec-Fetch-Site` must not be `cross-site` or `same-site` — else 403 "cross-origin
request refused". A raw-body or multipart POST is a CORS "simple" request a page
on another port of the same host name could otherwise send with the unlocked
cookie. A router carrying any
websocket route, `Mount` or plain Starlette `Route` is refused (`ValueError` →
`failed`): those take no dependencies, so the operator check could not run on
them. Static files go through `serve_static`.

**Static files** — `serve_static(directory, entry, css, locales)` serves
`/ext/<id>/<path>`: GET/HEAD for an unlocked session and, since 2.3.4, a
locked device (it mounts the frontend too); never a machine caller; only `js mjs css json map wasm onnx wav mp3 ogg png
jpg webp woff2` (anything else — HTML, SVG and XML documents in particular —
is a 404: an extension renders into the shell's DOM and never ships a page),
`X-Content-Type-Options: nosniff`, a deny-all CSP, a strong ETag, `no-cache`
— and `private, immutable` for a year when the request's `?v=` equals the
current assets hash. Locale bundles live at `<static>/locales/<lang>.json`.

**The thread websocket** — `thread_websocket(name, handler, max_per_session=2,
max_total=3)` mounts `/ws/<name>/{thread_id}` (the name is your id or starts
with it). DisPatch runs the gate, then calls `handler(ws, sock)` with the
socket **not yet accepted**:

1. a present `Origin` must equal `Host` — else **1008** before accept;
2. the tier, decided like the main `/ws`: with a PIN, a live unlocked
   `lc_session`, else a **locked** device;
3. the thread exists and, for a locked device, its bot is `safe` **and
   listed** (`visible` — the threads a locked device's chat list shows, the
   same rule as a Safe-Mode send on the main `/ws`) — else **1008** before
   accept;
4. at most `max_total` open sockets under the name — beyond it
   accept-then-**1013** "Voice is busy on this server (3 chats open)" (a
   session that already holds one is never "busy": its oldest is replaced;
   the replacement is counted before the old socket's eviction is awaited, so
   another session's handshake meanwhile still sees the cap);
5. at most `max_per_session` per session (per `?d=` device id without a PIN):
   the oldest is closed **4001** "replaced". Its `on_replaced` callbacks get
   up to 1 s to run before the new socket reaches your handler; the close
   itself runs in the background, so a half-open phone that never answers the
   close frame does not hold up its own reconnect.

`sock` is a `ThreadSocket {thread, bot (a BotView), token, session_live(),
touch(), close_unavailable(reason), on_replaced(fn), locked}`. `locked` (2.3.4)
says the socket came from a locked device (`token` is None): every turn the
host takes for that thread — `submit_turn` while the socket is open and for
two minutes after it closed — runs with `TurnOptions(locked=True)` exactly
like a typed Safe-Mode send, and `speculate_turn` answers None there (a
speculation is built unlocked). Such a turn is also a Safe-Mode send for the
daily budget: while a locked socket is open on the thread, `submit_turn` is
charged to that device's `DECOY_TURN_QUOTA` (the same "turn" budget as the
main `/ws`) and, once it is spent — or the bot has stopped being safe or
listed meanwhile — it raises `PermissionError` before anything is persisted
(the voice add-on tells the listener it could not send). The extension never
has to pass `locked`; it reads it only to show less. `session_live()` turns
false when the session lapses, a PIN appears on a PIN-less install, the
thread is deleted, the bot is removed, or (for a locked socket) the bot stops
being `safe` or listed —
re-check it (≤ 1/s) and close 1008 when it does. `close_unavailable(reason)`
is accept-then-1013 with your reason (≤ 120 characters). A handler that returns
without accepting is answered accept-then-1013 "extension did not accept" (and
logged), never a bare 1006. A websocket to a name
**no** extension provides runs the same gate and answers accept-then-1013
"No extension provides <name>" — never a bare 1006 a client would retry.

**Thread turns** — all `async`:

- `submit_turn(thread_id, text, *, instructions=None, thinking=None,
  metadata=None, prefetched=None) -> row id | None` — persists the user row
  (media and reaction directives stripped; `None` for empty text), broadcasts
  it **before** the turn task exists and runs the turn exactly like a typed
  message. `instructions` is a `VoiceInstructions`-shaped object read by
  attribute: `.system` goes into the system slot of advisor and API bots,
  `.trailer` is appended at the **end** of what a gateway agent reads (never
  prepended). `prefetched` (a speculative run) is adopted only when the
  thread's last message is still the one its prompt was built after and no
  turn holds the thread; otherwise it is discarded and counted as
  `speculation_stale`.
- `speculate_turn(thread_id, text, *, instructions=None) -> run | None` —
  advisor and API bots only (a gateway agent runs tools; a speculative run
  must never act): starts the model call, persists and broadcasts nothing,
  refused while a turn holds the thread; a second call discards the first.
- `abort_turn(thread_id, *, keep_partial=True) -> row id | None` — cancels the
  live speculation, every queued turn and the running one. `keep_partial=True`
  (an interruption): the direct backends persist the text streamed so far as
  the assistant row with `metadata.voice_heard = {interrupted: true}` and its
  id is returned; `False` (a merge): nothing is persisted. An interruption
  also stops a turn someone typed into the same thread (the session is
  speaking its reply); a merge only ever touches the extension's own turns.
  On a gateway bot the call answers within 1.5 s: if the turn has not wound
  down by then the id is `None` — defer `note_heard` to `stream_done`.
- `retract_message(thread_id, message_id, *, only_metadata_key="voice")`,
  `replies_after(thread_id, after_id)`.
- `note_heard(thread_id, message_id, *, heard_upto, heard_text, unheard_text) -> bool`
  — merges `metadata.voice_heard {upto, interrupted, heard ≤ 160, unheard ≤ 240}`
  into an assistant row and broadcasts `message_update`; False for an unknown
  or provisional (`run:…`) id.
- `subscribe_frames(thread_id, on_frame, *, token) -> unsubscribe` — the
  thread's frames, exactly as the chat UI gets them, in order, through a
  bounded queue the host owns (256; overflow drops the oldest) — never inside
  the broadcast. A reply still streaming when you subscribe arrives first as
  `stream_start` + `stream_chunk {replace: true}` with everything so far. The
  Safe-Mode redaction applies the instant `token`'s session lapses.

| Frame | Fields you may rely on |
|---|---|
| `thinking` | `thread_id, bot_id, status: started \| stopped` |
| `stream_start` | `thread_id, bot_id, message_id` (provisional `run:…` or the row id) |
| `stream_chunk` | `thread_id, bot_id, message_id, text` (a delta) · `replace: true` (snapshot only) |
| `stream_done` | `thread_id, bot_id, message_id, message, provisional_id?` |
| `message` | `thread_id, bot_id, message` (rows that never streamed) |
| `message_update` | `thread_id, bot_id, message_id, message` |
| `error` | `thread_id, bot_id, message, detail?` |
| `progress` | `thread_id, item: {kind: tool \| text \| thinking, name?}` |

**Bots** — DisPatch stores two opaque fields for extensions in
`config.yaml bots[]`: `voice` (≤ 80) and `voice_rp` (≤ 32), returned by the
full-access `GET /api/bots/all` and accepted by `PUT /api/bots/order` (never
in the `bots` broadcast). `set_bot_field_validator(field, fn)` decides what a
save may set (`ValueError` → 422 with its message). Without a validator a save
may only keep the stored value or clear it.

**Tools** — `register_tools([row, …])`, each row read by attribute: `id`,
`feature`, `title`, `icon`, `opt_in` (off until `tools.yaml` says
`enabled: true`), `probe()` ("installed on this box"). They list in
`GET /api/tools` like the core panes; the frontend draws their tile.

**Lifecycle** — `on_startup(fn, thread=False)` (a coroutine is awaited;
`thread=True` runs it on a daemon thread, never awaited), `on_shutdown(fn)`.
A raising hook is logged, never fatal.

**Locked status** (2.3.4) — `locked_status(fn)`: what a locked device may
read about you, e.g. `{"enabled": True, "ready": True}` for voice. Called per
`GET /api/extensions` from a locked device and served in your row as
`locked_status`, cut to at most 16 flat scalars (bool, number, None, a string
≤ 80 characters); a raising `fn` is `None`. Never put a secret in it.

> **The trailer is in-band.** A gateway agent reads the voice note as part of
> the user's message — there is no separate system slot on that path — so an
> agent could, in principle, quote or narrate it. The wording is chosen to be
> a quiet standing instruction, and advisor/API bots get it in the system
> prompt instead.

---

## Host services: folders, credentials, settings

Three things an extension used to do for itself, and kept getting wrong in a
container, now come from the host. All of them are additive (no `EXT_API`
bump): feature-test with `hasattr(ctx, "secrets")`.

### The models folder

`ctx.paths.models_dir` is **`<data>/models`**, or `DISPATCH_SPEECH_MODELS` when the
operator set it. The extension picks its own subfolders (the voice add-on uses
`speech/` and `chatterbox/`). Until 3.0 the folder was `<data>/speech-models`,
which also held Chatterbox. When the voice add-on loads, before its `setup`
runs, DisPatch moves the old folder to the new name once:

| old folder | new folder | what happens |
|---|---|---|
| exists | missing, or empty | one rename |
| exists | has files | the old entries move in one by one; a name that already exists in the new folder stays where it is, and the old folder is kept if anything stayed |
| a symlink, a mount point, or not renamable | any | left alone, a warning in the log; the add-on still reads files in the old place |
| any | `DISPATCH_SPEECH_MODELS` set | never touched |

`DISPATCH_MODELS_MIGRATE=dry-run` logs what would move and moves nothing;
`DISPATCH_MODELS_MIGRATE=off` skips it. The function is
`extensions.migrate_models_dir(dry_run=True, data=None)` (a dry run by default;
start-up passes `dry_run=False`). It never overwrites and a second run finds
nothing.

### Owner-only folders

`ctx.paths.state_dir` (`<data>/ext/<id>/`) and `<data>/ext` are owner-only: mode
0700 on POSIX, an owner-only ACL on Windows (`app/fsperm.py`, the host's copy of
the add-on's helper). The data folder itself gets the same treatment only when
DisPatch is what created it; an existing one may be shared on purpose.

### Credentials: `ctx.secrets`

`ctx.secrets.get(name)`, `.set(name, value)`, `.delete(name)`, `.has(name)` and
`.backend`. A name is 1-64 letters, digits, `.`, `_`, `-`; a value is a non-empty
string. It reuses `app/agents/secrets.py`: the OS keyring when one is usable
(service `dispatch-chat-ext-<id>`), otherwise a 0600 JSON file,
`<data>/ext/<id>/secrets.json`, which is on the data volume in a container and so
survives an image update and works with a read-only root. Every stored value is
registered with the log redactor. No HTTP route returns one. The voice add-on
keeps its GPU-rig token here (`speechforge_token`) and still reads
`SPEECHFORGE_TOKEN`, then its own file, as fallbacks; `speechforge.env` is no
longer written.

### Settings: `ctx.settings_writer(namespace)`

Returns `write(key, value)` for the settings store under `namespace`: the
extension's own id, or `<id>.<sub>` (another extension's namespace raises
`ValueError`). When `app/settings_store.py` exists and offers `writer(namespace)`,
that is what you get. Until then it is a no-op that logs once, so an extension can
call it today and go live when the store lands. `extensions.is_live_writer(w)` is
False for the no-op: **do not record a one-way migration as done through it.**

At start (the lifespan, before the extensions' `on_startup` hooks) DisPatch calls
the voice add-on's `dispatch_voice.host.migration.migrate_voice_yaml(writer,
dry_run=False)` with the live writer, and only then. The add-on calls
`writer("voice.speed", 1.1)`-style keys (fully qualified, `voice.<id>`, with `bots`
as one whole value), keeps a `voice.yaml.migrated` marker so the next start is a
no-op, copies the file to `voice.yaml.bak` and leaves it in place. A raising
migration is logged and never stops the start. DisPatch Voice 1.6 still reads and
writes `voice.yaml` only, so it answers `deferred` and writes nothing until the
release that reads the store (the host logs nothing for it).

### Listing provider plugins: `GET /api/voice/providers`

Voice provider plugins (speech-to-text, text-to-speech, turn-taking) are Python
packages with an entry point in the `dispatch_voice.providers` group. With the
add-on running, its own route answers (each provider's health, capabilities and
settings, and the installed plugins with whether each is switched on). Without it
the host answers with the installed plugins only, `installed: false` and the
add-on's state, so Settings → Plugins & Apps can still show them; nothing is imported and
no `module:attr` target is shown. Both need `manage_plugins`
(`app/route_policy.py`); using a provider is `use_voice` (`/ext/*`, `/ws/voice`).

---

## Live tools from a package

`ctx.register_live_tools(specs)` adds lookups an advisor bot's model can call
itself (see "Live tools" in [advisor.md](advisor.md)) without touching
DisPatch. They join the eight built-in lookups in one registry: a bot with
`advisor.tools: true` is offered the built-ins **and** every extension tool, and a
`tools:` list may name them. A **locked (Safe-Mode) turn** gets only the eight
built-ins by default: an extension tool reaches a family device only when the
owner names it in that bot's `tools_locked` (opt-in, like everything else Safe
Mode can touch).

A complete minimal extension (a `dice` roller):

```python
# src/dice_pack/__init__.py
import random

EXTENSION = {
    "id": "dice-pack", "name": "Dice pack", "version": "0.1.0",
    "api": [1, 1], "setup": "dice_pack:setup",
}


async def roll(args: dict) -> str:
    sides = args.get("sides", 6)
    if not isinstance(sides, int) or isinstance(sides, bool) or not 2 <= sides <= 1000:
        return "sides must be a whole number from 2 to 1000"
    return f"d{sides}: {random.randint(1, sides)}"


def setup(ctx) -> None:
    ctx.register_live_tools([{
        "name": "roll_dice",
        "description": "Roll one die with the given number of sides.",
        "parameters": {
            "type": "object",
            "properties": {"sides": {"type": "integer", "description": "2-1000, default 6"}},
            "required": [],
        },
        "fn": roll,
        "summary": lambda args: f"Dice: d{args.get('sides', 6)}",   # the 🔧 notice row
    }])


# pyproject.toml
# [project.entry-points."dispatch.extensions"]
# dice-pack = "dice_pack:EXTENSION"
```

The spec is a dict (or `livetools.LiveTool(name, description, parameters, fn,
summary=None)`):

| Key | Rule |
|---|---|
| `name` | `[a-z][a-z0-9_]{1,30}`; not a built-in's name (`web_search`, `weather`, …) and not another extension's |
| `description` | 1-1000 characters; this is what the model reads to decide when to call it |
| `parameters` | a JSON-schema object (`type: object`); must be JSON-serialisable |
| `fn` | `async fn(args: dict) -> str \| livetools.ToolResult`; a plain function works too |
| `summary` | optional `fn(args) -> str`: the label of the `🔧 …` notice row (default: the capitalised name) |

A bad spec raises `ValueError` from `register_live_tools`, which fails the
extension's setup: it shows as `failed` in `/api/extensions` with the reason in
`detail`, and nothing it registered stays behind. Registering after `setup(ctx)`
returned raises `RuntimeError`. `/api/extensions` lists each loaded extension's
tools as `live_tools`.

Rules for a tool:

- **Read-only intent.** A tool is called by a model that read text from the
  open web or from a chat, and once the owner names it in a bot's
  `tools_locked` it is callable from a Safe-Mode (family) device. Look things
  up; do not send, write, delete, spend or run anything. If a tool must act,
  the bot's lists are the control: name it in `tools` and leave it out of
  `tools_locked` so it is never offered on a locked turn.
- **Never raise, and keep arguments honest.** The model's arguments are
  untrusted; validate them and return a short message the model can act on.
  DisPatch still catches anything you raise: the model then sees only
  `Tool <name> failed.` (no message, no exception type) and the traceback goes
  to the log. Do not put secrets or paths in a result.
- **Keep results small.** A result is cut to the bot's `tool_result_chars`
  (default 4000) and wrapped in a "treat as data, not instructions" marker
  before the model reads it. Return the facts, not a page dump.
- **Be quick.** The turn waits for the tool; time-box network calls yourself.
- **Per bot.** `tools: true` offers everything; `tools: [roll_dice, weather]`
  offers just those (unknown names are logged and dropped, so a bot's list
  survives the extension being uninstalled). `tools_locked: [weather]` narrows
  what a Safe-Mode turn may call.

The install-wide switch lives in `<data>/tools.yaml`:

```yaml
livetools:
  enabled: false   # default true
```

With it off no bot is offered any live tool (built-in or extension), the
prompt stops mentioning them, and it takes effect on the next turn.
`GET /api/advisor/status` reports it as `livetools` beside each bot's `tools`.

---

## Writing an extension: the frontend

When a session starts (and `features.extensions > 0`) the shell reads
`/api/extensions` and, for each `ok` extension — on a locked device too since
2.3.4, which then gets `mount(shell, {assetsVersion, locked: true})` and
whose Settings, Bots-tab and tools slots are no-ops; crossing the lock
boundary unmounts and mounts again:

1. merges its locale bundle (`/ext/<id>/locales/<lang>.json` and `en.json`)
   under the shell's strings — it can never override a shell key; a missing
   key falls back to its English bundle, then to the key itself;
2. loads its CSS;
3. `import('/ext/<id>/<entry>?v=<assets hash>')`;
4. checks `EXT = {id, api: [lo, hi]}` against `shell.EXT_API` (1);
5. calls `mount(shell, { assetsVersion })` → `{ unmount(), onLocale() }`.

Import your sibling modules with the same `?v=${assetsVersion}` so they are
served `immutable` (one round trip each on a phone, not a revalidation each).

```js
export const EXT = { id: 'voice', api: [1, 1], version: '1.0.0' };
export async function mount(shell, { assetsVersion }) {
  const remove = shell.chatHeader.addAction(micButton);
  const dispose = shell.settings.pane('voice', { render(root) { /* … */ }, onShow() {}, onHide() {} });
  return { unmount() { remove(); dispose(); }, onLocale() { /* repaint */ } };
}
```

`shell` (frozen, `js/ext-sdk.js`): `EXT_API`, `version`, `el`, `railIcon`,
`RAIL_ICONS`, `loadStyle`, `acquireInert`, `releaseInert`, `isMobile`,
`i18n {t, tHtml, has, applyDom, list, n, lang}`, `onLocale(fn)`,
`onLocked(fn)`, `onThread(fn)` (the open chat changed or closed; also
spelled `onThreadChange`), `api(path, {method, body, query,
signal})` (same-origin JSON; `DecoyError` on a Safe-Mode 403),
`activeThread()`, `bots()`, `botById(id)`, `allBots()`, `isLocked()`,
`language()`, `features()`, `can(permission)` (2026-10-19: does this session hold
`manage_plugins`, `use_voice`, …? — a Member uses voice but does not administer it;
the server decides either way), `threadFace(thread, bot)` (honours No-Image Mode),
`toast`, `confirm`, `prompt`, `navigate`, `activity()` (2.2.0: the person is
using the app — re-arms the idle auto-lock the way a touch does; call it while
your feature is in use without touches, e.g. a voice chat; it never unlocks
anything and does nothing on a locked or remembered device), and the slots:

| Slot | What it does |
|---|---|
| `chatHeader.addAction(node)` | your button in the chat header, before the thread menu → `remove()` |
| `settings.pane(id, {render, onShow, onHide, title, titleKey, glyph, public})` | `'voice'` takes over Settings → Voice (`dispose()` gives it back to the install page); any other id makes a **new Settings tab** (see [Settings panes](#settings-panes)) → `dispose()` removes it |
| `settings.open(id)` / `settings.close()` | opens Settings on a tab / closes it (asking first when it has unsaved edits) |
| `bots.rowExtra(fn(bot, onChange))` | a control on each bot's Bots-tab row (safe bots included since 2.3.1); `onChange({voice})` marks the field for the next Save |
| `tools.register([{id, feature, iconPaths, nameKey, ariaKey, open(id)}])` | the rail tile for a tool row your backend registered |
| `tools.openPane({id, title, subtitle, iconPaths, render(root), onClose})` | the shell's native full-page tool pane → `close()`; `onClose()` runs whenever the shell closes it (Back, ✕, another tool, lock) |
| `history.push(tag, onBack)` | a Back-button entry (Back calls `onBack`) → `pop()`. Entries stack: Settings opened over your overlay takes its own entry, so Back closes Settings first and your overlay only on the next Back |

Render with the shell's own classes (`.dev-group`, `.dev-row`,
`.btn-primary`, `.btn-secondary`, the tool pane's `terminal-view` chrome) so
every palette applies; keep your stylesheet to what nothing in the shell has.

#### Settings panes

`shell.settings.pane(id, spec)` has two modes:

- **`'voice'`** is pre-declared: the section always exists (it shows the install
  page until the voice add-on claims it) and keeps its place in the nav.
- **Any other id** matching `[a-z][a-z0-9-]{1,30}` (and not a section id or one of
  the old tab ids, which stay reserved as aliases: `appearance`, `account`, `voice`,
  `characters`, `connections`, `people`, `plugins`, `system`, `bots`, `theme`,
  `device`, `ai`, `agents`, `reactions`, `health`, `security`, `settings`) creates a
  tab on demand. The shell builds the nav button (`stab-<id>`) and the pane
  (`spane-<id>`) and places them in the **Add-ons** group, after the household
  sections. `dispose()` (or your `unmount()`, a lock, or a failed mount) removes
  both again.
- **`settings.open(id)`** takes a section id, an old tab id (`'theme'` opens
  Appearance, `'bots'` Characters) or `'section#anchor'` to open a subsection.

```js
const dispose = shell.settings.pane('stats', {
  titleKey: 'stats.tab',        // a key in your locale bundle (merged into the shell's strings)
  // title: 'Stats',            // …or plain text
  glyph: '📊',                  // optional, a short emoji; default is a puzzle piece
  // public: true,              // visible in Safe Mode too (default: admin-only)
  render(root) { /* build into root with the shell's classes */ },
  onShow() {}, onHide() {},
});
```

A tab is **admin-only** by default: hidden for a Safe-Mode session exactly like
Reactions or Health. `public: true` shows it to everyone, so a public pane must
never render anything an admin pane would. A malformed or built-in id, a missing
`render`, or an id another extension already holds throws. The tab strip scrolls
and the arrow keys walk it, so any number of add-in tabs fits.

The Plugins tab (Settings → Plugins & Apps) lists every discovered extension with an
install-wide on/off switch. Switching one off writes `<DATA_DIR>/extensions.yaml`
(`disabled: [id, …]`); at the next start that id is listed as `disabled` and its
code is never imported. `GET /api/plugins` (operator) returns the same list plus
the Tools rows, the built-in lookups switch and whether the voice add-on is
installed.

**Teardown.** On lock or logout the shell calls `unmount()`, then disposes
everything you registered (slots, listeners) whether or not you did, and
drops your strings. A failed import, handshake or `mount()` leaves the shell
exactly as it was and Settings → Voice saying why — in either order relative
to `settings.pane`. The service worker never caches `/ext/`.

---

## Security model

Every gate stays in DisPatch; an extension only ever receives what passed it.

- Locked devices (2.3.4): Safe Mode reads `/api/extensions` (the mount rows
  and each one's `locked_status`, nothing else) and `/ext/*` (GET/HEAD), gets
  `features.extensions` only, and mounts the frontend told it is locked.
  Every extension ROUTE refuses it (403), the voice user's reads included. Its
  websockets are open to it on a `safe` bot's thread only, and every turn from
  there runs locked. A locked socket is resolved like the HTTP gate: a device
  bound to a Restricted profile is that profile (its quiet hours and minutes end
  the call through `session_live`, its turns spend its own daily budget); only a
  device nobody bound is an anonymous guest on the per-address defaults.
- Websockets: Origin==Host, the tier and an existing thread (a `safe` bot's
  for a locked device) are checked inline before `accept` (HTTP middleware
  never sees a websocket).
- HTTP: the declared requirement on every route (`manage_plugins`, or `use_voice`
  for the voice user's reads), the Safe-Mode block list, never
  the machine-inbound surface, body ceilings before the body is read.
- Static: an asset-type allowlist, no documents, `nosniff`, a deny-all CSP.
- Data: the context exposes `BotView`s and named paths — never provider blocks,
  API keys or the data directory holding `security.yaml`.
- HTTP writes: an extension route that can change something refuses a
  cross-origin browser request (Origin ≠ Host, or `Sec-Fetch-Site` cross-site /
  same-site), so another service on the same host name cannot drive it with the
  unlocked cookie.
- Boot: a failing extension is a state with its routes rolled back; a `setup`
  that hangs is abandoned after 60 s; the shell never fails to start because of
  one. A start-up that never settles (a native crash or a hang killed by the
  service manager while extensions load or warm up) is counted in
  `<data>/ext/.booting`; after two in a row DisPatch starts **without**
  extensions, Settings → Voice says why, and the next restart after DisPatch has
  run for a minute (or stopped cleanly) tries them again.

## Versioning

`EXT_API` (Python, `app/extensions.py`) and `shell.EXT_API` (JS) are one
integer each. Additive growth — a new hook, a new optional argument, a new
shell member — never bumps them; feature-test with `hasattr(ctx, "x")` /
`'x' in shell`. Removing a hook or changing its meaning bumps them, and an
extension that declared `api: [1, 1]` is then `incompatible` (Settings says
which side to update). Release numbers are informational and never compared.
