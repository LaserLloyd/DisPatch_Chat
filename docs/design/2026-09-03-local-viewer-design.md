# Local Viewer — open local files, folders and sites inside DisPatch

Status: design contract for the 2026-09-03 implementation round. Every implementer
works from this file; the file:line anchors refer to the tree at commit `510be1a`.

## 1. What it is

An unlocked operator taps a link to something on the DisPatch host's disk — an
HTML page, a whole static site folder, a PDF, a markdown report, a log, an image,
a video, a directory — and it opens **inside the app**, in a full-screen
lightbox-style overlay with an ✕ in the top corner. It works from any device that
can reach DisPatch (desktop, phone, tailnet via Tailscale Serve), because the
**server** serves the bytes; the browser never needs filesystem access.

Three things make a path "clickable":

1. **Bare paths in messages** — `/var/home/user/site/index.html`, `~/reports/x.md`,
   `file:///…`, and directories like `~/Projects/foo/` — are linkified (outside
   code fences/spans, unlocked sessions only).
2. **An explicit directive** agents can write: `[[view:/abs/path|label]]` →
   renders a card (👁 icon + label) that opens the viewer.
3. **Markdown links** whose href is an absolute local path: `[the report](~/x.md)`.

Plus: custom link buttons on the gear rail can point at a local path or choose
"open in viewer" for an http(s) URL; the command palette gains "Open a local
file…"; the Settings → Device pane gains a "Local viewer" section that edits the
served roots.

## 2. Security model (read this twice)

* **Tier:** unlocked full session only. Safe Mode never renders the affordance
  (no links, no cards, no rail buttons, no palette entry — `state.decoy` gates the
  client) AND the server 403s every viewer route for a decoy request
  (`_require_full_access` first line + `/local/` and `/api/local` in
  `_decoy_blocked`). Both halves are required; neither is sufficient.
* **Roots allowlist.** The server serves only paths that resolve (symlinks
  followed, `Path.resolve(strict=True)`) to inside one of the configured roots.
  Config file `<DATA_DIR>/local-viewer.yaml`:

  ```yaml
  roots:               # absolute or ~-prefixed directories; empty = feature OFF
    - ~
  deny:                # extra always-denied prefixes/globs (merged with the built-ins)
    - ~/Documents/Backups
  show_hidden: false   # dot-components BELOW a root are refused unless true
  max_text_bytes: 2000000   # in-app text/markdown render cap; larger → download only
  ```
  Env override for containers: `DISPATCH_VIEWER_ROOTS` (os.pathsep-separated)
  seeds `roots` when the yaml is absent. Default when nothing is configured:
  `roots: []` → every request 404s with `{"detail":"Local viewer is off — add a root in Settings"}`.
* **Built-in deny (never overridable):** everything in `_INGEST_DENY_ROOTS`
  (main.py:796-808: `~/.ssh ~/.gnupg ~/.config/secrets ~/.openclaw/secrets
  ~/.openclaw/agents /etc /proc /sys /dev`) plus `~/.openclaw/gateway.systemd.env`,
  `~/.config/systemd`, `DATA_DIR/{security.yaml,trusted-devices.yaml,RECOVERY-CODE.txt,chats.db*,backups}`,
  `local-viewer.yaml` itself, and filename patterns `*.env .env* *.pem *.key *.p12 *.pfx id_* *.kdbx *.gpg *.asc known_hosts authorized_keys *.sqlite *.db`.
  A denied path 403s with the same body a decoy gets for other things
  (`{"detail":"Not served by the local viewer"}`) — do not leak *why*.
* **Hidden components:** with `show_hidden: false`, any path component starting
  with `.` **below the root** is refused. Components above the root are the
  operator's business (root `~/.agent/workspace` is legal).
