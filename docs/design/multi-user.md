# DisPatch Chat — Multi-User Architecture Design

*Design for migrating an existing single-user install from PIN + Safe Mode to real
accounts, ahead of a public open-source release. Written against the code as of
2026-08-03.*

[← Back to the README](../../README.md) · Companion document:
[Remote Access & Authentication Design](remote-access-and-auth.md)

> **Historical note (2026-08-26).** The local ComfyUI feature was removed from
> DisPatch entirely: this box (AMD/ROCm) cannot run ComfyUI, and ClawForge on
> the remote rig is now the only image path. Every mention below of a ComfyUI
> service panel, its systemd control, `_require_comfy`, `_broadcast_comfy_state`
> or the `/api/comfy/*` routes describes code that no longer exists. The
> analysis is left intact because the *tier* reasoning it illustrates still
> applies to the surviving host-control surfaces (the harness pane).
>
> **Historical note (2026-09-09).** The coding terminal was removed the same
> way: `backend/app/terminal.py`, `/api/terminal/*`, `WS /ws/terminal`,
> `_require_terminal`, `terminal_ws` and the `terminal_state` frame are gone.
> Every mention of them below is likewise a description of code that no longer
> exists; the owner-only reasoning transfers to the harness pane unchanged.

---

## 0. Framing: what "multi-user" can and cannot mean here

Before any DDL, one thing has to be said out loud, because it determines how
strong every boundary below can honestly claim to be:

**A DisPatch "bot" is an OpenClaw CLI subprocess running as the host user.**
`openclaw.send_to_agent` spawns `openclaw agent --agent <id> --message-file …`
with the host's tools, filesystem, credentials and MCP servers. Anyone who can
send a message to a bot can ask that bot to read `~/.ssh`, list another user's
files, or (via the terminal bot) touch a PTY.

Therefore:

- Multi-user in DisPatch is **multi-account, not multi-tenant**. It stops family
  members reading each other's conversations. It does **not** sandbox them from
  the machine.
- The privilege boundary that actually matters is **which bots a role may talk
  to**. A bot with a shell/file toolchain must be restricted to trusted roles,
  the same way `safe:` restricts bots today.
- Host-control surfaces (the coding terminal, ComfyUI systemd control, raw
  OpenClaw transcript access) stay **owner-only**. Not admin. Owner.

This must go in the README verbatim before release. Shipping a login screen that
implies isolation the architecture cannot deliver is the single worst outcome of
this project.

### What exists today (baseline)

| Concern | Today |
|---|---|
| Identity | none — one PIN, `auth._sessions: dict[token, _Session]`, in memory |
| Tiers | full session vs. **Safe Mode** ("decoy"), anonymous, LAN-reachable |
| Authorization | one HTTP middleware `auth_gate` + a path-prefix table `_decoy_blocked` + in-handler `_deny_decoy_*` |
| Ownership | none. `threads`, `messages`, `files` have no user column |
| WS | `ws.py:ConnectionManager.broadcast` → **every frame to every socket**, filtered by one global `manager.redactor = redact_for_decoy` |
| Agent sessions | `openclaw.session_key_for` → `agent:<bot_id>:<thread_id>` |
| Machine callers | loopback-exempt + one global `security.yaml: api_token` |

Eight separate mechanisms implement the Safe-Mode tier: `auth_gate`,
`_decoy_blocked`, `_deny_decoy_bot`, `_deny_decoy_thread`,
`_deny_decoy_mutation`, `_is_safe_mode_caller`, `redact_for_decoy`,
`_block_fileserver_read`, plus `_gated_avatar_static` / `_safe_avatar_static`
and the decoy upload quota. That count is itself the argument for §5.

---

## 1. Data model

### 1.1 Principles

1. **Additive DDL only.** Every change is `CREATE TABLE` or
   `ALTER TABLE … ADD COLUMN`. No table rebuilds, no column drops. Consequence:
   **the pre-migration binary still runs on a migrated database** (it ignores
   the new columns). That is the rollback story, and it is worth the small
   amount of dead weight left behind.
2. **The old columns stay.** `threads.last_read_at` and `threads.is_pinned` are
   copied into a new per-user table and then left in place, untouched, as a
   recovery source.
3. **Ownership is nullable and backfilled**, never `NOT NULL`, so a partially
   migrated DB is still readable.
4. SQLite constraint to respect: `ALTER TABLE … ADD COLUMN` **cannot** add a
   `UNIQUE`/`PRIMARY KEY` column, cannot add `CHECK`, and a column with a
   `REFERENCES` clause **must** default to `NULL` while `PRAGMA foreign_keys=ON`
   (which `Database.connect` sets). All DDL below obeys this.

### 1.2 New tables

Append to `database.py:SCHEMA` (it is already `CREATE TABLE IF NOT EXISTS`
throughout, so it stays idempotent).

```sql
-- ---------------------------------------------------------------- users ----
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,          -- uuid4
    username      TEXT NOT NULL UNIQUE,      -- ALREADY casefolded by the caller
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member'
                  CHECK (role IN ('owner','admin','member','guest')),
    -- 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>' — same primitive as  -- scrub-ok: hash FORMAT, no secret
    -- auth._hash_pin, self-describing so iterations can be raised later.
    password_hash TEXT,
    avatar        TEXT,                      -- filename under frontend/static/avatars/
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    -- Bumped on password change / role change / "sign out everywhere".
    -- Every live session carrying a lower epoch is invalid. See §4.6.
    session_epoch INTEGER NOT NULL DEFAULT 0,
    settings      TEXT                       -- JSON: per-user UI prefs
);
```

`username` is stored **already casefolded** by `users.create()`. Do **not** rely
on `COLLATE NOCASE` — SQLite's NOCASE is ASCII-only and would let `ALEX` and
`JAKÉ`-adjacent Unicode collide or not collide unpredictably. `display_name`
carries the presentation form.

```sql
-- ------------------------------------------------------------- sessions ----
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash   TEXT PRIMARY KEY,   -- sha256(token) hex; token lives only in the cookie
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    epoch        INTEGER NOT NULL DEFAULT 0,   -- snapshot of users.session_epoch
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT,               -- NULL = idle-expiry only (non-persistent)
    persistent   INTEGER NOT NULL DEFAULT 0,   -- "keep this device signed in"
    user_agent   TEXT,
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON user_sessions(expires_at);
```

This **supersedes `trusted-devices.yaml`**. Persistence is now the norm, not the
opt-in: today `auth._sessions` is in-memory and "a server restart logs everyone
out, which is the safe default for a lock" — correct for one owner, wrong for
a household whose phones get logged out every time the unit restarts.
`remember_device_days` survives as the `expires_at` window for `persistent=1`
sessions.

