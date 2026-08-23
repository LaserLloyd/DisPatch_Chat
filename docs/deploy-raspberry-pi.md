# Deploying DisPatch Chat on a Raspberry Pi

A Pi is an excellent host for this app. It idles at roughly 90–130 MB of RAM, has no
build step, and needs no GPU. A Pi 4 with 4 GB handles a household comfortably.

The only thing that needs real thought is **storage**, and most of what you have read
about SD cards and SQLite is wrong in an interesting way. That section is long because
it is the part people get wrong.

- [Hardware](#hardware)
- [Storage: the part that matters](#storage-the-part-that-matters)
- [Install](#install)
- [Tuning for a Pi](#tuning-for-a-pi)
- [Backups](#backups)
- [Reducing writes](#reducing-writes)
- [Troubleshooting](#troubleshooting)

---

## Hardware

| | Minimum | Recommended |
|---|---|---|
| Model | Pi 4, 2 GB | Pi 4 or 5, 4 GB+ |
| Storage | 16 GB A1 microSD | **USB3 SSD** (Pi 4) or **NVMe via M.2 HAT+** (Pi 5) |
| Power | Official PSU | Official PSU. Not a phone charger. |
| OS | Raspberry Pi OS Lite (64-bit) | same |
| Network | Wi-Fi works | Ethernet |

**64-bit is required.** Published images are `linux/amd64` and `linux/arm64`; there is
no 32-bit `armv7` image. Check with `uname -m` — you want `aarch64`, not `armv7l`. Pi
OS still offers a 32-bit image and plenty of people install it by accident.

Use **Pi OS Lite**. The desktop spends RAM you would rather give to the app.

---

## Storage: the part that matters

### The short version

1. **Use an SSD if you can.** Not because SD cards "wear out from SQLite" — see below —
   but for random-IO performance and because SSDs report SMART health while SD cards
   tell you nothing until they fail.
2. **If you use an SD card, buy a high-endurance one** and accept that it is a
   consumable.
3. **Format as ext4. Never exFAT.** SD cards over 32 GB ship formatted exFAT.
4. **Never put the database on a network share.**
5. **Back up off the device.** This is the one that actually saves you.

### Wear is not the risk people think it is

DisPatch writes roughly **16 KiB per chat message** once WAL frames and checkpointing
are counted. That is about a 100x amplification over the ~176-byte row, which sounds
alarming until you multiply it out:

| Messages/day | Written per year |
|---|---|
| 100 | ~0.6 GB |
| 1,000 | ~6 GB |
| 10,000 | ~61 GB |

A WD Purple 256 GB high-endurance microSD is rated at **128 TBW**. At 1,000 messages a
day you would need roughly twenty thousand years. Even assuming a pathological 1000x
write amplification inside the card's flash translation layer, you are into decades.

**Chat traffic will not wear out your card.** What actually kills Pi storage is power
loss, cheap cards, and filesystem choice.

> On the SD-vs-SSD endurance gap specifically: it is smaller than the internet
> believes. WD Purple 256 GB = 128 TBW (0.50 TBW/GB); Samsung 870 EVO 500 GB = 300 TBW
> (0.60 TBW/GB). The real cliff is **consumer vs high-endurance**, not SD vs SSD —
> consumer cards (Ultra, EVO Select) publish no endurance rating at all, and that
> absence is the point.
>
> The honest reasons to prefer an SSD are random-IO performance, SMART telemetry, and
> a mature power-loss story — not a fabricated endurance gap. There is no official
> Raspberry Pi statement recommending SSDs for reliability; anyone citing one is
> inventing it.

### Why small random writes are still worth reducing

SD cards erase in large segments. Arnd Bergmann's
[LWN analysis](https://lwn.net/Articles/428584/) found "the most common size for these
segments is **4MB**", and that "some brands can only have one or two segments open at a
time, which causes them to constantly go through garbage collection."

So a 4 KiB write can cost anywhere from 4 KiB to a full segment erase. **Nobody
publishes a measured amplification figure for microSD under small random writes** — the
vendors do not disclose it. The practical takeaway is not "you will wear the card out",
it is "small random writes are slow and unpredictable on SD", which is a latency
argument for an SSD.

### Durability: keep `synchronous=FULL`

DisPatch sets `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=FULL`. **Do not change
this.** You will find advice telling you `synchronous=NORMAL` is faster and safe. Both
halves are true, and it is still the wrong trade for a chat app.

SQLite's own wording:

> "**WAL mode is safe from corruption with synchronous=NORMAL** … But **WAL mode does
> lose durability. A transaction committed in WAL mode with synchronous=NORMAL might
> roll back following a power loss or system crash.** Transactions are durable across
> application crashes regardless of the synchronous setting."
> — <https://www.sqlite.org/pragma.html#pragma_synchronous>

and, on the other side:

> "For maximum reliability and for robustness against database corruption, SQLite
> should always be run with its default synchronous setting of FULL."
> — <https://www.sqlite.org/howtocorrupt.html>

Those reconcile: NORMAL is *corruption*-safe but not *durable*. On a Pi with no UPS,
NORMAL means a brownout silently discards the last few messages — precisely the data
this app exists to keep.

The performance cost is irrelevant here. Measured on NVMe, FULL sustains ~408
commits/sec versus ~38,000 for NORMAL. Ninety-three times slower, and still three
orders of magnitude more than a household chat produces. An SD card will be far slower
in absolute terms and still far faster than you need.

### Power supply is the real defence

The dominant real-world cause of Pi corruption is undervoltage and unclean shutdown.
Use the official PSU, a thick short cable, and shut down properly rather than pulling
the plug. This does more for your data than any pragma.

SQLite is blunt about what it cannot protect you from:

> "most consumer-grade mass storage devices lie about syncing… **USB flash memory
> sticks seem to be especially pernicious liars** regarding sync requests. … There is
> no way for SQLite to detect that either is lying."
> — <https://www.sqlite.org/howtocorrupt.html>

### Filesystem

**ext4.** SD cards larger than 32 GB ship as exFAT, which has no journaling and no
POSIX ownership — so a crash can damage the directory entry for `chats.db` no matter
what SQLite does, and Docker's UID mapping becomes meaningless. Reformat.

**Never a network share.** NFS, SMB, or a NAS mount will eventually corrupt the
database:

> "**All processes using a database must be on the same host computer; WAL does not
> work over a network filesystem.**" — <https://www.sqlite.org/wal.html>
>
> "SQLite relies on exclusive locks for write operations, and those have been known to
> operate incorrectly for some network filesystems. **This has led to database
> corruption.**" — <https://www.sqlite.org/useovernet.html>

If you want media on a NAS, keep the database local.

### A1 vs A2 cards

Counterintuitive, and version-dependent:

- **Pi 4 and earlier:** A2's advantage needs a Command Queue-capable host controller.
  The BCM2711 has none, so **A2 buys you nothing** over A1 and sometimes benchmarks
  worse. Buy A1 high-endurance and save the money.
- **Pi 5:** CQE support exists (`dtparam=sd_cqe`) and helps. But some specific cards
  misbehave — the kernel carries per-card quirks disabling CQE for certain models, and
  the reported symptom is `mmc0: running CQE recovery` messages and **slowness**, not
  corruption. If you see those, `dtparam=sd_cqe=off` in `/boot/firmware/config.txt` is
  the escape hatch.

---

## Install

### 1. Prepare the Pi

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Use the gear icon to
preset hostname, SSH and Wi-Fi.

```bash
ssh pi@raspberrypi.local
sudo apt update && sudo apt full-upgrade -y
uname -m          # must print aarch64
```

### 2. Move to an SSD (recommended)

With the SSD attached over USB3 (blue port):

```bash
lsblk                                  # identify it, e.g. /dev/sda
sudo mkfs.ext4 -L dispatch /dev/sda1   # DESTROYS everything on that device
sudo mkdir -p /srv/dispatch
echo 'LABEL=dispatch /srv/dispatch ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo chown -R 1000:1000 /srv/dispatch
```

`noatime` avoids a metadata write on every read. Linux has defaulted to `relatime`
since 2.6.30, so this is a small win rather than a dramatic one — but it is free.

Do **not** add `nobarrier`. It is only appropriate with a battery-backed write cache,
which a Pi does not have.

### 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world
```

Docker's convenience script supports Raspberry Pi OS on arm64 as a first-class
platform. Do not use `apt install docker.io` — it is usually far behind.

### 4. Deploy

```bash
git clone https://github.com/LaserLloyd/dispatch-chat.git ~/dispatch
cd ~/dispatch
cp .env.example .env
```

Edit `.env`:

```ini
# Put the data on the SSD. Skip this line to use a Docker named volume
# (which will live on whatever disk /var/lib/docker is on — the SD card,
# unless you moved it).
DATA_PATH=/srv/dispatch/data

# The Pi OS default user is UID 1000, so this matches out of the box.
PUID=1000
PGID=1000

TZ=Europe/London

# Pi-appropriate limits — see the next section.
MEM_LIMIT=1g
CPU_LIMIT=3.0
DISPATCH_FILES_TOTAL_MAX=5368709120     # 5 GiB, not the 20 GiB default
DISPATCH_MIRROR=0
```

```bash
mkdir -p /srv/dispatch/data
sudo chown -R 1000:1000 /srv/dispatch/data
docker compose up -d
docker compose logs -f
```

First boot takes noticeably longer than on a desktop — schema creation, reaction pack
seeding, crash-recovery checks. On an SD card, tens of seconds is normal. The compose
health check allows 90 seconds for this.

Open `http://<pi-ip>:8765` and **set a PIN**.

---

## Tuning for a Pi

The `.env` values worth revisiting:

| Setting | Default | On a Pi | Why |
|---|---|---|---|
| `DISPATCH_FILES_TOTAL_MAX` | 20 GiB | **5 GiB** | The default assumes a desktop disk. On a 32 GB card it is a promise you cannot keep. |
| `MEM_LIMIT` | 1g | 1g (512m on a 2 GB Pi) | A hard cap is a *feature*: without it the kernel OOM killer picks a victim system-wide, and it is often `sshd`. |
| `DISPATCH_MIRROR` | 0 in the image | **0** | The mirror scans agent transcripts every few seconds. With no agent backend it finds nothing — pure wasted IO. |
| `DISPATCH_BACKUP_INTERVAL` | 21600 (6h) | 21600 | Each snapshot is a full copy of the DB. Do not set this to minutes. |
| `DISPATCH_MAX_CONCURRENCY` | 3 | 1 | Only relevant with a *local* model. Irrelevant if agents are remote or absent. |
| `CPU_LIMIT` | 2.0 | 3.0 | Leave one of four cores for the OS. |

Do not raise `MEM_LIMIT` above what the Pi has. A limit larger than physical RAM is not
a limit.

**Swap.** Pi OS ships 100 MB of dphys-swapfile on the SD card. Raising it to multiple
gigabytes is the classic bad advice: swapping to flash is slow and *is* genuinely
write-heavy. If you are hitting swap, lower `MEM_LIMIT` or add RAM, don't add swap.

---

## Backups

The app writes rotated snapshots into `<data>/backups/` every 6 hours. Those are
consistent online snapshots — the right technique — but **they are on the same disk as
the database**. They protect against corruption and mistakes, not against losing the
card.

**Never `cp` or `rsync` a live SQLite database.** SQLite lists copying a database
without its journal as a way to produce a corrupt copy
([howtocorrupt.html](https://www.sqlite.org/howtocorrupt.html)). The rotated snapshots
exist precisely so you have something safe to copy.

A minimal off-device job — copy the newest snapshot somewhere else:

```bash
sudo tee /usr/local/bin/dispatch-offsite >/dev/null <<'EOF'
#!/bin/bash
set -euo pipefail
SRC=/srv/dispatch/data/backups
DEST=user@nas.local:/volume1/backups/dispatch/
latest=$(ls -1t "$SRC"/*.db 2>/dev/null | head -1)
[ -n "$latest" ] || { echo "no snapshot found" >&2; exit 1; }
rsync -a --partial "$latest" "$DEST"
EOF
sudo chmod +x /usr/local/bin/dispatch-offsite

# Daily at 04:30.
( crontab -l 2>/dev/null; echo "30 4 * * * /usr/local/bin/dispatch-offsite" ) | crontab -
```

For everything (media, File Server blobs, config), stop first so the WAL is folded in:

```bash
docker compose stop app
sudo tar czf /mnt/usb/dispatch-$(date +%F).tar.gz -C /srv/dispatch/data .
docker compose start app
```

**Test a restore.** A backup you have never restored is a hypothesis. Copy the tarball
to another machine, unpack it, point a container at it, and confirm your messages are
there.

---

## Reducing writes

Worth doing for latency and card longevity, in rough order of value.

### 1. Journald

Check whether you even have a problem:

```bash
ls -d /var/log/journal 2>/dev/null || echo "journald is already volatile — nothing to do"
```

If the directory does not exist, logs are already in RAM and **log2ram would add
nothing**. If it does exist, prefer configuring journald directly over installing
another moving part — in `/etc/systemd/journald.conf`:

```ini
[Journal]
Storage=volatile
RuntimeMaxUse=32M
```

then `sudo systemctl restart systemd-journald`.

> log2ram is popular but its own README notes it will not sync "in case of power
> failures" — so you lose the logs for exactly the event you most needed them for.
> Configuring journald achieves the same write reduction with one less package.

### 2. Bound the container logs

Already handled: `docker-compose.yml` sets `max-size: 10m`, `max-file: 3`. Docker's
default json-file driver has **no** size limit, so any container you add yourself
should get the same treatment.

### 3. Access logging

Already off — the image passes `--no-access-log`. A journald line per HTTP request is
the single largest avoidable source of writes this app produces.

### 4. `commit=` mount option (optional)

You will see `commit=60` recommended. The kernel documentation is reassuring about
safety:

> "you will lose as much as the latest 5 seconds of metadata changes (**your filesystem
> will not be damaged though, thanks to the journaling**)… Note that due to delayed
> allocation **even older data can be lost on power failure** since writeback of those
> data begins only after `dirty_expire_centisecs`."
> — <https://docs.kernel.org/admin-guide/ext4.html>

Raising it does **not** endanger fsync'd SQLite commits — those are synced explicitly.
It does widen the window for losing recently-written **media blobs**, which are written
without fsync. A modest win; skip it unless you are optimising hard. Keep
`data=ordered`.

### 5. Do not tune `page_size`

SQLite: "The default page size is recommended for most applications." Changing it on a
WAL database means leaving WAL mode first. Not worth it.

---

## Troubleshooting

### Everything is slow, especially opening the app

Almost always SD-card random IO. Check:

```bash
docker compose exec app python - <<'PY'
import time, sqlite3
db = sqlite3.connect('/data/chats.db')
t = time.time()
for i in range(100):
    db.execute("CREATE TABLE IF NOT EXISTS _probe(x)"); db.execute("INSERT INTO _probe VALUES (1)"); db.commit()
print(f"{100/(time.time()-t):.0f} commits/sec")
db.execute("DROP TABLE _probe"); db.commit()
PY
```

Under ~30 commits/sec means the storage is the bottleneck. Move to an SSD.

### The container is killed and restarts

Out of memory. `docker compose logs app` plus `dmesg | grep -i oom`. Usually triggered
by an image upload — Pillow decodes the whole bitmap. Raise `MEM_LIMIT` if the Pi has
the RAM; otherwise ask people to send smaller pictures.

### `sqlite3.OperationalError: database is locked`

Two writers. Either two containers share the volume, or someone added `--workers`. This
app must run as **exactly one process** — sessions, WebSocket clients and background
loops all live in process memory.

If neither applies, the data directory is probably on a filesystem with broken
locking — check it is not a network share or exFAT.

### `chats.db-wal` is left behind after a crash

**Normal, and not corruption.** SQLite replays the WAL on the next open. It does mean
any backup you took while the app was stopped that way is incomplete — take a fresh
one.

To confirm the database is fine:

```bash
docker compose exec app python -c \
  "import sqlite3;print(sqlite3.connect('/data/chats.db').execute('PRAGMA integrity_check').fetchall())"
```

`[('ok',)]` is what you want.

### The card is full

```bash
du -sh /srv/dispatch/data/*
```

Usually `media/`, `files/` or `reactions/spent/` (fired reaction images are kept
forever by design so old chat traces still open). Lower
`DISPATCH_FILES_TOTAL_MAX`, delete old File Server entries from the UI, or prune
`reactions/spent/` if you do not care about re-opening historical reactions.

### Permission denied on `/data`

The bind-mount UID mismatch. `sudo chown -R 1000:1000 /srv/dispatch/data`, or see
[deploy-docker.md § Volume permissions](deploy-docker.md#volume-permissions). The
entrypoint prints the two UIDs involved.

### `exec format error` when starting the container

You are on a 32-bit OS. `uname -m` will say `armv7l`. Reflash with the 64-bit image;
there is no 32-bit build.

### Undervoltage warnings

```bash
vcgencmd get_throttled     # 0x0 is healthy
```

Anything else means the power supply or cable is inadequate. Fix it before worrying
about anything else on this page — it is the most likely cause of eventual data loss.
