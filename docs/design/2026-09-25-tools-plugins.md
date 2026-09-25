# DisPatch Tools — modular, out-of-repo tool panels

Date: 2026-09-25. Status: contract for implementation (backend + frontend built in
parallel against this document).

## Why

The operator wants pages that are *theirs*, not the repo's — the CrucibleForge benchmark
board, for one — reachable from DisPatch's left rail, full-page, at all times.
Today every tool (DeepSeek Harness, StudioForge panel, Emails, Clients) is
hand-wired in ~10 places and there is no way to add one without a code change.
The four builtins also cannot be switched off except through env flags.

## Shape

A **tool** is an entry in `<DATA_DIR>/tools.yaml` (never in git — the data dir is
not deployed). The backend reads the file, serves each tool's page, and exposes
the list to the unlocked UI. The rail shows tools in a **Tools** group under the
bots. Clicking one opens it **full-page**: only the left rail stays; the thread
list column and chat pane are hidden.

```yaml
# <DATA_DIR>/tools.yaml
tools:
  - id: benchmark            # [a-z0-9-]{1,40}, unique, not a bot id
    title: Benchmark Board   # rail label
    icon: 📊                 # one emoji/short glyph for the rail tile
    kind: static             # static | url | builtin
    root: /home/you/Projects/crucibleforge/results   # static: dir served read-only
    entry: report.html       # static: page inside root (default index.html)
    enabled: true            # default true
    safe: false              # default false → unlocked only
    refresh:                 # optional, static only: regenerate the page
      argv: [uv, run, crucibleforge, report]   # fixed argv, never a shell string
      cwd: /home/you/Projects/crucibleforge
      timeout_s: 600         # default 300, max 3600
  - id: studioforge-web
    title: StudioForge
    icon: 🎛️
    kind: url
    url: http://192.0.2.5:8080/    # iframe src; http(s) only
  - id: deepseek-harness     # builtin ids: deepseek-harness, studioforge-panel,
    kind: builtin            #   mail-panel, clients-panel
    enabled: false           # the ONLY field honoured for builtins besides id
```

Rules:

- **builtin** — the four existing panes keep their code and views. `tools.yaml`
  only adds an on/off switch: `enabled: false` makes `*_available()` false
  (routes 404, feature flag off, rail entry gone). An absent builtin entry means
  "as before" (env-flag/auto). Builtins are always listed by `GET /api/tools`
  (unlocked) with `enabled` reflecting the resolved value so the Settings tab can
  toggle them.
- **static** — the backend serves `root` read-only at `/tools/<id>/{path}`,
  entry at `/tools/<id>/`. Guards: `root` must be an absolute existing directory;
  every request path resolves with `strict=True` and must stay under the
  resolved root (symlink escape refused); hidden files and the deny patterns from
  `localview` (`.env`, `*.pem`, `*.key`, `security.yaml`, …) are refused; one
  uniform 404 for any refusal; max file size 64 MB; `Cache-Control: no-cache`.
  Response CSP mirrors `/local/*`: `sandbox allow-scripts allow-forms allow-popups`
  (NO `allow-same-origin` — the page can run its own sort/filter JS but cannot
  touch DisPatch cookies/DOM), `frame-ancestors 'self'`, `X-Frame-Options`
  omitted. Content-type by extension; unknown → `application/octet-stream`.
- **url** — an iframe of an external page, opened only when the browser is on
  the host or the URL is loopback-free (same rule the Harness view uses:
  loopback URLs load only when `location.hostname` is the host). Reachability is
  the page's problem; the pane shows the "not reachable" strip if the iframe
  errors (`onerror` is unreliable for iframes — use a `HEAD`/`GET` probe through
  `GET /api/tools/<id>/status` which reports `reachable` for url tools).
- **refresh** — `POST /api/tools/<id>/refresh` (operator session only) runs the
  fixed argv with `cwd`, `shell=False`, `timeout_s`, env = the service's env
  minus any `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_PIN` names. One run at a time per
  tool (409 while running). Returns `{rc, seconds, stdout_tail, stderr_tail}`
  (last 2 KB each). The page's `mtime` is reported by `status` so the UI can
  show "updated N min ago". No cron here — the rig-side cron already regenerates
  the report; the button is for "now".
