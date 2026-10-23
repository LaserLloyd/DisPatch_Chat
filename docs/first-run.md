# First run

A fresh install opens a six-step wizard in the browser. It appears on an install that is not set up yet (no Admin,
or an Admin but no model connection), can be put off with **Set up later** (until you reload the page), and goes
away for good when you press **Finish**. Every step is an API call, so a script or a test can do the same.

| # | Step | What it does | API |
|---|---|---|---|
| 1 | **Your account** | Creates the first Admin (the protected one). Allowed from the computer DisPatch runs on, or from anywhere with the one-time *setup token* printed in the server log (`docker compose logs app` in Docker). At least a password (8+ characters) or a PIN (4+) | `POST /api/auth/setup` |
| 2 | **A model** | Looks for StudioForge, LM Studio and Ollama on this computer, or takes an address and key; **Test and save** tests it, makes it the default and points the default character at it | `POST /api/wizard/detect`, `POST /api/wizard/connection` |
| 3 | **Voice** (optional) | A link to Settings -> Voice; voice chat is a separate add-on | - |
| 4 | **People** (optional) | Creates User and Basic profiles (a User gets its own Atlas; a Basic profile gets the household bots you pick) and makes sure the shared **DisPatch** profile exists: a User with no PIN that locked and unsigned devices land on. You, the first Admin, use the household Atlas | `POST /api/wizard/family` |
| 5 | **Network** | *This computer only* (the default) or *my home network*, with the HTTPS note | `POST /api/wizard/network` (Admins: `admin_system`) |
| 6 | **Done** | A checklist of what is left (a working model, encrypted backups, saving your recovery code) | `GET /api/wizard`, `POST /api/wizard/finish` |

`GET /api/wizard` answers `{needed, claimed}` to anyone and the full checklist to someone who may manage settings.
`POST /api/wizard/step/{voice|family}` with `{"status": "skipped"}` records a skipped optional step. (The route keeps its 3.x name `family`; the screen is called People.)

Locked out later (lost the first Admin's password and PIN)? `python -m app admin reset` or a `RESET_LOGIN` file puts the install back at this
step with a new setup token, keeping chats and profiles: [accounts.md](accounts.md).

## Network exposure, plainly

The wizard records your decision (`system.network_exposure`) and tells you what to do about it; **the address DisPatch
listens on is set by how you start it**:

- *This computer only*: `uvicorn ... --host 127.0.0.1` (the default); in Docker leave `BIND_ADDR` at `127.0.0.1`.
- *My home network*: `--host 0.0.0.0` (Docker: `BIND_ADDR=0.0.0.0`). The wizard refuses to choose this before an Admin
  exists. Phones and tablets need **HTTPS** for install-to-home-screen, push notifications and the microphone: put DisPatch
  behind a reverse proxy (the compose file has a Caddy profile) or Tailscale Serve - see [remote-access.md](remote-access.md).
  Set `system.public_url` to the https address so the Health tab knows the connection is encrypted.

If what you chose and what DisPatch reports it is listening on disagree, the wizard says so.

## Upgrading from 2.x

An existing install that already has an Admin/PIN and a connected character never sees the wizard. One without a PIN gets
it once (an unclaimed install is only usable from its own computer since 3.0 - see the CHANGELOG), and can finish it in a
minute. On upgrade to 4.0 the migration keeps every profile and chat (Owner becomes the protected Admin, Member a User,
Restricted a Basic profile) and, when it can tell which profile was the shared one, makes that the shared default profile
([security.md](security.md#upgrading-to-40)).