```sql
-- --------------------------------------------------------- thread access ----
CREATE TABLE IF NOT EXISTS thread_members (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    access    TEXT NOT NULL DEFAULT 'writer'
              CHECK (access IN ('writer','reader')),
    added_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (thread_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_members_user
    ON thread_members(user_id, thread_id);

-- ------------------------------------------------- per-user thread state ----
-- Read state and pinning are PERSONAL. threads.last_read_at / is_pinned are
-- single-user concepts and become per-user here.
CREATE TABLE IF NOT EXISTS thread_user_state (
    thread_id    TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    last_read_at TEXT,
    is_pinned    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tus_user ON thread_user_state(user_id);

-- ------------------------------------------------------------- invites -----
-- No email, no SMTP. The owner generates a code and hands it over in person /
-- by signal/QR. Reuses auth._RECOVERY_ALPHABET so codes are dictatable.
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,        -- sha256(normalised code)
    role       TEXT NOT NULL
               CHECK (role IN ('admin','member','guest')),  -- never 'owner'
    created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    revoked_at TEXT
);

-- ---------------------------------------------------------- api tokens -----
-- Replaces the single global security.yaml:api_token. Every machine caller now
-- resolves to a USER, so an injected message has an owner.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'inject'
                 CHECK (scope IN ('inject','full')),
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- -------------------------------------------------------------- audit ------
-- Small, append-only, owner-readable. A family app with shared history needs to
-- be able to answer "who deleted that thread". 10 columns, no framework.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor_id   TEXT,                  -- NULL = system/agent
    action     TEXT NOT NULL,         -- 'thread.delete','user.role','files.wipe',…
    target     TEXT,                  -- thread id / user id / file id
    detail     TEXT                   -- JSON, small
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);

-- ---------------------------------------------------------- migrations -----
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note       TEXT
);
```

### 1.3 Altering the existing tables

```sql
-- threads -------------------------------------------------------------------
ALTER TABLE threads ADD COLUMN owner_id   TEXT REFERENCES users(id);
ALTER TABLE threads ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
      -- 'private' | 'house'   (explicit per-person sharing = thread_members)
ALTER TABLE threads ADD COLUMN origin     TEXT NOT NULL DEFAULT 'chat';
      -- 'chat' | 'daily' | 'mirror' | 'import'  — provenance, drives defaults

CREATE INDEX IF NOT EXISTS idx_threads_owner    ON threads(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_vis      ON threads(visibility, updated_at DESC);

-- messages ------------------------------------------------------------------
ALTER TABLE messages ADD COLUMN author_id TEXT REFERENCES users(id);
      -- set for role='user'; NULL for assistant/system

-- files ---------------------------------------------------------------------
ALTER TABLE files ADD COLUMN owner_id   TEXT REFERENCES users(id);
ALTER TABLE files ADD COLUMN thread_id  TEXT REFERENCES threads(id);
      -- chat attachments: the thread they were posted into (inherit its ACL)
ALTER TABLE files ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
      -- 'private' | 'house' | 'thread'  ('thread' ⇒ follow files.thread_id)
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id, created_at DESC);
```

`ON DELETE` is deliberately omitted on `threads.owner_id` / `files.owner_id`:
deleting a user must **not** cascade away the family's chat history. Deleting a
user re-owns their threads (see §1.6).

### 1.4 The one predicate that matters

Everything in §3 reduces to a single SQL fragment. Add it next to the existing
`Database._UNREAD_SQL` — same idiom, same file:

```python
class Database:
    # A thread is visible to :viewer when they own it, it is house-wide, or
    # they hold an explicit membership row. Admin/owner bypass is applied by
    # the CALLER passing viewer_id=None (see visible_clause()).
    _VISIBLE_SQL = """
        (t.visibility = 'house'
         OR t.owner_id = :viewer
         OR EXISTS (SELECT 1 FROM thread_members tm
                     WHERE tm.thread_id = t.id AND tm.user_id = :viewer))
    """

    @staticmethod
    def visible_clause(viewer_id: str | None) -> tuple[str, dict]:
        """('', {}) for an unrestricted (admin/owner/system) reader, else the
        predicate + its bound parameter. Callers MUST pass viewer_id
        explicitly — see §7 step 3 on why it is a required positional arg."""
        if viewer_id is None:
            return "", {}
        return Database._VISIBLE_SQL, {"viewer": viewer_id}
```

And the unread subquery becomes per-user:

```python
    _UNREAD_SQL = """
       (SELECT MIN(m.created_at) FROM messages m WHERE m.thread_id = t.id
          AND m.role != 'user'
          AND m.created_at > COALESCE(
                (SELECT s.last_read_at FROM thread_user_state s
                  WHERE s.thread_id = t.id AND s.user_id = :viewer), '')) AS unread_since
    """
```

Note `:viewer` now appears in `_UNREAD_SQL` too, so `all_threads`,
`list_threads`, `get_thread` and `unread_summary` all take a viewer. For the
system/agent reader pass the owner's id (not `None`) so unread state is
well-defined.

### 1.5 Bots stay global — with a role floor

Bots are **not** per-user rows in the DB. They are host-level OpenClaw agents
whose model routing and credentials live in the agent runtime's own config file,
which DisPatch does not own. Making them per-user would mean per-user gateway config,
which is out of scope and would break `openclaw.list_agent_sessions`.

Instead, `config.Bot` gains one field, replacing `safe`:

```python
@dataclass
class Bot:
    ...
    access: str = "member"       # minimum role: 'guest'|'member'|'admin'|'owner'
    exclusive_to: str = ""       # optional username — a bot that belongs to one person
```

- `safe: true`  → `access: "guest"`
- `safe: false` → `access: "member"`
- New: `access: "owner"` for anything with a dangerous toolchain (a shell-capable
  bot, the coding terminal bot).
- `exclusive_to: "alex"` — only that user (plus owner) sees the bot. This is the
  honest answer to per-user agent memory (§6.4).

`config.load_bots()` keeps `safe` as a **deprecated alias** read on load and
written back as `access` on the next `save_bot_order()`, so an existing
`config.yaml` upgrades itself. `_safe_bot_ids()` becomes
`bots_for_role(role) -> set[str]`.

### 1.6 Deleting a user

Not a cascade. `users.delete(uid, reassign_to=owner_id)`:

1. `UPDATE threads SET owner_id = :owner WHERE owner_id = :uid` — history
   survives, re-homed to the owner, **visibility forced to `private`**.
2. `UPDATE files  SET owner_id = :owner WHERE owner_id = :uid`.
3. `messages.author_id` is left pointing at the deleted row → the FK is
   `REFERENCES users(id)` with no `ON DELETE`, so instead of deleting the row we
   **soft-delete**: `is_active = 0`, `password_hash = NULL`,
   `session_epoch = session_epoch + 1`. The name still renders in old bubbles.
   Hard row deletion is an owner-only, confirm-twice operation that first
   reassigns.

---

## 2. Roles

### 2.1 The set: `owner` > `admin` > `member` > `guest`

Four, and no more. No permission table, no RBAC engine — a rank comparison plus
one thread-ACL check.

```python
_ROLE_RANK = {"guest": 0, "member": 1, "admin": 2, "owner": 3}
```

| Role | Who | One-line charter |
|---|---|---|
| **owner** | the person who installed it | administers **the machine**. Exactly one; transferable, never deletable. |
| **admin** | second parent / co-maintainer | administers **the app**: accounts, invites, bots, reactions. No host control. |
| **member** | family member with an account | full chat: own threads, own files, talk to `access:member` bots. |
| **guest** | the kid's tablet, a visitor | the successor to Safe Mode: `access:guest` bots only, no media, no files, no search/export, view + send, upload quota. |

