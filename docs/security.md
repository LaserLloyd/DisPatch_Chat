# Security guide for operators

To report a vulnerability, see [SECURITY.md](../SECURITY.md). This page is about
running a DisPatch Chat install safely.

Most of what follows is checked automatically. Open **Settings → System**;
anything wrong appears as a finding with the fix attached. Check it after any
change to how the app is exposed.

## What you are trusting

Be clear about this before you put private conversations in it.

**The host is the trust boundary.** Anyone with shell access, or read access to
the data directory, can read every message and file. The database is a plain
SQLite file. If you need protection against someone taking the disk, use
full-disk encryption — DisPatch cannot provide it.

**Messages are not end-to-end encrypted**, and cannot be in an app shaped like
this: the server stores history, indexes it for search, and hands text to agent
backends. Transport is protected by TLS. Storage is protected by your
filesystem. Anyone marketing an app like this one as "E2E encrypted" is
misleading you about one of those three.

**Multi-user is isolation, not adversarial separation.** Since 4.0 a conversation
belongs to the *profile* it was had in. Nobody else lists, opens, searches,
exports or receives live updates of it in the app, **Admins included**, and a
Basic profile's chats are as private as a User's. A bot a User or Admin made is
private to that profile too. Two exceptions are on purpose: the shared
**DisPatch** profile (everyone who uses it shares its bots and chats), and the
household bots, which an Admin manages but whose chats stay with whoever had
them. That is a statement about the app, not about the host: an Admin has host
access and can download backups, and an agent bot can read anything on the
machine (see below). DisPatch is built for a household or small team, and it
is not designed to resist a determined insider who also controls the host. The
app says this in Settings → Account ("Stored on the host computer"), and once
per sign-in in a short card.

**Agent backends run as your user.** A bot is a local process with your
permissions. Who you let talk to a bot is a real privilege decision, not a
cosmetic one.

## What the defaults expose

The honest list of what a default 4.x install lets through. None of it is a bug; each is a trade for a chat app a
household can walk up to.

- **The default profile is open to anything that can reach the port.** With `lock_target: default_profile` (the
  fresh-install default), a device nobody signed in on is the shared DisPatch profile: it can chat with that profile's
  bots and see that profile's chats, over plain http on a LAN as readily as over a tailnet. It cannot run agents or
  clone voices. Set `lock_target: profile_list` if that is too open for the network, and keep the app off the
  internet unless it is behind TLS and your own authentication.
- **There are no per-profile budgets.** Anyone who can use a bot can spend the connection behind it, and the GPU rig
  behind an image or voice add-on (agents keep their own per-agent dollar limits, see [agents.md](agents.md)). Point
  shared bots at flat-rate or capped accounts, and give a profile a metered connection only on purpose.
- **Chat pictures are protected by an unguessable address, not by a per-owner check.** `/media/<uuid>` names are
  random, but any profile (or the shared one) that has an address can fetch it, and a deleted picture stays fetchable
  by anyone who kept the address until the last message that uses it is gone.
- **An agent bot's own records outlive a Reset.** An agent runtime (OpenClaw, for example) keeps its own session
  transcripts, which DisPatch does not manage.
- **Agent bots can read any chat on the machine**, and an Admin has host access and can download backups. A voice
  recording is protected by the ownership rules but lives in a plain file in `<data>/voices/`.
- **The shared profile can be reset by anyone who reaches it**, by design.

## People, roles and permissions

Everything below is enforced on the server, on every HTTP route and every
WebSocket action; the browser never decides what it may do. PINs and passwords
are optional on every profile except that an Admin signs in with a password.

