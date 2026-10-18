# Changelog

All notable changes to DisPatch Chat are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Voice (the host side of DisPatch Voice 1.6)

- **Models folder renamed** `<data>/speech-models` -> `<data>/models` (`ExtPaths.models_dir`); at
  start the old folder moves across once, never overwriting (`DISPATCH_MODELS_MIGRATE=dry-run|off`).
- **`ctx.secrets`** for extensions: the OS keyring, else a 0600 file in the extension's `state_dir`
  (reuses `agents/secrets.py`). The voice add-on keeps its GPU-rig token there, so it lives under
  `/data`, survives an image update and works with a read-only root.
- **`ctx.settings_writer(namespace)`** and a start-up call of the add-on's `migrate_voice_yaml`
  once the settings store exists (a no-op that logs once until then).
- **`state_dir` and a newly created data folder are owner-only** on Windows too (`app/fsperm.py`).
- **`GET /api/voice/providers`** answers with the installed `dispatch_voice.providers` plugins when
  the add-on is not running (the add-on's own route answers when it is); `manage_plugins`.
- **`PUT /api/bots/order` accepts a partial entry** (`{id, voice_rp}`) without moving that character
  to the top of the list.
- **Docker, `VOICE=1`:** installs `ffmpeg` and `libopus0`, takes the add-on from `VOICE_REF` (default
  the 3.0 refactor branch; `refs/tags/v1.6.0` after release) or a local `--build-context`; the
  entrypoint's CRLF `sed` workaround became a guard now that `.gitattributes` pins `*.sh` to LF.
- **Settings -> Voice, add-on absent:** the models are downloaded from the add-on's page, not a
  terminal command. See [docs/voice.md](docs/voice.md).

## [3.0.0] — 2026-10-18

Profiles, roles and one policy engine. The binary "PIN or Safe Mode" model is replaced by
an Owner, Admins, Members and Restricted profiles; every route and WebSocket action now
declares the permission it needs; and the backend runs on native Windows.

### Old name -> new name

| Before | Now |
|---|---|
| decoy, Safe Mode, locked device, "limited tier", `request.state.decoy` | **Restricted** |
| Unlocked, operator, full session | **Owner** (or **Admin**) |
| the one PIN | the **Owner's PIN** (Members have their own PIN, Admins a password) |
| bot flags `safe` + `hide_unlocked` | `audience`: `all` / `members` / `restricted` (flags kept for one release) |
| per-IP in-memory budgets, `DISPATCH_DECOY_*` | per-profile budgets in `chats.db` (`DISPATCH_DECOY_*` still set the defaults) |

### BREAKING

- **A new install is closed to the network until it has an owner.** With no owner and no
  PIN, only the machine DisPatch runs on may use it; any other machine sees the shell and
  the sign-in state, and can finish setup only with the one-time **setup token** printed
  in the server log (`SETUP TOKEN: ...`). Docker users: `docker compose logs app`. This
  closes the 2.x hole where the first network client to call `POST /api/auth/setup`
  became the owner. An install that already has a PIN is unaffected. To keep the old
  "open until a PIN is set" behaviour set `DISPATCH_ALLOW_OPEN_INSTALL=1`.
- **Authorisation moved from a path deny-list to declared permissions.** `main._decoy_blocked`
  and the eight operator checks are gone from the request path; `auth_gate` enforces the
  requirement each route declares in `backend/app/route_policy.py` and refuses a route
  that declares none. Behaviour for the Owner is unchanged. Responses to a Restricted
  caller keep `detail: "Unlock for full access"` and `"decoy": true`, and gain
  `required_permission` and `error`.
- **Error bodies gain an `error` object** `{code, message, fix, docs}` (additive: `detail`
  is byte-for-byte what it was). Catalogue: `docs/errors.md`. A machine caller gets the
  same object inside its `application/problem+json` document.
- **Cookies are `SameSite=Strict`** (were `Lax`), and a state-changing request that
  carries a session or device cookie is refused unless its `Origin` is ours and
  `Sec-Fetch-Site` is not `cross-site` / `same-site` (`error.code: csrf_origin`). A
  reverse proxy must pass the original `Host` header through.
- **Machine access is a principal with scopes, not network position.** Agents holding
  the `api_token` get the named scopes in `docs/contracts/permissions.json`
  (`machine_scopes` in `security.yaml` narrows them). On-box processes on `127.0.0.1`
  are still trusted without a key while `machine_loopback_trust: true` (the default
  after an upgrade); set it to `false` once your crons send the key.
- **The unlock throttle is per (device, profile) + per address + a global ceiling**, not
  one process-wide counter. After 5 failures that device and profile wait 5 s, doubling
  to about 5 minutes.
- `GET /api/openapi.json` without the key answers `401` (was `403`); `POST
  /api/auth/setup` answers `403 setup_forbidden` from the network; `/ws` refuses
  (`1008`) an unclaimed install from the network.
- `Dockerfile` / compose users: the container binds `0.0.0.0`, so a fresh container
  needs the setup token from `docker compose logs`.

### Added

- **Agent dispatch core.** A new self-contained package
  `backend/app/agents/` defines the `AgentBackend` interface (`start_task`, `send`,
  `events`, `respond_approval`, `interrupt`, `resume`, `cancel`, `list_sessions`,
  `capabilities`), the normalized event stream (status, text, tool calls and results,
  approval requests, subagent start/finish with parent ids, retries, artifacts, cost
  updates labelled as estimates, result, error with a catalogued code), a scriptable
  fake backend, and three adapters: the existing OpenClaw transports wrapped onto the
  interface (with a protocol-4 probe that fails loudly on a mismatch), a Claude Agent
  backend built on the Python Agent SDK (one supervised worker process per task, empty
  `setting_sources`, strict MCP config, explicit permission modes, per-task
  `CLAUDE_CONFIG_DIR`, `max_budget_usd`/`max_turns`, API-key auth only), and a stub for
  the generic CLI adapter. Also: per-task workspaces (git worktree for repo tasks) that
  reject `..` and symlink escapes, budgets, a secret provider (keyring, else a 0600
  file) with redaction, branding rules and the reader/actor quarantine rule. One suite
  (`backend/tests/agents`, 300+ tests, runs on Windows) holds every adapter to the same
  contract. Docs: `docs/agents/backends.md`, `docs/agents/security.md`.
- Optional extra `agents-claude` (`claude-agent-sdk==0.2.168`, exact pin); it is not in
  the default dependencies and `uv.lock` gains its resolution.
- **Agents and tasks are first-class** (docs/agents.md). An *agent* is a configured worker
  (backend, model, instructions, tool allow-list, MCP servers, workspace and approval policy,
  budget) that an Owner or Admin publishes to profiles; a *task* is one dispatched job with a
  live timeline, approvals, artifacts, an estimated cost and a state. Backends: **OpenClaw**
  (unchanged same-host requirement) and **Claude Agent** (API key, Amazon Bedrock, Google
  Cloud or Microsoft Foundry credentials only; no subscription sign-in).
  - New tables `agents`, `tasks`, `task_events`, `approvals`, `agent_spend`, `agent_settings`
    (additive; migration step `008_agents`, with the pre-migration backup and dry run).
    `python -m app.migrate --dry-run` lists them.
  - Routes (all declared in `route_policy.py`): `/api/agents*` (`manage_agents`),
    `/api/agents/available` and `/api/tasks*` (`dispatch_agents`), `/api/approvals/{id}`
    (`approve_agent_actions`). A Restricted profile gets 403 from every one of them, listing
    included; a Member sees only agents published to them and only their own tasks; an Owner
    or Admin sees all. 14 new error codes (`agent_*`, `task_*`, `approval_*`).
  - WebSocket frames `task.event`, `task.approval`, `task.update`: sent only to sockets whose
    profile may see the task; added to the locked-socket deny list.
  - **Budgets**: per task (dollars, turns, minutes, tokens) and per agent and per person per
    day, hard stop plus a notification at the limit; all figures labelled *estimate*.
  - **Approvals**: destructive tools (a shell, file writes, anything that sends, posts,
    deletes or creates) ask every time and offer only "once". Web Push ping (no content),
    a notice line in the originating chat, and a rail badge on approval-needed / done / failed.
  - Audit rows for every finished task (who, agent, approvals, estimated cost, duration,
    outcome); transcript retention setting (default 30 days) with a purge that keeps the audit
    row; `agents_store.redacted_summary()` for support bundles (no prompts or instructions).
  - UI: **Tasks** page (rail button, filters), **Assign task** from any chat and `@agent`
    mentions, live task cards with subagent tree and inline approval cards, **Settings ->
    Agents** (backends, Claude credentials, agent editor, limits, MCP secrets). Eight locales.
  - **`agents.unified_dispatch`** (default **off**): runs OpenClaw chat turns and advisor
    `[[handoff:...]]` cards through the task model. Off, nothing changes. See
    docs/agents.md for what it does and does not carry over.
  - Privacy: `docs/privacy.md` says what each backend sends where.

- **Accounts** (`backend/app/accounts.py`, tables `profiles`, `devices`, `sessions`,
  `usage_daily`, `activity`, `schema_migrations` in `chats.db`): Owner (exactly one),
  Admin, Member, Restricted. argon2id for PINs and passwords (`cryptography`). Per-profile
  PIN quick-switch; Admin/Owner need their credential; per-role and per-profile session
  timeouts. Sessions end when a profile is deleted, disabled, re-roled or re-credentialed.
- **People & Profiles** API (`/api/people*`, `/api/auth/login|switch|me|password|api-token`)
  and Settings tab: create/edit profiles, budgets (messages, conversations, ratings,
  upload MB, **minutes per day**, **quiet hours**), grants/denies, sign-out, bind **this
  device** to a Member or Restricted profile, activity summary, admin audit log.
- **Policy engine** (`backend/app/policy.py`, `contracts/permissions.py`): the role ->
  permission matrix, `require_permission(...)`, `request.state.policy`, and
  `dispatch_agents` as its own permission (a Member cannot run a host agent until granted).
- **Route-table gate** (`tests/test_route_table.py`) and **access-control suite**
  (`tests/test_g5_access.py`): every route and WebSocket action is called with no session
  and with a Restricted session and must be refused; a Restricted turn reaches only
  `audience in (all, restricted)` characters.
- **Contracts** (`backend/app/contracts/`, `docs/contracts/`, `docs/errors.md`,
  `scripts/gen_contracts.py`): OpenAPI, permissions, routes, error catalogue (208 codes),
  capability registry and JSON Schemas for Connection, Tool plugin, Voice provider and
  Character manifests, regenerated and checked in CI (`tests/test_contracts_drift.py`).
- **Migration with a safety net**: `python -m app.migrate [--dry-run] [--restore DIR]`;
  the server runs it at start. See "Upgrading" below.
- `anonymous_access: none` in `security.yaml`: a device that has not signed in sees only
  the sign-in page.
- Security headers on every response (`nosniff`, `Referrer-Policy`, `X-Frame-Options`
  where no CSP frame-ancestors, `no-store` on auth and people APIs, HSTS over TLS).
- **Native Windows**: the backend imports and the test suite collects on Windows
  (`app/_filelock.py` replaces `fcntl`); `.gitattributes` keeps LF everywhere; the
  frontend suite (820 tests) passes on Windows; `npm run test:ci` works under cmd.exe and
  PowerShell; `frontend/package-lock.json` is committed.
- `.claude/agents/`: contract-guardian, security-reviewer, migration-verifier,
  docs-drift-checker, a11y-reviewer.
- **One settings schema and store** (`backend/app/settings_schema.py`, `settings_store.py`). Every
  user-facing setting - key, type, default, range, group, Basic/Advanced, restart-required, environment
  alias, secret - is described once (`schema: 1`; reference: `docs/settings.md`, generated and drift-tested).
  Values live in the `settings` table of `chats.db`; an environment variable still wins and is reported as
  `source: env`; `security.yaml` and `tools.yaml` settings are written through to their files. Import of
  today's values is idempotent and reports every key it does not recognise instead of dropping it.
  `GET /api/settings/schema`, `GET`/`PATCH /api/settings` (`manage_settings`; per-key validation,
  reset-to-default), public `GET /api/settings/defaults`. Add-ons write through
  `settings_store.writer("<namespace>")`. **Settings -> All settings** is drawn from the schema (ForgeUI
  `.ui-setting` rows, search, Basic/Advanced, "set by environment" badge); the older tabs stay for what is not in
  the schema yet.
- **Connections** (`docs/connections.md`): one record per model server (thirteen types), keys in the secret store
  (keyring or a `0600` file) and never in `config.yaml`, an actionable **Test** (reachable / key accepted / models /
  latency), StudioForge recognised by `GET /api/version` and sent `Authorization: Bearer`, **Find servers** for
  StudioForge / LM Studio / Ollama / OpenAI-style servers, and an SSRF guard that refuses link-local and
  cloud-metadata addresses unless an admin allows them. Characters refer to a connection (`connection: <id>`).
- **Characters** (`docs/characters.md`): the 3.0 name and API for bots (`/api/characters`, `manage_characters`;
  `/api/bots*` stay as aliases for one release). Per-character connection, model, personality preset,
  `audience`, `profiles` (who may see it), description, and a link to Voice -> Voices. **Starter packs**: *Animal
  Friends* (six gentle animals for Restricted profiles) and *Assistants*.
- **First-run wizard** (`docs/first-run.md`): owner, first connection (with auto-detect and Test), voice, family,
  network exposure, done - fully driveable through `/api/wizard/*`.
- **Backups** (`docs/backups.md`): scheduled **encrypted** archives (AES-256-GCM, scrypt), restore that checks first
  and applies at the next start, a **support bundle** with redaction (prompts excluded by default),
  `GET /api/users/{id}/export` and `DELETE /api/users/{id}`.
- **Tool manifests**: extensions, apps and `tools.yaml` rows are described as one Tool manifest (back-compat shim),
  with the permissions each requests; a new plugin must be approved before the Plugins switch turns it on; an app's
  `env:` names and any manifest `settings` appear under `tools.<id>.*`.
- **ForgeUI 1.2.0** vendored whole (`forgeui.json`, `forgeui vendor --check` in CI and as a unit test): slate,
  high-contrast and high-contrast-light themes, the component layer (`ui-components`), the app-owned overlay
  `adapters/dispatch-extra.css`. Forest, Paper and Purple changed upstream (recorded in `theme-palettes.test.js`).
- **InfoForge 2.1.0**: the advisor's offline-Wikipedia lookup uses `infoforge search --no-ensure ... --` when the
  installed version is 2.1.0 or newer (read-only, and a query starting with `-` cannot become an option).
- **Dependency audit**: Pillow >= 12.3.0, Starlette >= 1.3.1, AnyIO >= 4.14.2 (the audit's 17 advisories) and a
  `pip-audit` job in CI.

### Changed

- `openclaw.send_to_agent` takes an optional `on_spawn` callback (default `None`, no
  behaviour change) so the agent adapter can stop the CLI process on cancel.

- Settings and docs say **Restricted** / **Owner**; the Bots tab shows each character's
  audience. Strings in all eight languages (`frontend/static/locales/I18N-TODO.md` lists
  the entries still awaiting a translator).
- `GET /api/tools` has three audiences: the Owner/Admin sees everything, a Member every
  enabled page tool (no host paths), Restricted only tools flagged `safe`.
- Provider keys leave `config.yaml` on upgrade (see "Upgrading", steps 7-9). `GET /api/llm/providers`, `/api/llm/test`,
  `/api/llm/connect` and the Connect-an-AI panel work as before and still write inline settings; the next start (or
  Settings -> Connections -> **Move keys here**) turns them into connections.
- `/api/bots*` are thin aliases of `/api/characters*` (same permissions); the bot row gains `connection`,
  `profiles`, `description`, `pack`.
- A character a profile is not listed for cannot be opened or messaged by that profile; Owner and Admin always see all.
  The same rule now narrows every WebSocket broadcast per connection (`bots` after an edit, `thread_*` and `message`
  frames; `ConnectionManager.scoper` / `main.scope_frame_for_principal`), not only the roster and `hello`; `get_messages`
  over the socket checks it for every profile. Before, a profile left off a character's `profiles` list saw the
  character's new name and avatar, and its threads being created, in the broadcasts that followed an edit.
- `deploy/compose/stack.yml` passes `SF_OWNER_USER` / `SF_OWNER_PASSWORD` to StudioForge (its 2.x images refuse to
  listen beyond loopback without an owner account) and seeds Atlas's provider as `llamacpp`, so the migrated connection
  is of type `studioforge` rather than a generic OpenAI-compatible server. `docs/deploy/stack.md` says how to give that
  connection a StudioForge API key.
- Settings has two new tabs, **Connections** and **All settings**; the Bots tab gains a **Details** button per row and a
  **Starter packs** card; `index.html`'s theme picker lists the three new themes.
- `GET /api/plugins/manifests` and the approval rule on `PUT /api/plugins/extensions/{id}` (additive: a body of
  just `{"enabled": ...}` still works for anything already approved).
- `frontend/static/js/w5-api.js` holds the REST calls of the new pages: `api.js` is imported at one `?v=` by a dozen
  modules, so extending it would force a version cascade through files other work is editing.

### Upgrading from 2.x (exact behaviour)

Start 3.0.0 as usual; the server migrates before it serves. Or look first:
`python -m app.migrate --dry-run --data-dir <dir>` prints each step and writes nothing.

1. **Backup** (only when there is data to protect and a step is pending):
   `<data>/backups/3.0.0-<UTC stamp>/` receives a consistent copy of `chats.db`,
   `config.yaml`, `security.yaml` and `trusted-devices.yaml`. The newest five are kept.
   Undo with `python -m app.migrate --restore <that directory>` (server stopped).
2. `001_accounts` creates the new tables (additive; a 2.x binary still runs on the result).
3. `002_owner_from_pin` — **an install with a PIN gets an Owner profile built from that
   PIN.** The PIN is unchanged and unlocks as before. The profile has **no password yet**;
   the first time you do something that needs one (create an Admin) the app asks you to
   set it. Nothing is locked out; remembered devices stay remembered (as the Owner's).
4. `003_audience` writes `audience` on every character in `config.yaml`, computed from the
   legacy flags: `safe` + `hide_unlocked` -> `restricted`; `safe` -> `all`; otherwise
   `members`. The flags are kept and still read; when the two disagree the more
   restrictive reading wins.
5. `004_security_keys` writes `machine_loopback_trust: true`, `machine_scopes: null`,
   `anonymous_access: restricted` and the per-role session timeouts into `security.yaml`
   at the values that reproduce 2.x behaviour.
6. An install with **no PIN** is not migrated into an owner: it stays unclaimed (see
   BREAKING, first item) until you create one.
7. `005_settings` creates the `settings`, `settings_unknown` and `settings_meta` tables and imports the values it
   finds in `security.yaml` (lock timeout, remembered-device days, machine trust, anonymous access, session timeouts) and
   `tools.yaml` (the lookups switch). **A key it does not recognise is kept** in `settings_unknown` and listed by
   `GET /api/settings` (secret-looking names are masked there); nothing is deleted from your files.
8. `006_connections` gives every character's inline provider block (`api:` and each `advisor.providers` entry) a
   connection: one per distinct provider + address + key. **The API key moves into the secret store** (read back
   before it is removed from `config.yaml`); `provider`, `base_url`, `model` and everything else stay inline beside
   `connection: <id>`. A key named by `api_key_env` stays a reference to that variable. The pre-migration backup holds the
   original file, keys included. Downgrading to 2.x after this step: restore that backup (the keys in the secret store
   are not read by 2.x).
9. `007_plugin_approvals` approves, with `source: upgrade`, every add-on and app that is running now and asks for
   permissions; a plugin added later needs approval before the Plugins switch turns it on.
10. `008_agents` creates the `agents`, `tasks`, `task_events`, `approvals`, `agent_settings` and `agent_spend` tables (additive,
    nothing existing is altered; a 3.0 binary without agents ignores them).
11. A restore staged from the app (`POST /api/backups/{id}/restore`) is applied at the start of the migration run, before
    anything above, after copying the current files to `backups/pre-restore-<time>/`.

Environment variables are not copied into the store: an environment variable keeps winning and is shown as such.

Running it again does nothing. `DISPATCH_ARGON2_MEMORY_KIB` / `DISPATCH_ARGON2_PASSES`
exist for tests only.

### Known limits

- Conversations are one shared household space; roles gate actions and characters, not
  individual threads (`docs/design/multi-user.md` lists what is deferred).
- Behind a TLS-terminating proxy that rewrites `Host`, the Origin check will refuse
  state-changing requests; make the proxy preserve `Host`.
- Agents: the Claude Agent has been exercised against recorded and scripted streams only
  (no API key was available); the first real run may reveal a field the fakes do not model.
  Claude Managed Agents (hosted sessions) and the generic CLI adapter are not built. The
  scheduler cannot yet dispatch recurring tasks. Approved shell commands are not sandboxed.

## [2.4.2] — 2026-10-17

### Changed

- **Settings → Bots: Show locked / Show unlocked.** A bot's visibility is now two
  independent switches. *Show locked* is the Safe-Mode flag (`safe`, unchanged in meaning
  and still server-enforced); *Show unlocked* is the inverse of 2.4.0's `hide_unlocked`.
  With neither on, the bot shows on no device: it stays on the roster, the Bots tab lists
  it dimmed and tagged *Hidden*, and its chats still open by link on an unlocked device
  (never on a locked one). The separate "show in sidebar" switch is gone: the old
  `visible: false` loads as "neither" (and wins over `safe: true`, so nothing new reaches a
  locked device); `visible` stays on bot records as a derived, read-only field, and an
  API `visible: false` still means "neither". *Show hidden bots on this device* now also
  brings back the bots shown nowhere, on that device only. Strings in all eight languages.

## [2.4.0] — 2026-10-17

### Added

- **Hide a bot on unlocked devices.** The inverse of the Safe-Mode shield: a bot flagged
  `hide_unlocked` (Settings → Bots, the "Hide unlocked" pill; also `PATCH /api/bots/{id}`,
  `POST /api/bots` and the Bots-tab save) is left off the rail, out of Ctrl-K and the
  companions list on unlocked devices, while a locked device still lists it when it is
  `safe`. Made for bots that belong on the family tablet and not on the owner's own screen.
  It is a display preference, not a gate: the server still lists the bot to every tier
  allowed to see it, the Bots tab still edits it, and its threads still open by link. Any
  unlocked device can bring them back with **Settings → Device → Show hidden bots on this
  device** (stored on that device only; "clear this device" forgets it). Strings in all
  eight languages.

### Changed

- A locked turn on a direct-API bot with no agents and no lookups no longer gets the
  "Safe Mode / research markers" paragraph: there is nothing to take away or explain.
- Dragging a bot on the rail moves it by id, so parked and hidden bots no longer skew where
  it lands.

## [2.3.9] — 2026-10-15

### Added

- **Image Studio** (`apps/imagestudio/`, operator only). A tool in the rail for making
  pictures from the image API provider set up under Settings → AI models → Image
  generation: a prompt box with a 1,500-character counter and recent prompts, provider and
  model, aspect ratio, 1-9 pictures per request, MiniMax's prompt optimizer, an optional
  seed and an optional reference image (an upload, an earlier result or a picture from a
  chat; MiniMax's character `subject_reference`), each hidden for a provider that cannot
  use it. Ctrl+Enter (Cmd+Enter on a Mac) generates, the same as the button and only while
  it is enabled. Results land in the same media store as every other generated picture,
  with the prompt and settings in a ledger and a per-day count; each has Download, Copy
  prompt, Use as reference, Send to chat (an assistant or system message through the
  ordinary inject path) and Set as bot avatar (the existing face + full avatar upload).
  One Generate press is one provider request; nothing runs on its own, a second press
  while one runs is refused, and a calm note says when the provider is metered. The key is
  never sent to the page. Locked devices, cookieless callers and forged cookies get the
  usual 403 on every route. Strings in all eight languages.
- **One lightbox, now with a gallery.** The picture viewer chat images already use steps
  through every picture of the open thread, in message order: the arrow keys, the side
  buttons and a swipe go to the next (newer) or previous (older) picture, stop at the
  ends and show "n / total"; only the picture on screen is loaded and No-Image Mode never
  gets a gallery. Image Studio opens its results in this same viewer through a new
  `lightbox()` call in the app SDK (`dispatch:lightbox`). The ✕, Download, caption,
  counter and arrows now hide unless the pointer is near the bottom of the picture or on
  a control, fade 1.5 s after it leaves, toggle on a touch tap, show on keyboard focus,
  never fade under reduced motion, and never get in the way of Esc.

### Changed

- `image_providers.generate_batch()`: several pictures per provider request, plus seed,
  optimizer and reference for MiniMax. `generate()` is its one-picture case.
- **Personality placeholder `{user}`.** A persona now says `{user}` for the
  person it talks to (it was `{owner}`): the presets, the shipped default, the
  editor hint in all eight languages and the docs. `{owner}` is still filled in
  as an alias, so a saved persona that uses it keeps working; the `owner:`
  config key is unchanged.


## [2.3.8] — 2026-10-14

### Changed

- **Settings on a phone (360px).** Each Bots row is now avatar, name and small
  inline up/down buttons on one line, with the pills, switches and the
  Personality button wrapping underneath. The Plugins tools table becomes one card
  per tool instead of a sideways scroller, and its duplicate explanation above the
  table is gone (the tools.yaml path stays, as a small wrapping monospace line).
  Server paths in the Security note wrap instead of running off the screen. The
  Reactions describe box takes its own full line, and the German placeholder is
  shorter. Desktop layout is unchanged.

### Fixed

- **Settings → Bots → Personality.** An unsaved edit survives the row being
  rebuilt (a rename, the Lookups pill, a save on another row used to replace
  the editor with the stored text). A legacy API bot is no longer offered the
  shipped advisor persona as a preset, and saving that text no longer wipes
  its system prompt; a preset's `{name}` / `{owner}` are filled in for a
  legacy bot's turn as they are for a direct-API bot. Esc and Close hand
  focus back to the Personality button.
- **Bot edits no longer overwrite each other.** `PATCH /api/bots/<id>` reads,
  changes and writes the bot under one lock (`config.update_bot`), so a
  persona save racing a rename or a Lookups toggle — two tabs, or two
  controls on one row — keeps both (before: the second writer carried a stale
  copy of the bot and undid the first, every time).
- A lone surrogate in any JSON string field (`"\ud800"`) is a 422 with the
  validation error, not a 500: the 422 body no longer echoes text the
  response cannot encode.
- **Settings tab strip.** A mouse click on the ‹ › edge buttons no longer
  takes focus off the tablist, so ←/→ keep working afterwards.

## [2.3.7] — 2026-10-14

### Added

- **Image generation through a standard API.** Settings → AI models → Image
  generation picks a provider for `[[pic:…]]` pictures: **MiniMax**
  (`image-01`, `POST /v1/image_generation`), **OpenAI** (`gpt-image-1`) and
  **xAI Grok** (`grok-imagine-image`) through the OpenAI-shape
  `POST /v1/images/generations`, or **Custom** for any compatible host. Test
  makes one picture and shows it; the setting lives in `<data>/image.yaml`
  (0600) and never returns a key. With no key of its own a provider borrows a
  chat connection's key for the same host. `app/image_providers.py` answers
  the same calls as the MCP client, so placeholders, deadlines, coalescing,
  rate limits and Safe-Mode redaction are unchanged. A bot naming an image
  workflow keeps the MCP image server; rows written before 2.3.7 read back as
  MCP jobs. Reaction and avatar pools never use an API provider.
  Routes: `GET/PUT /api/llm/image`, `POST /api/llm/image/test`.
- **One key for everything.** Settings → AI models → *Set up everything with
  one provider* (and a button on the first-run card): MiniMax, OpenAI or xAI
  and one key configure chat on Atlas, pictures and, for OpenAI, knowledge
  embeddings, each with one live test, answered as a ✓ / — / ✗ checklist. The
  key is stored once, on the chat connection. `GET/POST /api/llm/quicksetup`
  (`test: false` makes no calls).
- **Works with DisPatch.** Settings → Plugins opens with a card per companion
  (AI providers, image API, MCP image server, StudioForge, OpenClaw, DisPatch
  Voice, SpeechForge, InfoForge, MailForge, lookups, CrucibleForge, ThemeForge):
  what it adds, how it connects, a public repository link where there is one,
  and its live status. Data in `backend/app/catalog.yaml`;
  `GET /api/plugins/catalog`.
- **A third guide chat, "Set up add-ons"**, in the recommended order, seeded on a
  fresh install; *Show setup guide* (or `POST /api/guide/seed?name=…`) posts it
  on an existing one.
- **Pictures switch per bot** in Settings → Bots (`image_jobs`), and
  direct-API bots with it on are told about `[[pic:…]]` when an image
  generator is configured (unless their persona already explains it).
- **`advisor.lookup` kind `wikipedia`**: the online encyclopedia as an
  InfoForge alternative (full-text search + lead summaries).
- Chat presets: **Local server (llama.cpp / StudioForge)**; Grok suggests
  `grok-4.7`.
- `docs/integrations.md`: every companion, image providers, the one-key setup,
  a feature-by-feature matrix of what needs what, how to add an alternative,
  and cost.

### Fixed

- A message to an unconnected Atlas no longer leaves the thread "thinking" with
  Send disabled after a reload.
- A `[[pic:…]]` on an install with no image generator leaves one quiet
  "needs an image generator" line instead of vanishing.
- Health no longer warns "Agent CLI not found, nothing will reply" (and probes
  a gateway) on an install that never configured an agent.
- Reactions and avatar pools say "needs an image tool" instead of "image host
  unreachable", and Refill is disabled without one.
- Public-repo hygiene: neutral sample names and cities in docs, comments and
  tests; the guide's voice screenshot re-shot; guide pictures are now covered
  by the screenshot allowlist (`guide/<file>` in `docs/screenshots/ALLOWED.txt`).

- **Locked voice (2.3.4) follows the chat list and the daily budget.** An
  extension thread websocket from a locked device is admitted only on a
  `safe` bot that is also **listed** (`visible`) — the same rule as a
  Safe-Mode send on the main `/ws`; a hidden safe bot's thread was reachable
  by voice but not by chat. A locked device's voice turns (spoken or typed on
  the call screen) are now charged to the same daily Safe-Mode budget as a
  typed send (`DECOY_TURN_QUOTA` per client) and refused once it is spent —
  they ran unmetered before. The host also refuses a locked thread's turn
  itself when the bot has stopped being safe or listed, under the add-on's
  own `session_live` check. `ThreadSocket` is unchanged; `Hooks` gains
  `client_ip`.

## [2.3.6] — 2026-10-14

### Added

- **Settings → Bots → Personality.** Each direct-API bot (and each legacy
  API bot) has a Personality panel: a preset picker, a system-prompt editor
  with a character count (up to 8,000), Save, Revert and Reset to preset.
  Picking a preset fills the editor; editing it after that makes it Custom.
  Ten presets ship as data in `backend/app/personas/*.md` next to the shipped
  default: Sleek assistant, Cat-girl, Challenger, Contrarian, Warm friend,
  Patient tutor, Blunt critic, Storyteller, Deadpan wit and Hype trainer. A save
  applies from the next reply, no restart. The text is only the first block of
  the prompt: the date, the rules (the locked rules on a Safe-Mode device), the
  lookups and the profile are still added by DisPatch after it. A bot shown on
  family devices says so in the panel. Agent bots get a pointer to their
  OpenClaw workspace instead of an editor.
- `GET /api/bots/personas` (operator only) lists the presets;
  `PATCH /api/bots/<id>` takes `persona` (`""` = back to the default). Rows
  from `/api/bots/all` carry `persona`, `persona_custom` and `persona_kind`;
  the public roster never carries a persona.

### Changed

- The AI models Connect field ("System prompt") shows the picked bot's
  current personality and writes the same field as the Bots tab, so the two
  never disagree. Sending the shipped default text back means "the default".

## [2.3.5] — 2026-10-14

### Fixed

- **Settings shows and reaches every tab.** Ten tabs are wider than the
  desktop dialog as well as a phone, and the strip has no scrollbar, so with a
  mouse there was no way to reach Security and nothing said it was there. The
  strip now has ‹ / › edge buttons that appear only while there are tabs that
  way (right-to-left aware), a mouse wheel scrolls it sideways, and the active
  tab is kept clear of the buttons. Add-in tabs are measured too.
- The Security tab no longer pulls focus into the PIN field when it is reached
  with the arrow keys (the next arrow press moved the caret instead of the
  tab) or on a touch screen (the keyboard covered the pane).
- Settings polish: "Lock now" lines up with the PIN fields, and the lead
  sentence of Security and AI models has room above the first field.

## [2.3.4] — 2026-10-14

### Changed

- **Voice chat works on locked devices too**, on the bots a locked device can
  already chat with (the `safe` ones). Every turn it sends, spoken or typed on
  the call screen, runs exactly like a typed Safe-Mode message: no agent
  hand-offs, only the bot's `tools_locked` lookups, the locked rules in the
  prompt. Voice settings (Settings → Voice, the Bots-tab Voice pill, the voice
  tools) stay unlocked-only, as does every `/api/voice` route. Needs DisPatch
  Voice 1.3.3. For add-on authors: a locked device now reads
  `GET /api/extensions` (mount rows plus each add-on's new `locked_status`) and
  `/ext/<id>/`, mounts the frontend with `locked: true`, and a thread websocket
  from it carries `sock.locked`; the host marks every turn for that thread
  locked itself and refuses speculative starts there.
- The Bots-tab row control an add-on registers now shows on `safe` bots too
  (the docs said so since 2.3.1).

### Fixed

- The reaction fire rate limiter keys the actor case- and space-blind.

## [2.3.3] — 2026-10-14

### Fixed

- **The reaction pool's daily run never mints into a full shelf.** Each mood's
  quota (fresh images plus the ones used since the last run) is capped at the
  room left under its target, so a full shelf costs nothing and a used image
  is replaced the next morning. The target (`per_mood`) is the outage buffer:
  how many unused images stay on hand per mood if the image server is away.
  Until now a full 32-mood shelf with a daily add of 10 rendered about 470
  images a day, retiring an unused one for each.

## [2.3.2] — 2026-10-14

A review pass over 2.3.0/2.3.1.

### Fixed

- The AI models panel's **system prompt** now reaches a direct-API bot
  (`advisor.persona`); a blank field never clears a saved one.
- **Lookups on/off keeps a hand-picked list**: switching a bot's lookups off and
  on again restores the list it had (`advisor.tools_picked`).
- **Test connection** re-tests a connected bot without re-typing its key, and
  only credential-shaped environment names are ever read for a probe.
- Safe-Mode turns: the locked prompt no longer tells the model to answer from
  its own knowledge ahead of the lookups it has.
- Agent ids no longer accept a dot; Settings → Plugins shows the restart hint
  exactly when the server says a restart is pending; an add-in asking for a
  Settings tab before the host exists gets a clear error.
- Docs: the locked-tools default and the add-on tools rule read as the code
  behaves; README names the current release.

## [2.3.1] — 2026-10-14

### Changed

- **Extension websockets accept a `safe` bot's thread.** The voice add-on can
  now serve a bot that locked devices may also talk to (Atlas, a Wikipedia
  bot). The gate is unchanged where it matters: Origin == Host and a live
  unlocked session — a Safe-Mode device never reaches an extension socket.
  Pairs with DisPatch Voice 1.3.1.

## [2.3.0] — 2026-10-14

DisPatch becomes a platform: a default bot that only needs a key, chats that
explain the app on first run, and a Settings dialog that reads in the order you
use it. Voice stays an add-on; tools can now be add-ons too.

### Added

- **Atlas, the default bot.** A fresh install starts with Atlas, a direct-API
  assistant with lookups (Safe-Mode safe), instead of the two agent-CLI bots.
  On an empty database DisPatch seeds two chats on him: "Meet Atlas" and "How
  DisPatch works".
- **Connect an AI connects a bot.** Settings → AI models writes the provider,
  model and key into the chosen bot's `advisor.providers` (Atlas by default, any
  direct-API bot, or a new one), with a Lookups switch. The previous behaviour
  stays available as the legacy `api:` backend.
- **Bots tab editing.** Rename a bot, change its emoji and model hint, switch
  lookups, add a direct-API or agent bot, remove a bot (its threads are kept).
- **Quick actions** (desktop): ChatForge's twelve canned prompts as chips on an
  empty thread and a composer pill that applies one to your next message, for
  every backend. Override or hide them in `quick-actions.yaml`.
- **Plugins tab** in Settings: installed add-ons with an install-wide on/off
  switch (`extensions.yaml`), apps and tools (the old Tools tab), and a switch
  for the built-in lookups (`livetools: {enabled}` in `tools.yaml`).
- **Add-in Settings tabs.** An extension may create a Settings tab of its own
  with any new pane id.
- **`register_live_tools`** on the extension context: a package adds lookups to
  the live-tools registry, usable by the per-bot `tools` and `tools_locked`
  lists.
- **Web Push notifications** (Settings → Device → Push notifications): a
  lock-screen ping when a bot replies, a picture is ready, or a machine post
  asks for one (`/api/inject` `notify`). Pings carry a name-only title and the
  thread id, never message text. Unlocked, remembered devices only; a device
  that is locked, forgotten or expires loses its subscription. Needs the
  https address. New dependency: `pywebpush`.
- `api:` bots stream for real, retry a 429/502/503/504 once before the first
  token, and accept `extra_body`, `max_tokens` and `read_timeout_s`; new
  `minimax` preset.
- **Advisor live tools** (`advisor.tools`): OpenAI tool calling over the
  eight read-only lookups ported from ChatForge (`app/livetools`): web and
  news search (DuckDuckGo), a page fetch (SSRF-hardened), weather
  (Open-Meteo), Wikipedia, exchange rates (ECB), the time and a calculator.
  Each call that ran shows as a collapsible notice row before the answer.
  `tools_locked` names what a Safe-Mode device may trigger (default: all of
  them). New dependency: `ddgs`.
- **Advisor lookup** (`advisor.lookup`): the turn runs `infoforge search
  --json --text` itself and hands the articles to the model as reference
  notes, so a Wikipedia bot answers in about two seconds with a citation.
- The advisor's date line now carries the local time and zone.

### Changed

- **Settings order** is now Bots, Theme, Device, Voice, Plugins, AI models,
  Reactions, Health, Security.
- The **Tools tab is folded into Plugins**; the Tools popup is unchanged.
- **Default roster:** Atlas replaces the `quick` and `smart` agent bots. Existing
  installs keep their own `config.yaml` roster.
- Safe-Mode lookups default to the whole set (`tools_locked` narrows them).
- The reaction pool's daily add defaults to 2 images per mood (was 10: on a full
  shelf that was ~320 renders a day to replace a handful, each retiring an unused
  image).
- **An advisor may be a Safe-Mode bot.** `safe: true` sticks (it used to be
  forced off silently, including from the Bot Manager). What a locked device
  may make it do is decided per turn: a send, retry or resend over a Safe-Mode
  socket runs the turn locked — no research or handoff marker is acted on, the
  prompt says so, and only `tools_locked` are offered.

### Fixed

- Re-connecting an API bot no longer drops its other fields.
- The instance-lock file handle is closed at shutdown (ResourceWarning).

## [2.2.0] — 2026-10-11

Voice chat leaves DisPatch and becomes an add-on: **DisPatch Voice**, a
separate MIT package (github.com/LaserLloyd/DisPatch_Voice). DisPatch itself
ships no speech code and no speech dependency any more; it gains a small,
stable **extension host** that the add-on (and any later one) plugs into.
Guide: [docs/extensions.md](docs/extensions.md).

### Upgrading an install that used voice

- Install the add-on into DisPatch's environment and fetch its models once —
  the Settings → Voice tab shows the exact commands, and
  [docs/extensions.md](docs/extensions.md#voice) has every install mode. With
  `uv run` as the service's start line: add
  `--with-editable /path/to/a-release-checkout-of-DisPatch_Voice` to it (a `git
  worktree` at the release tag — never the checkout you develop in), plus
  `--offline` (boot must never need the network), `--ws-max-size 262144` (the
  voice socket's frame cap; the main `/ws` never sends more than a
  65,536-character message) and `--ws-ping-timeout 60`; better, start it
  through `scripts/local-chat-launch`. Warm the overlay online once before the
  restart.
- The `voice` dependency group is still declared, **empty**, so a 2.1 unit
  drop-in that runs `uv run --group voice …` keeps booting (without the group
  uv exits 2 and the restart loop takes the app down). It is removed in 2.3.0.
- `DISPATCH_VOICE_PKG=<src dir>` (the 2.1 way to find dispatch-voice) is
  honoured for 2.2.x only, logged as deprecated.
- `<data>/voice.yaml`, `<data>/voices/`, the speech-model directories and each
  bot's `voice` keep their names and paths: the add-on reads them as they are.
- `tools.yaml` rows for the voice tools are kept untouched while the add-on is
  missing (see *dormant rows* below) and work again the moment it is back.

### Added

- **Extension host** (`backend/app/extensions.py`). Installed add-ons are found
  through the `dispatch.extensions` entry point group (or
  `DISPATCH_EXTENSIONS=off | module:attr,…`), version-checked against
  `EXT_API = 1` before any of their code beyond the metadata is imported, and
  set up with an `ExtensionContext`: thread turns (submit, speculate, abort
  with or without keeping the spoken part, retract, replies after, heard
  notes), the frame stream of one thread (bounded, never inside a broadcast,
  with a snapshot of any reply still streaming), a gated thread websocket,
  static assets, tools rows, bot-field validators and lifecycle hooks. A
  failing add-on is a state, never a failed boot: its routes are rolled back.
- **Every gate stays in DisPatch.** `/ws/<name>/{thread}` runs Origin==Host →
  an unlocked session → the thread and a non-Safe-Mode bot → per-session
  slots (the oldest is replaced, 4001) → a server-wide cap (`busy`, 1013), all
  before `accept`; refusals are 1008 pre-accept. A name no extension provides
  runs the same gate and answers 1013 "No extension provides …" instead of a
  bare 1006 a client would retry (so does a handler that never accepts). A
  replaced socket is closed in the background, so a phone reconnecting from a
  half-open link is never held up by its own stale socket. An add-on's HTTP
  routes (API routes only — no mounts) all carry the
  operator dependency, live under `/api/<id>`, join the Safe-Mode block list,
  never join the machine-inbound surface, and may declare body ceilings that
  are enforced before the body is read.
- `GET /api/extensions` (operator, or an authenticated on-box machine — the
  deploy check): names, versions, states (`ok` / `incompatible` / `failed` /
  `disabled`), the assets hash — never a path; `detail` for operators only,
  with the home directory shown as `~`. `/api/auth/status.features.extensions`
  counts the `ok` ones, so a fresh install costs no extra request.
- `/ext/<id>/<path>`: an add-on's frontend files, operator only (403 for a
  locked device), an allowlist of asset types (no HTML, SVG or XML document
  is ever served: 404), `nosniff`, a deny-all CSP, `immutable` caching when
  `?v=` matches the content hash and `no-cache` + ETag otherwise. The service
  worker never caches `/ext/`.
- **Frontend loader and SDK** (`js/extensions.js`, `js/ext-sdk.js`): an
  add-on's strings are merged under the shell's (`i18n.mergeBundle` — it can
  never override a shell key), its CSS loaded, its module imported and
  handshaken, then mounted with a frozen `shell` object: the chat-header
  action slot, a Settings pane, a control on each Bots-tab row, rail tiles and
  a native tool pane, Back-button entries that stack (Settings opened over an
  add-on's overlay takes its own Back, so Back never hangs up a call that
  Settings covered). Everything an add-on registered is torn down when the
  device locks or the add-on fails.
- **Settings → Voice without the add-on** (`js/voice-absent.js`): what voice
  chat is, and the three steps to add it (install the latest 1.x release —
  the add-on's moving `v1` tag — fetch the models, restart and **Check
  again**, which mounts it without a reload), with copyable commands, the
  Docker and `uv run` variants and the optional Kokoro plugin; or, when it is
  installed but cannot run, why (incompatible, failed with the server's
  reason, disabled by `DISPATCH_EXTENSIONS`). Eight languages.
- Bots: `voice_rp` (how role-play text is read aloud) joins `voice` as a bot
  field DisPatch stores and the add-on validates. Without the add-on a save
  may only keep a value or clear it.
- Docker: `--build-arg VOICE=1` (or `VOICE=1` in `.env`) builds the image with
  the add-on.
- `scripts/local-chat-launch`: the recommended start command for a `uv run`
  service. It runs DisPatch with the add-on overlay offline after proving, in a
  throwaway process, that the overlay resolves and its host package and native
  libraries import, and falls back to DisPatch without it when they do not (a
  native crash of that probe included), so a cold cache or a broken wheel costs
  voice chat, not the app.
- **The chat shows what a voice-chat listener did not hear.** When someone talks
  over a spoken reply, the part they never heard is drawn muted behind a mic-off
  mark titled "Not heard in voice chat" (`metadata.voice_heard.upto`, cut at a
  markdown-safe point); a reply stored only up to the cut says "Cut off in voice
  chat" (`js/voice-heard.js`, eight languages).
- `shell.activity()` (extension SDK, additive): an extension whose use makes no
  touches — a voice chat — re-arms the shell's idle auto-lock. Before, DisPatch
  locked itself in the middle of a hands-free conversation.
- `BotView.owner` / `BotView.has_persona` (additive): how an advisor's persona
  names its user, and whether a bot has a persona of its own — names and a flag,
  never prompt text.

### Security and robustness (final review)

- An extension route that can change something (any method but GET, HEAD,
  OPTIONS) refuses a cross-origin browser request: a present `Origin` must equal
  the `Host` and `Sec-Fetch-Site` must not be cross-site or same-site. A
  raw-body or multipart POST is a CORS "simple" request, so another service on
  the same host name could drive one with the unlocked cookie (a cross-origin
  `POST /api/voice/lab/test` loaded the speech models).
- The thread websocket's server-wide cap holds while a reconnect is evicting
  its predecessor (another session's handshake in that second was admitted:
  three open with a cap of two).
- An extension `setup` that has not returned after 60 s is abandoned (failed,
  routes rolled back, context sealed) instead of keeping the port unbound for
  ever; a start-up that never settles is counted in `<data>/ext/.booting`, and
  after two in a row DisPatch starts without extensions (Settings → Voice says
  why) — the next restart once it has run a minute tries them again.
- `/ext/` assets are cached `private, immutable` (operator-only resources).
- `DISPATCH_EXT_BOOT_MARKER=0` turns the crash-loop marker off; the test suite
  does, so a run with the add-on installed never counts "failed starts" in a
  real data directory.

### Changed

- **Instructions for a spoken turn** reach the model in its own slot: the
  system prompt of advisor and API bots, a short note appended at the END of
  what a gateway agent reads — never prepended to the user's words (which is
  what made bots answer "Hands-free mode, got it").
- **Dormant tools rows.** A `tools.yaml` builtin row whose id no installed
  extension provides is kept verbatim through load and save, never listed and
  logged once. Before, one such row invalidated the whole manifest.
- The user-facing name is **Voice chat** (it was Drive mode).
- Keyboard focus on a language/voice `<select>` shows the app's focus ring (the
  accent border alone was fainter than the resting border on the default
  palette).
