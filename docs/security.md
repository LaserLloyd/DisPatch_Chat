# Security guide for operators

To report a vulnerability, see [SECURITY.md](../SECURITY.md). This page is about
running a DisPatch Chat install safely.

Most of what follows is checked automatically. Open **Settings → Dashboard**;
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

**Multi-user is isolation, not adversarial separation.** One member must not be
able to read another's conversations; if they can, it is a bug and we want the
report. But DisPatch is built for a household or small team, and it is not
designed to resist a determined insider who also controls the host.

**Agent backends run as your user.** A bot is a local process with your
permissions. Who you let talk to a bot is a real privilege decision, not a
cosmetic one.

## People, roles and permissions

Since 3.0 DisPatch has real accounts. Everything below is enforced on the server,
on every HTTP route and every WebSocket action; the browser never decides
what it may do.

| Role | Who it is for | What it can do |
|---|---|---|
| **Owner** | You. Exactly one. | Everything, including host control (the coding harness, wiping files, full export, logs) and the machine API key. |
| **Admin** | A trusted adult. | Everything except host control and ownership: manage characters, connections, plugins, people (Members and Restricted), settings, approve agent actions, see the activity summary. Needs a password. |
| **Member** | An adult or teen household member. | Chat, voice, images, the tools you enabled, notifications, rename/delete their conversations. **Not** agents (see below) and not configuration. Needs a PIN. |
| **Restricted** | A child or a guest. | Chat and voice with the characters whose *audience* is "Restricted only" or "Everyone". No tools (unless you flag one `safe`), no agents, no settings, no file access, no history management. Daily budgets and time limits apply. |

A **permission** is a named capability (`use_chat`, `use_voice`, `use_images`,
`use_tools`, `dispatch_agents`, `approve_agent_actions`, `manage_agents`,
`manage_connections`, `manage_characters`, `manage_plugins`, `manage_users`,
`manage_settings`, `view_activity`, `admin_system`, and a few more). The
role-to-permission matrix is in [contracts/permissions.json](contracts/permissions.json); the
permission each route and WebSocket action requires is in
[contracts/routes.json](contracts/routes.json). Both are generated from the code.

- **Per-profile grants and denies.** The Owner can give one Member
  `dispatch_agents`, or take `use_voice` away from one profile. A Restricted
  profile can never be granted a management permission, and `admin_system` is
  Owner-only whatever anyone types.
- **Dispatching an agent is its own permission.** A character backed by an agent
  on the host (OpenClaw) runs with your user's rights. A Member cannot talk to
  one until you grant `dispatch_agents`; a Restricted profile's turn on such a
  character always runs *locked*.
- **Shared devices.** In **Settings → People & Profiles → This device** you can
  bind a browser to a Member or Restricted profile: the kitchen tablet then opens
  as "Mia" with nobody signing in. Admins and the Owner cannot be a device
  default — stepping up always needs their credential.
- **Quick switch.** The lock screen lists the profiles; tap yours and enter your
  PIN (Member), your password (Admin) or your password/PIN (Owner). A Restricted
  profile without a PIN is just a tap.
- **Session timeouts** are per role (`session_timeout_member`,
  `session_timeout_restricted` in `security.yaml`; Owner and Admin follow
  `lock_timeout_seconds`) and can be overridden per profile.
- **Every admin action is audited** (who, what, to whom) in
  **People & Profiles → Activity → Audit**.

### Old names, new names

| Before 3.0 (code / docs / API) | Now |
|---|---|
| decoy, Safe Mode, locked device, "limited tier", `request.state.decoy` | **Restricted** (role); `"decoy": true` stays in 403 bodies for old clients |
| Unlocked, operator, full session | **Owner** (or **Admin**) |
| the one PIN | the **Owner's PIN**; Members have their own, Admins a password |
| `safe` / `hide_unlocked` on a character | **`audience`**: `all`, `members`, `restricted` (the old flags are still read and written for one release) |
| `DISPATCH_DECOY_*_QUOTA` | still honoured; budgets are now per profile (below) |
| per-IP counters in memory | per-profile daily budgets in `chats.db` |

