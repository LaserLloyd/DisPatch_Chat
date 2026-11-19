# Lock, resume and "keep this device unlocked" (4.4 dev, 2026-11-19)

Owner's report: DisPatch signed him out while he was using it (mostly on his phone). He
wants his own phone to open straight into his session every time unless HE locks it, and,
when a device IS locked, a split screen: the main profile on top, an anonymous "Resume
previous session" with a PIN pad below.

## 1. Why he was signed out (evidence)

| # | Cause | Evidence | Fix |
|---|---|---|---|
| a | A restart drops every session that is not a remembered device. `auth._sessions` is memory only. | Today's only `sessions` row (Owner, Android, `persistent=0`) was created 12:26:46 JST; `local-chat` restarted 12:37:43; the row is still in `chats.db` (a lock or an expiry deletes it, a restart does not). No remembered device was touched today (`trusted-devices.yaml` unchanged since 11-17 22:37). All his recent phone sign-ins (11-13, 11-16, 11-17, 11-19) were non-remembered. | §3.1 sessions survive a restart. |
| b | Activity inside a tool window is invisible to the idle timer: `bumpIdle` listens on the top window only; an iframe's keys and pointer never reach it. The Owner's idle window is `lock_timeout_seconds: 600`. | Code (`main.js` idle wiring; `tools.js` has no activity message). Reproduced on a scratch instance. | §3.3 frame activity counts. |
| c | A phone that wakes with no network yet fails the "coming back" check, and the failure path called `lockToTarget`, which POSTs `/api/auth/lock` = **revoke the session and forget the remembered device**. Same for a check that lands while the server restarts. | `main.js` visibilitychange `.catch(() => handleLocked())`; `auth.revoke` drops trust. | §3.2 a failed check covers and retries; only a server answer locks. |
| d | A remembered device's cookie is set once with `Max-Age = 30 days` and never refreshed, while the server-side trust slides with use: regular use was cut off on day 30. | `people_api.session_response` sets max_age at issue only. | §3.4 the cookie slides on every app open. |
| f | Two tabs (or a tab plus a tool window it could not see): each tab ran its own idle timer and POSTed a lock when ITS timer ran out, even while the other tab was in use. | `lockToTarget` had no server check; `bumpIdle` is per tab. | §3.5 the server refuses an idle lock while its own idle clock says the session is in use. |
| e | Service-worker update reload / window-restore code. | Reload happens only when the tab is hidden and keeps the cookie; window code touches no session state. | Ruled out. A reload lands on the chat list (unchanged; exact place restore is not wanted). |

## 2. The model

A session has three independent properties:

* **remembered** (`persistent`, existing): survives closing the browser, 30-day sliding trust.
* **keep unlocked** (new, per session = per device): exempt from the idle auto-lock.
* **held** (new): the session is LOCKED. It identifies nobody on any route
  (`auth.get_session` returns None, so the device is the lock-target default profile or the
  sign-in screen). It can be resumed with that profile's own credential for
  `HOLD_SECONDS = 12 h` from the moment it locked, then it is dropped (with its remembered
  trust) and the profile signs in normally.

