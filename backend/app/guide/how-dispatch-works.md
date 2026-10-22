## What DisPatch is

DisPatch is a private chat app for your own AI. You run it on one machine, protect it with a PIN, and everyone you add opens it from their own phone, tablet or laptop with a profile of their own. The conversations live in one SQLite file on your server, and the AI behind each bot is whatever you choose to connect. Nothing here needs an account with us, and nothing is sent anywhere you did not point it.

![Welcome screen](/static/guide/welcome.png)

Turn it on: you already have. Set a PIN under Settings → Account if you have not yet.

## Bots and backends

Every conversation partner is a bot, and every bot has a backend that decides where its answers come from.

A **direct API** bot talks to a model API itself: any OpenAI-compatible service, the Anthropic API, or a model server on your own network such as LM Studio, Ollama or StudioForge. It streams replies, can use lookups, and can fall back to a second model if the first fails. Atlas is one of these. An **agent** bot hands the turn to an agent runtime on the same machine, such as an OpenClaw gateway or a command-line agent, and can use tools, files and long jobs. An **app** bot belongs to an app package. The older `api:` block still works and is shown as a legacy direct API bot, without lookups.

![The Bots tab](/static/guide/bots-tab.png)

Turn it on: Settings → Characters lists every bot. Add one, rename it, or remove it there (the ⋯ menu); removing a bot keeps its threads.

## Connect an AI

Atlas works the moment he has a model behind him.

1. Open Settings → AI & Connections and press Add a connection.
2. Choose a provider. For a local server, enter its address instead of a key.
3. Paste the API key if the provider wants one, press Test, pick a model from the list that comes back and Save.
4. In Settings → Characters, open Atlas's Details and choose that connection.

The key is kept in your computer's keyring when one is available, otherwise in a file only the DisPatch user can read (mode 0600). It is not encrypted, and it is never shown again or sent back to the browser. If you would rather keep it out of both, name an environment variable and DisPatch reads the key from there. A connection belongs to the whole household: every profile's bots can use it, and only an Admin ever sees it. Details per provider are in [llm-providers.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/llm-providers.md).

![Settings, AI models](/static/guide/ai-models.png)

Turn it on: Settings → AI & Connections, add a connection and Test it; then pick it in Atlas's Details (or another direct API bot's).

Quickest of all: Settings → AI & Connections → **Set up everything with one provider**. Pick MiniMax, OpenAI or xAI, paste one key, and chat, pictures and (where the provider has it) knowledge search are set up and tested in one go.

## Lookups

A direct API bot on an OpenAI-compatible provider can look things up while it answers. There are eight: web search, news search, a page fetch, weather, Wikipedia, exchange rates, the current time and a calculator. They only read; none of them can run a program or touch your machine, and the page fetch refuses private and local addresses. Each lookup that runs leaves a small notice row before the answer.

Lookups are on or off per bot, and every profile can use them. An install-wide switch lives in Settings → Plugins & Apps.

Turn it on: Settings → Characters, open the bot's Details and switch Lookups on.

## Quick actions

On a desktop, an empty thread offers a row of ready-made prompts such as summarise, translate, fix grammar or explain. Pick one and a small pill appears in the composer; whatever you type next is sent through that action. The message you see in the thread is still just your own words. The same actions work with every kind of bot.

![Quick actions](/static/guide/quick-actions.png)

You can hide the built-in ones or add your own in `quick-actions.yaml` in the data directory.

Turn it on: nothing to do on desktop; open an empty thread. Edit `quick-actions.yaml` to change the list.

## Profiles and the PIN

DisPatch has three kinds of profile. An **Admin** runs the install: settings, connections, agents, backups, the other profiles. A **User** makes their own bots, copies the household ones and picks what answers for them. A **Basic** profile gets only the household bots an Admin adds. PINs and passwords are optional. The first Admin can't be demoted or deleted, so you can't lock yourself out.

Chats are private. A chat belongs to the profile it was had in, and nobody else can open it in the app, Admins included. The exception is **DisPatch**, the shared profile with no PIN that a locked or signed-out device lands on (or the profile list, if you choose that under Settings → People & Access): everyone who uses it shares its bots and chats. It can never run agents or clone voices.

