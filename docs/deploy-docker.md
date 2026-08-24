# Deploying DisPatch Chat with Docker

This is the **primary supported deployment path**. Multi-arch images are published for
`linux/amd64` and `linux/arm64`.

- [Quick start](#quick-start)
- [What you actually get](#what-you-actually-get)
- [What does not work in a container](#what-does-not-work-in-a-container)
- [Volume permissions](#volume-permissions)
- [Agents in a container](#agents-in-a-container)
- [Reverse proxy and HTTPS](#reverse-proxy-and-https)
- [Health checks](#health-checks)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [Building it yourself](#building-it-yourself)
- [Troubleshooting](#troubleshooting)

---

## Quick start

You need Docker Engine 24+ with Compose V2 (`docker compose`, two words).

```bash
git clone https://github.com/LaserLloyd/dispatch-chat.git
cd dispatch-chat
cp .env.example .env
$EDITOR .env          # optional; the defaults work
docker compose up -d
```

If you are running your own copy, one line repoints every reference in the tree
at your own fork:

```bash
grep -rl 'LaserLloyd/dispatch-chat' --exclude-dir=.git . \
  | xargs sed -i 's|LaserLloyd/dispatch-chat|myuser/dispatch-chat|g'
```

With `IMAGE` unset (the default), this **builds the image from the checkout**
rather than pulling — so it works with no registry account and on any
architecture. Set `IMAGE` in `.env` to pull a published image instead.

Open <http://localhost:8765> — or `http://<this-machine's-LAN-IP>:8765` from a phone
on the same network.

```bash
docker compose logs -f          # watch it boot
docker compose ps               # STATUS should reach "healthy" within ~90s
                                # (the first boot seeds assets; start_period is 90s)
```

**Set a PIN immediately.** With no PIN configured the app is *completely open* to
anyone who can reach the port — that is deliberate (it makes first-run painless) but
it is not a state to leave it in. Open the app, go to Security, set a PIN, and write
down the recovery code it shows you.

---

## What you actually get

A single container running one uvicorn process:

| | |
|---|---|
| Base image | `python:3.13-slim-trixie` |
| Size | ~178 MB (measured, amd64) |
| Runs as | UID/GID 1000, non-root |
| Data | one volume at `/data` |
| Port | 8765 |
| Idle memory | ~90–130 MB RSS |

Everything mutable lives in `/data`: the SQLite database, uploaded media, File Server
blobs, avatars, reaction images, backups, and the config/security YAML files. Back up
that one directory and you have backed up everything.

---

## What does not work in a container

Be clear-eyed about this before you start. Three features of this app drive the *host
operating system*, and a container is specifically designed to stop that.

### The coding terminal — disabled

It spawns an interactive CLI on a server-side PTY and exposes it over a WebSocket.
Inside a container it would give you a shell *in the container*, which is what
`docker exec -it dispatch bash` already does, with better isolation and no network
exposure. `DISPATCH_TERMINAL=0` in the image; leave it there.

### The ComfyUI service panel — disabled

It shells out to `systemctl --user`. There is no systemd in the container, so every
button in that panel returns an error. `DISPATCH_COMFY=0` in the image.

If you run ComfyUI on the host, the panel's *service control* is what breaks; nothing
stops you pointing a browser at ComfyUI directly.

### Agent replies — optional, and genuinely awkward

This gets its own section below, because it is the honest weak point of containerising
this app.

---

## Volume permissions

This is the number one Docker support issue for every self-hosted app, and it behaves
completely differently on Linux than on Windows/macOS.

### Use a named volume (the easy path)

Leave `DATA_PATH` unset in `.env`. Compose then uses the named volume `dispatch-data`.

When Docker creates an **empty named volume** and mounts it at a path that exists in
the image, it copies that path's contents *and its ownership* into the volume. The
image ships `/data` owned by `app:app` (1000:1000), so the volume comes out owned by
1000:1000, and the container — which runs as 1000:1000 — can write to it. Nothing for
you to do.

The cost is that the data lives under `/var/lib/docker/volumes/dispatch-data/_data`,
which is a slightly awkward place to point a backup script at. That is a real
trade-off, not a reason to avoid named volumes; see [Backups](#backups).

> Named volumes write **directly to the host filesystem**, not through the container's
> copy-on-write layer. Docker's own storage-driver documentation says to
> ["use volumes for write-heavy workloads"](https://docs.docker.com/engine/storage/drivers/)
> and notes that OverlayFS
> ["only implements a subset of the POSIX standards"](https://docs.docker.com/engine/storage/drivers/overlayfs-driver/).
> Never let a SQLite database sit on the container's writable layer.

### Use a bind mount (when you need to choose the location)

Set `DATA_PATH=/srv/dispatch/data` in `.env`. Now you control where it lives — which
matters on a Pi, where you want the database on an SSD rather than the SD card.

**Docker does not chown a bind mount.** The host directory's ownership shows through
exactly as-is. If your host user is not UID 1000, the container cannot write and the
app refuses to start.

**Rootless podman is the same problem with a different cause.** There the mismatch is
the user-namespace remap: your UID maps to a different UID inside the container, so a
directory you own appears owned by someone else. `podman run --userns=keep-id` (or
`userns_mode: keep-id` in the compose file) maps your UID straight through and fixes
it; a named volume sidesteps it entirely. Podman also **ignores the image's
`HEALTHCHECK`** unless the image is in Docker format — with the default OCI format it
warns and skips it, so `podman ps` will never show a health status. Nothing is broken;
the compose file's own `healthcheck:` block still applies where the runtime honours
it.

The entrypoint detects this and prints the fix rather than dying cryptically. Here is
what it actually looks like — this output is from a real run, not an illustration:

```
dispatch-entrypoint:  /data is not writable.
dispatch-entrypoint:    running as : 1000:1000
dispatch-entrypoint:    dir owned by: 0:0
dispatch-entrypoint: FATAL: data directory not writable
```

Two fixes:

**a) Match the host directory to the container (simplest):**

```bash
sudo mkdir -p /srv/dispatch/data
sudo chown -R 1000:1000 /srv/dispatch/data
```

**b) Tell the container which IDs to use.** In `.env`:

```ini
PUID=1001          # your `id -u`
PGID=1001          # your `id -g`
RUN_AS=0:0         # let it start as root so it can chown, then drop
```

and uncomment the `cap_add:` line in `docker-compose.yml`. The container starts as
root, chowns `/data`, drops to `PUID:PGID` via `setpriv`, and never runs application
code as root.

The recursive chown only runs when it detects a mismatch, not on every boot — a
recursive chown across a 20 GB media library on every restart is a real problem on a
Pi, and plenty of images get that wrong.

### Why this never bites you on Windows or macOS

Docker Desktop runs Linux in a VM and passes host directories through a translation
layer that reports everything as owned by whoever asked. Permissions "just work" —
which is a trap, because it means a compose file that works perfectly on a Windows
laptop fails immediately when the same person deploys it to a Linux server. If you
develop on Windows and deploy on Linux, test the Linux path.

### Avatars are user data

Uploaded avatars are user data, and the app stores them in the **data directory** at
`/data/avatars` — on the volume, beside `media/` and `files/`. Nothing special is
needed: the data volume already carries them, they survive `docker compose pull`, and
they are in your backup the moment the data directory is.

The URL is unchanged. `/static/avatars/*` is mounted from the data directory
explicitly, ahead of the general `/static` mount, and Starlette matches mounts in
order — so the specific one claims the path.

**This is why it is a mount and not a symlink.** A symlink was tested, not assumed:

```
/app/frontend/static/avatars -> /data/avatars
GET /static/avatars/probe.png   =>  HTTP 404
```

Starlette's `StaticFiles` resolves the real path of every request and refuses anything
that escapes the mounted directory. That is a traversal defence doing its job — and the
reason the app mounts the data directory as its own route instead. Use the
mount.

The entrypoint checks whether the path is a real mount and warns loudly if it is not,
so you find out at boot rather than after losing pictures.

### Rootless Docker and Podman

Both remap container UIDs into a *subordinate UID range* on the host. A file the
container writes as UID 1000 lands on the host as something like `525287`.

This is not cosmetic. Verified on this machine: after a container chowned its bind
mount, the host files became `525287:525287` and a plain `rm -rf` failed with
`Permission denied` on every file. The fix is to run the command inside the same user
namespace:

```bash
podman unshare rm -rf /path/to/data     # or: podman unshare chown -R ...
```

Podman notes:

- Add `:Z` to bind mounts on SELinux systems (Fedora, RHEL) or the container gets
  `Permission denied` regardless of ownership.
- `podman build` defaults to the OCI image format, which **silently discards
  `HEALTHCHECK`** with a warning. Use `podman build --format docker` if you want it.
- Named volumes are the path of least resistance under Podman too.

---

## Agents in a container

**Read this before you plan around agent replies.** This is where containerising this
app is genuinely awkward, and pretending otherwise would waste your time.

### Why it is hard

DisPatch does not talk to an agent over a network API. It integrates with a *local CLI
runtime* in three host-shaped ways:

1. **It spawns a binary.** Each turn runs
   `openclaw agent --agent <id> --message-file <path> --session-key <k> --json`
   as a subprocess.
2. **It passes a filesystem path.** The message goes via `--message-file` pointing at
   a temp file (argv has a ~128 KB per-argument limit that long messages exceed). The
   agent process must be able to *open that exact path*.
3. **It reads the agent's transcripts off disk.** Streaming partial replies and the
   conversation mirror both tail `~/.openclaw/agents/<id>/sessions/*.jsonl` directly.

Point 2 is the one that kills the obvious workarounds. A sibling container or a remote
host cannot open `/tmp/dispatch-msg-abc123.txt` from inside the app container. A
network shim is not enough; the two processes must share a filesystem view.

### What the container does by default

Nothing. `OPENCLAW_BIN` is unset, the CLI is not in the image, and on boot you get:

```
WARNING local-chat: openclaw CLI not found at 'openclaw' — agent replies will fail.
```

**This is a supported configuration.** Chat between people, the File Server, media,
threads, search, Safe Mode and remembered devices all work normally. Only bot replies
are unavailable, and they fail with a clean error rather than a hang. If you want a
self-hosted family chat and no bots, you are already done.

### Option 1 — bake the agent into a derived image (recommended if you want agents)

Everything shares one filesystem and one process namespace, so all three requirements
above are satisfied. It is the only arrangement that works without modifying the app.

**The agent runtime is not part of this project and is not distributed with it.**
DisPatch spawns whatever binary `OPENCLAW_BIN` names and reads that runtime's
session transcripts; you install it yourself, and the two names below have to
agree — the `ENV` must point at the binary the `RUN` step actually produced.

```dockerfile
FROM ghcr.io/LaserLloyd/dispatch-chat:latest
USER root
# Install your agent runtime here. This example is Node-based; substitute the
# real package, and check where it lands (`npm bin -g`) before trusting the path.
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && npm install -g <your-agent-cli-package> \
 && rm -rf /var/lib/apt/lists/*
USER app
# Must match what the install above actually put on disk.
ENV OPENCLAW_BIN=/usr/local/bin/<your-agent-cli> \
    DISPATCH_MIRROR=1
```

Costs: a much bigger image, two things to update in lockstep, and you have moved from
"pull an image" to "maintain a Dockerfile". Be honest with yourself about whether you
want that.

### Option 2 — reach an agent running on the host

Mount the pieces in. This works, but only because you are deliberately punching holes
through the isolation you added by using a container.

```yaml
services:
  app:
    volumes:
      - dispatch-data:/data
      # The CLI and its runtime.
      - /home/you/.local/bin/openclaw:/usr/local/bin/openclaw:ro
      - /home/you/.nodejs:/opt/nodejs:ro
      # Session transcripts, for streaming and the mirror. Read-only is enough.
      - /home/you/.openclaw:/home/app/.openclaw:ro
      # A SHARED temp dir, so --message-file paths resolve on both sides.
      - /tmp/dispatch-shared:/tmp/dispatch-shared
    environment:
      OPENCLAW_BIN: /usr/local/bin/openclaw
      TMPDIR: /tmp/dispatch-shared
      DISPATCH_MIRROR: 1
```

Sharp edges, all of them real:

- **The binary must run in *this* container.** A Node or Python CLI needs its
  interpreter and libraries present too. A dynamically linked binary needs matching
  glibc. Statically linked single-file binaries are the only ones that mount cleanly.
- **`~/.openclaw` must be at the same path inside.** The app builds transcript paths
  from `Path.home()`, and home is `/home/app` in the container. Mount it there.
- **UIDs must line up.** Container UID 1000 must be able to read the host files.
- **This is not really isolation any more.** You have given the container your agent
  runtime and its session history. Decide that on purpose.

### Option 3 — the right long-term fix (not implemented yet)

The coupling above is an app design issue, not a Docker one. Making the agent backend
genuinely pluggable means:

- an `AgentBackend` interface with `send(bot_id, session_key, message) -> reply`;
- the existing subprocess implementation behind it (`backend: subprocess`);
- an **HTTP backend** (`backend: http`) that POSTs the message body — no temp file, no
  shared filesystem — so an agent can live in a sibling container or on another host;
- streaming over that HTTP connection instead of by tailing `.jsonl` files;
- `backend: none` as the explicit, documented default.

Only the HTTP backend makes `dispatch` + `agent` a clean two-container compose stack.
Until it exists, Option 1 is the honest recommendation and Option 2 is the pragmatic
one.

> If you are evaluating this app *for* its agent features, deploy it
> [on bare metal](deploy-bare-metal.md) instead. The container is the better choice
> for chat; the host install is the better choice for agents.

---

## Reverse proxy and HTTPS

```bash
# in .env
SITE_ADDRESS=chat.example.com
ACME_EMAIL=you@example.com
BIND_ADDR=127.0.0.1        # stop publishing the app itself to the LAN
```

```bash
docker compose --profile proxy up -d
```

Worth doing even on a LAN: clipboard image paste, PWA install, service workers and
notifications are all restricted to secure contexts, so over plain `http://` on a LAN
IP they are simply unavailable.

### The one that will get you: `Host` header rewriting

The app's WebSocket handshake guard requires

```
URL(Origin).host  ==  Host header
```

and closes the socket with code 1008 when they differ. This is a CSWSH defence and it
is doing its job.

**Caddy v2 preserves the original `Host` by default, so the shipped config just
works.** That is the main reason these docs recommend Caddy.

**nginx does not.** The WebSocket snippet everyone copy-pastes ends with
`proxy_set_header Host $proxy_host;`, which sets `Host: app:8765` while the browser is
still sending `Origin: https://chat.example.com`. Result:

- the page loads
- every REST call works
- **the chat never updates again**, with no error shown

If you use nginx, `proxy_set_header Host $host;` is mandatory:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host       $host;          # <-- NOT $proxy_host
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;                   # agent turns are slow
}
```

### Do not strip forwarding headers

The app treats the presence of `X-Forwarded-For` / `Forwarded` as proof that a request
did **not** originate from a local process, which forces the API-token check on its
machine-to-machine endpoints. Strip those headers at the proxy and a proxied remote
caller starts looking like loopback. Leave them on.

### Do not use `network_mode: host`

Same reason. The loopback exemption is only safe because in bridge networking nothing
outside the container can present as `127.0.0.1`. On the host network, anything on the
box can.

### Do not enable uvicorn's `--proxy-headers`

You do not need it, and `--forwarded-allow-ips='*'` would let a client set its own
apparent address. The app's guard is written to fail closed against this, but there is
no reason to test that.

---

## Health checks

The image ships a `HEALTHCHECK` that polls `/api/health` — an endpoint deliberately
outside the auth gate, so it works whether or not a PIN is set.

```bash
$ curl -s localhost:8765/api/health | jq
{
  "status": "ok",
  "openclaw_available": false,
  "clients": 0,
  "db_integrity_ok": true,
  "last_backup_ok": null,
  "last_backup_at": null,
  "gateway_ok": false,
  "reaction_fire_failures_24h": 0,
  "pool_refill_failures_24h": 0
}
```

An unauthenticated caller sees the fields above and no more: paths, reconciliation
counters and anything else that describes the host are session-only, so the probe
stays useful without becoming a reconnaissance endpoint.

The check requires `status == "ok"` **and** `db_integrity_ok != false`. A process that
is listening but has a corrupt database is exactly what a health check should catch,
and a plain HTTP-200 probe would miss it.

It deliberately ignores `gateway_ok` and `openclaw_available`. The agent backend is
optional; reporting the container unhealthy because you chose not to run agents would
be actively misleading. `"gateway_ok": false` in the output above is a **normal,
healthy** container with no agent backend.

`start_period` is 90s in compose. First boot creates the schema, seeds the reaction
starter pack and runs crash recovery, which on a Pi 4 with an SD card genuinely takes
tens of seconds. Too short a start period turns a slow first boot into a restart loop.

---

## Backups

The app takes its own rotated snapshots into `/data/backups/` every 6 hours
(`DISPATCH_BACKUP_INTERVAL`). Those are consistent online snapshots of a live
database — which is the right way to do it.

**Never `cp` or `rsync` a live SQLite database.** SQLite's own
[How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html)
lists copying a database without its journal as a way to produce a corrupt copy.

**Those snapshots are in the same volume as the database.** They protect you from
corruption and mistakes, not from losing the disk. You need an off-box copy:

```bash
# Consistent, and safe to run while the app is up.
docker compose exec -T app \
  python -c "import sqlite3; sqlite3.connect('/data/chats.db').execute(\
    \"VACUUM INTO '/data/backups/offbox.db'\")"

docker compose cp app:/data/backups/offbox.db ./dispatch-$(date +%F).db
```

To back up everything including media and blobs, stop first so the WAL is folded in:

```bash
docker compose stop app
docker run --rm -v dispatch-data:/data -v "$PWD":/out alpine \
  tar czf /out/dispatch-$(date +%F).tar.gz -C /data .
docker compose start app
```

The stop is not optional if you want the tarball to be restorable in one step.

---

## Upgrading

```bash
docker compose pull
docker compose up -d
```

The data volume is untouched; schema migrations run at startup.

Before a major version, take a snapshot you can actually roll back to:

```bash
docker compose stop app
docker run --rm -v dispatch-data:/data -v "$PWD":/out alpine \
  tar czf /out/pre-upgrade.tar.gz -C /data .
docker compose pull && docker compose up -d
```

**Pin a version in production.** `IMAGE=ghcr.io/LaserLloyd/dispatch-chat:1.2.3` in `.env`.
`latest` eventually restarts you onto a release whose notes you did not read.

**The frontend is aggressively cached by a service worker.** After an upgrade, a
browser can hold the old assets. The app bumps its cache name on release, but if a
client looks stale: hard-reload (Ctrl-Shift-R), or Application → Service Workers →
Unregister in devtools.

---

## Building it yourself

```bash
docker build -t dispatch:dev .
docker compose up -d --build
```

Multi-arch, if you are publishing:

```bash
docker buildx create --use --name dispatch-builder
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/LaserLloyd/dispatch-chat:dev --push .
```

Notes on the Dockerfile, since a few things in it look changeable but are not:

- **Builder and runtime bases are a matched pair.**
  `ghcr.io/astral-sh/uv:python3.13-trixie-slim` → `python:3.13-slim-trixie`. The venv
  contains an absolute symlink to `/usr/local/bin/python3`; copy it onto a base with a
  different interpreter path and everything fails with `required file not found`
  ([astral-sh/uv#7758](https://github.com/astral-sh/uv/issues/7758)).
- **Do not "fix" the tags to `bookworm`.** uv removed those images in 0.10.0 but GHCR
  still serves the last build, so `uv:python3.13-bookworm-slim` silently pins you to
  uv 0.9.30 instead of failing.
- **`uv sync --locked`, not `--frozen`.** `--locked` asserts `uv.lock` matches
  `pyproject.toml` and fails if not; `--frozen` skips the check, which is how you ship
  an image whose dependencies disagree with the manifest you reviewed.
- **uv is not in the runtime image.** `uvicorn` is invoked straight from
  `/app/.venv/bin`. uv 0.12 forwards SIGTERM correctly, so this is about image size
  and about the container exiting `0` instead of `143` on a normal stop.
- **`CMD` is exec form, and that is load-bearing.** See the next section.

No compiler is needed for either architecture: every wheel this app uses (pillow,
pydantic-core, uvloop, httptools, watchfiles, websockets, pyyaml) publishes prebuilt
`manylinux` wheels for both `x86_64` and `aarch64`.

---

## Troubleshooting

### The container exits immediately with "data directory not writable"

Working as intended — see [Volume permissions](#volume-permissions). The log tells you
the UID it runs as and the UID that owns the directory.

### `docker compose ps` shows `(unhealthy)` but the app responds fine

Check the actual health output:

```bash
docker inspect --format '{{json .State.Health}}' dispatch | jq
```

If `db_integrity_ok` is `false`, the database has a real problem:

```bash
docker compose exec app python -c \
  "import sqlite3;print(sqlite3.connect('/data/chats.db').execute('PRAGMA integrity_check').fetchall())"
```

Restore from `/data/backups/` if it reports anything other than `ok`.

### The page loads but messages never appear until I refresh

The WebSocket is being rejected. Almost always a proxy rewriting `Host` — see
[Reverse proxy](#the-one-that-will-get-you-host-header-rewriting). Confirm in the
browser console: a close code of **1008** is the app's CSWSH guard.

Also check you are reaching the app on the same host:port the page was loaded from. A
mismatch (`http://192.0.2.5:8765` in the URL bar, proxied elsewhere) trips the same
guard.

### Shutdown takes 30 seconds and the logs stop mid-sentence

Something is preventing SIGTERM from reaching uvicorn. This is measurable, and the
difference is stark. Two runs of the same image, differing only in how the command is
invoked:

| | PID 1 | Stop time | Exit code | Graceful shutdown logged |
|---|---|---|---|---|
| exec form (shipped) | `uvicorn` | **0.44s** | **0** | yes |
| shell form | `sh` | **25.2s** (SIGKILL) | 137 | **no** |

After the exec-form stop, `/data` contained only `chats.db` — the WAL had been folded
in and `-wal`/`-shm` deleted. After the shell-form kill, `chats.db-wal` and
`chats.db-shm` were still sitting there.

If you have overridden `command:` in your compose file, make it a list, not a string:

```yaml
command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]   # good
command: uvicorn app.main:app --host 0.0.0.0 --port 8765                      # BAD
```

The string form runs `/bin/sh -c "..."`, and `sh` does not forward signals to its
child.

> A leftover `chats.db-wal` after a crash is **not** corruption. SQLite replays it on
> the next open. It does mean any backup you took while stopped was incomplete.

### Agent replies fail with "OpenClaw not available"

Expected with no agent backend. See [Agents in a container](#agents-in-a-container).

### `database is locked`

Two writers. Either you are running more than one container against the same volume,
or you added `--workers`. **Never run this app with more than one worker process** —
unlock sessions, WebSocket clients, rate-limit counters and the backup/mirror loops
all live in process memory, so a second worker means unlocking in one tab and staying
locked in another, plus two processes racing the same database file.

If neither applies, the data directory may be on a filesystem with broken locking —
see the next item.

### The database is on a network share (NFS/SMB) and behaves strangely

Move it. This is not a tuning problem. SQLite's WAL documentation states plainly:

> **All processes using a database must be on the same host computer; WAL does not
> work over a network filesystem.** — <https://www.sqlite.org/wal.html>

and, on network filesystems generally:

> "SQLite relies on exclusive locks for write operations, and those have been known to
> operate incorrectly for some network filesystems. **This has led to database
> corruption.**" — <https://www.sqlite.org/useovernet.html>

Put `/data` on local storage. If you need the media on a NAS, keep the database local
and mount the NAS elsewhere.

### Out of memory / the container keeps restarting on a Pi

Raise `MEM_LIMIT` in `.env`. Uploading a large image is the expensive moment — Pillow
decodes the whole bitmap into memory. Below about 512 MB you will eventually OOM on an
image upload rather than on chat traffic.

### Uploads of large files fail or the container gets OOM-killed mid-upload

Do not mount `/tmp` as tmpfs. Multipart uploads spool through `TMPDIR` before reaching
the blob store, and the per-file ceiling is 4 GiB — on tmpfs that is a 4 GiB RAM
allocation. The image points `TMPDIR` at `/data/tmp` for exactly this reason; if you
override it, keep it on real disk.

### Avatars I uploaded disappeared after an upgrade

The avatars mount is missing from your compose file — see
[Avatars are user data](#avatars-are-user-data). The container warns about this at
startup:

```
dispatch-entrypoint: WARNING: /app/frontend/static/avatars is not a mount. Avatars
                     uploaded in the UI will be LOST on the next image update.
```

### Everything is slow on a Raspberry Pi

See [deploy-raspberry-pi.md](deploy-raspberry-pi.md). The usual answer is that the
data directory is on an SD card and should be on an SSD.