`keep unlocked` defaults: ON for a remembered device of the Owner, Admins and Users (that is
today's behaviour: a remembered device never idle-locks); OFF and unavailable for Basic
(the restricted tier): its limits, quiet hours and budgets need the idle lock. A non-remembered
session is never keep-unlocked. Turning it ON in Settings → Device on a non-remembered
session makes the session remembered too (it needs `remember_device_days > 0`).
`remember_device_days: 0` (the existing kill switch) turns both off install-wide.
Per-role idle timeouts (`session_timeout_*`, `lock_timeout_seconds`) still decide the idle
window of every session that is not keep-unlocked.

Manual **Lock** always works: it holds the session (keep-unlocked or not). The idle lock (tab
timer or the server's own idle clock) also holds it. Signing in as anyone on that device (or
picking a profile) drops the previous held session of that device; the most recently locked
session is the only resumable one (one session cookie per browser).

## 3. Part 1 fixes

1. **Restart.** The `sessions` table (token SHA-256 only) gains `epoch`, `locked_at`,
   `keep_unlocked`; `last_seen` is written at most once a minute on use. An unknown cookie
   after a restart is re-minted from its row when the profile exists, is enabled, its epoch
   matches, and the row is within its idle window (wall clock) or hold window. A credential
   or role change bumps the epoch, so no revoked session can come back.
2. **Wake.** The visibility check covers the screen first (unchanged), then: server says
   signed in → uncover; server says not → lock screen; no answer → stay covered and retry
   (2 s, 4 s, 8 s ... 30 s) and on `online`; never POST `/api/auth/lock` for a failed check.
3. **Frames.** `app-sdk.js` posts `dispatch:activity` (throttled 5 s) on key, pointer, wheel
   and input inside an app frame; the shell accepts it only from the frame of a live window
   of an app or static tool (origin + source checked) and calls `bumpIdle`. A url tool
   (someone else's site) counts only focus moving into its frame and the pointer entering it.
4. **Cookie.** `GET /api/auth/status` from a remembered session re-sets the cookie's
   `Max-Age` (same attributes), so use keeps it alive.
5. **Idle lock asks first.** The tab's idle timer POSTs `/api/auth/lock {reason: "idle"}`;
   the server answers `{ok: false, active: true}` unless its own idle clock (touched by
   every request and socket frame from any tab) is within 90 s of the timeout, and the tab
   re-arms. A manual Lock is never refused; a lock request that fails falls through to
   the local lock (fail closed).
6. Frame activity as built: `tools.js` attaches capture listeners to same-origin app
   frames directly (no `app-sdk.js` change) and counts only trusted events (a script's
   `dispatchEvent` is not use); every tool frame also counts focus moving into it and the
   pointer entering it. Static and url frames are not trusted to report their own
   activity (the security review: a timer posting "activity" would hold off the idle
   lock forever, Basic's included), so ten minutes of typing in a STATIC tool without
   touching the pointer still idles (known limit; the built-in tools are apps).

## 4. Part 2: the lock screen

Shown ONLY when `GET /api/auth/status` says `resumable: true` for this browser: its cookie
names a held session that is not expired and not throttled. A browser with no held session
gets the normal sign-in, unchanged.

```
┌──────────────────────────────┐
│   (main profile picture)     │  TOP: the lock-target default profile, its picture and
│        DisPatch              │  name: the only identity on screen. One tap = main.
│        [ Go to main ]        │  profile_list install: "Choose a profile" (tiles).
├──────────────────────────────┤
│  Resume previous session     │  BOTTOM: no name, no picture, no hint of length.
│        • • • •  (masked)     │  One neutral field: pad digits AND typed text
│   1 2 3 / 4 5 6 / 7 8 9      │  (a password goes in the same field; "ABC" switches
│   ABC  0  ⌫                  │  the phone keyboard to text). Enter / Resume submits.
│        [ Resume ]            │
└──────────────────────────────┘
```

* Desktop: two halves side by side above 900 px wide, stacked on a phone (top ~40 %).
  Keys ≥ 56 px (min 44), ThemeForge tokens, the existing `.keypad` look, light/dark,
  RTL mirrors the row order only, reduced motion drops transitions.
* Screen reader order: main button, then the resume field, pad, Resume. The field is
  labelled "PIN or password for the previous session"; nothing names the profile.
* **Right credential** → `POST /api/auth/resume` un-holds the same session; the page
  rebuilds as a fresh open of that profile (chat list), with NO sign-in notice cards.
* **Wrong credential** → the server counts it in its OWN throttle bucket (subject
  `resume`, per device and address; the profile's login bucket is not touched, so tile
  failures cannot change whether a resume is offered: that would be an oracle for whose
  it is) and answers a neutral 401, the same body as "nothing held"; the page goes to
  main. After 5 wrong resumes the hold is dropped (that profile signs in normally). A
  throttled or dropped hold is reported `resumable: false`: the bottom half is simply not
  offered (the screen says nothing about why).
* **Constant work**: every resume attempt runs one PBKDF2 (install PIN, dummy salt when
  none) and two argon2id verifications (dummy hashes where the profile has no PIN or no
  password), whether or not anything is held, so timing says nothing either.
* **Back from main**: while `resumable`, the "Switch profile"/sign-in action on the main
  profile shows this split screen again (its top is "Go to main", plus "Other profiles"
  for a normal sign-in). Other people get in by the normal sign-in tiles from there.
* **PIN-less previous profile**: its held session is never offered (`resumable: false`;
  showing it would let anyone in and the pad would leak "no PIN"); that person picks their
  tile from the normal sign-in. If the previous session WAS the main profile there is no
  held session at all (main is not a signed-in session).
* **Owner / password profiles**: same field, typed. The server tries the profile's PIN
  (install PIN for the Owner) and then its password; one failure counted per attempt.
* Lock during a picture or a voice call: the picture job keeps running server-side and
  lands in its chat; the voice socket ends (its session no longer resolves); the page is
  torn down like today.

### What the locked screen must never contain
No name, picture, initials, colour, theme or tool/pill/chat text of the held profile in
the DOM, titles, aria, storage or network responses. The page tears the held profile's
app down completely before the lock screen paints (`reboot()` into the main profile,
windows closed, local cache tier wiped). `/api/auth/status` adds one boolean,
`resumable`; `/api/auth/resume` answers `{ok}` or a neutral 401/429 and never names the
profile before success. Known, unchanged: per-profile localStorage keys for window
layouts (`dispatch-appwin:v1:<profile id>`) exist for every profile that ever used the
device. 4.4 wipes them (`dispatch-appwin:*`, `dispatch-last-character`) on every lock and
when a held browser boots, so nothing in storage points at the held profile.
Push: a hold unbinds the device's Web Push subscription (a locked phone gets no pings for
that profile); a resume does not rebind it (the next Settings visit does). A held token
earns no remembered-device throttle privilege and is never `touch`ed.

### Notices on resume
| Notice | Fresh sign-in | Resume | Why safe |
|---|---|---|---|
| Sign-in notice (who can see what: `disclosure`, host storage) | shown, at most daily per device | skipped | this device already showed it to this session at its sign-in |
| PIN-reset notice ("an Admin reset your PIN") | shown once per notice per device | skipped (stays unseen, shows at the next sign-in) | not safety-critical; never lost |
| "Set a password" card (Admin without one) | shown once per device | skipped | already offered at this session's sign-in |
| First-run guide threads | first install only | n/a | |
| Landing banner ("signed out after N min") | on landing | on landing only | names main, not the held profile |

## 5. Routes (all declared in `route_policy.py`)

* `GET /api/auth/status` gains `resumable` (bool), `keep_unlocked` (bool), `keep_unlocked_allowed` (bool).
* `POST /api/auth/lock` holds the caller's session (body ignored); the cookie stays.
* `POST /api/auth/resume` `{secret}`: public, throttled exactly like `/api/auth/login`.
* `POST /api/auth/keep-unlocked` `{on, secret?}`: the caller's own signed-in session
  (`use_chat`); refused for Basic. Turning it ON on a session that is not yet remembered
  needs the profile's credential (throttled like a sign-in; asking with no secret is not
  a failed attempt) whenever it is off and the session was not signed in within the
  last 180 s, remembered device or not (the one-time offer card right after a sign-in), and
  never for a re-minted session (a restart is not a fresh sign-in).

## 6. Durability (security review)

Session rows now decide what a restart brings back, so every write that ends or locks a
session is retried; a lock that cannot be written ENDS the session instead. "Sign out
everywhere" also moves the profile's epoch, so a row a failed delete left behind can
never be re-minted. Wrong resumes are counted in the row (a restart gives no fresh
guesses). A remembered device that is not keep-unlocked keeps its idle clock across a
restart. There is no separate "sign out and forget this device" button: Lock holds for
12 h; an Admin's "Sign out everywhere" (People) ends it at once.

## 7. Keep-unlocked and the idle timeouts

The idle lock window of a session is the per-role timeout (`session_timeout_*`, or
`lock_timeout_seconds` for the Owner). "Keep this device unlocked" removes the idle lock
for that one device: the role timeout still applies on every other device of the same
profile. It is offered once per device right after a sign-in (card) and lives in
Settings, This device. Manual Lock always works and always offers resume.

Cookies, CSRF, Origin checks and WS auth are unchanged; a held session's sockets get the
existing `locked` frame.
