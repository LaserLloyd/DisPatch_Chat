## What DisPatch is

DisPatch is a private chat app for your own AI. You run it on one machine, protect it with a PIN, and everyone in the house opens it from their own phone, tablet or laptop. The conversations live in one SQLite file on your server, and the AI behind each bot is whatever you choose to connect. Nothing here needs an account with us, and nothing is sent anywhere you did not point it.

![Welcome screen](/static/guide/welcome.png)

Turn it on: you already have. Set a PIN under Settings → Security if you have not yet.

## Bots and backends

Every conversation partner is a bot, and every bot has a backend that decides where its answers come from.

A **direct API** bot talks to a model API itself: any OpenAI-compatible service, the Anthropic API, or a model server on your own network such as LM Studio, Ollama or StudioForge. It streams replies, can use lookups, and can fall back to a second model if the first fails. Atlas is one of these. An **agent** bot hands the turn to an agent runtime on the same machine, such as an OpenClaw gateway or a command-line agent, and can use tools, files and long jobs. An **app** bot belongs to an app package. The older `api:` block still works and is shown as a legacy direct API bot, without lookups.

![The Bots tab](/static/guide/bots-tab.png)

Turn it on: Settings → Bots lists every bot. Add one, rename it, or remove it there; removing a bot keeps its threads.

## Connect an AI

Atlas works the moment he has a model behind him.

1. Open Settings → AI models.
2. Choose a provider. For a local server, enter its address instead of a key.
3. Paste the API key if the provider wants one, then press Test.
4. Pick a model from the list that comes back and press Connect.

The key is written to `config.yaml` in your data directory, which is set to owner-only (0600). It is not encrypted, and it is never shown again or sent back to the browser. If you would rather keep it out of the file, name an environment variable and DisPatch reads the key from there. Details per provider are in [llm-providers.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/llm-providers.md).

![Settings, AI models](/static/guide/ai-models.png)

Turn it on: Settings → AI models, choose Atlas (or another direct API bot) as the target, Test, Connect.

## Lookups

A direct API bot can look things up while it answers. There are eight: web search, news search, a page fetch, weather, Wikipedia, exchange rates, the current time and a calculator. They only read; none of them can run a program or touch your machine, and the page fetch refuses private and local addresses. Each lookup that runs leaves a small notice row before the answer.

Lookups are on or off per bot. Safe Mode devices get a subset, by default all eight, and you can narrow it with `tools_locked`. An install-wide switch lives in Settings → Plugins.

Turn it on: Settings → Bots, open the bot and switch Lookups on.

## Quick actions

On a desktop, an empty thread offers a row of ready-made prompts such as summarise, translate, fix grammar or explain. Pick one and a small pill appears in the composer; whatever you type next is sent through that action. The message you see in the thread is still just your own words. The same actions work with every kind of bot.

![Quick actions](/static/guide/quick-actions.png)

You can hide the built-in ones or add your own in `quick-actions.yaml` in the data directory.

Turn it on: nothing to do on desktop; open an empty thread. Edit `quick-actions.yaml` to change the list.

## Safe Mode and the PIN

DisPatch has two tiers. With the PIN entered, a device is unlocked and can do everything: settings, files, agents, deletion. Without it, a device is in Safe Mode: it can read and send, but only to bots you marked safe, with media stripped and every changing action refused by the server. That is the setting for a kitchen tablet or a child's phone.

You can mark a device as remembered so it stays unlocked for a while. Forgetting it, or locking it, puts it back in Safe Mode. A bot is reachable from a locked device only if you mark it safe, and a safe direct API bot answers there with the lookups you allow it (`tools_locked`; the eight built-in ones by default, add-on tools only when you name them).

![Safe Mode](/static/guide/safe-mode.png)

Turn it on: Settings → Security to set the PIN; Settings → Bots to mark a bot safe.

## Reactions and pictures

Reactions are small pictures a bot can attach to a message to show a mood, drawn from a pack you build. Pictures on request use the `[[pic:…]]` marker: the bot writes a prompt, a placeholder appears at once, and the image replaces it when it is ready. Both need an image server, such as ClawForge or another ComfyUI-style service; DisPatch does not draw anything itself. Without one, the bots simply never offer pictures.

Turn it on: configure image jobs as described in [configuration.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/configuration.md), then enable Reactions or Pool per bot in Settings → Bots.

## Voice chat

Talking to a thread hands-free is an add-on, not part of DisPatch. The package is [DisPatch_Voice](https://github.com/LaserLloyd/DisPatch_Voice). Install it into DisPatch's environment, fetch its speech models once, and restart:

```
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
dispatch-voice fetch-models
```

Speech recognition and voices then run on the same machine. A separate GPU machine can take over for faster, better voices, but it is optional.

![Voice](/static/guide/voice-screen.png)

Turn it on: install the add-on, restart, then open Settings → Voice and switch it on.

## Apps and tools

The Tools popup holds pages and small apps you add yourself: a static folder, a web address, or an app package with its own backend, page and bot. They are listed in `tools.yaml` and in the `apps/<id>/` folder of your install. Settings → Plugins shows all of it in one place: installed add-ons with an on/off switch, your apps and tools, and the built-in lookups. An add-on can also add a tab of its own to Settings.

![Plugins tab](/static/guide/plugins-tab.png)

Turn it on: Settings → Plugins; see [apps.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/apps.md) and [extensions.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/extensions.md) to write your own.

## Agents

An agent bot connects DisPatch to a runtime that can act: read and write files, run commands, work for an hour on a task. DisPatch speaks to an OpenClaw gateway over its WebSocket, or runs an agent command-line tool. Because agents act on real machines, they are never reachable from Safe Mode unless you say so. The Job Board is the example app: a board of tasks with a bot that works them through an agent.

Turn it on: add an agent bot in Settings → Bots, choose Agent, and name the agent; see [agents.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/agents.md).

## Remote access

Out of the box DisPatch is reachable on your home network. To use it away from home, put it behind Tailscale Serve, a reverse proxy with a real certificate, or a small VPS tunnel. Docker, a Raspberry Pi, Windows and bare Linux all have a guide. Notifications and installing on a phone's home screen need the https address.

Turn it on: pick a route in [remote-access.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/remote-access.md), then follow the deploy guide for your platform.

## Where things live

Everything is in one data directory, `~/.local/share/local-chat` by default or `/data` in the container images. It holds `chats.db` (every message), `config.yaml` (your bots and connection settings), `tools.yaml`, `extensions.yaml` and `quick-actions.yaml` when you use them, media folders, and `backups/`, where rotating snapshots are kept. Copy the folder to back it up; move it to change machines.

Turn it on: nothing to do. Check the Health tab to see backups and storage.