* **Framing.** HTML is served with
  `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; frame-ancestors 'self'`
  — never `allow-same-origin`. The framed document gets an opaque origin: it can
  run its own scripts but it cannot read DisPatch's DOM, cookies or storage.
  **An opaque origin sends no cookie**, so behind a PIN the page's own
  `<link>/<script>/<img>` would 403 through `/local/file` (found in E2E:
  every framed site rendered bare). Framed content is therefore served through
  a **frame ticket**: the cookie-authenticated `stat` call returns
  `frame_url = /local/view/<ticket>/<name>` (or `/local/view/<ticket>/` for a
  folder with an index) — a `secrets.token_urlsafe(24)` capability, bound to
  the requesting client address, sliding 2 h expiry, table capped at 200,
  scoped to the page's **own directory** (`..`, symlinks and sibling trees are
  refused with the uniform 403; a missing file inside the scope is an honest
  404 so a broken `<img>` in a site behaves normally). The session gate lets
  `/local/view/` through and the route is its own lock. Ticket responses add
  `Access-Control-Allow-Origin: *` so `fetch()` from the framed page works for
  files in its own subtree — which is also the exfiltration bound for a hostile
  HTML file: its own folder, never the rest of the root. Refusals on this route
  are not logged (a hostile page could flood the denial log with `<img>` tags).
  Every non-HTML viewer response carries
  `default-src 'none'; sandbox; frame-ancestors 'self'`. All viewer responses:
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: private, max-age=0, must-revalidate`, `X-Robots-Tag: noindex`.
* **Clickjacking fix that rides along:** the app's own HTML (`/`,
  `/static/index.html`) now gets the header `Content-Security-Policy: frame-ancestors 'self'`
  (index.html:11-12 says that is the server's job and the server did not do it).
  A header CSP is additive to the meta CSP; only `frame-ancestors` goes in the
  header.
* **Content types:** derived from extension via a fixed table (never sniffed).
  `.html/.htm` → `text/html; charset=utf-8`; `.svg` → `image/svg+xml` (served
  under the sandbox CSP, rendered client-side as `<img>` so it never scripts);
  `.md/.txt/.log/.json/.yaml/.py/.js/.css/...` → `text/plain; charset=utf-8`
  **when fetched via the viewer for in-app rendering**, but `.css`/`.js`
  requested **as a subresource of a framed page** must carry their real types —
  so the type table is by extension and correct (`text/css`,
  `text/javascript`), and the in-app text renderer simply fetches and shows
  bytes. Unknown extension → `application/octet-stream` +
  `Content-Disposition: attachment`.
* **No listing of denied/hidden entries** in directory listings; a listing never
  reveals entries the file route would refuse.
* **Denied attempts are logged** (`warning`, path + client) and counted;
  `/api/health` gains `viewer_denied_24h` (same 24 h rolling-counter pattern as
  `reaction_fire_failures_24h`).

## 3. Backend

New module `backend/app/localview.py` (APIRouter, mounted in main.py). Keep
main.py additions to: `include_router`, the two `_decoy_blocked` prefixes, the
headers middleware branch, the health counter, and the frame-ancestors header.

Routes (all: `_require_full_access(request)` first line, comment
`# second lock, see _require_full_access`):

| Route | Behaviour |
|---|---|
| `GET /local/file/{path:path}` | Serve the file at `/{path}` (leading slash re-added; `~` expanded only when the first segment is literally `~`). Directory without trailing slash → 307 to the trailing-slash form (so relative links resolve). Directory with slash: if `index.html`/`index.htm` exists serve it, else 404 `{"detail":"no index"}` (the client renders the listing instead). Supports Range (Starlette FileResponse). |
| `GET /api/local/stat?path=` | `{ok, path (resolved, ~-shortened for display), name, kind: "file"\|"dir", size, mtime, mime, ext, viewer: "html"\|"image"\|"svg"\|"video"\|"audio"\|"pdf"\|"markdown"\|"text"\|"listing"\|"download", url: "/local/file/…", has_index (dirs), text_ok (size ≤ max_text_bytes)}`. 404 not found / 403 denied / 404 feature-off. |
| `GET /api/local/ls?path=` | Directory listing: `{path, parent (or null at a root), entries:[{name, kind, size, mtime, ext, viewer}], truncated}` sorted dirs-first then case-insensitive name; cap 2000 entries; hidden/denied entries omitted. |
| `GET /api/local/roots` | `{roots:[{path, exists}], enabled, show_hidden}` |
| `PUT /api/local/config` | body `{roots:[…], show_hidden}`; validates each root is an existing directory, not under a built-in deny, not a symlink escaping; writes `local-viewer.yaml` atomically (mkstemp + os.replace, 0600), busts cache. Returns the same shape as `roots`. Mutation → `_deny_decoy_mutation` semantics apply via `_require_full_access`. |