### First run, and the setup token

A fresh install has no owner. The first account is created in the browser **on
the machine DisPatch runs on**, or from any other device with the one-time
**setup token** the server prints in its log at startup:

```
DisPatch has no owner account yet.
 - On THIS computer: open http://localhost:8765 and create it.
 - From another device: use this one-time setup token.
       SETUP TOKEN: ...
```

Until the owner exists, the network sees only the app shell and the sign-in
state — never data. (Docker: `docker compose logs app | grep "SETUP TOKEN"`.)
Wrong-token guesses are throttled. The token lives in memory, changes every
start and is spent when the owner is created. This replaces 2.x, where the first
network client to set a PIN became the owner.

`DISPATCH_ALLOW_OPEN_INSTALL=1` restores the 2.x behaviour (an install with no
account is open to the LAN) — only for a network you fully control, and
the log says so loudly.

### Restricted profiles

- **Characters.** A Restricted caller sees and reaches only characters whose
  audience is `restricted` or `all`; frames, avatars, reactions, threads and voice
  sockets about any other character are refused or never sent.
- **Budgets per profile** (People & Profiles → edit): messages, new
  conversations, ratings and upload megabytes per day, **minutes per day** of
  active time and **quiet hours** (for example 20:30–07:00). They are stored in
  `chats.db`, survive a restart, and follow the *person*, not the network
  address — two children behind one router each get their own. An anonymous
  device without a profile is still budgeted per address (`DISPATCH_DECOY_*`
  defaults).
- **Guardrails.** Restricted turns run locked: no tools, no lookups beyond the
  character's allow-list, no agent hand-offs, and the guardrail system prompt and
  the output filter hook stay in place.
- **Activity summary.** The Owner (and Admins with `view_activity`) can see counts
  per day — logins, messages, new conversations, uploads, limits hit — never the
  content. The Restricted UI shows a banner saying so, and a "my activity" panel
  with the same numbers.

### Sign-in protection

- Passwords and PINs for Members/Admins are **argon2id** (19 MiB, 2 passes — the
  OWASP minimum). The Owner's PIN stays in `security.yaml` as PBKDF2-HMAC-SHA256
  so the file-edit recovery and `RECOVERY-CODE.txt` keep working; the Owner's
  *password* (recommended) is argon2id.
- **Throttle.** Five failures lock that *device and profile* for 5 s, doubling to
  about five minutes; fifteen from one address lock the address; a global ceiling
  catches a distributed guesser. One noisy client can no longer lock everyone out
  (2.x had one process-wide counter).
- **Sessions** die when a profile is deleted, disabled, changed to another role,
  or has its PIN/password changed.
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

### What the sign-in page reveals

`GET /api/auth/status` is public (the lock screen needs it). On a claimed install it lists
each profile's **display name, role, avatar and which credential it takes** so a household
member can pick their tile - never a hash, a PIN length or a session. If you do not want
names visible to your network, set `anonymous_access: none` and put the app behind your
reverse proxy's own authentication, or keep it off the network.

### Machine access (agents, crons, the image server)

A machine is not a person and holds no role. It is a **principal with named
scopes** (`machine:inject`, `machine:threads`, `machine:image_jobs`, …; the list
is in `contracts/permissions.json`). It proves who it is with the install's API
key (`X-API-Key` or `Authorization: Bearer`), which the Owner generates in
**People & Profiles → Machine access** (shown once) and can narrow to a subset of
scopes (`machine_scopes` in `security.yaml`).

