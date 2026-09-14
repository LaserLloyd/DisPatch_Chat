// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v96';  // v96: jobs(mobile) + jobs(unmount) — dedicated #job-board-host slot (sibling of #chatview, not child), mobile Jobs tab, mount path now uses the slot so the toolbar cannot bleed into the chat panel.
const SHELL = [
  '/',
  '/static/theme.css',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/ws.js',
  '/static/js/markdown.js', '/static/js/checklist.js', '/static/js/util.js', '/static/js/theme.js',
  '/static/js/reactions.js', '/static/js/i18n.js',
  '/static/js/dashboard.js', '/static/js/privacy.js', '/static/js/llm.js',
  '/static/js/nim.js', '/static/js/about.js', '/static/js/pins.js',
  '/static/js/imagejobs.js', '/static/js/links.js', '/static/js/viewer.js',
  '/static/js/menubots.js',
  // Jobs board (added 2026-09-14). Additive modules only — must be in the
  // shell or the cold offline start loads the new sidebar entry but no
  // module, and the click fails silently with a blank panel.
  '/static/js/jobs.js', '/static/js/job-thread.js',
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
