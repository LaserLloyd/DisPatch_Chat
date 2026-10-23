# Daily maintenance, storage and retention

DisPatch keeps itself fast over years of use with one small task that runs inside the server once a
day. It deletes **no chat and no picture** unless an Admin turns that on. This page is the reference
for what it does, how to change it, how to undo an image expiry, and what each number on the
Storage card means.

Designed for 20 profiles over three years: about one to two million messages in one SQLite file,
without a slowdown. The query side (indexes, paged lists) is described in `docs/architecture` /
the migration notes; this page covers the housekeeping.

## When it runs

* Every day at **04:30 local time** (`system.maintenance.time`). That is after the 02:00-02:40
  backup window and clear of the 04:00-04:45 operator crons that call the API.
* **It waits** (and tries again every 30 minutes) while a backup is being written or the operator
  maintenance marker `~/.local/state/openclaw/maintenance` exists. If the marker appears in the
  middle of a run, the run stops before its next step and records why.
* A run that was missed (the server was off at 04:30) happens ten minutes after the next start.
* An Admin can run it on demand: **Settings → System → Storage → Run now**
  (`POST /api/maintenance/run`), or `uv run python -m app.maintenance run [--dry-run] [--step NAME]`.
* One run at a time (a process-wide lock). It uses its own database connection with a busy
  timeout and deletes in **batches of 5,000 rows** with a short pause between them, so a request
  never waits longer than one batch. Every step is isolated: a failing step is recorded with its
  error and the next step still runs.

Turn it off with `system.maintenance.enabled` (off keeps everything exactly as it is, forever).

## What it does, in order

Every step writes one row to the `maintenance_runs` table (rows, bytes, milliseconds, error), plus
one `run` summary row. Defaults are Admin-editable in Settings > System (`system.retention.*`).

| Step | Default |
|---|---|
| `sync_media_index` | keeps the "is this picture still shown?" index (`media_refs`) current from the messages added since the last run (a backstop: DisPatch also writes `media_refs` when a message is saved or edited), and records which `media/` files were generated |
| `prune_activity` | activity rows older than 90 days; audit rows older than 400 days |
| `prune_task_events` | event logs of finished agent tasks older than 90 days |
| `prune_job_events` | Job Board event rows of closed jobs older than 60 days |
| `prune_usage_daily` | machine-upload counters older than 45 days |
| `prune_transcript_seen` | agent-mirror de-duplication rows older than 180 days |
| `prune_sessions` | expired and long-idle sessions, and devices idle for longer than the remember window (at least 35 days). A device still inside its remember-me window is kept |
| `prune_agent_spend` / `prune_approvals` / `prune_job_feedback` | 400 days / decided approvals older than 180 days / feedback on closed jobs older than 180 days |
| `archive_chats` | **off.** A profile can ask for "archive my chats older than N months" for itself (Settings > You > Account, `GET/PUT /api/me/archive`). The task then sets `is_archived` on that profile's aged chats. Never deletes. See below |
| `image_retention` | **forever (does nothing).** See below |
| `prune_image_jobs` | finished, failed and cancelled picture requests older than 90 days |
| `orphan_sweep` | files nothing refers to, older than 7 days, move to `trash/orphans-<date>/` |
| `trash_purge` | trash folders past their hold are deleted: expired pictures after 14 days, swept orphans after 14 days |
| `backup_tiers` | database snapshots thinned to 8 recent / 7 daily / 4 weekly (never fewer than 4); pre-migration copies keep the newest 3 |
| `fts_maintenance` | a bounded merge of the search index nightly (many short write locks); a full optimize on Sundays for a small index, an extended merge for a large one |
| `sqlite_optimize` | `PRAGMA optimize` |
| `incremental_vacuum` | hands freed pages back to the filesystem a few thousand at a time (needs `auto_vacuum = INCREMENTAL`, which migration 011 sets) |
| `wal_checkpoint` | `PRAGMA wal_checkpoint(TRUNCATE)` last, retried once if a reader blocks it |
| `storage_scan` | measures the data folder for the Storage card (measures only) |