For installs upgraded from 2.x, on-box scripts on `127.0.0.1` are still trusted
without a key while **`machine_loopback_trust`** is `true` (the default) — the
2.x behaviour, kept so your crons keep working. Once they send the key, set it to
`false` and a machine is identified by its credential alone, not by where it
runs. The API key itself is stored in `security.yaml` (mode 0600, written atomically) in
plain text, as before; it is shown once when generated and never returned by the API again. Anything that passed through a reverse proxy (`X-Forwarded-*`,
`Tailscale-*` headers) is never treated as on-box. The image server's completion
callback is the one other machine route; it carries its own per-job token.

## Checklist

**Create the owner account immediately.** With no owner, the app answers only
the machine it runs on. The first-run page asks for a name, a password and,
optionally, a PIN for quick unlock. The log says so loudly if you bind to
anything other than `127.0.0.1` with no owner.

**Do not expose port 8765 to the internet directly.** Port-forward 443 to a
reverse proxy that terminates TLS, never 8765 to the app. On a LAN it is
perfectly normal to reach the app on 8765 — that is what the deployment guides
set up — but the moment it is reachable from outside your network it belongs
behind TLS. See [remote-access.md](remote-access.md).

**Keep the recovery code.** It is written to `RECOVERY-CODE.txt` in the data
directory when the Owner's PIN is set. Losing both it and the PIN means editing
`security.yaml` on the host to clear the PIN — which is the right proof of
ownership for self-hosted software, but it means you need shell access. An owner
with a password can always sign in with it.

**Decide who is Restricted.** Anyone who reaches the port without signing in is
Restricted — useful for a tablet on the kitchen counter, and the reason the
audience of each character is a decision, not a default. Give children their own
profiles (budgets follow the profile), bind their tablets to them, and turn the
anonymous Restricted view off entirely if the app is reachable from the internet
by putting it behind your reverse proxy's own authentication.

**Watch the budgets.** The message budget matters most: every message to a bot
spawns an agent turn, which may cost real money. Defaults are in
[configuration.md](configuration.md) (`DISPATCH_DECOY_*`); per-profile values
override them.

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

- **Owner only (`admin_system`).** A Restricted profile never sees the affordance, *and* the
  server refuses every viewer route for any role below Owner. Both halves are
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
  hand-edited `tools.yaml` is read — otherwise a `safe: true` tool rooted there
  would hand `files/`, `media/` and `tools.yaml` itself to a Restricted device.
  The resolver refuses it again at serve time.
- **Uniform refusals.** For an Admin or the Owner, anything not served — unknown tool,
  disabled tool, traversal, symlink escape, denied name, missing file —
  answers the same `404 Not found`. A Restricted caller sees only tools marked
  `safe: true` (a Member sees every enabled page, without host paths); every other
  tool id, existing or not, answers the same `403`, so a Restricted device cannot
  enumerate the manifest.
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
  Only an Admin or the Owner (`manage_plugins`) can trigger it, or anyone on an
  install with no owner yet (set one). Never a Restricted profile, never
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
because nothing is cached. Enable it per device in **Settings → Privacy**.

Note what this does *not* do. It stops the device retaining data. It does not
hide anything from the server, which still stores the full history — that is
what the delete controls are for.

## Reasonable expectations

DisPatch is a small application maintained by volunteers. It has had several
focused security reviews, it fails closed by default, and its access control is
covered by tests. It has not had a professional audit or a formal threat-model
review.

Run it for your household. Don't run it for a newsroom's source communications.

## Upgrading to 3.0

The exact steps, the backup location and the rollback are in the
[3.0.0 changelog entry](../CHANGELOG.md#300--2026-10-18). In short:

```
python -m app.migrate --dry-run --data-dir <your data dir>   # what would change; writes nothing
python -m app.migrate --data-dir <your data dir>             # or just start the server
python -m app.migrate --restore <data dir>/backups/3.0.0-<stamp>   # undo (server stopped)
```

An existing PIN becomes the Owner's PIN and keeps working; set the Owner's password the
first time the app asks. Characters gain an `audience` computed from their old flags.
An install that never had a PIN stays unclaimed until you create the owner account, and
until then only the machine it runs on can use it.
