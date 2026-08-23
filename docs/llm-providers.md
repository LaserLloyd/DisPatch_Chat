# Connect an AI — direct LLM providers

The other way to get replies in DisPatch Chat. Where [agents.md](agents.md) describes
an agent *runtime* on the same machine, this page describes pointing a bot
straight at a model provider's HTTP API — LM Studio on the same box, OpenAI,
Anthropic, or anything OpenAI-compatible.

Nothing is installed and no CLI is involved. Two fields and you have an
assistant.

## The two-step version

1. Unlock DisPatch, open **⚙ Settings → 🔌 AI models** (or the **Connect an AI**
   card the empty chat area shows on a fresh install).
2. Pick a provider, paste a key if it needs one, press **Test connection**, pick
   a model from the list it returns, press **Save & chat**.

That writes a new bot into `config.yaml` and opens a conversation with it. It is
a real bot: it appears in the sidebar, keeps history, is searchable, and obeys
the same PIN rules as everything else.

## What you get, and what you do not

A connected provider gives you a **conversational assistant**. It sees the
recent messages in the thread and replies.

It is not an agent. There are no tools, no file access, no shell, no ability to
act on your machine — and no attachments or images sent to the model (v1). If
you want a thing that can *do* something, you want an agent backend
([agents.md](agents.md)); if you want something to talk to, this is simpler,
safer, and works everywhere DisPatch works.

## Providers

| Provider | Base URL | Key | Notes |
|---|---|---|---|
| **LM Studio** | `http://127.0.0.1:1234/v1` | none | Start its local server first |
| **Ollama** | `http://127.0.0.1:11434/v1` | none | Must be running; models are whatever you have pulled |
| **OpenAI** | `https://api.openai.com/v1` | `OPENAI_API_KEY` | |
| **Anthropic (Claude)** | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | Uses the official `anthropic` SDK, not raw HTTP |
| **DeepSeek** | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | |
| **Groq** | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | One key, many models |
| **Mistral** | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | |
| **xAI (Grok)** | `https://api.x.ai/v1` | `XAI_API_KEY` | |
| **Together AI** | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` | |
| **Custom** | you supply it | optional | Anything speaking OpenAI's `/chat/completions` |

The Base URL column is the default the panel pre-fills. It is editable for every
provider — a proxy or a self-hosted gateway in front of a vendor is a normal
thing to have, and the app should not argue with you about it.

### The local ones (LM Studio, Ollama)

No key, no account, nothing leaves the machine. The one thing that catches
people out is that **the server has to already be running**:

- **LM Studio** — open it, go to the *Developer* / local-server tab, start the
  server, and load a model. `Test connection` lists whatever is loaded.
- **Ollama** — `ollama serve` (usually already running), and `ollama pull
  <model>` for anything you want to use. `Test connection` lists what you have
  pulled.

If DisPatch is in a container and the model server is on the host,
`127.0.0.1` inside the container is the container. Use the host's LAN address,
or `host.docker.internal` where your runtime provides it.

### Custom / OpenAI-compatible

Point it at any server implementing `POST {base_url}/chat/completions`. That is
llama.cpp's server, vLLM, LocalAI, text-generation-webui's OpenAI extension, a
corporate gateway, and most smaller vendors.

Two things to know:

- The base URL usually ends in `/v1`. Pointing at the web UI's address instead
  of the API's is the single most common mistake, and `Test connection` says so
  explicitly when it happens.
- `GET {base_url}/models` is optional in that ecosystem. If your server does not
  implement it, type the model name into the field yourself — `Test connection`
  then verifies the chat endpoint directly instead, and tells you if that fails.

## Where the API key is stored

In **`config.yaml` in the data directory** (`~/.local/share/local-chat` by
default, `/data` in the container images), under the bot's `api` block:

```yaml
- id: llm-openai
  name: OpenAI
  emoji: 🟢
  model_hint: gpt-4o-mini
  order: 2
  visible: true
  safe: false
  api:
    provider: openai
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key: sk-…
```

**Every writer chmods that file to `0600`** — owner read/write only — because it
can hold credentials. Be aware of what that does and does not buy you:

- It stops other *users* on the box reading your key.
- It does **not** encrypt anything. Anyone with your account, or root, or a copy
  of your backups, has the key. That is the same trust boundary as the rest of
  DisPatch (see [security.md](security.md)) — your host is the boundary.
- Back the data directory up as a secret. If you sync it somewhere, the key goes
  with it.

The key is never returned by any endpoint. The API reports `has_key: true` and
nothing else, and `/api/bots` — which a Safe-Mode device can read — carries only
the provider *id*, not the URL and not the credential.

### Keeping the key out of the file: `api_key_env`

Put the name of an environment variable in the **"…or read it from an
environment variable"** field instead of pasting a key. DisPatch then reads the
key from the process environment at call time and writes no secret to disk:

```yaml
  api:
    provider: openai
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: OPENAI_API_KEY
```

Set the variable wherever the service gets its environment — `.env` for Docker
Compose, `Environment=` or `EnvironmentFile=` in the systemd unit.

**If both are set, the environment variable wins.** An operator who configured
both has said which one they trust, and it is not the one on disk.

## Access control

The setup panel and all three routes behind it (`/api/llm/providers`,
`/api/llm/test`, `/api/llm/connect`) are **full-session only** — a PIN-locked
device never sees the 🔌 AI models tab and gets a 403 if it constructs the request by
hand. Setting up a provider spends money and writes a credential; it belongs on
the same side of the door as the dashboard and the terminal.

A newly connected bot is created with `safe: false`, so it does **not** appear
on Safe-Mode devices until you flip that yourself in **Settings → Bot Manager**.
Decide deliberately: a safe bot is reachable by anyone holding a family tablet,
and every message it sends costs you tokens.

## Configuration reference

Everything the `api` block accepts:

| Field | Required | Meaning |
|---|---|---|
| `provider` | yes | One of the ids above (`openai`, `anthropic`, `lmstudio`, `custom`, …) |
| `model` | yes | The model id sent to the provider |
| `base_url` | no | Overrides the preset default. Must be `http://` or `https://` |
| `api_key` | no | The key, stored in this file |
| `api_key_env` | no | Name of an environment variable holding the key. Wins over `api_key` |
| `system_prompt` | no | Replaces the default ("You are `<name>`, a friendly assistant in a family chat app called DisPatch Chat…") |
| `max_history_chars` | no | Context budget, default `24000` |

