# Apps — trusted add-on packages

An **app** is a self-contained package that adds a whole feature to DisPatch:
a backend API, a full-page UI in the left rail's **Tools** group, an optional
panel inside a bot's chat threads, its own translated strings and, if it needs
one, its own roster bot. The shell only needs a loader; everything specific to
the feature lives in the package.

Apps are the opposite of [tools](configuration.md#tools-data_dirtoolsyaml) (`kind: static`
/ `url`), which are deliberately powerless sandboxed pages. An app's code runs
**inside the DisPatch server process** and its page runs **on DisPatch's own
origin with the operator's session**. That is why only trusted code is ever
loaded as an app (see [Where apps come from](#where-apps-come-from)).

The Job Board (`apps/jobboard/`) is the first app and the reference example.
The design contract is `docs/design/2026-09-25-apps.md`.

## Package layout

```
apps/<id>/                      # repo-shipped apps live here (deployed with DisPatch)
  app.yaml                      # the manifest (below)
  backend.py                    # def build(ctx: AppContext) -> fastapi.APIRouter
  *.py                          # optional sibling modules (imported relatively)
  static/
    index.html                  # the full-page panel (framed, same-origin, trusted)
    *.js, *.css                 # the app's own assets, served at /apps/<id>/…
    thread.js                   # OPTIONAL chat-side hook (ES module, see below)
  locales/<lang>.json           # OPTIONAL, same 8 languages as the shell, flat keys
  tests/test_*.py               # pytest (collected by `cd backend && uv run pytest`)
  tests/*.test.js               # node --test
<DATA_DIR>/apps/<id>/            # add-on apps NOT in git — same layout; loaded only
                                # when tools.yaml carries {id, kind: app, trusted: true}
```

## Where apps come from

- **Repo apps** — every `apps/<id>/app.yaml` next to `backend/` is loaded at
  startup and is **on by default**. `{id: <id>, kind: app, enabled: false}` in
  `<DATA_DIR>/tools.yaml` turns one off (its API, its page and its tile 404).
- **Data-dir apps** — a package in `<DATA_DIR>/apps/<id>/` is loaded **only**
  when `tools.yaml` has `{id: <id>, kind: app, trusted: true}`. Loading one
  runs its Python in the server, so that grant lives in the operator's own file:
  `PUT /api/tools` (the Settings → Tools tab) can switch an app on and off but
  can never introduce or change `trusted`. A data-dir package can never shadow
  a repo app of the same id.

Load order is the manifest's `order`, then `id`. A package that fails to load
— bad manifest, import error, `build()` raising or not returning a router — is
logged and skipped. **The shell never fails to boot because of an app.**
Apps are loaded once at startup; changing a backend means restarting
`local-chat.service`.

## The manifest (`app.yaml`)

```yaml
id: jobboard                    # [a-z0-9-]{1,40}; must equal the directory name
title: Job Board                # rail label (default: the id)
icon: clipboard                 # a shell line-icon name, or an emoji (≤16 chars)
safe: false                     # default false → operator only (Safe Mode never sees it)
order: 50                       # rail order among apps (default 100)
api:
  legacy_prefix: /api/jobs      # OPTIONAL: mount the router here TOO, unchanged
bot:                            # OPTIONAL: a roster bot the app needs to exist
  id: jobboard
  name: Job Board
  emoji: "🎯"
  avatar: jobboard-face.png     # a filename in the avatar dir; absent = letter block
  agent: scout                  # OpenClaw agent id the bot's threads dispatch to
  visible: false                # default false: the app tile is the front door
thread_hook: true               # static/thread.js exists; mount it in that bot's threads
env:                            # OPTIONAL: env var names the backend may read
  - SOME_SETTING
```

The schema is **closed**: an unknown key anywhere (top level, `api`, `bot`)
refuses the manifest, so a typo never silently means "default". Other rules:

- `id` must be unique across tools, bots and apps — except that an app may
  share its id with **its own** `bot.id` (the Job Board does).
- `api.legacy_prefix` must be an `/api/<segment>[/…]` path of lowercase
  segments; `/api/apps`, `/api/tools`, `/api/auth`, `/api/local`, `/api/media`
  and `/api/files` are reserved. If it collides with a route that already
  exists, the legacy mount is skipped (logged) and the app is served at
  `/api/apps/<id>` only.
- `thread_hook: true` needs a `bot:` and an existing `static/thread.js`.
- `env` is a list of up to 32 `UPPER_CASE` names; only those reach
  `ctx.env`.

## The backend: `build(ctx)`

`backend.py` is imported as the package `dispatch_app_<id>` (dashes become
underscores) whose search path is the app directory, so sibling modules import
relatively (`from . import jobs_score`). It must define:

```python
from fastapi import APIRouter, Depends, Request

router = APIRouter()          # prefix-less: the loader decides where it lives

def build(ctx) -> APIRouter:
    @router.get("/items")
    def items(request: Request):
        ctx.require_access(request)          # operator or on-box machine
        return {"items": []}

    @router.post("/settings", dependencies=[Depends(ctx.require_operator)])
    def save(body: dict):
        ...
    return router
```

The router is mounted at **`/api/apps/<id>`** and, when the manifest names one,
at **`api.legacy_prefix`** as well — the same router twice, so both prefixes
answer identically. Route paths are relative (`""`, `"/items"`).

### `AppContext`

Everything an app gets from the shell, and all of it:

| Field | What it is |
|---|---|
| `app_id` | the manifest id |
| `data_dir` | `<DATA_DIR>/apps-data/<id>/`, created `0700` on first use — the app's own files |
| `db` | the shared `Database` (the shell's SQLite handle) |
| `config` | the shell's `config` module (`config.get_bot(...)`, `config.DATA_DIR`, …) |
| `require_operator` | dependency/callable: a real operator session (or a no-PIN install); **never** the machine tier. Put it on operator-only routes. |
| `require_access` | dependency/callable: the operator **or** an authenticated on-box machine (loopback, or a remote caller with the `api_token`). What agents and crons use. |
| `broadcast(frame)` | `async`: WebSocket broadcast to operator clients. `frame["type"]` **must** start with `app:<id>:` (anything else raises `ValueError`); Safe-Mode sockets never receive it. |
| `bot_id` | the manifest's `bot.id`, or `None` |
| `env` | `{name: value}` for the manifest's `env` names that are set |
| `log` | a `logging.Logger` named `local-chat.app.<id>` |
| `broadcast_message(thread_id, bot_id, message)` | `async`: the shell's own `message` frame for a message the app stored with `ctx.db.add_message`, so open chats show it live |
| `dispatch_turn(thread_id, bot_id, text)` | start that bot's agent turn for `text` (fire-and-forget); `False` when there is nothing to dispatch to |

`broadcast_message` and `dispatch_turn` were added for the Job Board: posting
into a thread and starting a bot's turn are shell behaviours an app must not
reimplement. `require_operator` / `require_access` refuse with the same
`403 {"detail": "Unlock for full access", "decoy": true}` the rest of DisPatch
sends a locked caller.

An app should import nothing else from the shell. (The Job Board also imports
`app.database` for the monthly-thread title helpers, because its tables and the
storage methods that build those titles live in the shell's `database.py`.)

## Gating

Three layers, each sufficient on its own to keep Safe Mode out:

1. **The auth gate.** Every mounted app's API (both prefixes) is on the
   machine-inbound surface: an on-box agent reaches it without a session from
   loopback, a remote caller needs the `api_token` (401 without), and a
   browser never takes that branch. Every app path — `/api/apps/*`, `/apps/*`,
   each legacy prefix — is in `_decoy_blocked` unless the app is `safe: true`
   and the method only reads, so a locked browser gets the uniform
   `403 {"decoy": true}` before any app code runs. An app path naming no app
   is blocked outright.
2. **The router gate** the loader adds to every route: the decoy 403 for a
   Safe-Mode caller of a non-safe app, then **404 when the app is switched
   off** in `tools.yaml` (for everyone, machines included — the switch turns
   the feature off).
3. **Per route**, the app's own `ctx.require_access` / `ctx.require_operator`.

`safe: true` lets Safe Mode `GET`/`HEAD` the app's page, assets and read
routes (and lists the app in the Safe-Mode rail); mutations stay refused.

## Static serving

`/apps/<id>/` serves `static/index.html`; `/apps/<id>/{path}` serves any file
under `static/`; `/apps/<id>/locales/<lang>.json` serves `locales/<lang>.json`
(the locale files live beside `static/`, not in it). The guards are the same as
static tools: strict resolution, no `..` or symlink escape, no dotfiles, the
Local Viewer's secret-shaped name deny list, files over 64 MB refused — every
refusal the same 404. `GET` and `HEAD` only.

Headers:

- HTML: `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src
  'self'; frame-ancestors 'self'` — a **first-party** policy with **no
  `sandbox`**: the page is trusted code and needs the session cookie to call
  its own API. No inline scripts; put code in files.
- Everything: `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  same-origin`, `X-Robots-Tag: noindex`.
- Cache: assets `private, max-age=0, must-revalidate`; `index.html` (and the
  bare `/apps/<id>/`) `private, no-cache`. `?v=` cache-busting of the app's own
  assets is the app's business — bump it when a file changes (the deploy
  tool refuses a changed asset whose `?v=` did not move).

## The bot

If the manifest has `bot:`, DisPatch makes sure the roster has that id on
startup: if it is **absent**, the row is appended to `config.yaml` (through the
same serializer as every other roster write) with the manifest's fields and its
`visible` (default `false`); if it is **present**, nothing about it is changed —
the operator owns it. One log line when a row is added. `GET /api/bots` is
unchanged (a hidden bot is left out of it but resolved by id everywhere).

A bot with `agent:` dispatches its threads' turns as
`agent:<agent>:<thread_id>` — that is ordinary bot behaviour, not app-specific.

## The legacy prefix

`api.legacy_prefix` exists so an app can take over an API that external
callers already use. The Job Board keeps `/api/jobs`: the scout agent's
`dispatch-jobs` skill and the daily sweep cron call it sessionless from
loopback, and they did not have to change. Both mounts share one router, one
gate and one on/off switch.

## In the rail: `/api/tools`

`GET /api/tools` lists apps after the builtins and before static/url tools, by
`order` then id:

```json
{"id": "jobboard", "title": "Job Board", "icon": "clipboard", "kind": "app",
 "enabled": true, "safe": false, "order": 50, "has_refresh": false,
 "bot_id": "jobboard", "thread_hook": "/apps/jobboard/thread.js",
 "entry": "/apps/jobboard/", "hook_version": "3f2a…"}
```

`enabled` is the `tools.yaml` switch; `bot_id` is present only with a bot;
`thread_hook` is `null` without one; `hook_version` (only with a hook) changes
when `thread.js` does. Only apps that loaded are listed.
`GET /api/tools/<id>/status` → `{id, kind: "app", enabled, mounted}`.

`PUT /api/tools` accepts app rows of `{id, kind: "app", enabled}`. The
package-owned fields `GET` returned may be sent back unchanged (they are
dropped); changing one — or sending any other key — is a 422. `trusted` keeps
what the file holds. App rows are never removed through the API.

## Frontend: the SDK

An app page imports **only** `/static/js/app-sdk.js` from the shell:

- `api(path, {method, body, query})` → JSON. Same-origin `fetch` with
  `credentials: 'same-origin'`. A `403 {decoy: true}` throws `DecoyError` (show
  "Unlock for full access"); other 4xx/5xx throw with the server's `detail`.
  Call your own API as `/api/apps/<id>/…`.
- `t(key, vars)` / `ready()` / `has(key)`: loads
  `/apps/<id>/locales/<lang>.json` for the shell's current language (the same
  localStorage key the shell uses, then `navigator.language`, then `en`), with
  English as the fallback.
- `theme()`: applies the shell's palette and dark/light mode to
  `document.documentElement` (the same localStorage keys the shell's no-FOUC
  script reads) and links the theme stylesheets the way `index.html` does;
  listens for `storage` events so a palette change in the shell re-themes the
  app.
- `openThread(threadId)`, `close()`, `toast(text)`, `setTitle(text)`: post
  `{type: 'dispatch:<name>', …}` to the parent shell, same origin only.
- `onMessage(fn)`: messages from the shell (`dispatch:theme`, `dispatch:lang`).

The shell accepts messages only when `event.origin === location.origin` and
`event.source` is the open app frame, and handles `open-thread` (closes the
app pane and opens the thread), `close`, `toast` and `set-title`.

The app opens in the shell's tool pane, full-page, framed at `/apps/<id>/`
**without** a `sandbox` attribute (same-origin, trusted). The tile draws like a
builtin's (line icon by name, or the emoji). `#tool=<id>` deep-links to it.

### The thread hook

When a thread whose `bot_id` equals an app's `bot_id` is opened and the app has
`thread_hook`, the shell lazy-imports `/apps/<id>/thread.js` (with
`?v=<hook_version>`) and calls

```js
export async function mount({ threadEl, headerEl, thread, api, t, openThread }) {
  // … render into the thread …
  return { unmount() { /* remove what you added */ } };
}
```

and calls the returned `unmount()` when the thread is left. The module must
not import from the shell except `app-sdk.js`. (The Job Board's job-detail
panel is this hook.)

The service worker never precaches `/apps/*` (network-only).

## Tests

- Python: `apps/<id>/tests/test_*.py`. `backend/pyproject.toml` collects
  `../apps`, so `cd backend && uv run pytest` runs them with everything else.
  `apps/conftest.py` gives them the backend suite's hermetic fixtures (a
  throwaway `DATA_DIR`, the synthetic roster, the cheap KDF) and imports the
  shell, which mounts every repo app — so a test can simply
  `import dispatch_app_<id>` and call its handlers, or drive `app.main.app`
  with a `TestClient` at either prefix. Name test files uniquely across the
  whole repo (`test_<id>_*.py`): pytest imports them by basename.
- JavaScript: `apps/<id>/tests/*.test.js` (`node --test`).
- The loader itself is covered by `backend/tests/test_apps_loader.py`.

## Deploy

The deploy tool's file allowlist (and `scripts/sync_from_live.py`'s `ALLOW`,
the other way) carry
`apps/conftest.py` and, recursively, each app's `app.yaml`, `*.py`,
`static/**`, `locales/*.json` and `tests/**` — never `__pycache__`, `*.pyc` or
dot-paths. The stale-`?v=` check also covers `apps/<id>/static/*.js|*.css`
referenced from the app's own pages. A backend change needs
`systemctl --user restart local-chat.service` (never automatic); a
static-only change needs only a reload.
