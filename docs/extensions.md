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

- [Voice chat (DisPatch Voice)](#voice)
- [Discovery and the handshake](#discovery-and-the-handshake)
- [Writing an extension: the backend](#writing-an-extension-the-backend)
- [Writing an extension: the frontend](#writing-an-extension-the-frontend)
- [Security model](#security-model)
- [Versioning](#versioning)

---

## Voice

Voice chat lets you talk to a thread hands-free: DisPatch listens, works out
when you have finished, sends what you said like a typed message and speaks
the bot's reply back sentence by sentence. Everything runs on the server's
CPU; nothing you say leaves the machine. It understands and speaks English
only for now. Unlocked tier only — a locked (Safe Mode) device never gets it.

**Settings → Voice** tells you which state you are in. Without the add-on it
explains what voice chat is and shows the commands below, ready to copy, with
a **Check again** button that mounts the add-on without a page reload once you
have restarted DisPatch.

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
the next restart:

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

Optional belt: `scripts/local-chat-launch` runs that line, but first checks
that the overlay resolves offline and, when it does not, starts DisPatch
**without** it (and says why in the journal) — a cold cache then costs voice
chat, not the app:

```
WorkingDirectory=/path/to/dispatch/backend
ExecStart=/path/to/dispatch/scripts/local-chat-launch \
    --with-editable /path/to/DisPatch_Voice -- \
    uvicorn app.main:app --host 0.0.0.0 --port 8765 --no-access-log --ws-max-size 262144
```

**Docker**: build the image with the add-on, then fetch the models into the
data volume (they are never baked into the image):

```bash
VOICE=1 docker compose build && docker compose up -d
docker compose exec app dispatch-voice fetch-models
```

`--build-arg VOICE_REF=v1.0.0` pins a release.

### 2. Fetch the speech models

About 1.3 GB, once (speech recognition 631 MB, the voice 688 MB, turn
detection 11 MB):

```bash
.venv/bin/dispatch-voice fetch-models
```

They land in `<data>/speech-models` (or `DISPATCH_SPEECH_MODELS`) and the
add-on's own model directory (`DISPATCH_VOICE_MODELS`).

### 3. Restart DisPatch, then turn voice chat on

Restart the service, open **Settings → Voice** and press **Check again** (or
just reload). The add-on's own settings replace the install page; turn voice
chat on there. The journal shows `ext voice: DisPatch Voice 1.x.y ok (api 1)`,
and `curl -s 127.0.0.1:8765/api/extensions` lists `voice` as `ok`.

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
| `disabled` | installed, but `DISPATCH_EXTENSIONS` leaves it out |

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
work only inside it (the context is sealed afterwards).

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
voice_rp, backend}` — never a provider block), `paths` (`ExtPaths`: your files
by name — `voice_yaml`, `acks_yaml`, `voices_dir`, `cache_dir`, `models_dir`,
and `state_dir` = `<data>/ext/<id>/`, 0700 — never the data directory itself),
`db()`, `is_operator(request)`, `require_operator(request)`,
`bot_backend(bot_id)`, `socket_count(name)`, `touch_session(token)`,
`counters` (host-side counters such as `speculation_stale`).

**HTTP** — `include_router(router, prefix, body_limits=)`: the prefix must be
`/api/<id>` or below it. Every route gets the operator dependency (a live
unlocked session, or anyone when no PIN exists — never Safe Mode, never the
machine-inbound tier), the prefix joins the Safe-Mode block list and is kept
off the machine-inbound surface, and `body_limits` (exact path → bytes) are
enforced from `Content-Length` before the body is read. A router carrying any
websocket route, `Mount` or plain Starlette `Route` is refused (`ValueError` →
`failed`): those take no dependencies, so the operator check could not run on
them. Static files go through `serve_static`.

**Static files** — `serve_static(directory, entry, css, locales)` serves
`/ext/<id>/<path>`: operator only (a locked device gets the decoy 403 and
never downloads a byte), only `js mjs css json map wasm onnx wav mp3 ogg png
jpg webp woff2` (anything else — HTML, SVG and XML documents in particular —
is a 404: an extension renders into the shell's DOM and never ships a page),
`X-Content-Type-Options: nosniff`, a deny-all CSP, a strong ETag, `no-cache`
— and `immutable` for a year when the request's `?v=` equals the current
assets hash. Locale bundles live at `<static>/locales/<lang>.json`.

**The thread websocket** — `thread_websocket(name, handler, max_per_session=2,
max_total=3)` mounts `/ws/<name>/{thread_id}` (the name is your id or starts
with it). DisPatch runs the gate, then calls `handler(ws, sock)` with the
socket **not yet accepted**:

1. a present `Origin` must equal `Host` — else **1008** before accept;
2. with a PIN, a live unlocked `lc_session` — else **1008** before accept;
3. the thread exists and its bot is not a Safe-Mode bot — else **1008** before accept;
4. at most `max_total` open sockets under the name — beyond it
   accept-then-**1013** "Voice is busy on this server (3 chats open)" (a
   session that already holds one is never "busy": its oldest is replaced);
5. at most `max_per_session` per session (per `?d=` device id without a PIN):
   the oldest is closed **4001** "replaced". Its `on_replaced` callbacks get
   up to 1 s to run before the new socket reaches your handler; the close
   itself runs in the background, so a half-open phone that never answers the
   close frame does not hold up its own reconnect.

`sock` is a `ThreadSocket {thread, bot (a BotView, never safe), token,
session_live(), touch(), close_unavailable(reason), on_replaced(fn)}`.
`session_live()` turns false when the session lapses, a PIN appears on a
PIN-less install, the thread is deleted or its bot becomes a Safe-Mode bot —
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

> **The trailer is in-band.** A gateway agent reads the voice note as part of
> the user's message — there is no separate system slot on that path — so an
> agent could, in principle, quote or narrate it. The wording is chosen to be
> a quiet standing instruction, and advisor/API bots get it in the system
> prompt instead.

---

## Writing an extension: the frontend

When an unlocked session starts (and `features.extensions > 0`) the shell
reads `/api/extensions` and, for each `ok` extension:

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
`language()`, `features()`, `threadFace(thread, bot)` (honours No-Image Mode),
`toast`, `confirm`, `prompt`, `navigate`, and the slots:

| Slot | What it does |
|---|---|
| `chatHeader.addAction(node)` | your button in the chat header, before the thread menu → `remove()` |
| `settings.pane('voice', {render, onShow, onHide})` | takes over Settings → Voice → `dispose()` gives it back to the install page |
| `settings.open(id)` / `settings.close()` | opens Settings on a tab / closes it (asking first when it has unsaved edits) |
| `bots.rowExtra(fn(bot, onChange))` | a control on each non-safe bot's Bots-tab row; `onChange({voice})` marks the field for the next Save |
| `tools.register([{id, feature, iconPaths, nameKey, ariaKey, open(id)}])` | the rail tile for a tool row your backend registered |
| `tools.openPane({id, title, subtitle, iconPaths, render(root), onClose})` | the shell's native full-page tool pane → `close()`; `onClose()` runs whenever the shell closes it (Back, ✕, another tool, lock) |
| `history.push(tag, onBack)` | a Back-button entry (Back calls `onBack`) → `pop()`. Entries stack: Settings opened over your overlay takes its own entry, so Back closes Settings first and your overlay only on the next Back |

Render with the shell's own classes (`.dev-group`, `.dev-row`,
`.btn-primary`, `.btn-secondary`, the tool pane's `terminal-view` chrome) so
every palette applies; keep your stylesheet to what nothing in the shell has.

**Teardown.** On lock or logout the shell calls `unmount()`, then disposes
everything you registered (slots, listeners) whether or not you did, and
drops your strings. A failed import, handshake or `mount()` leaves the shell
exactly as it was and Settings → Voice saying why — in either order relative
to `settings.pane`. The service worker never caches `/ext/`.

---

## Security model

Every gate stays in DisPatch; an extension only ever receives what passed it.

- Unlocked tier only: Safe Mode gets 403 on `/api/extensions`, `/ext/*` and
  every extension route, 1008 before accept on its websockets, no
  `features.extensions`, and the shell never mounts anything.
- Websockets: Origin==Host, a live session and a non-safe bot are checked
  inline before `accept` (HTTP middleware never sees a websocket).
- HTTP: the operator dependency on every route, the Safe-Mode block list, never
  the machine-inbound surface, body ceilings before the body is read.
- Static: an asset-type allowlist, no documents, `nosniff`, a deny-all CSP.
- Data: the context exposes `BotView`s and named paths — never provider blocks,
  API keys or the data directory holding `security.yaml`.
- Boot: a failing extension is a state with its routes rolled back; the shell
  never fails to start because of one.

## Versioning

`EXT_API` (Python, `app/extensions.py`) and `shell.EXT_API` (JS) are one
integer each. Additive growth — a new hook, a new optional argument, a new
shell member — never bumps them; feature-test with `hasattr(ctx, "x")` /
`'x' in shell`. Removing a hook or changing its meaning bumps them, and an
extension that declared `api: [1, 1]` is then `incompatible` (Settings says
which side to update). Release numbers are informational and never compared.