- **Safe Mode** — every `/api/tools*` and `/tools/*` path is in
  `_decoy_blocked` unless the tool is `safe: true`, in which case `GET
  /tools/<id>/*` and its `status` are allowed for a Safe-Mode browser and the
  tool appears in the Safe-Mode rail. Refresh and the Settings tab are
  operator-only regardless. Builtins are never safe.
- **Write path** — `PUT /api/tools` (operator) replaces the list after
  validation and rewrites `tools.yaml` atomically (0600). Validation errors are
  422 with the offending index + field. Unknown keys are refused (schema is
  closed). A `refresh.argv` may only be set from the file, not from the UI (the
  UI sends the existing `refresh` back untouched; it cannot introduce one).

## API

```
GET  /api/tools                 → {tools:[ToolOut], path:"<DATA_DIR>/tools.yaml"}
                                  Safe-Mode caller: only safe static/url tools,
                                  no root/cwd/argv fields, no path.
GET  /api/tools/<id>/status     → {id, enabled, kind, reachable?, mtime?, refreshing,
                                  last_refresh:{at, rc, seconds}|null}
POST /api/tools/<id>/refresh    → 200 {rc, seconds, stdout_tail, stderr_tail}
                                  404 no such tool / no refresh, 409 already running,
                                  403 not operator
PUT  /api/tools                 → {tools:[...]}  (operator) → 200 {tools:[ToolOut]}
GET  /tools/<id>/               → the entry page (static)     ; url tools → 404
GET  /tools/<id>/{path}         → any file under root (static)
```

`ToolOut = {id, title, icon, kind, enabled, safe, entry?, url?, root?, has_refresh,
builtin_feature?}` where `builtin_feature ∈ {harness, studioforge, mail, practice}`
so the frontend can map a builtin to its existing opener.

`/api/auth/status.features` gains `tools: true`. The WS `hello` is unchanged.

Builtin ids and their existing openers: `deepseek-harness`→`openHarnessView`,
`studioforge-panel`→`openStudioForgeView`, `mail-panel`→`openMailView`,
`clients-panel`→`openClientsPanel`, `jobboard`→`openJobsView` (see the
addendum below — the Job Board was first left out of scope as a bot, then
moved in the same day).

## Frontend

- `js/tools.js` (new, flat file — the deploy globs are not recursive): loads
  `/api/tools` after auth, renders the **Tools** group into `#tool-list` (a new
  `<nav>` directly under `#bot-list` in the sidebar), one tile per enabled tool:
  icon + title, status dot for builtins (reuse the existing `toolsGroupState`
  colours), active state when open. Clicking a builtin calls its existing
  opener; clicking static/url opens the generic pane.
- The ⌥ `#tools-menu` popover keeps ONLY the parked-bots rows (`menubots.js`);
  its four hardcoded tool rows go away (the rail group replaces them). Keep
  `syncToolsMenuRows`' status-dot logic by pointing it at the rail tiles.
- Generic pane `#tool-view` (one, reused): header row = icon+title, "updated N
  min ago" (from `status.mtime`), ↻ Refresh button when `has_refresh` (disabled
  while `refreshing`; on completion reloads the iframe; a non-zero rc shows the
  stderr tail in a collapsed strip), "open in new tab" ↗, and ✕. Body = sandboxed
  iframe (`sandbox="allow-scripts allow-forms allow-popups"` for static; url
  tools add `allow-same-origin` because they are a different origin anyway).
- **Full-page layout**: opening ANY tool (builtin or generic) sets
  `body.tool-full`. CSS: `.app` grid collapses to `sidebar | content`; `#threads`
  column hidden; the pane fills the content column. Closing removes the class
  and restores the previous thread. Mobile: the pane is the only view; the
  bottom tab bar's back returns to the rail. The old `body.terminal-active`
  behaviour (thread-list chrome hidden, placeholder panel) is superseded — the
  builtin openers switch to `tool-full`.
- Settings → **Tools** tab (admin-only): a table of every tool (builtin rows
  first) with an enabled toggle; for static/url rows: title/icon editable,
  root/entry/url shown read-only with a "edit tools.yaml for paths and refresh"
  hint; ＋ Add tool (static or url; fields: id, title, icon, root+entry or url);
  🗑 remove (non-builtin only). Saves via `PUT /api/tools`, then re-renders the
  rail.
