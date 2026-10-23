# Integrations: what DisPatch works with, and how to connect it

DisPatch on its own is a chat app with lookups, quick actions, profiles and push
pings. Everything else is a service you connect: an AI provider for answers, an
image provider for pictures, an agent runtime for agent bots, an add-on for voice.
This page lists each one, what it needs, what you can use instead, and the exact
setting. **Settings → Plugins & Apps → Discover more** shows the live status of
every entry, and the "Set up add-ons" chat with Atlas walks through them in order
(on an existing install, press **Show setup guide** there).

Contents: [quickest start](#quickest-start-one-key) ·
[AI providers](#ai-provider) · [image generation](#image-api) ·
[MCP image server](#image-server) · [StudioForge](#studioforge) ·
[OpenClaw](#openclaw) · [DisPatch Voice](#dispatch-voice) ·
[SpeechForge](#speechforge) · [InfoForge](#infoforge) · [MailForge](#mailforge) ·
[built-in lookups](#lookups) · [CrucibleForge](#crucibleforge) ·
[ThemeForge](#themeforge) · [feature matrix](#feature-matrix) ·
[adding an alternative](#adding-an-alternative) · [cost](#cost)

## Quickest start: one key

**Settings → AI & Connections → Set up everything with one provider.** Pick a provider,
paste one key, press **Set up**. DisPatch configures every capability that
provider offers and tests each one once:

| Provider | Chat (on Atlas) | Pictures | Knowledge search (embeddings) | Voice |
|---|---|---|---|---|
| **MiniMax** | `MiniMax-M3`, thinking off | `image-01` | — (not in MiniMax's current API reference) | — (see below) |
| **OpenAI** | `gpt-4o-mini` | `gpt-image-1` | `text-embedding-3-small` | — |
| **xAI** | `grok-4.7` | `grok-imagine-image` | — (no embeddings API) | — |

The key is stored once, on Atlas's chat connection (`advisor.providers[0]` in
`config.yaml`, mode 0600). The image provider is saved without a key of its own
and borrows that one, because it points at the same host. Embeddings are written
to Atlas's `advisor.embeddings` and are used once Atlas has a knowledge folder
(`advisor.knowledge_dir`). The result is a checklist: ✓ configured and tested,
— not offered by that provider, ✗ the test failed (with the provider's reason).

The test makes one model-list request, **one picture** and one tiny embedding.
Send `"test": false` to `POST /api/llm/quicksetup` to configure without calling
the provider at all.

Voice: MiniMax offers a text-to-speech API, but the voice add-on uses its own
speech models and does not call it. Using a provider's speech API for voice chat
is a possible add-on feature, not part of DisPatch today.

<a id="ai-provider"></a>

## AI provider

What you get: answers. Atlas, and any bot you add as "Direct API", streams replies
from a model API and can use the built-in lookups.

**Settings → AI & Connections → Add a connection.** Presets: MiniMax, OpenAI, xAI (Grok),
OpenRouter, DeepSeek, Groq, Mistral, Together, Anthropic, LM Studio, Ollama,
**Local server (llama.cpp / StudioForge)**, and **Custom** for any other
OpenAI-compatible host. Details, key storage and troubleshooting:
[llm-providers.md](llm-providers.md).

| Provider | Base URL | Suggested model | Key from |
|---|---|---|---|
| MiniMax | `https://api.minimax.io/v1` | `MiniMax-M3` | [platform.minimax.io](https://platform.minimax.io/) |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | [platform.openai.com](https://platform.openai.com/api-keys) |
| xAI Grok | `https://api.x.ai/v1` | `grok-4.7` | [console.x.ai](https://console.x.ai/) |
| OpenRouter | `https://openrouter.ai/api/v1` | any listed | [openrouter.ai/keys](https://openrouter.ai/keys) |
| llama.cpp / StudioForge | your server, ending in `/v1` | whatever it serves | no key |

Lookups (tool calling) work with every OpenAI-compatible provider. The Anthropic
preset answers without lookups.

<a id="image-api"></a>

## Image generation (API)

What you get: pictures in chat. A bot with **Pictures** switched on
(Settings → Characters) writes `[[pic:<description>|<caption>]]`; a placeholder appears
at once and the picture replaces it when it is ready. Direct-API bots are told
about the marker automatically once an image generator is configured.

**Settings → AI & Connections → Image generation.** Pick a provider, check the base URL
and model, paste a key or leave it empty to share the chat key for the same host,
press **Test** (one picture, shown inline, not stored), then **Save**. The setting
lives in `<data>/image.yaml` (mode 0600):

```yaml
provider: minimax          # "" automatic | none | clawforge | minimax | openai | xai | custom
base_url: ""               # empty = the preset's
model: ""                  # empty = the preset's
api_key: ""                # or:
api_key_env: MINIMAX_API_KEY
```

| Preset | Request | Model | Notes |
|---|---|---|---|
| MiniMax | `POST https://api.minimax.io/v1/image_generation` `{model, prompt, aspect_ratio, response_format: "base64", n: 1}` | `image-01` | Aspect ratios 1:1, 16:9, 4:3, 3:2, 2:3, 3:4, 9:16, 21:9 (the nearest is used); prompt cap 1500 characters. [Docs](https://platform.minimax.io/docs/api-reference/image-generation-t2i) |
| OpenAI | `POST https://api.openai.com/v1/images/generations` `{model, prompt, n: 1, size}` | `gpt-image-1` | `size` is 1024x1024, 1536x1024 or 1024x1536 from the bot's ratio; the reply is `b64_json`. |
| xAI Grok | `POST https://api.x.ai/v1/images/generations` `{model, prompt, n: 1}` | `grok-imagine-image` | No size is sent (xAI does not document one). Also `grok-imagine-image-quality`, `grok-imagine-image-2.0`. [Docs](https://docs.x.ai/docs/guides/image-generations), [models](https://docs.x.ai/docs/models) |
| Custom | `POST {base}/images/generations` | yours | Any host with the OpenAI images endpoint; a `url` or `b64_json` reply both work. |

Rules worth knowing:

- A bot whose `image_workflow` names a workflow uses the MCP image server when
  one is configured; every other bot uses the provider chosen here.
- `DISPATCH_IMAGE_JOBS=0` switches every picture path off.
- Without any image generator, a `[[pic:…]]` leaves one quiet "No picture: needs
  an image generator" line instead of disappearing.
- Reaction and avatar **pools never use an API provider** (see [cost](#cost)).

<a id="image-server"></a>

## MCP image server

A self-hosted ComfyUI-style server that speaks MCP over streamable HTTP, such as
a private ClawForge server if you have one. DisPatch calls `generate_image`
(with `wait: false`), `get_job`, `cancel_job` and `comfy_status`, and fetches the
file from the server's `/files/`. Set `DISPATCH_CLAWFORGE_URL` to the MCP endpoint
and restart; details in [configuration.md](configuration.md#agent-image-jobs).

Reaction pools and avatar pools refill through an image command-line tool instead
(`DISPATCH_IMAGE_CLI`, an executable taking `--count 1 --priority 3 …`), so they
work with whatever generator that tool drives.

<a id="studioforge"></a>

## StudioForge

[StudioForge](https://github.com/LaserLloyd/StudioForge) (MIT) turns a machine with
NVIDIA GPUs into an AI server: an OpenAI-compatible gateway over llama.cpp that
loads a model just in time when a request names it, unloads it when idle, plans
graphics memory across cards and shares it with other programs through leases.

Connect it like any provider: **Connect an AI → Local server (llama.cpp /
StudioForge)** with its `/v1` address (StudioForge listens on port 1234 by
default). Optionally, `DISPATCH_STUDIOFORGE=1` and `DISPATCH_STUDIOFORGE_URL` add
its control panel as a tab for Admins.

<a id="openclaw"></a>

## OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is the agent runtime behind
**agent bots**: tools, files, long jobs. It also carries the research and hand-off
markers a direct-API bot can use (`advisor.agents`) and the Job Board app.

Put the `openclaw` command on DisPatch's PATH (or set `OPENCLAW_BIN`), or run its
gateway and set `DISPATCH_GATEWAY_WS=1`. Then **Settings → Characters → Add → New character →
Agent**. See [agents.md](agents.md). There is no API substitute: an agent bot
needs an agent runtime.

<a id="dispatch-voice"></a>

## DisPatch Voice

[DisPatch_Voice](https://github.com/LaserLloyd/DisPatch_Voice) (MIT) is the voice
chat add-on: streaming speech recognition, turn-taking, spoken replies, all on the
same machine (about 1.3 GB of models, CPU is enough).

```bash
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
```

Restart, then **Settings → Voice**: switch it on and press **Download** (the models
come down from that page; no terminal). Its install-wide switch is in Settings →
Plugins & Apps → Add-ons. See [extensions.md](extensions.md#voice).

<a id="speechforge"></a>

## SpeechForge

A GPU speech server the voice add-on can hand recognition and voices to. It is not
public yet; the voice add-on works fully without it. When you have one, it is
configured inside the add-on (Settings → Voice).

<a id="infoforge"></a>

## InfoForge

Offline Wikipedia search: before each answer, a direct-API bot with
`advisor.lookup` runs `infoforge search --json --text` and gets the matching
articles as reference notes, so it cites them and nothing leaves your network.
InfoForge is at [github.com/LaserLloyd/InfoForge](https://github.com/LaserLloyd/InfoForge).

**Use InfoForge 2.1.0 or newer.** DisPatch runs `infoforge --version` once and,
from 2.1.0 on, calls `infoforge search --json --text --no-ensure --max-articles N -- "<message>"`:
`--no-ensure` makes the lookup read-only (it never creates or starts the kiwix
container; if kiwix-serve is down it fails fast with exit 2 and the turn answers
without notes) and `--` stops a message that starts with `-` from being read as
an option. An older InfoForge, or one whose version cannot be read, gets the
original command without those two, and may start the kiwix container.
Details: [advisor lookup](advisor.md#lookup-the-bot-reads-reference-material-itself).

**The `[dispatch]` extra.** InfoForge also ships a DisPatch add-on
(`src/infoforge_dispatch`, entry point `dispatch.extensions` named `infoforge`)
that registers one live tool, `wiki_search(query, max_articles)`, which a bot
lists in its `tools:`.
The add-on runs InfoForge in-process, read-only (`ensure=False`, never starts
podman). Install it from the InfoForge clone into DisPatch's own environment
(it is not on PyPI):

```bash
uv pip install --python /path/to/DisPatch_Chat/backend/.venv/bin/python '/path/to/InfoForge[dispatch]'
```

The `dispatch` extra adds no packages beyond the base install; it only records
the intent. You do not need it for `advisor.lookup`, which uses the CLI on PATH.

**Ports (8090 vs 8080).** InfoForge opens no port of its own. The kiwix-serve
container it manages is the only listener: `127.0.0.1:8090` in a fresh config and
in the compose files, and the CLI (not DisPatch) is what talks to it. DisPatch
never connects to kiwix directly. `8080` is the **StudioForge panel's** port on
the Windows rig, which is why fresh InfoForge configs moved to 8090; an
InfoForge config file that omits `kiwix_port` keeps its 2.0 default of 8080 and
will collide with a co-located StudioForge panel, so set `kiwix_port: 8090`
(or `INFOFORGE_KIWIX_PORT`). The two are different services and different
settings.

**Alternative, built in:** the online encyclopedia does the same job.

```yaml
advisor:
  lookup: {kind: wikipedia, max_articles: 3}
```

`kind: wikipedia` runs a Wikipedia full-text search and reads each hit's lead
summary. It needs no install, but it sends the user's words to wikipedia.org,
from every profile; the offline kind keeps them on your machine. Any
direct-API bot with lookups can also call the `wikipedia` tool itself.

<a id="mailforge"></a>

## MailForge

[MailForge](https://github.com/LaserLloyd/MailForge) is a local email assistant
that triages, screens and drafts, and never sends anything without a click.
DisPatch shows it as an **Emails** tab for Admins. Run it on the same
machine; `DISPATCH_MAIL=auto` (the default) finds it by its data directory. No
API substitute: mail needs MailForge.

<a id="lookups"></a>

## Built-in lookups

Web and news search, page fetch, weather, Wikipedia, exchange rates, current time
and a calculator, keyless, for direct-API bots on any OpenAI-compatible provider.
Per bot in Settings → Characters; install-wide in Settings → Plugins & Apps.

<a id="crucibleforge"></a>

## CrucibleForge

[CrucibleForge](https://github.com/LaserLloyd/CrucibleForge) is an LLM benchmark.
Its results page can be listed as a `url` tool in `tools.yaml` (id `benchmark`) so
it opens from the Tools menu. See [apps.md](apps.md).

<a id="themeforge"></a>

## ThemeForge

[ThemeForge](https://github.com/LaserLloyd/ThemeForge) is the shared colour theme
DisPatch ships with. Nothing to set up.

## Feature matrix

What each feature needs, and what a fresh install shows without it.

| Feature | Requires | Alternative | How to enable | Without it |
|---|---|---|---|---|
| Chat with Atlas / direct-API bots | an AI provider key or a local model server | any OpenAI-compatible host | Settings → AI & Connections | A "not connected yet" notice in the thread |
| Lookups | internet access | — | built in, per bot | — |
| Knowledge search | `advisor.knowledge_dir`; embeddings optional | any OpenAI-compatible `/embeddings` (`advisor.embeddings: {base_url, model, api_key_env}`) | `config.yaml`, or one-key setup with OpenAI | Keyword search (FTS5) |
| `[[pic:…]]` pictures | an image API or MCP image server | MiniMax, OpenAI, xAI, any OpenAI images host | Settings → AI & Connections → Image generation; Pictures per bot | One "needs an image generator" line |
| Reaction images (pack) | nothing | — | Settings → Characters → Reactions | — |
| Reaction pool refills | `DISPATCH_IMAGE_CLI` | — (pools never use a paid API) | env var, restart | "Needs an image tool" line; the pack still works |
| Avatar pools | `DISPATCH_IMAGE_CLI` | — | env var, Pool per bot | "Needs an image tool"; Refill disabled |
| Agent bots | OpenClaw | — | `openclaw` on PATH or `DISPATCH_GATEWAY_WS=1` | Health shows "No agent backend"; an agent turn says OpenClaw is not available |
| Research / hand-off markers | OpenClaw agents in `advisor.agents` | — | `config.yaml` | Not offered to the model |
| Job Board app | OpenClaw (`scout` agent) | — | ships in `apps/` | Board loads; finding jobs reports the agent unreachable |
| Voice chat | DisPatch Voice add-on | — | install, restart, Settings → Voice | Settings → Voice shows how to install |
| GPU speech | SpeechForge (not public) | the add-on's on-machine speech | inside the add-on | the add-on uses this machine |
| Offline Wikipedia lookup | InfoForge + kiwix | `lookup: {kind: wikipedia}` | `config.yaml` | no lookup notes |
| Emails tab | MailForge | — | run MailForge | tab hidden |
| StudioForge panel | `DISPATCH_STUDIOFORGE=1` + URL | — | env vars | tab hidden |
| DeepSeek Harness panel | the `dsh` command | — | on PATH | tab hidden |
| Web Push | the https address | — | Settings → Appearance | explains it needs https |
| File Server, Local Viewer, quick actions, profiles | nothing | — | built in | — |

Known gaps (tracked for a later release): the "not available" rows in Settings → Plugins & Apps
for built-in panels do not yet say what each needs; Add bot → Agent does not yet
warn when OpenClaw is missing; the Job Board is listed even with no agent runtime;
quick actions that ask for lookups are offered to bots without them.

## Adding an alternative

- **Another chat or image host.** If it speaks the OpenAI API, choose **Custom**
  in Connect an AI or in Image generation and enter its base URL. Nothing to code.
- **A new image wire shape.** Add a preset and a `shape` branch in
  `backend/app/image_providers.py` (`generate()`); the worker, placeholders and
  delivery are shared.
- **A new lookup.** Write an add-on that calls `ctx.register_live_tools([...])`
  ([extensions.md](extensions.md)); bots pick it up in their `tools` list.
- **A new companion card.** Add an entry to `backend/app/catalog.yaml` (and a
  probe in `main.py` if it has a status). Public repositories only in `repo`.
- **A whole feature.** An extension can add routes, static files, a Settings tab
  and per-bot fields ([extensions.md](extensions.md)), or ship as an app package
  ([apps.md](apps.md)).

## Cost

API pictures are billed per image (2026-10: OpenAI and xAI per image; MiniMax per
image or from a Token Plan quota; xAI lists `grok-imagine-image` at $0.02 and
`grok-imagine-image-quality` at $0.05). DisPatch makes **one** generation per
accepted `[[pic:…]]` request, after the existing limits: at most 2 per message,
3 per bot per minute, and a newer request replaces a still-queued one. The Test
button and the one-key setup make one picture each. Nothing else calls an image
API: reaction and avatar pools refill only through `DISPATCH_IMAGE_CLI`, never an
API provider, so no shelf can refill itself against a paid account.
