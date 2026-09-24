// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v120';  // v120: the theme is now a drop-in folder,
  //   static/ui-theme/ (runtime, tokens, adapters), replacing the two loose
  //   files ui-theme.js / ui-theme.css. Served no-cache with no ?v=, so a
  //   later theme update is "replace the folder" and needs NO bump here: this
  //   worker is network-first, and the shell below is only the offline copy.
  //   This bump is for the path change itself (SHELL entries moved).
  // v119: the Job Board detail opens at once
  //   with a loading card, and a second click (or a double-click) while a job
  //   is loading replaces the pending detail instead of stacking a second
  //   overlay that outside-click and Escape could not remove.
  //   job-thread.js 7→8, main.js 95→96 (its importer), app.css 81→82.
  // v118: themes come from the shared theme
  //   package — ten themes (the six DisPatch shipped plus Electric Yellow,
  //   LaserLloyd, LaserLloyd Light and Night Red). Two NEW vendored files,
  //   ui-theme.js (the blocking runtime, first script in <head>) and
  //   ui-theme.css (tokens + the DisPatch adapter), both at ?v=20260922 and
  //   both in SHELL below — without them an offline cold start paints an
  //   unthemed page. theme.css 5→6 (palette blocks removed; DisPatch-only
  //   tokens and the night profile), app.css 80→81, dashboard.css 5→6,
  //   theme.js 15→16 (gallery from UITheme.list()), every locale (theme
  //   revision line + the "More themes" rule), index.html (runtime script,
  //   trimmed pre-paint script and its CSP hash).
  // v117: the feature wave — regenerate with
  //   alternates, edit-and-rerun, quote/reply, a per-thread model and thinking
  //   override with a context meter, the mood-driven header face, scene
  //   backdrops, drafts with a persisted outbox and offline reading, and thumbs
  //   feedback that reaches the agent. Built as five parallel groups and merged
  //   here, which is why this one bump covers them all.
  //   Cache-correctness note, the same trap v114 documents: six modules had
  //   their CONTENT changed by a later group while keeping the ?v= an earlier
  //   group had already assigned — a warm client would have loaded new main.js
  //   beside its stale copy of them. checklist 3→4, clients 3→4, job-thread
  //   6→7, jobs 6→7, privacy 7→8, reactions 16→17. Also in this wave:
  //   api 27→28, app.css 79→80, main 94→95, ws 8→9, and two new modules,
  //   modelchip.js and store.js, at v=1.
  // v116: the cold-start flash, and a resume
  //   that left the last session on screen. The flash was THIS FILE: every
  //   deploy bumps CACHE, so the first reopen of the installed PWA installed a
  //   new worker, clients.claim() fired controllerchange on an already-painted
  //   page, and index.html reloaded it -- paint, tear down, repaint. The fetch
  //   handler is network-first, so that navigation had already brought down
  //   current bytes and the reload bought nothing. It is skipped on a fresh
  //   document now and deferred to hidden otherwise. Separately: returning to
  //   a backgrounded app now covers the screen before re-checking the session,
  //   the check is time-boxed and fails CLOSED, the shared fetch wrapper has a
  //   ceiling at last, and the boot veil re-checks instead of tearing open at
  //   9s over a half-built shell. index.html + main.js?v=93→94 +
  //   app.css?v=78→79 + api.js?v=26→27.
  // v115: the Today/Older split in the thread
  //   list now shows on the PHONE. It shipped "first-cut desktop-only" and the
  //   CSS hid the headers under 769px — a scoping decision that switched the
  //   feature off for the device the list is mostly read on. Search still
  //   suppresses it, because results are ranked by relevance and date buckets
  //   would fight that order. main.js?v=92→93 + app.css?v=77→78 +
  //   thread-sections.js?v=1→2.
  // v114: the hardening round, and a
  //   cache-correctness fix that is the reason this bump matters more than
  //   most. Nine modules had their IMPORT lines rewritten by the previous
  //   round (api.js?v and util.js?v moved) without their OWN ?v= changing, so
  //   a warm client could load new main.js beside stale reactions.js and end
  //   up running two api.js/util.js instances — the split-brain assets.test.js
  //   documents. Every module that differs from the deployed copy is now
  //   bumped: api 26, checklist 3, clients 3, dashboard 7, imagejobs 3,
  //   job-thread 6, jobs 6, links 5, llm 5, main 92, markdown 31, pins 9,
  //   reactions 16, theme.js 15, util 18, viewer 3, app.css 77, theme.css 5.
  //   Behaviour in this round: the job detail modal and the Clients confirm
  //   dialog close on a drop to Safe Mode; tool panes no longer stack; the
  //   Harness session poll stops with its pane; `inert` is ref-counted so
  //   overlapping overlays cannot lift each other's focus trap; job-board
  //   failures keep the rows and a vote never re-fetches; status dots use the
  //   per-palette *-text tokens so they are visible on Paper and Daylight.
  // v113: the Clients job poll now stops when
  //   the pane closes (it rescheduled itself and kept talking to the practice
  //   box for the rest of the session, including after a drop to Safe Mode),
  //   the lock screen is a real labelled dialog that takes and traps focus
  //   through the app's ONE inert owner, the file-drop tick uses --success
  //   instead of an --ok token that has never existed, and the three
  //   admin-only settings tabs are marked as a group. main.js?v=90→91 +
  //   app.css?v=75→76 + clients.js?v=1→2 + index.html changed.
  // v112: the review's second round — the
  //   Emails and Clients panes now close on a drop to Safe Mode (they were
  //   missing from closeAllOverlays and stayed painted on a locked device),
  //   the thinking dot moved to --warning-text (the raw --warning measured
  //   1.02:1 on Paper — a yellow dot on cream), the job detail modal makes the
  //   page behind it inert so Tab cannot walk out of an aria-modal dialog, the
  //   composer's attachment-remove button and the two PIN error lines are no
  //   longer silent to a screen reader, markdown degrades instead of throwing
  //   without a DOM, and the StudioForge "insecure" state got the dot colour
  //   it was missing. main.js?v=89→90 + app.css?v=74→75 + markdown.js?v=29→30
  //   + job-thread.js?v=4→5 + all eight locales changed.
  // v111: the Job Board stopped re-fetching
  //   and rebuilding itself. Filtering and sorting were already happening
  //   client-side in render(), and the toolbar ALSO sent them to the server
  //   and refetched, then replaced the whole board — including the <select>
  //   you had just used — with a "Loading" line. The toolbar is now built once
  //   and never destroyed, filters are pure operations on the list already in
  //   memory, a superseded request cannot win (AbortController), and a
  //   job_updated frame patches one row using the job the server already sent
  //   instead of re-fetching the board on every connected tab.
  //   jobs.js?v=4→5 + job-thread.js?v=3→4 + api.js?v=24→25 + main.js?v=88→89
  //   + app.css?v=73→74 changed; api.js moved, so every module importing it
  //   moved with it.
  // v110: Android-PWA viewport contract
  //   (interactive-widget=resizes-content, so the on-screen keyboard shrinks
  //   the layout viewport instead of hiding the composer under the keys),
  //   the notification-contrast pair (--notify / --dot-idle in every palette,
  //   replacing an unread dot that was DARKER than the resting one), the
  //   markdown sub/superscript whitespace guard (`2^10 = 1024 and x^n` no
  //   longer superscripts the middle of the sentence), and the StudioForge
  //   "insecure" state that finally explains the blank pane over Tailscale.
  //   index.html + main.js?v=87→88 + util.js?v=15→16 + markdown.js?v=28→29 +
  //   app.css?v=72→73 + theme.css?v=3→4 changed, and every module that
  //   imports util.js moved with it — a warm cache holding BOTH util.js
  //   versions would run two copies of RAIL_ICONS.
  // v109: Emails tab (MailForge dashboard,
  //   embedded via its own launch URL — /api/mail/status) and Clients tab
  //   ("WebBuilder": Overview/Active/Completed/12-step detail against the
  //   practice box's client-pipeline API via /api/practice/* — new module
  //   js/clients.js, ported from practice/gui/static/app.js). Both
  //   unlocked-session only, gated the same way as Harness/StudioForge.
  //   index.html + main.js?v=86→87 + api.js?v=23→24 + app.css?v=71→72 +
  //   en.json + ja.json changed, and clients.js is new — an installed
  //   client MUST pick up a new cache or the new rail buttons 404 on their
  //   module import.
  // v108: the mark's second pass — the
  //   first one read as an ordinary fish (shallow bumps, big tail fan).
  //   Seven real spines on one contour, a small tail, and a simplified
  //   5-spine drawing for the 16px layer of the .ico, where the detailed
  //   one mushes. All five icon files plus the inline .boot-logo.
  // v107: the mark is a pufferfish drawn in
  //   the app's own icon language — white line art on a black tile, no fill
  //   and no colour, replacing the cartoon orange fish. favicon.svg,
  //   favicon.ico, favicon-32/icon-192/icon-512.png and index.html's inline
  //   .boot-logo all carry the same paths. The shell caches the icons by
  //   PATH, so without this bump an installed client keeps the old fish.
  // v106: one icon language, everywhere. The
  //   mobile tab bar, the settings tab strip, the chat header, the composer,
  //   the Bot Manager badges and the lock face were still colour emoji (the
  //   platform's own palette, not this app's) — they're line-icon <svg> now,
  //   in currentColor, like the rail. util.js gained iconLabel()+more
  //   RAIL_ICONS entries; main.js's iconifyChrome() does the swap at start
  //   and locale strings keep their emoji only as the no-JS fallback (stripped
  //   at render via glyphless()). util.js?v=14→15, main.js?v=85→86, llm.js
  //   ?v=3→4, app.css?v=70→71, index.html (all 11 util.js importers moved
  //   together). The shell is cached by PATH, so
  //   without this bump an installed client keeps painting the old emoji.
  // v105: the two big empty-state emoji (no bot picked, no chat picked) plus
  //   the "no threads yet", file-server-empty and "connect an AI" glyphs are
  //   line-icon <svg>, not colour emoji — they take the panel's own
  //   --text-secondary, so they theme like the rest of the app. util.js
  //   ?v=13→14 (all eleven importers), llm.js?v=2→3, main.js?v=84→85,
  //   app.css?v=69→70.
  // v104: the PIN screen follows the theme.
  //   The lock face was pinned to the purple palette (data-palette in
  //   index.html + color-scheme: dark in app.css); its backdrop now derives
  //   from --bg-primary so it wears the active palette like every other
  //   surface. index.html + app.css?v=68→69 + theme.css?v=2→3.
  // v103: tablet rail overflow — the rail's
  //   button group (pins, custom links, ⚙) is a scrolling column above the
  //   phone breakpoint and a WRAPPING row below it, so buttons no longer fall
  //   off the bottom of a landscape tablet or off both edges of the Bots page.
  //   app.css?v=67→v=68 + index.html. Shell is cached by PATH, so without this
  //   bump an installed tablet keeps painting the broken layout.
  // v102: Job Board detail-panel rewrite —
  //   real modal CSS (scroll fix, dark/light token colours, full-screen
  //   sheet ≤640px, corrected z-index stack), single-render job-thread.js
  //   (feedback-for-Scout, activity log, server-truth vote state), the
  //   applied-endpoint fix, thumbnail emoji fallback, and board auto-
  //   refresh after a vote. index.html + main.js + api.js + jobs.js +
  //   job-thread.js + app.css + every locale changed, so an installed
  //   client MUST pick up a new cache.
  // v101: DeepSeek Harness live sessions pane (third tab: launch several dsh
  //   runs at once, watch each one's live event stream, Stop to make it
  //   disappear). index.html + main.js + api.js + app.css + every locale
  //   changed, so an installed client MUST pick up a new cache.
  // v100: appearance is a fixed palette, not a dark/light pair. theme.css?v=1→v=2 (six [data-palette] blocks; light-dark() and the data-theme selectors are gone), app.css?v=64→v=65 (the Settings → Theme gallery), theme.js?v=13→v=14 (palette persistence + picker; the rail button now opens Settings → Theme), main.js?v=81→v=82 (theme tab + rail wiring). The shell is cached by PATH, so without this bump an installed client would keep serving itself the old theme.css and paint an unstyled page.
