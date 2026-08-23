// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v63';  // v63: markdown tables sort by header (tap/click/Enter: asc → desc → authored; numeric-aware, blanks last) and resize by dragging the header edge (pointer events, mouse+touch); table column alignment (|:-:|) and h5/h6 headings no longer dropped by the sanitizer; bubble headings scaled. v62: media directives inside code spans/fences are quoted text again — `[[media:…]]` in backticks no longer renders (fixes agent inject-echo pictures leaking into the gateway mirror thread). v61: review pass — Content-Security-Policy meta (hashed inline scripts, no unsafe-inline), sanitizer drops protocol-relative and data:text/html URIs, thread previews render plain text instead of raw markdown, host dashboard translated (dash.*), About row with the AGPL source link, boot mark inlined for No-Image Mode, lightbox is a real modal dialog. v60: DeepSeek Harness (dsh) pseudo-bot — embedded Web UI, dsh-web service control, default-model switch, headless jobs (full-session only). v59: markdown tables — fixed column collapse (display:block removed; .table-scroll wrapper + zebra). v58: avatar pools — per-bot 🖼 Pool toggle in the Bot Manager, Avatar pools panel (status + prompt banks) in the Bots pane, avatar_pool WS frame. v57: review-pass fixes — code blocks no longer crash on render (i18n `t` shadow); SW now actually controls the page (scope '/'); Safe-Mode data-full gated at source; File Server previews respect No-Image Mode; bots-broadcast repaints thread rows too; head-only avatar crops. v56: one lightbox per click (capture-phase, no double-open); thread-row avatar click shows the picture without switching threads; avatar-change broadcasts repaint headers+messages; Safe Mode is thumbnails-only everywhere; thread avatar URLs carry ?s=<pin> so re-pins bust the cache. v55: a thread wears its OWN face in the chat header and on every message; agent-sent (markdown) images open their full-res original. v54: left-rail avatars open the chat, not the lightbox. v53: every thumbnail opens its own full-res original. v52: incremental thread-row repaint. v51: thread avatar snapshots + mobile icon rail. v50: rename — app shell now says "DisPatch Chat" (title, boot splash, manifest name). v49: isMediaOnly used a message shape that does not exist. v48: applyNimChange called two functions that never existed; minimal-avatars desync. v47: NIM also hides Settings avatars, change-photo, reaction-manager thumbs and reaction trace rows. v46: lock-screen options moved below the Unlock button. v45: lock-screen labels K_Unlocked / NI_Mode. v44: No-Image Mode (js/nim.js). v43: Settings is a tab container — ⚡ Reactions / 🩺 Health / 🔌 AI models moved off the gear rail into panes
const SHELL = [
  '/',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/ws.js',
  '/static/js/markdown.js', '/static/js/util.js', '/static/js/theme.js',
  '/static/js/reactions.js', '/static/js/i18n.js',
  '/static/js/dashboard.js', '/static/js/privacy.js', '/static/js/llm.js',
  '/static/js/nim.js', '/static/js/about.js',
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

// Clicking a desktop notification (e.g. "ComfyUI is ready") focuses an existing
// DisPatch tab if one is open, otherwise opens a fresh one.
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (c.url.includes(location.origin) && 'focus' in c) return c.focus();
    }
    if (clients.openWindow) return clients.openWindow('/');
  })());
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
      .catch(() => caches.match(url.pathname))
  );
});
