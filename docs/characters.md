# Characters

A **character** is who you talk to: a name, an emoji or picture, a personality, an answering engine (a
[connection](connections.md) and model, or an agent), and a voice. It is what 2.x called a *bot*; the app shows
both words. Nothing was moved: `config.yaml` is still the roster and every existing bot, personality preset, avatar,
reaction pool and avatar pool is simply a character. The API and the words changed; `/api/bots*` keeps working
as thin aliases of `/api/characters*` for one more release, with the same permissions.

## Fields

| Field | Meaning |
|---|---|
| `id`, `name`, `emoji`, `avatar`, `color`, `description` | identity; the description is one line for the picker |
| `scope` | `household` (managed by Admins, available to profiles per their `household_bots` list) or `personal` (made by one profile, private to it: `owner_profile`). `cloned_from` names the household bot a personal copy came from |
| `hidden` | an Admin's "keep it off the rail" switch. Display only: the bot still opens by link, and a profile whose explicit household list names it still sees it |
| `backend` | `{kind: direct \| agent, connection, model, agent}`. A direct character answers through a connection; an agent character through the agent runtime |
| `personality` | `{preset, custom, text}`; pick a preset (`default`, `warm-friend`, `tutor`, ...) or write your own |
| `voice`, `voice_rp` | owned by the voice add-on, which validates them; see below |
| `pack` | the starter pack it came from |
| flags | `reactions`, `reaction_autopilot`, `avatar_pool`, `image_jobs`, `quick_actions`, `tools` (built-in lookups), `hidden` |

There is no audience, Restricted or Safe Mode setting any more. The 3.x keys `audience`, `safe`, `hide_unlocked`,
`visible` and a per-character `profiles` list are **inert legacy data**: they stay in an old `config.yaml` and are
round-tripped, nothing reads them, and the API refuses them (`422`). Migration 010 turned `hide_unlocked` into `hidden`
and folded a character's `profiles` list into each profile's `household_bots`.

## Who sees what

Every new profile that can own bots (an Admin or a User, apart from the first Admin of a fresh install) gets its own
**Atlas**, a personal copy of the shipped direct-API character. Beyond that:

| | Household characters | Personal characters |
|---|---|---|
| **Admin** | all of them, and manages them | only its own |
| **User** | all of them, or only the ones its `household_bots` list names (an Admin chooses; default all) | only its own |
| **Basic** | only the ones its `household_bots` list names; `"all"` gives a Basic profile nothing, an Admin picks | none (a Basic profile cannot own bots) |

An agent-backed household character (a bot whose turns run an agent on the host) also needs the `dispatch_agents`
permission for anyone but an Admin, and an app-backed one (the Job Board) is Admin-only. A personal character is
private to its owner, **Admins included**. A machine (the API key) sees the household characters and the first
Admin's personal ones.

`GET /api/characters` returns the characters the caller may talk to; one it may not use answers like one that does
not exist. `GET /api/characters/all` (with `manage_characters`) returns every **household** character with its
settings. A profile's own characters are not in that list for anyone else.

**Live updates follow the same rule.** The list, a chat, the first WebSocket frame (`hello`) and every later
broadcast on an open WebSocket are narrowed per connection to the characters that profile may use
(`main.scope_frame_for_principal`, installed as `ConnectionManager.scoper`). A profile never receives a character's
name, avatar or threads unless it may use it.

## Your own characters

A User (and an Admin) can make characters of their own under **Settings → You → My bots** (`/api/me/characters`):
by hand, or by describing it to an AI (`POST /api/me/characters/draft`). The permission is `create_characters`. A User
picks the answering engine only among the household [connections](connections.md) an Admin allows for that profile
(`choose_backend`); the API keys behind them are household property and are never shown. Everything about a personal
character is private to its owner, and **deleting the profile deletes them**.

**Copying.** A User copies a household character into their own profile
(`POST /api/characters/{id}/clone`), and an Admin can copy one into any User or Admin profile
(`POST /api/characters/{id}/clone-to/{profile}`; the Admin cannot see or edit the copy). A copy takes the **setup only**:
name, picture, personality, voice, backend. **Chats are never copied.** Agent characters cannot be copied. A copy keeps
the source's voice only if the new owner may use it.

**Reset to default** (**Settings → You → Account**, or an Admin from People, who never sees the contents) deletes every
custom character and every chat in the profile, then gives an Admin or User a fresh Atlas and a Basic profile its
assigned household characters again. The confirmation names the counts first, and cloned voices are kept. Any profile
can reset itself, **the shared DisPatch profile included**.

## Changing a character

**Settings -> Characters -> Details** on a row, or `PATCH /api/characters/{id}`:

```json
{"connection": "studioforge", "model": "qwen3-8b", "personality": "tutor",
 "hidden": false, "description": "Helps with homework"}
```

`connection: null` detaches the character (its inline provider stays; the key does not come back). Choosing a
connection for an agent character is refused (`character_invalid`).

## Voice

Voices belong to the voice add-on (**Settings -> Voice -> Voices**). A character's `voice` is an opaque id the add-on
validates (for example `builtin:af_sky`); without the add-on installed a save may only keep the value the character
already has. **Details -> Voice -> Voices** opens the Voice tab and sets `location.hash` to `#voice/voices/<character id>`
so the add-on's section can jump to this character.

## Starter packs

`GET /api/starter-packs` lists them; `POST /api/starter-packs/{id}/install` creates the characters
(`{connection?, model?, only?: [ids]}`) as household characters. Installing is idempotent: a character that
already exists is reported as skipped and never overwritten.

| Pack | Characters | For |
|---|---|---|
| `assistants` | four general-purpose assistants: explain, write, code, plan | everyone |

A pack is a YAML file in `backend/app/starter_packs/` whose entries are **Character manifests**
(`docs/contracts/manifests/character.schema.json`); adding one is adding a file.

## Migrating from 2.x

No data moves. Step `006_connections` (see [connections.md](connections.md)) adds `connection:` references; the
new fields (`pack`, `description`, `scope`, `hidden`) are written to `config.yaml` only when set, so a roster that does
not use them keeps the file it had. Every existing character stays a household character in 4.0.
