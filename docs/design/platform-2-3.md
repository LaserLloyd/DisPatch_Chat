# DisPatch 2.3 — the platform round

> **Superseded in part by 4.0.** This is the 2.3 design note, kept as written. Where it mentions
> Safe Mode, the locked tier, `tools_locked` or "the PIN" as the line between full and limited
> access, that model is gone: 4.0 has Admin, User and Basic profiles, a shared no-PIN DisPatch
> profile that unsigned devices land on, and private chats. The current rules are in
> [../security.md](../security.md); the 4.0 change list is the [CHANGELOG 4.0.0 entry](../../CHANGELOG.md#400--2026-10-23);
> [multi-user.md](multi-user.md) is the superseded 3.0 record.

*Design note, 2026-10-14. The brief, in the owner's words: "setup DisPatch as a
platform that facilitates these chats, with speech being an add-on package, tools
being add-on packages, and it should have the ability to directly chat with APIs";
an easy-to-install Git app with a default explainer chat and a default API bot that
"just needs a key"; ChatForge's canned prompts on desktop; settings ordered by use
with a plugin manager and room for add-in tabs. This note is the meta-review: what
a new user actually needs, what already exists, and the decisions.*

## 1. The user we are building for

Someone clones the repo, runs it, opens it in a browser and wants, in this order:

1. to see something that explains itself (not an empty chat with two bots that
   cannot answer);
2. to connect an AI — an API key or a local model — in one minute, and get a
   first useful reply, with lookups (weather, news, a page) working out of the box;
3. to learn what else is there (voice, pictures, apps, agents) and switch each on
   when they are ready, without reading the whole manual;
4. to keep it tidy: bots they add or remove, add-ons they install, all in one
   Settings dialog that reads top-down by what they touch most.

Two facts shape the design. The **advisor backend** (`advisor:` block) is already
DisPatch's best direct-API path: provider chain with fallback, live tools, the
InfoForge lookup, speculation — but it is config-only and named for a role
("advisor", "owner", "knowledge_dir") instead of what it is. And the **extension
host** (EXT_API 1) already lets a package add routes, static files, a Settings
pane, tool tiles and bot fields — the voice add-on proves it — but the Settings
pane ids are pre-declared, and nothing lists the installed add-ons.

## 2. Decisions

| Area | Decision | Why |
|---|---|---|
| Direct-API bot | **A direct-API bot is an advisor-backend bot.** The UI calls it "Direct API". The `advisor:` block keeps its name in YAML (compatibility) and gains sensible defaults: no owner, no corpus, no agents, tools on. The old `api:` backend keeps working as "legacy direct API (no tools)". | One backend to document, test and improve; tools and fallback for free. |
| Default roster | `DEFAULT_BOTS` becomes **Atlas** (`atlas`, 🌍, direct API, tools on, `safe: true`, no provider yet). The agent-CLI `quick`/`smart` pair goes. | A fresh install must have a bot that works the moment a key is entered. |
| First run | On an **empty database** DisPatch seeds two threads on Atlas: **"Meet Atlas"** (his introduction: what he is, where to put the key, his tools, how to disable or remove him) and **"How DisPatch works"** (the tour: bots and backends, Connect an AI, profiles and the PIN, reactions and pictures, the voice add-on, apps and tools, agents, remote access — each with "how to turn it on" and a screenshot). | The explainer lives where people already look. Replying in the tour thread asks Atlas, who has the tour in context. |
| Connect an AI | The AI tab **connects a bot** (Atlas by default, any direct-API bot, or a new one) by writing `advisor.providers[0]` from the preset: provider, base URL, model, key (stored 0600 in config.yaml, or an env name). Test before save. A "Lookups" switch (= `advisor.tools`). | "Put in your key and I'll work." |
| Bots tab | Per bot: rename, emoji, model hint; **Add bot** (direct API or agent), **Remove bot** (threads kept); lookups on/off for direct-API bots; the existing flags. | The owner manages bots without editing YAML. |
| Quick actions | ChatForge's twelve canned prompts, ported: `backend/app/quick_actions.py` (defaults + `<data>/quick-actions.yaml` overrides/hidden), chips on the **desktop** empty-thread state, a composer pill that modifies the next send (`action` id travels with the message; the server expands it into the prompt; the stored row keeps the typed text and `metadata.action`). Works for every backend. | The brief; and it is the one ChatForge feature that fits a chat app one-to-one. |
| Settings order | **Bots · Theme · Device · Voice · Plugins · AI models · Reactions · Health · Security.** | Day-to-day first (bots, look, this device, voice), setup after (plugins, AI), rare last. |
| Plugins tab | New. Three groups: **Add-ons** (every discovered extension: name, version, state, install-wide on/off kept in `<data>/extensions.yaml`; the voice add-on's install command when absent), **Apps & tools** (the existing Tools table moves here), **Built-in lookups** (install-wide switch for `app/livetools`, per-bot note). | One place that answers "what is installed and what is on". |
| Add-in tabs | `shell.settings.pane(id, …)` accepts any new id matching `[a-z][a-z0-9-]{1,30}`: the shell creates the tab button and pane on demand, titled by the spec (`title` key or text), placed after Voice. Pre-declared ids keep their place. | "Allow adding tabs for add-ins or plugins." |
| Tools as packages | `ctx.register_live_tools([...])` on the extension context: a package adds tool specs to the live-tools registry (same spec shape as `livetools/registry.py`); the per-bot `tools` list accepts their names (`tools_locked`, which this note once described, was removed in 4.0). Built-in lookups stay in-tree (zero-install), listed as a plugin with a switch. | Add-on tools without forcing every install to pip-install the basics. |
| Speech | Already a package (DisPatch_Voice). The Plugins tab and the README present it as such; nothing moves. | Done in 2.2. |
| Version | **2.3.0**, one commit, deployed and pushed together. | The brief. |

Out of scope (deliberately): a multi-step onboarding wizard (the two seeded threads
and the first-run card do the job), editing quick actions in the UI (YAML, documented),
moving `app/livetools` out of the repo.

## 3. API contracts (the agents build against these)

### Bots

- `GET /api/bots/all` — unchanged shape, plus `backend: "direct" | "legacy-api" | "agent" | "app"`, `tools: bool` (direct-API bots), `connected: bool` (a direct-API bot with at least one provider).
- `POST /api/bots` (operator) — `{id?, name, emoji?, backend: "direct"|"agent", agent?, model_hint?, safe?, visible?, tools?}` → 201 with the `_bot_entry` row. Id derived from the name when absent (`[a-z0-9_-]{1,32}`, must be unused). A `direct` bot starts with `advisor: {providers: [], tools: true}`.
- `PATCH /api/bots/{id}` (operator) — any of `{name, emoji, model_hint, visible, safe, reactions, avatar_pool, tools}`; unknown keys 422; returns the row. Other fields untouched.
- `DELETE /api/bots/{id}` (operator) — removes the bot from `config.yaml`; threads and messages stay (they render with the bot's last name and avatar); 404 unknown; 409 for an app-owned bot. Returns `{removed: id}`.
- `POST /api/llm/connect` (operator, existing) gains `target: "atlas"` (any bot id) and `backend: "direct"` (default) | `"legacy-api"`. With `direct` it writes `advisor.providers = [{provider, base_url, model, api_key|api_key_env, max_tokens?, extra_body?}]` and `advisor.tools` from `tools` (default true) on the target bot, creating the bot when the id is new. The response is the bot row. The old behaviour is `backend: "legacy-api"`.

### Quick actions

- `GET /api/quick-actions` — `{actions: [{id, label, hint, tools: bool}]}` in display order, hidden ones left out. Public to any signed-in profile.
- Send paths (`/ws` `send`, `POST /api/threads/{id}/messages`, retry/resend) accept `action: "<id>"`. The server stores the user row with the typed text and `metadata: {action: {id, label}}`, and runs the turn on `expand(action, text)`. Unknown action → 422 / WS error `bad_action`.
- `<data>/quick-actions.yaml`: `{hidden: [ids], actions: [{id, label, instructions, hint?, tools?: bool}]}` (an entry with a built-in id overrides it; ≤ 50 entries; label ≤ 40, instructions ≤ 4000).

### Plugins

- `GET /api/plugins` (operator) — `{extensions: [{id, name, version, state, enabled, source, detail?}], tools: <the /api/tools rows>, livetools: {enabled: bool, names: [...]}, voice: {installed: bool, version?, install_cmd}}`.
- `PUT /api/plugins/extensions/{id}` (operator) — `{enabled: bool}` → writes `<data>/extensions.yaml` `{disabled: [ids]}`; takes effect at the next restart (the response says `restart_required: true`).
- `PUT /api/plugins/livetools` (operator) — `{enabled: bool}` → `<data>/tools.yaml` key `livetools: {enabled}`; a disabled registry offers no tools to any bot (effective at once).

### Extension host

- `ctx.register_live_tools(specs)` — each spec `{name, description, parameters (JSON schema), fn (async callable(args) -> str | ToolResult), summary?: callable}`; names `[a-z][a-z0-9_]{1,30}`, must not collide with a built-in; registered before startup completes.
- `shell.settings.pane(id, spec)` — any new id creates a tab; `spec.title` (a string, or `{key}` for a locale key) names it.

## 4. Who builds what

Five worktree agents, one integration pass:

- **A — bots & first run** (backend): defaults, seed threads, bot routes, connect-to-direct, tests. `config.py`, `models.py`, `main.py` (bot routes region), `llm_api.py` (connect), new `firstrun.py`, `backend/app/guide/` (markdown + image list).
- **B — quick actions** (backend + frontend): `quick_actions.py`, routes, send-path expansion, chips/pill/menu, locales, tests. `main.py` (send region), `main.js` (composer/welcome region), `index.html` (composer), `app.css`, locales (namespace `quick`).
- **C — settings & plugins** (frontend + small backend): tab order, Plugins tab (`plugins.js`, `plugins_api.py`), dynamic extension panes, Bots tab editing against A's contract. `main.js` (settings/bot-manager region), `index.html` (tablist/panes), `extensions.js`, `ext-sdk.js`, locales (namespaces `plugins`, `bots`).
- **D — tools as packages** (backend): `register_live_tools`, the install-wide switch, tests, `docs/extensions.md`.
- **E — words** (docs): README value proposition and feature map, the two seeded chats' text, `docs/*` updates, CHANGELOG 2.3.0.

Integration (the owner's session): merge, screenshots for the tour and the README,
`sw.js` cache and `?v=` bumps, the version in its five places, the full suites,
scratch-instance drive, deploy, one commit, push.
