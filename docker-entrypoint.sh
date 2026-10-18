#!/bin/sh
# =============================================================================
#  DisPatch Chat container entrypoint
# =============================================================================
#  Deliberately POSIX sh, deliberately short. An entrypoint that does clever
#  things is an entrypoint that fails in ways nobody can debug from a `docker
#  logs` tail.
#
#  Responsibilities, in order:
#    1. Make the data directory usable, or explain precisely why it is not.
#    2. Optionally drop from root to PUID:PGID (only if started as root).
#    3. exec the app, so the app becomes the process that receives SIGTERM.
#
#  What it does NOT do: install anything, migrate anything, or "fix" your
#  volume by recursively chowning gigabytes on every boot.
# =============================================================================
set -eu

# Every setting answers to two names: DISPATCH_* (documented, wins) and the
# legacy LOCAL_CHAT_* (kept so existing installs upgrade cleanly). That order
# is decided in backend/app/config.py env(), and this script MUST agree with
# it. When it did not, a `.env` with DISPATCH_DATA_DIR sent the app to one
# directory while this script created, chowned and writability-tested another —
# so the diagnostic passed and the app then failed on its first write, which is
# precisely the failure the check exists to prevent.
DATA_DIR="${DISPATCH_DATA_DIR:-${LOCAL_CHAT_DATA_DIR:-/data}}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