The owner/admin split is the one non-obvious call, and it is the important one:
**admin administers the app; owner administers the machine.** ComfyUI service
control, the terminal PTY and `/api/openclaw/*` are host operations that happen
to be reachable through a chat app. An admin who can add accounts should not
thereby get a root-adjacent shell.

### 2.2 Enforcement primitives

Three functions replace `_deny_decoy_bot` / `_deny_decoy_thread` /
`_deny_decoy_mutation`. Everything else is these three.

```python
@dataclass(frozen=True)
class Principal:
    user_id: str | None
    username: str
    display_name: str
    role: str                      # 'owner'|'admin'|'member'|'guest'|'system'
    token: str | None = None
    is_machine: bool = False       # api_token or loopback agent

    @property
    def rank(self) -> int: return _ROLE_RANK.get(self.role, -1)
    @property
    def unrestricted(self) -> bool: return self.role in ("owner", "admin", "system")


def principal_of(request: Request) -> Principal          # set by auth_gate
def require_role(request: Request, minimum: str) -> Principal
def require_thread(request: Request, thread_id: str, *, write: bool = False
                   ) -> tuple[Principal, ThreadOut]
def require_bot(request: Request, bot_id: str) -> tuple[Principal, config.Bot]
```

`require_thread` is the workhorse:

```python
async def require_thread(request, thread_id, *, write=False):
    p = principal_of(request)
    t = await db.get_thread(thread_id, viewer_id=None)      # raw row
    if not t:
        raise HTTPException(404, "Thread not found")
    if not p.unrestricted:
        acc = await db.thread_access(thread_id, p.user_id)  # None|'reader'|'writer'|'owner'
        if acc is None:
            raise HTTPException(404, "Thread not found")     # 404, not 403 — see below
        if write and acc == "reader":
            raise HTTPException(403, "Read-only in this conversation")
    require_bot_access(p, t.bot_id)                          # role floor on the bot
    return p, t
```

**404 not 403 for "you can't see this thread."** A 403 confirms the thread
exists, which in a household is exactly the leak you are trying to prevent
("so there *is* a thread I'm not allowed to see"). 403 is reserved for
"you can see it but can't do that to it".

### 2.3 Capability map — every privileged operation in `main.py`

Legend: **O**=owner, **A**=admin+, **M**=member+, **G**=guest+, **S**=self/ACL,
**MACH**=machine principal (api_token or loopback agent).

#### Auth & accounts
| Route / function | Today | New gate |
|---|---|---|
| `GET /api/auth/status` `auth_status` | open | open (returns `user`, `role`) |
| `POST /api/auth/unlock` → `/api/auth/login` | PIN | open (rate-limited, `auth.throttle_wait`) |
| `POST /api/auth/lock` `auth_lock` | any | **S** (own session) |
| `POST /api/auth/setup` `auth_setup` | PIN owner | **S** password change; reset another's = **A**; owner's own = **O** |
| `POST /api/auth/recover` `auth_recover` | recovery code | **O** only path — resets the *owner* password from `RECOVERY-CODE.txt` |
| `POST /api/auth/remember-config` | full session | **O** (server-wide policy) |
| `POST /api/auth/forget-devices` | full session | **S** own devices; all users = **O** |
| *new* `GET/POST/PATCH/DELETE /api/users` | — | **A** (cannot create/modify `owner`) |
| *new* `POST /api/invites`, `POST /api/join` | — | **A** create; `join` open with a valid code |
| *new* `GET/POST/DELETE /api/tokens` | — | **A** (own tokens); other users' = **O** |

#### Bots
| Route / function | Today | New gate |
|---|---|---|
| `GET /api/bots` `get_bots` | safe-filtered for decoy | filtered by `bots_for_role(p.role)` + `exclusive_to` |
| `GET /api/bots/all` `get_all_bots` | full session | **A** |
| `PUT /api/bots/order` `put_bot_order` | full session | **A** (it sets `access`, i.e. who can reach a bot) |
| `GET /api/bots/{id}/avatar` | safe bots only | **G** if the bot is in the caller's set |
| `GET /api/bots/{id}/avatar/full` | full session | **M** |
| `POST /api/bots/{id}/avatar` `upload_bot_avatar` | full session | **A** |

#### Threads & messages
| Route / function | Today | New gate |
|---|---|---|
| `GET /api/threads` `list_threads` | `_deny_decoy_bot` | **G** + `_VISIBLE_SQL` scoping in SQL |
| `POST /api/threads` `create_thread` | `_deny_decoy_bot` | **G** + bot floor; `owner_id = p.user_id`, `visibility='private'` |
| `GET /api/threads/{id}` `get_thread` | `_deny_decoy_bot` | `require_thread` |
| `GET /api/threads/{id}/messages` `get_messages` | `_deny_decoy_bot` (**bot-level only — IDOR today**) | `require_thread` |
| `PATCH /api/threads/{id}` `patch_thread` | `_deny_decoy_mutation` | rename: thread owner or **A**; **pin: self** (writes `thread_user_state`) |
| `POST /api/threads/{id}/read` `mark_read` | decoy-thread check | `require_thread`, writes `thread_user_state` for `p.user_id` |
| `GET /api/unread` `unread_summary` | safe-filtered | per-viewer SQL |
| `DELETE /api/messages/{id}` `delete_message_endpoint` | `_deny_decoy_mutation` | message author, thread owner, or **A** |
| `PATCH /api/messages/{id}/checklist` `update_message_checklist` | `_deny_decoy_mutation` | message author, thread owner, or **A** |
| `DELETE /api/threads/{id}` `delete_thread` | `_deny_decoy_mutation` | archive: thread owner+; `hard=true`: thread owner or **A**, audit-logged |
| *new* `PUT /api/threads/{id}/share` | — | thread owner or **A** |
| WS `_handle_send` | `_ws_bot_allowed` | `require_thread(write=True)` on the connection principal |
| WS `_handle_create_thread` | `_ws_bot_allowed` | bot floor; owned by the connection principal |
| WS `_handle_get_threads` | `_ws_bot_allowed` | viewer-scoped query |
| WS `_handle_get_messages` | **decoy-only check — IDOR today** | `require_thread` |
| WS `_handle_archive` | decoy-blocked | thread owner or **A** |
| WS `_handle_retry` | `_ws_bot_allowed` | `require_thread(write=True)` |

Two live IDORs to note: `_handle_get_messages` only checks ACL when
`manager.conn_decoy(ws)` is true, and `get_messages`/`_handle_retry` check the
*bot*, never the thread. Harmless with one user; a direct cross-user read the
moment accounts exist.