Every new profile gets its own Atlas. **Reset to default** (Settings → Account) deletes a profile's own bots and chats and starts it over; copying a bot copies its setup, never its chats. There are no daily limits. Images stay unrestricted apart from two hard lines: adult-only depictions, and no sexual imagery of real, identifiable people.

You can mark a device as remembered so it stays signed in for a while. Forgetting it, or locking it, puts it back on the lock screen.

Turn it on: Settings → Account to set your PIN; Settings → People & Access to add profiles and choose what a locked device shows.

## Reactions and pictures

Reactions are small pictures a bot can attach to a message to show a mood, drawn from a pack you build; the built-in pack works with nothing else installed. Pictures on request use the `[[pic:…]]` marker: the bot writes a description, a placeholder appears at once, and the image replaces it when it is ready. DisPatch does not draw anything itself. It sends the request to an image API (MiniMax, OpenAI, xAI Grok, or any host with the OpenAI images endpoint) or to your own image server. API pictures are billed per image, and DisPatch makes one per request.

Refilling a reaction or avatar pool automatically needs an image tool of your own (`DISPATCH_IMAGE_CLI`); pools never use a paid API.

Turn it on: Settings → AI & Connections → Image generation, press Test, Save; then switch Pictures in chat on in a bot's Details (Settings → Characters). The "Set up add-ons" chat and [integrations.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/integrations.md) cover every option.

## Voice chat

Talking to a thread hands-free is an add-on, not part of DisPatch. The package is [DisPatch_Voice](https://github.com/LaserLloyd/DisPatch_Voice). Install it into DisPatch's environment and restart; its speech models download from Settings → Voice:

```
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
```

Speech recognition and voices then run on the same machine. A separate GPU machine can take over for faster, better voices, but it is optional.

![Voice](/static/guide/voice-screen.png)

Turn it on: install the add-on, restart, then open Settings → Voice, switch it on and press Download.

## Apps and tools

The Tools popup holds pages and small apps you add yourself: a static folder, a web address, or an app package with its own backend, page and bot. They are listed in `tools.yaml` and in the `apps/<id>/` folder of your install. Settings → Plugins & Apps shows all of it in one place: installed add-ons with an on/off switch, your apps and tools, and the built-in lookups. An add-on can also add a tab of its own to Settings.

![Plugins tab](/static/guide/plugins-tab.png)

Turn it on: Settings → Plugins & Apps; see [apps.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/apps.md) and [extensions.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/extensions.md) to write your own.

## Agents

An agent bot connects DisPatch to a runtime that can act: read and write files, run commands, work for an hour on a task. DisPatch speaks to an OpenClaw gateway over its WebSocket, or runs an agent command-line tool. Because agents act on real machines, they are off for Users and Basic profiles until an Admin switches them on, and never available to the shared DisPatch profile. The Job Board is the example app: a board of tasks with a bot that works them through an agent.

Turn it on: add an agent bot in Settings → Characters (Add → New character), choose Agent, and name the agent; see [agents.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/agents.md).

## Remote access

Out of the box DisPatch is reachable on your home network. To use it away from home, put it behind Tailscale Serve, a reverse proxy with a real certificate, or a small VPS tunnel. Docker, a Raspberry Pi, Windows and bare Linux all have a guide. Notifications and installing on a phone's home screen need the https address.

Turn it on: pick a route in [remote-access.md](https://github.com/LaserLloyd/DisPatch_Chat/blob/main/docs/remote-access.md), then follow the deploy guide for your platform.

## Where things live

Everything is in one data directory, `~/.local/share/local-chat` by default or `/data` in the container images. It holds `chats.db` (every message), `config.yaml` (your bots and connection settings), `tools.yaml`, `extensions.yaml` and `quick-actions.yaml` when you use them, media folders, and `backups/`, where rotating snapshots are kept. Copy the folder to back it up; move it to change machines.

Turn it on: nothing to do. Settings → System shows backups and storage.
