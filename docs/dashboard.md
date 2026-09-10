# The host dashboard

One page that answers a single question: **is this install healthy, and if not,
what do I do about it?**

Open it from **Settings → 🩺 Health** (the ⚙ button in the gear rail).
Everything on it is read-only — the dashboard
never changes anything, it only looks. It is admin-only: a limited (Safe-Mode)
device cannot see it, and neither can anyone without the PIN.

- `GET /api/dashboard` — the cheap snapshot the page polls while it is open.
- `GET /api/dashboard/deep` — the expensive checks, only when you press the
  button (full database scan + a fresh measurement of the file stores).
- `GET /api/dashboard/logs?lines=N` — the tail of the app log, if there is one.

If a check itself fails — a directory it cannot read, a probe that takes too
long — that becomes a red finding explaining what failed. The page never goes
blank and never returns an error page. A dashboard that breaks when the box is
broken is worthless.

---

## The banner

| Colour | Meaning | What to do |
|---|---|---|
| 🟢 **Healthy** | Every check passed. | Nothing. |
| 🟠 **Attention** | At least one check is a warning. | Read it today, not now. It is a problem that is coming, not one that has arrived. |
| 🔴 **Problem** | At least one check failed. | Act now. Something is broken, unsafe, or about to lose data. |

The banner is always the **worst** finding on the page, so a single red item
turns the whole thing red no matter how much else is fine.

---

## The cards

### Process

