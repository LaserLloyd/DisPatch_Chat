# Connections

A **connection** is one model server DisPatch talks to: an AI provider's API, StudioForge, LM Studio,
Ollama, or any server that speaks the OpenAI chat API. In 2.x the provider, address and API key were
written into every character's own settings, so the same key was pasted once per character and "which
server does this character use?" had no answer you could point at. In 3.0 each server is one record and
a character *refers* to it.

Open **Settings -> Connections** (owner and admins), or use the API below.

## What a connection holds

| Field | Meaning |
|---|---|
| `id` | A short stable name (`openai`, `studioforge`, `lmstudio-2`). Made from the type unless you choose one. |
| `type` | `openai_compatible`, `studioforge`, `lmstudio`, `ollama`, `openai`, `anthropic`, `deepseek`, `xai`, `minimax`, `openrouter`, `groq`, `mistral`, `together`. |
| `base_url` | Where it is, e.g. `http://127.0.0.1:1234/v1`. Checked when saved (see *Safety*). |
| credential | The key lives in the **secret store** (the OS keyring when one is usable, otherwise a `0600` file under `<data>/agents/`), or is read from an **environment variable** you name (`env:OPENAI_API_KEY`) every time it is used. A key is accepted when you save and is **never returned** by any route or written to `config.yaml`. |
| `capabilities` | What it can do: `chat`, `vision`, `images`, `embeddings`, `tools`, `agent`. Defaults come from the type; you can change them. |
| `required_permission` | What a profile needs to change it (`manage_connections`; only the Owner may change this). |
| `default_model`, `models` | The model a character uses when it does not choose one, and the list the last **Test** returned (a cache, not a source of truth). |

`GET /api/connections` also returns the type table: what each type needs (a key, a fixed address) and its
default capabilities. The same table is available as manifests: every type validates against the frozen
`connection` manifest (`docs/contracts/manifests/connection.schema.json`).

## Test

**Test** says what is wrong in words, not codes:

- *reachable* - did anything answer at that address (and, if not, the likely reason: server not started, wrong
  port, a container where `127.0.0.1` is the container itself - use `host.docker.internal`);
- *key accepted* - a rejected key is reported as such, not as "down";
- *models* - how many the server lists, or "connected, but no model list" for servers that only implement chat;
- *latency* - milliseconds for the model-list request;
- *product* - what answered, when it can tell (see StudioForge below).

`POST /api/connections/test` tests the **unsaved** form. If you are editing a saved connection and leave the key
blank, the saved key is used - but only for the same type and address, so a Test can never carry one server's
key to another.

## StudioForge

StudioForge is recognised by `GET /api/version` answering `{"product": "StudioForge", ...}`. A StudioForge
connection sends `Authorization: Bearer <key>` from its credential. StudioForge 2.0 requires a key when it is
reached from another machine; on the same computer it does not.

## Finding servers

**Find servers** (`POST /api/connections/discover`) looks for StudioForge, LM Studio, Ollama and OpenAI-style
servers on:

1. this computer (`127.0.0.1`, ports 1234, 8080, 11434),
2. the container host (`host.docker.internal`) when DisPatch runs in a container,
3. any hosts you name, and
4. with **Also search my home network**, the other addresses in this computer's /24.

Each probe is a few short read-only `GET` requests (`/api/version`, `/api/v0/models`, `/api/tags`, `/v1/models`).
Nothing is changed on the servers and nothing is saved until you press **Use this** and then **Add connection**.

## Safety: what addresses are allowed

A connection makes DisPatch send requests to an address a person typed, so the address is checked, and checked
again each time a character uses it:

- only `http://` and `https://`; no user name or password in the address (put the key in the key field);
- the host is resolved and **every** address it resolves to is checked, so a public-looking name that points at an
  internal service is refused too;
- **link-local** addresses (`169.254.0.0/16`, `fe80::/10`) and **cloud metadata** names and addresses
  (`169.254.169.254`, `metadata.google.internal`, ...) are refused with `connection_blocked_target`, and so is
  `0.0.0.0`;
- loopback and ordinary home-network addresses are allowed: a model server on this computer or the next shelf is
  the point.

An admin who really runs a model server on a link-local address can allow it with the setting
`connections.allow_private_targets` (Settings -> All settings -> Connections -> Safety).

## Characters use connections

In **Settings -> Bots -> Details** (or `PATCH /api/characters/{id}` with `connection` and `model`) a character is
pointed at a connection. The turn code resolves the provider, address and key through the connection each time,
so changing a key or address in one place changes it for every character that uses it. A connection that
characters still use cannot be deleted (`connection_in_use`).

## Migrating from 2.x ("Connect an AI")

Nothing breaks. On the first 3.0 start (migration step `006_connections`, after the pre-migration backup) every
character's inline provider block - `api:` or each entry of `advisor.providers` - gets a connection:

- one connection per distinct provider, address and key (two characters using the same OpenAI key share one);
- the **API key moves** into the secret store (read back before it is removed from `config.yaml`); a key named by
  `api_key_env` stays a reference to that variable;
- `provider`, `base_url`, `model` and everything else stay in `config.yaml`, readable, beside a new
  `connection: <id>`; a 2.x reader, or a person with a text editor, still sees what the character uses;
- the old **Connect an AI** panel and `/api/llm/*` routes keep working and keep writing inline settings; the next
  start (or **Settings -> Connections -> Move keys here**, `POST /api/connections/adopt`) turns those into
  connections too.

`python -m app.migrate --dry-run` prints exactly which connections would be created.

If the secret store is unavailable when a connection is used, the character fails with a message that names the
connection; the pre-migration backup in `<data>/backups/3.0.0-<time>/` has the original `config.yaml` with its keys.

## API

All `manage_connections`. Errors carry a catalogued `error.code` (`docs/errors.md`).

| Route | Does |
|---|---|
| `GET /api/connections` | connections (never a key), types, the default connection |
| `POST /api/connections` | `{type, name?, base_url?, api_key? \| api_key_env?, capabilities?, default_model?, id?}` |
| `GET` / `PATCH` / `DELETE /api/connections/{id}` | one connection; `PATCH` takes the same fields plus `clear_key` |
| `POST /api/connections/{id}/test` | Test a saved connection and record the result and model list |
| `POST /api/connections/test` | Test an unsaved form |
| `POST /api/connections/discover` | `{hosts?: [...], lan?: bool}` |
| `POST /api/connections/adopt` | give every inline provider block a connection now |