#### Media & files
| Route / function | Today | New gate |
|---|---|---|
| `POST /api/upload` `upload_media` | open, decoy quota | **G** + per-user quota (`_decoy_upload_used` re-keyed from IP → `user_id`) |
| `GET /api/media` `serve_media` | decoy-blocked | **M** (see §3.4 on capability URLs) |
| `/media/*` **StaticFiles mount** | **unauthenticated** | replace mount with a gated route — see §3.4 |
| `POST /api/drop` `drop_file` | open | **G** |
| `POST /api/files` `file_upload` | *no handler gate* | **M**, `owner_id = p.user_id`, `visibility='private'` |
| `GET /api/files` `file_list` | *no handler gate* | **M**, returns own + `house` + `thread`-visible; **A** sees all |
| `GET /api/files/{id}/download` `file_download` | `_block_fileserver_read` | file owner / house / thread-ACL / **A** |
| `GET /api/files/{id}/raw` `file_raw` | `_block_fileserver_read` | same |
| `DELETE /api/files/{id}` `file_delete` | *no handler gate* | file owner or **A** |
| `POST /api/files/wipe` `file_wipe` | *no handler gate* | **O** — bulk date-range destruction, audit-logged |

#### Machine / inbound
| Route / function | Today | New gate |
|---|---|---|
| `POST /api/inject` `inject_message` | loopback or `api_token`, `_deny_agent_route_to_browser` | **MACH**; message attributed to the token's user; target thread must be visible to that user **or** the principal is `system` (loopback) |
| `POST /api/daily` `ensure_daily_thread` | same | **MACH**; creates `origin='daily'`, `visibility='house'` |
| `POST /api/threads/{id}/messages` `post_message_rest` | same | **MACH** + thread check |
| `POST /api/reactions/fire` `reactions_fire` | agent-kind only | unchanged, plus audience routing (§4.5) |

#### Search / export / recovery / bridge
| Route / function | Today | New gate |
|---|---|---|
| `GET /api/search` `search_messages` | `_decoy_blocked` prefix | **M**, `Database.search_messages(viewer_id=…)` — **both** the FTS branch and the LIKE fallback |
| `GET /api/export` `export_all` | `_decoy_blocked` | **M** own visible threads; whole-house dump = **O** |
| `POST /api/recover/transcript` `recover_transcript` | `_decoy_blocked` | **A**; `all: true` = **O** |
| `GET /api/openclaw/sessions` | `_decoy_blocked` | **O** |
| `GET /api/openclaw/transcript` | `_decoy_blocked` | **O** |
| `POST /api/openclaw/import` | `_decoy_blocked` | **O** |

`/api/openclaw/*` is owner-only because it reads **raw host transcripts of every
agent session** — cron runs, subagent chatter, the operator's own Control-UI
conversations. It is a filesystem read primitive wearing a chat-app costume.

#### Reactions
| Route | Today | New gate |
|---|---|---|
| `GET /api/reactions` | safe-filtered | **G** (filtered), **MACH** unfiltered |
| `POST /api/reactions/fire` | agent-only | agent-only (unchanged) |
| `GET /api/reactions/{rid}/image` | safe-only for decoy | guest: `safe` cards only; **M** all |
| `POST /api/reactions` (upload), `PATCH`, `DELETE`, `/generate`, `/reseed`, `/settings`, `/prompts`, `/pool*` | full session or **MACH** | **A** (or **MACH**) |

#### Host control
| Route | Today | New gate |
|---|---|---|
| `/api/comfy/service/*`, `/api/comfy/workflows/*` (`_require_comfy`) | `_deny_decoy_mutation` | **O** |
| `/api/terminal/*` (`_require_terminal`), `WS /ws/terminal` (`terminal_ws`) | full session | **O** |
| `GET /api/health` | open | open but **trimmed** to `{"status","version"}`; `data_dir`, `gateway_ok`, `db_integrity_ok`, `last_backup_*`, `clients` only for **A** |

### 2.4 Out-of-band admin (required for a self-hosted app)

Today the recovery story is "hand-edit `security.yaml: pin:`". That must have a
successor, or a locked-out owner has a dead app:

- `RECOVERY-CODE.txt` keeps working — it now resets the **owner's password**
  (not the whole lock) and bumps `users.session_epoch` for everyone.
- A new CLI `backend/manage.py` (`uv run python -m app.manage`):
  `user list | user add | user passwd | user role | user activate` +
  `token add | token revoke`. It talks to `chats.db` directly. Whoever can run
  it already owns the machine, so no extra auth — but it must refuse to run
  while `dispatch.service` holds a write lock without `busy_timeout`.

---

## 3. Sharing model

### 3.1 The default: **threads are private to their creator**

A thread is 1:1 with an OpenClaw session key — structurally a DM with an
assistant, not a channel. Three reasons this is the right default for a family
app specifically:

1. **Asymmetric failure cost.** Getting it wrong toward private costs one click
   ("Share with the house"). Getting it wrong toward public means a teenager's
   or a spouse's conversation with a bot is in someone else's sidebar, silently,
   forever, in a product whose entire pitch is "self-hosted, your data".
2. **It matches what people already assume** from every other chat app: your
   conversation with an assistant is yours.
3. **It is the only default that is safe under the §0 threat model.** A member
   can ask a bot to read host files; the transcript of that lands in a thread.
   Public-by-default would broadcast every such transcript to the house.

