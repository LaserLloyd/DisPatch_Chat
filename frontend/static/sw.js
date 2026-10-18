// Minimal service worker: network-first with a cached app-shell fallback.
// Only registers on secure contexts (https / localhost); over plain LAN http
// the app still works fully — this just enables PWA install + cold-start
// resilience where the context allows it.
const CACHE = 'local-chat-v168';  // v168: ForgeUI 1.2.0 vendored whole (forgeui.json; docs/development.md, "Themes"): SHELL gains ui-theme/ui-components.css + ui-components.js + locales/en.json + locales/ja.json and adapters/dispatch-compat.css becomes adapters/dispatch-extra.css (the app-owned overlay); index.html links the component layer and adds slate, high-contrast and high-contrast-light to the picker; theme.js?v=19->20 (revision line reads UITheme.version), app-sdk.js?v=2->3 (overlay path, contrast profile from themes.json) and every importer: main.js?v=144->145, ext-sdk.js?v=15->16, apps/jobboard board.js?v=2->3, apps/imagestudio studio.js?v=1->2.
// v167: 3.0 profiles, roles and policy - new js/profiles.js (pure helpers), js/signin.js (profile tiles, first-run setup, Restricted strip, rail chip) and js/people.js (Settings -> People & Profiles) join SHELL; api.js?v=33->34 (me/login/switchProfile/setupOwner/people/devices/..., error.code/fix/retryAfter on thrown errors, signin_required + time_limit/quiet_hours hooks) and its importers bots-edit.js, checklist.js, clients.js, plugins.js, reactions.js, tools.js; ws.js?v=9->10 (a 1008 close stops the reconnect loop); about.js?v=20->21 (APP_VERSION 3.0.0) and its importers ext-sdk.js, voice-absent.js; privacy.js?v=10->11 (APP_KEYS: dp-firstrun-dismissed) and its importer pins.js; main.js?v=143->144, app.css?v=106->107, index.html; locales role.*, signin.*, setup.*, password.*, people.*, restricted.*, notice.*, chip.*, Safe Mode -> Restricted wording (see locales/I18N-TODO.md).
// v166: 2.4.2 — Settings → Bots visibility is two switches, Show locked (safe) and Show unlocked (!hide_unlocked); neither = Hidden (dimmed row, shown on no device); the old visible switch is folded in server-side: main.js?v=142→143, app.css?v=105→106 (.bm-hidden-tag, .bm-row--hidden), about.js?v=19→20 (APP_VERSION 2.4.2) and its importers ext-sdk.js?v=14→15, voice-absent.js?v=13→14, plugins.js?v=13→14; locales settings.show_locked_*, show_unlocked_*, hidden_* (replace safe_badge/safe_title/hide_unlocked_*/show_in_sidebar), settings.show_hidden_desc.
// v165: 2.4.1 — a bot can opt out of quick actions (bot.quick_actions=false): quick-actions.js?v=1→2 (setAllowed), about.js?v=18→19 (APP_VERSION 2.4.1) and its importers ext-sdk.js?v=13→14, voice-absent.js?v=12→13, plugins.js?v=12→13, main.js?v=141→142.
// v164: 2.4.0 — bots hidden on unlocked devices: menubots.js?v=2→3 (hiddenHere, the per-device "show hidden bots" switch), privacy.js?v=9→10 (wipes that key) and its importer pins.js?v=13→14, about.js?v=17→18 (APP_VERSION 2.4.0) and its importers ext-sdk.js?v=12→13, voice-absent.js?v=11→12, plugins.js?v=11→12, main.js?v=140→141; locales settings.hide_unlocked_*, settings.show_hidden*.
// v163: 2.3.9 — Image Studio (apps/imagestudio) and a better lightbox: new js/lightbox-nav.js joins SHELL (gallery stepping, auto-hiding chrome), main.js?v=139→140, tools.js?v=14→15 (the dispatch:lightbox message), app-sdk.js?v=1→2 (lightbox()) and its importers ext-sdk.js, the Job Board's board.js/thread.js (and index.html board.js?v=1→2), app.css?v=104→105, about.js?v=16→17 (APP_VERSION 2.3.9) and its importers ext-sdk.js?v=11→12, voice-absent.js?v=10→11, plugins.js?v=10→11; locales lightbox.* and the 2.3.7 translations merged from main (ar, de, es, fr, ja, pt, zh).
// v162: 2.3.8 — Settings on a phone: compact Bots rows, the Plugins tools table as cards, wrapping server paths, a full-width Reactions describe box; tools.js?v=13→14, app.css?v=103→104, about.js?v=15→16 (APP_VERSION 2.3.8) and its importers ext-sdk.js?v=10→11, voice-absent.js?v=9→10, plugins.js?v=9→10, main.js?v=138→139; locales tools.settings_path (replaces tools.settings_hint), reactions.generate_placeholder.
// v161: merge of review 2.3.6 (personality.js?v=1→2 draft survives rebuilds, legacy bots skip the advisor default, focus returns; tabstrip.js?v=1→2 arrow click keeps tablist focus; bots-edit.js?v=2→3) onto 2.3.7; main.js?v=137→138.
// v160: 2.3.7 — integrations: new js/integrations.js joins SHELL (one-key setup, Image generation, the Works with DisPatch catalog), llm.js?v=11→12, plugins.js?v=8→9, reactions.js?v=25→26 (needs-line for an unset image tool), push.js?v=1→2 (neutral example name), about.js?v=14→15 (APP_VERSION 2.3.7) and its importers ext-sdk.js?v=9→10, voice-absent.js?v=8→9, main.js?v=136→137, app.css?v=102→103; locales integrations.*, settings.pics_*, settings.pool_needs_cli, reactions.pool_needs_cli.
// v159: 2.3.6 — Settings → Bots → Personality: new js/personality.js joins SHELL (preset picker + system-prompt editor), bots-edit.js?v=1→2, llm.js?v=10→11 (the Connect field reads the bot's persona), app.css?v=101→102, about.js?v=13→14 and its importers ext-sdk.js?v=8→9, voice-absent.js?v=7→8, plugins.js?v=7→8, main.js?v=135→136; locales persona.*.
// v158: 2.3.5 — the Settings tab strip shows and reaches every tab: new js/tabstrip.js joins SHELL (edge buttons, wheel), app.css?v=100→101, about.js?v=12→13 and its importers ext-sdk.js?v=7→8, voice-absent.js?v=6→7, plugins.js?v=6→7, main.js?v=134→135, index.html, locales (settings.tabs_prev, settings.tabs_next).
// v157: 2.3.4 — voice on locked devices: extensions.js?v=2→3 (a locked device mounts add-ons, told it is locked), about.js?v=11→12 and its importers ext-sdk.js?v=6→7, voice-absent.js?v=5→6, plugins.js?v=5→6, main.js?v=133→134.
// v156: 2.3.3 — about.js?v=10→11 and its importers ext-sdk.js?v=5→6, voice-absent.js?v=4→5, plugins.js?v=4→5, main.js?v=132→133 (the reaction pool's daily run never mints into a full shelf; locales reactions.pool_daily_add_hint).
// v155: 2.3.2 — about.js?v=9→10 and its importers ext-sdk.js?v=4→5, voice-absent.js?v=3→4, plugins.js?v=3→4, main.js?v=131→132 (review fixes).
// v154: 2.3.1 review — llm.js?v=9→10 (Test connection sends target + api_key_env so a connected bot's saved key is used), plugins.js?v=2→3 (restart hint follows the server's rule for failed/incompatible add-ons), main.js?v=130→131 (settings.pane guard before the tabs exist).
// v153: 2.3.1 — about.js?v=8→9 and its importers ext-sdk.js?v=3→4, voice-absent.js?v=2→3, plugins.js?v=1→2, main.js?v=129→130 (version 2.3.1; extension websockets accept a safe bot's thread, voice for Safe-Mode bots).
// v152: 2.3.0 — quick actions (js/quick-actions.js), Settings → Plugins (js/plugins.js) and Bots-tab editing (js/bots-edit.js) join SHELL; the Tools tab folds into Plugins; add-in tabs; main.js?v=128→129, api.js?v=32→33, extensions.js?v=1→2, ext-sdk.js?v=2→3, voice-absent.js?v=1→2, llm.js?v=8→9, tools.js?v=12→13, app.css?v=99→100, index.html, locales (quick.*, plugins.*, bots_edit.*).
// v151: Web Push — js/push.js joins SHELL (Settings → Device → Notifications), this worker gains push + notificationclick (ping only: a title, never message text); main.js?v=127→128, index.html, locales (push.*).
// v150: password fields wrapped in <form data-no-submit> (Chromium form warning), advisor bots hide the thinking row; main.js?v=126→127, index.html.
// v149: 2.2.0 final review — js/voice-heard.js joins SHELL (the chat marks what a voice-chat listener did not hear); ext-sdk.js?v=1→2 (shell.activity), main.js?v=125→126, app.css?v=98→99, index.html, locales (msg.not_heard, msg.voice_cut).
// v148: 2.2.0 — voice chat is an add-on. drive.js, voice-worklet.js, voice.css, drive-settings.js, voices.js, voice-tools.js leave SHELL; js/extensions.js, js/ext-sdk.js, js/voice-absent.js join it; /ext/ is network-only; i18n.js?v=3→4 (mergeBundle) and every importer's ?v, tools.js?v=11→12, about.js?v=6→7 (APP_VERSION 2.2.0), main.js, app.css, index.html, locales.
// v147: voice is optional (Settings → Voice on/off switch) and its advanced pieces are opt-in tools; new js/voice-tools.js joins SHELL; drive-settings.js?v=3→4, voices.js?v=3→4, drive.js?v=5→6, voice.css?v=3→4, tools.js?v=10→11, about.js?v=5→6 (VOICE_REPO_URL), main.js?v=123→124, app.css?v=96→97, index.html, locales.
// v146: Drive settings move to their own Settings → Voice tab; drive-settings.js?v=2→3, voices.js?v=2→3, drive.js?v=4→5, voice.css?v=2→3, main.js?v=122→123, app.css?v=95→96, index.html, locales.
// v145: neutral example names in drive-settings.js + locales; drive-settings.js?v=1→2, voices.js?v=1→2, drive.js?v=3→4, main.js?v=121→122, index.html.
// v144: Drive mode v2 — drive.js?v=2→3, voice-worklet.js?v=1→2, voice.css?v=1→2,
// new js/drive-settings.js + js/voices.js join SHELL; main.js?v=120→121, app.css?v=94→95, index.html, locales.
// v143: 2.1.0 — checklist/clients/reactions/tools ?v bumps (api.js v32 import). v142: 2.1.0 RC review — drive.js
// reconnect gives up after repeated 1013s (drive.js?v=1→2), Drive-mode
// strings localised; main.js?v=118→119, index.html, locales.
// v141: Drive mode — drive.js,
// voice-worklet.js and voice.css join SHELL; main.js?v=117→118, index.html.
// v140: release 2.1.0 — about.js shows
// APP_VERSION 2.1.0. about.js?v=4→5, main.js?v=116→117, index.html.
// v139: release 2.0.0 — about.js shows
// APP_VERSION 2.0.0. about.js?v=3→4, main.js?v=114→115, index.html.
// v138: public-repo scrub — notice.js
// flags any sender metadata block carrying failure:true (no sender named),
// about.js points at the DisPatch_Chat repo. notice.js?v=3→4,
// about.js?v=2→3, main.js?v=113→114, index.html.
// v137: review round — tool fallback
// card buttons / menu ↗ / Settings "New window" switch get their CSS, dead
// locale keys (settings.language_hint, settings.minimal_avatars_hint,
// pins.theme_hint) removed from all eight locales, an unused variable in
// main.js. index.html + main.js?v=112→113 + app.css?v=92→93 + locales.
// v136: settings modal fixed size, Device tab
// cards, pin pills, tools open:window + fallback card, clients site links.
// index.html + main.js?v=111→112 + app.css?v=91→92 + api.js?v=30→31 +
// tools.js?v=8→9 + clients.js?v=8→9 + pins.js?v=12→13 + checklist/reactions
// (import bump only). v135: tools are add-ons on the phone —
  // the Bots page shows ONE quiet "Tools" row under the roster (no tile grid)
  // that opens the tools + parked bots as a bottom sheet; hardware Back and
  // every builtin ‹ close a full-page tool; off the host the Harness /
  // StudioForge / Emails / url tools use their optional remote address or say
  // "host only" instead of framing a loopback URL. tools.js 7→8, main.js
  // 110→111, app.css 90→91, index.html, locales (mail.*).
  // v134: per-tool layout state bridge —
  // a framed static/url/app tool may post {type:'dispatch:tool-state',
  // op:'get'|'set'} and the shell keeps its state in this browser's
  // localStorage (a sandboxed static page cannot); privacy mode wipes that
  // key family (privacy.js APP_KEY_PREFIXES). tools.js 6→7, privacy.js 8→9
  // (so pins.js 11→12), main.js 109→110, index.html.
  // v133: apps review round — an app's
  // thread hint is keyed by app AND thread (app B's hook can no longer eat
  // app A's hint), and in Safe Mode an app's open-thread is ignored unless
  // the thread's bot is in the session's (safe) roster. main.js 108→109,
  // index.html.
  // v132: the desktop rail's Tools group
  // is ONE button with a popup menu (a row per tool, then the parked bots
  // under a rule); the phone Bots page keeps the tile grid. The old ⌥
  // parked-bots button is gone — its #tools-menu is now that popup.
  // tools.js 5→6, menubots.js 1→2 (comments), main.js 107→108, app.css
  // 89→90, index.html, every locale (tools.menu_aria).
  // v131: apps — trusted add-on packages
  // under apps/<id>/ (docs/design/2026-09-25-apps.md). New module
  // js/app-sdk.js (in SHELL: main.js imports it); the Job Board moved out of
  // the shell into apps/jobboard/ (js/jobs.js and js/job-thread.js are gone
  // from SHELL, its CSS left app.css, its strings left every locale), and
  // /apps/* is network-only here. api.js 29→30 (jobs calls out, thread(id)
  // in) and so every importer moved: checklist 7→8, clients 7→8,
  // reactions 20→21, tools 4→5; main.js 106→107, app.css 88→89, index.html
  // (the Job Board pane is gone, #app-thread-host is new), all eight locales.
  // v130: tool tiles are the bot tile's
  // shape on the theme's surface with line icons (util.js 19→20 gained
  // terminal/mail/users/chart, so every util.js importer moved: viewer 4→5,
  // theme 17→18, reactions 19→20, pins 10→11, markdown 32→33, checklist 6→7,
  // llm 6→7, links 6→7, job-thread 10→11, jobs 9→10, imagejobs 4→5,
  // dashboard 8→9, clients 6→7), and the Job Board is a builtin tool
  // (tools.js 3→4, main.js 105→106, app.css 87→88, index.html: the board is a
  // tool pane, the mobile Jobs tab is gone; all eight locales).
  // v129: a builtin tool's Settings toggle
  // saves the manifest switch, not the resolved state (tools.js 2→3).
  // v128: Tools review round — tools.js
  //   1→2: a tool frame is unloaded via about:blank (a bare removeAttribute
  //   kept the old document running), and entry into ANY tool — builtin
  //   panes included, via rememberPrev() — records the chat to return to, so
  //   ✕ on a generic tool opened over a builtin lands on the last real chat.
  //   main.js 103→104, index.html.
  // v127: metadata.notice is the one
  //   collapse convention for machine events — an explicit notice renders as
  //   a one-line notice even when the server also set `sub` (notice.js 2→3),
  //   a plain sub row shows its own first line instead of "Working…", an
  //   info-level dot. main.js 102→103, app.css 86→87, index.html.
  // v126: Tools — new module js/tools.js (in
  //   SHELL): a Tools group in the rail (#tool-list), a generic full-page tool
  //   pane (#tool-view), Settings → Tools, `body.tool-full` on every tool
  //   opener; the ⌥ menu keeps only parked bots. main.js 101→102, api.js
  //   28→29 (and every importer: checklist 5→6, reactions 18→19, clients
  //   5→6, jobs 8→9, job-thread 9→10), app.css 85→86, index.html, every locale.
  // v125: the per-bot scene backdrop tint is
  //   off (owner request: "just use standard") — the message pane uses the theme's own
  //   background. main.js 100→101, app.css 84→85.
  // v124: (2026-09-24 review, round 2) the header mood
  //   face survives the thread_update repaint, an offline boot reboots into
  //   the real tier on the first hello, quote excerpts render as plain text,
  //   Copy copies the alternate on screen, an injected post needs a failure
  //   word to collapse (notice.js 1→2), main.js 99→100.
  // v123: (2026-09-24 review) js/store.js was the one
  //   module missing from SHELL, so an offline cold start whose HTTP cache had
  //   lost it got the worker's 503 and the boot veil never came down — the
  //   exact case the offline-reading feature exists for. Also main.js 98→99,
  //   store.js, modelchip.js, index.html, theme.css, locales.
  // v122: machine-posted alerts (failed runs,
  //   watchdogs, missed pictures, failed image jobs) collapse to one muted
  //   line — new module js/notice.js (in SHELL), main.js 97→98, app.css 83→84,
  //   all eight locales (msg.notice).
  // v121: calmer chat. Each message shows only
  //   a Copy icon and a ⋯ menu on its timestamp line; Reply, thumbs, Regenerate,
  //   Edit, Copy to composer, Hide from context and Delete moved into the menu.
  //   "Model & thinking" moved into the header ⋯ (the chip only shows when it
  //   has something to say; the model pill is hidden on phones). Settings →
  //   Bots "Change photo" is an icon. main.js 96→97, app.css 82→83,
  //   util.js 18→19, and every module whose import line that changed moved
  //   one ?v= too (clients, dashboard, imagejobs, job-thread, jobs, links,
  //   llm, markdown, checklist, pins, reactions, theme, viewer), index.html
  //   + all eight locales.
  // v120: the theme is now a drop-in folder,
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
  '/static/ui-theme/adapters/dispatch-extra.css',
  // ForgeUI 1.2.0 component layer (v168): index.html links the sheet and
  // defers the script; the string tables are fetched on demand by
  // UIComponents.loadLocale(), so an offline switch of language still works.
  '/static/ui-theme/ui-components.css', '/static/ui-theme/ui-components.js',
  '/static/ui-theme/locales/en.json', '/static/ui-theme/locales/ja.json',
  '/static/theme.css',
  '/static/app.css',
  '/static/dashboard.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/ws.js',
  '/static/js/markdown.js', '/static/js/checklist.js', '/static/js/util.js', '/static/js/theme.js',
  '/static/js/reactions.js', '/static/js/i18n.js',
  '/static/js/dashboard.js', '/static/js/privacy.js', '/static/js/llm.js',
  '/static/js/nim.js', '/static/js/about.js', '/static/js/pins.js',
  '/static/js/imagejobs.js', '/static/js/advisor.js', '/static/js/links.js', '/static/js/viewer.js',
  '/static/js/menubots.js', '/static/js/integrations.js',
  // The app SDK (2026-09-25): statically imported by main.js, so a cold
  // offline start needs it. The apps themselves (/apps/*) are never cached.
  '/static/js/app-sdk.js',
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
  // System-notice classifier (2026-09-24): pure module, imported by main.js.
  '/static/js/notice.js',
  // Drafts / outbox / offline reading (IndexedDB). Statically imported by
  // main.js — tests/shell-modules.test.js now asserts every js/*.js is here.
  '/static/js/store.js',
  // Tools rail group + generic tool pane + Settings → Tools (2026-09-25).
  // Pure module, statically imported by main.js.
  '/static/js/tools.js',
  // Extensions (2.2.0): the loader, the SDK shell object and the page the
  // Voice tab shows while the voice add-on is absent, all statically imported
  // by main.js. An add-on (/ext/<id>/…) is NEVER cached here: see the fetch
  // handler. (Voice chat itself left the shell in 2.2.0.)
  '/static/js/extensions.js', '/static/js/ext-sdk.js', '/static/js/voice-absent.js',
  '/static/js/plugins.js', '/static/js/bots-edit.js', '/static/js/personality.js',
  // What a voice-chat listener did not hear of a reply (2.2.0): pure module,
  // statically imported by the message renderer in main.js.
  '/static/js/voice-heard.js',
  // Web Push settings row (2026-10-12): pure module, statically imported by
  // main.js.
  '/static/js/push.js', '/static/js/quick-actions.js',
  // 2.3.5: the edge buttons + wheel of the Settings tab strip (main.js imports it).
  '/static/js/tabstrip.js', '/static/js/lightbox-nav.js',
  // 3.0 profiles, roles and policy: pure helpers, the sign-in / setup / Restricted
  // DOM and Settings -> People & Profiles, all statically imported by main.js.
  '/static/js/profiles.js', '/static/js/signin.js', '/static/js/people.js',
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
  // Apps (/apps/<id>/…) are network-only, always: an app is served with its
  // own caching rules and its data is gated per session, and a precached
  // copy would outlive an app that was switched off in tools.yaml.
  // Extensions (/ext/<id>/…) likewise: an add-on's code is operator-only,
  // content-hashed (`?v=`) by the server and must never outlive its install
  // — or reach a device after it locked — through a cached copy.
  if (p.startsWith('/apps/') || p.startsWith('/ext/')) return;
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


