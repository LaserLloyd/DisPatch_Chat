# Characters

A **character** is who you talk to: a name, an emoji or picture, a personality, an answering engine (a
[connection](connections.md) and model, or an agent), who it is for, and a voice. It is what 2.x called a
*bot*. Nothing was moved: `config.yaml` is still the roster and every existing bot, personality preset, avatar,
reaction pool and avatar pool is simply a character. The API and the words changed; `/api/bots*` keeps working
as thin aliases of `/api/characters*` for one more release, with the same permissions.

## Fields

| Field | Meaning |
|---|---|
| `id`, `name`, `emoji`, `avatar`, `color`, `description` | identity; the description is one line for the picker |
| `audience` | `all` (everyone), `members` (signed-in Members, Admins, Owner; never a Restricted profile) or `restricted` (made for Restricted profiles; kept off the Owner's own rail) |
| `profiles` | optional list of profile ids that may see it **in addition to** the audience rule; empty = everyone the audience allows. Owners and Admins always see every character |
| `backend` | `{kind: direct \| agent, connection, model, agent}`. A direct character answers through a connection; an agent character through the agent runtime |
| `personality` | `{preset, custom, text}`; pick a preset (`default`, `warm-friend`, `tutor`, ...) or write your own |
| `voice`, `voice_rp` | owned by the voice add-on, which validates them; see below |
| `pack` | the starter pack it came from |
| flags | `reactions`, `reaction_autopilot`, `avatar_pool`, `image_jobs`, `quick_actions`, `tools` (built-in lookups), `hide_unlocked` |

The legacy `safe` / `hide_unlocked` pair is still read and written; `audience` is the one to use. When a hand
edit leaves the two disagreeing, the one that grants **less** wins.

## Who sees what

`GET /api/characters` returns the characters the caller may talk to. A Restricted profile (or a signed-out device)
sees `audience: restricted | all` characters; a Member or Restricted profile that is not in a character's
`profiles` list does not see it, cannot start a chat with it (`403`) and cannot send to it over the WebSocket.
The Owner and Admins see all of them. `GET /api/characters/all` (with `manage_characters`) returns every character
with its settings.

**Live updates follow the same rule.** The list, a chat, the first WebSocket frame (`hello`) and every later
broadcast on an open WebSocket (`bots` after an edit, `thread_created` / `thread_update` / `message` frames) are
narrowed per connection to the characters that profile may use (`main.scope_frame_for_principal`, installed as
`ConnectionManager.scoper`). A profile that is not on a character's `profiles` list never receives that character's
name, avatar or threads, in the roster or in a broadcast. Owner and Admin sockets receive everything.

## Changing a character

**Settings -> Bots -> Details** on a row, or `PATCH /api/characters/{id}`:

```json
{"connection": "studioforge", "model": "qwen3-8b", "personality": "tutor",
 "audience": "restricted", "profiles": ["<profile id>"], "description": "Helps with homework"}
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
(`{connection?, model?, only?: [ids], profiles?: [profile ids]}`). Installing is idempotent: a character that
already exists is reported as skipped and never overwritten.

| Pack | Characters | For |
|---|---|---|
| `animal_friends` | Otto the Otter, Hazel the Hedgehog, Professor Hoot, Pip the Penguin, Maple the Fox, Bruno the Bear | **Restricted** profiles (children). Gentle personalities that never ask for personal details and point a worried child to a grown-up, built-in gentle voices, no lookups, no quick actions, no reactions |
| `assistants` | Sage, Scribe, Dev, Planner | Members: explain, write, code, plan |

A pack is a YAML file in `backend/app/starter_packs/` whose entries are **Character manifests**
(`docs/contracts/manifests/character.schema.json`); adding one is adding a file.

## Migrating from 2.x

No data moves. Step `006_connections` (see [connections.md](connections.md)) adds `connection:` references; the
new fields (`profiles`, `pack`, `description`) are written to `config.yaml` only when set, so a roster that does
not use them keeps the file it had.
