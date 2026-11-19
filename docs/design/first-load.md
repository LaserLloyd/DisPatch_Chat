# First load (2026-11-19)

Report: "it's taking a very long time to connect on first load, and says loading for quite
some time", mostly on a phone, on a day with five deploys (each bumps the service-worker
CACHE, so the next open re-fetches the shell).

## How it was measured

* `tests/e2e/first_load.py`: a scratch instance from this checkout (Owner + 4 bots, 120
  daily threads x 6 messages, a few pictures), a throttling TCP proxy in front (latency as
  a delay line, bandwidth as one shared pipe; CDP network emulation does not reach a
  service worker's own fetches), headless Chromium with a persistent profile.
  Phone = 390x844, CPU x4, 150 ms RTT, 1.6 Mbit/s down. Desktop = loopback, no throttling.
  Scenarios: **cold** (empty profile), **warm** (reopen), **update** (the server switches to
  a tree whose CACHE and main.js `?v=` moved, as every deploy does).
  "boot" = `window.__bootDone` (the veil lifts, the bot list is usable), "hello" = the
  WebSocket's first `hello` frame (the connection dot stops saying Connecting).
* `tests/e2e/sw_upgrade.py`: previous release installed -> deploy -> the new main.js runs,
  the new worker takes over with only the new CACHE, a warm reopen loads no module from
  the network, offline start works.
* The live server, read-only GETs through `dispatch-api`.

Caveat: the proxy speaks HTTP/1.1 (6 connections); a phone on the Tailscale Serve origin
gets HTTP/2 (checked), so request COUNT costs more here than on the phone; bytes cost the
same. The box's load average was ~17 during the last runs, so desktop times move by
~±200 ms between runs; read the desktop rows for requests and bytes.

## Causes, ranked by measured cost

| # | Cause | Measured cost | Fix | Gain | Risk |
|---|---|---|---|---|---|
| 1 | `GET /api/reactions`, fetched at every app start, minted each pool picture's id by re-listing its mood folder (quadratic) inside an `async` route | live: **8.3-8.9 s** per call, 984 KB (80 KB gzip); `/api/auth/status` sent during it took **9.0 s**: the whole server, WebSocket hello included, waits behind it, for every device | one-pass mint `_ids_by_mood` (same ids, tested), one label per mood; the client fetches the pack on demand (Settings manager, first reaction event, test fire) | synthetic 2,656-picture shelf: mint 5.2 s -> 15 ms, whole listing 112 ms; no request at start | low; ids proven equal; first reaction of a session uses default overlay settings |
| 2 | The worker registered during parsing and its install fetched ~100 bare paths with `cache: 'reload'`, a second full download of the shell on the same slow link | phone cold +~0.9 MB, update **1,563 KB** per open after a deploy, worker settles 7.7-8.5 s after load | register after boot; precache each versioned file at the page's own `?v=` URL (generated `VERSIONS` table), every entry revalidated (`no-cache`, a 304) | update 1,563 -> 286 KB, phone update boot 4.9 -> 3.4 s | low; `sw_upgrade.py` passes; privacy mode re-checked at registration |
| 3 | Versioned files had no Cache-Control and the worker was network-first for them | phone warm: **112 requests** per reopen, boot 4.1 s | the worker answers a file at the version it precached from its own cache (never writes another version under that path); the server says `immutable` for the CURRENT `?v=` only (not an old one, not in the 2 minutes after a deploy wrote sw.js) | phone warm 112 -> 32 requests, boot 4.1 -> 2.1 s | low; a rollback or an old tab gets `no-cache` |
| 4 | Startup module graph: 71 modules, 1.89 MB raw / 0.62 MB gzip (main.js alone 536 KB / 168 KB) | ~3 s of a cold phone load at 1.6 Mbit/s; ~0.85 s per deploy that changes main.js | not changed (see below); budget test added | - | - |
| 5 | Render-blocking unversioned files (ui-theme folder x11, en.json, two vendor scripts) revalidate on every open | ~1 RTT on HTTP/2 | not changed | - | - |
| 6 | Serial startup chain: auth/status -> bots -> IndexedDB restore -> socket -> hello | ~4 RTT | not changed | - | - |

Tried and dropped: a generated `<link rel="modulepreload">` for the whole graph. Phone cold
boot -0.6 s, but every warm open +0.8 s first paint and 3x the long-task time (the preloads
compete with the stylesheets and compile in one burst). Warm opens are the common case.

Not done: lazy `import()` of the Settings-only modules. The ones main.js alone imports
(people, plugins, backup, dashboard, clients, push, oidc, storage card, ...) add up to
~59 KB gzip, ~0.3 s of a cold phone load and nothing on a warm one; most of them take an
`init*()` call at boot, so the change touches many call sites for a small gain.

## Before -> after (median of 3 runs, 2 for the shipped phone rows)

| Load | boot (usable) | hello | requests | KB on the wire |
|---|---|---|---|---|
| phone cold | 8.6 -> 7.3 s | 9.0 -> 7.8 s | 216 -> 218 | 2,594 -> 1,729 |
| phone warm | 4.1 -> 2.1 s | 4.5 -> 2.4 s | 112 -> 32 | 62 -> 45 |
| phone after update | 4.9 -> 3.4 s | 5.3 -> 3.7 s | 174 -> 138 | 1,563 -> 286 |
| desktop cold | 0.77 -> 0.95 s (noise) | 0.87 -> 1.1 s | 221 -> 223 | 2,638 -> 1,733 |
| desktop warm | 0.45 -> 0.49 s (noise) | 0.55 -> 0.63 s | 39 -> 38 | 47 -> 47 |
| desktop after update | 0.52 -> 0.51 s | 0.64 -> 0.67 s | 142 -> 143 | 1,557 -> 288 |

The harness has no reaction pool, so cause 1 is not in these rows. On the live install it
added ~9 s to "hello" and to the first chat list on every open (and stalled every other
device for those seconds).

Request counts on cold/update include the worker's revalidating install (304s), which now
runs after boot.

## Guards

* `frontend/tests/startup-graph.test.js`: the startup graph stays within 72 modules,
  1.95 MB raw, 640 KB gzip (raise only with a reason here); the generated `VERSIONS`
  table is current (`node frontend/scripts/startup-graph.mjs` after any `?v=` bump; the
  test fails until you do); the install revalidates and never uses `cache: 'reload'`; the
  worker registers after boot and re-checks privacy mode; no whole-graph modulepreload.
* `backend/tests/test_unversioned_static_revalidate.py`: immutable only for the current
  `?v=`; old versions and the deploy window get `no-cache`.
* `backend/tests/test_pool_identity.py`: the batch mint equals the per-file mint
  (clashes, case-folded folders); a 1,200-picture shelf mints in under a second.

## Release discipline

One more step per frontend round: after the `?v=` bumps, run
`node frontend/scripts/startup-graph.mjs` (it rewrites the table in sw.js). The node
suite fails until you do, and the deploy gate already requires the sw.js CACHE bump.

## Next gains, if wanted

1. A build step (esbuild: bundle + minify, still no framework) would take the 0.62 MB
   gzip graph to roughly 0.2-0.25 MB and one request: about -2 s on a cold slow-4G phone
   and -0.5 s per deploy. It changes the release process (the `?v=` discipline becomes a
   content hash) and is the one big lever left.
2. Brotli for text (a dependency): ~15-20% fewer bytes than gzip.
3. Start the socket in parallel with `/api/bots` (careful: restored outbox frames must be
   queued first) and versioned URLs for the ui-theme folder: ~2-3 RTT on every open.