// ---------------------------------------------------------------------------
// Web Push (2026-10-12). The server sends a PING — {kind, thread_id, title},
// e.g. "Nova replied" — and never message text, so a lock screen shows that
// something arrived and nothing of what it says. Opening the app decides what
// this device may see, by the usual session rules.
// ---------------------------------------------------------------------------
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {}; }
  const title = (typeof d.title === 'string' && d.title.trim()) ? d.title.slice(0, 80) : 'DisPatch';
  const thread = typeof d.thread_id === 'string' ? d.thread_id.slice(0, 200) : '';
  e.waitUntil((async () => {
    // The app is open and in front: it is already showing the reply.
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (wins.some((c) => c.focused && c.visibilityState === 'visible')) return;
    await self.registration.showNotification(title, {
      // One notification per thread: a newer ping replaces the older one.
      tag: thread ? `thread:${thread}` : 'dispatch',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { thread_id: thread },
    });
  })());
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const thread = (e.notification.data && e.notification.data.thread_id) || '';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins.find((c) => new URL(c.url).origin === location.origin);
    if (win) {
      await win.focus();
      if (thread) win.postMessage({ type: 'dispatch:open-thread', thread_id: thread });
      return;
    }
    await self.clients.openWindow(thread ? `/?thread=${encodeURIComponent(thread)}` : '/');
  })());
});
