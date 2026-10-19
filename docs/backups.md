# Backups, restore, the support bundle, and your data

Three kinds of backup sit side by side in `<data>/backups/` and are listed together in **Settings -> All settings ->
System -> Backups and recovery** (and `GET /api/backups`). All of it is host control (`admin_system`): the **Owner's and the Admins'**.

| Kind | What | Made by |
|---|---|---|
| **Encrypted archive** (`archives/dispatch-<time>.dpbk`) | Everything you need on a new computer: a consistent copy of `chats.db`, the roster and settings files, avatars, the reaction pack, the connection API keys, and (if asked) pictures and files | **Back up now**, or the schedule |
| **Snapshot** (`chats-<time>.db`) | The rotating, plain SQLite copy of the database | DisPatch, every 6 hours (`system.backup_interval`, `system.backup_keep`) |
| **Before upgrade** (`<new version>-<time>/`, e.g. `3.0.0-20261018-065040`) | The files a migration is about to change (`chats.db`, `config.yaml`, `security.yaml`, `trusted-devices.yaml`); written only when a step is pending | `uv run python -m app.migrate`, and the server on start; the newest 5 are kept |

## Encrypted archives

`.dpbk` is AES-256-GCM over a gzip'd tar in 1 MiB chunks, with the key derived from your passphrase by scrypt. The
header and each chunk's number and "last chunk" flag are part of what is authenticated, so a flipped bit, two
swapped chunks or a file cut short all fail to decrypt - with an error that says "damaged" or "cut short", and
"passphrase is wrong" only when it is. Plain bytes of your chats and keys do not appear in the file.

**The passphrase**: type one when you press *Back up now*, or set `system.backup_passphrase` (Settings -> System ->
Backups; stored in the secret store, never shown again), or do neither and DisPatch generates a **recovery key** like
`ABCDE-FGHJK-MNPQR-STUVW-XYZ23` and shows it **once**. Keep it somewhere safe: without it an archive cannot be read, by
anyone, including us.

**The recovery key is also this install's backup passphrase.** A generated recovery key is saved in the install's secret
store as `system.backup_passphrase` (as a passphrase you set in Settings is), because the schedule and a restore that
types nothing both need it. That has three consequences:

- Encryption protects an archive that is **off this computer**. Anyone who holds the install itself (the data folder
  *and* its keyring or `agents/secrets.json`) already holds the key to the archives next to it; they gain nothing from
  stealing a `.dpbk`, and the archive does not protect you from a compromised install.
- The key you were shown is the copy that matters when this computer is lost. Keep it away from it (a password manager,
  paper); an archive copied elsewhere is useless without it.
- A schedule that starts with no passphrase makes a recovery key **nobody is shown**. Set your own passphrase first
  (Settings -> System -> Backups) if you want to restore on another computer. A new passphrase applies to new archives;
  an older archive still needs the one it was made with. A passphrase typed into *Back up now* is never saved.

**The schedule**: turn on `system.encrypted_backup_enabled` (off by default). DisPatch checks every ten minutes and
writes one when the last is older than `system.encrypted_backup_interval_hours` (24), keeping the newest
`system.encrypted_backup_keep` (7). `system.encrypted_backup_include_media` adds the media and File Server folders.

## Restoring

`POST /api/backups/{id}/restore` (`{confirm: true, passphrase?}`) **stages** the restore: it decrypts and checks the
archive (checksums for every file, the database's integrity), refuses any path that is not one of DisPatch's own,
and leaves the files in `<data>/restore-staging/`. The restore is **applied at the next start**, before the database
is opened, because the database is in use while the server runs. Before applying, the current state is copied to
`backups/pre-restore-<time>/`. `DELETE /api/backups/pending` cancels a staged restore.

Offline, with DisPatch stopped:

```
python -m app.backups --data-dir /path/to/data restore dispatch-20261018-120000.dpbk --passphrase-file pass.txt
```

`POST /api/backups/{id}/verify` decrypts and checks an archive without restoring it. Connection keys inside an
archive go back into the secret store on restore, so a restore onto a new computer keeps your connections working.

## Support bundle

`POST /api/support-bundle` (Settings -> System -> *Download support bundle*) returns a zip for a bug report: versions,
every setting (secrets masked), connection and character summaries, the profiles' roles, the list of backups and the
end of the log. It **never** contains conversations, uploads or files, API keys, tokens, PINs, passwords, cookies,
the backup passphrase or recovery keys; credential-shaped text in the log is removed, and **character prompts are left
out unless you tick *Include character prompts***. Read it before you share it; the scrubbing is automatic, you know
your data best.

## One person's data

`GET /api/users/{profile_id}/export` returns everything DisPatch holds about one profile as JSON: the profile
(never a PIN or password hash), its devices and sessions, daily usage, its activity rows and the admin actions taken
on it, and the characters limited to it. `DELETE /api/users/{profile_id}` erases the profile and what is attached to
it (its activity, usage, sessions, device bindings and its place in character visibility lists); the Owner cannot be
erased, an Admin cannot touch another Admin, and nobody erases the profile they are using.

**Conversations are shared by the household in this version and are not attributed to one profile**, so they are not
part of a per-profile export; use the History export (`GET /api/export`) for the conversations.

Everything that changes data here writes an audit row (who, what, which profile - never the data).