| Metric | What it means | When to care |
|---|---|---|
| **Version** | The DisPatch Chat version running. | Compare with what you meant to deploy after an upgrade. |
| **Uptime** | How long this process has been running. | An uptime that keeps resetting means something is crashing and restarting it. |
| **Python** | Interpreter version. | Only relevant when a dependency complains. |
| **Platform** | OS, kernel, CPU architecture. | Handy when asking for help. |
| **Container** | `docker`, `podman`, `kubernetes`, … or blank for bare metal. | Changes the advice about running as root, and reminds you the data directory is a volume you must back up separately. |
| **PID** | Process id. | For `kill -HUP`, `strace`, or matching against `ps`. |
| **Memory (RSS)** | Physical memory in use right now. | On a 1–2 GB Raspberry Pi, steady growth over days is a leak worth reporting. On some platforms only the *peak* is available and the page says so. |
| **CPU** | Percentage of one core, measured between the last two refreshes. | Blank on the first refresh — there is nothing to compare against yet. Sustained high CPU with nobody chatting is unusual. |
| **User** | The account the service runs as. | Should not be `root` — see `process.root`. |
| **Bind** | The address and port the server actually listens on, when that can be verified — the page reads the live socket (and, on Linux, the kernel's listener table) and says which source it used. When it cannot verify, it falls back to the configured values (`DISPATCH_HOST` / `DISPATCH_PORT`, legacy `LOCAL_CHAT_*`) and labels the line "Configured host" with a caveat. | `0.0.0.0` means "every network this machine is on". `127.0.0.1` means "this machine only". The `auth.no_pin` / `net.tls` findings never report "only reachable from this machine" from an *unverified* value — they degrade to a warning instead, erring towards "you are exposed". |

### Storage

| Metric | What it means | When to care |
|---|---|---|
| **Data directory** | Where everything lives: database, uploads, backups, config. | This is the folder to back up. Everything else is reinstallable. |
| **Free space** | Free bytes on the filesystem holding that folder. | The single most common cause of a self-hosted install falling over. |
| **Database file** | Size of `chats.db`. | Grows slowly; messages are small. |
| **WAL** | The write-ahead log sitting next to the database. | Normally a few MB. A WAL that grows to hundreds of MB and stays there means checkpoints are not completing — usually a stuck reader; restart the service. |
| **Images / Files** | Size and file count of the `media/` and `files/` stores. | These are what actually fill a disk. |
| **Backups** | Size of the rotated snapshot folder. | Roughly the database size × how many snapshots you keep. |
| **Reactions** | Size of the reaction-image store (packs, mood pools, fired one-shots). | Fired images are kept forever so old chat traces stay re-openable — this line is how you notice that policy's cost. Counted separately from the upload cap, which governs `media/` and `files/` only. |
| **Cap** | Total stored blobs against the configured ceiling (`DISPATCH_FILES_TOTAL_MAX`, 20 GB by default). | Uploads are refused once the cap is reached. |

Sizes are measured with a bounded directory walk and cached for ~30 seconds, so
polling the page is cheap. A store with an enormous number of files may report
"approximate" — the walk stopped at its budget rather than tying up the server.
Press **Run deep check** for an exact measurement.

### Database

| Metric | What it means | When to care |
|---|---|---|
| **Check** | `quick_check` on the normal refresh, full `integrity_check` on a deep check. | Anything other than "ok" is a red finding — see `db.integrity`. |
| **Journal mode** | Should be `wal`. | See `db.journal_mode`. |
| **Pages / page size** | The database's internal size accounting. | Mostly informational; `pages × page size` is the logical size, which can be smaller than the file after deletions. |
| **Free pages** | Space inside the file that has been freed but not returned to the disk. | A large fraction means a `VACUUM` would shrink the file. Not urgent. |
| **Search index** | Whether full-text search is available. | If off, search still works via a slower fallback scan. |
| **Threads / messages** | Row counts. Deep check only (counting is a full scan). | Sanity-check after a restore. |
| **Backups** | Snapshot count, when the newest one was written, and whether the last attempt verified. | See the `backup.*` findings. |

Backup health is read from the backups folder itself rather than from memory,
so it stays true across a restart — which is exactly when you want to know
whether last night's snapshot worked.

### Connections

Live WebSocket clients — open browser tabs, phones, and the app on a wall
display — split into:

- **Full** — unlocked, admin-capable sessions.
- **Limited** — Safe-Mode devices (no PIN entered, or the session idled out).

Counts only. The dashboard deliberately shows no addresses, no device names and
no session identifiers: knowing *how many* people are connected is operations,
knowing *who* is surveillance.

### Agent backend

Whether the configured agent CLI exists, is executable, and what version it
reports. **This is optional.** A build with no agent backend is a supported
setup — history, uploads, search and this dashboard all work without one; only
the replies are missing. Set `OPENCLAW_BIN=""` to say so explicitly and the
page stops mentioning it.

The card also shows a **Direct providers** row (payload field `api_bots`) when
one or more bots are connected straight to an LLM provider via **Connect an
AI** — see [llm-providers.md](llm-providers.md). Those bots reply over HTTP and
need no CLI, so a box with `Configured: none` and `Direct providers: 2` is a
fully working install, not a broken one. The card's footnote says so rather
than leaving you to infer it.

### Log tail

The last lines of the app log. On a default install there is no log *file* —
DisPatch logs to standard output, which is captured by:

- systemd: `journalctl --user -u local-chat -f`
- Docker: `docker logs -f <container>`

Set `DISPATCH_LOG_FILE=/path/to/dispatch.log` (and point your logging there)
if you want the viewer to show it. The default expected path is
`<data dir>/logs/dispatch.log`.

---

## The findings

Every finding has a stable **id** shown on the page. Look it up here.

### Security

#### `auth.no_pin`
No PIN is configured.

- 🔴 **Red** when the app is reachable over the network. Anyone who can reach
  this machine has full access: every conversation, the file store, and the
  harness pane if it is enabled.
- 🟢 **Green** when bound to `127.0.0.1`, because nothing off this box can
  connect.

**Fix:** Settings → 🔒 Security & PIN → set a PIN. Or, to keep it
machine-local, set `DISPATCH_HOST=127.0.0.1` and restart.

#### `auth.lock`
🟢 A PIN is set. Locked devices get the limited Safe-Mode view; media and
unlisted assistants stay behind the PIN.

#### `net.tls`
Whether traffic is encrypted.

- 🟢 Bound to loopback only — nothing crosses a network.
- 🟢 A TLS terminator is declared (see fix).
- 🟠 Otherwise: plain HTTP. On a home LAN or a private tailnet that is a
  reasonable trade-off; over anything wider, your PIN and every message travel
  in the clear.

**Fix:** put DisPatch behind a TLS terminator — Caddy, nginx, or
`tailscale serve` — then set `DISPATCH_BEHIND_TLS=1` (or
`DISPATCH_PUBLIC_URL=https://…`) so this check knows. The app cannot detect a
reverse proxy by itself, which is why the declaration is manual.

#### `auth.api_token_default`
🔴 `api_token` in `security.yaml` is a well-known placeholder (`changeme`,
`secret`, …). That token lets a remote caller post messages *as your
assistants*.

**Fix:** replace it with a long random value:
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

#### `auth.api_token_weak`
🟠 The token is shorter than 16 characters. It is a password; treat it like one.

#### `auth.api_token`
🟢 Either no token is configured (remote inbound calls are refused outright,
on-box automation still works) or a strong one is set.

#### `auth.security_file_mode`
🟠 `security.yaml` is readable by other users on this machine, and it holds the
PIN hash and the API token.

**Fix:** `chmod 600 <data dir>/security.yaml`.

#### `process.root`
Running as the root user.

- 🟠 **On bare metal.** DisPatch does not need root. Anything that can talk it
  into writing a file writes as root — the harness feature especially.
  **Fix:** run it as a normal user (a systemd *user* unit, or `User=` in a
  system unit) and `chown` the data directory to that user.
- 🟢 **Inside a container**, where it is the norm and the blast radius stops at
  the container. Still worth running the image as a non-root user if you can.

### Storage

#### `storage.disk_full`
🔴 Less than 200 MB — or under 2% — free on the filesystem holding the data
directory. Writes are about to start failing: messages, uploads and backups all
stop at zero free bytes, and a database write interrupted by a full disk is how
databases get damaged.

**Fix:** free space now. Delete old snapshots from the `backups/` folder, then
use the File Server's wipe control to drop old uploads.

#### `storage.disk_low`
🟠 Under 1 GB, or under 10%, free. Not urgent yet.

**Fix:** trim old uploads, or lower `DISPATCH_BACKUP_KEEP` so fewer snapshots
are retained.

#### `storage.disk`
🟢 Plenty of space — or 🔴 if free space could not be read at all (the data
directory is missing or unreadable).

#### `storage.not_writable`
🔴 The data directory cannot be written by the user running the service.
Nothing can be saved at all.

**Fix:** `chown -R <service user> <data dir>` (in Docker: check the volume's
ownership against the container's user), then restart.

#### `storage.cap_reached`
🔴 Stored images and files have hit the configured ceiling. New uploads are
being refused.

**Fix:** delete files from the File Server, or raise
`DISPATCH_FILES_TOTAL_MAX` (in bytes) and restart.

#### `storage.cap_near`
🟠 Over 80% of the cap.

#### `storage.cap`
🟢 Comfortably under the cap.

### Database

#### `db.integrity`
🟢 The database passed its structural check, or 🔴 it did not.

A failure means real damage — usually caused by a full disk, a power cut on a
filesystem without proper flushing (very common on SD cards), or a database
file on a network mount.

**Fix:** stop the service and restore the newest snapshot from the `backups/`
folder. They are plain SQLite files: copy one over `chats.db` (and delete any
`chats.db-wal` / `chats.db-shm` next to it), start the service, and run the
deep check again to confirm.

#### `db.journal_mode`
🟠 The database is not in WAL mode. WAL is what keeps reads fast while a write
is in flight, and what the online snapshot depends on.

**Fix:** this almost always means the data directory is on a filesystem that
cannot do shared memory — an NFS or SMB mount, or some Docker volume drivers.
Move the data directory to local storage.

### Backups

#### `backup.ok`
🟢 Snapshots exist and the newest one is recent.

#### `backup.disabled`
🟠 `DISPATCH_BACKUP_INTERVAL` is 0, so no snapshots are taken at all. A
corrupt database would mean starting over.

**Fix:** set it to a number of seconds (`21600` = every 6 hours) and restart —
or take your own copies of `chats.db` on a schedule.

#### `backup.never_run`
No snapshot exists yet. 🟢 right after startup (the first one is written a
minute in), 🟠 once the process has been up long enough that one should exist.

**Fix:** check the log for `DB backup failed`. It is nearly always a full disk
or an unwritable data directory.

#### `backup.failed`
🔴 The most recent snapshot did not pass verification and was set aside as
`.corrupt`. A snapshot usually fails verification because the *live* database
is damaged.

**Fix:** run the deep check on this page. If it also fails, restore from the
last good snapshot (see `db.integrity`).

#### `backup.stale`
🟠 The newest snapshot is much older than the configured interval, so the
backup loop has stopped running.

**Fix:** check the log for backup errors, then restart the service.

#### `backup.unreadable`
🟠 The backups folder could not be listed, so backup health is unknown.

**Fix:** check permissions on `<data dir>/backups/`.

#### `backup.bad_snapshot`
🟠 One or more snapshot files are unusable — zero bytes, or not a SQLite
database. This is what a backup that *failed mid-write* leaves behind (a full
disk is the classic cause), and an unusable snapshot no longer counts toward
"backups are current".

**Fix:** free disk space if that was the cause, delete the bad files, and
watch for the next snapshot to verify.

### Agent backend

#### `agent.api_bots`
🟢 One or more bots are wired straight to an LLM provider through **Connect an
AI** (Settings → 🔌 AI models). These answer over the provider's HTTP
API and never touch the agent CLI, so the findings below do not apply to them
— which is why this one is listed first. Only shown when at least one such bot
exists.

Their credentials live in `config.yaml` in the data directory, which every
writer chmods to `0600`. See [llm-providers.md](llm-providers.md).

#### `agent.ok`
🟢 The CLI was found and is executable. Its version is shown when it can be
obtained cheaply; if it cannot, the reason is shown and nothing is wrong.

#### `agent.none`
🟢 No agent backend is configured, and that is a supported setup.

#### `agent.missing`
🟠 A CLI is configured but was not found on `PATH` (or the absolute path does
not exist). Messages are still accepted and stored — nothing will reply to
them.

**Fix:** install the agent CLI, or point `OPENCLAW_BIN` at its absolute path
and restart. Set `OPENCLAW_BIN=""` if you are deliberately running without one.

#### `agent.gateway`
🟢/🟠 Whether the agent *gateway* answers on its port — checked separately
from the CLI, because "the binary exists" and "something replies" are
different facts. A present CLI with an unreachable gateway is exactly the
"nothing answers my messages" symptom: the turn spawns, then times out.

**Fix:** start (or restart) the gateway service the CLI talks to, then send a
test message.

#### `agent.not_executable`
🟠 The file exists but this user cannot execute it.

**Fix:** `chmod +x <path>`, and check the file is readable by the service user.

### Clock

#### `time.clock`
🟢 The system clock agrees with the data.

#### `time.clock_skew`
The clock is wrong, in one of two ways:

- Stored messages are dated **in the future** — the clock moved backwards.
  New messages sort into the middle of old conversations and daily threads land
  on the wrong day. 🟠 over 5 minutes, 🔴 over an hour.
- The clock **jumped** while the process was running (comparing wall time
  against a monotonic timer). 🟠 over 5 minutes. A single jump shortly after
  boot is normal on a board with no battery-backed clock finally reaching a
  time server.

**Fix:** enable time sync — `sudo timedatectl set-ntp true` on most Linux
systems — and restart DisPatch so background timers re-anchor. On a Raspberry
Pi with no RTC module, this is expected at every boot until the network is up.

### Probe failures

#### `probe.process`, `probe.storage`, `probe.database`, `probe.connections`, `probe.agent`, `probe.clock`, `probe.findings`
🔴 That check could not run — it raised an error or exceeded its time budget —
and the reason is shown verbatim. A check that did not run is never reported as
a pass.

Common causes: the data directory or `/proc` is unreadable by the service user,
the database connection is down, or the disk is so busy the probe timed out.

**Fix:** read the detail text, then the service log for the full error. If
`probe.database` is present, the app cannot talk to its own database — the
service almost certainly needs restarting, and `db.integrity` should be checked
once it comes back.

---

## Running the deep check

The button runs the expensive things the automatic refresh deliberately skips:

- `PRAGMA integrity_check` — reads every page of the database. Seconds on a
  family-sized database, longer on a big one. It runs on its own connection, so
  chat keeps working while it does.
- A fresh, uncached measurement of the media and file stores.
- Exact thread and message counts.

Only one deep check runs at a time; a second request while one is in flight is
refused rather than queued.

## Configuration reference

Every variable below also answers to the legacy `LOCAL_CHAT_` prefix; the
`DISPATCH_` spelling is the documented one and wins where both are set.

| Variable | Effect on the dashboard |
|---|---|
| `DISPATCH_HOST` / `DISPATCH_PORT` | The bind shown, and what `auth.no_pin` and `net.tls` consider "reachable". |
| `DISPATCH_DATA_DIR` | The folder measured for free space and stores. |
| `DISPATCH_BACKUP_INTERVAL` / `DISPATCH_BACKUP_KEEP` | Drives the `backup.*` findings. |
| `DISPATCH_FILES_TOTAL_MAX` | The storage cap the `storage.cap*` findings measure against. |
| `DISPATCH_BEHIND_TLS` / `DISPATCH_PUBLIC_URL` | Declares that a TLS terminator sits in front (clears `net.tls`). |
| `DISPATCH_LOG_FILE` | Which file the log viewer tails. |
| `OPENCLAW_BIN` | Which agent CLI is checked; `""` means "none, on purpose". |
