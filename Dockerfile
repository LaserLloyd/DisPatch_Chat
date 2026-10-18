# syntax=docker/dockerfile:1.7
# =============================================================================
#  DisPatch Chat — multi-stage container image
# =============================================================================
#  Build:   docker build -t dispatch:dev .
#  Run:     docker compose up -d
#
#  Design notes that are load-bearing (do not "simplify" these away):
#
#   * The BUILDER and RUNTIME bases must ship the same interpreter at the same
#     absolute path. A uv-created venv contains an absolute symlink
#     (.venv/bin/python -> /usr/local/bin/python3) and a pyvenv.cfg pointing at
#     `home = /usr/local/bin`. Copy that venv onto a base whose Python lives
#     somewhere else and every command dies with "required file not found".
#     ghcr.io/astral-sh/uv:python3.13-trixie-slim is built FROM
#     python:3.13-slim-trixie, so the pair below is guaranteed to match.
#     See https://github.com/astral-sh/uv/issues/7758
#
#   * uv is NOT in the runtime image. Nothing wraps uvicorn — no `uv run`, no
#     shell. uv 0.12 does forward SIGTERM correctly, so this is not about
#     signal safety any more; it is about image size (~65 MB of uv binary) and
#     about the container exiting 0 rather than 143 on a normal stop.
#
#   * `bookworm` tags are FROZEN, not gone. uv removed them in 0.10.0 but ghcr
#     still serves the last build, so `uv:python3.13-bookworm-slim` silently
#     pins you to uv 0.9.30 from February instead of failing. Use trixie.
#     https://github.com/astral-sh/uv/blob/main/changelogs/0.10.x.md
# =============================================================================


# ---------------------------------------------------------------------------
#  Stage 0: the voice add-on's source, when the build is given a local copy
# ---------------------------------------------------------------------------
# EMPTY unless the build passes a named context (VOICE=1 only):
#   docker build --build-arg VOICE=1 --build-arg VOICE_SRC=local \
#       --build-context voice-src=/path/to/DisPatch_Voice .
# That installs the add-on from a directory instead of GitHub: CI, an air-gapped
# build, a branch that is not pushed yet. Pass a clean export (`git archive HEAD`),
# not a working tree with a .venv and node_modules in it: the whole directory is
# sent to the builder.
FROM scratch AS voice-src


# ---------------------------------------------------------------------------
#  Stage 1: builder — resolve and install dependencies into /app/.venv
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# UV_COMPILE_BYTECODE=1  — precompile .pyc at build time. Costs build seconds,
#                          buys cold-start latency. On a Pi that difference is
#                          the several seconds between `up -d` and a page load.
# UV_LINK_MODE=copy      — the cache mount below is a different filesystem from
#                          /app, so uv's default hardlink strategy fails over
#                          to copying anyway and prints a warning on every
#                          build. This just silences the noise.
# UV_PYTHON_DOWNLOADS=0  — never fetch a managed interpreter; use the base
#                          image's. Without this a mismatch can leave you with
#                          a venv built against a Python the runtime lacks.
# UV_PROJECT_ENVIRONMENT — pin the venv to /app/.venv even though the project
#                          lives in /app/backend, so the runtime COPY is one
#                          predictable path.

WORKDIR /app/backend

# --- Dependency layer ------------------------------------------------------
# Copy ONLY the manifests first. This layer is invalidated by a dependency
# change and by nothing else, so ordinary code edits reuse the whole install.
#
# Astral's docs use `--mount=type=bind` for these instead of COPY. It is
# marginally leaner, but it breaks under rootless podman + SELinux
# ("Permission denied (os error 13)"). COPY is portable and the caching is
# identical, so COPY it is.
COPY backend/pyproject.toml backend/uv.lock ./

# `--locked` (not `--frozen`): assert uv.lock is in sync with pyproject.toml
# and FAIL if it is not. `--frozen` skips that check, which is how you ship an
# image whose dependencies quietly disagree with the manifest you reviewed.
#
# `--no-install-project`: install dependencies only, not this project. This
# repo sets `[tool.uv] package = false` so there is nothing to install either
# way, but stating it keeps the layer honest if that ever changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