log()  { printf '%s dispatch-entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die()  { log "FATAL: $*"; exit 1; }

# -----------------------------------------------------------------------------
# 1. Privilege handling
# -----------------------------------------------------------------------------
# The image declares USER app (UID 1000) and that is the supported way to run
# it. But people bind-mount host directories owned by other UIDs, and the only
# way to fix that from inside is to start as root and chown. So: if we are
# root, take that as consent to adjust ownership and then drop privileges.
# If we are not root, we never try — we just check and report.
#
# setpriv comes from util-linux, which is Essential on Debian, so this costs
# no extra package (unlike the usual gosu/su-exec download).
if [ "$(id -u)" = "0" ]; then
    log "started as root; will chown ${DATA_DIR} to ${PUID}:${PGID} and drop privileges"

    # Align the baked-in `app` account with the requested ids so file
    # ownership inside and outside the container agree.
    if [ "$(id -u app)" != "$PUID" ] || [ "$(id -g app)" != "$PGID" ]; then
        groupmod -o -g "$PGID" app 2>/dev/null || true
        usermod  -o -u "$PUID" -g "$PGID" app 2>/dev/null || true
    fi

    mkdir -p "$DATA_DIR"
    # Chown the directory itself and the small control files ALWAYS, but only
    # recurse when the top level is visibly wrong. A recursive chown over a
    # 20 GB media library on every container start turns a 2-second boot into
    # a multi-minute one and hammers a Pi's SD card for no reason.
    chown "$PUID:$PGID" "$DATA_DIR"
    if [ "$(stat -c '%u' "$DATA_DIR/chats.db" 2>/dev/null || echo "$PUID")" != "$PUID" ]; then
        log "ownership mismatch detected — running a one-time recursive chown (may take a while on a large volume)"
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi

    # `--user 0:0` leaves HOME=/root, which the dropped user cannot read (the app
    # then dies on /root/.local/share/...). Point HOME at the image user's home.
    chown "$PUID:$PGID" /home/app 2>/dev/null || true
    export HOME=/home/app
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups --inh-caps=-all -- "$0" "$@"
fi

# -----------------------------------------------------------------------------
# 2. Writability check — fail LOUD and EARLY, not on the first message
# -----------------------------------------------------------------------------
mkdir -p "$DATA_DIR" 2>/dev/null || true
if [ ! -w "$DATA_DIR" ]; then
    owner="$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo 'unknown')"
    log "-----------------------------------------------------------------"
    log " ${DATA_DIR} is not writable."
    log "   running as : $(id -u):$(id -g)"
    log "   dir owned by: ${owner}"
    log ""
    log " This is the classic bind-mount UID mismatch. Pick one:"
    log "   a) chown the host directory to match:"
    log "        sudo chown -R $(id -u):$(id -g) /your/host/path"
    log "   b) tell compose which ids to use, in .env:"
    log "        PUID=\$(id -u)   PGID=\$(id -g)"
    log "      and set  user: \"0:0\"  on the service so this script can chown."
    log "   c) drop DATA_PATH from .env and use a named volume, which Docker"
    log "      initialises with the image's ownership and which just works."
    log " Full explanation: docs/deploy-docker.md § Volume permissions"
    log "-----------------------------------------------------------------"
    die "data directory not writable"
fi

# Upload spool. FastAPI's UploadFile spools multipart bodies to TMPDIR before
# they reach the blob store, and the per-file ceiling is 4 GiB. Keeping it on
# the data volume means (a) it is real disk, not the container's RAM-backed
# /tmp if someone mounted one, and (b) the final move is a rename, not a copy
# across filesystems.
mkdir -p "$DATA_DIR/tmp"
export TMPDIR="${TMPDIR:-$DATA_DIR/tmp}"

# Clear anything a previous hard kill left spooled. These are always garbage:
# an interrupted upload is not resumable.
find "$DATA_DIR/tmp" -maxdepth 1 -type f -mmin +60 -delete 2>/dev/null || true

# -----------------------------------------------------------------------------
# 3. Avatars are USER DATA and live on the volume at /data/avatars, beside
#    media/ and files/. Nothing to mount, nothing to warn about: the data
#    volume already carries them, so they survive `docker compose pull` and
#    "back up /data" covers them.
#
#    This used to require a subpath mount over /app/frontend/static/avatars,
#    because the app served avatars from the source tree inside the read-only
#    image. A symlink could not stand in for that mount -- tested:
#
#      /app/frontend/static/avatars -> /data/avatars
#      GET /static/avatars/probe.png  =>  HTTP 404
#
#    Starlette's StaticFiles resolves the real path of every request and
#    refuses anything escaping the mounted directory. The app now mounts the
#    data directory as its own route ahead of /static, which keeps the URL and
#    makes the whole problem go away -- including the Docker 26+ requirement.
# -----------------------------------------------------------------------------
mkdir -p "$DATA_DIR/avatars"

# -----------------------------------------------------------------------------
# 3b. Pre-migration snapshot. When the image version differs from the one that
#     last wrote this data directory, copy the databases and config.yaml to
#     $DATA_DIR/backups/<oldversion>-<UTC timestamp>/ BEFORE the new version's
#     schema migrations run, and keep the newest DISPATCH_PREMIGRATE_KEEP (5).
#     The scheduled snapshots (backups/chats-*.db) are separate and untouched.
#     A failed snapshot warns and starts anyway, leaving the marker alone so the
#     next start retries -- it must never keep the family's chat down.
# -----------------------------------------------------------------------------
NEW_VERSION="${DISPATCH_IMAGE_VERSION:-}"
if [ -z "$NEW_VERSION" ] || [ "$NEW_VERSION" = "dev" ]; then
    NEW_VERSION="dev-$(sed -n 's/^version *= *"\(.*\)"/\1/p' /app/backend/pyproject.toml 2>/dev/null | head -n1)"
fi
MARKER="$DATA_DIR/.image-version"
OLD_VERSION="$(cat "$MARKER" 2>/dev/null || true)"
HAS_DATA=0
for f in "$DATA_DIR"/*.db; do   # a seeded config.yaml alone is not "data to protect"
    [ -e "$f" ] && HAS_DATA=1
done
if [ "$HAS_DATA" = "1" ] && [ "$OLD_VERSION" != "$NEW_VERSION" ]; then
    log "image version ${OLD_VERSION:-<unrecorded>} -> ${NEW_VERSION}: snapshotting before migrations"
    if python - "$DATA_DIR" "${OLD_VERSION:-unrecorded}" "${DISPATCH_PREMIGRATE_KEEP:-5}" <<'PY'
import glob, os, shutil, sqlite3, sys, time

data, old, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])
root = os.path.join(data, "backups")
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
dest = os.path.join(root, f"{old}-{stamp}")
os.makedirs(dest, exist_ok=False)
copied = []
for db in sorted(glob.glob(os.path.join(data, "*.db"))):
    out = os.path.join(dest, os.path.basename(db))
    src = sqlite3.connect(db, timeout=30)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)  # WAL-safe, consistent online copy
        finally:
            dst.close()
    finally:
        src.close()
    copied.append(os.path.basename(db))
cfg = os.path.join(data, "config.yaml")
if os.path.exists(cfg):
    shutil.copy2(cfg, dest)
    copied.append("config.yaml")
open(os.path.join(dest, ".premigrate"), "w").write(f"from {old}\n")
print(f"dispatch-entrypoint: wrote {dest}: {', '.join(copied)}", file=sys.stderr)

snaps = sorted(
    (d for d in glob.glob(os.path.join(root, "*"))
     if os.path.isfile(os.path.join(d, ".premigrate"))),
    key=os.path.getmtime, reverse=True)
for old_dir in snaps[keep:]:
    shutil.rmtree(old_dir, ignore_errors=True)
    print(f"dispatch-entrypoint: pruned {old_dir}", file=sys.stderr)
PY
    then
        printf '%s\n' "$NEW_VERSION" > "$MARKER"
    else
        log "WARNING: the pre-migration snapshot FAILED; starting anyway (marker not updated, next start retries)."
    fi
elif [ "$HAS_DATA" = "0" ]; then
    printf '%s\n' "$NEW_VERSION" > "$MARKER"
fi

# -----------------------------------------------------------------------------
# 4. Sanity warnings for configurations that will disappoint you later
# -----------------------------------------------------------------------------
if [ -n "${OPENCLAW_BIN:-}" ] && [ ! -x "${OPENCLAW_BIN}" ]; then
    log "WARNING: OPENCLAW_BIN=${OPENCLAW_BIN} is not an executable file in this container. Agent replies will fail with a clear error; chat itself is unaffected. See docs/deploy-docker.md § Agents in a container."
fi

log "data=${DATA_DIR} uid=$(id -u) gid=$(id -g) tmp=${TMPDIR}"

# -----------------------------------------------------------------------------
# 5. Hand over
# -----------------------------------------------------------------------------
# `exec` matters. Without it this shell stays PID 1 (or the child of tini) and
# SIGTERM goes to /bin/sh, which does not forward it. The app would then be
# SIGKILLed after the grace period, skipping the shutdown path that cancels
# background tasks and runs PRAGMA wal_checkpoint(TRUNCATE). You would not get
# a corrupt database — SQLite is safe — but you would leave a -wal file next
# to it every single time, and any backup taken while stopped would be
# incomplete.
exec "$@"
