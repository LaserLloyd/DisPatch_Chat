# Deploying DisPatch Chat on bare Linux (systemd)

No container. The app is a single Python process, so this is genuinely simple — and it
is the **only** deployment where the optional agent, terminal and service-control
features work properly, because those features drive the host operating system.

Two variants, and the choice is not about taste:

| | **System unit** | **User unit** |
|---|---|---|
| Runs as | dedicated `dispatch` account | you |
| Starts at boot | yes, always | yes, **but only with linger enabled** |
| Sandboxing | full systemd sandbox | limited |
| Agent CLI in your home | awkward | natural |
| Coding terminal | no | yes |
| **Pick this if** | you want a chat server | you want the agent integrations |

---

## Prerequisites

- Linux with systemd 229+ (anything from the last decade)
- Python 3.12 or newer (CI tests 3.12 and 3.13)
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

No compiler, no Node, no build step. The frontend is vanilla JS with vendored
libraries; there is nothing to bundle.

---

## Variant A: user unit

Best for a desktop or home server you log into, and the right choice if you want agent
replies.

```bash
git clone https://github.com/LaserLloyd/dispatch-chat.git ~/dispatch
cd ~/dispatch/backend
uv sync --frozen --no-dev
```

`--frozen` installs exactly what `uv.lock` pins. Drop `--no-dev` if you intend to run
the test suite.

Install the unit:

```bash
mkdir -p ~/.config/systemd/user
sed "s#__APP_DIR__#$HOME/dispatch#g" ~/dispatch/deploy/systemd/dispatch-user.service \
  > ~/.config/systemd/user/dispatch.service

systemctl --user daemon-reload
systemctl --user enable --now dispatch
systemctl --user status dispatch
```

### Then do the thing everyone forgets

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger      # expect Linger=yes
```

Without linger, systemd tears down your entire user manager when your last session
ends. Your "always-on" chat server dies the moment you close the SSH connection, and it
does not start at boot. This is the single most common bare-metal support question.

```bash
journalctl --user -u dispatch -f
```

Open `http://<your-ip>:8765` and **set a PIN** in the Security panel.

---

## Variant B: system unit

Best for a headless server. Runs as an unprivileged service account under a real
systemd sandbox.

```bash
# 1. Service account with no shell. --create-home gives it a real home so uv
#    has somewhere to put its cache; the app's DATA lives in /var/lib/dispatch,
#    which systemd's StateDirectory= creates for us on every start.
sudo useradd --system --create-home --home-dir /var/lib/dispatch \
     --shell /usr/sbin/nologin dispatch

# 2. uv where root and the service account can both see it. The installer in
#    the Prerequisites section puts uv in YOUR ~/.local/bin, which is not on
#    root's PATH and not readable as `dispatch` — this is the step people skip
#    and then get "uv: command not found" from sudo.
sudo install -m 0755 "$(command -v uv)"  /usr/local/bin/uv
sudo install -m 0755 "$(command -v uvx)" /usr/local/bin/uvx

# 3. Code in /opt, owned by that account.
sudo install -d -o dispatch -g dispatch /opt/dispatch
sudo git clone https://github.com/LaserLloyd/dispatch-chat.git /opt/dispatch
sudo chown -R dispatch:dispatch /opt/dispatch

# 4. Build the venv at a fixed path as the service user. HOME and UV_CACHE_DIR
#    are not optional: `sudo -u` keeps root's HOME, and uv would try to write
#    its cache into /root.
sudo -u dispatch env HOME=/var/lib/dispatch \
     UV_CACHE_DIR=/var/lib/dispatch/.cache/uv \
     UV_PROJECT_ENVIRONMENT=/opt/dispatch/.venv \
     /usr/local/bin/uv sync --frozen --no-dev --project /opt/dispatch/backend

# 5. Install and start.
sudo cp /opt/dispatch/deploy/systemd/dispatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dispatch
systemctl status dispatch
```

`journalctl -u dispatch -f` for logs.

The unit uses `StateDirectory=dispatch`, so systemd creates and chowns
`/var/lib/dispatch` on every start. That removes the most common permission problem
before it happens.

### Verify the sandbox

```bash
systemd-analyze security dispatch.service
```

The shipped unit should score in the "OK"/"GOOD" range. If you relax directives to
enable an optional feature, re-run this and know what you gave up.

---

## Why the unit does not use `uv run`

Both units call `.venv/bin/uvicorn` directly. Two reasons, and neither is cosmetic:

**Signals.** On stop the app drains connections, cancels its background tasks, folds
the SQLite WAL back into `chats.db` with `PRAGMA wal_checkpoint(TRUNCATE)` and closes
the database. That only happens if SIGTERM reaches uvicorn. Every extra process in
between is a chance to lose it.

(uv 0.12 *does* forward SIGTERM correctly, so `uv run` is no longer actively broken —
but it exits 143 instead of 0, which makes a clean stop look like a failure in
`systemctl status`. The old units worked around this with `KillSignal=SIGINT` and
`SuccessExitStatus=130`; calling uvicorn directly removes the need for both.)