# --- Application layer -----------------------------------------------------
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Second sync is a near-no-op for a `package = false` project, but it is what
# picks up the project itself the moment someone flips that flag.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# --- Optional: voice chat (the DisPatch Voice add-on, MIT) -----------------
# `docker compose build --build-arg VOICE=1`. AFTER the `uv sync` above, which
# is exact and would remove it again. The speech models are NOT baked in: the
# add-on's own page (Settings -> Voice) downloads them into the data volume with
# a progress bar and Cancel. ffmpeg and libopus, which the add-on shells out to,
# are installed in the runtime stage below.
#
# Which source (VOICE_SRC):
#   github (default)  the archive of VOICE_REF on GitHub; the builder needs no git.
#   local             the directory passed as `--build-context voice-src=<dir>`.
# VOICE_REF is a full ref (`refs/heads/<branch>`, `refs/tags/<tag>`) or a bare tag
# name. It defaults to the 3.0 refactor branch of the add-on, which carries the
# 1.6.0 line (one models folder, the host credential store, settings schema). The
# moving tag `v1` still names 1.5.0.
#   TODO(release): when 1.6.0 is tagged, default this to `refs/tags/v1.6.0`
#   (or `v1` once that tag moves). Move it together with js/voice-absent.js VOICE_TAG.
ARG VOICE=0
ARG VOICE_SRC=github
ARG VOICE_REF=refs/heads/refactor/prosumer-2026
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,from=voice-src,target=/voice-src \
    set -eu; \
    if [ "$VOICE" = "1" ]; then \
      if [ "$VOICE_SRC" = "local" ]; then \
        [ -f /voice-src/pyproject.toml ] || { echo "VOICE_SRC=local needs --build-context voice-src=<DisPatch_Voice checkout>"; exit 1; }; \
        uv pip install --python /app/.venv/bin/python /voice-src; \
      else \
        case "$VOICE_REF" in refs/*) ref="$VOICE_REF" ;; *) ref="refs/tags/$VOICE_REF" ;; esac; \
        uv pip install --python /app/.venv/bin/python \
          "dispatch-voice @ https://github.com/LaserLloyd/DisPatch_Voice/archive/${ref}.tar.gz"; \
      fi; \
    fi

# Strip caches the previous layers created. Saves ~10-20 MB and, more usefully,
# stops stale .pyc from a different Python landing in the runtime image.
RUN find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /app -name '*.pyc' -delete \
 && rm -rf /app/backend/tests /app/backend/turn_probe.py


# ---------------------------------------------------------------------------
#  Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie AS runtime

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED

# These labels name the upstream repository. They only affect image metadata
# (and the GHCR "linked repository" behaviour) — the build works untouched.
# To repoint them at your own fork:
#   grep -rlE 'LaserLloyd/DisPatch_Chat|ghcr.io/laserlloyd/' --exclude-dir=.git . \
#     | xargs sed -i -e 's|LaserLloyd/DisPatch_Chat|myuser/DisPatch_Chat|g' -e 's|ghcr.io/laserlloyd/|ghcr.io/myuser/|g'
LABEL org.opencontainers.image.title="DisPatch Chat" \
      org.opencontainers.image.description="Self-hosted chat for your household and your AI agents" \
      org.opencontainers.image.source="https://github.com/LaserLloyd/DisPatch_Chat" \
      org.opencontainers.image.documentation="https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/deploy-docker.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# --- System packages -------------------------------------------------------
# Deliberately almost none. Every wheel this app needs (pillow, pydantic-core,
# uvloop, httptools, watchfiles, websockets, pyyaml) publishes prebuilt
# manylinux wheels for BOTH x86_64 and aarch64, so there is no compiler and no
# libjpeg/libz here — Pillow's manylinux wheel vendors its own image codecs.
#   `tzdata` so TZ= actually resolves to a real zone instead of silently
#           staying UTC (the slim images ship no zoneinfo database).
#   `ca-certificates` for outbound HTTPS from httpx.
# util-linux (setpriv) and passwd (usermod/groupmod) are Essential in Debian
# and already present — verified, not assumed — which is why the entrypoint
# can drop privileges without vendoring gosu.
#   `ffmpeg` and `libopus0` ONLY when built with VOICE=1: the voice add-on decodes
#           the clip you upload to clone a voice with ffmpeg, and its low-bandwidth
#           link uses the Opus codec through libopus. Without them cloning fails and
#           the link stays on raw PCM (Settings -> Voice says which, with the fix).
ARG VOICE=0
RUN set -eu; \
    extra=""; \
    if [ "$VOICE" = "1" ]; then extra="ffmpeg libopus0"; fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates $extra \
 && rm -rf /var/lib/apt/lists/*

# --- Non-root user ---------------------------------------------------------
# UID/GID 1000 on purpose. It is the first non-system id on essentially every
# desktop Linux and Raspberry Pi OS install, so a bind-mounted host directory
# owned by the person running `docker compose up` is writable with no further
# ceremony. Picking 999 or a random high id is "more correct" and generates a
# permission-denied support thread for every single Linux user.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

# --- Application -----------------------------------------------------------
COPY --from=builder --chown=app:app /app/.venv   /app/.venv
COPY --from=builder --chown=app:app /app/backend /app/backend
COPY --from=builder --chown=app:app /app/frontend /app/frontend
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# .gitattributes pins `*.sh` to LF (`text eol=lf`), so a checkout on Windows
# (core.autocrlf=true) no longer hands the build a CRLF script. This guard stays: a
# CRLF shebang ("#!/bin/sh\r") only shows up at container start as
# "exec /usr/local/bin/docker-entrypoint.sh: no such file or directory", so a build
# from a copy that bypassed git (a hand-made archive) fails HERE, with the reason.
RUN head -n1 /usr/local/bin/docker-entrypoint.sh | grep -qx '#!/bin/sh' \
 || { echo "docker-entrypoint.sh has CRLF line endings; check out with .gitattributes (*.sh text eol=lf)"; exit 1; }

# Putting the venv's bin first is what lets us call `uvicorn` with no wrapper.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- Defaults --------------------------------------------------------------
# These make `docker run` with no flags produce a working, sensible server.
#
# Deliberately the LEGACY LOCAL_CHAT_ spelling, not the documented DISPATCH_
# one: the app resolves DISPATCH_FOO before LOCAL_CHAT_FOO, so a DISPATCH_
# default baked into the image would outrank — and silently ignore — a
# LOCAL_CHAT_ value an existing install passes in. Image defaults go on the
# lower-priority name so that an override in either spelling beats them.
ENV LOCAL_CHAT_DATA_DIR=/data \
    LOCAL_CHAT_HOST=0.0.0.0 \
    LOCAL_CHAT_PORT=8765 \
    TMPDIR=/data/tmp \
    DISPATCH_IMAGE_VERSION=${VERSION}

# NOTE ON AVATARS: uploaded avatars are user data and are stored in the data
# directory (/data/avatars), so the data volume already carries them and no
# bind mount over the source tree is required.
# A symlink would NOT have worked — Starlette's StaticFiles resolves real paths
# and 404s anything escaping the static root (verified). The app instead mounts
# /static/avatars from the data directory ahead of the general /static mount,
# which keeps the URL identical.
# Host-OS integrations, OFF in a container because they drive the host's
# systemd and the host's files. See docs/deploy-docker.md § "What does not
# work in a container". Turning them on here produces broken UI, not features.
ENV LOCAL_CHAT_MIRROR=0

# NOTE: no `VOLUME /data`. A VOLUME instruction would silently create an
# anonymous volume for anyone who forgets to mount one — their data would
# survive a restart, look fine for months, and then vanish the first time they
# ran `docker compose down -v` or pruned. An unmounted container losing state
# on removal is the honest, obvious failure. Compose mounts it explicitly.
RUN install -d -o app -g app -m 0750 /data /data/tmp /data/avatars

WORKDIR /app/backend
USER app
EXPOSE 8765

# --- Healthcheck -----------------------------------------------------------
# /api/health is deliberately un-gated (it is in the app's open-path set), so
# this works whether or not a PIN is configured.
#
# Uses the venv's Python rather than curl/wget — neither is installed, and
# adding one for a healthcheck is 1-2 MB and an extra CVE surface for nothing.
# Checks the JSON body, not just the status code: an HTTP 200 from a process
# that is up but has a broken database is exactly the state a healthcheck is
# supposed to catch.
#
# `db_integrity_ok` is checked but `gateway_ok` deliberately is NOT — the
# agent backend is optional, and a container that reports unhealthy because
# you chose not to run agents would be actively misleading.
#
# start-period is 90s (matched in docker-compose.yml — keep them in step): first boot seeds the reaction starter pack, runs the
# schema migration and does crash recovery. On a Pi 4 with an SD card that can
# genuinely take half a minute, and a short start-period turns it into a
# restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "\
import json,sys,urllib.request;\
r=urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=4);\
d=json.load(r);\
sys.exit(0 if r.status==200 and d.get('status')=='ok' and d.get('db_integrity_ok') is not False else 1)"]

# --- Process ---------------------------------------------------------------
# STOPSIGNAL is SIGTERM by default; stated explicitly because the shutdown
# path matters here. On SIGTERM uvicorn drains connections, the lifespan
# teardown cancels background tasks, runs PRAGMA wal_checkpoint(TRUNCATE) to
# fold the WAL back into chats.db, and closes the database. Miss that and you
# leave a -wal file behind on every stop.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# EXEC FORM, NOT SHELL FORM. `CMD uvicorn app.main:app ...` (shell form) runs
# `/bin/sh -c "uvicorn ..."`, and sh does not forward signals to its child:
# SIGTERM kills the shell, uvicorn never hears it, Docker waits out the 10s
# grace period and SIGKILLs — no drain, no checkpoint. The bracket syntax is
# the entire difference.
#
# --timeout-graceful-shutdown 20 must stay BELOW compose's stop_grace_period
# (30s), or the checkpoint gets SIGKILLed halfway through.
#
# NO --workers. Unlock sessions, WebSocket clients, rate-limit counters and
# the backup/mirror loops all live in process memory. A second worker means
# unlocking in one tab and staying locked in another, two processes writing
# the same SQLite file, and duplicated backup jobs.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8765", \
     "--no-access-log", \
     "--timeout-graceful-shutdown", "20"]
