# The DisPatch + StudioForge stack (Docker and Podman)

One compose file, [`deploy/compose/stack.yml`](../../deploy/compose/stack.yml), brings up:

| Service | Image | Role | Host port (default, loopback) |
| --- | --- | --- | --- |
| `studioforge` | `ghcr.io/laserlloyd/studioforge:<v>-cuda` | The GPU-only LLM server | 1234 gateway, 8080 panel |
| `dispatch-seed` | the DisPatch image | One-shot: writes DisPatch's first connection into a fresh data volume | none |
| `dispatch` | `dispatch-chat:local` (built from this checkout) or `ghcr.io/laserlloyd/dispatch:<v>` | The chat workspace | 8765 |
| `kiwix`, `infoforge-seed`, `infoforge` | Kiwix's image, `ghcr.io/laserlloyd/infoforge` | Optional offline knowledge, **profile `knowledge`** | none (kiwix is private to the network) |

They share one private network (`dispatch-stack`). DisPatch reaches the model server at
`http://studioforge:1234/v1`; StudioForge's panel is wired into DisPatch's StudioForge pane.

## Start

```bash
cp deploy/compose/stack.env.example deploy/compose/stack.env      # set STACK_MODEL
docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env up -d
docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env ps   # all "healthy"
```

Open <http://localhost:8765> and finish the **setup wizard**: a fresh container is closed to the
network until an owner exists, and the wizard asks for the one-time setup token printed by
`docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env logs dispatch`
(`SETUP TOKEN: ...`). Then drop your GGUF files in the folder `STUDIOFORGE_MODELS` points at
(default `deploy/compose/models/`) and load one from StudioForge's panel at <http://localhost:8080>.
With the optional knowledge stack: `… --profile knowledge up -d`, then `… run --rm infoforge doctor`.

What `up` does: StudioForge starts (read-only root, data on a volume, an owner account created from
`SF_OWNER_USER` / `SF_OWNER_PASSWORD` on a fresh volume) and must report `healthy`; `dispatch-seed`
writes `/data/config.yaml` **only if it does not exist** (the Atlas bot with one `llamacpp` provider,
`base_url: http://studioforge:1234/v1`, `model: $STACK_MODEL`, which DisPatch's first start turns into
a Connection of type **StudioForge**); then DisPatch starts. You can change the connection later in
**Settings, Connections**; the seed never overwrites it.

**Give the connection a key.** Once StudioForge has an owner (every 2.x image on a published port),
every request to it except `/health` needs an API key, so the seeded connection's **Test** answers
"the provider rejected the API key" until you create one: sign in to the panel as the owner (the
password you set, or `docker compose … exec studioforge cat /data/owner-password.initial`), create a
key under **Users & Keys** (or `docker compose … exec studioforge studioforge keys create --name dispatch
--scope inference`; the token is printed once) and paste it into **Settings, Connections,
StudioForge, API key**. The `1.26-09-23` images have no owner bootstrap and need no key; they also
answer `/api/version` without `product`, so DisPatch labels them "LM Studio" in **Find servers**.

DisPatch's `.env` (the one next to `docker-compose.yml`) is read too, if present.

## GPU passthrough on the `studioforge` service

The shipped file reserves NVIDIA GPUs (what StudioForge drives today):

```yaml
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: all, capabilities: [gpu]}]
```