The `viewer` classifier is one function used by stat/ls and mirrored in the
client's icon table:
image `png jpg jpeg gif webp avif bmp ico`; svg; video `mp4 webm mov m4v ogv mkv`;
audio `mp3 m4a ogg oga wav flac aac opus`; pdf; markdown `md markdown`;
html `html htm xhtml`; text = `_TEXT_MIMES`-style set + code extensions
(`txt log csv tsv json jsonl yaml yml toml ini cfg conf xml py js mjs ts css sh bash zsh
sql rs go c h cpp hpp java kt swift rb php pl lua r m txt diff patch env-less…`);
everything else `download`.

Config cache: mtime-checked like `auth.load()`; malformed yaml → feature OFF
(fail closed) + one warning.

Tests `backend/tests/test_localview.py` (TDD — write these first, run red, then
implement): traversal (`..`, encoded `%2e%2e`, `//`), symlink escaping a root,
symlink inside a root to inside a root (allowed), hidden component below root
refused / above root allowed, each built-in deny root and pattern, roots empty →
404 with the off message, decoy → 403 on every route (add rows to
`DECOY_BLOCKED` in `test_auth_gate.py:76`), unlocked → 200, machine-inbound
X-API-Key is NOT accepted (browser-only feature: the routes are not in
`_is_inbound`), dir→307→index, dir without index → 404 no index + `ls` works,
listing omits hidden/denied, Range request 206, content-type table (html/css/js/
svg/unknown→attachment), CSP headers on html vs non-html, frame-ancestors header
on `/`, `PUT /api/local/config` validation + atomic write + reload, health
counter increments on a denial, `~` expansion only as first segment.

## 4. Frontend

### 4.1 `js/viewer.js` (new; owns the overlay)

`openViewer({ path, url, title, returnFocus })` → builds a
`div.lightbox.viewer[role=dialog][aria-modal=true]` exactly like
`openLightbox` (main.js:2065-2166) does: `lb._close = close`, backdrop is NOT a
close target here (content fills the screen), `app.inert = true`, Esc handled by
the existing global route (it clicks `.lightbox-close`), focus returned on close,
`closeAllOverlays` already calls `_close`. Also `history.pushState({viewer:1})`
on open and close on `popstate` so a phone's Back closes it (guard against
double-pop).

Layout (mobile-first):

```
┌ header (44px min, safe-area-inset-top) ─────────────────────────┐
│ ‹ back  │ 📄 icon  title/path (ellipsis, tap = copy path)  │ ⤴ ⤓ ↻ ✕ │
├ body (flex:1, overflow:auto, -webkit-overflow-scrolling:touch) ┤
│  html/pdf → <iframe sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" referrerpolicy="no-referrer">
│  image/svg → <img> (data-full NOT set — this IS the lightbox)
│  video/audio → native controls
│  markdown → renderMarkdown (markdown.js) into .viewer-doc (noLocal:false so nested paths are clickable)
│  text → <pre class="viewer-text"> (+ wrap toggle, line numbers optional)
│  listing → breadcrumb + entry list (folders first; tap = navigate in place)
│  download → card with size + ⤓ button
└─────────────────────────────────────────────────────────────────┘
```

* Flow: `openViewer` → `GET /api/local/stat` → render by `viewer` kind. Errors
  render **inside** the overlay (403 "not served", 404 "not found", feature-off
  hint with a link to Settings), never as a silent toast.
* In-viewer history stack (listing → file → back). Header ‹ is hidden at depth 0.
* ⤴ opens `url` in a new tab (`noopener`), ⤓ = `<a download>` of the raw url,
  ↻ reloads the current pane. Copy path = toast `Copied …` (reuses the existing
  toast callback).
* Close must `iframe.removeAttribute('src')` before removal (harness precedent
  main.js:4101-4102).
* NIM (No-Image Mode, `nimEnabled()` in nim.js): image/video/svg kinds render a
  placeholder (filename + size + ⤴ button) and fetch nothing; html/pdf still
  frame (the operator asked for that page explicitly — note it in docs).
* `openViewer({url})` for http(s): no stat call; iframe directly, with a
  "this site may refuse to be framed — ⤴ open in a tab" hint shown if the frame
  fails to load within 8 s (`load` never fires for blocked frames in some
  browsers; use a timer + a visible fallback bar, do not promise detection).
* Touch targets ≥ 44 px, `100dvh` with `100vh` fallback, safe-area insets on
  all four sides, header buttons use `util.js` `railIcon` line icons where a
  matching icon exists (add `viewer-open`, `viewer-download`, `viewer-reload`,
  `viewer-back` to `RAIL_ICONS` if needed — thin-stroke, currentColor, createElementNS).
