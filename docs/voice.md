# Voice chat: install, data and what is left

Voice chat is the separate **[DisPatch Voice](https://github.com/LaserLloyd/DisPatch_Voice)**
add-on (MIT). DisPatch is the host: it loads the add-on, gates its routes and
sockets, and gives it folders, a credential store and a settings writer
([extensions.md](extensions.md#host-services-folders-credentials-settings)). The
add-on's own documentation (`docs/voice-chat/`) covers how a conversation works.
This page is about getting it running, where its data lives, and how many things a
person has to touch.

## Docker: one build flag, then one Settings page

```bash
# .env:  VOICE=1        (optional: VOICE_REF=refs/tags/v1.6.0 once that is released)
docker compose up -d --build
```

Then, in the app, **Settings → Voice**: switch voice on and press **Download**.
That is the whole setup. There is no terminal step after the image is built and no
restart after the download: the add-on registers its speech engine as soon as the
models are on disk.

What `VOICE=1` puts in the image:

| | |
|---|---|
| the add-on | installed from `VOICE_REF` (default: `refs/heads/main` of the add-on, the 1.6.0 line; the moving `v1` tag still names 1.5.0), or from a local directory (`VOICE_SRC=local` with `--build-context voice-src=<dir>`, see [extensions.md](extensions.md#1-install-it)) |
| `ffmpeg` | decodes the clip you upload to clone a voice |
| `libopus0` | the codec of the low-bandwidth voice link (without it the link stays on raw PCM) |
| the models | **not** baked in; the page downloads them into the data volume |

`ffmpeg` is the expensive part: on Debian it brings a long list of libraries with
it, so a `VOICE=1` image is about 0.84 GB larger than the base image (measured on
the 3.0 refactor build: 872 MB without, 1.71 GB with). A leaner ffmpeg (a pinned
static build copied in from another stage) would cut most of it; that is a choice
about a third-party binary, so it is left to a decision.

### Where the data lives (all under `/data`, nothing in the image layer)

| path | what | survives an image update |
|---|---|---|
| `/data/models/speech/`, `/data/models/chatterbox/` | the downloaded models (~1.4 GB) | yes |
| `/data/voice.yaml` | the add-on's settings | yes |
| `/data/voices/` | recorded and cloned voices | yes |
| `/data/ext/voice/secrets.json` (0600) | the GPU-rig token, in the host credential store | yes |
| `/data/ext/voice/cursor.sqlite` | what each voice chat has submitted | yes |

`/data/models` was `/data/speech-models` before 3.0. DisPatch moves the old folder
at start, once and without overwriting; see
[extensions.md → The models folder](extensions.md#the-models-folder).

The container runs with a read-only root (`read_only: true`, `/tmp` on tmpfs): the
token, the settings and the models are all under `/data`, and nothing writes to the
home folder.

## Without Docker

```bash
uv pip install --python .venv/bin/python "dispatch-voice @ git+https://github.com/LaserLloyd/DisPatch_Voice@v1"
# restart DisPatch, then Settings → Voice → switch on → Download
```

You also need `ffmpeg` (voice cloning) and, optionally, libopus on the host; Settings
→ Voice names what is missing, with the install command for your system.

## Who can use it

| Who | What |
|---|---|
| Owner, Admin | Everything: the switch, server settings, voices and cloning, the lab, the GPU rig, models. |
| Member | Voice calls with the characters they can chat with, Settings → Voice (once the add-on is installed), the voice list and samples (`use_voice`). Changing the server's voice setup is refused (`manage_plugins`). |
| Restricted (a signed-in child, a device bound to one, or a guest) | Voice calls on the characters their audience allows, from the call screen only; every turn runs locked and counts against that profile's (or, for a guest, the default) budgets and quiet hours. |

## Touch points for CPU voice

One thing a person has to *do*, counted from a fresh install in the order they happen.
A restart counts, a click on the first page counts. The audit started at 4 (install
the package, fetch the models, restart, switch on) and the 1.6.0 review at 4 again
(install, restart, one Settings page, a second restart after the download).

| install | touch points now |
|---|---|
| **Docker, image built with `VOICE=1`** | **1**: Settings → Voice (switch on, Download). The build flag belongs to building the image; there is no step after it. |
| Docker, image built without voice | 2: rebuild with `VOICE=1`, then Settings → Voice |
| `uv` on a host | 3: install the package, restart, Settings → Voice |
| a GPU rig on top | the address and the token are two more fields on the same Settings page |

What was exercised on a `VOICE=1` image (a scratch volume, port 18765, read-only
root): the voice status, settings schema, providers and `/ext/voice/*` assets; a
model download started and cancelled with no partial file left; the rig token saved
under the read-only root; a setting changed; the container removed and started again
on the same volume with the token, the setting and the downloaded file intact; and a
volume carrying the old `speech-models` folder, which moved to `models`. Nobody was
timed, and the full 1.4 GB download was not run.

### What is left

1. **The `VOICE=1` choice is made when the image is built.** The published image has
   no voice add-on and a read-only image cannot install one at run time. Installing
   the add-on from Settings → Plugins & Apps (and shipping a voice image variant) is the
   plugin work, not this one.
2. **Cloned-voice management still waits for one restart** if the models were
   downloaded *after* DisPatch started: the `/api/voice/profiles` router is sealed
   when the add-on sets up. Speaking replies, in the stock voice, works without it;
   the page says so after the download.
3. **The image size** above. And the first download needs the network from inside the
   container (GitHub and Hugging Face).
4. **The add-on pin.** `VOICE_REF` points at `main` until 1.6.0 is tagged; the `v1`
   install command on the "add voice" page installs 1.5.0 until the tag moves. Both
   are one-line changes at release (the TODO in the Dockerfile and `VOICE_TAG` in
   `js/voice-absent.js`).
5. **Two status hints still name the terminal** (`dispatch-voice fetch-models` in the
   add-on's `missing` list). They are hints beside a working button, not steps.
6. **The first-run owner account** is DisPatch's own step, not voice's.

## Related

- [extensions.md](extensions.md): the host side of every extension, including the
  folders, the credential store and the settings writer used above.
- [deploy-docker.md](deploy-docker.md): the compose setup, volumes and backups.
- [voice-drive-mode.md](voice-drive-mode.md): the old name.