| Runtime | How |
| --- | --- |
| **Docker + NVIDIA** | The block above; needs the NVIDIA Container Toolkit (Docker Desktop: built in). |
| **Podman + NVIDIA** | Generate a CDI spec once (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`). `podman-compose` turns the reservation into `--device nvidia.com/gpu=all` itself; or replace the block with `devices: ["nvidia.com/gpu=all"]` and `security_opt: [label=disable]`. |
| **AMD ROCm / Vulkan** | Replace the block with `devices: ["/dev/kfd:/dev/kfd", "/dev/dri:/dev/dri"]` and `group_add: ["<video gid>", "<render gid>"]`, and use the `-rocm` / `-vulkan` image. These images boot but do not serve yet (StudioForge is NVIDIA-only today). |
| **No GPU (smoke test)** | Delete the `deploy:` block of `studioforge` (or `docker compose … -f stack.yml -f nogpu.yml` with `deploy: !reset null`). Every service still starts; StudioForge's `/health` reports `can_serve=false`. |

## Your data survives updates

- Four named volumes hold all state: `dispatch-data` (`chats.db` + WAL, `advisor.db`, avatars,
  uploads, `config.yaml`, `backups/`), `studioforge-data` (config, registry, `engines/`, logs),
  `infoforge-config`, `infoforge-state`. Nothing stateful is written inside an image, and `dispatch`,
  `studioforge`, `kiwix` and `infoforge` run with **read-only root filesystems** so a leak fails
  loudly.
- `docker compose pull && up -d`, a rebuild, `stop`/`start` and `down` all keep the volumes.
  **`docker compose down -v` (and `docker volume rm`) destroys them.**
- SQLite durability: the volumes are real filesystems, never tmpfs; `stop_grace_period` is 30 s for
  DisPatch (its WAL checkpoint on SIGTERM; `--timeout-graceful-shutdown 20` stays below it) and 60 s
  for StudioForge. Do not put the DisPatch volume on NFS/SMB.
- **Pre-migration snapshots.** On the first start of a *new image version*, each entrypoint copies the
  databases (SQLite online-backup, WAL-safe) and `config.yaml` to
  `<volume>/backups/<oldversion>-<UTC time>/` before the new version migrates, and keeps the newest 5
  (`DISPATCH_PREMIGRATE_KEEP`, `SF_PREMIGRATE_KEEP`). DisPatch's own scheduled snapshots
  (`backups/chats-*.db`) continue as before. The version comes from the image's `VERSION` build arg
  (`ghcr.io` images set it; a local build sets it from `VERSION` in `stack.env`, `dev` falls back to
  the app's own version).
- UID consistency: both apps run as uid/gid 1000. For a **bind mount** (`DATA_PATH`-style) owned by a
  host user other than 1000, start the service as root once with `PUID`/`PGID` set (DisPatch and
  StudioForge entrypoints `chown` then drop privileges), or `chown` the directory.

## Backup and restore

Stop first (a clean stop checkpoints the WAL), archive, start:

```bash
P="docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env"
$P stop dispatch studioforge
for v in dispatch-stack_dispatch-data dispatch-stack_studioforge-data; do
  docker run --rm -v $v:/data:ro -v "$PWD":/backup alpine tar czf /backup/$v.tgz -C /data .
done
$P start studioforge dispatch
```

Restore into fresh volumes (the stack is down):

```bash
docker volume create dispatch-stack_dispatch-data
docker run --rm -v dispatch-stack_dispatch-data:/data -v "$PWD":/backup:ro alpine \
  sh -c 'tar xzf /backup/dispatch-stack_dispatch-data.tgz -C /data && chown -R 1000:1000 /data'
```

(volume names are `<project>_<volume>`; the project is `dispatch-stack`.) With Podman use `podman`
in place of `docker` and add `:z` to bind mounts on SELinux hosts. Never copy `chats.db` alone out of
a running instance: the `-wal` file holds the recent writes.

## Podman

`podman-compose -f deploy/compose/stack.yml up -d` works; known differences:

- Build images with `--format docker` so `HEALTHCHECK` survives; healthchecks need a systemd user
  session, and without one `depends_on: service_healthy` never completes.
- `read_only` + `tmpfs` are supported (the home tmpfs uses `mode=1777`, not `uid=`).
- Rootless + `kiwix`: `KIWIX_USER=0`.

## Known limits

- DisPatch's `advisor.lookup: kind: infoforge` runs the `infoforge` CLI **inside the DisPatch
  container**, which this image does not contain. The `knowledge` profile gives you kiwix and the
  CLI as a separate container (`compose run --rm infoforge …`); wiring the two is the InfoForge
  integration work, not part of this file.
- StudioForge's `-cuda` image has no compiler: its first start needs an engine supplied or built
  once (see its [docker guide](https://github.com/LaserLloyd/StudioForge/blob/main/docs/deploy/docker.md)).
- Running the stack on a LAN: set `BIND_ADDR=0.0.0.0` only after DisPatch has an owner (the setup
  wizard) and the StudioForge connection carries an API key (above).
- **Find servers** in DisPatch probes loopback and the Docker host, not the compose hostname: use
  **Add by address** with `http://studioforge:1234/v1` (or keep the seeded connection) to add the
  stack's own StudioForge.
