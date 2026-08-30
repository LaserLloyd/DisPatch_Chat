// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v70';  // v70: agent-fired image jobs — a bot asks for a picture (POST /api/image-jobs) and a placeholder message appears at once, which the server rewrites in place into the picture or into a visible "image failed" line when the render lands; new js/imagejobs.js card and a message_update WS frame that replaces an already-rendered message. v69: review-pass fixes — harness job output renders with the same noMedia strip as every other markdown surface (a dsh answer with an image was the one place No-Image Mode still fetched one); composer attachment chips fall back to a file glyph + filename in NIM instead of a live thumbnail; privacy mode's wipe list gained 'dispatch-pinned-settings' and is now derived-and-checked by a test rather than hand-maintained; [[doc:…]] ids are percent-encoded (a crafted id built a card that downloaded a full chat export) and every api.js path parameter with it; locking empties the transcript/search/file/harness panels and their caches, and clears the API-key field, instead of only hiding the backdrop; a reaction trace no longer starts an avatar fetch for a row it discards; reference-style images count as media when deciding a row is picture-only; the socket handlers act on the socket that fired them and the reconnect timer is single-armed; the offline fallback answers 503 instead of undefined. v68: GitHub-style callouts (> [!NOTE] / [!TIP] / [!IMPORTANT] / [!WARNING] / [!CAUTION]) render as labelled panels instead of a quote whose first line reads as a typo; code blocks gain a wrap toggle for long unbroken lines and the language badge now names an auto-detected language, marked as detected rather than declared; a third screen-reader region (role=status) carries "X is responding…" so the transcript region keeps one announcement per finished reply. v67: markdown fence protection now holds for the WHOLE fence, not just its first line (CODE_SPAN_RE's end-of-fence branch was a bare `$` under /m, so quoted [[media:]]/[[doc:]] on line 2+ were expanded and parked HTML landed inside the copy button's data-code attribute); restore() refuses to inject raw HTML into an attribute or code body; an untagged fence must actually parse as JSON before it collapses behind the JSON widget (brace-shaped bash and function bodies were being hidden); a reaction chip opened by a load-race auto-expand is clickable again; No-Image Mode repaints the thread-list header avatar. v66: local ComfyUI removed — this box (AMD/ROCm) cannot run ComfyUI at all, so the gear-rail chip, the service/flags/logs panel, the workflow manager, the launch banner, the desktop-notification handler and every /api/comfy/* route are gone. ClawForge on the remote rig is the only image path and it needs no in-app control surface. v65: pinnable settings — a device setting (No-Image Mode, Privacy mode) can be pinned to the rail from its Settings row, with the tier gate applied to what is drawn: Safe Mode sees only pins marked safe, and the rail is rebuilt on every lock/unlock so a pin cannot outlive its tier. v64: ```checklist fenced tables — a leading checkbox column turns a markdown table into an interactive workout/task checklist (sortable data columns, completed rows pinned to the bottom in check order, state persisted on the message and broadcast to every device). v63: markdown tables sort by header (tap/click/Enter: asc → desc → authored; numeric-aware, blanks last) and resize by dragging the header edge (pointer events, mouse+touch); table column alignment (|:-:|) and h5/h6 headings no longer dropped by the sanitizer; bubble headings scaled. v62: media directives inside code spans/fences are quoted text again — `[[media:…]]` in backticks no longer renders (fixes agent inject-echo pictures leaking into the gateway mirror thread). v61: review pass — Content-Security-Policy meta (hashed inline scripts, no unsafe-inline), sanitizer drops protocol-relative and data:text/html URIs, thread previews render plain text instead of raw markdown, host dashboard translated (dash.*), About row with the AGPL source link, boot mark inlined for No-Image Mode, lightbox is a real modal dialog. v60: DeepSeek Harness (dsh) pseudo-bot — embedded Web UI, dsh-web service control, default-model switch, headless jobs (full-session only). v59: markdown tables — fixed column collapse (display:block removed; .table-scroll wrapper + zebra). v58: avatar pools — per-bot 🖼 Pool toggle in the Bot Manager, Avatar pools panel (status + prompt banks) in the Bots pane, avatar_pool WS frame. v57: review-pass fixes — code blocks no longer crash on render (i18n `t` shadow); SW now actually controls the page (scope '/'); Safe-Mode data-full gated at source; File Server previews respect No-Image Mode; bots-broadcast repaints thread rows too; head-only avatar crops. v56: one lightbox per click (capture-phase, no double-open); thread-row avatar click shows the picture without switching threads; avatar-change broadcasts repaint headers+messages; Safe Mode is thumbnails-only everywhere; thread avatar URLs carry ?s=<pin> so re-pins bust the cache. v55: a thread wears its OWN face in the chat header and on every message; agent-sent (markdown) images open their full-res original. v54: left-rail avatars open the chat, not the lightbox. v53: every thumbnail opens its own full-res original. v52: incremental thread-row repaint. v51: thread avatar snapshots + mobile icon rail. v50: rename — app shell now says "DisPatch Chat" (title, boot splash, manifest name). v49: isMediaOnly used a message shape that does not exist. v48: applyNimChange called two functions that never existed; minimal-avatars desync. v47: NIM also hides Settings avatars, change-photo, reaction-manager thumbs and reaction trace rows. v46: lock-screen options moved below the Unlock button. v45: lock-screen labels K_Unlocked / NI_Mode. v44: No-Image Mode (js/nim.js). v43: Settings is a tab container — ⚡ Reactions / 🩺 Health / 🔌 AI models moved off the gear rail into panes
const SHELL = [
  '/',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/ws.js',
  '/static/js/markdown.js', '/static/js/checklist.js', '/static/js/util.js', '/static/js/theme.js',
  '/static/js/reactions.js', '/static/js/i18n.js',
  '/static/js/dashboard.js', '/static/js/privacy.js', '/static/js/llm.js',
  '/static/js/nim.js', '/static/js/about.js', '/static/js/pins.js',
  '/static/js/imagejobs.js',
  // Install metadata + icons. These were missing, so a cold offline start had
  // the shell but no manifest and no icon — the PWA that is the whole reason
  // this worker exists degraded to an unnamed, iconless page.
  '/static/manifest.webmanifest',
  '/static/favicon.svg', '/static/icon-192.png', '/static/icon-512.png',
  // Every locale, not just the active one: the language picker switches without
  // a reload, so an offline user must be able to pick any of them. English is
  // load-bearing beyond its own locale — it is the per-key fallback dictionary,
  // so a missing en.json degrades EVERY language, not just en.
  '/static/locales/en.json', '/static/locales/ar.json', '/static/locales/de.json',
  '/static/locales/es.json', '/static/locales/fr.json', '/static/locales/ja.json',
  '/static/locales/pt.json', '/static/locales/zh.json',
  '/static/vendor/marked.min.js', '/static/vendor/highlight.min.js',
  '/static/vendor/purify.min.js', '/static/vendor/github-dark.min.css',
  '/static/vendor/xterm.js', '/static/vendor/xterm.css', '/static/vendor/xterm-addon-fit.js',
  '/static/vendor/xterm-addon-web-links.js', '/static/vendor/xterm-addon-unicode11.js',
  '/static/vendor/xterm-addon-search.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});


self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Cache ONLY the same-origin app shell. Everything else (API, websocket,
  // media, avatar images, cross-origin embeds) stays strictly network-only —
  // chat media must never persist in Cache Storage (Safe Mode relies on it),
  // and opaque cross-origin responses bloat the quota.
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const p = url.pathname;
  const isShell = p === '/' ||
    (p.startsWith('/static/') && !p.startsWith('/static/avatars/'));
  if (!isShell) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          // Key by pathname only (drop ?v=N): the install-time precache
          // (bare paths, see SHELL) and runtime fetches (versioned URLs) must
          // land on the SAME cache entry per logical file, or stale ?v=N
          // variants pile up under distinct keys and an offline fallback
          // match can resolve to any of them instead of the latest one.
          caches.open(CACHE).then((c) => c.put(url.pathname, copy)).catch(() => {});
        }
        return res;
      })
      // Single key per pathname above means an exact match is always the
      // freshest version fetched (or the install-time precache if none was).
      //
      // caches.match resolves to UNDEFINED on a miss, and respondWith(undefined)
      // is a TypeError — the fetch handler rejects and the browser reports a
      // network error whose cause looks like the service worker itself. That is
      // the offline path for anything not in SHELL and not yet fetched (a
      // locale added since install, a vendor file). Answer with a real, minimal
      // 503 instead: a response the caller can see the status of.
      .catch(() => caches.match(url.pathname).then((hit) => hit || new Response(
        'Offline — this file is not in the cache.',
        { status: 503, statusText: 'Offline', headers: { 'Content-Type': 'text/plain; charset=utf-8' } },
      )))
  );
});