You can hand-edit this file; DisPatch re-reads it when the mtime changes. Run
the panel again for the same provider and it **updates** that bot rather than
creating a second one — leaving the key field blank keeps the stored key.

## Behaviour and limits

- **History.** The most recent messages are sent, newest-first, up to
  `max_history_chars` (default 24 000). Collapsed working-output and system rows
  are excluded; consecutive same-role turns are merged.
- **Timeouts.** 10 s to connect, 120 s to answer. A local model loading for the
  first time can exceed that — it usually succeeds on the retry, once warm.
- **Streaming.** The reply is fetched whole and then delivered through the app's
  existing simulated-streaming path, so it appears the same way an agent's reply
  does. Token-by-token streaming from the provider is not implemented.
- **Errors are always visible.** A bad key, a missing model, an unreachable
  server, an Anthropic refusal — each produces an error in the thread naming the
  cause. Nothing fails silently, and the thread never stays stuck "thinking".
- **Cost is yours.** DisPatch does not meter, cap, or display spend. A cloud
  provider connected to a bot that Safe-Mode devices can reach is a bill with no
  ceiling; the provider's own dashboard is where you set limits.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not reach the provider" | Server not running, wrong port, or (in Docker) `127.0.0.1` resolving to the container |
| "The provider did not answer with JSON" | Base URL points at a web UI, not the API. Add `/v1` |
| "rejected the API key" | Wrong key, revoked key, or a key for a different provider |
| "could not find that model or endpoint" | Model id not available on this account; press Test to list what is |
| "This server has no model list" | The server does not implement `/models`. Type the model name in and test again |
| "The model replied with nothing" | Usually a reasoning model that spent its whole output budget thinking |
| "The model declined to answer that" | Anthropic returned `stop_reason: refusal`. Nothing is misconfigured |

The host dashboard's **Agent backend** card shows a *Direct providers* row when
any of these are configured — see [dashboard.md](dashboard.md).