**Offline restarts.** `uv run` re-resolves the environment on every start. If the
network is down at boot — which is exactly when things reboot — that can fail. A
prebuilt venv cannot.

### Never add `--workers`

The app keeps unlock sessions, WebSocket clients, rate-limit counters, the terminal PTY
and the backup/mirror loops **in process memory**. A second worker gets its own copy of
all of it:

- unlock the app in one tab, still locked in the next
- two processes writing the same SQLite file
- duplicated backup jobs and duplicated mirroring

One process. It comfortably serves a household.

---

## Optional features (user unit only)

These are off by default in the shipped unit. Each is genuinely powerful — read before
enabling.

(The shipped units set the legacy `LOCAL_CHAT_*` spellings on purpose, so that a
`DISPATCH_*` value in a drop-in or `EnvironmentFile` overrides them. Both names
work everywhere; the `DISPATCH_*` names used below are the documented ones.)

### Agent backend

```ini
Environment=OPENCLAW_BIN=%h/.local/bin/openclaw
Environment=DISPATCH_MIRROR=1
```

**The agent CLI is not part of this project and is not distributed with it.**
DisPatch spawns whatever binary `OPENCLAW_BIN` points at and reads that
runtime's session transcripts off disk; you install it separately. With nothing
installed you get a fully working chat server that simply has no bot replies —
see [agents.md](agents.md) for the contract a backend has to satisfy, and
[llm-providers.md](llm-providers.md) for the no-agent-runtime alternative.

Use an **absolute path**. A user unit does not source your shell profile, so its `PATH`
is whatever the unit says it is — this is the number one cause of "agent replies work
in my terminal but fail as a service".

Check what the service actually sees:

```bash
systemctl --user show dispatch -p Environment
journalctl --user -u dispatch | grep -i openclaw
```

The app logs a warning at boot if the binary is missing, and agent turns then fail with
a clean error rather than hanging.

### Coding terminal

```ini
Environment=DISPATCH_TERMINAL=1
```

**Understand what this is.** It spawns an interactive CLI on a server-side PTY, running
as your user, with your filesystem access, reachable over a WebSocket. It is gated
behind the PIN and restricted to fully-unlocked sessions — which makes setting a
PIN a precondition, not an afterthought, since with no credential configured
there is no unlocked session for the gate to check — but you are putting a
remote shell on your network. Set a strong PIN
first, and do not enable this on a machine reachable from the internet.

The CLI it spawns is **external and not distributed with this project** — name
it with `DISPATCH_TERMINAL_BIN` (a bare name resolved on `PATH`, or an absolute
path). There is deliberately no default: with the variable unset the terminal
reports that no CLI is configured rather than guessing at a binary.

```ini
Environment=DISPATCH_TERMINAL_BIN=your-cli
# optional: extra PATH entries for the spawned CLI, colon-separated
Environment=DISPATCH_TERMINAL_PATH=%h/.local/share/your-cli/bin
# optional: TOML the model picker reads for a CLI that keeps providers in a file
Environment=DISPATCH_TERMINAL_CONFIG=%h/.config/your-cli/config.toml
```

---

## Reverse proxy

Same rules as the container. The one that matters:

**The WebSocket guard requires `URL(Origin).host == Host`.** A proxy that rewrites the
`Host` header breaks live updates *silently* — pages load, REST works, the chat just
stops updating.

Caddy v2 preserves `Host` by default and needs no special configuration. nginx does
not, and the WebSocket snippet everyone copy-pastes sets `Host` to the upstream:

```nginx
server {
    server_name chat.example.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;        # NOT $proxy_host
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;                 # agent turns can be long
        client_max_body_size 4G;                  # File Server limit
    }
}
```

Behind a proxy, also set `DISPATCH_HOST=127.0.0.1` in the unit so the app is not
independently reachable on the LAN.

**Do not strip `X-Forwarded-For`.** The app uses its presence as proof that a request
did not originate from a local process, which forces the API-token check on its
machine-to-machine endpoints.

**Do not add uvicorn's `--proxy-headers`/`--forwarded-allow-ips`.** You do not need it,
and it exists to let clients influence their apparent address.

---

## Avatars live in the data directory

Uploaded avatars are user data, and they are stored with the rest of it — in
`<data dir>/avatars/`, beside `media/` and `files/`. They survive a `git pull`,
a redeploy, and a reinstall, and they are included whenever you back up the
data directory. No extra `ReadWritePaths=` line is needed.

The URL is unchanged: `/static/avatars/<file>` is mounted from the data
directory explicitly, ahead of the general `/static` mount. (A symlink would
not work — Starlette's `StaticFiles` resolves real paths and returns 404 for
anything escaping the static root. The separate mount is why this works.)

**Upgrading from an older install?** Nothing to do. If avatars are still in
`frontend/static/avatars/` and the data directory has none, the app keeps using
the old location, so your roster does not go blank. To move them, copy the
directory into `<data dir>/avatars/` while the app is stopped; it prefers the
data directory whenever it exists.