- A bot whose avatar file is missing (a roster entry without one gets
  `default.png`; an app bot's face that ships nowhere) is drawn with its letter
  instead of requesting a picture that 404s on every render.
- Service worker cache `local-chat-v149`.

### Removed

- The in-tree voice code: `backend/app/voice/`, `drive.js`,
  `voice-worklet.js`, `voice.css`, `js/drive-settings.js`, `js/voices.js`,
  `js/voice-tools.js`, their tests and 306 locale keys per language — all of it
  moved, unchanged, to DisPatch Voice. The voice section of
  `THIRD_PARTY_NOTICES.md` moved with it. `/api/voices` is gone (the add-on
  serves its voice profiles under `/api/voice/profiles`).

### Voice work after 2.1.0, never released in DisPatch

The notes below describe voice changes made after 2.1.0 that were never part
of a DisPatch release. They ship in DisPatch Voice 1.0.0, together with the
code; they are kept here as history.

#### Changed

- **Voice is optional, with an on/off switch.** Settings → Voice opens with
  *Voice (Drive mode)*, a server-wide switch stored as `enabled` in
  `<data>/voice.yaml` and applied live. It is **off on a fresh install**, even
  when every package and model is present: no microphone button, `/ws/voice`
  refuses with 1013 "Voice is turned off in Settings › Voice", and no speech
  model is loaded at start-up. Turning it off ends open drives and releases
  the loaded models; turning it on prewarms them in the background.
  `DISPATCH_VOICE=1` / `0` still force it on / off (the switch is then locked
  or hidden, and the API answers 409). **Upgrading an install that uses voice:
  add `enabled: true` to `<data>/voice.yaml`** (or flip the switch once);
  `DISPATCH_VOICE=auto` no longer means "on whenever the models are present".
  While voice is off the tab shows only the switch and what turning it on
  needs, with the commands.
- **Settings → Voice holds everyday settings only:** Voice (default voice with
  preview; speaking speed only when built-in voices are installed), Listening,
  Replies, This device. The Advanced group and the Voices manager left the
  tab; *Manage voices* appears only while the Voice cloning tool is on.
- **Voice tools.** The advanced pieces are three opt-in builtin tools, off
  unless `tools.yaml` has their row with `enabled: true` (Settings → Tools can
  switch them), unlocked tier only and never in Safe Mode: **Voice cloning**
  (the Voices manager; the `/api/voices` API now answers 404 while it is off),
  **Built-in voices** (status, install steps and voice list of the optional
  GPL-3.0 `dispatch-voice-kokoro` plugin) and **Voice lab** (engine status,
  turn-merge window and hard end of turn, thinking level, cloud/rig engine
  status, *Show timings on the Drive screen*, and a record-3-seconds quick
  test with latency numbers). A tool whose package is missing opens with
  "not installed" and how to install it. New routes `GET /api/voice/lab` and
  `POST /api/voice/lab/test`; new settings keys `enabled`, `merge_window_s`,
  `hard_silence_s` and `reset`.
- **dispatch-voice is published on its own** at
  github.com/LaserLloyd/DisPatch_Voice (MIT). DisPatch finds it when it is
  simply installed (`uv run --group voice --with "dispatch-voice @
  git+https://github.com/LaserLloyd/DisPatch_Voice" …`); `DISPATCH_VOICE_PKG`
  is now only the override for a local checkout and wins over an installed
  copy. Every setup step in the app and the docs uses that command; the Kokoro
  plugin stays a separately distributed optional GPL-3.0 plugin.
- **Review fixes (voice).** Undo is refused once the answer has started
  (it used to delete the question and orphan a fast API/advisor reply), and
  the Drive screen hides it on the first reply audio. A cloned or stock voice
  no longer renders a second set of acknowledgements on every start-up (up to
  ~25 Chatterbox synths). Without a PIN, voice sockets are counted per browser
  (`?d=` device id), so a third family member no longer ends the first drive.
  A hand edit of `enabled` in voice.yaml applies live within 5 s. Switching
  off returns the memory (`malloc_trim`; measured 1.5 GB → 0.16 GB, steady
  across off/on cycles) and calls plugins' release hooks. Previews and the
  lab test wait for idle and run one at a time; a provider's preview failure
  is a JSON 503. Extension API 3: a built-in voices provider describes itself
  (name, licence, steps), so the Built-in voices tool hard-codes no plugin;
  `add_release_hook`. Cache tags are validated and pruned to 8.
- The stock voice shows as "Stock voice" with a **Stock** badge and cannot be
  renamed or deleted in the app; a server with no default voice yet shows
  "Choose a voice" instead of pretending the first one is chosen. The engine
  line tells cloned voices with a built-in fallback, cloned only, and stock
  only apart.
- **Voice licence split.** Drive mode's core install is now entirely
  permissive, so it never changes DisPatch's MIT licence. The Kokoro built-in
  voices moved out into a separately installed, optional plugin,
  **dispatch-voice-kokoro** (GPL-3.0-or-later), because Kokoro's phonemizer
  and espeak-ng are GPL-3.0 and load into the process. `kokoro-onnx` (with
  phonemizer and espeakng-loader) left the `voice` dependency group, and so
  did the unused `soxr`. `python -m app.voice.download` no longer fetches
  Kokoro, so the core download is ~680 MB. The plugin has its own
  `fetch-models [--int8]` and still finds files in
  `$DISPATCH_SPEECH_MODELS/kokoro`. Enable it with
  `DISPATCH_VOICE_EXTENSIONS=dispatch_voice_kokoro:setup`; with it installed
  everything behaves as in 2.1.
- **Default voice without the plugin:** Chatterbox via dispatch-voice speaks
  everything. When no voice is cloned, it uses dispatch-voice's new **stock
  voice** (`stock-host`, built from a bundled clip of synthetic speech, not a
  real person). A `builtin:` voice saved earlier degrades to the default voice
  instead of failing. With no TTS engine at all, `/api/voice/status` says what
  to install and nothing crashes.
- **Engine-neutral extension API (version 2).** `register_engine` takes
  `role="primary"|"fallback"`, `missing=` and `sample_rate=`; new
  `set_builtin_voices_provider(list, preview=)`; engines opt in to the disk
  cache with `cache_tag()` and receive live settings with `apply_settings()`.
  DisPatch no longer names Kokoro anywhere in code.
- **Setting names.** `DISPATCH_VOICE_BUILTIN_VOICE` / `DISPATCH_VOICE_SPEED`
  and `builtin_voice` / `speed` in `voice.yaml` replace the `KOKORO_` names,
  which still work as aliases. The next save rewrites a 2.1 `voice.yaml`.
  `/api/voice/status` drops `kokoro_model`; `builtin_voices` is empty without
  a provider, and `default_voice` can be `""`.

- **Drive mode v2.** The Drive screen follows the active theme and shows the
  bot's avatar inside a state ring, a hint for what a tap does, End drive and
  Pause buttons, live captions with the current sentence highlighted, a
  thinking timer with cancel, and Undo for a misheard sentence. Errors show on
  the Drive screen and failed turns are spoken.
- **Drive settings in the app.** A quick sheet (gear on the Drive screen) and
  a Voice tab in Settings: voice with preview, speaking
  speed, pause before replying, interrupt by talking, reply length, follow-up
  questions, microphone, sounds, screen-on. Each bot can have its own voice
  (Bots tab). Voices are recorded, uploaded, tested and managed inside
  the app (now the Voice cloning tool, above).
- **Protocol.** `start` takes `barge_in`, `reply_style`, `follow_up` and can be
  re-sent mid-drive; new `ping`/`pong`, `ready`, `undo`, `cancel`, `duck`,
  `turn_error` frames; `GET/PATCH /api/voice/settings`, `GET /api/voice/preview`.

#### Fixed

- Drive mode: echo of the reply no longer cuts it off (talking over a reply
  ducks it until the words are confirmed); a dropped phone no longer locks
  Drive mode out (newest socket replaces the oldest, heartbeat); a server
  without speech models says so instead of reconnecting forever; first words
  are no longer lost; speech models load at startup; built-in voice uses the
  fp32 Kokoro model (about 6x faster on CPU than int8; this raised that
  download from the ~810 MB stated for 2.1.0 to ~1 GB, until the licence split
  above moved Kokoro into the plugin).

#### Added

- `THIRD_PARTY_NOTICES.md`: attribution for the Drive-mode models (Parakeet
  CC-BY-4.0 incl. the istupakov ONNX conversion, Silero VAD, Smart Turn,
  Chatterbox) and the runtime libraries of the `voice` group.

## [2.1.0] — 2026-10-09

### Added

- **Advisor bots.** A bot with an `advisor:` block in `config.yaml` answers
  directly from a model API with real token streaming, through an ordered
  provider chain (cloud first, local second; a provider that fails before its
  first word falls through, and a reasoning-only reply does too). Per-provider
  `max_tokens`, `extra_body` (for example to disable thinking) and
  `read_timeout_s`; `<think>` blocks are hidden from the stream.
- **Knowledge corpus.** A directory of Markdown files is the truth; a SQLite
  FTS5 index with optional embeddings from any OpenAI-compatible `/embeddings`
  endpoint gives hybrid ranking. `_profile.md` is always in the prompt, with
  `profile_fallback` files when it is absent.
- **Delegation to agents.** `[[research:<agent>|brief]]` dispatches at once;
  `[[handoff:<agent>|task]]` shows a card with Send and Dismiss and needs a full
  unlocked session, so an agent can never approve its own handoff. Reports are
  saved to `research/` and `actions/` in the corpus and posted into the thread.
  Requests are tracked in `advisor.db` (proposed, running, done, failed,
  dismissed); requests running at a restart are marked failed.
- **Nightly digest.** `POST /api/advisor/digest?day=YYYY-MM-DD[&force=true]`
  writes `conversations/<date>-<bot>.md`; `GET /api/advisor/status` reports
  providers, index and recent requests. Both are machine-inbound. See
  [docs/advisor.md](docs/advisor.md).
- **Drive mode (hands-free voice).** A 🎙 button on a thread opens a voice view:
  speak, and DisPatch detects the end of your turn (Silero VAD + Smart Turn,
  with a turn-merge window for mid-sentence pauses), transcribes it on the CPU
  (Parakeet), sends it through the normal send path tagged `metadata.voice`,
  and speaks the reply sentence by sentence while it streams. Barge-in, echo
  rejection, spoken acknowledgements, Wake Lock and Media Session keys.
  Unlocked tier and non-safe bots only. New endpoints: `/ws/voice/{thread_id}`
  (inline Origin + session gate), `GET /api/voice/status`, and — when the
  optional [`dispatch-voice`](docs/voice-drive-mode.md#cloned-voices-dispatch-voice)
  package is present (`DISPATCH_VOICE_PKG`) — the Voices panel at
  `/api/voices` for cloning a voice from a short clip. Optional `voice`
  dependency group (`uv sync --group voice`, then
  `uv run python -m app.voice.download` for ~810 MB of pinned, sha256-checked
  models, with the int8 Kokoro build this release fetched). Configured by `DISPATCH_VOICE` (`auto` by default: on only when the
  deps and models are present) and the `DISPATCH_VOICE_*` variables. Licence
  note: the Kokoro fallback voice pulls in a **GPL-3.0** phonemizer chain
  (phonemizer + espeak-ng) at runtime through the `voice` group; nothing GPL is
  vendored in this repository. A bot's `voice:` picks its profile. See
  [docs/voice-drive-mode.md](docs/voice-drive-mode.md).

### Changed

- **Default prompt follows the tier.** A direct-API ("Connect an AI") bot with
  no `system_prompt` of its own is no longer told it is in a family chat
  unless it is a Safe-Mode (`safe: true`) bot; unlocked-only bots get a neutral
  default.
- `llm_api` gained `resolve_api`, resolving a provider entry without a bot.
- `config.yaml` bots gain an optional `advisor:` block, omitted when unset;
  admin output redacts its provider and embeddings keys. They also gain an
  optional `voice:` (Drive-mode profile id), shown in admin output only.
- Drive mode is optional at import time: if `app/voice` cannot be imported the
  app starts without it (logged) and `/ws/voice` answers 1013.

## [2.0.0] — 2026-10-09

The first tagged release. It rolls up everything since the 1.0.0 source tree
of August 2026: a socket-first, drop-proof reply path with live streaming, a
conversation toolkit, tools and app packages, ten shared themes, and the
licence change to MIT. Breaking for operators: the coding-terminal pane and its
`DISPATCH_TERMINAL*` switches are gone, and the light/dark switch is replaced
by palettes (a stored `data-theme` choice is not carried over).

### Added

- **Tools and apps.** A Tools group in the rail (one Tools button with a popup
  menu on desktop, a tile grid and bottom sheet on the phone), full-page tool
  panes and **Settings → Tools**. Tools are manifest-driven (`tools.yaml`): a
  static folder, a URL, or a builtin, each switchable, and opening framed or in
  a new window (`open: frame|window`) for pages that refuse framing. **Trusted
  app packages** (`apps/<id>/`) add their own backend routes, static UI, bot
  and thread hook through `AppContext` and the app SDK; the **Job Board** is
  the first one. See [docs/apps.md](docs/apps.md).
- **Conversation toolkit.** Regenerate keeps the alternates, a message can be
  edited and re-run or dropped from the context, quote/reply, thumbs feedback,
  and a per-thread **model and thinking-level override** with a context meter
  — all on one shared turn seam (`TurnOptions`).
- **Drafts, a persisted outbox and offline reading.** An unsent draft is kept
  per thread, a message sent while offline waits in an outbox that survives a
  reload, and threads already opened stay readable offline.
- **Calmer message rows.** Copy and a ⋯ menu, nothing else on screen; machine
  events carry `metadata.notice` and collapse to one muted line.
- **Today / Older section headers** in the thread list, on desktop and phone.
- **Mood-driven header face and a scene backdrop** for bots that have them.
- **Custom link buttons** on the rail, and thin-stroke SVG rail icons.
- **DeepSeek Harness live sessions** — launch, watch and stop a `dsh` run from
  the harness pane (operator-only).
- **StudioForge control panel** as a builtin tool when the LLM rig is present.
- **Inline `[[pic:prompt|caption]]` marker** — a bot's reply can fire an image
  job that lands in place of a placeholder.
- **Clients site links** (practice integration): editable live/repo/notes links
  per client, stored in the data directory.
- **Live reply streaming.** A reply paints as the gateway produces it: the
  OpenClaw `chat` delta events (per-session `sessions.messages.subscribe`)
  drive `stream_start` / `stream_chunk` / `stream_done` frames, coalesced to
  ~100 ms, and the streaming row swaps to the persisted message on
  `stream_done`. Text appears as soon as the model emits it instead of after
  the run ends. Providers that hand OpenClaw a single block (DeepSeek's
  reasoning models do) still land in one paint — the gateway, not DisPatch,
  decides the granularity.
- **Turn phases.** A status line under the composer names what the run is
  doing (preparing context, loading model, writing, using *tool*, finishing)
  from `turn_status` frames (`turn.phase_*` locale keys, all eight languages).
- **Stop.** A ⏹ button beside Send aborts the in-flight run
  (`{type:"abort"}` over the socket, `POST /api/threads/{id}/abort`);
  unlocked sessions only — Safe Mode gets 403.
- **Optimistic send.** The sent bubble paints immediately and is reconciled
  with the server row when it lands (`provisional_id`).

- **Health → "Live transport" card.** The host dashboard shows the gateway
  socket state, replies delivered live vs backfilled, unrepaired truncation,
  `messageSeq` gaps, turns sent over the socket vs the CLI, image-rig
  reachability and 24 h image-job failures — the `/api/health` counters the
  socket turn transport exposes, so "which path did that reply take" is
  answerable from the app.
- **Command palette reaches every rail surface.** Ctrl/⌘-K offers Settings,
  the Health dashboard and the File Server when unlocked, and "Send a file"
  in Safe Mode.

- **Interactive checklist tables**: a ```` ```checklist ```` fenced block posts a
  markdown table that renders as a checkable widget — sortable columns, a
  checkbox per row (completed rows group at the bottom in check order), and
  state persisted on the message so it survives a reload and syncs across
  devices.
- **Image jobs: the placeholder says when the image server is unreachable.**
  A job whose rig cannot be reached shows "🖼️ Waiting for the image rig
  (ConnectError)…" and reverts to the normal placeholder when the rig answers
  again; `/api/health` gains `image_rig` (`reachable`, `unreachable_for_s`,
  `last_error`).

- **Local viewer.** An unlocked operator taps a path in a message and the
  thing opens inside the app, full-screen: an HTML page or a whole static site
  folder, a PDF, markdown, a log, an image, a video, a directory listing. The
  server reads the bytes, so it works from any device that can reach DisPatch
  — a phone on the sofa opens a file on the host with no filesystem access of
  its own. Three ways in: a bare path in a message (`/var/log/x.log`,
  `~/reports/`), the `[[view:/abs/path|label]]` directive an agent can write,
  and a markdown link whose href is a local path. A gear-rail link button can
  point at a local path (or open an http(s) URL in the viewer instead of a
  tab), and the command palette gains **"Open a local file…"** — type a path,
  and the last eight you opened are offered as rows of their own
  (`dispatch-viewer-recent`, per device and on the privacy wipe list).
  Settings → Device gains a **Local viewer** section that edits the served
  folders: add or remove a root, toggle "show hidden files", each change saved
  straight to the server (`PUT /api/local/config`) like every other preference
  in that pane. Unlocked sessions only — the section is not built in Safe
  Mode.

  It serves **nothing** until you name a folder in Settings → Device, and then
  only what resolves inside it. `~/.ssh`, `~/.config/secrets`, `/etc`, `/proc`,
  the database, the security files, and anything matching `*.env *.pem *.key
  id_* known_hosts …` are refused whatever the roots say; dotfiles below a root
  are refused unless you turn them on. Safe Mode never sees any of it — the
  affordance is not rendered and every route answers 403 — and `/api/health`
  counts refusals as `viewer_denied_24h`. Framed pages run under a sandbox
  with an opaque origin: their own scripts and relative assets work, `fetch()`
  from inside them does not.

### Changed

- **No dropped replies.** Every run the gateway accepts is registered in an
  in-flight table and settled with `agent.wait`, so a reply whose live delta
  stream was cut (socket blip, restart mid-run) still lands as a message —
  recovered on startup for runs that were in flight when the server went
  down. `/api/health` carries `inflight_runs`, `dropped_local`, `tick_closes`
  and `transcript_backstop`.

- **Turn dispatch over the already-open gateway socket.** When the native
  gateway transport (`DISPATCH_GATEWAY_WS=1`) is connected, a user turn is
  sent as a gateway `agent` request on that socket instead of spawning
  `openclaw agent` per message — about 1.1 s of fixed pre-model overhead
  gone from every reply (measured 2259 → 1115 ms). `DISPATCH_TURN_TRANSPORT`
  = `auto` (default: socket when connected, CLI otherwise) / `1` (socket only,
  a turn with no socket fails instead of silently paying the CLI cost) / `0`
  (the old per-turn CLI spawn). Only a run the gateway never accepted is
  retried; an accepted run is never re-sent.

- **Image jobs: faster and cheaper on the wire.** One MCP session is reused
  across calls (a forgotten session is rebuilt once and the call repeated); a
  submit or a rig callback wakes the worker instead of waiting for the next
  5-second tick; the sweep advances up to three jobs at once; an unreachable
  rig trips a 15-second breaker so queued jobs fail fast rather than each
  waiting out a connect timeout in series.

- **Appearance is a palette, not a light/dark pair.** `theme.css` first shipped six
  complete skins selected by `data-palette` on `<html>`: **Glacier** (the
  default), Midnight Gold, Forest, Paper, Daylight and Classic Purple (today's
  purple, frozen to the dark values it shipped). Each block redefines the full
  thematic token set with flat values, so a palette can no longer inherit half
  its colours from the other skin, and each declares the `color-scheme` its
  surfaces need. The rail button and the Cmd/Ctrl-K "Change theme" action open
  **Settings → Theme**: a grid of live miniature previews (the card carries its
  own `data-palette`, so a preview is painted by the real token block, never by
  a copy of the values) with radio-group arrow-key handling. The pick is per
  device, applied by the no-FOUC `<head>` script before first paint, and stored
  as `dispatch-palette` — on privacy mode's wipe list.
  `frontend/tests/theme-palettes.test.js` pins the palette ids across
  `theme.css`, `js/theme.js` and `index.html`, and fails if any palette omits a
  token or redefines a structural one.
  Since 2026-09-22 the palettes come from a shared theme package vendored as
  the drop-in folder `static/ui-theme/` — **ten themes** (adding Electric
  Yellow, Night Red and two LaserLloyd skins), with a "More themes" row.
- **Licence: MIT** (relicensed from AGPL-3.0 on 2026-08-30).
- **Text responses are gzipped** and the thread-list preview is bounded.
- **A failed startup exits** instead of lingering as a zombie process.

### Removed

- **The light/dark theme switch.** `data-theme`, the `light-dark()` pairs in
  `theme.css`, `js/theme.js`'s toggle and the `theme.dark` / `theme.light`
  locale keys are gone — Light and Dark as *choices* are replaced by the Paper
  and Daylight palettes above. `light-dark()` still appears in `app.css` and
  `dashboard.css` for two narrow cases (minimal-avatar name tints, host
  dashboard severities); those resolve per palette through each block's
  `color-scheme`. The lock face carries `data-palette="purple"` so it stays
  dark on a light palette, and the rail's theme shortcut is hidden in Safe Mode
  with the rest of the admin surface (Settings is where a palette is chosen
  now, and Settings is full-session only — the language picker was already
  behind the same door).

- **The coding-terminal pane.** The server-side PTY (`backend/app/terminal.py`,
  `/api/terminal/*`, `WS /ws/terminal`, the `terminal_state` frame), its
  sidebar row, action bar, find box and model picker, the `terminal.*` locale
  namespace in all eight languages, the `DISPATCH_TERMINAL*` /
  `LOCAL_CHAT_TERMINAL` switches and the vendored terminal-emulator bundle
  (six files, 317KB) are gone. The pane existed to host one operator-side
  CLI that is no longer part of the stack; the DeepSeek Harness pane is the
  coding-agent surface. Nothing loosens: the harness and StudioForge routes
  keep their own full-session gates, and the `/api/terminal` prefix is now
  simply unrouted (404) rather than decoy-blocked (403). `features.terminal`
  no longer appears in `/api/auth/status`.

### Fixed

- **First-run "Connect an AI" hero on a socket-only host.** `features.agent`
  keyed off the `openclaw` CLI alone, so a host whose turns run over the
  gateway socket greeted an unlocked admin with the onboarding banner. It now
  reports true when either the CLI or a connected gateway socket can answer a
  turn (`test_agent_operability.py`).
- **Thread `⋯` menu glyphs.** Pin/Rename/Archive/Delete carry an icon like
  Sync and Transcript already did; the Bots pane's "no prompt bank — refills
  generate nothing" now reads "no prompt bank yet — add prompts to enable
  refills".

- **Phantom `messageSeq` gaps no longer trigger a backfill per reply.** The
  gateway folds tool-result rows into the assistant message, so every
  "gap of 2" the router repaired was phantom (32 of 32 in the logs). Gap
  repair is now deferred and coalesced (`GAP_BACKFILL_DELAY_S`), the range
  excludes the just-delivered message, and the health counters count
  deliveries that actually landed rather than offers. Side effect fixed with
  it: the backfill used to deliver the revealing reply first as a `followup`,
  which is what was suppressing one bot's reaction markers.
- **`backup_age_s` in `/api/health` could go negative** — the backup stamp was
  naive local time compared against UTC; it is now `datetime.now(UTC)`.
- **Thread list: a picture-only newest message previews as its caption** (or
  a 🖼️ glyph) instead of "No messages yet", and the row repaints when an
  image job's placeholder is rewritten — it used to sit on "Generating an
  image…" after the picture had landed.
- **Image jobs: a callback and the sweep can no longer advance one job
  twice** (a duplicate enqueue was possible when both arrived together).
- **Image jobs: a file name returned by the image server is checked** for
  `..`, empty segments and embedded schemes before it is fetched.
- **Reaction and avatar ids are anchored with `\A…\Z`**: a trailing newline
  in a `bot_id` or mood name passed `^…$` and became a path component.
- The `image_jobs` loop reported a 20-second period to `/api/health`'s
  loop beats while ticking every 5.
- **The app's own pages now send `Content-Security-Policy: frame-ancestors
  'self'` as a header.** `index.html` said that was the server's job and the
  server never did it, so nothing stopped another site from framing DisPatch.

## [1.0.0] — 2026-08-26

The source tree distributed as a zip in August 2026 (never tagged). The history below is the work that produced it, grouped by
theme rather than by commit.

### Added

- **Chat**: threads with pinning, archiving, rename and full-text search;
  markdown with syntax-highlighted code blocks, copy buttons and collapsible
  JSON; image, video and file attachments with inline previews; streaming
  replies with a collapsible "what the agent is doing" panel; an installable,
  offline-capable PWA.
- **Two access tiers**: a PIN unlocks everything, while a limited no-PIN tier
  can read and send to the bots you mark safe, with media stripped server-side,
  per-device daily budgets and a one-way file drop. The split is enforced on the
  server, never in the interface.
- **Remembered devices**: opt-in, sliding-window trust for a device, stored as
  SHA-256 token hashes only.
- **No-Image Mode**: a device setting that omits every picture — chat media,
  avatars, reactions — and, more importantly, downloads none of them. Available
  on the lock screen, so it works before unlocking.
- **Privacy mode**: a device that holds nothing locally — no offline cache, no
  saved settings, no persistent session.
- **Direct LLM providers**: point a bot at OpenAI, Anthropic, DeepSeek, Groq,
  OpenRouter, Mistral, xAI, Together, LM Studio, Ollama or anything
  OpenAI-compatible, from a panel in the app. No agent runtime, no install.
- **Agent backend**: an adapter that spawns an agent CLI and tails its session
  transcripts, plus a native gateway WebSocket transport with session-scoped
  deduplication, truncation repair and gap backfill. Agents reply into threads
  like any other participant and can push proactive messages.
- **Gateway chat mirror**: an agent runtime's own conversations are tailed into
  DisPatch threads, both directions, with gap-filling for replies that arrive
  after the follower has stopped watching.
- **Coding-agent panes**: an admin-only server-side terminal, and a second
  engine next to it for the DeepSeek Harness (`dsh`) with service control, a
  model switch and one-at-a-time headless jobs.
- **Reaction images**: a named reusable pack plus per-bot one-shot mood pools
  where the filesystem is the manifest; agent-only firing, a visible refusal row
  when a fire is rejected, registry↔disk healing, and a server-side autopilot
  that fires a mood when a bot's reply carries no marker.
- **Avatar pools**: burn-on-use face/full pairs, so a new thread draws a face of
  its own and keeps wearing it; a thread's own snapshot is shown in its header
  and on every message inside it.
- **Host dashboard**: health, storage, database integrity, backup status and a
  findings checklist that says what is misconfigured and how to fix it.
- **Backups**: rotating online snapshots taken with `VACUUM INTO` on a private
  connection, integrity-verified, with crash recovery that reconciles
  interrupted conversations rather than losing them.
- **Internationalisation**: eight languages including full right-to-left layout,
  a locale validator that doubles as an XSS boundary, and no build step.
- **Deployment**: multi-arch container images, a compose stack with an optional
  Caddy profile, system and user systemd units, and guides for Docker, bare
  metal, Raspberry Pi and Windows.
- **Repository tooling**: a scrubber that fails the build on private data (as a
  pre-commit, commit-message and pre-push hook, and in CI), a locale checker, and
  a CI matrix covering both supported Python versions.

### Changed

- Renamed the product to DisPatch Chat. Every setting now answers to both the
  `DISPATCH_` and the legacy `LOCAL_CHAT_` prefix, so existing service units
  keep working; only the `DISPATCH_` names are documented.
- The agent-facing API accepts a case-insensitive `bot_id`, takes `text` as an
  alias for `content`, and exposes thread management to machine callers without
  ever taking the machine branch for a browser-shaped request.
- Markdown rendering and text sanitisation were brought to parity with the
  agent runtime's own renderer; markdown tables now scroll sideways instead of
  collapsing their columns.
- The test suite was restructured to be strictly hermetic — every test gets a
  throwaway database and monkeypatched data directories — and runs several times
  faster as a result.
- Ruff configuration that describes the codebase, with a clean run.

### Fixed

- Message identity is scoped by session: an 8-hex-character upstream id collides
  often enough at household volume to silently drop a reply.
- A reply cut short in transit is repaired rather than delivered truncated, and
  an unrepaired truncation is surfaced as a stop-the-line signal.
- A delegated answer can no longer be lost to a missed follow-up window; a turn
  that errors leaves a visible placeholder instead of silence.
- Reaction markers inside code blocks are quoted, not fired; quoted runtime
  delimiters no longer eat the message.
- Two hazard handlers that were dead on arrival — a deadlock, and a field read
  that always returned nothing — were fixed, along with a live render crash and
  a batch of seam bugs found in review.
- The database backup no longer loses a race with the app's own reads.
- The left rail opens the chat rather than the lightbox; every thumbnail opens
  its own full-resolution original, including images an agent sent.
- Tests no longer write into a live install.

### Security

- Fail-closed authentication throughout: WebSocket routes carry inline auth and
  an `Origin`/`Host` check, mutating endpoints answer 403 to a limited-tier
  session, and the file server is a one-way drop with no download path.
- The scrubber gained more pattern classes and now scans commit messages and
  pushes, not just the working tree.

[Unreleased]: https://github.com/LaserLloyd/DisPatch_Chat/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LaserLloyd/DisPatch_Chat/releases/tag/v1.0.0