const SHELL = [
  '/',
  // The theme runtime, tokens and the DisPatch adapter (the drop-in folder,
  // v120). The runtime is the first script in <head> and paints the theme
  // before anything else, so an offline start without it has no theme at all.
  '/static/ui-theme/ui-theme.js', '/static/ui-theme/ui-theme.css',
  '/static/ui-theme/adapters/dispatch-compat.css',
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
  // Emails + Clients tabs (added 2026-09-19). clients.js is a pure module —
  // no side effects at import time — but it MUST be in SHELL so the cold
  // offline start resolves the import main.js now carries. The Emails pane
  // has no module of its own (its logic lives in main.js next to Harness/
  // StudioForge), so there is nothing else to add here for it.
  '/static/js/clients.js',
  // Thread list Today/Older bucketing (added 2026-09-15). Pure module —
  // no side effects at import time — but it MUST be in SHELL so the cold
  // offline start resolves the import that main.js now carries.
  '/static/js/thread-sections.js',
  // Per-thread model/thinking override chip (Feature 7, added 2026-09-20).
  // Pure module, no side effects at import time — same reasoning as
  // thread-sections.js just above: it MUST be in SHELL or a cold offline
  // start has the import main.js carries but not the file behind it.
  '/static/js/modelchip.js',
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
