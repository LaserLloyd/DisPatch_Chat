# Managed bots ("bot kits"), 4.1

A **kit** makes a household bot a template whose setup and chats are pushed to every personal copy,
the way a company pushes settings and announcements to its users' machines. Code: `backend/app/kits.py`
(engine), `backend/app/kits_api.py` (Admin routes), `frontend/static/js/bot-kits.js` (Settings panel).

## What a kit holds

| Part | Source |
|---|---|
| Managed setup | name, emoji, description, colour, personality text, quick actions, lookups switch, picture (face file hash). Read from the household bot; for Atlas from `firstrun.atlas_template()` + the household Atlas's personality + the shipped picture. Never pools, knowledge folder, image jobs, agents, `api` blocks, keys, voice. Connection and model are never managed. |
| Kit chats | `id`, `title`, Markdown body, `audience` (`everyone` / `admins`), `rev`. |
| `auto_clone` | every Admin and User profile gets a copy. |

Atlas's kit is **shipped** (`kits.SHIPPED`, Python so a deploy can never drop it; its chats are the guide
files in `backend/app/guide/`). A **household** kit is `<data>/bot-kits/<bot_id>.yaml`, written atomically
(0600); for `atlas` it adds chats to the shipped ones and may switch `auto_clone` off. Only direct-backend
household bots can carry one (agent, app and legacy-API bots stay uncopyable).

## Copies

A personal bot is a copy of kit `k` when `cloned_from == k` (Atlas: `pack == "atlas"`). Two persisted
`Bot` fields (written to `config.yaml` only when set, so older rosters load unchanged):

* `kit_version`: hash of the template the copy was last synced to;
* `kit_baseline`: the managed values (and picture hash) at that sync.

**Setup sync:** when the template's version changes, each field whose current value still equals the
baseline takes the new template value; a field the owner changed is kept. Then the new baseline/version is
stored. A copy made before its kit existed is *adopted*: baseline := current template, nothing changed.

Every copy is made by `profiles_service.make_copy` (spawn_atlas, clone_to_own, clone_to_profile,
auto-clone, Reset profile): Atlas from its template with `pack` set, other bots through `_copy_into`'s
allowlist and voice rule, then the template applied with its baseline, "removed" cleared, a sync requested.

## The engine

`kits.sync(db, deliver)` = `apply_setup()` in a thread under `profiles_service.LOCK` (one-off upgrade step,
auto-clone, every copy's setup sync, state GC) + `deliver_chats()` (async). Idempotent, never raises. Runs at
server start (after the first-run seed, before What's new), after a profile is created or promoted, after a
copy is made, after any household roster change (a kitted bot edited, its picture changed), after a kit is
saved, on Push, and on a copy's Reset. `request_sync()` is thread-safe and coalesced.

**Chats** are posted as new threads in the copy owner's chats through `main._deliver_assistant_text`
(`metadata.seeded`, `metadata.kit = {bot, chat, rev}`, source id `kit:<copy>:<chat>:<rev>`). What each copy
received is recorded in `<data>/bot-kits/state.json` (`delivered`), not inferred from messages, so a chat
the owner deleted does not come back by itself. A higher `rev` is delivered once more. Admin-written bodies
are neutralised: `[[` and `:react:` are broken with a zero-width space so no marker (`[[media:/path]]` reads
host files) is acted on.

**Auto-clone** gives every enabled Admin/User profile a copy of each `auto_clone` kit it has not removed,
except the protected first Admin (it edits the household original). A household kit only reaches profiles
that may use the household bot. Deleting a copy records `removed[profile] += kit`; adding it back, Reset
profile, or a role change through Basic clears it. On the first 4.1 start every Admin/User profile without
an Atlas copy gets one (nobody is marked as having removed it).

**Reset a copy** (`POST /api/me/characters/{id}/reset`, owner only): every managed field and the picture
back to the template, new baseline, and kit chats whose recorded thread is gone are posted again (a
`:r<ms>` suffix on the source id).

## Upgrading from 4.0

Once per install (`state.migrated`): the 4.0 Atlas copies count their guides as delivered under the
`guide:<bot>:<file>` source ids (unless the copy was still in 4.0's pending queue, which is then removed);
a profile that already has chats but no Atlas copy (a 3.x profile, or one that deleted it in 4.0) counts as
having removed Atlas. A new or empty profile without a copy gets one. "What's new" (`post_release_notes`)
is unchanged and keyed by version, so 4.0.0's is not posted again.

## Security

Admin routes: `manage_characters`, household bots only (a personal id is 404), the copy count is a number.
Owner route: `create_characters`, own copies only (anyone else's is 404). CSRF/Origin rules unchanged.
Kit chats are written by Admins only and go through the assistant funnel like any message.
