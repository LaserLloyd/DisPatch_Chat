# Deploying DisPatch Chat on Windows

## Read this first

**Windows is a fine place to try DisPatch and a poor place to host it 24/7.**

There is one specific, unfixable reason: **neither Docker Desktop nor WSL2 starts
without an interactive Windows sign-in.** Not "it's fiddly" — there is no supported
mechanism, on either product, in 2026.

- Docker Desktop's only startup option is *"Start Docker Desktop when you **sign in**
  to your computer."*
  ([settings docs](https://docs.docker.com/desktop/settings-and-maintenance/settings/))
  The installer's `--always-run-service` flag starts the privileged helper service,
  not the daemon, and does not start your containers.
  [docker/roadmap#515](https://github.com/docker/roadmap/issues/515) asks for
  boot-without-login, opened July 2023, still open with no maintainer commitment.
- WSL2 VMs are started on demand inside a *user session*. Microsoft's systemd
  documentation states it plainly:

  > "It is also important to note that with these changes, **systemd services will NOT
  > keep your WSL instance alive.**"
  > — <https://learn.microsoft.com/en-us/windows/wsl/systemd>

  So `systemctl enable docker` makes Docker start when the distro starts. It does not
  make the distro start, and it does not keep it running.

So: after a Windows Update reboot at 3 a.m., your family chat server is down until
somebody logs into that PC. There are Task Scheduler and NSSM workarounds (below), all
community-maintained, all with side effects.

**If you want a chat server that survives reboots unattended, use Linux.** A Raspberry
Pi 4 costs less than a Windows licence and does this correctly with one
`systemctl enable`. A Linux VM under Hyper-V on the same Windows box is also a
perfectly good answer — you get `systemctl enable --now` and Hyper-V starts the VM at
boot without a login.

With that said, here is how to do it properly on Windows.

---

## Which path?

| | Docker Desktop | Docker Engine inside WSL2 |
|---|---|---|
| Supported by Docker | **Yes** | No — [the platform list](https://docs.docker.com/engine/install/) has no Windows/WSL entry |
| Licence | Free for personal use, education, and businesses with **<250 employees AND <$10M revenue**. Otherwise from $5/user/month ([terms](https://docs.docker.com/subscription/desktop-license/)) | Apache 2.0, always free |
| Setup effort | Installer, next-next-finish | ~15 minutes of Linux admin |
| GUI, updates, `docker compose` | Included | You manage it |
| **Recommended for** | **Almost everyone** | Large orgs without a licence budget |

**Use Docker Desktop unless licensing rules it out.** The alternative is not better
technically; it is just free at large scale.

Docker Desktop's WSL2 backend is the default and the one to use. The Hyper-V backend
still exists and is still supported — the docs say functionality is consistent between
them — but it is all-users-install-only and uses a separate resource-limit mechanism.
There is no documented plan to remove it, despite what forums claim.

> **Watch this space:** Microsoft shipped **WSL containers** (`wslc.exe`) in public
> preview on 2026-06-29 — a Linux container runtime built into WSL, no Docker Desktop,
> no licence, with virtiofs for faster file access. GA is targeted for autumn 2026.
> It does **not** support Docker Compose yet, so it cannot run this stack today.
> <https://learn.microsoft.com/en-us/windows/wsl/wsl-container>

---

## Path A: Docker Desktop (recommended)

### 1. Install

1. Windows 11, or Windows 10 22H2+. Virtualisation enabled in BIOS.
2. Install WSL2: open PowerShell **as Administrator** and run

   ```powershell
   wsl --install
   wsl --update
   ```

3. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>.
4. Settings → General → confirm **"Use the WSL 2 based engine"** is ticked.
5. Leave it in **Linux containers** mode (the default). Windows containers cannot run
   Linux images — the kernel must match, and there is no cross-OS execution. Nothing
   about a Python app changes this.

### 2. Cap the VM's resources

WSL2 defaults to **50% of your total RAM** and all logical processors. It also does
not return cached pages to Windows until the VM shuts down, which is the `vmmem`
memory complaint people hit.

Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=4GB
processors=4
swap=2GB
```

Then `wsl --shutdown` in PowerShell — **the settings do not apply until you do**, and
the VM takes about 8 seconds to actually stop.

4 GB is generous for this app; it idles at roughly 90–130 MB.

### 3. Get the app running

Open **Windows Terminal → Ubuntu** (the WSL2 shell, *not* PowerShell) and work
entirely inside the Linux filesystem:

```bash
cd ~                        # /home/<you> — the Linux ext4 disk. NOT /mnt/c.
git clone https://github.com/LaserLloyd/dispatch-chat.git
cd dispatch-chat
cp .env.example .env
docker compose up -d
```

Open <http://localhost:8765>.

**Set a PIN immediately** in the app's Security panel. With no PIN the app is fully
open to anything that can reach the port.

### 4. Make it reachable from your phone

Docker Desktop publishes to `localhost` on the Windows host, but Windows Firewall
blocks inbound LAN connections by default. In an Administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "DisPatch 8765" -Direction Inbound `
  -LocalPort 8765 -Protocol TCP -Action Allow -Profile Private
```

Note `-Profile Private`. If your network is classified Public, either change the
classification (Settings → Network → Properties → Private) or you have just opened a
port on whatever café Wi-Fi you join next.

Find the LAN address with `ipconfig`, then browse to `http://<that-ip>:8765`.

---

## Path B: Docker Engine inside WSL2 (no Docker Desktop)

Only if licensing blocks Path A.

```powershell
wsl --install -d Ubuntu
```

Inside Ubuntu:

```bash
# 1. Enable systemd (landed in WSL 0.67.6; default for current Ubuntu images).
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

```powershell
wsl --shutdown          # from PowerShell, then reopen Ubuntu
```

```bash
# 2. Install Docker Engine — the standard Linux instructions.
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker

# 3. Start at distro start.
sudo systemctl enable --now docker

# 4. Deploy as in Path A.
cd ~ && git clone https://github.com/LaserLloyd/dispatch-chat.git && cd dispatch-chat
cp .env.example .env && docker compose up -d
```

If `systemd=true` will not work for you, `wsl.conf` has a fallback that Microsoft's own
docs illustrate with Docker:

```ini
[boot]
command=service docker start
```

**Known first-run failure:** the daemon refuses to start with iptables errors. WSL
kernels want the legacy backend:

```bash
sudo update-alternatives --config iptables    # choose iptables-legacy
sudo systemctl restart docker
```

**Port forwarding differs from Path A.** Docker Desktop proxies published ports to the
Windows host automatically; a bare Engine install inside WSL2 does not always. Recent
WSL versions mirror localhost, but for reliable LAN access add to `.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

---

## The gotchas that actually matter

### 1. Never put your data on the Windows drive

This is the single most important rule on Windows, and it is a correctness issue, not
just a performance one.

`/mnt/c` is not a filesystem mount. It is a **9P network protocol server**
(`wslservice.exe`) reached across the VM boundary
([drvfs technical docs](https://github.com/microsoft/WSL/blob/master/doc/docs/technical-documentation/drvfs.md)).

Microsoft's own guidance:

> "We recommend against working across operating systems with your files… For the
> fastest performance speed, store your files in the WSL file system."
> — <https://learn.microsoft.com/en-us/windows/wsl/filesystems>

Docker's:

> "Performance is much higher when files are bind-mounted from the Linux filesystem,
> rather than accessed from the Windows host filesystem."
> — <https://docs.docker.com/desktop/features/wsl/best-practices/>

Measured in [microsoft/WSL#4197](https://github.com/microsoft/WSL/issues/4197): `dd`
write throughput was **442 MB/s on WSL1 vs 40.4 MB/s on WSL2** for the same Windows
drive — roughly 10x slower.

**And SQLite needs working file locking.** SQLite's own documentation:

> "SQLite depends on the underlying filesystem to do locking as the documentation says
> it will. But some filesystems contain bugs in their locking logic… **This is
> especially true of network filesystems.** … database corruption might result."
> — <https://www.sqlite.org/howtocorrupt.html>

and WAL mode specifically requires shared memory between processes:

> "**All processes using a database must be on the same host computer; WAL does not
> work over a network filesystem.**" — <https://www.sqlite.org/wal.html>

9P over a VM boundary is a network filesystem by construction.

> **Honest caveat:** I could not find a crisp, current, reproduced bug report pinning
> SQLite corruption specifically to WSL2 + 9P + a Docker bind mount. The confidence
> here comes from SQLite's stated requirements versus what 9P provides, plus a long
> tail of consistent `database is locked` reports. It is cheap insurance against a
> failure mode whose symptom is silent data loss.

**So:**

| | |
|---|---|
| ✅ Named volume (the default) | Lives on ext4 inside the Docker VM. Correct and fast. |
| ✅ Bind mount to `~/something` in WSL2 | ext4 in the distro. Fine. |
| ❌ `DATA_PATH=/mnt/c/Users/...` | 9P. Slow, no inotify, unsafe for SQLite. |
| ❌ `DATA_PATH=C:\Users\...` in `.env` | Same thing with a friendlier spelling. |

**Leave `DATA_PATH` unset on Windows.** The default named volume is the right answer.

**A quick self-check** if you suspect you got this wrong:

```bash
docker compose exec app python -c \
  "import sqlite3;print(sqlite3.connect('/data/chats.db').execute('pragma journal_mode').fetchone())"
```

It must print `('wal',)`. Anything else means the filesystem could not do it.

### 2. Permissions "just work" here — and that is a trap

Docker Desktop reports shared files as mode **0777**, and this is not configurable:

> "When sharing files from Windows, Docker Desktop sets permissions on shared volumes
> to a default value of 0777… **The default permissions on shared volumes are not
> configurable.**"
> — <https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/>

`PUID`/`PGID`/`RUN_AS` in `.env` therefore do nothing useful on Windows. Fine.

The trap: a compose file that works perfectly on your Windows laptop can fail
immediately with `EACCES` the first time it runs on a Linux server, because that is the
first time the numeric UID has to genuinely match. **If you develop on Windows and
deploy on Linux, test on Linux before you call it done.** See
[deploy-docker.md § Volume permissions](deploy-docker.md#volume-permissions).

### 3. File watching does not work across `/mnt/c`

> "Linux containers **only receive file change events, 'inotify events', if the
> original files are stored in the Linux filesystem.**"
> — <https://docs.docker.com/desktop/features/wsl/best-practices/>

Not slow — *absent*. Irrelevant for normal operation; it will confuse you badly if you
try to develop with `--reload` against a `/mnt/c` checkout.

### 4. Agent features

Everything in
[deploy-docker.md § Agents in a container](deploy-docker.md#agents-in-a-container)
applies, and Windows adds a layer: the agent CLI would need to be a **Linux** binary,
since it runs inside the Linux container. A Windows `.exe` is not reachable from the
container in any supported way.

Practically: on Windows, run DisPatch as a chat server and expect no bot replies unless
you build a derived image with a Linux agent CLI inside it.

---

## Making it survive reboots (as far as Windows allows)

Ranked, most to least honest:

### Best: don't. Use a Linux VM on the same machine.

Hyper-V (Windows Pro) or VirtualBox starts a VM at boot with no login. Install Ubuntu
Server, follow [deploy-bare-metal.md](deploy-bare-metal.md) or run Docker inside it,
and you get `systemctl enable --now` semantics with none of the caveats below.

### Good: automatic login + "start on sign in"

1. Docker Desktop → Settings → General → **Start Docker Desktop when you sign in**.
2. `.env`: nothing to change — compose's `restart: unless-stopped` restarts containers
   once the engine is up.
3. Configure Windows automatic sign-in (`netplwiz`, untick "Users must enter a user
   name and password").

**This disables meaningful at-rest protection on that account.** Acceptable for a
dedicated always-on box in your house; not acceptable for a laptop you carry.

### Workable: Task Scheduler

Trigger *At startup*, "Run whether user is logged on or not", highest privileges:

```
Program:   C:\Windows\System32\wsl.exe
Arguments: -d Ubuntu -u root /usr/sbin/service docker start
```

or `wsl.exe -d Ubuntu -e sleep infinity` just to pin the VM up.

**Known side effect:** starting WSL before a user logs in causes permission errors when
that user later tries to browse `\\wsl.localhost\<distro>` from Explorer. It is a real,
reported annoyance, not a theoretical one.

### Also seen: NSSM

Wrap `wsl.exe` as a Windows service with [NSSM](https://nssm.cc/). Same class of
solution, same lack of vendor support.

---

## Troubleshooting

### "Docker Desktop requires a newer WSL kernel version"

```powershell
wsl --update
wsl --shutdown
```

### `docker compose` says the daemon is not running

Docker Desktop must be running and past the "Engine starting…" state. On Path B,
`sudo systemctl status docker`, and check the iptables-legacy fix above.

### `vmmem` / `Vmmem` is eating all my RAM

Expected without a cap — WSL2 takes up to 50% of RAM by default and does not return
cached pages until shutdown. Set `memory=` in `.wslconfig` and `wsl --shutdown`. See
step 2 above.

### Everything is extremely slow

Your project or data directory is on `/mnt/c`. Move the checkout into `~` inside WSL2
and leave `DATA_PATH` unset. See gotcha 1.

### `database is locked` / `disk I/O error`

Almost certainly a `/mnt/c` bind mount. Verify with the `journal_mode` check in gotcha
1; it must return `wal`. Move the data to a named volume.

### The app loads but the chat never updates

WebSocket rejected. If you added a reverse proxy, see
[deploy-docker.md](deploy-docker.md#the-one-that-will-get-you-host-header-rewriting) —
a proxy that rewrites the `Host` header breaks the app's CSWSH guard, and the failure
is silent.

### My phone cannot reach it

1. Firewall rule added (step 4)?
2. Network profile set to **Private**, not Public?
3. `docker compose ps` shows the port published on `0.0.0.0`, not `127.0.0.1`? Check
   `BIND_ADDR` in `.env`.
4. Some corporate/VPN clients block LAN traffic outright.

### It was working, then Windows rebooted overnight and it was down

That is the documented behaviour described at the top of this page. Pick one of the
options in [Making it survive reboots](#making-it-survive-reboots-as-far-as-windows-allows),
or move the deployment to Linux.