The settings keys are `system.retention.activity_days`, `audit_days`, `task_event_days`,
`job_event_days`, `usage_days`, `transcript_seen_days`, `image_job_days`, `agent_spend_days`,
`approval_days`, `job_feedback_days`, `orphan_grace_days`, `orphan_trash_days`, the three image keys
below, and `system.backups.keep_recent`, `keep_daily`, `keep_weekly`.

### Overlap with the box crons

Two operator jobs also touch the database; neither does what this task does, and neither runs at
04:30. `local-chat-daily` (02:00) creates the day's thread, rotates avatars and deletes
zero-message daily threads; `dispatch-daily-cleanup` (03:00) deletes zero-message daily threads
again. Neither prunes event tables, vacuums, checkpoints, or touches pictures. Both keep working
unchanged.

## Generated images: keep forever, or N months

Settings > System > **Generated images: keep [Forever | N months]** (Admin only).

* **Forever is the shipped default**: the step does nothing.
* **N months** (1 to 120; the page offers **12** once "N months" is chosen) applies to every
  generated picture, with one rule:
  * reaction pictures that were already shown (`reactions/spent`)
  * Image Studio results
  * pictures made in chats (`[[pic:...]]`, the image jobs' output)
  * spent avatar-pool pictures
  * thread avatar snapshots no thread uses any more
* **Age.** A picture is old when the newer of *its own date* and *the newest message that shows
  it* is older than the cutoff. A picture in a live conversation is never stripped from under it;
  a picture whose last message is older than the window is.
* **Never expired** (the in-use set): the current avatars of characters and profiles; the avatar
  pool's unused pictures; the **unfired reaction shelf**; any picture a message newer than the
  cutoff still shows; any picture a queued or running picture job reads; the avatar snapshot a thread
  points at; everything under `static/`; and any file in `media/` that DisPatch did not record as
  generated (an upload, a pasted picture, an agent's attachment).
* **Preview before saving.** `GET /api/maintenance/images/preview?mode=months&months=12` returns
  what the rule would expire right now (counts and bytes per kind, what was protected and why, the
  oldest and newest). The Settings card shows it and enables Save only after. The nightly step
  uses the same function, so the preview is exactly what the job then does.
* **Placeholder.** An expired picture's name goes into `media_expired`. `/media/<name>` then
  answers **410 Gone** with `X-Image-Expired: 1` (not 404), and the page shows a small "image
  expired" box in chats, Image Studio and the reaction history. Messages are never edited.

### Undoing an expiry (the trash)

Nothing is deleted at once. Files move (an instant rename) to
`<data>/trash/images-<YYYYMMDD>/` keeping their path below the data folder, for example
`trash/images-20261101/media/<name>.png` or
`trash/images-20261101/reactions/spent/<mood>/<file>.png`. They stay there for
`system.retention.images.trash_days` (default 14), then the next run deletes them for good.

To put them back, inside the hold:

* Admin: `POST /api/maintenance/images/restore` with `{"date": "20261101"}` (and optionally
  `"names": [...]`), or
* from a shell: `uv run python -m app.maintenance restore-images [--date 20261101] [--name <name>]`, or
* by hand: move the files from `trash/images-<date>/<path>` back to `<data>/<path>`, and delete
  their rows from `media_expired` (`DELETE FROM media_expired WHERE name = '<name>'`).

Switching the setting back to **Forever** stops further expiry; it does not restore what already
moved.

## Archiving old chats (optional, per profile)

Off for everyone. A profile sets "Archive my chats older than [off | N months]" for **itself**: the
route only ever reads and writes the caller's own value, and no Admin can set it for someone else.
The task archives the chats of that profile whose last message **and** last read are older than N
months. A pinned chat, a chat that is thinking, and a chat with a running picture job or open agent
task are left alone. Archived chats leave the rail and the first page of `hello`, stay searchable,
open by link, and are listed in the **Archived** view; opening one and un-archiving it sticks.
Nothing is deleted. `GET /api/me/archive?months=N` says how many chats a value would archive.

## Orphan sweep

Moves to `trash/orphans-<date>/` (kept 14 days, then deleted):

* `media/` files that no message, generated-picture record, Image Studio row, image job or file
  record refers to;
* `files/` blobs with no `files` row;
* `avatars/` files no character and no profile uses.

Only files older than 7 days move. File **records** whose blob is missing are reported in the run
(never deleted). A sweep that would move more than 20% of a store (and more than 200 files) refuses:
that reads as a gap in the index, not as orphans.

## Database snapshots

The server writes a snapshot every 6 hours (`chats-<stamp>.db`). At a year's size a snapshot is
1-2 GB, so "keep 12" would hold ~24 GB. The task keeps tiers instead:

* **recent**: the newest 8 (two days at 6-hourly)
* **daily**: the last snapshot of each of the newest 7 days
* **weekly**: the last snapshot of each of the newest 4 weeks older than the dailies
* never fewer than 4 in total, and an empty or non-database file never counts

A daily or weekly keeper is **hard-linked** into `backups/tiers/daily/` or `backups/tiers/weekly/`
(no extra disk) which is also what keeps the server's own newest-N rotation from removing it. They
are listed in Settings > System > Backups with their tier and restore like any snapshot.
Pre-migration copies keep the newest 3.

## The Storage card

`GET /api/maintenance/storage` returns the last measurement (the data folder is walked by the
nightly job or by **Refresh**, never per request): bytes and files per area, free disk, the
snapshot count per tier, and the stray `*.bak*` files in the data folder. Stray files are **listed
only**; the task never deletes them.

| Area | What it is |
|---|---|
| `database`, `wal` | `chats.db` (+ `-shm`) and its write-ahead log; `database_fts_bytes` is the search index's share of the file |
| `media` | uploads and generated chat pictures |
| `imagestudio` | Image Studio results |
| `reactions_spent`, `reactions_shelf` | fired reaction pictures; the rest of `reactions/` (shelf, pack, builtin) |
| `avatars`, `avatar_pool` | current avatars and thread snapshots; the avatar pool |
| `files`, `voices` | File Server blobs; cloned voices |
| `backups_6h`, `backups_daily`, `backups_weekly`, `backups_other` | snapshots by tier; archives and pre-migration copies |
| `trash` | expired pictures and swept orphans waiting for their hold |
| `stray_bak` | `*.bak*` files at the top of the data folder |
| `other` | everything else |

A hard-linked file is counted once, so the total matches `du`.

## API (all Admin only; Users and Basic profiles get 403)

| Route | Purpose |
|---|---|
| `GET /api/maintenance/status` | schedule, next run, running/blocked, last run with per-step rows/bytes/ms/error, the image rule |
| `GET /api/maintenance/storage` | the cached numbers above |
| `POST /api/maintenance/storage/refresh` | start a new measurement (202) |
| `POST /api/maintenance/run` | `{"dry_run": true}` answers at once with each step's count; otherwise starts a run (202) |
| `GET /api/maintenance/images/preview?mode=months&months=12` | the dry-run count |
| `GET` / `PUT /api/maintenance/retention` | every retention setting with its default; `PUT` takes `{"system.retention.images.mode": "months", ...}` and refuses a bad value with 422 |
| `POST /api/maintenance/images/restore` | `{"date": "YYYYMMDD", "names": [...]}` put pictures back from the trash |
| `GET` / `PUT /api/me/archive` | the caller's own archive-after value (any signed-in profile that manages its history) |

## Reading a failed step

Settings → System → Storage shows each step of the last run; a step with an error is red with the
message. The same rows are in the `maintenance_runs` table. Common causes: a locked database for
longer than the busy timeout (the next run retries), a read-only data folder, a full disk.