| Role | Who it is for | What it is |
|---|---|---|
| **Admin** | Whoever runs the install. | Every household permission: people, characters, connections, wiping files, full export, logs, Health, backups and the machine API key. The first Admin, **the Owner**, also holds the host-level powers ([below](#what-only-the-owner-can-do)). Its own chats and bots are private to it. |
| **User** | A person. The default **DisPatch** profile is a User. | Its own bots (create, edit, delete, AI build), copies of household bots, and the household bots an Admin enables (default: all). Bots and chats are private: Admins cannot see them. |
| **Basic** | A simple managed profile. | Only the household bots an Admin adds. Chats are private: Admins cannot read them. No bots of its own. |

The **first Admin is protected**: it cannot be demoted, disabled or deleted (and
no other Admin can change its credentials, sign it out or reset it), so the
install can never lock itself out. Every other profile can be removed. A Basic
profile can be made a User and keeps its chats; a User made Basic keeps its
name and PIN but **every private bot and every chat is deleted**, and the
confirmation names the counts first. Removing a profile deletes what it owns
(chats, its bots, pictures, files, cloned voices) after the same kind of
confirmation.

### What each role may do

This is the permission table, maximum delegated down. An Admin can switch off
any item for one profile, and can switch on the ones marked "switchable".

| | Admin | User | Basic |
|---|---|---|---|
| Chat, voice chat, built-in voices | yes | yes | yes |
| Attach files or photos in a chat | yes | yes | yes |
| Live lookups (web, news, fetch, weather, Wikipedia, rates, time, calculator) | yes | yes | yes |
| Quick actions, reactions, personalities | yes | yes | yes |
| Rename, pin, delete or export own chats; Reset to default | yes | yes | yes |
| Own appearance, theme, language, PIN | yes | yes | yes |
| File Server | yes | upload, plus list and download own files | upload only |
| Image generation (Image Studio, pictures in chat) | yes | yes | off (switchable) |
| Voice cloning / Voice Lab | yes | yes | off (switchable) |
| Own bots, AI build, copy household bots | yes | yes | no |
| Choose a bot's backend | yes | from household connections the Admin allows | no |
| OpenClaw agent bots / Tasks | yes | off, switchable (see warning) | off, switchable (see warning) |
| Benchmark Board, StudioForge panel | yes | no | no |
| Mail, Clients, Job Board (personal data) | yes | never | never |
| Coding terminal, Local Viewer (host) | the Owner only | never | never |
| Profiles and permissions, household bots, connections and keys, plugins, voice setup, backups, security, audit log | yes | never | never |

**Agent bots can run commands on this machine, including reading anyone's
chats.** When an Admin switches them on for a profile, the screen says so
plainly. A gateway (agent) bot runs with its own OpenClaw configuration, tools
included, whoever talks to it; give it only to people you would hand a shell
to. An advisor's research and hand-off markers need the same permission;
without it the advisor answers with them switched off.

A **permission** is a named capability (`use_chat`, `use_voice`, `use_images`,
`attach_files`, `use_lookups`, `files_upload`, `files_read`, `voice_lab`,
`create_characters`, `choose_backend`, `dispatch_agents`,
`approve_agent_actions`, `use_tools`, `manage_*`, `view_activity`,
`admin_system`, and a few more). The role-to-permission matrix, with its three
columns (on, switchable, never), is in
[contracts/permissions.json](contracts/permissions.json); the permission each
route and WebSocket action requires is in
[contracts/routes.json](contracts/routes.json). Both are generated from the code.
A grant outside a role's "switchable" column is refused with 422.

- **API keys are household connections.** A provider key lives in the secret
  store behind a [connection](connections.md) that every profile's bots may
  use (one account serves everyone); nobody but an Admin sees a key, and no
  route returns one. A User picks a backend only among the connections an
  Admin allows for that profile.
- **No daily limits.** There are no per-profile budgets, quiet hours or minute
  caps. The one limit left is plumbing: a machine caller (an agent handing a
  document back with `POST /api/upload`) shares a 200 MB-a-day byte guard
  (`DISPATCH_MACHINE_UPLOAD_QUOTA`), so a looping agent cannot fill the disk.
- **Reset to default** (Settings → You → Account) deletes every custom bot and
  every chat in the profile, then gives an Admin or User a fresh Atlas and a
  Basic profile its assigned household bots again. The confirmation names the
  counts. Cloned voices are kept. By default any profile can reset itself,
  **including anyone using the shared DisPatch profile**; an Admin can also
  reset a profile from People without seeing what is in it.
- **Copying a bot** copies its setup only (name, picture, personality, voice,
  backend), never its chats. A User copies a household bot into their own
  profile; an Admin can copy one into a profile. Agent bots cannot be copied.
- **Every profile gets its own Atlas.** An Admin or User created later gets a
  personal copy of the shipped direct-API bot. The first Admin of a fresh
  install uses the household Atlas, and a Basic profile uses the household
  bots it was given.
- **Per-profile voice clones.** With a voice add-on that declares it, cloned
  voices belong to the profile that made them (gated by `voice_lab`, not by
  plugin administration); see [voice.md](voice.md). Voice cloning is never on
  for the shared default profile.
- **Session timeouts** are per role (`session_timeout_user`,
  `session_timeout_basic` in `security.yaml`; Admin sessions follow
  `lock_timeout_seconds`) and can be overridden per profile.
- **Every admin action is audited** (who, what, to whom; rows carry ids and
  counts, never names or content) in **People & Access → Activity & audit**.

### What only the Owner can do

Admins run the household; only the Owner runs host-level things. The Owner is the
first, protected Admin (the one that cannot be demoted or deleted). Since 4.3 anything
that runs a program on the host computer, reads its files, or hands out a secret the
host holds needs the Owner, even though every Admin holds `admin_system`. A second Admin
who tries gets 403 `host_owner_only`, and the app shows those controls as "Owner only".
On an install with one Admin nothing changes.

| Only the Owner | Every Admin |
|---|---|
| The coding harness (it runs code on this computer) | People, profiles, PINs and roles |
| The local file viewer (it reads files on this computer) | Characters: household bots, templates and copies |
| The MailForge and Clients panes, and the practice proxy (they open other services on this computer) | Backups, the support bundle, storage, maintenance and retention |
| Recovering a chat from an agent's transcript files, and listing, reading or importing OpenClaw sessions (`/api/openclaw/sessions`, `/transcript`, `/import`) | Network exposure and the network wizard |
| Typing a local file path (`[[media:/path]]`) into a chat and having it copied in | Plugins, the dashboard and the data invariants |
| Settings that name a program or a file on this computer (`tools.openclaw_bin`, `connections.image_cli`, `connections.gpu_cli`, `tools.harness_unit`, `system.log_file`, `tools.mail_data_dir`, `tools.practice_pin_file`) | Every other setting an Admin could change before |
| Agent shell and file tools, fewer "always ask" tools, local-program and privileged MCP servers, unattended tools, a repository's own settings, shell commands that may run without asking, a task's repository folder | Defining and publishing agents within those limits, and editing an agent the Owner approved |
| Using a server environment variable as a connection's key, moving a connection that holds a key to another server, changing a connection's required permission | Adding a connection with a pasted key, renaming it, changing its model |
| Outside sign-in (the identity provider that decides who signs in) | Linking their own profile to it once it is on |
| Showing the generated backup recovery key again | Making, verifying and restoring backups |
| Command-line programs agents may run | Agent schedules (seeing everyone's, pausing or deleting them) |
| Which addresses DisPatch answers to (`people.host_check`, `people.allowed_hosts`, the list of unknown addresses) | |

In the code this is `Requirement.host` in `backend/app/route_policy.py` (`"host": true`
in [contracts/routes.json](contracts/routes.json)), enforced in `policy.authorize`:
`Principal.is_host` is the protected Admin, or an install nobody has claimed yet, and
never a machine caller. `backend/tests/test_i43_host_tier.py` sorts every `admin_system`
route into one column or the other, so a new route cannot slip through.

### Sign-in, lock and the DisPatch profile

**DisPatch** is the default profile: a User with no PIN and its own Atlas, a
playground for new people, **shared on purpose**. Everyone who taps into it
shares its bots and chats, and the app says so in its sign-in popup. An Admin
can downgrade it to Basic or point the lock somewhere else.

- **Lock and unsigned devices** land on the profile named by `lock_target` in
  `security.yaml`: `default_profile` (the DisPatch profile, the fresh-install
  default) or `profile_list` (the sign-in screen with the profile tiles). An
  Admin picks it in People ("When locked, show"). An Admin profile cannot be
  the default profile. If the default profile is deleted, the lock falls back
  to the profile list.
- **The default profile never has a PIN or password** (4.3). A profile with a
  PIN or password cannot be picked as the default (422 `lock_target_invalid`),
  and nobody can set a PIN or password on the current default profile, not an
  Admin and not the profile itself (409 `default_profile_no_credential`; one
  guard in `accounts.update_profile` covers every route). To give that profile
  a PIN, make another profile (or the profile list) the default first.
  Invariant **I12** reports a default profile that is disabled, an Admin or
  holds a credential anyway (a hand edit or a restored backup).
- **The fence.** On whichever profile is the lock target, `dispatch_agents`,
  `approve_agent_actions` and `voice_lab` are never effective and cannot be
  granted (422), whatever its role says. A device that can reach the port
  without signing in is using that profile, so it can never reach an agent.
- **A profile button** switches to your own profile, or anyone else's, with
  their PIN if they have one.
- **The sign-in popup** (privacy card) shows once per sign-in and closes with a
  click anywhere, the close button or Esc. It says where chats are stored,
  that they are private to the profile, that the DisPatch profile is the
  exception, and, when an Admin reset the profile's PIN, who did it and when.
- **Device binding is gone.** There is no "this device opens as that profile"
  setting: a lock always goes to the lock target.

### Old names, new names

| Before 4.0 (code / docs / API) | Now |
|---|---|
| Owner | the **protected Admin** (the first Admin; stored internally as the word `owner`) |
| Member | **User** |
| Restricted, decoy, Safe Mode, locked device, "limited tier" | there is no such tier. A Basic profile is an ordinary managed profile, and a locked or unsigned device is the lock-target profile |
| `audience`, `safe`, `hide_unlocked` on a character | inert legacy data. Who may use a bot is its scope (household or personal), each profile's `household_bots` list and the permission table; `hidden` keeps a bot off the rail |
| per-profile daily budgets, minutes per day, quiet hours, `DISPATCH_DECOY_*` | removed. No limits (the 200 MB machine upload guard stays) |
| `anonymous_access` in `security.yaml` | replaced by `lock_target`; read once by the 4.0 migration |
| supervision, Restricted guardrails and output filter | removed |

The role words `owner`, `member` and `restricted` are still accepted wherever
a role is typed (the command line, old API callers) and mean Admin, User and
Basic.

### First run, and the setup token

A fresh install has no Admin yet. The first account (the protected Admin) is created in the browser **on
the machine DisPatch runs on**, or from any other device with the one-time
**setup token** the server prints in its log at startup:

```
DisPatch has no owner account yet.
 - On THIS computer: open http://localhost:8765 and create it.
 - From another device: use this one-time setup token.
       SETUP TOKEN: ...
```

Until that first Admin exists, the network sees only the app shell and the sign-in
state — never data. (Docker: `docker compose logs app | grep "SETUP TOKEN"`.)
Wrong-token guesses are throttled. The token lives in memory, changes every
start and is spent when the first Admin is created (after `owner reset` it is also kept in
`SETUP-TOKEN.txt`, owner-only, and deleted with it). This replaces 2.x, where the first
network client to set a PIN became the owner.

Until then DisPatch serves a request only when all of these hold (4.3):

- the socket peer is loopback and no proxy header is present (Tailscale Serve and every
  reverse proxy connect from 127.0.0.1 and add one);
- the `Host` header is a loopback name: `localhost`, `127.x.x.x` or `[::1]`, any port;
- for anything but GET, HEAD and OPTIONS, and for every WebSocket handshake, `Origin` is
  absent or equal to `Host`, and `Sec-Fetch-Site` is not `cross-site` or `same-site`.

Anything else gets 403 `setup_required` (sockets close with 1008) and can only finish setup
with the setup token. That stops a web page that points its own name at 127.0.0.1 (DNS
rebinding) or posts from another site while you browse on the same computer. One predicate,
`policy.local_setup_request`, decides it for HTTP, `/ws` and the add-on socket.

`DISPATCH_ALLOW_OPEN_INSTALL=1` restores the 2.x behaviour (an install with no
account is open to the LAN) — only for a network you fully control, and
the log says so loudly. It drops the first two rules but keeps the third: another
site's page still cannot post.

### Lost your sign-in

Anyone with the machine can get back in; nobody without it can. `python -m app users passwd <name>` sets a new password or
PIN, `users add` / `users delete` manage profiles, `owner reset` clears the first Admin's password and PIN and opens setup again
with a new setup token, and a `RESET_LOGIN` file in the data directory (or `DISPATCH_RESET_OWNER` in the environment) does
the same at the next start, deleting itself first. None of it is reachable over the network, and none of it touches chats
or other profiles. A running server follows within a second. Commands for every install, and what is kept:
[accounts.md](accounts.md).

### Sign-in protection

- Passwords and PINs are **argon2id** (19 MiB, 2 passes — the
  OWASP minimum). The protected Admin's PIN stays in `security.yaml` as PBKDF2-HMAC-SHA256
  so the file-edit recovery and `RECOVERY-CODE.txt` keep working; that Admin's
  *password* (recommended) is argon2id.
- **Throttle (4.3: one throttle for every credential).** Owner unlock, profile sign-in and
  switch, the setup token, the recovery code, password and PIN changes, the backup-key
  reveal and outside sign-in all go through `auth_attempts.guarded_verify`:
  1. per device and profile: 5 failures, then a 5 s wait that doubles to about five minutes;
  2. per address: 15 failures, the same wait. IPv4 counts per address, IPv6 per /64;
  3. install-wide: 60 failures in 10 minutes. Past this ceiling only a remembered device of
     the profile being signed in, or a browser on this computer (loopback, no proxy
     headers), has its credential checked: right signs in, wrong is counted and answered
     429. Every other device gets 429 **before** its credential is looked at.

  So your own remembered phone still gets in while someone elsewhere floods the install
  with guesses; a device that was never remembered may have to wait, and that wait is what
  stops the guessing.
- **Picking a profile with no PIN** is limited to 20 sign-ins per address per 10 minutes.
- **Live sessions** are capped at 64 per profile and 2,000 for the install. The oldest
  session that is not a remembered device (never your own) makes room; when every session
  of the profile is a remembered device, the new sign-in gets 429 `too_many_sessions`
  (forget a device in People & Access).
- **Sessions** die when a profile is deleted, disabled, changed to another role,
  or has its PIN/password changed.
- **Forwarded headers.** See [Reverse proxies](#reverse-proxies-and-forwarded-headers) below.
- **CSRF.** Session cookies are `HttpOnly; SameSite=Strict` (and `Secure` over
  TLS). On top of that every state-changing request that carries a session or
  device cookie must come from our own origin: a foreign `Origin`, or
  `Sec-Fetch-Site: cross-site` / `same-site`, is refused with `csrf_origin`. A
  reverse proxy must therefore preserve the `Host` header (Tailscale Serve and
  Caddy do by default).
- **Headers on every response:** `X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, `X-Frame-Options: SAMEORIGIN`, `frame-ancestors 'self'` on
  the shell, `Cache-Control: no-store` on the sign-in and people APIs, and HSTS
  when the request arrived over TLS. The sandboxed viewers keep their own
  stricter policies.

### Reverse proxies and forwarded headers

`X-Forwarded-For` (its rightmost value) and `X-Forwarded-Proto` are believed only when the
socket peer is loopback or inside a trusted proxy range (4.3). From any other peer they are
ignored: the peer address is the client, and a session cookie is `Secure` only on a real
TLS connection. The same rule picks the address the sign-in throttle counts.

- **Tailscale Serve, or a proxy on the same computer:** nothing to set. Loopback is always
  trusted.
- **A proxy on another address** (another machine, or a container on a bridge network):
  name it in `security.yaml`, `trusted_proxies: ["192.0.2.20/32"]`, or in the environment,
  `DISPATCH_TRUSTED_PROXIES=192.0.2.20/32` (comma separated; added to the file's list).
  Name the proxy's own address, as narrowly as you can: anything inside the range may claim
  to be any client.
- **The shipped Caddy profile** (`docker compose --profile caddy`): Caddy sits on a fixed
  address of a declared subnet, `172.29.87.10`, and compose sets <!-- scrub-ok: the compose project's own private subnet, not a real LAN -->
  `DISPATCH_TRUSTED_PROXIES=172.29.87.10/32` for the app: that one address, not the whole <!-- scrub-ok: the compose project's own private subnet, not a real LAN -->
  bridge. If the subnet clashes with one you already have, set `DISPATCH_NET_PREFIX` in
  `.env` (three numbers, e.g. `172.30.42`); the subnet, Caddy's address and the trusted
  address all follow it.

Without this, every client behind a proxy shares the proxy's throttle bucket and cookies
lose `Secure` behind a TLS proxy on another address.

### Addresses DisPatch answers to (DNS rebinding)

A web page on another site can re-point its own name (`evil.example`) at your computer's
address and then talk to DisPatch from your browser. It sends its own name in the `Host`
header, so on a claimed install it would land on the PIN-less default profile, and the CSRF
check would not stop it (its Origin matches its own Host). DisPatch therefore compares the
`Host` name (the port is ignored) with the names it answers to:

- `localhost` and every loopback address (`127.0.0.0/8`, `::1`);
- this computer's name (`socket.gethostname()` and its FQDN), `<hostname>.local`, and
  `<hostname>.<tailnet>.ts.net` (Tailscale Serve and MagicDNS keep that name in `Host`); <!-- scrub-ok: Tailscale's own domain suffix, not a real tailnet -->
- every address of the computer's network interfaces (LAN, tailnet), re-read at most once a
  minute so Wi-Fi changes and Tailscale coming up are picked up;
- the host of `DISPATCH_PUBLIC_URL` / `system.public_url` and of `DISPATCH_CALLBACK_BASE`;
- each proxy in `trusted_proxies` written as one address (a range adds nothing);
- `allowed_hosts` in `security.yaml` (Settings → System → Network → *Other addresses for
  DisPatch*), plus `DISPATCH_ALLOWED_HOSTS` (comma separated). An entry is a name, an address
  or `*.name` (any name ending in `.name`, not `name` itself); a bare `*` is refused.

`host_check` in `security.yaml` (Settings → *Unknown addresses*; `DISPATCH_HOST_CHECK`
overrides it) decides what a request with any other name gets:

- **`log`** (the default): it is let through, counted, and logged once per name per hour.
  Settings → System → Network lists the unknown names with a count and an **Allow** button
  (`GET /api/system/host-check`, the Owner's).
- **`enforce`**: it is refused before sign-in is looked at - `421 host_not_allowed` for
  `/api/*` and `/ws`, a short page naming the address for anything else, and a WebSocket is
  closed (1008). A request with no `Host` at all (HTTP/1.0) is refused too.

Switch to `enforce` once that list shows nothing you use. If a name you need is refused,
add it to `allowed_hosts`, or set `host_check: log` in `security.yaml` and restart DisPatch
(or start it with `DISPATCH_HOST_CHECK=log`); on-box scripts that call `127.0.0.1` are never
affected.

### What the sign-in page reveals

`GET /api/auth/status` is public (the lock screen needs it). On a claimed install it lists
each profile's **display name, role, avatar and which credential it takes** so a person
can pick their tile - never a hash, a PIN length or a session. With `lock_target: default_profile`
it also names the default profile. If you do not want names visible to your network, put
the app behind your reverse proxy's own authentication, or keep it off the network.

### Machine access (agents, crons, the image server)

A machine is not a person and holds no role. It is a **principal with named
scopes** (`machine:inject`, `machine:threads`, `machine:image_jobs`, …; the list
is in `contracts/permissions.json`). It proves who it is with the install's API
key (`X-API-Key` or `Authorization: Bearer`), which an Admin generates in
**People & Access → Machine access** (shown once) and can narrow to a subset of
scopes (`machine_scopes` in `security.yaml`).

**A presented key is judged on its own (4.3).** A request that sends `X-API-Key` or
`Authorization: Bearer` is a machine only if the key is right; a wrong key is 401
`invalid_api_key`, also from this computer and also with loopback trust on.

**`machine_loopback_trust`** decides whether a program on this computer that sends **no**
key counts as a machine (the 2.x behaviour, kept so old crons keep working):

- **A fresh 4.3 install starts with it off** (`false`): every machine caller needs the key.
- **An upgraded install keeps what its file says.** A `security.yaml` written before 4.3
  without the key reads `true`, so crons keep working after the upgrade.
- While it is on, the server warns at every start, and the start-up posture line says so.

To change it, switch **Trust scripts on this machine** in Settings → People & Access →
Machine access (it saves at once), or edit `security.yaml` with the server stopped:

```yaml
machine_loopback_trust: false
```

Turn it off once every program on the computer sends the key; then a machine is identified
by its credential alone, not by where it runs. The host scripts shipped in `scripts/` (`dispatch-jobs`,
`dispatch-avatar-rotate`, `local-chat-daily.sh`) send it from `$DISPATCH_API_KEY`,
else from an owner-only file, `~/.config/local-chat/machine.env` (one line,
`DISPATCH_API_KEY=<key>`; `$DISPATCH_KEY_FILE` moves it), and pass it to `curl`
as a config file on a pipe so it never appears in a process list. Give your own
crons the same file. An agent hands a document back with `POST /api/upload`,
which needs `machine:upload` and shares one daily byte guard (`DISPATCH_MACHINE_UPLOAD_QUOTA`,
200 MB a day), so a looping agent cannot fill the disk. A keyless upload is never a
machine's: it is judged as the unsigned device it is (the lock-target profile, or refused). The API key itself is stored in `security.yaml` (mode 0600, written atomically) in
plain text, as before; it is shown once when generated and never returned by the API again. Anything that passed through a reverse proxy (`X-Forwarded-*`,
`Tailscale-*` headers) is never treated as on-box. The image server's completion
callback is the one other machine route; it carries its own per-job token.

## Outside sign-in (OpenID Connect)

Off by default; the Owner turns it on. How to set it up is in [oidc.md](oidc.md). What it
guarantees:

- Authorization code with PKCE (S256). `state` and `nonce` live in a 10-minute, single-use
  record on the server, keyed by the `__Host-dispatch-oidc` cookie (`Secure`, `HttpOnly`,
  `SameSite=Lax`, `Path=/`). It is Lax because the provider's redirect back is cross-site;
  the session cookie it then issues is `SameSite=Strict` as always. So it needs an https
  `DISPATCH_PUBLIC_URL`.
- The ID token must be signed with RS, PS or ES 256-512 (`none` and the HS algorithms are
  refused) by a key from the issuer's own https host (cached an hour, fetched again for an
  unknown key id at most every 30 seconds). `iss` must equal the configured issuer byte for
  byte, `aud` must contain the client id, `azp` must equal it when present or when there are
  several audiences, and `exp`, `nbf`, `iat` and `nonce` are checked with one minute of
  leeway.
- Discovery, the key set and the token endpoint are fetched only from the issuer's host,
  over https, with redirects off and a 512 KB cap.
- **Only an existing link signs in.** No profile is created, nothing is matched by email,
  and a disabled profile is refused. Linking needs the profile's own session (or the
  Owner's) and a completed round trip at the provider; one outside account links one
  profile. Failures are throttled like a PIN.
- **The Owner keeps a local credential.** Outside sign-in never replaces the PIN or
  password where one is asked for, and removing the Owner's last local credential while a
  link exists is refused (422 `oidc_last_credential`, invariant I11).

## Image generation hard lines

DisPatch does not filter what you ask for in general; image generation has two
hard lines, and the server enforces only those. A prompt is refused, with a
plain message, when it asks for (1) anything sexual involving a person under
18 (depictions are adult-only), or (2) sexual imagery of a real, identifiable
person (a named person, a celebrity, someone the requester knows, or
deepfake/face-swap wording). The check (`app/image_guard.py`) runs on every
image prompt, whether it comes from a bot's `[[pic:]]` marker, the image-jobs
API or the Image Studio, before a provider or the image server is contacted. It
is a text heuristic with narrow triggers, so it is one layer, not a guarantee:
the provider's own moderation still applies, and everything outside these two
lines stays unrestricted.

## Connections, keys and backups (3.0)

- **Provider keys** (household connections, shared by every profile's bots; only an Admin sees or changes one) live in the secret store (the OS keyring, else a file made private to the service user: `0600`, or an owner-only ACL on Windows) behind a [connection](connections.md);
  no route returns one and `config.yaml` no longer holds them after the upgrade migrates them.
  Naming a **server environment variable** as a key, or moving a connection that holds a key to another server
  (scheme, host or port), is the Owner's alone (403 `env_key_owner_only`): otherwise any Admin could send a secret
  the host holds to an address of their choosing. **Test** never sends a saved key to another address, for anybody.
- **Unknown settings are masked.** `GET /api/settings` sends the value of a key the schema does not know as
  `{masked, type, length}`, never the value.
- **Connection addresses** are validated and re-checked on every use: link-local and cloud-metadata addresses are refused
  (`connections.allow_private_targets` is the admin's explicit opt-out), so a model-server address cannot be used to
  reach internal services.
- **Backups** carry every stored secret (connection keys, agent secrets, the outside sign-in client secret) inside
  the encrypted archive, are encrypted (AES-256-GCM, scrypt) and restore refuses anything outside DisPatch's own files; the
  **support bundle** removes keys, tokens, PINs and passwords and leaves out character prompts unless asked
  ([backups.md](backups.md)). These, and per-profile export / erase, are host control (`admin_system`): the
  Admins'. Nobody but the protected Admin may export or erase that profile.
- **Plugins** declare what they ask for and a new one must be approved before the Plugins switch turns it on
  ([extensions.md](extensions.md#manifests-permissions-and-approval-30)).

## Checklist

**Create the first Admin account immediately.** With no Admin, the app answers only
the machine it runs on. The first-run page asks for a name, a password and,
optionally, a PIN for quick unlock. The log says so loudly if you bind to
anything other than `127.0.0.1` with no Admin.

**Do not expose port 8765 to the internet directly.** Port-forward 443 to a
reverse proxy that terminates TLS, never 8765 to the app. On a LAN it is
perfectly normal to reach the app on 8765 — that is what the deployment guides
set up — but the moment it is reachable from outside your network it belongs
behind TLS. See [remote-access.md](remote-access.md).

**Keep the recovery code.** It is written to `RECOVERY-CODE.txt` in the data
directory when the protected Admin's PIN is set. Losing both it and the PIN means editing
`security.yaml` on the host to clear the PIN — which is the right proof of
ownership for self-hosted software, but it means you need shell access. An Admin
with a password can always sign in with it.

Using the code on the lock screen removes the PIN on a one-person install. Once
other people have profiles it **replaces** the PIN instead: type the code and a
new PIN together (`POST /api/auth/recover` with `new_pin`). Removing the only
protected-Admin credential there would leave the install with no way in, so a request
without a new PIN is refused with `recovery_needs_new_pin` and nothing changes.

**Decide who can reach the port.** Anyone who reaches the port without signing
in lands on the lock-target profile (the shared DisPatch profile by default),
so treat its bots and chats as public to your network and keep it a plain
User: it can never run agents or clone voices. If the app is reachable from
the internet, set `lock_target: profile_list` (the sign-in screen only) or put
it behind your reverse proxy's own authentication. Give each person their own
profile; their chats are private to it.

**Watch what a connection can spend.** There are no per-profile budgets, so
anyone who can use a bot can spend the connection behind it. Put a metered
provider key only behind a connection an Admin allows for the profiles that
should use it, and prefer a flat-rate account for the shared one. A looping
agent cannot fill the disk: machine uploads share one 200 MB-a-day guard.

**Run one process.** Sessions, connections and rate limits are in-process state.
The app takes an exclusive lock on its data directory and refuses to start
twice, so `--workers 4` fails loudly rather than half-working.

**Do not run as root.** The container image runs as UID 1000. If you run bare,
use a dedicated service account.

**Check your backups restore.** DisPatch takes rotating integrity-checked
snapshots, but a snapshot is not a backup until it is off the machine and you
have restored one. Note that snapshots cover the database, not the media and
file blobs — copy the whole data directory.

## The local viewer

The local viewer hands a browser bytes from the host's filesystem, which makes
it the widest read surface in the app. Its threat model, in full:

- **The Owner only (host tier, 4.3).** A User, a Basic profile or a second Admin never sees the
  affordance, *and* the server refuses every viewer route for anyone but the Owner (403
  `host_owner_only` for another Admin). Both halves are
  required; neither is sufficient. It is also deliberately absent from the
  machine-inbound surface, so the inbound API token — which belongs to on-box
  automation and, in some setups, a separate render machine — is not a key to
  your home directory.
- **Allowlist, not blocklist, first.** Nothing is served unless it resolves
  (symlinks followed) to inside a directory you configured. The default
  configuration is *no directories*, so the feature does nothing until you turn
  it on.
- **A deny list your configuration cannot override** sits on top of that: SSH
  and GPG keys, secret stores, agent state, `/etc /proc /sys /dev`, systemd
  units, this app's own credentials and database, and a filename pattern list
  for keys, `.env` files and databases. A root of `~` is therefore usable
  without exposing `~/.ssh`.
- **Dotfiles below a root are refused** unless you ask for them. Dotfiles
  *above* a root are your business: `~/.agent/workspace` is a legal root.
- **One refusal message.** Outside a root, on the deny list, or hidden — all
  three answer `Not served by the local viewer`. The client is never told which,
  so the endpoint cannot be walked to map the disk. Every refusal is logged with
  the path and the caller, and counted in `/api/health` as `viewer_denied_24h`.
- **Framing is sandboxed with an opaque origin.** HTML is served with
  `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups
  allow-modals allow-downloads; frame-ancestors 'self'` — never
  `allow-same-origin`. A framed page can run its own scripts but cannot read
  DisPatch's DOM, cookies or storage.
- **Framed pages load their assets through a scoped ticket, not the cookie.**
  An opaque origin sends no cookie, so a framed page is served from
  `/local/view/<ticket>/…`: a random capability minted by the authenticated
  `stat` call, bound to the caller's address, expiring after two idle hours,
  and valid only inside the page's own directory. A hostile HTML file you open
  can `fetch()` its own folder and nothing else; the ticket route refuses
  parent, sibling and symlink escapes with the same uniform message and is
  never on the machine-inbound surface. Every non-HTML
  viewer response gets `default-src 'none'; sandbox; frame-ancestors 'self'`,
  and all of them get `nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: private, max-age=0, must-revalidate` and
  `X-Robots-Tag: noindex`.
- **Content types come from a fixed extension table, never from sniffing**, so a
  `.txt` that begins with `<html>` cannot become an active document, and an
  unknown extension is sent as an attachment rather than rendered.
- **Directory listings never name an entry the file route would refuse**, so a
  listing cannot be the disclosure.

What it does *not* protect against: an operator who deliberately roots the
viewer at a directory full of things they did not want to share, or a **hard
link** inside a root to a file outside it (a hard link has no target to
resolve; only symlinks are seen through). The allowlist is the control; choose
it deliberately. The `*secret*` and `*credentials*` filename patterns are
deliberately broad and will also hide innocent files with those words in their
names. See
[configuration.md](configuration.md#local-viewer).

## Tools

A **static** tool (`<data dir>/tools.yaml`, `kind: static`) serves a directory
of files at `/tools/<id>/` — the same kind of disk-read surface as the local
viewer, so it is built the same way, around refusing. A **url** tool is only
framed by the browser; the server's one contact with it is a reachability
probe. Contract: [design/2026-09-25-tools-plugins.md](design/2026-09-25-tools-plugins.md).

- **One allowlisted root per tool.** A static tool serves nothing outside the
  directory named in its `root`. Every request resolves with symlinks followed
  and must land inside that resolved root; `..`, dot-components and backslashes
  are refused before resolution, so they are never normalised away.
- **The local viewer's deny list, shared, not copied.** The same built-in deny
  subtrees (SSH/GPG keys, secret stores, agent state, `/etc /proc /sys /dev`,
  systemd units, this app's credentials) and the same filename patterns (keys,
  `.env` files, databases, `security.yaml`, `trusted-devices.yaml`, their
  backup siblings) apply to every path below a tool root. One list, so the two
  surfaces cannot drift apart.
- **The app's own data directory is never a root.** A `root` that is, or is
  under, the data directory is refused when the manifest is saved *and* when a
  hand-edited `tools.yaml` is read — otherwise a tool rooted there
  would hand `files/`, `media/` and `tools.yaml` itself to whoever may open it.
  The resolver refuses it again at serve time.
- **Uniform refusals.** For an Admin, anything not served — unknown tool,
  disabled tool, traversal, symlink escape, denied name, missing file —
  answers the same `404 Not found`. Tool panels are Admin-only (`use_tools` is
  never in a User's or Basic profile's column); for any other caller every tool
  id, existing or not, answers the same `403`, so a profile cannot enumerate the
  manifest. A tool's old `safe` flag is legacy data and is never read.
- **Framing is sandboxed with an opaque origin.** HTML is served with
  `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups;
  frame-ancestors 'self'` — never `allow-same-origin` — and the client frames it
  under the matching `sandbox` attribute. Every non-HTML response gets
  `default-src 'none'; sandbox; frame-ancestors 'self'`, and all of them get
  `nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: private, max-age=0, must-revalidate` and
  `X-Robots-Tag: noindex`. Content types come from the viewer's extension
  table; an unknown extension is an attachment.
- **Framed pages load their assets through a ticket, not the cookie.** With a
  PIN set, an operator's request for an HTML page that will be **framed**
  (`Sec-Fetch-Dest: iframe`/`frame`, or no fetch metadata at all) is redirected
  to `/tools/_t/<ticket>/…`. The ticket is a random capability scoped to the
  **whole tool root** (a tool is one site, so its pages link each other), bound
  to the caller's address, and expiring after **two idle hours** (sliding).
  A top-level navigation (`Sec-Fetch-Dest: document`, "open in new tab") carries
  the cookie itself and is served directly — no ticket is minted for it. Every
  guard above still applies below a ticket.
- **Refresh runs a fixed argv, operator-only.** A `refresh` block is a list of
  arguments, never a shell string, and can only come from editing
  `tools.yaml` by hand — the Settings API refuses to introduce or change one.
  Only an Admin (`manage_plugins`) can trigger it, or anyone on an
  install with no Admin yet (set one). Never a User or Basic profile, never
  the machine-inbound API token. It runs as the service
  user with `shell=False`, stdin closed, its own process group (killed whole on
  timeout), and an environment scrubbed of credential-shaped names
  (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PIN`, `*_PASSWORD`) and of live
  capabilities (`SSH_AUTH_SOCK`, `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS`,
  `WAYLAND_DISPLAY`, `DISPLAY`).
- **The url probe never reads a body.** `HEAD`, falling back to a streamed
  `GET` whose body is never read when the server answers 405; redirects are
  not followed; the whole probe is bounded to a few seconds.

What it does *not* protect against: the operator choosing a root full of
things they did not mean to publish (the root is the control, exactly as with
the viewer), a hard link inside a root, or what a refresh command itself does —
it runs with the service user's full rights, which is why only the file can
define it.

## Privacy mode

For a device you would rather leave no trace on — a shared tablet, a phone that
travels — privacy mode makes the client hold nothing locally:

- The service worker is unregistered and its cache purged, so no application
  shell or content is stored offline.
- Preferences move to session storage, so they die with the tab.
- The session cookie becomes non-persistent, and "remember this device" is
  refused.
- Drafts are never saved.

The cost is honest: the app will not work offline and will be slower to start,
because nothing is cached. Enable it per device in **Settings → Account → Pictures & privacy**.

Note what this does *not* do. It stops the device retaining data. It does not
hide anything from the server, which still stores the full history — that is
what the delete controls are for.

## Reasonable expectations

DisPatch is a small application maintained by volunteers. It has had several
focused security reviews, it fails closed by default, and its access control is
covered by tests. It has not had a professional audit or a formal threat-model
review.

Run it for your household. Don't run it for a newsroom's source communications.

## Upgrading to 4.3

4.3 has no migration step; it adds tables and files that 4.2 ignores. What changes for
people:

- **A second Admin loses the host-level powers** in the table [above](#what-only-the-owner-can-do).
  An install with one Admin sees no change.
- **Unattended agents that use Bash lose Bash** until the Owner fills their list of shell
  commands that may run without asking (agent editor). An interactive agent whose Admin had
  taken Bash out of "always ask" asks for each command again.
- **Agent tasks get default limits** ($2.00 and 60 turns per task, $10.00 per agent per
  day) unless the agent has its own; chat turns are never limited by them.
- **`machine_loopback_trust`**: an existing `security.yaml` keeps what it says; only a brand-new
  install starts with it off.
- Behind a reverse proxy on another address, name it in `trusted_proxies` (see
  [Reverse proxies](#reverse-proxies-and-forwarded-headers)); until you do, the app ignores its
  forwarded headers.

## Upgrading to 4.0

The server runs the migrations itself when it starts, after taking a backup
(`<data>/backups/<version>-<stamp>/`). To see first, with the server stopped:

```
uv run python -m app.migrate --dry-run --data-dir <your data dir>   # what would change; writes nothing
uv run python -m app.migrate --data-dir <your data dir>             # or just start the server
uv run python -m app.migrate --restore <data dir>/backups/<version>-<stamp>   # put the backup back (server stopped)
```

Roles map as Owner → the protected Admin, Admin → Admin, Member → User, Restricted
→ Basic. Everyone keeps their PIN and password. Every conversation keeps the owner
migration 009 gave it. If exactly one Restricted profile had no PIN and
`anonymous_access` was `restricted`, it becomes the shared **DisPatch** default
profile (as a User) and the lock lands on it; otherwise `lock_target` is
`profile_list`. `--default-profile <id|name>` picks it explicitly. Characters
become household bots, and a 3.x `hide_unlocked` bot becomes `hidden`. A Restricted
profile's character list becomes its `household_bots`. Existing bots are not
copied for anyone; a profile gets its own Atlas when it is created or on its first
Reset. The changelog entry has the full list:
[4.0.0](../CHANGELOG.md#400--2026-10-23).

Earlier upgrades: the exact steps for 3.0 are in the
[3.0.0 changelog entry](../CHANGELOG.md#300--2026-10-18).