- Deep link: `#tool=<id>` in the URL hash opens that tool on boot (unlocked) so
  The operator can bookmark the benchmark page inside DisPatch.
- Locales: new `tools.*` namespace in all 8 locales; `nav.tools` already exists.
- sw.js: add `js/tools.js` to SHELL, bump `CACHE`, bump `?v=` on every touched
  module/CSS (main.js, index.html app.css, tools.js import).

## Tests

Backend (`backend/tests/test_tools.py`): manifest parse/validation (bad id,
missing root, relative root, unknown key, builtin with extra fields), static
serving (entry, nested path, traversal `..`, symlink escape, hidden file, deny
pattern → uniform 404), CSP header on `/tools/*`, decoy matrix rows for
`/api/tools`, `/api/tools/x/refresh`, `/tools/x/`, safe tool visible in Safe
Mode, builtin `enabled:false` → `/api/harness/status` 404 and feature off,
refresh: fixed argv runs (use `["python3","-c","..."]`), 409 on concurrent, env
scrub, timeout → rc 124. Frontend: `tools-wiring.test.js` pins (tools.js in
SHELL, `#tool-list` in index.html, `tool-full` class toggled by every opener),
i18n key tests, CSP test.

## Live setup after deploy (data dir, by hand — not in git)

```yaml
tools:
  - id: benchmark
    title: Benchmark Board
    icon: 📊
    kind: static
    root: /home/you/Projects/crucibleforge/results
    entry: report.html
    refresh:
      argv: [uv, run, crucibleforge, report]
      cwd: /home/you/Projects/crucibleforge
      timeout_s: 600
```

## Out of scope (deliberately)

Per-tool CSS/JS bundles in subfolders (deploy globs), tool-to-agent messaging,
scheduling refreshes from DisPatch, exposing builtins in Safe Mode, migrating the
Local Viewer.

## Addendum (2026-09-25, same day): tile design + the Job Board as a tool

**Tiles.** A tool tile is a bot tile (`.bot-btn` + `.bot-avatar`: 56 px, 16 px
radius, hover scale, `.active` accent ring and glow, status dot in the
trailing-bottom corner, name tip; Minimal-avatar rows and the phone's Bots-page
grid unchanged). Inside it: the theme's line art in `currentColor` on the
theme's surface (`--bg-tertiary`, `--border-strong`, `--text-secondary`; the
glyph turns `--accent-text` when active) — the gear row's recipe at bot-tile
size, so it re-colours with every palette and light/dark family. Builtins use
`RAIL_ICONS` (`terminal`, `tools` sliders, `mail`, `users`, `jobs`); a manifest
`icon` may be a `RAIL_ICONS` name or one of the chrome emoji aliased to one
(📊 → `chart`, …); anything else is drawn as typed. The hard-coded blue/amber
service tiles are gone; the pane headers' mini tiles use the same icons.

**Job Board.** Builtin id `jobboard`, feature `jobs`, title "Job Board". The
feature exists when `JOBS_ENABLED=1` mounted the `/api/jobs` router
(`_jobs_flag_on`); `tools.yaml` `enabled: false` 404s the whole router
(`_require_jobs_switch`, a router dependency) and drops `features.jobs` from
`/api/auth/status`. No PIN precondition (the board runs no code; its routes
keep their own gates, and `/api/jobs` stays in `_decoy_blocked`). Frontend:
`openJobsView()` opens `#jobs-view` (a terminal-view inside `#chatview`,
`body.tool-full`, `rememberPrev()` on entry), `jobs.js` mounts into its
`#job-board-host` and is unmounted on close; ✕ / ‹ hand the previous chat back
through `restorePrev()`. The `data-view="jobs"` screen, the hard-coded
`jobboard` rail click and the mobile Jobs tab are removed. The `jobboard` BOT
row stays in `config.yaml` with `visible: false`: the board's monthly threads
belong to it, its avatar is the board's picture, `/api/jobs/find` 404s without
it, and its `agent:` routes feedback turns to the search agent.