* Exports: `openViewer`, `installViewerHandlers({ onToast, isDecoy })`, and
  `viewerIcon(kind)`.

### 4.2 `js/markdown.js` (linkify)

* New renderer option `noLocal` (like `noMedia`): when true, local-path
  affordances are **not** produced (plain text / plain code). main.js passes
  `noLocal: state.decoy`.
* Bare paths outside code: linkify tokens matching
  `(?:file://)?(?:~|/(?:var|home|tmp|mnt|opt|srv|media|run/media|Users|root))/[^\s<>"'`|\]\)]*` (allow
  trailing `/`; strip trailing `.,;:!?)` punctuation) into
  `<a class="markdown-file-link" data-file-path="…"><code>…</code></a>`.
  Existing `codespan` file links (markdown.js:712-723) are unchanged; a code
  span that is a rooted directory (no extension, trailing slash or ≥2 segments
  under the allowed roots list above) also becomes a file link.
* `[[view:<path>|<label>]]` directive (through `subOutsideCode`, parked HTML
  like `expandDocDirectives`) → `<div class="doc-card view-card"><span class="doc-icon">👁</span><a class="markdown-file-link doc-link" data-file-path="…">label</a></div>`.
  Unknown/other `[[x:]]` remain literal text. Note `[[media:]]` for images is
  untouched and still wins for media extensions in the auto-rewrite path.
* Markdown links `[label](/abs/path)` / `(~/x)` / `(file:///x)`: in
  `enhanceContent`, an `<a href>` whose href starts with `/var/ /home/ /tmp/
  /mnt/ /opt/ /srv/ /Users/ ~/ file://` (i.e. NOT `/api/ /media/ /static/`) is
  rewritten: href removed, `data-file-path` set, class added.
* `installMarkdownHandlers(onToast, { onOpenFile })`: the file-link branch
  (markdown.js:1103-1113) calls `onOpenFile(path, line, ev)` when provided;
  **Shift/Alt-click or a long-press (≥500 ms touch) keeps the old copy-to-clipboard**.
* Sanitizer: no new tags. `data-file-path` is already allowed.
* Tests in `frontend/tests/markdown*.test.js`: every case above incl. code-fence
  immunity, `noLocal` suppression, punctuation stripping, `[[view:]]` inside
  backticks stays literal, `/api/x` and `/media/x.png` NOT linkified, and the
  existing `thumbnails.test.js` still passes (`[[view:]]` cards set no data-full).

### 4.3 `js/links.js`

* Entry shape grows `open: 'tab' | 'viewer'`; `url` may now be an http(s) URL
  **or** an absolute local path (`/…` or `~/…`). Validation: http(s) via `new
  URL` as today; local path = starts with `/` or `~/`, no `..` segment, no
  whitespace-only. `file:`, `javascript:`, `data:` still refused.
  Re-validated on every read; legacy entries without `open` read as `'tab'`.
* Rail: a local-path link or `open:'viewer'` renders a `<button>` that calls
  `openViewer` (passed in via `renderLinkRail(container, { decoy, openViewer })`);
  http(s)+tab stays an `<a target=_blank>`.
* Editor: an "Open in viewer" checkbox on the add row; local paths force it.
* Tests `frontend/tests/links.test.js` extended.

### 4.4 `main.js` / `index.html` / `sw.js` (integration — one agent, last)

* Imports: `viewer.js?v=1`, bump `markdown.js?v=25` (every importer), `links.js?v=4`,
  `main.js?v=68`, `app.css?v=53`, SW `CACHE = 'local-chat-v80'` + changelog line,
  `viewer.js` added to `SHELL`.
* `installMarkdownHandlers(toast, { onOpenFile: (p) => !state.decoy && openViewer({path:p}) })`;
  `renderMarkdown(..., { noLocal: state.decoy })` at every render call site (grep
  for `noMedia:` — mirror it).
* Command palette (Ctrl/⌘-K, unlocked): "Open a local file…" → prompt for a path
  → `openViewer`. Recent paths (last 10) in localStorage key
  `dispatch-viewer-recent` (add to `privacy.js APP_KEYS`).
* Settings → Device: new section `#viewer-row` after the links section,
  unlocked only, built like `mountLanguagePicker` (main.js:2447-2506): roots list
  with ✕, add row (path), "Show hidden files" checkbox, status line ("Local
  viewer is off — add a root" / "N roots"), saves through `PUT /api/local/config`
  (api.js helper). Errors shown inline (`role=alert`).
* CSP meta `frame-src`: `'self' http: https:` (URL-mode viewer; document in
  security.md that user-initiated framing of arbitrary sites is by design).
  Re-run `frontend/tests/csp.test.js`; the inline script hashes are unaffected
  unless an inline script changes.
* `dom` id registry (main.js:141-160) gets any new static ids.
* File Server panel: nothing (one-way drop stays one-way).

### 4.5 i18n — the complete key list (all 8 locales, `scripts/check-locales.py` clean)

```
viewer.title            "Local viewer"
viewer.close            "Close"
viewer.back             "Back"
viewer.open_tab         "Open in a new tab"
viewer.download         "Download"
viewer.reload           "Reload"
viewer.copy_path        "Copy path"
viewer.copied           "Copied {path}"
viewer.loading          "Loading…"
viewer.folder           "Folder"
viewer.empty_folder     "This folder is empty"
viewer.items            {one:"{n} item", other:"{n} items"}
viewer.truncated        "Showing the first {n} entries"
viewer.no_index         "No index page — showing the folder"
viewer.too_large        "Too large to show here ({size}) — download it instead"
viewer.not_found        "Not found"
viewer.denied           "Not served by the local viewer"
viewer.off              "The local viewer is off — add a folder in Settings → Device"
viewer.open_settings    "Open Settings"
viewer.nim_hidden       "Hidden by No-Image Mode"
viewer.frame_hint       "If this page stays blank it refused to be framed — open it in a tab"
viewer.wrap             "Wrap lines"
viewer.settings_title   "Local viewer"
viewer.settings_hint    "Folders the app may show when a local path is opened. Secrets, dotfiles and system paths are always refused."
viewer.roots_none       "Off — add a folder to enable"
viewer.roots_count      {one:"{n} folder served", other:"{n} folders served"}
viewer.add_root         "Add folder"
viewer.root_path        "Folder path"
viewer.remove_root      "Remove"
viewer.show_hidden      "Show hidden files and folders"
viewer.save_failed      "Could not save: {error}"
viewer.bad_root         "Not an existing folder, or a folder that is always refused"
links.open_in_viewer    "Open in viewer"
links.url_or_path       "URL or local path"
links.need_url          (update text: "Enter an http(s) URL or an absolute local path")
cmdk.open_local         "Open a local file…"
cmdk.open_local_prompt  "Path on the DisPatch host"
```

## 5. Docs & agent-facing

* `docs/configuration.md` (local-viewer.yaml + env), `docs/security.md` (threat
  model above), `README.md` feature bullet, `CHANGELOG.md`, `.env.example`.
* `~/.openclaw/skills/dispatch/SKILL.md`: a "Linking a local file or page"
  section — write the bare path or `[[view:/abs/path|label]]`; it opens for the
  operator in-app; Safe-Mode devices see plain text; only paths under the served
  roots open; media still uses `[[media:]]`. Same note in
  `~/.openclaw/skills/dispatch-files/SKILL.md` (handing a report back: prefer
  `[[view:]]` for html/md/logs, `[[doc:]]` when the user should download).
* `docs/agents.md` gets the directive in its table.

## 6. Verification (not optional)

1. `cd backend && uv run pytest -q` — 0 failed.
2. `cd frontend && node --test "tests/**/*.test.js"`; `python3 scripts/check-locales.py`.
3. the staging deploy, then drive it with headless Chromium
   (Playwright, `uv run` per memory `playwright_chromium_restored_20260731`):
   set a PIN, unlock, configure a root pointing at a scratch site (index.html +
   css + js + img + a markdown + pdf + a folder + a hidden `.secret` + a symlink
   out of root), post messages containing each link form, click each → assert
   the overlay, the rendered kind, the 403/404 panes, Esc/✕/Back close, iframe
   src removed on close; repeat in Safe Mode → plain text, no overlay; repeat at
   iPhone viewport (390×844, touch) with screenshots to the scratchpad.
4. deploy dry-run → go → restart the service
   (check nobody is mid-turn first) → hard-refresh check on :8765 → write the
   live `local-viewer.yaml` with `roots: [~]`.
