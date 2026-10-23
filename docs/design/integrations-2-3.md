# DisPatch 2.3.7 — integrations

> **Historical design note (3.x).** It uses the 3.x access words Safe Mode, locked device, Restricted
> and Owner. 4.0 replaced them with Admin, User and Basic profiles and private chats; see
> [../security.md](../security.md) for how access works now. Everything else here is kept as written.

*Design note, 2026-10-14. The brief: "we need to clarify how to connect various
services to this thing. Image generation? Can we just link into some standard API
such as MiniMax? … list repos it works with … a simple way to add these into the
program or enable as desired, or how to add alternatives (such as Grok image gen,
Grok API for chat)." Extended the same day: "a lot of this app won't work without
my stack … all other features and plugins need similar treatment", plus a third
Atlas guide thread on setting up add-ons.*

## 1. The problem

A fresh public install can chat with any API (2.3.0), but every picture path goes
through one private image server over MCP, and nothing in the app says which
companion projects exist, which are public, or what a feature needs before it
works. Several features look present and simply never do anything.

## 2. Decisions

| Area | Decision | Why |
|---|---|---|
| Image providers | `image_jobs` keeps its worker, placeholder and delivery code untouched. A second backend, `image_providers.ApiImages`, implements the same five calls the worker makes on the MCP client (`readiness`, `enqueue`, `poll`, `fetch`, `cancel`, plus `status`). `enqueue` starts an in-process task that makes ONE synchronous HTTP generation; `poll` reports it; `fetch` hands back validated bytes. | Zero behaviour change for the MCP path; the worker cannot tell the backends apart, so the placeholder → picture rewrite, deadlines, coalescing, rate limits and Safe-Mode redaction all apply unchanged. |
| Shapes | Two wire shapes: **`openai-images`** (`POST {base}/images/generations`, reads `data[0].b64_json` or `data[0].url`) covers OpenAI, xAI Grok and compatible hosts; **`minimax`** (`POST {base}/image_generation`, `model: image-01`, `aspect_ratio`, reads `data.image_urls[0]` / `image_base64[0]`, checks `base_resp.status_code`). Presets: OpenAI, Grok, MiniMax, Custom (OpenAI-compatible). | Two shapes cover every mainstream image API today. |
| Config | `<data>/image.yaml` (0600, atomic write): `provider, base_url, model, api_key | api_key_env`. The key is never returned; `has_key` only. A blank key on re-save keeps the stored one. With no key of its own, a provider borrows the key of a chat connection to the **same host** (Atlas on MiniMax → MiniMax pictures with no second paste). | Same rules as Connect an AI. |
| Routing | A job records its backend in the spec at creation. A bot naming a workflow uses the MCP image server when one is configured; otherwise the default provider (`image.yaml`); with no `image.yaml` the default is the MCP server if configured (today's behaviour). Rows written before 2.3.7 have no backend field and read back as MCP. | The live install changes nothing. |
| Pools | Reaction and avatar pools keep their own image CLI. They never call an API provider. | API pictures are metered; a shelf of 30 moods × N images must never refill against a paid API by default. |
| Settings → AI models | New "Image generation" section: provider picker (None / image server / OpenAI / Grok / MiniMax / Custom), base URL, model, key or env name, **Test** (one small generation, shows the picture), status line. Operator only (routes under `/api/llm/`, already decoy-blocked and `_require_operator`). Chat presets gain a local llama.cpp / StudioForge entry and a suggested Grok model. | |
| Works with DisPatch | `backend/app/catalog.yaml` is the single source: one entry per companion with description, public repo URL (public repos only), how it connects, and a `probe` key. `GET /api/plugins/catalog` resolves live status (connected / not configured / installed / disabled). Settings → Plugins renders it as cards, with the existing switches (extensions, lookups) where they exist. | New entry = one YAML edit. |
| Setup guide | Third seeded Atlas thread "Set up add-ons" (`guide/set-up-add-ons.md`), written from the same catalog order. Existing installs never auto-seed; `POST /api/guide/seed?name=set-up-add-ons` (operator) adds one on demand, and the catalog has a "Show setup guide" button. | |
| Fresh-install audit | Matrix in `docs/integrations.md`: feature → requires → alternative → how to enable. The cheap fixes ship here; the rest is listed as follow-up. | |

## 3. Contracts

- `GET /api/llm/image` → `{config: {provider, base_url, model, api_key_env, has_key, key_source}, presets: [...], clawforge: bool, status: {...}}`.
- `PUT /api/llm/image` `{provider, base_url?, model?, api_key?, api_key_env?}` → same shape. `provider: ""` = automatic (the MCP server if configured), `"none"` = off.
- `POST /api/llm/image/test` same body as PUT (unsaved values allowed) → `{ok, error?, mime, bytes, width?, height?, preview}` where `preview` is a `/media/` URL of the test picture.
- `GET /api/plugins/catalog` → `{entries: [{id, name, summary, repo?, connects, status, detail, docs, toggle?}]}`.
- `POST /api/guide/seed?name=<guide>` → `{thread_id}`; 404 unknown guide, 409 when that guide thread already exists (unless `force=true`).

## 4. Cost

API pictures are metered per image (2026-10: OpenAI gpt-image-1 and Grok per image, MiniMax image-01 per image or within a Token Plan quota). DisPatch makes exactly one generation per `[[pic:]]` marker that survives the existing limits (3 per bot per minute, at most 2 per message, coalescing). Nothing else calls the provider: no pool, no retry loop, no warm-up. The Test button is one generation.
