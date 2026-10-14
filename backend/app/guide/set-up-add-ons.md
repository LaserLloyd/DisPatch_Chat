## What already works

Out of the box, with nothing else installed, you get chat with any AI API (with my lookups: web and news search, weather, Wikipedia, exchange rates, the time and a calculator), quick actions, Safe Mode for family devices, reactions from the built-in pack, the file drop and push notifications on an https address. Everything below is optional. Take the steps in this order and stop wherever you have what you need. Settings → Plugins → **Works with DisPatch** shows the status of each one.

## The quickest start: one key for everything

Open **Settings → AI models → Set up everything with one provider**, choose MiniMax, OpenAI or xAI, paste one key and press Set up. DisPatch connects me for chat, switches on picture generation with the same key, and adds knowledge search where the provider has it. Each capability gets one small live test and a line in the result: ✓ configured, — not offered by that provider, or ✗ with the reason.

MiniMax covers the most with one key (chat and pictures). OpenAI adds embeddings for knowledge search. Pictures are billed per image on every provider, so the test makes exactly one.

## 1. An AI provider

What you get: answers. Without one I can only tell you I'm not connected.

What it needs: an API key from any OpenAI-compatible service (MiniMax, OpenAI, xAI Grok, OpenRouter, DeepSeek, Groq, Mistral, Together), an Anthropic key, or a model server on your own network.

How: **Settings → AI models → Connect an AI**. Pick a provider, paste the key, press Test, then Connect.

## 2. Image generation

What you get: pictures in chat. A bot with pictures switched on writes `[[pic:…]]`, a placeholder appears at once, and the picture replaces it when it is ready.

What it needs: either an image API key (MiniMax image-01, OpenAI, xAI Grok, or any host with the OpenAI images endpoint), or your own image server that speaks MCP, such as a ComfyUI-style server.

How: **Settings → AI models → Image generation**. Pick the provider, press Test (one picture), then Save. Then switch Pictures on for a bot in **Settings → Bots**. API pictures cost money per image; DisPatch makes one per request and never refills anything in the background with a paid API. Reaction and avatar pools use an image command-line tool of your own (`DISPATCH_IMAGE_CLI`).

## 3. StudioForge, for local models

What you get: your own GPUs as an AI server. StudioForge loads a model just in time when a request names it, unloads it when it has been idle, and shares graphics memory with other programs through leases. It speaks the OpenAI API, so I talk to it like any provider.

What it needs: a machine with an NVIDIA GPU and the StudioForge server: [github.com/LaserLloyd/StudioForge](https://github.com/LaserLloyd/StudioForge).

How: **Settings → AI models → Connect an AI**, choose **Local server (llama.cpp / StudioForge)** and enter its `/v1` address. To watch its control panel from inside DisPatch, set `DISPATCH_STUDIOFORGE=1` and `DISPATCH_STUDIOFORGE_URL` and restart.

## 4. OpenClaw, for agent bots

What you get: agent bots that can use tools, read and write files and work on long jobs, plus research hand-offs from direct-API bots and the Job Board app.

What it needs: the OpenClaw agent runtime on the same machine: [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw).

How: put the `openclaw` command on DisPatch's PATH, or run its gateway and set `DISPATCH_GATEWAY_WS=1`, then add an agent bot in **Settings → Bots → Add bot → Agent**. Agents are never reachable from Safe Mode unless you mark them safe.

## 5. DisPatch Voice

What you get: hands-free voice chat with any bot: speech recognition, turn-taking and spoken replies, on this machine.

What it needs: the add-on, about 1.3 GB of speech models, and a few CPU cores: [github.com/LaserLloyd/DisPatch_Voice](https://github.com/LaserLloyd/DisPatch_Voice).

How: install it into DisPatch's environment, fetch the models once, restart, then switch it on in **Settings → Voice**:

```
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
dispatch-voice fetch-models
```

For faster and better voices the add-on can hand speech to a GPU machine running SpeechForge. SpeechForge is not public yet; the add-on works fully without it.

## 6. InfoForge, for offline Wikipedia

What you get: a bot that searches a full offline copy of Wikipedia before it answers and cites the article, with nothing leaving your network.

What it needs: InfoForge and a kiwix server with a Wikipedia file. InfoForge is not public yet. Until it is, a bot can use the online encyclopedia the same way: set `lookup: {kind: wikipedia}` in its `advisor:` block in `config.yaml`. That sends the user's words to wikipedia.org, also from Safe Mode devices.

How: put the `infoforge` command on PATH and set `lookup: {kind: infoforge}` on the bot.

## 7. MailForge

What you get: an Emails tab for a local email assistant that sorts, screens and drafts replies, and never sends anything without your click.

What it needs: MailForge running on the same machine: [github.com/LaserLloyd/MailForge](https://github.com/LaserLloyd/MailForge).

How: run MailForge, then restart DisPatch. With `DISPATCH_MAIL=auto` (the default) DisPatch finds it by its data directory and shows the tab to unlocked devices.

## Something else?

Any OpenAI-compatible host works as a provider: choose **Custom** and enter its address. To add a lookup tool or a whole feature of your own, write an add-on; the guide is [extensions.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/extensions.md), and every option above is described in [integrations.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/integrations.md).
