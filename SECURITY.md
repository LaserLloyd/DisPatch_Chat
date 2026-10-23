# Security Policy

DisPatch Chat is a self-hosted application that holds private conversations and
files. Security reports are welcome and taken seriously.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private reporting: go to the **Security** tab → **Report a
vulnerability**. That opens a private advisory visible only to the maintainers.

If that option is not visible, private reporting has not been enabled on the
repository yet. In that case open a **normal issue with minimal detail** —
"I believe I have found a security issue in <component>, how should I send the
details?" — and wait for a maintainer to reply with a private channel. Do not
put the reproduction in a public issue.

Please include:

- What the issue is and which component it affects.
- The steps to reproduce it, ideally against a fresh install.
- What an attacker gets out of it — reading another user's messages is a
  different severity from crashing the process.
- Your assessment of severity, and whether it requires an existing account,
  network position, or physical access.

You will get an acknowledgement within **7 days** and an assessment within
**30 days**. If a fix is warranted we will agree a disclosure timeline with
you; the default is public disclosure once a patched release is available.

There is no bug bounty. This is a volunteer project.

## Supported versions

Only the latest released version receives security fixes. There are no
long-term support branches.

## What counts as a vulnerability

**In scope:**

- Authentication or authorization bypass — reaching data without valid
  credentials, or reaching another user's data with valid ones.
- Session handling flaws: fixation, insufficient invalidation, token leakage.
- Injection of any kind: SQL, command, template, header.
- Path traversal or arbitrary file read/write through upload, download, media
  or export endpoints.
- Cross-site scripting, CSRF, or WebSocket cross-origin hijacking.
- Leaks of secrets, tokens or hashes through logs, error responses, or the
  diagnostics dashboard.
- Anything that lets a profile act beyond its role, or read another profile's
  private data.

**Out of scope:**

- Attacks that require the attacker to already be an administrator of the
  install. Admins are trusted by design.
- Denial of service through sheer volume against an install the operator has
  deliberately exposed to the open internet without a proxy or rate limiter.
- Missing hardening headers on a deployment where the operator has replaced or
  removed the shipped reverse-proxy configuration.
- Vulnerabilities in an optional agent backend (for example a locally installed
  CLI agent) rather than in DisPatch itself — report those upstream, though we
  do want to know if DisPatch invokes them unsafely.
- Findings from an automated scanner with no demonstrated impact.

## Security model, briefly

Understanding these assumptions will tell you whether something is a bug:

- **The operator's host is trusted.** Anyone with shell access to the machine
  running DisPatch, or with read access to its data directory, can read
  everything. The database is not encrypted at rest. If you need that, use full
  disk encryption.
- **Messages are not end-to-end encrypted.** The server necessarily sees
  plaintext — it stores conversation history and hands messages to agent
  backends. Transport is protected by TLS; storage is protected by the host.
- **Each profile is its own boundary.** DisPatch 4.0 has profiles with three
  roles (Admin, User, Basic), private chats per profile, and a shared default
  profile, "DisPatch", that a device lands on when nobody is signed in. What is a
  real bug, and worth reporting, is a crossing of one of these boundaries:
  - a profile reading another profile's chats, bots, files or Image Studio
    history (Admins included: an Admin manages profiles but does not read
    their chats);
  - a User or Basic profile reaching a permission it does not hold, or an
    Admin-only route;
  - the shared DisPatch profile running agents or cloning voices;
  - a machine call without the install's API key succeeding where loopback
    trust is off.
  The full table is in `docs/security.md#what-each-role-may-do`. DisPatch is in
  any case not designed to withstand a determined insider who also controls the
  host.
- **Anything exposed to the internet belongs behind TLS.** The shipped
  deployment configurations do this. See `docs/remote-access.md`.

### Inbound media ingestion is a deliberate read primitive

A message posted through `POST /api/inject` (or any inbound path that persists
agent text) may carry a `[[media:/absolute/path|caption]]` directive. DisPatch
resolves that path on the server and, if it is a readable regular file with a
recognised media extension inside one of the allowed media bases (the configured
media directory, `~/.openclaw/media`, and `/tmp/openclaw` when that is a real
directory owned by the service account), copies the bytes into the media store
and rewrites the directive to a `/media/<uuid>` URL that anyone holding the
address can fetch (media is protected by an unguessable name, not a per-owner
check; see `docs/security.md`). This is by
design — it is how on-box agents deliver pictures they have just produced. The
consequence: anyone who can reach that endpoint — a caller holding the configured
`api_token`, or a process on the loopback interface while
`machine_loopback_trust` is on — can publish
any media-extension file readable by the service account under those bases into
the chat. The inbound API key is therefore a scoped file-read capability, not
merely a "post a message" capability. Keep the token secret, keep sensitive
files out of the allowed media bases, and run DisPatch as a dedicated
unprivileged user.

## Hardening checklist for operators

The dashboard (`/api/dashboard`) checks most of these automatically and tells
you what is wrong. See `docs/security.md` for the full guide.

## Keeping private data out of this repository

This project is developed against real, live installs, so the repository itself
has a leak surface: a commit can carry a chat database, an uploaded avatar, a
credential, or the maintainer's own hostnames and paths.

`scripts/scrub_check.py` is the gate, and it is fail-closed — no warn-and-
continue, because a warning in a publish pipeline is a leak with extra steps. It
runs over the working tree in CI, over the git **index** in the `pre-commit`
hook, and over the pushed **commit tree** plus every outgoing **commit message**
in the `pre-push` hook. Install all three with `sh scripts/install-hooks.sh`.

One property is worth stating plainly: personal identifiers live in
`scripts/scrub-rules.local.txt`, which is **git-ignored by design**, because
publishing the list of words that must never be published is itself a leak. CI
therefore runs *without* them and the scanner says so on every such run. For
names, hostnames and handles, **the local hooks are the only gate**.

If you find private data that has already been published here, please report it
through the private channel above rather than opening an issue — an issue would
republish it.