---

## Where everything lives

| | User unit | System unit |
|---|---|---|
| Code | `~/dispatch` | `/opt/dispatch` |
| Data | `~/.local/share/local-chat` | `/var/lib/dispatch` |
| Database | `<data>/chats.db` | same |
| Media / File Server | `<data>/media`, `<data>/files` | same |
| Snapshots | `<data>/backups` | same |
| Bot roster | `<data>/config.yaml` | same |
| **PIN, recovery, API token** | `<data>/security.yaml` | same |
| Remembered devices | `<data>/trusted-devices.yaml` | same |
| Upload spool | `<data>/tmp` | same |

`security.yaml` holds the PIN hash, the recovery hash and the inbound API token. Back
it up, keep it `0600`, and never commit it.

---

## Backups

The app writes rotated snapshots to `<data>/backups/` every 6 hours. They are on the
same disk as the database, so they are not a disaster-recovery plan on their own.

**Never `cp` or `rsync` a live SQLite database** — SQLite lists that as a way to
produce a corrupt copy ([howtocorrupt.html](https://www.sqlite.org/howtocorrupt.html)).
Copy a snapshot, or take a fresh consistent one:

```bash
DATA=~/.local/share/local-chat        # or /var/lib/dispatch
sqlite3 "$DATA/chats.db" "VACUUM INTO '/tmp/dispatch-$(date +%F).db'"
```

`VACUUM INTO` is safe against a live database. For everything else:

```bash
systemctl --user stop dispatch
tar czf ~/dispatch-$(date +%F).tar.gz -C "$DATA" .
systemctl --user start dispatch
```

Stopping matters: it triggers the WAL checkpoint, so the tarball is self-contained.

---

## Upgrading

```bash
cd ~/dispatch
git pull
cd backend && uv sync --frozen --no-dev
systemctl --user restart dispatch
```

System unit:

```bash
sudo -u dispatch git -C /opt/dispatch pull
sudo -u dispatch env UV_PROJECT_ENVIRONMENT=/opt/dispatch/.venv \
     uv sync --frozen --no-dev --project /opt/dispatch/backend
sudo systemctl restart dispatch
```

Take a snapshot first for anything major. Schema migrations run at startup and are not
reversible.

After upgrading, a browser may hold stale frontend assets from the service worker.
Hard-reload (Ctrl-Shift-R) or unregister the worker in devtools.

---

## Troubleshooting

### The service dies when I log out

Linger. `loginctl enable-linger "$USER"`. See [Variant A](#then-do-the-thing-everyone-forgets).

### `status=203/EXEC` or "No such file or directory"

The `ExecStart` path is wrong. Confirm the venv exists:

```bash
ls -l ~/dispatch/backend/.venv/bin/uvicorn
systemctl --user cat dispatch | grep ExecStart
```

If you moved the checkout after installing, re-run the `sed` step — the path is baked
into the unit.

### Agent replies fail but the CLI works in my shell

`PATH`. A user unit does not read your shell profile. Set `OPENCLAW_BIN` to an absolute
path and check with `systemctl --user show dispatch -p Environment`.

### `Permission denied` writing to the data directory

System unit: `StateDirectory=` should prevent this; if you set `DISPATCH_DATA_DIR`
somewhere else, add a matching `ReadWritePaths=`.

User unit: `ls -ld ~/.local/share/local-chat`.

### Uploads of large files fail, or the machine runs out of memory during an upload

`TMPDIR` is pointing at a tmpfs. Multipart uploads spool there before reaching the blob
store and the per-file ceiling is 4 GiB — on tmpfs that is a 4 GiB RAM allocation. Most
modern distros mount `/tmp` as tmpfs, which is why both units set `TMPDIR` onto the
data directory. Do not remove those lines.

### Shutdown hangs for the full `TimeoutStopSec`

Something is holding the event loop. Check `journalctl -u dispatch -n 50` for the last
message before the stop. If the app was SIGKILLed, a `chats.db-wal` file is left
behind — **that is not corruption**, SQLite replays it on the next open, but a backup
taken in that state is incomplete.

### Avatar upload returns an error on the system unit

`ProtectSystem=strict` making the source tree read-only. See
[the avatars section](#known-wart-avatars-live-in-the-source-tree).

### DNS or outbound HTTP fails only under the system unit

`RestrictAddressFamilies=` is missing `AF_NETLINK`. glibc's `getaddrinfo()` uses a
netlink socket for `AI_ADDRCONFIG`, so without it name resolution breaks in a way that
looks like a network fault rather than a sandbox one. The shipped unit already includes
it; do not trim it.

### `database is locked`

Two processes on one database. Check for a stray manual `uvicorn`, a second unit, or an
added `--workers`.

### Everything works locally but not from another device

`DISPATCH_HOST` must be `0.0.0.0`, and the host firewall must allow the port:

```bash
sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload   # firewalld
sudo ufw allow 8765/tcp                                                          # ufw
```