The counter-argument ("families share everything, private-by-default makes it
feel siloed") is real, and is answered by making sharing *one click and
visible*, not by inverting the default.

### 3.2 The three sharing states

| State | Storage | Meaning |
|---|---|---|
| **Private** (default) | `visibility='private'`, no member rows | owner + admins only |
| **Shared with people** | `visibility='private'` + `thread_members` rows | named users, `writer` or `reader` |
| **House** | `visibility='house'` | every account whose role clears the bot's `access` floor |

One toggle + one people-picker in the thread menu. No groups, no nested
permissions, no inheritance. If a family needs groups they need Matrix, not
DisPatch.

A shared thread is a **group conversation**: writers can send, and their turns
run against the same OpenClaw session. See §6.2 for how the agent is told who
is speaking.

### 3.3 Provenance defaults (`threads.origin`)

The three thread sources that have no user context need explicit rules:

| origin | Created by | owner_id | visibility | Why |
|---|---|---|---|---|
| `chat` | `create_thread`, `_handle_create_thread` | creator | `private` | the default |
| `daily` | `find_or_create_daily_thread` (cron via `/api/daily`, `/api/inject`) | owner | **`house`** | the morning briefing is a noticeboard; its id `daily-<bot>-<date>` has no user in it, and per-user daily threads would multiply every cron's agent turns by N |
| `mirror` | `_mirror_create_thread` / `_gateway_mirror_loop` | owner | **`private`** | these are the *operator's* own Control-UI and `main`-session conversations arriving from outside the app. They are the most sensitive threads in the DB and nobody asked for them to be shared. Env `LOCAL_CHAT_MIRROR_OWNER` to re-home. |
| `import` | `openclaw_import_session` | importer (owner-only route) | `private` | raw agent transcript |

### 3.4 Files and media — three tiers, and one honest limitation

**Chat attachments** (`files.source='chat'`, posted via `upload_media` /
`drop_file`): `visibility='thread'`, `thread_id` set. They inherit the ACL of
the thread they were posted into. This is the only correct rule — a file
attached to a house thread must be readable by the house, and a file attached to
a private thread must not be.

**File Server drops** (`files.source='fileserver'`): `visibility='private'`,
owned by the uploader. The current "deliberate one-way drop" survives as: guests
can upload and cannot list or download **anything**; members see their own;
admins see all. On-box agents keep reading straight off `FILES_DIR` (they always
have — that is the point of the drop box).

**Inline media** (`MEDIA_DIR`, served at `/media/<uuid>.png`) is the honest
limitation and must be documented, not glossed:

> `app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)))` at the bottom of
> `main.py` serves every uploaded image with **no authentication at all** today
> — `auth_gate` blocks `/media/` for decoys via `_decoy_blocked`, but the mount
> itself has no notion of a user.

Recommendation, in two parts:

1. **Replace the StaticFiles mount with a route** `GET /media/{name}` that
   requires an authenticated principal of rank ≥ member and re-uses
   `_allowed_media_bases()` for path containment. That alone closes anonymous
   access.
2. **Accept capability-URL semantics for media within the member tier**, and say
   so in the README. Full per-thread ACL on media would require a
   media-blob → message index that does not exist, and would break the agent
   paths (`_normalize_media`, `_ingest_content_media`, `_salvage_media_refs`,
   `_media_second_look`) that reference a blob *before* the message row exists.
   A member who guesses another member's `uuid4().hex` filename can fetch it.
   That is a 128-bit guess, and it is a deliberate v1 trade, not an oversight.

   The v2 path, if someone wants it: add `media(name PRIMARY KEY, thread_id,
   owner_id)` populated in `_stream_upload`, and fall back to member-tier when
   the row is absent.

### 3.5 Bots: global, role-gated (recap)

Global, with `access` as a role floor and `exclusive_to` for a personal bot.
Justification is in §1.5. The practical upshot for a family install:

```yaml
bots:
  - {id: quick,     access: guest}    # was safe: true
  - {id: helper,    access: guest}
  - {id: assistant, access: member, reactions: true}
  - {id: smart,     access: member}
  - {id: shell-bot, access: owner}    # dangerous toolchain
```

---

## 4. Session + WebSocket

### 4.1 Session storage: DB-backed, sync-readable, memory-hot

Constraint that drives the design: **session lookup must be callable
synchronously.** `ws.py:ConnectionManager._is_safe` calls
`self.is_session_live(tok)` from inside `_frame_for`, which runs per-frame
inside `broadcast` — it cannot `await`, and it must not do I/O.

So:

- `auth.py` keeps `_sessions: dict[token, Session]` as the **hot cache**
  (unchanged shape, plus `user_id`, `role`, `epoch`), guarded by the existing
  `threading.RLock`.
- It gains a **synchronous `sqlite3` connection** to `chats.db` for durability
  (`PRAGMA busy_timeout=5000`, WAL already on, so it coexists with the async
  `aiosqlite` connection). Writes are rare: login, logout, and a throttled
  `last_seen_at` touch reusing the existing `_TRUSTED_TOUCH_MIN = 3600.0` rule.
- On boot, `auth.load_sessions()` warms the cache from `user_sessions` — this is
  what makes a service restart stop logging the family out.
- `trusted-devices.yaml` is migrated into `user_sessions` and then deleted.

Alternative considered and rejected: a separate `auth.db`. Same file is fine
under WAL, keeps `VACUUM INTO` backups complete in one artefact, and
`Database.backup_to` then captures sessions too (which is correct — a restored
backup should not log everyone out).

Keep `auth._lockdown_config()` / `_LOCKDOWN_HASH` semantics: if the user store
is unreadable, **fail closed** — no principal can be minted, the app serves the
login screen and nothing else.

### 4.2 Identity into the WS scope

`websocket_endpoint` already does its own cookie read because "HTTP middleware
doesn't run for the websocket scope". Keep exactly that structure:

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # ... existing CSWSH Origin==Host check, unchanged ...
    principal = auth.principal_for_token(ws.cookies.get(COOKIE_NAME))
    if principal is None:
        if not SETTINGS.anonymous_guest:          # §5
            await ws.close(code=1008); return
        principal = Principal(None, "guest", "Guest", "guest")
    await manager.connect(ws, principal=principal)
    bots = bots_for_principal(principal)
    await ws.send_json({"type": "hello", "user": principal.public_dict(),
                        "bots": [b.to_dict() for b in bots]})
```

`ConnectionManager._conns[ws]` stores `{"principal": Principal, "lock": Lock}`
instead of `{"decoy", "token", "lock"}`. `conn_decoy(ws)` becomes
`conn_principal(ws)`.

Revalidation on every received frame (extending the existing idle-expiry check):

```python
        live = auth.get_session(principal.token) if principal.token else None
        if principal.token and (live is None or live.epoch != principal.epoch):
            await ws.send_json({"type": "locked"})   # client reconnects → login
            break
```

The `epoch` comparison is what makes "sign out everywhere", a password change,
a role demotion and a user deactivation take effect on **already-open sockets**,
which the current token-only check cannot do.

`/ws/terminal` (`terminal_ws`) keeps its stricter rule and adds one line:
no principal, or `principal.role != "owner"` → `close(1008)`. There is no guest
tier on a PTY.

### 4.3 Kill the global redactor. Route by audience.

This is the central change of the whole design.

Today `ws.py` has one `self.redactor`, and `manager.broadcast(frame)` sends
every frame to every socket, filtered afterwards by `redact_for_decoy`. Look at
how that function ends:

```python
    if t in ("threads_list", "threads", "messages"):
        ...
    return frame          # ← anything unrecognised passes through, unredacted
```

It is **fail-open for unknown frame types**, and it only drops frames for a
hardcoded list of `type` values. With one user and one decoy tier that is a
manageable risk (worst case: a locked device sees a bit too much of the owner's
own data). With N users it means **any frame type added in the future leaks one
family member's messages to every other family member's socket by default.**

Replace filtering with **audience routing**:

```python
class ConnectionManager:
    async def send_to(self, audience: "Audience", message: dict) -> None: ...
    async def send(self, ws, message): ...          # unchanged, single socket

class Audience:
    """Who may receive a frame. Resolved ONCE per broadcast."""
    users: frozenset[str] | None   # None = every authenticated principal
    min_role: str = "guest"
    bot_id: str | None = None      # apply the bot's access floor too
```

and the three constructors that cover every call site in `main.py`:

```python
async def thread_audience(thread_id: str) -> Audience   # owner + members (+admins) or house
def user_audience(user_id: str) -> Audience
def admin_audience() -> Audience                        # comfy/terminal/pool telemetry
```

`ConnectionManager.broadcast(frame)` with no audience is **removed** — not
deprecated, removed — so every one of the ~30 broadcast call sites must state
its audience and the compiler (well, the test suite) finds the ones that
didn't. Grep targets: `_broadcast_thread_update`,
`_persist_and_broadcast_message`, `_persist_and_stream_message`,
`_deliver_assistant_text`, `run_agent_turn`'s `thinking` frames,
`_handle_send`, `_handle_create_thread`, `_handle_archive`, `create_thread`,
`delete_thread`, `delete_message_endpoint`, `inject_message`,
`ensure_daily_thread`, `post_message_rest`, `openclaw_import_session`,
`_mirror_import_items`, `_broadcast_comfy_state`, `_broadcast_pool_state`,
`_terminal_state_changed`, `fire_reaction`.

Audience cache, mirroring the existing `_thread_bot: dict[str, str]` idiom that
already exists for exactly this reason (a sync redactor needing thread → bot):

```python
_thread_audience: dict[str, Audience] = {}     # invalidate on share change,
                                               # visibility change, hard delete
```

Invalidation points: `PUT /api/threads/{id}/share`, `delete_thread(hard=True)`
(already pops `_thread_bot`), user deletion, role change.

### 4.4 The residual filter (defence in depth, not the boundary)

Keep a per-connection content filter, but demote it:

```python
    def _frame_for(self, meta: dict, message: dict) -> Optional[dict]:
        p = meta["principal"]
        if not audience_admits(message["_audience"], p):     # primary boundary
            return None
        return content_filter_for(p, message)                # tier-2: guest media strip
```

`content_filter_for` is `redact_for_decoy` reduced to what it should always have
been: strip media for guests (`_redact_message_dict`, `_redact_thread_dict`),
drop `stream_chunk`/`progress`/`terminal_state`/`reaction_pool`. It no longer
decides *who* sees a conversation — the audience does.

Change its tail from `return frame` to an **explicit allowlist**:

```python
_GUEST_FRAME_TYPES = {"hello","bots","message","stream_done","thread_update",
                      "thread_created","thread_deleted","thinking",
                      "message_deleted","messages","threads_list","threads",
                      "error","ack","pong","locked","reaction"}
    ...
    if t not in _GUEST_FRAME_TYPES:
        return None          # fail CLOSED for anything new
```

### 4.5 Reaction overlays

Today `fire_reaction` broadcasts an image to **every connected device** — that
is the feature. Under multi-user:

- **Thread-bound fire** (`payload.thread_id` set): audience =
  `thread_audience(thread_id)`. The trace row lands in that thread, so its
  visibility must match, or a locked-out user gets a popup for a conversation
  they cannot open.
- **Untargeted fire** (app-wide pop): audience = every principal whose role
  clears the firing bot's `access` floor. Keep it — the "everyone's screen lights
  up" moment is the point of the feature — but it can no longer reveal a thread.
- `GET /api/reactions/{rid}/image` gates on the caller's role (`safe` cards for
  guests), unchanged in spirit.

### 4.6 The leak checklist

Every one of these must be verified by a test, because each is a silent
cross-user read if missed:

1. `_handle_get_threads` → `db.list_threads(bot_id, viewer_id=p.user_id)`, not
   post-filtering.
2. `_handle_get_messages` → `require_thread` (currently decoy-only).
3. `_handle_retry` → `require_thread(write=True)` (currently bot-only).
4. `hello` frame's bot list per principal (`bots_for_principal`).
5. `thread_created` from `inject_message` / `ensure_daily_thread` /
   `_mirror_create_thread` → correct audience, not global.
6. `thinking` / `stream_*` frames from `run_agent_turn` and `_watch_progress` →
   thread audience. These run in **background tasks with no request context** —
   they must carry the audience resolved at turn start, alongside the existing
   `_thread_bot[thread_id] = bot_id` line.
7. `_deliver_assistant_text`, `_reconcile_transcript`, `_follow_session`,
   `_mirror_import_items` — the four delivery paths — all broadcast; all need
   the thread audience.
8. `Database.search_messages` — **both** the FTS5 branch and the LIKE fallback.
   FTS bypasses every other check because it queries `messages_fts` directly.
9. `Database.unread_summary` / `all_threads` (used by `export_all` and
   `recover_transcript(all=True)`).
10. `/media/*` mount (§3.4).
11. `_ACK_SEEN` is global; `ack` frames already go only to the originating
    socket via `manager.send(ws, …)` — verify no future refactor broadcasts them.
12. `audit_log` and `/api/health` detail → admin+.

---

## 5. Safe Mode: retire it, become `guest`

**Recommendation: remove anonymous Safe Mode. Convert it into the `guest` role.
Ship anonymous access as an explicitly-opt-in "kiosk mode", default off.**

### Why

1. **Safe Mode's defining property is *no identity*.** That is fundamentally
   incompatible with an ACL model: every endpoint, forever, has to remember the
   anonymous branch. The evidence is in the code — the tier costs
   `auth_gate` + `_decoy_blocked` + `_deny_decoy_bot` + `_deny_decoy_thread` +
   `_deny_decoy_mutation` + `_is_safe_mode_caller` + `_deny_agent_route_to_browser`
   + `redact_for_decoy` + `_block_fileserver_read` + `_gated_avatar_static` +
   `_safe_avatar_static` + `_decoy_quota_*`. Eleven mechanisms, several of which
   (per the comments) were added as each audit found a gap — the count argues for
   replacing the tier, not for an unfixed hole. Carrying that into a public
   release, on top of a new ACL layer, is how the two systems disagree and
   something slips between them.
2. **The deniability story does not survive a login screen anyway.** The code
   calls it a *decoy*: the app looks complete but limited, so a locked device
   reveals nothing. Once the app shows "Sign in", the existence of more content
   is announced. The decoy is over regardless of what we do with the code.
3. **Everything Safe Mode delivers is expressible as a role**, with strictly
   more capability: `guest` sees `access: guest` bots, gets media stripped, gets
   no files/search/export, is view+send, and carries the upload quota — but is
   now *attributable* (you can see it was the tablet), *revocable* (deactivate
   the account), and *ACL-aware* (a guest can be given a specific shared thread,
   which Safe Mode could never do).
4. It removes an unauthenticated write surface from a service that is reachable
   from the LAN the moment an operator sets `DISPATCH_HOST=0.0.0.0`. For a public
   release that is worth a lot on its own.

### What is lost, and the mitigation

| Lost | Mitigation |
|---|---|
| "Hand the phone to a visitor, no setup" | a shared `guest` account with a 4-digit password, or a QR invite code (`/join?code=…`) — 15 seconds |
| The decoy/deniability property | gone. Say so in the changelog; do not pretend otherwise |
| Existing family devices that never entered the PIN | they now see a login screen once. The migration prints the guest credentials it created (§7 step 1) |

### The escape hatch

```yaml
# security.yaml
anonymous_guest: false     # kiosk mode: serve an unauthenticated guest principal
```

`SETTINGS.anonymous_guest` (env `LOCAL_CHAT_ANON_GUEST`) mints an ephemeral
`Principal(user_id=None, role="guest")` for sessionless requests, reproducing
today's behaviour for people who liked it. Default **off**, documented as
"anyone who can reach the port can read your guest-tier bots".

`safe:` on bots does **not** disappear — it becomes `access: guest` (§1.5), so
the roster semantics the owner already tuned carry over exactly.

---

## 6. Per-user agent sessions

### 6.1 Do not change the session key

`openclaw.session_key_for(bot_id, thread_id)` → `agent:<bot_id>:<thread_id>`.

**Leave it exactly as is.** The key observation is that it is *already*
per-user once threads are per-user: two people talking to the assistant create
two threads with two uuids, hence two OpenClaw sessions with independent context.
The isolation requirement is satisfied by the thread model, not by the key.

Adding a user segment (`agent:<bot>:<user>:<thread>`) would break, all at once:

- `openclaw.resolve_session_file` and its lowercase-fallback logic;
- `openclaw._session_kind` and `openclaw.mirror_kind`, which parse
  `parts[2]` and special-case `tag == parts[1]`, `daily-*`, `dashboard`,
  `subagent`, `cron`;
- `_mirror_cycle`'s reuse of a gateway webchat thread tag **as the DisPatch
  thread id** (`tag = s["session_key"].split(":", 2)[2]`);
- `find_or_create_daily_thread`'s deterministic `daily-<bot>-<date>` id;
- `openclaw_transcript` / `openclaw_import_session` / `recover_transcript`;
- and above all **every existing session on the live install** — the family's
  history would be orphaned from its transcripts, which is precisely the
  outcome this migration exists to avoid.

### 6.2 Shared threads: tell the agent who is talking

The real gap is not isolation, it is **attribution in multi-participant
threads**. Today `run_agent_turn(thread_id, bot_id, text)` sends bare text.

```python
async def run_agent_turn(thread_id: str, bot_id: str, text: str,
                         speaker: Principal | None = None) -> None:
    ...
    agent_text = await _resolve_doc_refs(text)
    if speaker and await db.thread_is_multiparty(thread_id):
        agent_text = f"[from: {speaker.display_name}]\n{agent_text}"
```

Only for multi-party threads (`visibility='house'` or ≥1 `thread_members` row),
so **solo threads have byte-identical behaviour to today** — no prompt drift, no
"why did my bot start saying my name". Document the `[from: …]` convention in
[the agent integration contract](../agents.md) so agents can rely on it.

`messages.author_id` renders the same information in the UI.

### 6.3 Daily and mirror threads

Daily threads are house-visible and cron-driven (`/api/inject` has no user), so
they are inherently shared. Mirror threads are owner-private. Both keep their
existing keys. No change.

### 6.4 The limitation you cannot fix in DisPatch: agent long-term memory

Separate session keys give separate *conversation* context. They do **not**
separate an OpenClaw agent's **cross-session memory** (Memory Core, under
the agent runtime's own state directory). Two users talking to the same bot in
two private threads still share whatever that bot remembers about either of them
across sessions.

DisPatch cannot fix this — the fix lives in the agent runtime's own config file.
The honest options, in recommended order:

1. **Say it in the README.** "Bots have long-term memory that is per-bot, not
   per-user. If two people must not share what a bot remembers, give them
   different bots." This is the answer for 95% of installs.
2. **`exclusive_to: <username>`** (§1.5) — a personal bot, one line of config,
   backed by a real separate OpenClaw agent id. This is the supported path.
3. Per-user agent aliases derived automatically (`smart` → `smart_alice`) —
   **rejected**: DisPatch would have to create agents in the agent runtime's own
   config file, which is another program's config, and a missing agent id fails at turn time with
   an opaque CLI error.

---

## 7. Implementation plan

Nine steps. Each ends with the app working and tested. Sizes are rough
engineering days for someone who knows this codebase; "lines" are net new/changed
backend lines.

---

### Step 0 — Safety net (S · ~1 day · low risk)

- `schema_migrations` table + a `Database.migrate()` that runs numbered steps
  inside `connect()`, replacing the current `for ddl in (...): try/except pass`
  block (keep the old ALTERs as migration 1 so existing DBs record where they are).
- **Refuse to migrate without a fresh backup**: call the existing
  `_make_backup()` / `Database.backup_to()` first and abort if it fails. Write
  the backup path to the log and to `DATA_DIR/PRE-MULTIUSER-BACKUP.txt`.
- A test that runs the full migration against **a copy of the real
  `~/.local/share/local-chat/chats.db`** and asserts: thread count, message
  count, per-thread message counts, and FTS row count are identical before and
  after. This test is the whole reason the family history survives.

### Step 1 — Identity, no enforcement (L · ~4 days · **highest risk**)

New `users`, `user_sessions`, `invites`, `api_tokens` tables. Rewrite `auth.py`
around `Principal` + DB-backed sessions (§4.1). Login/join/logout routes. Owner
bootstrap. Admin user list UI (minimal).

**The elegant part of the migration:** the existing PIN is already
PBKDF2-HMAC-SHA256 (`auth._hash_pin`, `security.yaml: pin_hash/salt/iterations`).
The owner row is created by **copying that hash verbatim**:

```python
cfg = auth.load()
owner_hash = f"pbkdf2_sha256${cfg.iterations}${cfg.salt}${cfg.pin_hash}"  # scrub-ok: builds the format string
db.users.create(username="owner", display_name=os.environ.get("USER","Owner"),
                role="owner", password_hash=owner_hash)
```

**The owner's existing PIN is their new password.** No reset email, no "your
account has been migrated" dance. Their remembered devices (`trusted-devices.yaml`
hashes) become `user_sessions` rows with `persistent=1`, so **the phones that
were unlocked stay unlocked through the upgrade.**

If a guest tier is wanted, also create `guest` with a generated 6-character
password from `auth._RECOVERY_ALPHABET`, printed to the log and written to
`DATA_DIR/GUEST-PASSWORD.txt` (0600).

Behaviour at the end of this step: identical to today. Owner sees everything;
anyone without a session is treated exactly as a decoy is now.

*Risk:* `auth.py` is the fail-closed core. Preserve `_lockdown_config()`,
`throttle_wait`/`register_failure` (now per-username *and* per-IP), the atomic
0600 writes, and the "corrupt store → nothing unlocks" rule. Do not let the
threading lock and the new sqlite connection deadlock — take the sqlite call
**outside** `_lock` where possible.

### Step 2 — Ownership columns + backfill (M · ~2 days · low risk, high stakes)

All the `ALTER TABLE`s from §1.3, plus `thread_members`, `thread_user_state`,
`audit_log`. Then the backfill, in one transaction:

```sql
UPDATE threads  SET owner_id = :owner WHERE owner_id IS NULL;
UPDATE files    SET owner_id = :owner WHERE owner_id IS NULL;
UPDATE messages SET author_id = :owner WHERE role = 'user' AND author_id IS NULL;

-- Provenance, from the id shape the app already guarantees.
UPDATE threads SET origin = 'daily' WHERE id LIKE 'daily-%';

-- Per-user read/pin state, migrated for the owner only.
INSERT OR IGNORE INTO thread_user_state (thread_id, user_id, last_read_at, is_pinned)
SELECT id, :owner, last_read_at, is_pinned FROM threads;
```

**The visibility rule — this is the one to get right:**

```sql
-- A thread's PRE-migration audience is defined by whether its bot was `safe`:
-- safe-bot threads were readable by anyone on the LAN in Safe Mode, so they
-- become house-visible. Everything else was PIN-gated and becomes private.
UPDATE threads SET visibility = 'house'
 WHERE bot_id IN (:safe_bot_ids)      -- from config.load_bots() at migration time
    OR origin = 'daily';
```

This reproduces the observable status quo exactly, and strictly *tightens* it
(house = accounts, where before it was anyone on the network). Log the
before/after counts.

Mirror threads: `UPDATE threads SET origin='mirror', visibility='private'` for
ids present in `gateway-mirror.json` — read that file during migration, since
after this point it is the only record of which threads came from the mirror.

`threads.last_read_at` and `threads.is_pinned` are **left in place**, unread.

### Step 3 — ACL in the data layer (M–L · ~3 days · medium risk)

`Database.visible_clause()` + `_VISIBLE_SQL` + the per-viewer `_UNREAD_SQL`.
Thread every read method through it: `list_threads`, `all_threads`, `get_thread`,
`list_messages`, `dump_messages`, `unread_summary`, `search_messages`,
`list_files`, `get_file`.

**Make `viewer_id` a required positional argument**, not a keyword with a
default. A default of `None` (= unrestricted) means a call site you forgot
silently returns everything; a required argument makes it a `TypeError` at
import/test time. This one convention is worth more than any amount of review.

`search_messages` gets the predicate in **both** branches — the FTS5 `MATCH`
query and the `LIKE` fallback. Add a test that seeds two users' threads and
asserts the FTS path returns only the viewer's.

### Step 4 — Route authorization (M · ~3 days · medium risk, mechanical)

Delete `_decoy_blocked`, `_deny_decoy_bot`, `_deny_decoy_thread`,
`_deny_decoy_mutation`, `_is_safe_mode_caller`, `_deny_agent_route_to_browser`.
Rewrite `auth_gate` to resolve a `Principal` onto `request.state` and nothing
else — **no path-prefix authorization in middleware.** Authorization moves into
the handlers via `require_role` / `require_thread` / `require_bot` per §2.3.

Ship one test that makes this permanent:

```python
def test_every_route_is_gated():
    """Introspect app.routes; every /api route must appear in the capability
    table (main.ROUTE_CAPS) or in the explicit OPEN allowlist. A new route with
    no gate fails CI."""
```

That test is the reason a path-prefix table can be safely deleted: the
enforcement moves from "one list someone must remember to update" to "the route
does not exist unless it declared its gate".

Also here: the `/media` route replacing the StaticFiles mount, the `/api/health`
trim, and re-keying `_decoy_upload_used` from client IP to `user_id`.

### Step 5 — WebSocket audience routing (M–L · ~3 days · **highest risk after step 1**)

Per §4.3–4.6. Per-connection `Principal`, `Audience`, `send_to`, removal of
unaudienced `broadcast`, the `_thread_audience` cache and its invalidations, the
`epoch` revalidation, `_GUEST_FRAME_TYPES` fail-closed tail.

*Why this is the risky one:* a wrong audience does not throw — it silently
**fails to deliver**. And DisPatch has four redundant delivery paths plus a
follower plus a reconciler plus the gateway mirror, all of which will partially
paper over a missing broadcast in confusing, timing-dependent ways. Do it after
steps 3 and 4 so the ACL is already trustworthy and the tests exist, and add an
explicit test per delivery path: `run_agent_turn`, `_deliver_assistant_text`,
`_reconcile_transcript`, `_follow_session`, `_mirror_import_items`.

### Step 6 — Sharing UI, invites, admin panel (M · ~3 days · low backend risk)

Frontend: login/join screens, the account switcher, a thread "Share" sheet
(house toggle + people picker), an admin Users panel (invite, role, deactivate,
reset password), per-user avatars in bubbles for shared threads. Backend:
`PUT /api/threads/{id}/share`, `/api/users`, `/api/invites`.

Remember the frontend's cache-busting discipline: `?v=N` on `main.js`/`app.css`
**and** the `sw.js` CACHE name bump, or devices keep the old bundle and fail to
log in.

### Step 7 — Agent attribution + docs (S · ~1 day · low risk)

The `[from: …]` prefix for multi-party threads (§6.2). Update
[the agent integration contract](../agents.md) and the README with
the §0 threat model, the guest/kiosk explanation, and the media capability-URL
caveat.

### Step 8 — Safe Mode removal (S–M · ~1 day · low risk)

Delete `redact_for_decoy`'s routing role (already reduced in step 5), the
`decoy` field in the WS `hello`, `_gated_avatar_static`/`_safe_avatar_static`
(avatars now follow the bot's `access` floor), `_block_fileserver_read`,
`DECOY_UPLOAD_QUOTA` → `GUEST_UPLOAD_QUOTA`. Wire `SETTINGS.anonymous_guest`.
Migrate `config.yaml`'s `safe:` → `access:` on the next write.

### Step 9 — Hardening (M · ~2 days)

Per-user rate limits on turns (the global `_agent_sem` of 3 means one user can
starve the others — add a per-user semaphore of 1–2 so one member's long local
model run doesn't block the house). Audit-log writes on destructive ops.
Session-list UI ("devices signed in", revoke). `manage.py` CLI. Extend
`SECURITY.md` with the multi-user threat model, and coordinated-disclosure
contact.

---

### Total and sequencing

~22 engineering days, ~2500–3000 net backend lines plus frontend. Steps 0–2 can
ship to the live install and run for a week doing nothing but recording
ownership — that is the cheapest possible way to de-risk the migration, and this
is the recommended path.

### The five things most likely to go wrong

1. **`auth.py` fails open during the rewrite.** It currently fails closed in
   three separate places (`_lockdown_config`, the trusted-store parse, the
   `pin_set` + no-session default). Keep every one, and keep a test that a
   corrupt user store yields *no* usable principal.
2. **A read path missed in step 3** — especially `search_messages`'s FTS branch,
   which bypasses every join the rest of the app relies on.
3. **A broadcast missed in step 5** — silent non-delivery, masked by the
   redundant delivery funnel.
4. **The `thread_user_state` backfill** — if `threads.last_read_at` isn't copied,
   every thread in the family's history shows as unread on first login. Not a
   security bug, but it is the kind of thing that makes an upgrade feel broken.
5. **The `/media` StaticFiles mount** being forgotten because it is the last two
   lines of the file.

### Non-goals, stated explicitly for the release

- Not a security boundary between users and the host (§0).
- No SSO/OAuth/LDAP. A self-hosted family app needs a username and a password.
- No per-user OpenClaw agent memory (§6.4).
- No end-to-end encryption. The server reads everything; it runs the agents.
- No group/team abstraction beyond `house` + per-thread membership.

### Checklist state is global, not per user

`PATCH /api/messages/{id}/checklist` writes to the MESSAGE, so a checked row is
checked for everyone who can see it. That is the right behaviour for a shared
household list and the wrong one for a per-person habit tracker, and nothing in
the current model distinguishes them. Under per-person accounts this becomes a
real decision: either keep it global (a shared list) or key the state by user
id (a personal one). Recording it here because the choice is invisible in the
code today.
