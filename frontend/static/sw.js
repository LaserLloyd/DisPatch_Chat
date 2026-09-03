// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v81';  // v81: StudioForge control panel — an unlocked operator gets a rail button beside the DeepSeek Harness that embeds the LLM rig's own web panel in DisPatch (read-only: DisPatch frames it, links to it and reports whether it answered, and manages nothing — the rig is a different machine). Off by default and off until DISPATCH_STUDIOFORGE=1 and DISPATCH_STUDIOFORGE_URL name an address; no address is shipped. Full-session only, twice over: no rail button in Safe Mode and /api/studioforge 403s a decoy session. Deliberately NOT a reverse proxy — framed, the viewing device must itself be able to reach the rig; proxied, every LAN and remote client would inherit admin on a panel that has no password. v80: local viewer — an unlocked operator opens a file, folder or page on the DisPatch host inside the app: bare paths and [[view:/abs/path|label]] in messages become clickable, the command palette gains "Open a local file…" (with the last eight paths), a custom link button can point at a local path, and Settings → Device edits the folders the server may serve. HTML and PDF frame under a no-same-origin sandbox, images/video play, markdown and text render in-app, a directory lists. Unlocked only, twice over: the client renders no affordance in Safe Mode and every /local/ route 403s a decoy session; the server serves only paths resolving inside a configured root and never secrets, dotfiles or system paths. New js/viewer.js. v79: live reply streaming — a reply now paints token-by-token as the gateway emits it (stream_start/stream_chunk/stream_done ride the OpenClaw chat deltas instead of the after-the-fact re-chunking) and the row swaps to the persisted message on stream_done; a status line under the composer names the turn phase (preparing context → loading model → writing → using <tool> → finishing) from turn_status frames; a ⏹ Stop button beside Send aborts the in-flight run (unlocked only — the server 403s the abort in Safe Mode); the sent message paints optimistically and is reconciled when the server row lands. Backend: every accepted run is registered in an in-flight table and settled by agent.wait, so a reply whose delta stream was cut still lands as a message instead of vanishing; /api/health carries inflight_runs / dropped_local / tick_closes. v78: the Health pane grows a "Live transport" card (gateway socket state, live vs backfilled replies, unrepaired truncation, messageSeq gaps, turns over the socket vs the CLI, rig reachability + 24 h image-job failures — the counters the socket turn transport exposes on /api/health, so "which path did that reply take" is answerable from the app); the command palette (Ctrl/⌘-K) offers Settings, Health dashboard and File Server when unlocked, and "Send a file" in Safe Mode, so every rail surface is reachable from the keyboard; the thread ⋯ menu wears one glyph per row (📌 ✏️ 🗄️ 🗑️ beside the existing ⤓ 📜) instead of two iconed rows and four bare ones; the Bots pane says "no prompt bank yet — add prompts to enable refills" rather than "refills generate nothing". Backend (same round): the first-run "Connect an AI" hero keys off the gateway socket as well as the CLI, so a socket-only host no longer greets an unlocked admin with an onboarding banner. v77: a picture-only newest message previews as its caption (or a 🖼️ glyph) in the thread list instead of "No messages yet" — every agent-fired image job ends in exactly that row, so the list contradicted the chat under it; and the list repaints when an image job's placeholder is rewritten (thread_update now follows message_update), so it no longer sits on "Generating an image…" after the picture landed. v76: one icon language for the whole rail — the built-in buttons (theme, lock, unlock, file server, drop, gear) join the pins and links on the shared thin-stroke line-icon set (util.js railIcon/RAIL_ICONS; theme.js repaints moon/sun from it), so no full-colour emoji sits next to monochrome line art; and the DeepSeek Harness model picker grew up: the catalog now carries every provider route in ~/.dsh/settings.yaml (DeepSeek, MiniMax, an 11-model StudioForge roster, local LM Studio), the StudioForge group is starred with a ★ docs link to the laserlloyd.com article beside the select, and plain-http providers are probed (2s, 10s cache) for live state so a resident model shows ● (+ctx in the tooltip) and a mid-load one ◌ — picking a cold model stays legitimate, the first request just pays the load. v75: rail icons are thin-stroke SVG line art in currentColor instead of emoji (No-Image Mode = picture frame cut by a slash, Privacy = struck-through eye, Minimal avatars = silhouette, and a custom link with no chosen emoji wears an external-link arrow) — emoji rendered in the platform's colour set and clashed with the rail's monochrome chrome; the icons inherit button colour so theme + .pin-on accent apply for free. Built with createElementNS, no innerHTML. v74: Minimal avatars joins the pinnable settings (📌 on its Settings → Device row; forced-by-NIM renders as a disabled "No-Image Mode controls this" button, and the rail repaints on every NIM flip so that state can't go stale) and the No-Image Mode pin glyph is 🙈 instead of 🚫, which read as an error rather than "hide the pictures"; Minimal avatars pins as 👤. The avatar-style setter moved into nim.js (minimalAvatarsEnabled/setMinimalAvatars) so the checkbox, the pin and NIM's borrow of data-avatar-style share one owner. v73: custom link buttons — Settings → Device grows a "Custom link buttons" editor (glyph · label · URL) and each saved link becomes an <a> on the gear rail next to the pinned settings, opening in a new tab; unlocked sessions only (rail AND editor — the URLs people put here are internal panels Safe Mode must not advertise), per-device localStorage ('dispatch-custom-links', on the privacy wipe list), http(s) URLs only, re-validated on every read. New js/links.js. v72: image jobs follow the rig's new completion callback, priority bands, progress and cancel_job — a pending card now shows the render's percentage, and a withdrawn render says so ('the image was cancelled on the rig') instead of reading as a failure; js/imagejobs.js accepts the new 'cancelled' status. v71: relicensed AGPL-3.0 -> MIT — the About row in Settings -> Device now reads "Free software under the MIT licence." in all eight languages, and the locale files carry no ?v= of their own, so only this CACHE name reaches a warm-cached device. v70: agent-fired image jobs — a bot asks for a picture (POST /api/image-jobs) and a placeholder message appears at once, which the server rewrites in place into the picture or into a visible "image failed" line when the render lands; new js/imagejobs.js card and a message_update WS frame that replaces an already-rendered message. v69: review-pass fixes — harness job output renders with the same noMedia strip as every other markdown surface (a dsh answer with an image was the one place No-Image Mode still fetched one); composer attachment chips fall back to a file glyph + filename in NIM instead of a live thumbnail; privacy mode's wipe list gained 'dispatch-pinned-settings' and is now derived-and-checked by a test rather than hand-maintained; [[doc:…]] ids are percent-encoded (a crafted id built a card that downloaded a full chat export) and every api.js path parameter with it; locking empties the transcript/search/file/harness panels and their caches, and clears the API-key field, instead of only hiding the backdrop; a reaction trace no longer starts an avatar fetch for a row it discards; reference-style images count as media when deciding a row is picture-only; the socket handlers act on the socket that fired them and the reconnect timer is single-armed; the offline fallback answers 503 instead of undefined. v68: GitHub-style callouts (> [!NOTE] / [!TIP] / [!IMPORTANT] / [!WARNING] / [!CAUTION]) render as labelled panels instead of a quote whose first line reads as a typo; code blocks gain a wrap toggle for long unbroken lines and the language badge now names an auto-detected language, marked as detected rather than declared; a third screen-reader region (role=status) carries "X is responding…" so the transcript region keeps one announcement per finished reply. v67: markdown fence protection now holds for the WHOLE fence, not just its first line (CODE_SPAN_RE's end-of-fence branch was a bare `$` under /m, so quoted [[media:]]/[[doc:]] on line 2+ were expanded and parked HTML landed inside the copy button's data-code attribute); restore() refuses to inject raw HTML into an attribute or code body; an untagged fence must actually parse as JSON before it collapses behind the JSON widget (brace-shaped bash and function bodies were being hidden); a reaction chip opened by a load-race auto-expand is clickable again; No-Image Mode repaints the thread-list header avatar. v66: local ComfyUI removed — this box (AMD/ROCm) cannot run ComfyUI at all, so the gear-rail chip, the service/flags/logs panel, the workflow manager, the launch banner, the desktop-notification handler and every /api/comfy/* route are gone. ClawForge on the remote rig is the only image path and it needs no in-app control surface. v65: pinnable settings — a device setting (No-Image Mode, Privacy mode) can be pinned to the rail from its Settings row, with the tier gate applied to what is drawn: Safe Mode sees only pins marked safe, and the rail is rebuilt on every lock/unlock so a pin cannot outlive its tier. v64: ```checklist fenced tables — a leading checkbox column turns a markdown table into an interactive workout/task checklist (sortable data columns, completed rows pinned to the bottom in check order, state persisted on the message and broadcast to every device). v63: markdown tables sort by header (tap/click/Enter: asc → desc → authored; numeric-aware, blanks last) and resize by dragging the header edge (pointer events, mouse+touch); table column alignment (|:-:|) and h5/h6 headings no longer dropped by the sanitizer; bubble headings scaled. v62: media directives inside code spans/fences are quoted text again — `[[media:…]]` in backticks no longer renders (fixes agent inject-echo pictures leaking into the gateway mirror thread). v61: review pass — Content-Security-Policy meta (hashed inline scripts, no unsafe-inline), sanitizer drops protocol-relative and data:text/html URIs, thread previews render plain text instead of raw markdown, host dashboard translated (dash.*), About row with the source link, boot mark inlined for No-Image Mode, lightbox is a real modal dialog. v60: DeepSeek Harness (dsh) pseudo-bot — embedded Web UI, dsh-web service control, default-model switch, headless jobs (full-session only). v59: markdown tables — fixed column collapse (display:block removed; .table-scroll wrapper + zebra). v58: avatar pools — per-bot 🖼 Pool toggle in the Bot Manager, Avatar pools panel (status + prompt banks) in the Bots pane, avatar_pool WS frame. v57: review-pass fixes — code blocks no longer crash on render (i18n `t` shadow); SW now actually controls the page (scope '/'); Safe-Mode data-full gated at source; File Server previews respect No-Image Mode; bots-broadcast repaints thread rows too; head-only avatar crops. v56: one lightbox per click (capture-phase, no double-open); thread-row avatar click shows the picture without switching threads; avatar-change broadcasts repaint headers+messages; Safe Mode is thumbnails-only everywhere; thread avatar URLs carry ?s=<pin> so re-pins bust the cache. v55: a thread wears its OWN face in the chat header and on every message; agent-sent (markdown) images open their full-res original. v54: left-rail avatars open the chat, not the lightbox. v53: every thumbnail opens its own full-res original. v52: incremental thread-row repaint. v51: thread avatar snapshots + mobile icon rail. v50: rename — app shell now says "DisPatch Chat" (title, boot splash, manifest name). v49: isMediaOnly used a message shape that does not exist. v48: applyNimChange called two functions that never existed; minimal-avatars desync. v47: NIM also hides Settings avatars, change-photo, reaction-manager thumbs and reaction trace rows. v46: lock-screen options moved below the Unlock button. v45: lock-screen labels K_Unlocked / NI_Mode. v44: No-Image Mode (js/nim.js). v43: Settings is a tab container — ⚡ Reactions / 🩺 Health / 🔌 AI models moved off the gear rail into panes
const SHELL = [
  '/',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/ws.js',
  '/static/js/markdown.js', '/static/js/checklist.js', '/static/js/util.js', '/static/js/theme.js',
  '/static/js/reactions.js', '/static/js/i18n.js',
  '/static/js/dashboard.js', '/static/js/privacy.js', '/static/js/llm.js',
  '/static/js/nim.js', '/static/js/about.js', '/static/js/pins.js',
  '/static/js/imagejobs.js', '/static/js/links.js', '/static/js/viewer.js',
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
