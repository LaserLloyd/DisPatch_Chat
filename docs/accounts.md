# Accounts and lockout recovery

How to add, change and remove sign-ins from a terminal, and how to get back in when nobody can sign in. The
copy-paste version is in the README ([Accounts and lockout recovery](../README.md#accounts-and-lockout-recovery));
this page is the reference.

Everything here needs access to the machine (or the container, or the data folder). None of it can be reached from
the network: there is no route for it, no setting that turns it on, and the start-up hook that reads the recovery
file is called from one place only (`backend/app/main.py`, `_lifespan_startup`; a test pins that).

## The command line

```text
python -m app [--data-dir DIR] users list [--json]
python -m app [--data-dir DIR] users add <name> [--profile basic|user|admin|owner] [--password-stdin | --password-env VAR]
python -m app [--data-dir DIR] users passwd <name> [--kind pin|password] [--password-stdin | --password-env VAR]
python -m app [--data-dir DIR] users delete <name> [--yes]
python -m app [--data-dir DIR] owner reset [--yes]
python -m app [--data-dir DIR] token show | rotate
```

(`--data-dir` may also come after the verb.) Where to run it:

| Install | Command prefix |
| --- | --- |
| Native, from the clone | `cd DisPatch_Chat/backend` then `uv run python -m app ...` (Windows: `DisPatch_Chat\backend`) |
| Docker Compose | `docker compose exec app python -m app ...` |
| Docker stack | `docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env exec dispatch python -m app ...` |
| Container not running (or crash-looping) | `docker compose run --rm app python -m app ...` (a one-off container on the same volume; it publishes no port) |

Exit status is 0 on success, 1 when a command is refused (the reason is the last line on stderr) and 2 for a usage
error.

**Which data directory.** The first line of every run, on stderr, says which one it used and why:
`DisPatch data directory: <path>  (--data-dir | DISPATCH_DATA_DIR | LOCAL_CHAT_DATA_DIR | default location)`. The rule is
the server's own: `--data-dir`, else `DISPATCH_DATA_DIR`, else the legacy `LOCAL_CHAT_DATA_DIR`, else
`~/.local/share/local-chat` (`$XDG_DATA_HOME/local-chat` when that is set; on Windows
`%USERPROFILE%\.local\share\local-chat`). A folder that has no `chats.db` is refused rather than created, so a typo can
never leave a second, empty install behind.

**Secrets are never arguments.** A PIN or password would sit in your shell history and in `ps`. Each command that needs
one takes it from a hidden prompt (asked twice, when stdin is a terminal), from `--password-stdin` (the first line of
standard input) or from `--password-env VAR`. `--password foo` is a usage error. Nothing prints a secret except
`token show` / `token rotate`, whose whole job it is.

### Which credential a profile has

| Role | Signs in with | `users add` needs | `users passwd` sets |
| --- | --- | --- | --- |
| First Admin (`owner` on the command line; protected) | password, and/or the install PIN (`security.yaml`) | a password (only when there is no Admin yet) | the password; `--kind pin` the PIN |
| Admin | password | a password; the first Admin must have a password first | the password |
| User | PIN, or nothing (the shared DisPatch profile has none) | a PIN, or nothing | the PIN |
| Basic | PIN, or nothing (pick the tile) | nothing (the default); a PIN if you pass one | the PIN |

PINs are at least 4 characters and passwords at least 8 (the same rules as the Settings screens). The 3.x role words
`member` and `restricted` are still accepted and mean `user` and `basic`.

**Who may do this.** Whoever can run the command or write the data folder: it is host access, not a role. An Admin signed in to
the app can do everything the first Admin can except the structural exception, changing that profile's role or credentials; this
command line (or the recovery file) is how the first Admin's own sign-in gets reset. `users add --profile` defaults to the lowest
role (Basic), so a new sign-in gets nothing it was not asked for.

### users

- `users list` prints name, role, what each profile signs in with, and whether it is disabled. `--json` prints the
  profile records the People screen gets. A password or PIN hash never appears.
- `users add <name>` creates a profile. **Without `--profile` it is Basic**, the lowest role. A new Admin or User gets its own
  Atlas, like a profile made in the app. `--profile owner` works only on an install with no first Admin (there is exactly one;
  the command tells you to use `users passwd` or `owner reset` otherwise). Names are 1 to 40 characters and unique ignoring case.
- `users passwd <name>` sets a new credential and **signs the profile out everywhere** (its sessions and remembered
  devices). Admins have no PIN and Users/Basic profiles have no password; asking for the wrong one is refused.
- `users delete <name>` removes a profile **and everything it owns**: its chats, its own bots, pictures, files and cloned voices.
  The command names the counts and asks first on a terminal (it needs `--yes` in a script). **The first Admin cannot be
  deleted**: every install keeps exactly one. To lock that Admin out and start its sign-in again use `owner reset`.

### owner reset

For when the first Admin's password **and** PIN are both lost. It:

1. clears that Admin's password, the install PIN (`security.yaml`) and the recovery code (`RECOVERY-CODE.txt`);
2. ends that Admin's sessions and remembered devices;
3. makes a new one-time **setup token** and prints it (`SETUP TOKEN: ...`), also keeping it in
   `<data>/SETUP-TOKEN.txt` (owner-only) so the running server accepts it and the next start prints the same one;
4. writes an `owner_reset` row to the audit log.

The install is then unclaimed again, exactly like a first run: closed to the network, open to the browser on the machine
itself, and the wizard's **Your account** step gives the *existing* first-Admin row (its id, name, picture and history) a new
password. From another device, or from a browser talking to a container's published port, the wizard asks for the token.
The token file is deleted as soon as the first Admin exists. Other profiles keep their PINs and passwords, but they cannot sign in
until it is set again (an unclaimed install has no sign-in).

If you only forgot the password and still know the PIN, or the reverse, you do not need this: `users passwd <owner>` sets a
new password for that Admin without touching anything else.

### token

The machine API key (`X-API-Key`, the `api_token` in `security.yaml`) that OpenClaw, crons and the image server send.
`token show` prints it (or says none is set). `token rotate` makes a new one, prints it once more, and the old one stops
working immediately. These are the same operations as Settings, Security, API key.

### A running server follows

The command line and the server are separate processes sharing `chats.db` (WAL) and the files beside it. The server
re-checks a small revision counter in the database about once a second (`REV_RECHECK_SECONDS`), reloads its profile
cache when another process changed it, and re-reads `security.yaml`, `trusted-devices.yaml` and `SETUP-TOKEN.txt` when
their files change. So a `users passwd`, `users delete` or `owner reset` takes effect on a running DisPatch within about a
second: the old sessions of that profile stop working, with no restart. (The one exception is the recovery file, which is
read only while the server *starts*.)

## When there is no command line: the `RESET_LOGIN` file

Create a file named `RESET_LOGIN` (a Windows editor's `RESET_LOGIN.txt` also counts) **in the data directory**, then restart
DisPatch. While it starts, the server reads the file, **deletes it first**, and then acts on its first line:

| First line | Effect |
| --- | --- |
| *(empty file)* | the same as `owner reset`: owner credentials cleared, new setup token logged and written to `SETUP-TOKEN.txt` |
| `Olive` | the same, but only if `Olive` is the first Admin's name (any case); otherwise nothing is reset |
| `Olive:new-password` | `Olive`'s password is set to everything after the first colon (so it may contain colons and spaces; trailing whitespace from an editor is dropped). Creates the first Admin when there is none. Its sessions end. |

The server logs exactly one line, `login recovery: RESET_LOGIN consumed; ...`, and never the password.

Rules, so it cannot be abused or replayed:

- The file is deleted **before** it is acted on. If it cannot be deleted (a read-only mount), it is **not** acted on and
  the log says so: a file that stayed would repeat on every start.
- It must be a plain file under 1 KB. Everything is decided on one open file descriptor. A directory, a symlink (never followed), a hard-linked file, a larger file or text that is not UTF-8/UTF-16 is
  ignored, and on Linux and macOS a file that is world-writable or owned by another user (not root, not the user DisPatch
  runs as) is deleted unused. Windows PowerShell 5's UTF-16 and Notepad's UTF-8 BOM are understood.
- A refusal (wrong owner name, too-short password) still consumes the file and logs `NOT applied` with the reason.
- Nothing reads it at any other time: planting the file on a running server does nothing until the next start.

### Where the data directory is, per platform

| Install | Path |
| --- | --- |
| Docker (Compose, stack) | `/data` in the container: the `dispatch-data` volume, or your `DATA_PATH` folder. The stack's volume is `dispatch-stack_dispatch-data`. |
| Linux, macOS | `~/.local/share/local-chat` |
| systemd user service | the same, unless the unit sets `DISPATCH_DATA_DIR` |
| Windows (uv) | `%USERPROFILE%\.local\share\local-chat` |

### Docker

```bash
# container running
docker compose exec app sh -c ': > /data/RESET_LOGIN'                 # empty: open setup again
printf 'Olive:%s\n' "$NEWPW" | docker compose exec -T app sh -c 'cat > /data/RESET_LOGIN'
docker compose restart app
docker compose logs app | grep -E "login recovery|SETUP TOKEN"

# container stopped or crash-looping (a one-off container on the same volume)
docker compose run --rm app sh -c ': > /data/RESET_LOGIN'
docker compose up -d app

# bind mount (DATA_PATH): just create it on the host; the owner must be root or the container's user
sudo -u "#1000" touch "$DATA_PATH/RESET_LOGIN"
```

For the stack, replace `docker compose` with
`docker compose -f deploy/compose/stack.yml --env-file deploy/compose/stack.env` and `app` with `dispatch`. The volume's
host path (`docker volume inspect dispatch-data`) is inside Docker Desktop's VM on Windows and macOS, so use `exec`/`run` there
rather than the host path.

## `DISPATCH_RESET_OWNER`

For a container where you can edit the environment (`.env`, the stack's `stack.env`, a unit file) but not run a command:

```bash
echo 'DISPATCH_RESET_OWNER=1' >> .env && docker compose up -d      # recreates the container
docker compose logs app | grep -E "login recovery|SETUP TOKEN"
# then remove the line from .env
```

Any value except empty, `0`, `false`, `no` and `off` triggers the same as an empty `RESET_LOGIN`: it opens setup, it never sets
a password (an environment variable is visible to `docker inspect` and the process list). The value is never logged; a hash of it is recorded in
`<data>/.reset-owner-applied` when it is used, and **the same value is ignored on later starts** (the log says `already
applied`), so leaving it in a `.env` file does not wipe the owner on every restart. A different value (`2`, a date) runs it
again. Only the real process environment counts: the legacy `LOCAL_CHAT_` spelling and the settings store, which an Admin can
write over the network, are not consulted. If a `RESET_LOGIN` file is also present, the file is handled and the variable waits
for the next start. If both `RESET_LOGIN` and `RESET_LOGIN.txt` exist, the first is used and the other is deleted unused.

## What is reset, what is kept

| | `users passwd` | `users delete` | `owner reset`, empty file, variable | `Olive:pw` file |
| --- | --- | --- | --- | --- |
| That profile's credential | replaced | profile removed | first Admin's password, PIN, recovery code cleared | first Admin's password replaced |
| That profile's sessions, remembered devices | ended | ended | the first Admin's ended | the first Admin's ended |
| Other profiles and their PINs | kept | kept | kept (cannot sign in until the first Admin is back) | kept |
| Chats, roster, settings, connections, backups | kept | **the profile's own chats, bots and files go** | kept | kept |
| The first Admin's profile row (id, name, history) | kept | n/a | kept | kept |

Since 4.0 a conversation belongs to one profile, so deleting a profile deletes its conversations. Nobody else's are touched.

## Troubleshooting

- **`There is no DisPatch database at ...`**: wrong data directory, or DisPatch has never been started there. Pass `--data-dir`,
  set `DISPATCH_DATA_DIR`, or (Docker) run it inside the container; the first stderr line names the folder it looked in.
- **The file is still there after a restart**: the server did not start from this data directory, or the folder is read-only
  (`login recovery: cannot delete RESET_LOGIN ... NOT acting on it`). Check `docker compose logs app`.
- **`owned by another user; deleted without being used`**: the file was created by a different non-root user. Create it as root or as
  the user DisPatch runs as.
- **`names 'X' but this install's owner is 'Y'`**: use the first Admin's name (`users list` shows it).
- **Everyone is locked out after a reset**: that is the point of an unclaimed install; finish the setup wizard with the token and
  the other profiles can sign in again.
- **Still have the PIN-only recovery path**: the `pin:` directive and `RECOVERY-CODE.txt` described in
  [security.md](security.md) work as before.

## Tests

`backend/tests/test_cli_accounts.py` covers every verb (and that a secret is refused as an argument),
`backend/tests/test_login_recovery.py` the file and the variable (consumed, no replay, refusals, not network-triggerable) and
that a command-line change reaches a running server.
